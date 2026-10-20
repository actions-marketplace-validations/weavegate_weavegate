#!/usr/bin/env python3
"""Freeze, collect once in protocol order, and analyze an E5 bundle."""
import argparse
import fcntl
import os
import re
import shutil
import time
from support import *
from analyze import analyze, reduce_slot
from stream import Monitor


def validate(m):
    require(type(m.get('schema')) is int and m['schema'] == 1 and type(m.get('protocol_revision')) is int and m['protocol_revision'] == 1, 'schema/protocol revision must be 1')
    require(m.get('phase') in ('feasibility', 'measurement'), 'phase required')
    require(isinstance(m.get('campaign_id'), str) and m['campaign_id'], 'campaign ID required')
    require(m.get('seed') == SEED, 'protocol seed differs')
    require(m.get('protocol', {}).get('sha256') == PROTOCOL_SHA256, 'frozen protocol differs')
    require(m.get('pins'), 'live input inventory required')
    live_pins({'pins': m['pins']})
    for workload, spec in WORKLOADS.items():
        for variant in ('vulnerable', 'fixed'):
            inp = m['inputs'][workload][variant]
            require(inp['candidates'] == candidates(workload), 'candidate inventory/order differs')
            require(json_equal(inp['config']['Run'], {'Repeat': 20, 'ArriveTimeoutMS': 1000, 'ExplorePasses': 3}), 'effective run settings differ')
            require(inp['config']['Target']['SUT']['External']['Capacity'] == 2, 'capacity differs')
            for name, value in (('StartupTimeoutMS', 30000), ('CancelTimeoutMS', 5000), ('StopTimeoutMS', 20000)):
                require(inp['config']['Target']['SUT']['External'][name] == value, name + ' differs')
            require(all(type(n) is int and n > 0 for n in inp['counts'].values()), 'positive execution counts required')
            if m['phase'] == 'measurement':
                require(inp['counts'] == {'B1': 20, 'B2': 100, 'T-R': 20}, 'measurement counts differ')
            for arm in ARMS:
                command = m['commands'][workload][variant][arm]
                command_valid(command)
                require(command['timeout_seconds'] == CAPS[arm], 'arm collection cap differs')
                require(command['argv'][0] in m['pins'], 'command executable is unpinned')
                if m['phase'] == 'measurement' and arm in ('T-R', 'T-E'):
                    require(m['pins'][command['argv'][0]] == m['candidate']['cli_sha256'],
                            'T command executable differs from registered CLI')
    for hook in m.get('hooks', {}).values():
        command_valid(hook)
        require(hook['argv'][0] in m['pins'], 'hook executable is unpinned')
    if m['phase'] == 'measurement':
        require(set(m['hooks']) == {'prepare', 'warm_cleanup', 'observe', 'cleanup'}, 'measurement lifecycle hooks required')
        cleanup_dependencies(m)
        require(m['environment']['vcpus'] == 4 and m['environment']['memory_bytes'] == 16 * 1024**3,
                'protocol resource caps required')
        for key in ('host_id', 'vm_image', 'cpu_model', 'kernel', 'docker', 'background_policy'):
            require(m['environment'].get(key), 'missing environment identity: ' + key)
        require(re.fullmatch(r'.+@sha256:[0-9a-f]{64}', m['environment']['mysql_image']), 'MySQL digest required')
        require(m['candidate']['cli_sha256'] == m['e1_registration']['cli_sha256']
                and m['candidate']['sdk_sha256'] == m['e1_registration']['sdk_sha256'], 'E1 candidate identity differs')
        require(m['candidate']['cli_sha256'] in m['pins'].values(), 'candidate binary absent from pins')
        require(all(v['sdk_sha256'] == m['candidate']['sdk_sha256']
                    for workload in m['inputs'].values() for v in workload.values()), 'fixture SDK differs from E1')
        for key in ('url', 'revision', 'cli_sha256', 'sdk_sha256'):
            require(m['e1_registration'].get(key), 'E1 registration incomplete')
        for key in ('go_acceptance', 'java_acceptance', 'paired_acceptance', 'corpus_acceptance', 'baseline_feasibility'):
            p = m['prerequisites'][key]
            require(p in m['pins'], 'missing prerequisite evidence: ' + key)
        require(m.get('fixture_revision') == 'e1bd8ba3fc07d024b8cb4037aa2263d24044dbb0', 'fixture revision differs')
        measurement_dependencies(m)


