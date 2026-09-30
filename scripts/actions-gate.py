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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


RELEASES = "https://github.com/weavegate/weavegate/releases/download"
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?")
SCHEDULE = re.compile(r"sch_[0-9a-f]{12}")
HEADLINE = re.compile(r"## weavegate: (PASS|FAIL|FLAKY)(?: \([^\n]*\))?\n")
BASE_FILES = ("manifest.json", "scenario.json", "observation.json", "trace.json", "report.json", "report.md")
# Keep the complete UTF-8 comment body within this action's 64 KiB budget.
COMMENT_LIMIT = 65536
COMMENT_MARKER = "<!-- weavegate-gate-comment v1 -->"
REPORT_BEGIN = "<!-- weavegate-report-begin -->"
REPORT_END = "<!-- weavegate-report-end -->"
REPLAY_GUIDE = "https://github.com/weavegate/weavegate/blob/main/docs/howto/ci-gate.md#pull-request-comment"
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")
SERVER_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?")


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


def report_embed_obstacle(report):
    """Return why report bytes cannot appear unchanged in a comment, or ""."""
    try:
        text = report.decode("utf-8")
    except UnicodeDecodeError:
        return "it contains bytes that a comment cannot carry unchanged"
    if not text.endswith("\n") or any(char != "\n" and unicodedata.category(char) == "Cc" for char in text):
        return "it contains bytes that a comment cannot carry unchanged"
    return ""


def fenced_report(text):
    # A fence longer than every backtick run in the report cannot be closed by
    # the report, so its content stays literal text whatever it contains.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(4, longest + 1)
    return f"{REPORT_BEGIN}\n{fence}text\n{text}{fence}\n{REPORT_END}\n"


