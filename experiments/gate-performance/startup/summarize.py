#!/usr/bin/env python3
"""Verify an extracted A3 evidence bundle and reproduce its descriptive table."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

FILES = ("scenario.json", "observation.json", "trace.json", "report.md", "schedule.json")
FACTS = ("schema_version", "seed_data", "isolation_level", "engine", "adapter", "variant", "image", "sut_sha256")


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    inventory = read(root / "SHA256SUMS.json")
    actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(root.rglob("*")) if p.is_file() and p != root / "SHA256SUMS.json"}
    require(actual == inventory, "bundle inventory/checksum mismatch")
    print("| Workload | Parent seconds | Candidate seconds | Change | Executions per arm |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for variant in ("fixed", "vulnerable"):
        campaign = root / ("whole-" + variant)
        plan = read(campaign / "plan.json")
        require(plan["pairs_per_mode"] == 1 and plan["repeat"] == 20, "unexpected collection plan")
        require(len(read(campaign / "results.json")) == 4, "incomplete collection")
        for mode in ("replay", "explore"):
            peers = {}
            executions = 20 if mode == "replay" else (18 if variant == "fixed" else 21)
            for arm in ("baseline", "candidate"):
                dest = campaign / f"{mode}-1-{arm}"
                result = read(dest / "result.json")
                command = read(dest / "command.json")
                run, = (dest / "artifacts/runs").iterdir()
                observation = read(run / "observation.json")
                manifest = read(run / "manifest.json")
                require(result["exit"] == (0 if variant == "fixed" else 2), "unexpected exit")
                require(not any(result[k] for k in ("new_mysql_containers", "snapshot_children", "remaining_snapshots")), "cleanup incomplete")
                require(not manifest["cleanup_failed"] and not observation["flaky"], "quality failure")
                require(command[command.index("--repeat") + 1] == "20", "repeat changed")
                require(sum(s["name"] == "schedule_run" for s in manifest["timings"]["spans"]) == executions, "execution count changed")
                require(observation["timeouts"] == {"arrive_timeout_ms": 1000, "block_inference_timeout_ms": 1000,
                        "step_timeout_ms": 20000, "run_timeout_ms": 90000, "stop_timeout_ms": 20000}, "budgets changed")
                if mode == "explore":
                    # This field records completed passes: vulnerable stops
                    # at the first discovery before 20 confirmation replays.
                    require(observation["explore_passes"] == (3 if variant == "fixed" else 1), "exploration passes changed")
                if mode == "replay" or variant == "vulnerable":
                    require(observation["repeat"] == 20 and len(observation["fingerprints"]) == 1
                            and sum(observation["fingerprints"].values()) == 20, "repeat evidence incomplete")
                    require(read(run / "schedule.json")["id"] == "sch_7dcb74b1e506", "schedule changed")
                if arm == "candidate":
                    require(manifest["sut_jvm_tuning"] == "startup", "candidate policy missing")
                peers[arm] = (result, manifest, run)
            b, w = peers["baseline"], peers["candidate"]
            require(all(b[1][key] == w[1][key] for key in FACTS), "environment or JAR mismatch")
            for name in FILES:
                bp, wp = b[2] / name, w[2] / name
                require(bp.exists() == wp.exists(), "artifact presence mismatch")
                require(not bp.exists() or bp.read_bytes() == wp.read_bytes(), "deterministic artifacts differ")
            bs, ws = b[0]["wall_ns"] / 1e9, w[0]["wall_ns"] / 1e9
            print(f"| {variant} {mode} | {bs:.3f} | {ws:.3f} | {100 * (ws / bs - 1):+.1f}% | {executions} |")
    print("\n| Java stage (median of 3 startups) | Parent ms | Candidate ms |")
    print("| --- | ---: | ---: |")
    rows = {}
    for arm in ("baseline", "candidate"):
        rows[arm] = [json.loads(line) for line in (root / f"{arm}-startup.jsonl").read_text().splitlines()]
        require(len(rows[arm]) == 3, "attribution count changed")
        require(all(set(row) == {"initialize_ns", "registration_ns", "probe_ns"}
                    and all(type(value) is int and value > 0 for value in row.values()) for row in rows[arm]), "invalid attribution")
    for stage in ("initialize_ns", "registration_ns", "probe_ns"):
        values = [statistics.median(row[stage] for row in rows[arm]) / 1e6 for arm in rows]
        print(f"| {stage.removesuffix('_ns')} | {values[0]:.3f} | {values[1]:.3f} |")


if __name__ == "__main__":
    main()