def freeze(source, destination):
    source, destination = Path(source), Path(destination)
    m = read_json(source)
    validate(m)
    destination.mkdir(parents=True, exist_ok=False)
    frozen = destination / 'frozen'; frozen.mkdir()
    write_json(frozen / 'manifest.json', m)
    write_json(frozen / 'order.json', order())
    shutil.copyfile(m['protocol']['path'], frozen / 'protocol.md')
    require(digest(frozen / 'protocol.md') == m['protocol']['sha256'], 'protocol pin differs')
    originals = frozen / 'inputs'; originals.mkdir()
    # Retain bytes, not hashes alone; identical files are stored once by digest.
    for path, checksum in m['pins'].items():
        target = originals / checksum
        if not target.exists(): shutil.copyfile(path, target)
        require(digest(target) == checksum, 'input changed while freezing: ' + path)
    sources = frozen / 'source'; sources.mkdir()
    for p in HERE.glob('*.py'): shutil.copyfile(p, sources / p.name)
    shared = sources / 'shared'; shared.mkdir()
    for module in SHARED_MODULES:
        name = module + '.py'
        shutil.copyfile(SHARED / name, shared / name)
    seal(frozen)
    (destination / 'slots').mkdir()
    save_progress(destination, {})
    print('E5_FREEZE_RESULT slots=600 blocks=120 attempts=1')


def proof(directory, filename, expected, previous=None):
    receipt = read_json(directory / filename)
    require(all(json_equal(receipt.get(k), v) for k, v in expected.items()), filename + ' boundary differs')
    require(receipt.get('evidence'), filename + ' needs original evidence')
    for name, checksum in receipt['evidence'].items():
        require('/' in name and name.split('/')[0] == 'proof', 'proof originals must be under proof/')
        require(previous is None or name not in previous, filename + ' proof must be newly retained')
        require(digest(child(directory, name)) == checksum, filename + ' original differs')
    return receipt


def protect(bundle, active):
    """Authenticate frozen inputs and every previously sealed slot at boundaries."""
    verify(bundle / 'frozen')
    verify(bundle / 'progress')
    retained_registration(bundle, read_json(bundle / 'progress' / 'checkpoint.json'))
    for path in (bundle / 'slots').iterdir():
        if path != active: verify(path)


def unchanged(directory, before):
    for name, checksum in before.items():
        require(digest(child(directory, name)) == checksum, 'hook changed original: ' + name)


def registered(bundle, m, registration):
    if m['phase'] == 'feasibility': return
    verify(bundle / 'progress')
    progress = read_json(bundle / 'progress' / 'checkpoint.json')
    retained_value = retained_registration(bundle, progress)
    require(retained_value is not None or (not progress['slots'] and progress['active'] is None),
            'registration missing after collection began')
    require(registration is not None, 'public registration required before measurement')
    value = read_json(registration)
    for key, name in (('manifest_sha256', 'manifest.json'), ('order_sha256', 'order.json')):
        require(value[key] == digest(bundle / 'frozen' / name), 'registration hash differs')
    require(value.get('public_url', '').startswith('https://') and value.get('revision'), 'public registration identity required')
    require(timestamp(value['registered_at']) < timestamp(utc()), 'registration must precede collection')
    retained = bundle / 'registration.json'
    if retained.exists(): require(canonical(read_json(retained)) == canonical(value), 'registration changed')
    else: write_json(retained, value)
    if retained_value is None:
        progress['registration'] = value
        write_json(bundle / 'progress' / 'checkpoint.json', progress)
        seal(bundle / 'progress')


