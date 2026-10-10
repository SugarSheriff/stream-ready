# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep Miris credentials in the operating system's credential vault, never in a file.

Blender's "password" text fields only hide the characters on screen; the value is saved
in plain text in userpref.blend. So the add-on never stores secrets in Blender at all
(today that means the Sign in with Miris tokens, see oauth.py):

    Windows  Credential Manager (CredWriteW / CredReadW via ctypes; encrypted per user)
    macOS    login Keychain (the `security` tool)
    Linux    Secret Service, e.g. GNOME Keyring or KWallet (the `secret-tool` tool)

No third-party packages, so nothing to pip-install into Blender. If no vault is
available, saving fails loudly instead of quietly falling back to a plain-text file;
$MIRIS_API_KEY still works everywhere.
"""

import shutil
import subprocess
import sys

ACCOUNT = "default"


class KeystoreError(Exception):
    pass


def backend_name():
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "macOS Keychain"
    if shutil.which("secret-tool"):
        return "Secret Service (secret-tool)"
    return None


def save(secret, service):
    secret = (secret or "").strip()
    if not secret:
        raise KeystoreError("empty key")
    if sys.platform == "win32":
        return _win_write(service, secret)
    if sys.platform == "darwin":
        # -U updates an existing item. The secret goes in as an argument, so it is briefly
        # visible to other processes of the same user; the Keychain offers no stdin mode.
        return _run(["security", "add-generic-password", "-U", "-a", ACCOUNT, "-s", service, "-w", secret])
    if shutil.which("secret-tool"):
        # secret-tool reads the secret from stdin, so it never appears in a process list.
        return _run(["secret-tool", "store", "--label=Stream Ready: Miris sign-in",
                     "service", service, "account", ACCOUNT], stdin=secret)
    raise KeystoreError("no credential vault found; set $MIRIS_API_KEY instead")


def load(service):
    """The stored key, or "" if there is none (or no vault)."""
    try:
        if sys.platform == "win32":
            return _win_read(service)
        if sys.platform == "darwin":
            return _run(["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"]).strip()
        if shutil.which("secret-tool"):
            return _run(["secret-tool", "lookup", "service", service, "account", ACCOUNT]).strip()
    except KeystoreError:
        pass
    return ""


def delete(service):
    try:
        if sys.platform == "win32":
            _win_delete(service)
        elif sys.platform == "darwin":
            _run(["security", "delete-generic-password", "-a", ACCOUNT, "-s", service])
        elif shutil.which("secret-tool"):
            _run(["secret-tool", "clear", "service", service, "account", ACCOUNT])
    except KeystoreError:
        pass  # already gone


def _run(cmd, stdin=None):
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeystoreError(f"{cmd[0]}: {exc}") from None
    if proc.returncode != 0:
        # stderr from these tools never contains the secret, but keep it short anyway.
        raise KeystoreError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr.strip()[:120]}")
    return proc.stdout


# --- Windows Credential Manager -------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2  # survives logoff; stays on this machine (not roamed)
    ERROR_NOT_FOUND = 1168

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None

    # Credential Manager caps a blob at 2,560 bytes; OAuth tokens (JWTs, twice) can be
    # bigger. Long values are split across "<service>#1", "#2", … with a small header.
    CHUNK = 2048
    CHUNK_HEADER = "stream-ready-chunks:"

    def _win_write(service, secret):
        blob = secret.encode("utf-8")
        _win_delete_chunks(service)
        if len(blob) <= CHUNK:
            return _win_write_one(service, secret)
        parts = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)]
        for n, part in enumerate(parts, 1):
            _win_write_one(f"{service}#{n}", None, raw=part)  # may split a UTF-8 character; joined as bytes
        _win_write_one(service, f"{CHUNK_HEADER}{len(parts)}")

    def _win_read(service):
        head = _win_read_one(service)
        if not head.startswith(CHUNK_HEADER):
            return head
        count = int(head[len(CHUNK_HEADER):])
        return b"".join(_win_read_one(f"{service}#{n}", raw=True) for n in range(1, count + 1)).decode("utf-8")

    def _win_delete(service):
        _win_delete_chunks(service)
        _win_delete_one(service)

    def _win_delete_chunks(service):
        try:
            head = _win_read_one(service)
        except KeystoreError:
            return
        if head.startswith(CHUNK_HEADER):
            for n in range(1, int(head[len(CHUNK_HEADER):]) + 1):
                _win_delete_one(f"{service}#{n}")

    def _win_write_one(service, secret, raw=None):
        blob = raw if raw is not None else secret.encode("utf-8")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIAL()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = service
        cred.Comment = "Stream Ready (Blender): Miris sign-in"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = ACCOUNT
        if not _advapi.CredWriteW(ctypes.byref(cred), 0):
            raise KeystoreError(f"CredWriteW failed (error {ctypes.get_last_error()})")
        ctypes.memset(buf, 0, len(blob))  # don't leave a copy in this buffer

    def _win_read_one(service, raw=False):
        pcred = ctypes.POINTER(_CREDENTIAL)()
        if not _advapi.CredReadW(service, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return b"" if raw else ""
            raise KeystoreError(f"CredReadW failed (error {err})")
        try:
            cred = pcred.contents
            data = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return data if raw else data.decode("utf-8")
        finally:
            _advapi.CredFree(pcred)

    def _win_delete_one(service):
        if not _advapi.CredDeleteW(service, CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err != ERROR_NOT_FOUND:
                raise KeystoreError(f"CredDeleteW failed (error {err})")