def server_origin_and_host(value):
    """Accept only a root HTTPS server URL before using it in links or commands."""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "", ""
    if (parts.scheme != "https" or not SERVER_HOST.fullmatch(parts.netloc)
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return "", ""
    return f"https://{parts.netloc}", parts.netloc


def comment_body(status, report, size, context):
    """Wrap the stored report for a pull request comment.

    report is None when the file alone already exceeds the comment limit.
    Report content is only ever placed inside the literal block. Every value
    written into the wrapper or its commands comes from the runner or the
    action and has matched a closed grammar first.
    """
    exit_code = status.get("exit_code")
    run_name = Path(status.get("run_directory") or "").name
    run_name = run_name if SAFE_NAME.fullmatch(run_name) else ""
    schedule_id = status.get("schedule_id") or ""
    schedule_id = schedule_id if SCHEDULE.fullmatch(schedule_id) else ""
    repository = context["repository"] if REPOSITORY.fullmatch(context["repository"]) else ""
    server_url, server_host = server_origin_and_host(context["server_url"])
    run_id = context["run_id"] if context["run_id"].isascii() and context["run_id"].isdigit() else ""
    artifact_name = context["artifact_name"] if SAFE_NAME.fullmatch(context["artifact_name"]) else ""
    version = context["version"] if VERSION.fullmatch(context["version"]) else ""
    uploaded = context["upload_outcome"] == "success" and context["artifact_url"].startswith("https://")
    verdict = status.get("verdict") if status.get("verdict") in ("PASS", "FAIL", "FLAKY") else ""

    # Only the key facts stay outside the collapsed sections.
    facts = []
    if verdict:
        facts.append(f"Report verdict **{verdict}**")
    if schedule_id:
        facts.append(f"schedule `{schedule_id}`")
    facts.append(f"[evidence artifact]({context['artifact_url']})" if uploaded else "evidence upload did not succeed, so this run has no downloadable evidence")
    if server_url and repository and run_id:
        facts.append(f"[workflow run {run_id}]({server_url}/{repository}/actions/runs/{run_id})")
    head = f"{COMMENT_MARKER}\n### weavegate gate: process exit code {exit_code if isinstance(exit_code, int) else 'unavailable'}\n\n"
    line = " · ".join(facts)
    head += line[0].upper() + line[1:] + "\n\n"

    tail = ""
    if uploaded and schedule_id:
        tail += f"<details>\n<summary>Replay schedule <code>{schedule_id}</code></summary>\n\n"
        tail += "1. Check out the revision this workflow run tested and install weavegate" + (f" `{version}`.\n" if version else ".\n")
        if server_url and repository and run_id and artifact_name:
            repository_selector = repository if server_host == "github.com" else f"{server_host}/{repository}"
            tail += "2. Download the artifact and import its schedule from the repository root:\n\n"
            tail += "   ```sh\n"
            tail += f"   gh run download {run_id} --repo {repository_selector} --name {artifact_name} --dir weavegate-evidence\n"
            tail += "   mkdir -p .weavegate/schedules\n"
            tail += f"   cp weavegate-evidence/schedule.json .weavegate/schedules/{schedule_id}.json\n"
            tail += "   ```\n\n"
        else:
            tail += f"2. Download the artifact and copy its `schedule.json` to `.weavegate/schedules/{schedule_id}.json` under the repository root.\n"
        tail += "3. From the repository root, run the command on the report's `replay:` line. If that line contains a backslash escape it is a display form; rebuild the command from its original argument values.\n"
        tail += f"\nThe [CI gate how-to]({REPLAY_GUIDE}) describes this comment and the replay in full.\n\n</details>\n\n"
    tail += "Set `comment: 'false'` on the weavegate action to turn this comment off.\n"

    oversized = f"the comment would exceed GitHub's {COMMENT_LIMIT}-character limit"
    obstacle = oversized if report is None else report_embed_obstacle(report)
    if not obstacle:
        summary = "Stored <code>report.md</code>, unchanged and shown as literal text"
        body = head + f"<details>\n<summary>{summary}</summary>\n\n" + fenced_report(report.decode("utf-8")) + "\n</details>\n\n" + tail
        if len(body.encode("utf-8")) <= COMMENT_LIMIT:
            return body, True
        obstacle = oversized
    where = f" Read `runs/{run_name}/report.md` in the evidence artifact." if uploaded and run_name else ""
    return head + f"The stored `report.md` ({size} bytes) is not embedded here: {obstacle}. It was not truncated.{where}\n\n" + tail, False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A followed redirect would resend the Authorization header elsewhere.
    def redirect_request(self, *args, **kwargs):
        return None


def post_comment(api_url, repository, number, token, body):
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/repos/{repository}/issues/{number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "weavegate-actions-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        if response.status != 201:
            raise ValueError(f"unexpected HTTP status {response.status}")
        url = json.loads(response.read(1024 * 1024).decode("utf-8"))["html_url"]
    if not isinstance(url, str) or not url.startswith("https://") or "\n" in url or "\r" in url:
        raise ValueError("response has no comment URL")
    return url


def installed_version(root):
    try:
        for line in (root / "install.txt").read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                return line[len("version="):]
    except (OSError, ValueError):
        pass
    return ""


def decide_comment():
    """Post the stored report on the pull request; return (outcome, detail, url).

    The outcome is one of disabled, skipped, posted, or failed. Nothing here
    reads or writes the gate's inputs, so no outcome can change the verdict.
    """
    requested = os.environ.get("WEAVEGATE_COMMENT", "")
    if requested == "false":
        return "disabled", "the comment input is false", ""
    if requested != "true":
        return "skipped", "the comment input must be true or false", ""
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pull_request = event["pull_request"]
        number = pull_request["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("invalid pull request number")
    except (OSError, KeyError, TypeError, ValueError):
        return "skipped", "the workflow event is not a pull request", ""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not REPOSITORY.fullmatch(repository):
        return "skipped", "the repository name is unavailable", ""
    root = Path(os.environ.get("WEAVEGATE_EVIDENCE_DIR") or "/nonexistent")
    status = read_status(root)
    try:
        report_path = Path(status.get("report_path") or "/nonexistent")
        if report_path.parent.parent != root.resolve() / "runs" or not report_path.is_file():
            raise OSError("no stored report")
        size = report_path.stat().st_size
        report = report_path.read_bytes() if size <= COMMENT_LIMIT else None
    except (OSError, TypeError, ValueError):
        return "skipped", "this run has no stored report", ""
    token = os.environ.get("WEAVEGATE_COMMENT_TOKEN", "")
    if not token:
        return "skipped", "no token is available", ""
    body, _ = comment_body(status, report, size, {
        "repository": repository,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "server_url": os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
        "artifact_name": os.environ.get("WEAVEGATE_ARTIFACT_NAME", ""),
        "artifact_url": os.environ.get("WEAVEGATE_ARTIFACT_URL", ""),
        "upload_outcome": os.environ.get("WEAVEGATE_UPLOAD_OUTCOME", ""),
        "version": installed_version(root),
    })
    try:
        url = post_comment(os.environ.get("GITHUB_API_URL", "https://api.github.com"), repository, number, token, body)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            head = pull_request.get("head") if isinstance(pull_request.get("head"), dict) else {}
            fork = isinstance(head.get("repo"), dict) and head["repo"].get("full_name") != repository
            reason = "a fork pull request gets a read-only token" if fork else "the token cannot write pull request comments"
            return "skipped", f"{reason} (HTTP {error.code})", ""
        return "failed", f"GitHub answered HTTP {error.code}", ""
    except (OSError, KeyError, TypeError, ValueError) as error:
        return "failed", f"the comment request did not complete ({type(error).__name__})", ""
    return "posted", "the stored report was posted", url


def comment():
    try:
        outcome, detail, url = decide_comment()
    except Exception as error:
        outcome, detail, url = "failed", f"the comment step stopped ({type(error).__name__})", ""
    level = {"failed": "::warning::", "skipped": "::notice::"}.get(outcome, "")
    print(f"{level}weavegate comment: {outcome}: {detail}; the gate result is unaffected")
    try:
        output(comment_outcome=outcome, comment_url=url)
        destination = os.environ.get("GITHUB_STEP_SUMMARY")
        if destination:
            with open(destination, "a", encoding="utf-8") as stream:
                stream.write(f"\nPull request comment: {outcome} ({safe_cell(detail)})" + (f" — [open]({url})\n" if url else "\n"))
    except (OSError, ValueError) as error:
        print(f"::warning::weavegate comment: could not record the outcome ({type(error).__name__})")


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
        elif mode == "comment":
            comment()
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
