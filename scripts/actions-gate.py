#!/usr/bin/env python3
"""Composite-action boundary: install, run, retain, then decide the gate."""

import hashlib
import html
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


RELEASES = "https://github.com/weavegate/weavegate/releases/download"
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?")
SCHEDULE = re.compile(r"sch_[0-9a-f]{12}")
HEADLINE = re.compile(r"## weavegate: (PASS|FAIL|FLAKY)(?: \([^\n]*\))?\n")
BASE_FILES = ("manifest.json", "scenario.json", "observation.json", "trace.json", "report.json", "report.md")


def output(**values):
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as stream:
        for name, value in values.items():
            value = str(value)
            if "\n" in value or "\r" in value:
                raise ValueError("GitHub output contains a line break")
            stream.write(f"{name}={value}\n")


def evidence_dir():
    return Path(os.environ["WEAVEGATE_EVIDENCE_DIR"]).resolve()


def prepare():
    root = Path(tempfile.mkdtemp(prefix="weavegate-gate-", dir=os.environ["RUNNER_TEMP"]))
    (root / "prepared.txt").write_text("weavegate action prepared\n", encoding="utf-8")
    output(evidence_dir=root)


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "weavegate-actions-gate"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("release asset exceeds size limit")
    return payload


def verified_archive(version, arch, base_url=RELEASES):
    name = f"weavegate_{version[1:]}_linux_{arch}.tar.gz"
    base = f"{base_url}/{version}"
    checksums = download(f"{base}/checksums.txt", 65536).decode("utf-8")
    matches = []
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if match and match.group(2) == name:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise ValueError(f"exactly one checksum required for {name}")
    archive = download(f"{base}/{name}", 100 * 1024 * 1024)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != matches[0]:
        raise ValueError(f"checksum mismatch for {name}")
    return name, archive, digest


def release_version():
    version = os.environ.get("WEAVEGATE_VERSION", "")
    if version:
        if not VERSION.fullmatch(version):
            raise ValueError("version must be a published vX.Y.Z release tag")
        return version
    action_ref = os.environ.get("WEAVEGATE_ACTION_REF", "")
    if not VERSION.fullmatch(action_ref):
        raise ValueError(
            "set version to a published vX.Y.Z release tag when using a SHA, "
            "branch, local action, or moving major/minor tag"
        )
    if os.environ.get("WEAVEGATE_ACTION_REPOSITORY") != "weavegate/weavegate":
        raise ValueError("set version when using an action outside weavegate/weavegate")
    url = f"https://api.github.com/repos/weavegate/weavegate/git/ref/tags/{action_ref}"
    try:
        reference = json.loads(download(url, 65536))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError("set version when the action ref is not a repository tag") from error
        raise
    if reference.get("ref") != f"refs/tags/{action_ref}":
        raise ValueError("action ref is not an exact repository tag")
    return action_ref


def install():
    root = evidence_dir()
    version = release_version()
    if platform.system() != "Linux":
        raise ValueError("the action requires a Linux runner with Docker")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise ValueError(f"unsupported Linux architecture: {platform.machine()}")
    name, archive, digest = verified_archive(version, arch)
    expected = f"{name[:-7]}/weavegate"
    with tempfile.NamedTemporaryFile(dir=os.environ["RUNNER_TEMP"], prefix="weavegate-", delete=False) as file:
        binary = Path(file.name)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                member = tar.getmember(expected)
                if not member.isfile() or member.size > 50 * 1024 * 1024:
                    raise ValueError("invalid release binary member")
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("release binary member cannot be read")
                shutil.copyfileobj(source, file)
            binary.chmod(0o700)
        except Exception:
            binary.unlink(missing_ok=True)
            raise
    (root / "install.txt").write_text(
        f"version={version}\narchive={name}\nsha256={digest}\n", encoding="utf-8"
    )
    output(binary=binary)


def parse_run(root, stdout):
    result = {"run_directory": "", "report_path": "", "schedule_id": "", "verdict": "", "evidence_complete": False, "artifact_version": None, "evidence_note": "No run directory printed."}
    lines = stdout.splitlines()
    if not lines:
        return result
    candidate = Path(lines[-1])
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return result
    if candidate.parent != root / "runs" or not candidate.is_dir():
        return result
    result["run_directory"] = str(candidate)
    report_path = candidate / "report.md"
    if report_path.is_file():
        result["report_path"] = str(report_path)
    missing = [name for name in BASE_FILES if not (candidate / name).is_file()]
    if missing:
        result["evidence_note"] = "Missing run files: " + ", ".join(missing)
        return result
    try:
        manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        scenario = json.loads((candidate / "scenario.json").read_text(encoding="utf-8"))
        observation = json.loads((candidate / "observation.json").read_text(encoding="utf-8"))
        report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
        headline = HEADLINE.match(report_path.read_text(encoding="utf-8"))
        versions = {item["artifact_version"] for item in (manifest, scenario, observation, report)}
        if len(versions) != 1 or versions.pop() not in (2, 3):
            raise ValueError("inconsistent artifact versions")
        if manifest["run_id"] != candidate.name:
            raise ValueError("manifest run ID does not match directory")
        if not headline:
            raise ValueError("report headline is missing")
        schedule = scenario.get("schedule")
        if schedule is not None:
            schedule_id = schedule["id"]
            if not SCHEDULE.fullmatch(schedule_id) or not (candidate / "schedule.json").is_file():
                raise ValueError("schedule evidence is incomplete")
            result["schedule_id"] = schedule_id
            shutil.copyfile(candidate / "schedule.json", root / "schedule.json")
        result["artifact_version"] = manifest["artifact_version"]
        result["verdict"] = headline.group(1)
        result["evidence_complete"] = True
        result["evidence_note"] = "Complete run evidence retained."
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        result["evidence_note"] = f"Run evidence is partial or invalid: {error}"
    return result


