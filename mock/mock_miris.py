"""A local stand-in for the Miris API and its upload storage, for tests and demos.

Implements the slice of the Miris Public API the add-on uses (paths and field names from
Miris's published OpenAPI spec) plus just enough S3 to accept SigV4-signed uploads,
including multipart. It is strict where the real thing would be strict:

  * every storage request's SigV4 signature is recomputed and must match;
  * the session token must be the one issued for that asset;
  * x-amz-content-sha256 must match the body that arrived;
  * the completion manifest must match what is actually in storage;
  * "processing" runs the same bundle checks as the add-on, and reports failures as
    job_errors with Miris's error keys.

It also records whether the Miris API key ever showed up on a storage request, which
would mean the client leaked it to a third-party host.

    python mock/mock_miris.py --port 8787 --api-key dev-key
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "stream_ready"), ROOT]
from core import bundle, sigv4  # noqa: E402
from core.findings import MIRIS_CODES  # noqa: E402

AUTH_RE = re.compile(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
                     r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})")


class MirisState:
    def __init__(self, api_key="dev-key", *, region="us-west-2", fail_api=0, throttle_storage=0,
                 retry_after=0, processing_polls=2, log=None):
        self.api_key = api_key
        self.region = region
        self.fail_api = fail_api  # first N API calls answer 503
        self.throttle_storage = throttle_storage  # first N storage writes answer 429 + Retry-After
        self.retry_after = retry_after  # seconds, sent with every 429/503
        self.processing_polls = processing_polls  # GETs before a valid asset turns streamable
        self.log = log or (lambda msg: print(msg, flush=True))
        self.lock = threading.Lock()
        self.assets = {}
        self.objects = {}  # (bucket, key) -> bytes
        self.multipart = {}  # upload id -> {"bucket", "key", "parts": {n: bytes}}
        self.viewer_keys = {}
        self.leaked_api_key = False
        self.storage_requests = 0
        self.base_url = ""


def serve_in_thread(state, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(state))
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, name="mock-miris", daemon=True).start()
    return server, state.base_url


def _handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

        # --- routing ----------------------------------------------------------------

        def _route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            self.body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            try:
                if parts.path.startswith("/storage/"):
                    self._storage(method, parts)
                elif parts.path.startswith("/v1/"):
                    self._api(method, parts.path)
                else:
                    self._problem(404, "not_found", "Not Found")
            except Exception as exc:  # a mock that hangs on a bug is worse than one that says so
                self._problem(500, "mock_bug", f"mock server error: {exc!r}")

        def _api(self, method, path):
            if self.headers.get("Miris-API-Key") != state.api_key:
                return self._problem(401, "invalid_api_key", "Unauthorized", "The API key is missing or invalid.")
            with state.lock:
                if state.fail_api > 0:
                    state.fail_api -= 1
                    return self._problem(503, "unavailable", "Service Unavailable",
                                         headers={"Retry-After": str(state.retry_after)})

            if method == "GET" and path == "/v1/customer_info":
                return self._json(200, {"id": "00000000-0000-0000-0000-00000000c0de", "name": "Mock workspace"})
            if method == "POST" and path == "/v1/asset":
                return self._start_upload()
            m = re.fullmatch(r"/v1/asset/upload/([0-9a-f-]{36})", path)
            if method == "PUT" and m:
                return self._upload_status(m.group(1))
            m = re.fullmatch(r"/v1/asset/all/([0-9a-f-]{36})", path)
            if method == "GET" and m:
                return self._asset(m.group(1))
            if method == "POST" and path == "/v1/asset-viewer-key":
                return self._viewer_key()
            return self._problem(404, "not_found", "Not Found")

        # --- API endpoints ------------------------------------------------------------

        def _start_upload(self):
            body = self._body_json()
            missing = [k for k in ("input_type", "output_type", "name", "content_path") if not body.get(k)]
            if missing:
                return self._problem(400, "missing_field", "Bad Request", f"missing: {', '.join(missing)}")
            if body["output_type"] != "static":
                return self._problem(422, "invalid_output_type", "Unprocessable Content", "output_type must be static")
            asset_id = str(uuid.uuid4())
            asset = {
                "id": asset_id,
                "name": body["name"],
                "tags": body.get("tags") or [],
                "declared": (body.get("file_count"), body.get("total_bytes")),
                "creds": ("ASIA" + secrets.token_hex(8).upper(), secrets.token_urlsafe(30), secrets.token_urlsafe(40)),
                "bucket": "mock-bucket",
                "prefix": f"uploads/{asset_id}/",
                "status": "pending",
                "state": "uploading",
                "job_process_status": "pending",
                "job_errors": None,
                "polls_left": state.processing_polls,
                "created_at": _now(),
            }
            with state.lock:
                state.assets[asset_id] = asset
            access, secret, token = asset["creds"]
            state.log(f"POST /v1/asset -> {asset_id} ({body['name']})")
            self._json(200, {
                "id": asset_id,
                "endpoint_type": "s3",
                "endpoint_url": state.base_url + "/storage",
                "region": state.region,
                "access_key_id": access,
                "secret_key": secret,
                "session_token": token,
                "upload_path": f"s3://{asset['bucket']}/{asset['prefix']}",
                "total_bytes": body.get("total_bytes") or 0,
                "file_count": body.get("file_count") or 0,
            })

        def _upload_status(self, asset_id):
            asset = state.assets.get(asset_id)
            if not asset:
                return self._problem(404, "asset_not_found", "Not Found")
            body = self._body_json()
            status = body.get("status")
            if status not in ("pending", "processing", "completed", "failed", "canceled"):
                return self._problem(400, "invalid_status", "Bad Request")
            if status == "completed":
                stored = {k[len(asset["prefix"]):]: len(v) for (b, k), v in state.objects.items()
                          if b == asset["bucket"] and k.startswith(asset["prefix"])}
                for entry in body.get("manifest") or []:
                    got = stored.get(entry["file_name"])
                    if got != entry["size"]:
                        return self._problem(
                            422, "manifest_mismatch", "Unprocessable Content",
                            f"{entry['file_name']}: expected {entry['size']} bytes, storage has {got}",
                        )
                if not stored:
                    return self._problem(422, "empty_upload", "Unprocessable Content", "nothing was uploaded")
                self._process(asset)
            else:
                asset["status"] = asset["job_process_status"] = status
                asset["state"] = status
            state.log(f"PUT /v1/asset/upload/{asset_id[:8]} status={status}")
            self._json(200, {
                "id": asset_id, "name": asset["name"], "tags": asset["tags"], "file_count": len(body.get("manifest") or []),
                "total_bytes": sum(e["size"] for e in body.get("manifest") or []), "thumbnail": "",
                "status": asset["status"], "process_status": asset["job_process_status"],
                "created_at": asset["created_at"], "updated_at": _now(),
            })

        def _process(self, asset):
            # Rebuild the uploaded folder and run the same checks a real ingest would.
            workdir = tempfile.mkdtemp(prefix="mock_miris_")
            try:
                for (bucket, key), data in list(state.objects.items()):
                    if bucket == asset["bucket"] and key.startswith(asset["prefix"]):
                        path = os.path.join(workdir, *key[len(asset["prefix"]):].split("/"))
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                report = bundle.check(workdir)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            errors = [f for f in report["findings"] if f.severity == "error" and f.code in MIRIS_CODES]
            asset["status"] = "completed"
            if errors:
                asset["job_process_status"] = "failed"
                asset["state"] = "error"
                asset["job_errors"] = [{"error_key": f.code, "error_description": f.message, "context": {"target": f.target}}
                                       for f in errors]
            else:
                asset["job_process_status"] = "processing"
                asset["state"] = "processing_streamable"

        def _asset(self, asset_id):
            asset = state.assets.get(asset_id)
            if not asset:
                return self._problem(404, "asset_not_found", "Not Found")
            if asset["state"] == "processing_streamable":
                asset["polls_left"] -= 1
                if asset["polls_left"] <= 0:
                    asset["state"], asset["job_process_status"] = "streamable", "completed"
            self._json(200, {
                "id": asset_id, "name": asset["name"], "tags": asset["tags"], "is_published": False,
                "created_at": asset["created_at"], "updated_at": _now(), "state": asset["state"],
                "job_process_status": asset["job_process_status"], "job_errors": asset["job_errors"],
                "job_file_count": 0, "job_total_bytes": 0, "total_views": 0, "jwt": "mock.jwt.token",
            })

        def _viewer_key(self):
            body = self._body_json()
            if not body.get("name") or body.get("tags") is None:
                return self._problem(400, "missing_field", "Bad Request", "name and tags are required")
            key_id, key = str(uuid.uuid4()), secrets.token_urlsafe(32)
            state.viewer_keys[key_id] = body
            self._json(200, {"id": key_id, "person_id": str(uuid.uuid4()), "name": body["name"], "key": key,
                             "key_prefix": key[:6], "asset_ids": body.get("asset_ids"), "tags": body["tags"],
                             "created_at": _now(), "updated_at": _now()})

        # --- storage ------------------------------------------------------------------

        def _storage(self, method, parts):
            with state.lock:
                state.storage_requests += 1
                if self.headers.get("Miris-API-Key") or self.headers.get("Miris-Integration-Key"):
                    state.leaked_api_key = True
            asset, error = self._verify_sigv4(method, parts)
            if error:
                return self._s3_error(403, error[0], error[1])

            path = unquote(parts.path)[len("/storage/"):]
            bucket, _, key = path.partition("/")
            if bucket != asset["bucket"] or not key.startswith(asset["prefix"]):
                return self._s3_error(403, "AccessDenied", "credentials are scoped to another prefix")
            query = parse_qs(parts.query, keep_blank_values=True)

            if method in ("PUT", "POST") and state.throttle_storage > 0:
                with state.lock:
                    state.throttle_storage -= 1
                return self._s3_error(429, "SlowDown", "Please reduce your request rate.",
                                      {"Retry-After": str(state.retry_after)})

            if method == "PUT" and "partNumber" in query:
                upload = state.multipart.get(query["uploadId"][0])
                if not upload:
                    return self._s3_error(404, "NoSuchUpload", "unknown uploadId")
                upload["parts"][int(query["partNumber"][0])] = self.body
                return self._raw(200, b"", {"ETag": f'"{hashlib.md5(self.body).hexdigest()}"'})
            if method == "PUT":
                state.objects[(bucket, key)] = self.body
                state.log(f"  PUT {key[len(asset['prefix']):]} ({len(self.body):,} bytes, signature ok)")
                return self._raw(200, b"", {"ETag": f'"{hashlib.md5(self.body).hexdigest()}"'})
            if method == "POST" and "uploads" in query:
                upload_id = secrets.token_hex(12)
                state.multipart[upload_id] = {"bucket": bucket, "key": key, "parts": {}}
                xml = (f"<InitiateMultipartUploadResult><Bucket>{bucket}</Bucket><Key>{key}</Key>"
                       f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>")
                return self._raw(200, xml.encode(), {"Content-Type": "application/xml"})
            if method == "POST" and "uploadId" in query:
                upload = state.multipart.pop(query["uploadId"][0], None)
                if not upload:
                    return self._s3_error(404, "NoSuchUpload", "unknown uploadId")
                numbers = [int(n) for n in re.findall(r"<PartNumber>(\d+)</PartNumber>", self.body.decode())]
                if numbers != sorted(upload["parts"]):
                    return self._s3_error(400, "InvalidPart", f"parts listed {numbers}, uploaded {sorted(upload['parts'])}")
                state.objects[(bucket, key)] = b"".join(upload["parts"][n] for n in numbers)
                state.log(f"  multipart {key[len(asset['prefix']):]} ({len(numbers)} parts, signature ok)")
                return self._raw(200, b"<CompleteMultipartUploadResult/>", {"Content-Type": "application/xml"})
            if method == "DELETE" and "uploadId" in query:
                state.multipart.pop(query["uploadId"][0], None)
                return self._raw(204, b"")
            return self._s3_error(405, "MethodNotAllowed", f"{method} not supported by the mock")

        def _verify_sigv4(self, method, parts):
            auth = self.headers.get("Authorization") or ""
            m = AUTH_RE.fullmatch(auth)
            if not m:
                return None, ("AccessDenied", "missing or malformed SigV4 Authorization header")
            access, datestamp, region, signed_names, signature = m.groups()
            asset = next((a for a in state.assets.values() if a["creds"][0] == access), None)
            if not asset:
                return None, ("InvalidAccessKeyId", "unknown access key")
            _, secret, token = asset["creds"]
            if self.headers.get("x-amz-security-token") != token:
                return None, ("InvalidToken", "session token missing or does not match the access key")
            payload_hash = self.headers.get("x-amz-content-sha256") or ""
            if payload_hash != sigv4.sha256_hex(self.body):
                return None, ("XAmzContentSHA256Mismatch", "body does not match x-amz-content-sha256")
            amz_date = self.headers.get("x-amz-date") or ""
            when = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            implicit = {"host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token"}
            extra = {n: self.headers.get(n, "") for n in signed_names.split(";") if n not in implicit}
            url = f"http://{self.headers.get('Host')}{unquote(parts.path)}" + (f"?{parts.query}" if parts.query else "")
            expected = sigv4.sign(method, url, extra, payload_hash, access_key=access, secret_key=secret,
                                  session_token=token, region=region, now=when)
            if expected["Authorization"].rsplit("Signature=", 1)[1] != signature:
                return None, ("SignatureDoesNotMatch", "the request signature we calculated does not match")
            return asset, None

        # --- responses ----------------------------------------------------------------

        def _body_json(self):
            try:
                return json.loads(self.body or b"{}")
            except ValueError:
                return {}

        def _json(self, status, payload):
            self._raw(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})

        def _problem(self, status, reason, title, detail=None, headers=None):
            body = {"type": "about:blank", "status": status, "title": title, "reason": reason,
                    "code": f"MOCK-{status}", "detail": detail or title, "user_detail": detail or title}
            self._raw(status, json.dumps(body).encode(), dict({"Content-Type": "application/problem+json"}, **(headers or {})))

        def _s3_error(self, status, code, message, headers=None):
            xml = f"<Error><Code>{code}</Code><Message>{message}</Message></Error>".encode()
            state.log(f"  storage {status} {code}: {message}")
            self._raw(status, xml, dict({"Content-Type": "application/xml"}, **(headers or {})))

        def _raw(self, status, body, headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if status != 204:
                self.wfile.write(body)

    return Handler


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--api-key", default="dev-key")
    parser.add_argument("--fail-api", type=int, default=0, help="answer the first N API calls with 503")
    parser.add_argument("--throttle-storage", type=int, default=0, help="answer the first N storage writes with 429")
    args = parser.parse_args()
    st = MirisState(args.api_key, fail_api=args.fail_api, throttle_storage=args.throttle_storage)
    srv, base = serve_in_thread(st, args.port)
    print(f"mock Miris API on {base}  (API key: {args.api_key})")
    print("In Blender: Preferences > Add-ons > Stream Ready > API URL = " + base)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
