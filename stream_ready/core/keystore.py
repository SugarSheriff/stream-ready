# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the Miris API key in the operating system's credential vault, never in a file.

Blender's "password" text fields only hide the characters on screen; the value is saved
in plain text in userpref.blend. So the add-on never stores the key in Blender at all:

    Windows  Credential Manager (CredWriteW / CredReadW via ctypes; encrypted per user)
    macOS    login Keychain (the `security` tool)
    Linux    Secret Service, e.g. GNOME Keyring or KWallet (the `secret-tool` tool)

No third-party packages, so nothing to pip-install into Blender. If no vault is
available, saving fails loudly instead of quietly falling back to a plain-text file;
$MIRIS_API_KEY still works everywhere.
"""

import os
import shutil
import subprocess
import sys

SERVICE = "stream-ready/miris-api-key"
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


def save(secret, service=SERVICE):
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
        return _run(["secret-tool", "store", "--label=Stream Ready: Miris API key",
                     "service", service, "account", ACCOUNT], stdin=secret)
    raise KeystoreError("no credential vault found; set $MIRIS_API_KEY instead")


def load(service=SERVICE):
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


def delete(service=SERVICE):
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

    def _win_write(service, secret):
        blob = secret.encode("utf-8")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIAL()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = service
        cred.Comment = "Stream Ready (Blender): Miris API key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = ACCOUNT
        if not _advapi.CredWriteW(ctypes.byref(cred), 0):
            raise KeystoreError(f"CredWriteW failed (error {ctypes.get_last_error()})")
        ctypes.memset(buf, 0, len(blob))  # don't leave a copy in this buffer

    def _win_read(service):
        pcred = ctypes.POINTER(_CREDENTIAL)()
        if not _advapi.CredReadW(service, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return ""
            raise KeystoreError(f"CredReadW failed (error {err})")
        try:
            cred = pcred.contents
            return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
        finally:
            _advapi.CredFree(pcred)

    def _win_delete(service):
        if not _advapi.CredDeleteW(service, CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err != ERROR_NOT_FOUND:
                raise KeystoreError(f"CredDeleteW failed (error {err})")


def resolve(env=None):
    """(key, source) with $MIRIS_API_KEY first, then the vault. Never reads a file."""
    env = os.environ if env is None else env
    value = (env.get("MIRIS_API_KEY") or "").strip()
    if value:
        return value, "$MIRIS_API_KEY"
    value = load()
    if value:
        return value, backend_name()
    return "", ""
