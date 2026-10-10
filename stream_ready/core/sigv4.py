# SPDX-License-Identifier: GPL-3.0-or-later
"""AWS Signature Version 4, stdlib only.

Miris hands back temporary storage credentials (access key, secret, session token) rather
than presigned URLs, so the client has to sign its own storage requests. boto3 would do
this, but Blender doesn't ship it, and pip-installing into an artist's Blender is not a
dependency strategy. SigV4 is ~80 lines of hashing, and tests/test_sigv4.py checks it
against the worked examples in AWS's own S3 documentation.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def sign(method, url, headers, payload_hash, *, access_key, secret_key, region,
         session_token=None, service="s3", now=None):
    """Return a copy of `headers` with Authorization, x-amz-date, and friends added.

    `payload_hash` is the hex SHA-256 of the body (or UNSIGNED_PAYLOAD). The caller hashes
    because only the caller knows whether the body is in memory or streamed from disk.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    parts = urlsplit(url)

    signed = {k.lower(): str(v) for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    if session_token:
        signed["x-amz-security-token"] = session_token

    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{' '.join(signed[n].split())}\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([
        method.upper(),
        canonical_uri(parts.path),
        canonical_query(parts.query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        sha256_hex(canonical_request.encode("utf-8")),
    ])
    key = _signing_key(secret_key, datestamp, region, service)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out = dict(headers)
    out["Host"] = parts.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    if session_token:
        out["x-amz-security-token"] = session_token
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


def canonical_uri(path):
    # S3 is the one AWS service that does not double-encode the path. Keys arrive here
    # unencoded (see storage.object_url), so encode each byte exactly once.
    return quote(path or "/", safe="/-_.~")


def canonical_query(query):
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        key, _, value = item.partition("=")
        pairs.append((quote(unquote(key), safe="-_.~"), quote(unquote(value), safe="-_.~")))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def _signing_key(secret_key, datestamp, region, service):
    k = hmac.new(f"AWS4{secret_key}".encode("utf-8"), datestamp.encode("utf-8"), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode("utf-8"), hashlib.sha256).digest()
    return k
