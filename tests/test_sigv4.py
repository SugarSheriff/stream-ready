"""SigV4 against the worked examples in AWS's S3 documentation
("Signature Calculations for the Authorization Header: Transferring Payload in a Single Chunk").

If these pass, the signer agrees with S3 byte for byte, independent of the mock server
(which reuses this signer and so cannot catch its mistakes on its own).
"""

import unittest
from datetime import datetime, timezone

import fixtures  # noqa: F401  (sets up the import path)
from core import sigv4

ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
WHEN = datetime(2013, 5, 24, tzinfo=timezone.utc)


def signature(headers):
    return headers["Authorization"].rsplit("Signature=", 1)[1]


class AwsExamples(unittest.TestCase):
    def sign(self, method, url, headers, payload_hash):
        return sigv4.sign(method, url, headers, payload_hash, access_key=ACCESS, secret_key=SECRET,
                          region="us-east-1", now=WHEN)

    def test_get_object(self):
        out = self.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt",
                        {"Range": "bytes=0-9"}, sigv4.EMPTY_SHA256)
        self.assertEqual(signature(out), "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,", out["Authorization"])

    def test_put_object_with_reserved_character_in_key(self):
        body = b"Welcome to Amazon S3."
        out = self.sign("PUT", "https://examplebucket.s3.amazonaws.com/test$file.text", {
            "Date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        }, sigv4.sha256_hex(body))
        self.assertEqual(signature(out), "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")

    def test_get_bucket_lifecycle_valueless_query(self):
        out = self.sign("GET", "https://examplebucket.s3.amazonaws.com/?lifecycle", {}, sigv4.EMPTY_SHA256)
        self.assertEqual(signature(out), "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")

    def test_list_objects_sorted_query(self):
        out = self.sign("GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", {}, sigv4.EMPTY_SHA256)
        self.assertEqual(signature(out), "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


class SessionToken(unittest.TestCase):
    def test_temporary_credentials_are_signed_and_sent(self):
        out = sigv4.sign("PUT", "http://127.0.0.1:9000/bucket/a.usd", {}, sigv4.EMPTY_SHA256,
                         access_key=ACCESS, secret_key=SECRET, region="us-west-2",
                         session_token="FQoG/token", now=WHEN)
        self.assertEqual(out["x-amz-security-token"], "FQoG/token")
        self.assertIn("x-amz-security-token", out["Authorization"].split("SignedHeaders=")[1])
        self.assertIn("/20130524/us-west-2/s3/aws4_request", out["Authorization"])

    def test_keys_with_spaces_are_encoded_once(self):
        self.assertEqual(sigv4.canonical_uri("/bucket/textures/wood base.png"), "/bucket/textures/wood%20base.png")


if __name__ == "__main__":
    unittest.main()