def run():
    root = evidence_dir()
    command = [os.environ["WEAVEGATE_BINARY"], "run", "--config", os.environ["WEAVEGATE_CONFIG"], "--scenario", os.environ["WEAVEGATE_SCENARIO"], "--out", str(root)]
    for key, flag in (("WEAVEGATE_VARIANT", "--variant"), ("WEAVEGATE_REPLAY", "--replay"), ("WEAVEGATE_REPEAT", "--repeat")):
        if os.environ.get(key, "") != "":
            command.extend((flag, os.environ[key]))
    status = {"exit_code": None, "verdict": "", "run_directory": "", "schedule_id": "", "report_path": "", "evidence_complete": False, "evidence_note": "weavegate was not launched"}
    try:
        process = subprocess.run(command, cwd=os.environ["GITHUB_WORKSPACE"], capture_output=True, check=False)
        status["exit_code"] = process.returncode if process.returncode >= 0 else 128 - process.returncode
        (root / "stdout.log").write_bytes(process.stdout)
        (root / "stderr.log").write_bytes(process.stderr)
        for stream, payload in ((sys.stdout, process.stdout), (sys.stderr, process.stderr)):
            for line in payload.decode("utf-8", errors="replace").splitlines():
                print("weavegate | " + line, file=stream)
        status.update(parse_run(root, process.stdout.decode("utf-8", errors="replace")))
    except (OSError, ValueError) as error:
        status["evidence_note"] = f"Action could not launch or inspect weavegate: {error}"
    (root / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    output(exit_code="" if status["exit_code"] is None else status["exit_code"], verdict=status["verdict"], run_directory=status["run_directory"], schedule_id=status["schedule_id"], report_path=status["report_path"])


def read_status(root):
    try:
        return json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exit_code": None, "verdict": "", "evidence_note": "Run status unavailable; install or execution failed.", "evidence_complete": False}


def safe_cell(value):
    return html.escape(str(value).replace("\n", " ").replace("\r", " ")).replace("|", "\\|")


def summary():
    root = Path(os.environ.get("WEAVEGATE_EVIDENCE_DIR") or "/nonexistent")
    status = read_status(root)
    upload = os.environ.get("WEAVEGATE_UPLOAD_OUTCOME", "unknown")
    artifact_url = os.environ.get("WEAVEGATE_ARTIFACT_URL", "")
    rows = [
        ("Process exit code", status.get("exit_code") if status.get("exit_code") is not None else "not launched"),
        ("Report verdict", status.get("verdict") or "unavailable"),
        ("Evidence", status.get("evidence_note", "unavailable")),
        ("Artifact upload", upload),
        ("Schedule ID", status.get("schedule_id") or "unavailable"),
        ("Run directory", status.get("run_directory") or "unavailable"),
        ("Report path", status.get("report_path") or "unavailable"),
    ]
    content = "## weavegate gate\n\n| Field | Result |\n| --- | --- |\n"
    content += "".join(f"| {safe_cell(key)} | {safe_cell(value)} |\n" for key, value in rows)
    if artifact_url and upload == "success":
        content += f"\n[Download retained evidence]({artifact_url})\n"
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(content)
    else:
        print(content)


def gate():
    root = Path(os.environ.get("WEAVEGATE_EVIDENCE_DIR") or "/nonexistent")
    status = read_status(root)
    if os.environ.get("WEAVEGATE_UPLOAD_OUTCOME") != "success":
        print("weavegate gate: evidence upload failed or was skipped", file=sys.stderr)
        return 1
    if status.get("exit_code") == 0 and status.get("evidence_complete") and status.get("artifact_version") == 2 and status.get("verdict") == "PASS":
        print("weavegate gate: PASS")
        return 0
    print(f"weavegate gate: failed (process exit {status.get('exit_code')}; {status.get('evidence_note')})", file=sys.stderr)
    return 1


def main():
    mode = sys.argv[1]
    try:
        if mode == "prepare":
            prepare()
        elif mode == "install":
            install()
        elif mode == "run":
            run()
        elif mode == "summary":
            summary()
        elif mode == "gate":
            return gate()
        else:
            raise ValueError(f"unknown mode: {mode}")
    except Exception as error:
        if mode == "install":
            root = Path(os.environ.get("WEAVEGATE_EVIDENCE_DIR", "/nonexistent"))
            if root.is_dir():
                (root / "install.txt").write_text(f"Installation failed: {error}\n", encoding="utf-8")
        print(f"weavegate action: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
