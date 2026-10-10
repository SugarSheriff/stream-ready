"""API key storage: the environment first, then the OS vault, never a file."""

import os
import sys
import unittest
import uuid
from pathlib import Path

import fixtures  # noqa: F401  (sets up the import path)
from core import keystore

ROOT = Path(__file__).resolve().parents[1]


class Save(unittest.TestCase):
    def test_empty_secret_is_refused(self):
        with self.assertRaises(keystore.KeystoreError):
            keystore.save("   ", "stream-ready/test-empty")


@unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
class WindowsCredentialManager(unittest.TestCase):
    """Round-trips a throwaway credential through the real vault, then deletes it."""

    def setUp(self):
        self.service = f"stream-ready/test-{uuid.uuid4().hex[:8]}"
        self.addCleanup(keystore.delete, self.service)

    def test_save_load_delete(self):
        self.assertEqual(keystore.load(self.service), "")
        keystore.save("sk_test_éü_123", self.service)
        self.assertEqual(keystore.load(self.service), "sk_test_éü_123")
        keystore.save("rotated", self.service)  # overwrite, don't duplicate
        self.assertEqual(keystore.load(self.service), "rotated")
        keystore.delete(self.service)
        self.assertEqual(keystore.load(self.service), "")
        keystore.delete(self.service)  # deleting twice is fine

    def test_values_over_the_blob_limit_are_chunked(self):
        big = "eyJ" + "éx" * 3000  # ~9 KB of UTF-8: two JWTs' worth, past the 2,560-byte cap
        keystore.save(big, self.service)
        self.assertEqual(keystore.load(self.service), big)
        keystore.save("short again", self.service)  # old chunks are cleaned up
        self.assertEqual(keystore.load(self.service), "short again")
        self.assertEqual(keystore.load(self.service + "#1"), "")
        keystore.save(big, self.service)
        keystore.delete(self.service)
        self.assertEqual(keystore.load(self.service + "#2"), "")


class NoPlainTextKey(unittest.TestCase):
    def test_addon_has_no_secret_properties(self):
        # Blender saves every property, PASSWORD subtype included, as plain text in
        # userpref.blend or the .blend. No credential may ever be one of them.
        for name in ("properties.py", "operators.py"):
            source = (ROOT / "stream_ready" / name).read_text(encoding="utf-8")
            self.assertNotIn('subtype="PASSWORD"', source, name)

    def test_sign_in_worker_never_puts_tokens_in_shared_state(self):
        source = (ROOT / "stream_ready" / "operators.py").read_text(encoding="utf-8")
        worker = source.split("def _sign_in_worker", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("oauth.save_tokens(tokens)", worker)
        self.assertNotIn("signin.tokens", worker)
        self.assertNotIn("log(", worker)

    def test_gitignore_blocks_env_files(self):
        self.assertIn(".env", (ROOT / ".gitignore").read_text().split())


if __name__ == "__main__":
    unittest.main()
