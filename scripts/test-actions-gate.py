#!/usr/bin/env python3
"""Run with python3 scripts/test-actions-gate.py."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock


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


if __name__ == "__main__":
    unittest.main()
