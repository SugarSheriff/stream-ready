"""API key storage: the environment first, then the OS vault, never a file."""

import os
import sys
import unittest
import uuid
from pathlib import Path

import fixtures  # noqa: F401  (sets up the import path)
from core import keystore

ROOT = Path(__file__).resolve().parents[1]


class Resolve(unittest.TestCase):
    def test_environment_wins(self):
        self.assertEqual(keystore.resolve({"MIRIS_API_KEY": "  from-env \n"}), ("from-env", "$MIRIS_API_KEY"))

    def test_empty_key_is_refused(self):
        with self.assertRaises(keystore.KeystoreError):
            keystore.save("   ")


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


class NoPlainTextKey(unittest.TestCase):
    def test_addon_has_no_api_key_property(self):
        # Blender saves every AddonPreferences property, PASSWORD subtype included, as
        # plain text in userpref.blend. The key must never be one of them.
        source = (ROOT / "stream_ready" / "properties.py").read_text(encoding="utf-8")
        self.assertNotIn("api_key: StringProperty", source)

    def test_set_key_operator_is_not_logged_or_remembered(self):
        source = (ROOT / "stream_ready" / "operators.py").read_text(encoding="utf-8")
        block = source.split("class STREAM_READY_OT_set_api_key", 1)[1].split("\nclass ", 1)[0]
        self.assertIn('bl_options = {"INTERNAL"}', block)  # no REGISTER: not echoed to the Info log
        self.assertIn('options={"SKIP_SAVE"}', block)  # the dialog doesn't remember the last value

    def test_gitignore_blocks_env_files(self):
        self.assertIn(".env", (ROOT / ".gitignore").read_text().split())


if __name__ == "__main__":
    unittest.main()
