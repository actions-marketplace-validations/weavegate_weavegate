#!/usr/bin/env python3
"""Convert the paired Spring/MySQL test log into the shared acceptance manifest.

The paired rows pass only when the live test passed with every fixed result
marker, and the Go and Java isolated manifests from the same revision are
complete at the same vector pin. This tool accounts for evidence; it does not
re-judge verdicts or run either peer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING = runpy.run_path(str(ROOT / 'scripts/check-external-sut-acceptance.py'))
TEST = 'TestSpringMatchingPairedReplay'
HANDLER = 'cmd/weavegate/spring_integration_test.go:' + TEST
PACKAGE = 'github.com/weavegate/weavegate/cmd/weavegate'
SCHEDULE = 'sch_7dcb74b1e506'
ENVIRONMENT = 'SPRING_ENVIRONMENT '
ENVIRONMENT_KEYS = ('go', 'java', 'mysql', 'spring_boot', 'spring', 'transaction_manager',
                    'jdbc_driver', 'pool', 'weavegate_spring')


def markers(repetitions):
    """Every fixed phrase the live test must emit exactly once."""
    n = str(repetitions)
    return (
        'SPRING_EXPLORE_RESULT variant=vulnerable exit=2 diagnostic=WG001 schedule=' + SCHEDULE
        + ' repeat=' + n + ' flaky=false saved=byte_identical',
        'SPRING_REPLAY_RESULT schedule=' + SCHEDULE + ' variant=vulnerable repeat=' + n
        + ' exit=2 diagnostic=WG001 violation_runs=' + n + ' flaky=false',
        'SPRING_REPLAY_RESULT schedule=' + SCHEDULE + ' variant=fixed repeat=' + n
        + ' exit=0 verdict=PASS violation_runs=0 blocked_runs=' + n + ' flaky=false',
        'SPRING_ROLLBACK_RESULT exit=0 assignments=0 jvm=reaped connections=closed',
        'SPRING_DEATH_RESULT exit=5 fault=session assignments=0 jvm=reaped connections=closed',
        'SPRING_CANCEL_RESULT during=locking_read exit=130 jvm=reaped connections=closed',
        'SPRING_LIFECYCLE_RESULT resets=checked blocked=observed rollback=rolled_back death=rolled_back'
        ' cancel=cleaned jvm=reaped connections=closed snapshots=removed',
    )


def handler_exists():
    path, name = HANDLER.split(':')
    source = (ROOT / path).read_text()
    return re.search(r'^func ' + name + r'\(t \*testing\.T\) \{', source, re.MULTILINE) is not None


def isolated(path, target, revision, plan, data):
    manifest = ACCOUNTING['decode'](path.read_bytes())
    counts = ACCOUNTING['validate_result'](manifest, plan, data, path.parent)
    if manifest['target'] != target:
        raise ValueError(path.name + ': expected a ' + target + ' manifest')
    if counts['fail'] or counts['incomplete'] or manifest['run'] is None:
        raise ValueError(target + ' isolated acceptance is not complete')
    if manifest['run']['revision'] != revision:
        raise ValueError(target + ' isolated acceptance was recorded at another revision')
    return manifest


def environment(text):
    lines = [line.split(ENVIRONMENT, 1)[1] for line in text.splitlines() if ENVIRONMENT in line]
    if len(lines) != 1:
        raise ValueError('exactly one environment record required')
    values = dict(pair.split('=', 1) for pair in lines[0].split() if '=' in pair)
    if set(values) != set(ENVIRONMENT_KEYS) or not all(values.values()):
        raise ValueError('environment record fields')
    if not values['mysql'].startswith('8.4.'):
        raise ValueError('paired evidence requires MySQL 8.4')
    return values


def record(log, go_manifest, java_manifest, revision, command, repetitions, build_tool):
    plan, data = ACCOUNTING['load_plan']()
    directory = log.parent.resolve()
    for path in (go_manifest, java_manifest):
        if not path.resolve().is_relative_to(directory):
            raise ValueError('isolated manifests must be inside the paired evidence directory')
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('full implementation revision required')
    if repetitions < 20:
        raise ValueError('at least 20 repetitions required')
    if 'WEAVEGATE_SPRING_PAIRED=1' not in command or TEST not in command:
        raise ValueError('command must run the paired Spring test')
    if not build_tool.strip():
        raise ValueError('build tool version required')
    if not handler_exists():
        raise ValueError('missing paired test handler: ' + HANDLER)
    raw = log.read_bytes()
    text = raw.decode('utf-8')
    if re.search(r'^\s*(?:FAIL|--- FAIL:|--- SKIP:|panic:)', text, re.MULTILINE):
        raise ValueError('paired test log contains failures or skips')
    if not re.search(r'^--- PASS: ' + TEST + r' \(', text, re.MULTILINE):
        raise ValueError('paired test did not pass')
    if not re.search(r'^ok\s+' + re.escape(PACKAGE) + r'\s', text, re.MULTILINE):
        raise ValueError('successful package result missing')
    for marker in markers(repetitions):
        if sum(marker in line for line in text.splitlines()) != 1:
            raise ValueError('missing or repeated marker: ' + marker.split(' ', 1)[0])
    versions = environment(text)
    isolated(go_manifest, 'go', revision, plan, data)
    isolated(java_manifest, 'java', revision, plan, data)

    def artifact(path):
        return {'path': path.resolve().relative_to(directory).as_posix(),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    result = ACCOUNTING['template'](plan, data, 'paired')
    evidence = {
        'requirement/paired-live-peers': ['paired-log', 'go-manifest', 'java-manifest'],
        'requirement/paired-mysql-replay': ['paired-log'],
    }
    if set(evidence) != set(result['results']):
        raise ValueError('paired inventory changed; update the recorder')
    for row, ids in evidence.items():
        result['results'][row] = {'status': 'pass', 'reason': '', 'checks': {
            'observe/evidence': {'status': 'pass', 'handler': HANDLER, 'evidence': ids}}}
    result['run'] = {
        'revision': revision, 'command': command, 'repetitions': repetitions,
        'repetition_method': 'Each CLI replay uses --repeat N; every repetition resets the fixture and launches '
                             'a fresh child JVM, and one fingerprint must cover all N runs.',
        'versions': {
            'go': versions['go'], 'java': versions['java'], 'mysql': versions['mysql'],
            'spring': 'Spring Boot ' + versions['spring_boot'] + ' / Spring Framework ' + versions['spring'],
            'transaction_manager': 'DataSourceTransactionManager (' + versions['transaction_manager'] + ')',
            'jdbc_driver': 'MySQL Connector/J ' + versions['jdbc_driver'],
            'pool': 'HikariCP ' + versions['pool'],
            'build_tool': build_tool.strip(),
            'weavegate_spring': versions['weavegate_spring'],
        },
        'artifacts': {'paired-log': artifact(log), 'go-manifest': artifact(go_manifest),
                      'java-manifest': artifact(java_manifest)},
    }
    ACCOUNTING['validate_result'](result, plan, data, directory)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--go-manifest', type=Path, required=True)
    parser.add_argument('--java-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--command', required=True)
    parser.add_argument('--repetitions', type=int, default=20)
    parser.add_argument('--build-tool', required=True)
    args = parser.parse_args()
    try:
        if args.log.resolve().parent != args.output.resolve().parent or args.log.resolve() == args.output.resolve():
            raise ValueError('manifest and distinct log must share a directory')
        result = record(args.log, args.go_manifest, args.java_manifest, args.revision, args.command,
                        args.repetitions, args.build_tool)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    except (ValueError, OSError, KeyError, TypeError) as err:
        print('EXTERNAL_SUT_PAIRED_RECORD_ERROR ' + str(err), file=sys.stderr)
        return 1
    print('EXTERNAL_SUT_PAIRED_RECORD_RESULT markers=required isolated=complete pin=shared')
    return 0


if __name__ == '__main__':
    sys.exit(main())
