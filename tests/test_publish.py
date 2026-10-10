"""End to end: the real client, signer, and uploader against the mock Miris server."""

import shutil
import threading
import unittest

from fixtures import good_folder, make_folder, usda

from core import bundle
from core.http import Cancelled, Transport
from core.miris import MirisClient, MirisError
from core.publish import publish
from mock.mock_miris import MirisState, serve_in_thread

API_KEY = "test-api-key-do-not-log"


class PublishAgainstMock(unittest.TestCase):
    def start(self, **faults):
        self.state = MirisState(API_KEY, log=lambda msg: None, **faults)
        self.server, self.url = serve_in_thread(self.state)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.lines = []
        self.transport = Transport(timeout=5, backoff_base=0.01, backoff_cap=0.02, log=self.lines.append)
        return MirisClient(self.transport, API_KEY, self.url)

    def publish(self, client, folder, **kw):
        self.addCleanup(shutil.rmtree, folder, True)
        report = bundle.check(folder, use_pxr=False)
        kw.setdefault("poll_interval", 0)
        return publish(client, folder, report["files"], name="chair", root=report["root"], log=self.lines.append, **kw)

    def test_happy_path(self):
        client = self.start()
        progress = []
        result = self.publish(client, good_folder(), create_viewer_key=True,
                              progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["state"], "streamable")
        self.assertEqual(result["files"], 2)
        self.assertEqual(progress[-1][0], progress[-1][1])
        asset = self.state.assets[result["asset_id"]]
        self.assertEqual(sorted(k for (_, k) in self.state.objects), [
            f"{asset['prefix']}chair.usda", f"{asset['prefix']}textures/wood.png"])
        key = result["viewer_key"]
        self.assertTrue(key["key"].startswith(key["prefix"]))
        self.assertEqual(self.state.viewer_keys[key["id"]]["asset_ids"], [result["asset_id"]])

    def test_api_key_never_reaches_storage_or_the_log(self):
        client = self.start()
        self.publish(client, good_folder())
        self.assertGreater(self.state.storage_requests, 0)
        self.assertFalse(self.state.leaked_api_key, "Miris-API-Key was sent to the storage host")
        self.assertNotIn(API_KEY, "\n".join(self.lines))
        self.assertNotIn(API_KEY, repr(client))

    def test_large_file_goes_up_in_parts(self):
        client = self.start()
        big = bytes(range(256)) * 4096 * 3  # 3 MiB
        folder = make_folder({"chair.usda": usda(texture="./textures/big.png"), "textures/big.png": big})
        result = self.publish(client, folder, uploader_options={"threshold": 1 << 20, "part_size": 1 << 20})
        self.assertEqual(result["status"], "ready")
        asset = self.state.assets[result["asset_id"]]
        self.assertEqual(self.state.objects[("mock-bucket", asset["prefix"] + "textures/big.png")], big)
        self.assertIn("uploaded textures/big.png (3.0 MB)", "\n".join(self.lines))

    def test_retries_through_api_503s_and_storage_throttling(self):
        client = self.start(fail_api=2, throttle_storage=2)
        result = self.publish(client, good_folder())
        self.assertEqual(result["status"], "ready")
        log = "\n".join(self.lines)
        self.assertIn("POST /v1/asset -> 503", log)
        self.assertIn("429 SlowDown", log)

    def test_server_rejection_comes_back_as_miris_error_keys(self):
        # Preflight would have stopped this; publishing anyway shows the server agrees.
        client = self.start()
        result = self.publish(client, make_folder({"chair.usda": usda(size=0.002, texture=None)}))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual([f.code for f in result["findings"]], ["UNSUPPORTED_SCENE_SIZE"])
        self.assertIn("2.00 mm", result["findings"][0].message)
        self.assertIsNone(result["viewer_key"])

    def test_bad_api_key_is_a_clear_error_not_a_retry_loop(self):
        client = self.start()
        client = MirisClient(self.transport, "wrong-key", self.url)
        with self.assertRaises(MirisError) as ctx:
            self.publish(client, good_folder())
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.reason, "invalid_api_key")
        self.assertEqual(self.transport.attempts, 1)

    def test_manifest_catches_a_file_that_changed_during_upload(self):
        client = self.start()
        folder = good_folder()
        self.addCleanup(shutil.rmtree, folder, True)
        report = bundle.check(folder, use_pxr=False)
        files = [(rel, size + 1 if rel.endswith(".png") else size) for rel, size in report["files"]]
        with self.assertRaises(MirisError) as ctx:
            publish(client, folder, files, name="chair", root=report["root"], poll_interval=0)
        self.assertEqual(ctx.exception.reason, "manifest_mismatch")
        asset = next(iter(self.state.assets.values()))
        self.assertEqual(asset["status"], "failed", "a failed publish is marked failed on Miris, not left pending")

    def test_cancel_marks_the_upload_canceled(self):
        client = self.start(throttle_storage=10**9)  # storage never recovers; only Cancel ends this
        self.transport.cancel = threading.Event()
        threading.Timer(0.2, self.transport.cancel.set).start()
        self.transport.max_attempts = 10**9
        with self.assertRaises(Cancelled):
            self.publish(client, good_folder())
        asset = next(iter(self.state.assets.values()))
        self.assertEqual(asset["status"], "canceled")

    def test_signature_tampering_is_rejected_by_the_mock(self):
        # Guards the guard: the mock must actually be checking signatures.
        client = self.start()
        folder = good_folder()
        self.addCleanup(shutil.rmtree, folder, True)
        start = client.start_upload(name="x", content_path="chair.usda", file_count=1, total_bytes=1)
        from core.storage import Credentials, S3Uploader, parse_upload_path
        bucket, prefix = parse_upload_path(start["upload_path"])
        bad = Credentials(start["access_key_id"], "not-the-secret", start["session_token"], start["region"])
        with self.assertRaises(Exception) as ctx:
            S3Uploader(self.transport, start["endpoint_url"], bucket, bad).upload_file(prefix + "chair.usda", f"{folder}/chair.usda")
        self.assertIn("SignatureDoesNotMatch", str(ctx.exception))


class Addressing(unittest.TestCase):
    def test_virtual_hosted_on_aws_path_style_elsewhere(self):
        from core.storage import S3Uploader, parse_upload_path
        self.assertEqual(parse_upload_path("s3://miris-ingest/uploads/abc"), ("miris-ingest", "uploads/abc/"))
        self.assertEqual(parse_upload_path("miris-ingest/uploads/abc/"), ("miris-ingest", "uploads/abc/"))
        aws = S3Uploader(None, "https://s3.us-west-2.amazonaws.com", "miris-ingest", None)
        self.assertEqual(aws.object_url("uploads/a b.usd"), "https://miris-ingest.s3.us-west-2.amazonaws.com/uploads/a%20b.usd")
        other = S3Uploader(None, "https://storage.example.com/", "bucket", None)
        self.assertEqual(other.object_url("k.usd", "uploads"), "https://storage.example.com/bucket/k.usd?uploads")


if __name__ == "__main__":
    unittest.main()
