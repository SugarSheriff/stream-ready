# SPDX-License-Identifier: GPL-3.0-or-later
"""Client for the Miris public REST API (https://docs.miris.com/rest-api).

Endpoint paths, headers, and field names come from Miris's published OpenAPI spec
(public.yaml, "Miris Public API" 1.0.0). Where the spec leaves a value open, the
choice is marked ASSUMPTION so it is easy to find and confirm on a first live run.
"""

import json
from urllib.parse import quote, urlencode, urlsplit

from .http import HttpError

DEFAULT_BASE_URL = "https://app.miris.com"
API_KEY_HEADER = "Miris-API-Key"  # the spec's current header; Miris-Integration-Key is the legacy one

# ASSUMPTION: the spec types input_type as a free string. "usd" matches the docs'
# "upload a folder containing one root USD file" model. Configurable in preferences.
DEFAULT_INPUT_TYPE = "usd"


class MirisError(Exception):
    def __init__(self, message, status=None, reason=None, code=None):
        super().__init__(message)
        self.status = status
        self.reason = reason  # the spec's "slug identifying the refusal, for clients to branch on"
        self.code = code


class MirisClient:
    def __init__(self, transport, api_key, base_url=DEFAULT_BASE_URL, workspace=None):
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.workspace = workspace or None
        self._origin = _origin(self.base_url)

    def __repr__(self):
        return f"MirisClient({self.base_url!r}, api_key=<hidden>)"

    def with_transport(self, transport):
        return MirisClient(transport, self._api_key, self.base_url, self.workspace)

    # --- endpoints -----------------------------------------------------------------

    def customer_info(self):
        """GET /v1/customer_info: cheapest authenticated call, used by "Test connection"."""
        return self._json("GET", "/v1/customer_info")

    def start_upload(self, *, name, content_path, file_count, total_bytes, tags=None,
                     input_type=DEFAULT_INPUT_TYPE, streamable=False):
        """POST /v1/asset -> ContentUploadStartResponse (upload id + scoped storage credentials)."""
        body = {
            "name": name,
            "input_type": input_type,
            "output_type": "static",  # the only value the spec allows
            "content_path": content_path,
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        if tags:
            body["tags"] = list(tags)
        if streamable:  # skip the preview; the spec says this needs an account entitlement
            body["streamable"] = True
        start = self._json("POST", "/v1/asset", body)
        missing = [k for k in ("id", "endpoint_url", "access_key_id", "secret_key", "session_token", "upload_path")
                   if not start.get(k)]
        if missing:
            raise MirisError(f"POST /v1/asset response is missing {', '.join(missing)}")
        return start

    def set_upload_status(self, asset_id, status, manifest=None):
        """PUT /v1/asset/upload/{id}. With status=completed and a manifest, Miris checks
        every declared file and size against storage before it starts processing."""
        body = {"status": status}
        if manifest:
            body["manifest"] = manifest
        return self._json("PUT", f"/v1/asset/upload/{quote(asset_id)}", body)

    def asset_detail(self, asset_id):
        """GET /v1/asset/all/{id}. Unlike GET /v1/asset/{id}, this one answers while the
        asset is still processing, and carries job_process_status and job_errors."""
        return self._json("GET", f"/v1/asset/all/{quote(asset_id)}")

    def create_viewer_key(self, name, asset_ids, tags=(), expires_at=None):
        """POST /v1/asset-viewer-key. The returned `key` is shown once and never again."""
        body = {"name": name, "asset_ids": list(asset_ids), "tags": list(tags)}
        if expires_at:
            body["expires_at"] = expires_at
        return self._json("POST", "/v1/asset-viewer-key", body)

    # --- plumbing ------------------------------------------------------------------

    def _json(self, method, path, payload=None):
        url = self.base_url + path
        if self.workspace:
            url += "?" + urlencode({"workspace": self.workspace})
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        # The key only ever goes to the API origin. Storage URLs come from the server and
        # point at another host; forwarding the key there would hand it to a bucket log.
        if self._api_key and _origin(url) == self._origin:
            headers[API_KEY_HEADER] = self._api_key

        label = f"{method} {path}"
        try:
            resp = self.transport.send(label, lambda _: (method, url, headers, body))
        except HttpError as exc:
            p = exc.problem
            raise MirisError(str(exc), status=exc.status, reason=p.get("reason"), code=p.get("code")) from None
        try:
            return resp.json() if resp.body else {}
        except ValueError as exc:
            raise MirisError(f"{label}: response was not JSON") from exc


def _origin(url):
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname, parts.port)
