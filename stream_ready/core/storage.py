# SPDX-License-Identifier: GPL-3.0-or-later
"""Upload files to S3-compatible storage with the temporary credentials Miris issues.

Small files go up in one PUT. Large ones use multipart upload, so a dropped connection
at 90% of a 2 GB texture set costs one 16 MiB part, not the whole file.
"""

import os
import re
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

from . import sigv4
from .http import HttpError

MULTIPART_THRESHOLD = 64 * 1024 * 1024
PART_SIZE = 16 * 1024 * 1024  # S3 minimum is 5 MiB for every part but the last
DNS_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class Credentials:
    def __init__(self, access_key, secret_key, session_token, region):
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.region = region or "us-east-1"

    def __repr__(self):
        # Never let a secret reach a log line or a traceback by accident.
        return f"Credentials(access_key={self.access_key[:4]}…, region={self.region})"


def parse_upload_path(upload_path):
    """'s3://bucket/a/b', 'bucket/a/b', or '/bucket/a/b' -> ('bucket', 'a/b/')."""
    path = re.sub(r"^[a-z0-9]+://", "", upload_path.strip()).lstrip("/")
    bucket, _, prefix = path.partition("/")
    if not bucket:
        raise ValueError(f"upload_path has no bucket: {upload_path!r}")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


class S3Uploader:
    def __init__(self, transport, endpoint_url, bucket, creds, *, threshold=MULTIPART_THRESHOLD,
                 part_size=PART_SIZE):
        self.transport = transport
        self.endpoint = endpoint_url.rstrip("/")
        self.bucket = bucket
        self.creds = creds
        self.threshold = threshold
        self.part_size = part_size

    def object_url(self, key, query=""):
        # Virtual-hosted style on AWS (path style is deprecated there for new buckets);
        # path style everywhere else, which is what MinIO, R2, and local mocks expect.
        parts = urlsplit(self.endpoint)
        key_path = quote(key, safe="/-_.~")
        if parts.hostname and parts.hostname.endswith("amazonaws.com") and DNS_BUCKET.match(self.bucket):
            url = f"{parts.scheme}://{self.bucket}.{parts.netloc}/{key_path}"
        else:
            url = f"{self.endpoint}/{self.bucket}/{key_path}"
        return f"{url}?{query}" if query else url

    def upload_file(self, key, path, progress=None):
        size = os.path.getsize(path)
        if size >= self.threshold:
            return self._multipart(key, path, size, progress)
        with open(path, "rb") as f:
            data = f.read()
        digest = sigv4.sha256_hex(data)
        url = self.object_url(key)

        def build(_attempt):
            headers = {"Content-Type": _content_type(key), "Content-Length": str(len(data))}
            return "PUT", url, self._sign("PUT", url, headers, digest), data

        self.transport.send(f"PUT {key}", build)
        if progress:
            progress(size)
        return size

    def _multipart(self, key, path, size, progress):
        url = self.object_url(key, "uploads")
        resp = self.transport.send(f"POST {key}?uploads",
                                   lambda _: ("POST", url, self._sign("POST", url, {}, sigv4.EMPTY_SHA256), b""))
        upload_id = _xml_value(resp.body, "UploadId")
        if not upload_id:
            raise HttpError(f"{key}: storage did not return an UploadId")

        etags = []
        try:
            with open(path, "rb") as f:
                number = 0
                while True:
                    chunk = f.read(self.part_size)
                    if not chunk:
                        break
                    number += 1
                    etags.append((number, self._part(key, upload_id, number, chunk)))
                    if progress:
                        progress(len(chunk))
            body = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{escape(tag)}</ETag></Part>" for n, tag in etags
            ) + "</CompleteMultipartUpload>"
            data = body.encode("utf-8")
            url = self.object_url(key, f"uploadId={quote(upload_id, safe='')}")
            digest = sigv4.sha256_hex(data)
            self.transport.send(
                f"complete {key}",
                lambda _: ("POST", url, self._sign("POST", url, {"Content-Type": "application/xml"}, digest), data),
            )
        except BaseException:
            self._abort(key, upload_id)  # don't leave orphaned parts billing someone's bucket
            raise
        return size

    def _part(self, key, upload_id, number, chunk):
        url = self.object_url(key, f"partNumber={number}&uploadId={quote(upload_id, safe='')}")
        digest = sigv4.sha256_hex(chunk)

        def build(_attempt):
            headers = {"Content-Length": str(len(chunk))}
            return "PUT", url, self._sign("PUT", url, headers, digest), chunk

        resp = self.transport.send(f"PUT {key} part {number}", build)
        etag = resp.headers.get("ETag")
        if not etag:
            raise HttpError(f"{key} part {number}: storage returned no ETag")
        return etag

    def _abort(self, key, upload_id):
        url = self.object_url(key, f"uploadId={quote(upload_id, safe='')}")
        try:
            self.transport.send(f"abort {key}",
                                lambda _: ("DELETE", url, self._sign("DELETE", url, {}, sigv4.EMPTY_SHA256), None))
        except Exception:
            pass  # best effort; the original error is the one worth reporting

    def _sign(self, method, url, headers, payload_hash):
        c = self.creds
        return sigv4.sign(method, url, headers, payload_hash, access_key=c.access_key,
                          secret_key=c.secret_key, session_token=c.session_token, region=c.region)


def _content_type(key):
    ext = os.path.splitext(key)[1].lower()
    return {
        ".usda": "text/plain",
        ".usd": "application/octet-stream",
        ".usdc": "application/octet-stream",
        ".usdz": "model/vnd.usdz+zip",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".exr": "image/x-exr",
        ".hdr": "image/vnd.radiance",
    }.get(ext, "application/octet-stream")


def _xml_value(body, tag):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    match = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
    return match.group(1) if match else ""
