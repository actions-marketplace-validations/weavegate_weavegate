#!/usr/bin/env python3
"""Check exact bundle uploads, resumed deployments, and published releases."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("java_central", Path(__file__).with_name("java-central.py"))
CENTRAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CENTRAL)
DEPLOYMENT = "28570f16-da32-4c14-bd2e-c1acc0782365"
VERSION = "0.2.0-rc.1"
REVISION = "a" * 40
NAME = f"{CENTRAL.ARTIFACT}-{VERSION}-{REVISION}"
PURL = f"pkg:maven/{CENTRAL.GROUP}/{CENTRAL.ARTIFACT}@{VERSION}"


class Response(io.BytesIO):
    def __init__(self, status, data):
        super().__init__(data)
        self.status = status


def json_response(value):
    return Response(200, json.dumps(value).encode("utf-8"))


def listing(items, page_count=1):
    return json_response({"deployments": items, "pageCount": page_count})


def item(name=NAME, state="PUBLISHING"):
    return {"deploymentId": DEPLOYMENT, "deploymentName": name,
            "namespace": CENTRAL.GROUP, "deploymentState": state,
            "deploymentComponents": [{"purl": PURL}]}


def status(state):
    return json_response({"deploymentId": DEPLOYMENT, "deploymentState": state,
                          "purls": [PURL] if state == "PUBLISHED" else []})


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name) / "bundle.zip"
        self.bundle.write_bytes(b"verified zip bytes")

    def write_artifact_bundle(self):
        prefix = f"io/github/weavegate/{CENTRAL.ARTIFACT}/{VERSION}/{CENTRAL.ARTIFACT}-{VERSION}"
        artifacts = {prefix + suffix: ("verified " + suffix).encode("ascii")
                     for suffix in (".pom", ".jar", "-sources.jar", "-javadoc.jar")}
        with zipfile.ZipFile(self.bundle, "w") as archive:
            for name, content in artifacts.items():
                archive.writestr(name, content)
        return artifacts

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
                return listing([])
            if len(requests) == 2:
                return Response(201, DEPLOYMENT.encode("ascii"))
            return status("PUBLISHED")

        with patch.object(CENTRAL, "verify_bundle", side_effect=verify), \
                patch.object(CENTRAL, "published_coordinate", return_value=False), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                contextlib.redirect_stdout(io.StringIO()):
            CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)

        self.assertEqual(len(requests), 3)
        self.assertIn("deploymentName=" + NAME, requests[0].full_url)
        self.assertIn(b"verified zip bytes", requests[1].data)
        self.assertNotIn(b"changed after verification", requests[1].data)
        self.assertIn("publishingType=AUTOMATIC", requests[1].full_url)
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer dXNlcjp0b2tlbg==")
        self.assertIn("/api/v1/publisher/status?", requests[2].full_url)

    def test_invalid_bundle_never_calls_central(self):
        with patch.object(CENTRAL, "verify_bundle", side_effect=ValueError("invalid signature")), \
                patch.object(CENTRAL.urllib.request, "urlopen") as open_request:
            with self.assertRaisesRegex(ValueError, "invalid signature"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
        open_request.assert_not_called()

    def test_retry_resumes_accepted_deployment_after_status_error(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                return listing([])
            if len(requests) == 2:
                return Response(201, DEPLOYMENT.encode("ascii"))
            if len(requests) == 3:
                raise urllib.error.URLError("temporary status failure")
            if len(requests) == 4:
                return listing([item("unrelated-release")], page_count=2)
            if len(requests) == 5:
                return listing([item()], page_count=2)
            if len(requests) == 6:
                return status("PUBLISHING")
            return status("PUBLISHED")

        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "published_coordinate", return_value=False) as public_probe, \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                patch.object(CENTRAL.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(urllib.error.URLError):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
            CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)

        uploads = [request for request in requests if "/publisher/upload?" in request.full_url]
        self.assertEqual(len(uploads), 1)
        self.assertIn("page=1", requests[4].full_url)
        public_probe.assert_called_once()  # The resumed deployment needs no rebuilt-byte comparison.
        sleep.assert_called_once()

    def test_failed_existing_deployment_is_not_reuploaded(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return listing([item(state="FAILED")]) if len(requests) == 1 else status("FAILED")

        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "published_coordinate", return_value=False), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request):
            with self.assertRaisesRegex(ValueError, "did not publish: FAILED"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
        self.assertEqual(len(requests), 2)
        self.assertFalse(any("/publisher/upload?" in request.full_url for request in requests))

    def test_published_coordinate_is_verified_without_reupload(self):
        artifacts = self.write_artifact_bundle()
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            name = request.removeprefix(CENTRAL.REPOSITORY_URL + "/")
            return Response(200, b"valid signature" if name.endswith(".asc") else artifacts[name])

        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "existing_deployment", return_value=None), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                patch.object(CENTRAL.subprocess, "run") as gpg, \
                contextlib.redirect_stdout(io.StringIO()):
            gpg.return_value.returncode = 0
            CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)

        self.assertEqual(len(requests), 8)
        self.assertTrue(all(isinstance(request, str) for request in requests))
        parsed = [urllib.parse.urlparse(request) for request in requests]
        self.assertEqual([url.scheme for url in parsed], ["https"] * 8)
        self.assertEqual([url.hostname for url in parsed],
                         ["repo.maven.apache.org"] * 8)
        self.assertEqual({request.removeprefix(CENTRAL.REPOSITORY_URL + "/") for request in requests},
                         set(artifacts) | {name + ".asc" for name in artifacts})
        self.assertEqual(gpg.call_count, 4)

    def test_different_published_pom_blocks_upload(self):
        self.write_artifact_bundle()
        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "existing_deployment", return_value=None), \
                patch.object(CENTRAL.urllib.request, "urlopen", return_value=Response(200, b"other POM")) as open_request:
            with self.assertRaisesRegex(ValueError, "published artifact differs"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
        open_request.assert_called_once()

    def test_same_pom_with_different_binary_blocks_upload(self):
        artifacts = self.write_artifact_bundle()
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            name = request.removeprefix(CENTRAL.REPOSITORY_URL + "/")
            if name.endswith(".jar") and not name.endswith(("-sources.jar", "-javadoc.jar")):
                return Response(200, b"different binary")
            return Response(200, b"valid signature" if name.endswith(".asc") else artifacts[name])

        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "existing_deployment", return_value=None), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                patch.object(CENTRAL.subprocess, "run") as gpg:
            gpg.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, "published artifact differs"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
        self.assertEqual(len(requests), 3)  # POM, POM signature, then mismatched binary.
        gpg.assert_called_once()

    def test_invalid_published_javadoc_signature_blocks_success(self):
        artifacts = self.write_artifact_bundle()
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            name = request.removeprefix(CENTRAL.REPOSITORY_URL + "/")
            return Response(200, b"signature" if name.endswith(".asc") else artifacts[name])

        with patch.object(CENTRAL, "verify_bundle"), \
                patch.object(CENTRAL, "existing_deployment", return_value=None), \
                patch.object(CENTRAL.urllib.request, "urlopen", side_effect=open_request), \
                patch.object(CENTRAL.subprocess, "run") as gpg:
            gpg.side_effect = [SimpleNamespace(returncode=0) for _ in range(3)] + [
                SimpleNamespace(returncode=1)]
            with self.assertRaisesRegex(ValueError, "invalid published signature: .*javadoc.jar"):
                CENTRAL.publish_bundle(self.bundle, VERSION, "user", "token", REVISION)
        self.assertEqual(len(requests), 8)
        self.assertEqual(gpg.call_count, 4)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    print("JAVA_CENTRAL_PUBLISH_TEST_RESULT uploaded=verified_bytes retry=resumed published=verified failed_bundle=blocked failed_status=blocked")
