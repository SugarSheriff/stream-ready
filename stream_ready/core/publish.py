# SPDX-License-Identifier: GPL-3.0-or-later
"""The publish flow, end to end, with no Blender in it.

    POST /v1/asset                 reserve an asset; get scoped storage credentials
    PUT  <storage>/<prefix>/<file> SigV4-signed, one per file (multipart when large)
    PUT  /v1/asset/upload/{id}     status=completed + a manifest of every file and size
    GET  /v1/asset/all/{id}        poll until processed, rejected, or our patience runs out
    POST /v1/asset-viewer-key      optional: a share key scoped to just this asset

If anything fails between reserving and completing, the upload is marked failed (or
canceled) so it does not sit half-finished in the artist's Miris portal.
"""

import os
import time

from .findings import from_server
from .http import Cancelled
from .storage import Credentials, S3Uploader, parse_upload_path

# ASSUMPTION: the spec types job_process_status as a free string. These cover the usual
# spellings; anything else is treated as "still working" until the poll timeout.
DONE_STATES = {"completed", "complete", "succeeded", "success", "done", "finished"}
FAILED_STATES = {"failed", "error", "errored", "canceled", "cancelled", "rejected"}


def publish(client, folder, files, *, name, root, tags=(), input_type="usd", create_viewer_key=False,
            log=None, progress=None, poll_interval=3.0, poll_timeout=600.0, uploader_options=None,
            clock=time.monotonic):
    log = log or (lambda msg: None)
    transport = client.transport
    total = sum(size for _, size in files)
    log(f"{len(files)} file(s), {_fmt_bytes(total)}")

    start = client.start_upload(name=name, content_path=root, file_count=len(files),
                                total_bytes=total, tags=tags, input_type=input_type)
    asset_id = start["id"]
    log(f"asset {asset_id} reserved")

    bucket, prefix = parse_upload_path(start["upload_path"])
    creds = Credentials(start["access_key_id"], start["secret_key"], start["session_token"], start.get("region"))
    uploader = S3Uploader(transport, start["endpoint_url"], bucket, creds, **(uploader_options or {}))

    sent = [0]

    def advance(n):
        sent[0] += n
        if progress:
            progress(sent[0], total)

    try:
        for rel, size in files:
            uploader.upload_file(prefix + rel, os.path.join(folder, *rel.split("/")), progress=advance)
            log(f"uploaded {rel} ({_fmt_bytes(size)})")
        manifest = [{"file_name": rel, "size": size} for rel, size in files]
        client.set_upload_status(asset_id, "completed", manifest=manifest)
    except BaseException as exc:
        status = "canceled" if isinstance(exc, Cancelled) else "failed"
        try:
            # A cancelled transport refuses to send, so tell Miris on one that isn't.
            client.with_transport(transport.uncancellable(max_attempts=2)).set_upload_status(asset_id, status)
            log(f"upload marked {status} on Miris")
        except Exception:
            pass  # best effort: the original error is the one worth reporting
        raise
    log("upload verified by Miris; processing")

    result = {
        "asset_id": asset_id,
        "name": name,
        "files": len(files),
        "bytes": total,
        "status": "processing",
        "state": None,
        "job_process_status": None,
        "findings": [],
        "viewer_key": None,
    }
    _poll(client, asset_id, result, log, poll_interval, poll_timeout, clock)

    if create_viewer_key and result["status"] != "rejected":
        key = client.create_viewer_key(f"{name} (Blender share)", [asset_id], tags=["stream-ready"])
        # Shown once by the API and never retrievable again; kept in memory only.
        result["viewer_key"] = {"id": key.get("id"), "key": key.get("key"), "prefix": key.get("key_prefix")}
        log(f"viewer key {key.get('key_prefix')}… created, scoped to this asset")
    return result


def _poll(client, asset_id, result, log, interval, timeout, clock):
    deadline = clock() + timeout
    last = None
    while True:
        asset = client.asset_detail(asset_id)
        state = (asset.get("state") or "").lower()
        job = (asset.get("job_process_status") or "").lower()
        result["state"], result["job_process_status"] = state or None, job or None

        findings = from_server(asset.get("job_errors"))
        if findings or job in FAILED_STATES:
            result["status"] = "rejected"
            result["findings"] = findings
            log("Miris rejected the asset: " + (", ".join(f.code for f in findings) or job))
            return
        if state == "streamable" or job in DONE_STATES:
            result["status"] = "ready"
            log(f"asset ready ({state or job})")
            return
        if (state, job) != last:
            log(f"processing: state={state or '?'} job={job or '?'}")
            last = (state, job)
        if clock() >= deadline:
            log(f"still processing after {timeout:.0f}s; it will finish on Miris without Blender open")
            return
        client.transport.wait(interval)


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