def collect(bundle, registration=None, max_slots=None):
    bundle = Path(bundle).resolve()
    with (bundle / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify(bundle / 'frozen')
        m = read_json(bundle / 'frozen' / 'manifest.json')
        slots = read_json(bundle / 'frozen' / 'order.json')
        require(slots == order(), 'order differs')
        progress = retained_progress(bundle, m, slots)
        require(progress['stopped'] is None, 'campaign stopped; no replacement attempts')
        for path in (bundle / 'frozen/source').glob('*.py'):
            require(digest(HERE / path.name) == digest(path), 'collector/analyzer source changed after freeze')
        for path in (bundle / 'frozen/source/shared').glob('*.py'):
            require(digest(SHARED / path.name) == digest(path), 'shared collector source changed after freeze')
        require(max_slots is None or (m['phase'] == 'feasibility' and max_slots > 0), 'positive slot limit is feasibility-only')
        registered(bundle, m, registration)
        live_pins(m)
        consumed = progress['slots']
        done = 0
        for slot in slots:
            directory = bundle / 'slots' / slot['id']
            if directory.exists(): continue
            if max_slots is not None and done >= max_slots: break
            live_pins(m)
            save_progress(bundle, consumed, active=slot['id'])
            directory.mkdir()
            context = dict(slot, trial_dir=str(directory), campaign_dir=str(bundle), redact_env=[])
            state = {'slot': slot, 'started_at': utc(), 'cleanup_proven': False}
            write_json(directory / 'slot.json', state)
            stop_reason = None
            unsupported = m.get('unsupported', {}).get(slot['workload'] + '/' + slot['arm'])
            if unsupported:
                state['unsupported'] = unsupported
                write_json(directory / 'slot.json', state)
                seal(directory)
                consumed[slot['id']] = digest(directory / 'SHA256SUMS')
                save_progress(bundle, consumed)
                done += 1
                continue
            try:
                protect(bundle, directory)
                if m['phase'] == 'measurement':
                    prep = execute(m['hooks']['prepare'], context, directory, 'prepare', [])
                    require(succeeded(prep), 'restore failed')
                    receipt = proof(directory, 'preparation.json', {'host_id': m['environment']['host_id'],
                                    'disk_image': m['environment']['vm_image'], 'fresh_memory': True,
                                    'clean_data': True, 'vcpus': 4, 'memory_bytes': 16 * 1024**3})
                    state['preparation'] = receipt
                    if slot['cache'] == 'warm':
                        warm = directory / 'warmup'; warm.mkdir()
                        warm_context = dict(context, trial_dir=str(warm), arm='T-R')
                        cmd = execute(m['commands'][slot['workload']][slot['variant']]['T-R'], warm_context, warm, 'run', [])
                        from analyze import product
                        observed = product(warm, dict(slot, arm='T-R'), cmd, m['inputs'][slot['workload']][slot['variant']])
                        require(cmd['exit'] in (0, 2) and succeeded(dict(cmd, exit=0))
                                and cmd.get('containment_proven') is True and observed['cleanup_proven']
                                and not observed['quality_reasons'] and not observed['operational_reasons'], 'warm-up failed')
                        state['warmup'] = {'seconds': cmd['seconds'], 'exit': cmd['exit']}
                        before = inventory(directory)
                        cleanup = execute(m['hooks']['warm_cleanup'], context, directory, 'warm-cleanup', [])
                        require(succeeded(cleanup), 'warm-up output/data cleanup failed')
                        proof(directory, 'warm-cleanup.json', {'outputs_removed': True, 'data_removed': True,
                              'cache_preserved': True}, previous=before)
                        unchanged(directory, before)
                    protect(bundle, directory)
                live_pins(m)
                original = inventory(directory)
                spec = m['commands'][slot['workload']][slot['variant']][slot['arm']]
                start = time.monotonic()
                with Monitor(directory):
                    cmd = execute(spec, context, directory, 'run', [])
                # Baseline evaluations are persisted per execution. Product artifacts
                # arrive at CLI completion; endpoint validation is separately timed.
                from analyze import baseline, product
                expected = m['inputs'][slot['workload']][slot['variant']]
                try:
                    observed = (baseline if slot['arm'].startswith('B') else product)(directory, slot, cmd, expected)
                    state['evidence_received_seconds'] = [cmd['seconds'], time.monotonic() - start]
                    state['cleanup_proven'] = observed['cleanup_proven']
                except (OSError, ValueError, KeyError, TypeError) as error:
                    state['error'] = 'evidence: ' + str(error)
                unchanged(directory, original)
                protect(bundle, directory)
                if m['phase'] == 'measurement':
                    live_pins(m)
                    observed_files = inventory(directory)
                    observer = execute(m['hooks']['observe'], context, directory, 'observe', [])
                    require(succeeded(observer), 'observation failed')
                    receipt = proof(directory, 'observation.json', {'host_id': m['environment']['host_id']}, previous=observed_files)
                    state['resources'] = receipt['resources']
                    unchanged(directory, observed_files)
                    protect(bundle, directory)
            except (Exception, KeyboardInterrupt) as error:
                state['error'] = str(error)
                if isinstance(error, KeyboardInterrupt): stop_reason = 'collector interrupted'
            finally:
                if not state.get('unsupported'):
                    if m['phase'] == 'measurement':
                        # Cleanup is unconditional, but changed cleanup dependencies
                        # are not trusted. No reuse follows a failed proof.
                        with defer_interrupts():
                            try:
                                trusted = cleanup_dependencies(m)
                                live_pins({'pins': {p: m['pins'][p] for p in trusted}})
                                before_cleanup = inventory(directory)
                                cleanup = execute(m['hooks']['cleanup'], context, directory, 'cleanup', [])
                                require(succeeded(cleanup), 'cleanup command failed')
                                proof(directory, 'cleanup.json', {'proven': True}, previous=before_cleanup)
                                unchanged(directory, before_cleanup)
                                state['cleanup_proven'] = True
                            except (Exception, KeyboardInterrupt) as error:
                                state['cleanup_proven'] = False
                                state['cleanup_error'] = str(error)
                    if state['cleanup_proven'] is not True: stop_reason = 'allocation cleanup unproven'
                for check in (lambda: live_pins(m), lambda: protect(bundle, directory)):
                    try: check()
                    except (ValueError, OSError) as error:
                        stop_reason = str(error)
                        state.setdefault('integrity_errors', []).append(stop_reason)
                state['ended_at'] = utc()
                stopped = {'reason': stop_reason, 'slot': slot['id'], 'at': utc()} if stop_reason else None
                state['stop'] = stopped
                write_json(directory / 'slot.json', state)
                seal(directory)
                consumed[slot['id']] = digest(directory / 'SHA256SUMS')
                save_progress(bundle, consumed, stopped=stopped)
                if stopped: write_json(bundle / 'stopped.json', stopped)
            done += 1
            if stop_reason: break
        print('E5_COLLECT_RESULT consumed=%d stopped=%s phase=%s' % (done, (bundle / 'stopped.json').exists(), m['phase']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    f = sub.add_parser('freeze'); f.add_argument('manifest'); f.add_argument('bundle')
    c = sub.add_parser('collect'); c.add_argument('bundle'); c.add_argument('--registration'); c.add_argument('--max-slots', type=int)
    a = sub.add_parser('analyze'); a.add_argument('bundle'); a.add_argument('output')
    args = parser.parse_args()
    if args.action == 'freeze': freeze(args.manifest, args.bundle)
    elif args.action == 'collect': collect(args.bundle, args.registration, args.max_slots)
    else:
        analyze(args.bundle, args.output)
        print('E5_ANALYSIS_RESULT source=retained_json slots=600')


if __name__ == '__main__': main()
