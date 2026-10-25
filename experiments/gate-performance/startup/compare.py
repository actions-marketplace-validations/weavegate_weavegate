#!/usr/bin/env python3
"""Local startup tuning only: alternate matched whole gates and retain evidence.

No E1 inference, resource-efficiency claim or candidate freeze follows from this
small shared-host comparison. Each gate keeps repeat=20 and exploration passes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

DETERMINISTIC = ("scenario.json", "observation.json", "trace.json", "report.md", "schedule.json")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def containers():
    # The transient Testcontainers reaper has its own delayed lifetime; inspect
    # application databases separately, without reading credentials or logs.
    raw = subprocess.check_output(["docker", "ps", "-aq", "--filter", "ancestor=mysql:8.4"], text=True)
    return set(raw.split())


def children(temp):
    found = []
    for p in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if str(temp).encode() + b"/weavegate-jar-" in p.read_bytes():
                found.append(int(p.parent.name))
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
    return sorted(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "config", "schedule", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--variant", required=True, choices=("fixed", "vulnerable"))
    parser.add_argument("--pairs", type=int, default=2)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("pairs must be positive")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    binaries = {arm: getattr(args, arm).resolve() for arm in ("baseline", "candidate")}
    config, schedule = args.config.resolve(), args.schedule.resolve()
    pins = {str(p): sha(p) for p in (*binaries.values(), config, schedule, Path(__file__))}
    plan = {"kind": "local-tuning-not-E1", "pairs_per_mode": args.pairs,
            "variant": args.variant, "repeat": 20, "scenario": args.scenario,
            "pins": pins, "platform": platform.platform(), "cpus": os.cpu_count(),
            "java": subprocess.run(["java", "-version"], capture_output=True, text=True, check=True).stderr,
            "mysql": subprocess.check_output(["docker", "image", "inspect", "mysql:8.4", "--format", "{{.Id}}"], text=True).strip(),
            "cache": "prepared dependencies, no cache restoration",
            "resources": "shared host defaults, no aggregate cap; no concurrent test/build commands",
            "modes": ["replay", "explore"], "order": "baseline/candidate on odd pairs; reverse on even pairs"}
    write(args.out / "plan.json", plan)
    rows = []
    try:
        for mode in plan["modes"]:
            for pair in range(1, args.pairs + 1):
                peers = {}
                for arm in (binaries if pair % 2 else reversed(binaries)):
                    if any(sha(Path(p)) != digest for p, digest in pins.items()):
                        raise ValueError("registered input changed")
                    dest = args.out / f"{mode}-{pair}-{arm}"
                    dest.mkdir()
                    temp = dest / "tmp"
                    temp.mkdir()
                    command = [str(binaries[arm]), "run", "--config", str(config),
                               "--scenario", args.scenario, "--variant", args.variant,
                               "--repeat", "20", "--out", str(dest / "artifacts")]
                    if mode == "replay":
                        command += ["--replay", str(schedule)]
                    write(dest / "command.json", command)
                    before = containers()
                    with (dest / "stdout.txt").open("wb") as stdout, (dest / "stderr.txt").open("wb") as stderr:
                        started = time.monotonic_ns()
                        result = subprocess.run(command, stdout=stdout, stderr=stderr,
                                                env={**os.environ, "TMPDIR": str(temp)})
                        elapsed = time.monotonic_ns() - started
                    row = {"mode": mode, "pair": pair, "arm": arm, "wall_ns": elapsed,
                           "exit": result.returncode, "new_mysql_containers": sorted(containers() - before),
                           "snapshot_children": children(temp),
                           "remaining_snapshots": sorted(p.name for p in temp.glob("weavegate-jar-*"))}
                    rows.append(row)
                    write(dest / "result.json", row)
                    if row["exit"] != (0 if args.variant == "fixed" else 2):
                        raise ValueError(f"unexpected gate exit: {dest}")
                    if row["new_mysql_containers"] or row["snapshot_children"] or row["remaining_snapshots"]:
                        raise ValueError(f"cleanup unproven: {dest}")
                    run, = (dest / "artifacts/runs").iterdir()
                    observation = json.loads((run / "observation.json").read_text())
                    manifest = json.loads((run / "manifest.json").read_text())
                    if manifest["cleanup_failed"] or observation["flaky"]:
                        raise ValueError(f"quality failure: {dest}")
                    row["deterministic"] = {name: sha(run / name) if (run / name).exists() else None
                                            for name in DETERMINISTIC}
                    row["sut_sha256"] = manifest["sut_sha256"]
                    row["timings"] = manifest["timings"]
                    peers[arm] = row
                    write(dest / "result.json", row)
                    print(f"{mode} pair={pair} {arm} exit={result.returncode} seconds={elapsed/1e9:.3f}", flush=True)
                if peers["baseline"]["deterministic"] != peers["candidate"]["deterministic"]:
                    raise ValueError("deterministic artifacts differ")
                if peers["baseline"]["sut_sha256"] != peers["candidate"]["sut_sha256"]:
                    raise ValueError("application JAR differs")
        if any(sha(Path(p)) != digest for p, digest in pins.items()):
            raise ValueError("registered input changed")
    finally:
        write(args.out / "results.json", rows)
        write(args.out / "SHA256SUMS.json", {p.relative_to(args.out).as_posix(): sha(p)
                                          for p in sorted(args.out.rglob("*")) if p.is_file()})


if __name__ == "__main__":
    main()
