"""Sign in with Miris (device flow) against the mock server, plus token handling."""

import shutil
import sys
import unittest
import uuid

from fixtures import good_folder

from core import bundle, oauth
from core.http import Transport
from core.miris import MirisClient, MirisError
from core.publish import publish
from mock.mock_miris import MirisState, serve_in_thread


class DeviceFlow(unittest.TestCase):
    def start(self, **kw):
        self.state = MirisState("unused-api-key", log=lambda m: None, **kw)
        self.server, self.url = serve_in_thread(self.state)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.transport = Transport(timeout=5, backoff_base=0.01, sleep=lambda s: None)

    def sign_in(self):
        grant = oauth.start(self.transport, self.url)
        self.assertRegex(grant["user_code"], r"^[0-9A-F]{4}-[0-9A-F]{4}$")
        self.assertEqual(self.state.grants[grant["device_code"]]["scopes"], oauth.SCOPES)
        return oauth.wait_for_approval(self.transport, self.url, grant)

    def test_sign_in_then_publish_with_bearer_token(self):
        self.start(approve_after_polls=2)
        tokens = self.sign_in()
        self.assertEqual(tokens["base_url"], self.url)
        saved = []
        session = oauth.OAuthSession(self.transport, self.url, tokens, persist=saved.append)
        client = MirisClient(self.transport, session, self.url)
        self.assertEqual(client.customer_info()["name"], "Mock workspace")

        folder = good_folder()
        self.addCleanup(shutil.rmtree, folder, True)
        report = bundle.check(folder, use_pxr=False)
        result = publish(client, folder, report["files"], name="chair", root=report["root"], poll_interval=0)
        self.assertEqual(result["status"], "preview")
        self.assertFalse(self.state.leaked_api_key, "a bearer token reached the storage host")
        self.assertNotIn(tokens["access_token"], repr(session) + repr(client))

    def test_denied_in_the_browser(self):
        self.start(deny=True)
        with self.assertRaisesRegex(oauth.OAuthError, "denied"):
            self.sign_in()

    def test_expired_access_token_is_refreshed_and_persisted(self):
        self.start(access_ttl=1)  # inside the refresh margin from the start
        tokens = self.sign_in()
        saved = []
        session = oauth.OAuthSession(self.transport, self.url, tokens, persist=saved.append)
        MirisClient(self.transport, session, self.url).customer_info()
        self.assertEqual(self.state.refreshes, 1)
        self.assertEqual(len(saved), 1, "rotated tokens are written back to the vault")
        self.assertNotEqual(saved[0]["refresh_token"], tokens["refresh_token"])
        self.assertEqual(saved[0]["base_url"], self.url)

    def test_token_rejected_by_server_gets_one_refresh(self):
        self.start()
        tokens = self.sign_in()
        session = oauth.OAuthSession(self.transport, self.url, tokens, persist=lambda t: None)
        self.state.access_tokens.clear()  # server-side revocation of the access token only
        self.assertEqual(MirisClient(self.transport, session, self.url).customer_info()["name"], "Mock workspace")
        self.assertEqual(self.state.refreshes, 1)

    def test_revoked_refresh_token_means_sign_in_again(self):
        self.start()
        tokens = self.sign_in()
        oauth.revoke(self.transport, self.url, tokens)
        self.state.access_tokens.clear()
        session = oauth.OAuthSession(self.transport, self.url, tokens, persist=lambda t: None)
        with self.assertRaises(MirisError):
            MirisClient(self.transport, session, self.url).customer_info()


class Credential(unittest.TestCase):
    def test_environment_key_wins(self):
        kind, value, label = oauth.credential("https://app.miris.com", env={"MIRIS_API_KEY": " k \n"},
                                              service="stream-ready/test-none")
        self.assertEqual((kind, value, label), ("api_key", "k", "$MIRIS_API_KEY"))

    @unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
    def test_tokens_are_only_used_for_the_server_that_issued_them(self):
        service = f"stream-ready/test-{uuid.uuid4().hex[:8]}"
        self.addCleanup(oauth.forget_tokens, service)
        oauth.save_tokens({"access_token": "a" * 43, "refresh_token": "r" * 43, "expires_at": 0,
                           "base_url": "https://app.miris.com"}, service)
        self.assertEqual(oauth.credential("https://app.miris.com", env={}, service=service)[0], "oauth")
        # Pointing the add-on at a mock (or anything else) must not send it the real token.
        self.assertEqual(oauth.credential("http://127.0.0.1:8787", env={}, service=service)[0], "")


if __name__ == "__main__":
    unittest.main()
