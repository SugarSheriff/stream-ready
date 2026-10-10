# SPDX-License-Identifier: GPL-3.0-or-later
"""One retry loop for every request the add-on makes, API or storage.

Callers pass a `build(attempt)` function instead of a ready-made request, because a
storage request has to be re-signed on every attempt (SigV4 signatures embed the
timestamp) and a file body has to be re-read from disk. Everything else (backoff with
jitter, Retry-After, cancel) is shared.
"""

import json
import random
import time
from urllib import error, request

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60.0
USER_AGENT = "stream-ready-blender/0.1 (+https://github.com/SugarSheriff/stream-ready)"


class HttpError(Exception):
    def __init__(self, message, status=None, retryable=False, problem=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.problem = problem or {}  # parsed application/problem+json, when the server sent one


class Cancelled(Exception):
    """The user pressed Cancel; not a failure."""


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body or b"null")


class Transport:
    def __init__(self, *, timeout=60.0, max_attempts=5, backoff_base=0.5, backoff_cap=8.0,
                 log=None, cancel=None, sleep=None, opener=None):
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.log = log or (lambda msg: None)
        self.cancel = cancel  # threading.Event, set from the UI thread
        self._sleep = sleep or time.sleep
        self._open = opener or request.urlopen
        self.attempts = 0  # total HTTP attempts; handy in support logs and tests

    def uncancellable(self, max_attempts=None):
        """A sibling transport that ignores Cancel, for the cleanup call after a cancel."""
        return Transport(timeout=self.timeout, max_attempts=max_attempts or self.max_attempts,
                         backoff_base=self.backoff_base, backoff_cap=self.backoff_cap, log=self.log,
                         sleep=self._sleep, opener=self._open)

    def send(self, label, build, *, ok=(200, 201, 204)):
        """Run `build(attempt) -> (method, url, headers, body)` until it succeeds or gives up."""
        for attempt in range(1, self.max_attempts + 1):
            self.check_cancel()
            self.attempts += 1
            method, url, headers, body = build(attempt)
            headers = dict(headers, **{"User-Agent": USER_AGENT})
            req = request.Request(url, data=body, headers=headers, method=method)
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    out = Response(resp.status, resp.headers, resp.read())
                if out.status in ok:
                    return out
                raise HttpError(f"{label} -> unexpected {out.status}", status=out.status)
            except error.HTTPError as exc:
                problem, detail = _problem(exc)
                if exc.code not in RETRYABLE_STATUS:
                    raise HttpError(f"{label} -> {exc.code}: {detail}", status=exc.code, problem=problem) from None
                delay = _retry_after(exc) if exc.code in (429, 503) else None
                if delay is None:
                    delay = self.backoff(attempt)
                issue = f"{exc.code} {detail}"
            except (error.URLError, TimeoutError, ConnectionError) as exc:
                issue = f"network error: {getattr(exc, 'reason', exc)}"
                delay = self.backoff(attempt)

            if attempt == self.max_attempts:
                raise HttpError(f"{label} failed after {attempt} attempts ({issue})", retryable=True)
            self.log(f"{label} -> {issue}; retry {attempt}/{self.max_attempts - 1} in {delay:.1f}s")
            self.wait(delay)

    def backoff(self, attempt):
        # Exponential with "equal jitter": never less than half the step, so many artists
        # retrying through the same outage spread out instead of arriving in waves.
        step = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        return step / 2 + random.uniform(0, step / 2)

    def wait(self, seconds):
        if self.cancel is not None:
            if self.cancel.wait(seconds):
                raise Cancelled()
        else:
            self._sleep(seconds)

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()


def _retry_after(exc):
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return None  # absent, or an HTTP-date; fall back to our own backoff


def _problem(exc):
    """Pull a readable message out of an error body: problem+json, S3 XML, or plain text."""
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict):
        # Miris errors are RFC 9457 problem+json; user_detail is the one meant for humans.
        detail = (body.get("user_detail") or body.get("detail") or body.get("title")
                  or body.get("message") or str(body))
        if body.get("reason"):
            detail = f"{detail} [{body['reason']}]"
        return body, detail
    text = raw.decode("utf-8", "replace").strip()
    code = _xml_tag(text, "Code")
    if code:  # S3-style <Error><Code>…</Code><Message>…</Message></Error>
        return {"code": code}, f"{code}: {_xml_tag(text, 'Message') or ''}".strip()
    return {}, text[:200] or str(exc.reason)


def _xml_tag(text, tag):
    start = text.find(f"<{tag}>")
    end = text.find(f"</{tag}>")
    if start == -1 or end == -1:
        return ""
    return text[start + len(tag) + 2:end]
