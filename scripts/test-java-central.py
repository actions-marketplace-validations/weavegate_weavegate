#!/usr/bin/env python3
"""Check that Central receives exactly the verified ZIP and errors stop publication."""

import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("java_central", Path(__file__).with_name("java-central.py"))
CENTRAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CENTRAL)
DEPLOYMENT = "28570f16-da32-4c14-bd2e-c1acc0782365"
VERSION = "0.2.0-rc.1"


class Response(io.BytesIO):
    def __init__(self, status, data):
        super().__init__(data)
        self.status = status


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name) / "bundle.zip"
        self.bundle.write_bytes(b"verified zip bytes")

    def test_uploads_the_bytes_that_were_verified(self):
        requests = []

        def verify(source, target):
            self.assertEqual(target, VERSION)
            self.assertEqual(source.read(), b"verified zip bytes")
            self.bundle.write_bytes(b"changed after verification")

        def open_request(request, timeout):
            self.assertEqual(timeout, 60)
            requests.append(request)
            if len(requests) == 1:
                return Response(201, DEPLOYMENT.encode("ascii"))
            return Response(200, json.dumps({
                "deploymentId": DEPLOYMENT, "deploymentState": "PUBLISHED",
                "purls": [f"pkg:maven/{CENTRAL.GROUP}/{CENTRAL.ARTIFACT}@{VERSION}"],
            }).encode("utf-8"))

        with patch.object(CENTRAL, "verify_bundle", side_effect=verify), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                contextlib.redirect_stdout(io.StringIO()):
            CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token")

        self.assertEqual(len(requests), 2)
        self.assertIn(b"verified zip bytes", requests[0].data)
        self.assertNotIn(b"changed after verification", requests[0].data)
        self.assertIn("publishingType=AUTOMATIC", requests[0].full_url)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer dXNlcjp0b2tlbg==")
        self.assertIn("/api/v1/publisher/status?", requests[1].full_url)

    def test_invalid_bundle_never_uploads(self):
        with patch.object(CENTRAL, "verify_bundle", side_effect=ValueError("invalid signature")), \
                patch.object(CENTRAL.urllib.request, "urlopen") as open_request:
            with self.assertRaisesRegex(ValueError, "invalid signature"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token")
        open_request.assert_not_called()

    def test_failed_deployment_is_not_success(self):
        responses = [Response(201, DEPLOYMENT.encode("ascii")), Response(200, json.dumps({
            "deploymentId": DEPLOYMENT, "deploymentState": "FAILED", "purls": [],
        }).encode("utf-8"))]
        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "did not publish: FAILED"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token")


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    print("JAVA_CENTRAL_PUBLISH_TEST_RESULT uploaded=verified_bytes failed_bundle=blocked failed_status=blocked")
