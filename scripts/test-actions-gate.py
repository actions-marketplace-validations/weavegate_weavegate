#!/usr/bin/env python3
"""Run with python3 scripts/test-actions-gate.py."""

import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error


spec = importlib.util.spec_from_file_location("actions_gate", Path(__file__).with_name("actions-gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


FAKE = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
root = Path(args[args.index("--out") + 1])
(root / "argv.json").write_text(json.dumps(args))
code = int(os.environ["FAKE_EXIT"])
mode = os.environ.get("FAKE_MODE", "complete")
if mode == "none":
    sys.exit(code)
run = root / "runs" / "run_20260927T000000.000000000Z_0123456789abcdef0123456789abcdef"
run.mkdir(parents=True)
if mode == "partial":
    print(run)
    sys.exit(code)
version = 3 if mode == "v3" else 2
verdict = {0:"PASS", 2:"FAIL", 3:"FLAKY", 4:"PASS", 5:"PASS", 130:"PASS"}[code]
schedule = {"id":"sch_ba00582f9632", "steps":[]}
for name, data in {
    "manifest.json":{"artifact_version":version,"run_id":run.name},
    "scenario.json":{"artifact_version":version,"schedule":schedule},
    "observation.json":{"artifact_version":version},
    "trace.json":{"artifact_version":version},
    "report.json":{"artifact_version":version},
}.items():
    (run / name).write_text(json.dumps(data))
(run / "report.md").write_text("## weavegate: " + verdict + "\\n")
(run / "schedule.json").write_text(json.dumps(schedule))
print(run)
sys.exit(code)
'''


TOKEN = "ghs_commentTokenMustNeverBePrinted"


class CommentAPI(http.server.BaseHTTPRequestHandler):
    """Stands in for the GitHub REST endpoint and records every request."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, dict(self.headers), body))
        status = self.server.status
        self.send_response(status)
        if status == 302:
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
        payload = json.dumps({"html_url": "https://github.com/octo/demo/pull/7#issuecomment-1"}).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), b""))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gate test ; ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence with spaces"
        self.evidence.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.binary = self.root / "fake weavegate"
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.outputs = self.root / "outputs"
        self.base_env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(self.root),
            "WEAVEGATE_EVIDENCE_DIR": str(self.evidence),
            "WEAVEGATE_BINARY": str(self.binary),
            "WEAVEGATE_CONFIG": "config ; $(touch injected).yaml",
            "WEAVEGATE_SCENARIO": "concurrent-assign",
            "WEAVEGATE_VARIANT": "fixed ' ; *",
            "WEAVEGATE_REPLAY": "schedule ; $(touch injected).json",
            "WEAVEGATE_REPEAT": "20",
        }

    def execute(self, code, mode="complete"):
        shutil.rmtree(self.evidence / "runs", ignore_errors=True)
        (self.evidence / "schedule.json").unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {**self.base_env, "FAKE_EXIT": str(code), "FAKE_MODE": mode}, clear=True):
            gate.run()
        return json.loads((self.evidence / "status.json").read_text())

    def test_exit_codes_and_artifact_before_gate(self):
        for code in (0, 2, 3, 4, 5, 130):
            with self.subTest(code=code):
                status = self.execute(code)
                self.assertEqual(status["exit_code"], code)
                self.assertTrue(status["evidence_complete"])
                self.assertTrue((self.evidence / "schedule.json").is_file())
                with mock.patch.dict(os.environ, {"WEAVEGATE_EVIDENCE_DIR": str(self.evidence), "WEAVEGATE_UPLOAD_OUTCOME": "success"}, clear=True):
                    self.assertEqual(gate.gate(), 0 if code == 0 else 1)

    def test_missing_and_partial_evidence_cannot_pass(self):
        for mode in ("none", "partial"):
            with self.subTest(mode=mode):
                status = self.execute(0, mode)
                self.assertEqual(status["verdict"], "")
                self.assertFalse(status["evidence_complete"])
                with mock.patch.dict(os.environ, {"WEAVEGATE_EVIDENCE_DIR": str(self.evidence), "WEAVEGATE_UPLOAD_OUTCOME": "success"}, clear=True):
                    self.assertEqual(gate.gate(), 1)

    def test_retained_diagnostic_failure_never_passes(self):
        status = self.execute(5, "v3")
        self.assertEqual(status["artifact_version"], 3)
        self.assertEqual(status["verdict"], "PASS")
        with mock.patch.dict(os.environ, {"WEAVEGATE_EVIDENCE_DIR": str(self.evidence), "WEAVEGATE_UPLOAD_OUTCOME": "success"}, clear=True):
            self.assertEqual(gate.gate(), 1)

    def test_inputs_are_single_arguments_and_no_shell_runs(self):
        self.execute(0)
        args = json.loads((self.evidence / "argv.json").read_text())
        for key, flag in (("WEAVEGATE_CONFIG", "--config"), ("WEAVEGATE_VARIANT", "--variant"), ("WEAVEGATE_REPLAY", "--replay")):
            self.assertEqual(args[args.index(flag) + 1], self.base_env[key])
        self.assertFalse((self.workspace / "injected").exists())

    def test_upload_failure_blocks_pass(self):
        self.execute(0)
        with mock.patch.dict(os.environ, {"WEAVEGATE_EVIDENCE_DIR": str(self.evidence), "WEAVEGATE_UPLOAD_OUTCOME": "failure"}, clear=True):
            self.assertEqual(gate.gate(), 1)

    def start_api(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CommentAPI)
        server.requests = []
        server.status = 201
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def gate_result(self):
        with mock.patch.dict(os.environ, {"WEAVEGATE_EVIDENCE_DIR": str(self.evidence), "WEAVEGATE_UPLOAD_OUTCOME": "success"}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return gate.gate()

    def comment(self, server, event="pull", head="octo/demo", **overrides):
        """Run the comment step; return (outcome, URL, requests, everything it wrote)."""
        payload = {"ref": "refs/heads/main"}
        if event == "pull":
            payload = {"pull_request": {"number": 7, "head": {"repo": {"full_name": head}}}}
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(payload))
        self.outputs.unlink(missing_ok=True)
        summary = self.root / "summary.md"
        summary.unlink(missing_ok=True)
        env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "octo/demo",
            "GITHUB_RUN_ID": "4242",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
            "WEAVEGATE_EVIDENCE_DIR": str(self.evidence),
            "WEAVEGATE_UPLOAD_OUTCOME": "success",
            "WEAVEGATE_ARTIFACT_URL": "https://github.com/octo/demo/actions/runs/4242/artifacts/99",
            "WEAVEGATE_ARTIFACT_NAME": "weavegate-evidence",
            "WEAVEGATE_COMMENT": "true",
            "WEAVEGATE_COMMENT_TOKEN": TOKEN,
            **overrides,
        }
        del server.requests[:]
        gate_before = self.gate_result()
        status_before = (self.evidence / "status.json").read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate.sys, "argv", ["actions-gate.py", "comment"]):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(gate.main(), 0)
        self.assertEqual(self.gate_result(), gate_before)
        self.assertEqual((self.evidence / "status.json").read_bytes(), status_before)
        outputs = dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())
        written = stdout.getvalue() + stderr.getvalue() + self.outputs.read_text() + summary.read_text()
        self.assertNotIn(TOKEN, written)
        self.assertIn(outputs["comment_outcome"], summary.read_text())
        return outputs["comment_outcome"], outputs["comment_url"], list(server.requests), written

    def embedded_report(self, body):
        text = json.loads(body)["body"]
        begin = text.index(gate.REPORT_BEGIN + "\n") + len(gate.REPORT_BEGIN) + 1
        fence, _, rest = text[begin:].partition("text\n")
        self.assertRegex(fence, r"^`{4,}$")
        return rest[:rest.index(fence + "\n" + gate.REPORT_END)].encode("utf-8")

    def test_comment_cases_leave_the_gate_unchanged(self):
        server = self.start_api()
        self.assertEqual(self.execute(2)["exit_code"], 2)
        (self.evidence / "install.txt").write_text("version=v0.1.0-alpha\narchive=a\nsha256=b\n")
        report_path = next((self.evidence / "runs").iterdir()) / "report.md"
        hostile = "## weavegate: FAIL (WG001)\nreplay: weavegate run --replay sch_ba00582f9632\n  observed:  ````` </details> @octocat <!-- weavegate-report-end --> $(touch injected)\n``````\n[forged](https://example.invalid)\n"
        report_path.write_text(hostile)

        outcome, url, requests, written = self.comment(server)
        self.assertEqual((outcome, url), ("posted", "https://github.com/octo/demo/pull/7#issuecomment-1"))
        (path, headers, body), = requests
        self.assertEqual(path, "/repos/octo/demo/issues/7/comments")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(self.embedded_report(body), report_path.read_bytes())
        text = json.loads(body)["body"]
        # The report's own six-backtick line cannot close the literal block.
        self.assertIn("\n```````text\n", text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("process exit code 2", text)
        self.assertIn("https://github.com/octo/demo/actions/runs/4242/artifacts/99", text)
        self.assertIn("gh run download 4242 --repo octo/demo --name weavegate-evidence --dir weavegate-evidence\n", text)
        self.assertIn("cp weavegate-evidence/schedule.json .weavegate/schedules/sch_ba00582f9632.json\n", text)
        self.assertIn("install weavegate `v0.1.0-alpha`", text)
        # Key facts stay visible; the report and the replay steps are collapsed.
        visible = text[:text.index("<details>")]
        self.assertIn("Report verdict **FAIL** · schedule `sch_ba00582f9632` · [evidence artifact](", visible)
        self.assertIn("[workflow run 4242](https://github.com/octo/demo/actions/runs/4242)", visible)
        self.assertEqual(text.count("<details>\n<summary>"), 2)
        self.assertEqual(text.count("\n</details>\n"), 2)
        self.assertLess(text.index("<details>"), text.index(gate.REPORT_BEGIN))
        self.assertLess(text.index("Replay schedule <code>sch_ba00582f9632</code>"), text.index("gh run download"))
        self.assertTrue(text.endswith("</details>\n\nSet `comment: 'false'` on the weavegate action to turn this comment off.\n"))
        self.assertEqual(text.count("turn this comment off"), 1)
        # Report text appears once, inside the literal block only.
        self.assertEqual(text.count("$(touch injected)"), 1)
        self.assertFalse((self.workspace / "injected").exists())

        # Enterprise replay must select the server that owns the artifact.
        _, _, ((_, _, body),), _ = self.comment(server, GITHUB_SERVER_URL="https://ghe.example.com")
        enterprise = json.loads(body)["body"]
        self.assertIn("[workflow run 4242](https://ghe.example.com/octo/demo/actions/runs/4242)", enterprise)
        self.assertIn("gh run download 4242 --repo ghe.example.com/octo/demo --name weavegate-evidence", enterprise)
        _, _, ((_, _, body),), _ = self.comment(server, GITHUB_SERVER_URL="https://ghe.example.com/$(touch injected)")
        invalid_server = json.loads(body)["body"]
        self.assertNotIn("gh run download", invalid_server)
        self.assertNotIn("[workflow run 4242]", invalid_server)

        # An artifact name outside the closed grammar never reaches a command.
        _, _, ((_, _, body),), _ = self.comment(server, WEAVEGATE_ARTIFACT_NAME="evidence; $(touch injected)")
        self.assertNotIn("touch injected)\n   mkdir", json.loads(body)["body"])
        self.assertNotIn("gh run download", json.loads(body)["body"])

        outcome, url, requests, _ = self.comment(server, WEAVEGATE_COMMENT="false")
        self.assertEqual((outcome, url, requests), ("disabled", "", []))

        self.assertEqual(self.comment(server, WEAVEGATE_COMMENT="yes")[::2], ("skipped", []))
        self.assertEqual(self.comment(server, event="push")[::2], ("skipped", []))
        self.assertEqual(self.comment(server, WEAVEGATE_COMMENT_TOKEN="")[::2], ("skipped", []))

        server.status = 403
        outcome, _, requests, written = self.comment(server, head="fork/demo")
        self.assertEqual((outcome, len(requests)), ("skipped", 1))
        self.assertIn("fork pull request", written)
        outcome, _, requests, written = self.comment(server)
        self.assertEqual((outcome, len(requests)), ("skipped", 1))
        self.assertIn("cannot write pull request comments", written)

        server.status = 500
        self.assertEqual(self.comment(server)[0], "failed")
        server.status = 302
        outcome, _, requests, _ = self.comment(server)
        self.assertEqual((outcome, [path for path, _, _ in requests]), ("failed", ["/repos/octo/demo/issues/7/comments"]))
        server.status = 201

        # Too large to embed: an explicit artifact pointer, never a cut report.
        report_path.write_text("## weavegate: FAIL\n" + "observed: UNIQUE-ROW-TEXT\n" * 3000)
        outcome, _, ((_, _, body),), _ = self.comment(server)
        text = json.loads(body)["body"]
        self.assertEqual(outcome, "posted")
        self.assertLessEqual(len(text.encode("utf-8")), gate.COMMENT_LIMIT)
        self.assertNotIn("UNIQUE-ROW-TEXT", text)
        self.assertNotIn(gate.REPORT_BEGIN, text)
        self.assertIn("It was not truncated.", text)
        self.assertIn(f"Read `runs/{report_path.parent.name}/report.md` in the evidence artifact.", text)
        self.assertIn("https://github.com/octo/demo/actions/runs/4242/artifacts/99", text)
        # The largest report that still fits is embedded whole.
        overhead = len(gate.comment_body(json.loads((self.evidence / "status.json").read_text()), b"\n", 1, {
            "repository": "octo/demo", "run_id": "4242", "server_url": "https://github.com", "artifact_name": "weavegate-evidence",
            "artifact_url": "https://github.com/octo/demo/actions/runs/4242/artifacts/99", "upload_outcome": "success", "version": "v0.1.0-alpha",
        })[0].encode("utf-8")) - 1
        for size, embedded in ((gate.COMMENT_LIMIT - overhead, True), (gate.COMMENT_LIMIT - overhead + 1, False)):
            report_path.write_bytes(b"x" * (size - 1) + b"\n")
            _, _, ((_, _, body),), _ = self.comment(server)
            self.assertEqual(gate.REPORT_BEGIN in json.loads(body)["body"], embedded)
            self.assertLessEqual(len(json.loads(body)["body"].encode("utf-8")), gate.COMMENT_LIMIT)

        # Bytes a comment cannot carry unchanged are linked, not altered.
        for raw in (b"## weavegate: FAIL\n\x1b[31m\n", b"## weavegate: FAIL\n\xff\n", b"## weavegate: FAIL"):
            report_path.write_bytes(raw)
            _, _, ((_, _, body),), _ = self.comment(server)
            self.assertNotIn(gate.REPORT_BEGIN, json.loads(body)["body"])
            self.assertIn("cannot carry unchanged", json.loads(body)["body"])

        outcome, _, ((_, _, body),), _ = self.comment(server, WEAVEGATE_UPLOAD_OUTCOME="failure", WEAVEGATE_ARTIFACT_URL="")
        self.assertIn("no downloadable evidence", json.loads(body)["body"])
        self.assertNotIn("Replay schedule", json.loads(body)["body"])
        self.assertEqual(json.loads(body)["body"].count("turn this comment off"), 1)

        report_path.unlink()
        self.assertEqual(self.comment(server)[::2], ("skipped", []))
        print("ACTION_COMMENT_RESULT enabled=posted disabled=no_request fork=skipped no_permission=skipped no_token=skipped non_pull_request=skipped api_error=failed redirect=not_followed oversized=artifact_link report=byte_identical token=not_printed gate=unchanged")

    def test_checksum_accepts_one_exact_archive_and_rejects_mismatch(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            payload = b"binary"
            member = tarfile.TarInfo("weavegate_0.1.0-alpha_linux_amd64/weavegate")
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        name = "weavegate_0.1.0-alpha_linux_amd64.tar.gz"
        checksum = hashlib.sha256(archive.getvalue()).hexdigest()
        with mock.patch.object(gate, "download", side_effect=[f"{checksum}  {name}\n".encode(), archive.getvalue()]):
            self.assertEqual(gate.verified_archive("v0.1.0-alpha", "amd64")[2], checksum)
        with mock.patch.object(gate, "download", side_effect=[f"{'0'*64}  {name}\n".encode(), archive.getvalue()]):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                gate.verified_archive("v0.1.0-alpha", "amd64")

    def test_install_uses_action_tag_or_explicit_override(self):
        cases = (
            ("", "v0.2.0", "v0.2.0"),
            ("", "v0.2.0-rc.1", "v0.2.0-rc.1"),
            ("v0.1.0-alpha", "v0.2.0", "v0.1.0-alpha"),
            ("v0.1.0-alpha", "a" * 40, "v0.1.0-alpha"),
            ("v0.1.0-alpha", "main", "v0.1.0-alpha"),
            ("v0.1.0-alpha", "", "v0.1.0-alpha"),
            ("v0.1.0-alpha", "v0", "v0.1.0-alpha"),
        )
        for explicit, ref, expected in cases:
            with self.subTest(explicit=explicit, ref=ref):
                archive = io.BytesIO()
                payload = b"release binary"
                name = f"weavegate_{expected[1:]}_linux_amd64.tar.gz"
                with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                    member = tarfile.TarInfo(f"{name[:-7]}/weavegate")
                    member.size = len(payload)
                    tar.addfile(member, io.BytesIO(payload))
                checksum = hashlib.sha256(archive.getvalue()).hexdigest()
                with mock.patch.dict(os.environ, {
                    **self.base_env,
                    "WEAVEGATE_VERSION": explicit,
                    "WEAVEGATE_ACTION_REF": ref,
                    "WEAVEGATE_ACTION_REPOSITORY": "weavegate/weavegate",
                    "GITHUB_REF": "refs/tags/v99.0.0",
                    "GITHUB_REF_NAME": "v99.0.0",
                }, clear=True), mock.patch.object(gate.platform, "system", return_value="Linux"), mock.patch.object(
                    gate.platform, "machine", return_value="x86_64"
                ), mock.patch.object(gate, "download", side_effect=[
                    f"{checksum}  {name}\n".encode(), archive.getvalue()
                ] if explicit else [
                    json.dumps({"ref": f"refs/tags/{ref}"}).encode(),
                    f"{checksum}  {name}\n".encode(), archive.getvalue()
                ]) as download:
                    gate.install()
                expected_calls = [
                    mock.call(f"{gate.RELEASES}/{expected}/checksums.txt", 65536),
                    mock.call(f"{gate.RELEASES}/{expected}/{name}", 100 * 1024 * 1024),
                ]
                if not explicit:
                    expected_calls.insert(0, mock.call(
                        f"https://api.github.com/repos/weavegate/weavegate/git/ref/tags/{ref}", 65536
                    ))
                self.assertEqual(download.call_args_list, expected_calls)
                self.assertIn(f"version={expected}\n", (self.evidence / "install.txt").read_text())
                binary = Path(self.outputs.read_text().splitlines()[-1].removeprefix("binary="))
                self.assertEqual(binary.read_bytes(), payload)
                self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
                binary.unlink()

    def test_invalid_version_selection_retains_failure_and_never_downloads(self):
        cases = [("", ref) for ref in (
            "", "a" * 40, "main", "feat147/action-release-default", "v0", "v0.2",
            "refs/tags/v0.2.0", "v0.2.0; touch injected", "v0.2.0\n",
        )] + [(value, "v0.2.0") for value in (
            "main", "latest", "v0.2", " ", "v0.2.0; touch injected", "v0.2.0\n",
        )]
        for explicit, ref in cases:
            with self.subTest(explicit=explicit, ref=ref):
                with mock.patch.dict(os.environ, {
                    **self.base_env,
                    "WEAVEGATE_VERSION": explicit,
                    "WEAVEGATE_ACTION_REF": ref,
                    "WEAVEGATE_ACTION_REPOSITORY": "weavegate/weavegate",
                    "GITHUB_REF": "refs/tags/v99.0.0",
                    "GITHUB_REF_NAME": "v99.0.0",
                    "WEAVEGATE_UPLOAD_OUTCOME": "success",
                }, clear=True), mock.patch.object(sys, "argv", ["actions-gate.py", "install"]), mock.patch.object(
                    gate, "download"
                ) as download:
                    self.assertEqual(gate.main(), 1)
                    self.assertEqual(gate.gate(), 1)
                download.assert_not_called()
                failure = (self.evidence / "install.txt").read_text()
                self.assertIn("Installation failed:", failure)
                self.assertIn("version", failure)
                self.assertFalse(self.outputs.exists())
                self.assertFalse((self.evidence / "status.json").exists())

    def test_unpublished_action_tag_retains_install_failure_and_fails_gate(self):
        version = "v0.2.0-rc.1"
        url = f"{gate.RELEASES}/{version}/checksums.txt"
        with mock.patch.dict(os.environ, {
            **self.base_env,
            "WEAVEGATE_VERSION": "",
            "WEAVEGATE_ACTION_REF": version,
            "WEAVEGATE_ACTION_REPOSITORY": "weavegate/weavegate",
            "WEAVEGATE_UPLOAD_OUTCOME": "success",
        }, clear=True), mock.patch.object(sys, "argv", ["actions-gate.py", "install"]), mock.patch.object(
            gate.platform, "system", return_value="Linux"
        ), mock.patch.object(gate.platform, "machine", return_value="x86_64"), mock.patch.object(
            gate, "download", side_effect=[
                json.dumps({"ref": f"refs/tags/{version}"}).encode(),
                urllib.error.HTTPError(url, 404, "Not Found", None, None),
            ]
        ) as download:
            self.assertEqual(gate.main(), 1)
            self.assertEqual(gate.gate(), 1)
        self.assertEqual(download.call_args_list[-1], mock.call(url, 65536))
        self.assertIn("HTTP Error 404", (self.evidence / "install.txt").read_text())
        self.assertFalse(self.outputs.exists())
        self.assertFalse((self.evidence / "status.json").exists())

    def test_version_shaped_branch_requires_explicit_version(self):
        ref = "v0.2.0"
        url = f"https://api.github.com/repos/weavegate/weavegate/git/ref/tags/{ref}"
        for repository in ("weavegate/weavegate", "fork/weavegate"):
            with self.subTest(repository=repository), mock.patch.dict(os.environ, {
                **self.base_env,
                "WEAVEGATE_VERSION": "",
                "WEAVEGATE_ACTION_REF": ref,
                "WEAVEGATE_ACTION_REPOSITORY": repository,
                "WEAVEGATE_UPLOAD_OUTCOME": "success",
            }, clear=True), mock.patch.object(sys, "argv", ["actions-gate.py", "install"]), mock.patch.object(
                gate, "download", side_effect=urllib.error.HTTPError(url, 404, "Not Found", None, None)
            ) as download:
                self.assertEqual(gate.main(), 1)
                self.assertEqual(gate.gate(), 1)
                self.assertIn("Installation failed:", (self.evidence / "install.txt").read_text())
                self.assertEqual(download.call_count, 1 if repository == "weavegate/weavegate" else 0)


if __name__ == "__main__":
    result = unittest.main(exit=False)
    if result.result.wasSuccessful():
        print("ACTION_VERSION_RESULT tag=default prerelease=accepted explicit=preferred nonrelease=requires_version caller_ref=ignored install_failure=retained gate=failed")
    sys.exit(not result.result.wasSuccessful())
