#!/usr/bin/env python3
"""Freeze, collect once in protocol order, and analyze an E5 bundle."""
import argparse
import os
import re
import shutil
import time
import signal
import math
import runpy
from contextlib import ExitStack
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support', 'analyze', 'stream')})
from support import *
from analyze import analyze, command_receipt, reduce_slot
from stream import Monitor


def validate(m):
    require(type(m.get('schema')) is int and m['schema'] == 1 and type(m.get('protocol_revision')) is int and m['protocol_revision'] == 1, 'schema/protocol revision must be 1')
    require(m.get('phase') in ('feasibility', 'measurement'), 'phase required')
    campaign_deviations(m)
    unsupported_declarations(m)
    campaign_id(m)
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
        require(m.get('fixture_revision') == FIXTURE_REVISION, 'fixture revision differs')
        domain_expectations(m)
        measurement_dependencies(m)


def freeze(source, destination):
    source, destination = Path(source), Path(destination)
    m = read_json(source)
    validate(m)
    m['execution_runtime'] = runtime_identity()
    runtime = m['execution_runtime']
    require(m['pins'].get(runtime['path'], runtime['sha256']) == runtime['sha256'], 'execution runtime pin differs')
    m['source_inventory'] = {name: digest(HERE / name) for name in RUNNER_SOURCES}
    m['source_inventory'].update({'shared/' + name + '.py': digest(SHARED / (name + '.py'))
                                  for name in SHARED_MODULES})
    destination.mkdir(parents=True, exist_ok=False)
    frozen = destination / 'frozen'; frozen.mkdir()
    write_json(frozen / 'manifest.json', m)
    write_json(frozen / 'order.json', order())
    shutil.copyfile(m['protocol']['path'], frozen / 'protocol.md')
    require(digest(frozen / 'protocol.md') == m['protocol']['sha256'], 'protocol pin differs')
    originals = frozen / 'inputs'; originals.mkdir()
    # Retain bytes, not hashes alone; identical files are stored once by digest.
    for path, checksum in dict(m['pins'], **{runtime['path']: runtime['sha256']}).items():
        target = originals / checksum
        if not target.exists(): shutil.copyfile(path, target)
        require(digest(target) == checksum, 'input changed while freezing: ' + path)
    sources = frozen / 'source'; sources.mkdir()
    for name in RUNNER_SOURCES: shutil.copyfile(HERE / name, sources / name)
    shared = sources / 'shared'; shared.mkdir()
    for module in SHARED_MODULES:
        name = module + '.py'
        shutil.copyfile(SHARED / name, shared / name)
    seal(frozen)
    frozen_sources(destination)
    (destination / 'slots').mkdir()
    save_progress(destination, {})
    print('E5_FREEZE_RESULT slots=600 blocks=120 attempts=1')


def hook_snapshot(directory):
    snapshot = {}
    for path in files(directory):
        metadata = os.stat(path, follow_symlinks=False)
        snapshot[str(path.relative_to(directory))] = {
            'sha256': digest(path), 'identity': (metadata.st_dev, metadata.st_ino)}
    return snapshot


def proof(directory, filename, expected, previous=None):
    receipt = read_json(directory / filename)
    require(all(json_equal(receipt.get(k), v) for k, v in expected.items()), filename + ' boundary differs')
    require(receipt.get('evidence'), filename + ' needs original evidence')
    identities = {record['identity'] for record in (previous or {}).values()}
    for name, checksum in receipt['evidence'].items():
        require('/' in name and name.split('/')[0] == 'proof', 'proof originals must be under proof/')
        require(previous is None or name not in previous, filename + ' proof must be newly retained')
        path = child(directory, name)
        metadata = os.stat(path, follow_symlinks=False)
        require(metadata.st_nlink == 1 and (metadata.st_dev, metadata.st_ino) not in identities,
                filename + ' proof must be newly retained, without earlier inode or hard-link aliases')
        require(digest(path) == checksum, filename + ' original differs')
    return receipt


def warm_cache(receipt, directory):
    require(receipt.get('warmup_command_sha256') == digest(directory / 'warmup' / 'run.command.json'),
            'warm cache receipt differs from this warm-up')
    caches = receipt.get('startup_caches')
    require(isinstance(caches, list), 'warm startup cache inventory required (empty when none generated)')
    paths = set()
    for cache in caches:
        require(isinstance(cache, dict) and all(isinstance(cache.get(k), str) and cache[k].strip()
                                               for k in ('path', 'key', 'invalidation', 'proof')),
                'warm startup cache identity required')
        require(cache['path'] not in paths, 'duplicate warm startup cache')
        paths.add(cache['path'])
        require(type(cache.get('size_bytes')) is int and cache['size_bytes'] >= 0
                and re.fullmatch(r'[0-9a-f]{64}', cache.get('sha256', '')) is not None,
                'warm startup cache size/digest required')
        duration = cache.get('construction_seconds')
        require(type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
                'warm startup cache construction time required')
        require(cache['proof'] in receipt['evidence'], 'warm startup cache proof missing')


def protect(bundle, active):
    """Authenticate frozen inputs and every previously sealed slot at boundaries."""
    verify(bundle / 'frozen')
    frozen_sources(bundle)
    verify(bundle / 'progress')
    progress = read_json(bundle / 'progress' / 'checkpoint.json')
    retained_registration(bundle, progress)
    prior = {name: checksum for name, checksum in progress['slots'].items() if name != active.name}
    paths = {path.name: path for path in (bundle / 'slots').iterdir() if path != active}
    require(paths.keys() == prior.keys(), 'prior slots differ from progress checkpoint')
    for name, path in paths.items():
        verify(path)
        require(digest(path / 'SHA256SUMS') == prior[name], 'prior slot seal differs from progress checkpoint: ' + name)


def unchanged(directory, before):
    for name, record in before.items():
        path = child(directory, name)
        metadata = os.stat(path, follow_symlinks=False)
        require((metadata.st_dev, metadata.st_ino) == record['identity']
                and digest(path) == record['sha256'], 'hook changed original: ' + name)


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
    # Registration owns a checkpoint before slot activation owns recovery.
    # Deliver signals only after its retained copy and checksum agree.
    with defer_interrupts():
        retained = bundle / 'registration.json'
        if retained.exists(): require(canonical(read_json(retained)) == canonical(value), 'registration changed')
        else: write_json(retained, value)
        if retained_value is None:
            progress['registration'] = value
            write_json(bundle / 'progress' / 'checkpoint.json', progress)
            seal(bundle / 'progress')


def collect(bundle, registration=None, max_slots=None):
    def interrupted(signum, frame):
        raise KeyboardInterrupt('collection interrupted by signal %d' % signum)
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return collect_locked(bundle, registration, max_slots)
    finally:
        signal.signal(signal.SIGTERM, previous)


def collect_locked(bundle, registration, max_slots):
    bundle = Path(bundle).resolve()
    with campaign_lock(bundle) as check_lock:
        verify(bundle / 'frozen')
        m = read_json(bundle / 'frozen' / 'manifest.json')
        slots = read_json(bundle / 'frozen' / 'order.json')
        require(slots == order(), 'order differs')
        progress = retained_progress(bundle, m, slots)
        require(progress['stopped'] is None, 'campaign stopped; no replacement attempts')
        frozen_sources(bundle)
        frozen_seal = digest(bundle / 'frozen' / 'SHA256SUMS')
        require(max_slots is None or (m['phase'] == 'feasibility' and max_slots > 0), 'positive slot limit is feasibility-only')
        registered(bundle, m, registration)
        live_pins(m)
        consumed = progress['slots']
        done = 0
        for slot in slots:
            check_lock()
            directory = bundle / 'slots' / slot['id']
            if directory.exists(): continue
            if max_slots is not None and done >= max_slots: break
            live_pins(m)
            with ExitStack() as ownership, ExitStack() as activation:
                activation.enter_context(defer_interrupts())
                save_progress(bundle, consumed, active=slot['id'])
                check_directory, retained_directory = ownership.enter_context(
                    created_child(int(check_lock.directories['slots'].name), slot['id'],
                                  lambda: check_lock(marker=False)))
                context = dict(slot, trial_dir=str(directory), campaign_dir=str(bundle), redact_env=[],
                               execution_pins=m['pins'], execution_runtime=m['execution_runtime'],
                               execution_directories=execution_directories(m))
                metadata = retained_directory.stat()
                context['directory_identity'] = (metadata.st_dev, metadata.st_ino)
                state = {'slot': slot, 'started_at': utc(), 'cleanup_proven': True}
                check_lock()
                check_directory()
                write_json(retained_directory / 'slot.json', state)
                stop_reason = None
                allocation_started = False
                unsupported = m.get('unsupported', {}).get(slot['workload'] + '/' + slot['arm'])
                if unsupported: state['unsupported'] = unsupported
                def check_boundary():
                    check_lock()
                    check_directory()
                    protect(bundle, directory)
                    require(digest(bundle / 'frozen' / 'SHA256SUMS') == frozen_seal,
                            'frozen seal changed during collection')
                    require(json_equal(read_json(bundle / 'progress' / 'checkpoint.json')['slots'], consumed),
                            'progress slot mapping changed during collection')
                try:
                    # Pending activation signals are delivered only after recovery
                    # owns the slot. No allocation has been touched at this point.
                    activation.close()
                    if not unsupported:
                        allocation_started = True
                        state['cleanup_proven'] = False
                        check_boundary()
                        if m['phase'] == 'measurement':
                            before_prepare = hook_snapshot(directory)
                            prep = execute(m['hooks']['prepare'], context, directory, 'prepare', [])
                            require(succeeded(prep) and prep.get('containment_proven') is True, 'restore failed')
                            receipt = proof(directory, 'preparation.json', {'host_id': m['environment']['host_id'],
                                            'disk_image': m['environment']['vm_image'], 'fresh_memory': True,
                                            'clean_data': True, 'vcpus': 4, 'memory_bytes': 16 * 1024**3},
                                            previous=before_prepare)
                            unchanged(directory, before_prepare)
                            state['preparation'] = receipt
                            check_boundary()
                            if slot['cache'] == 'warm':
                                live_pins(m)
                                warm = directory / 'warmup'
                                _, retained_warm = ownership.enter_context(
                                    created_child(int(retained_directory.name), 'warmup', check_directory))
                                metadata = retained_warm.stat()
                                warm_context = dict(context, trial_dir=str(warm), arm='T-R',
                                                    directory_identity=(metadata.st_dev, metadata.st_ino))
                                cmd = execute(m['commands'][slot['workload']][slot['variant']]['T-R'], warm_context, warm, 'run', [])
                                from analyze import product
                                observed = product(warm, dict(slot, arm='T-R'), cmd, m['inputs'][slot['workload']][slot['variant']])
                                require(cmd['exit'] in (0, 2) and succeeded(dict(cmd, exit=0))
                                        and cmd.get('containment_proven') is True and observed['cleanup_proven']
                                        and not observed['quality_reasons'] and not observed['operational_reasons'], 'warm-up failed')
                                state['warmup'] = {'seconds': cmd['seconds'], 'exit': cmd['exit']}
                                before = hook_snapshot(directory)
                                live_pins(m)
                                cleanup = execute(m['hooks']['warm_cleanup'], context, directory, 'warm-cleanup', [])
                                require(succeeded(cleanup) and cleanup.get('containment_proven') is True,
                                        'warm-up output/data cleanup failed')
                                receipt = proof(directory, 'warm-cleanup.json', {'outputs_removed': True, 'data_removed': True,
                                                'cache_preserved': True}, previous=before)
                                warm_cache(receipt, directory)
                                state['warmup']['startup_caches'] = receipt['startup_caches']
                                unchanged(directory, before)
                            check_boundary()
                        live_pins(m)
                        original = hook_snapshot(directory)
                        spec = m['commands'][slot['workload']][slot['variant']][slot['arm']]
                        with Monitor(directory, identity=context['directory_identity']):
                            cmd = execute(spec, context, directory, 'run', [])
                        require(cmd.get('containment_proven') is True, 'measured process containment unproven')
                        # Baseline evaluations are persisted per execution. Product artifacts
                        # arrive at CLI completion; endpoint validation is separately timed.
                        from analyze import baseline, product
                        expected = m['inputs'][slot['workload']][slot['variant']]
                        try:
                            observed = (baseline if slot['arm'].startswith('B') else product)(directory, slot, cmd, expected)
                            received = (time.monotonic_ns() - cmd['collector_start_ns']) / 1e9
                            require(received >= cmd['seconds'], 'evidence receipt precedes command completion')
                            if not observed.get('evidence_absence'):
                                state['evidence_received_seconds'] = [received, received]
                            else:
                                state['evidence_absence'] = observed['evidence_absence']
                            state['cleanup_proven'] = observed['cleanup_proven']
                        except EVIDENCE_ERRORS as error:
                            state['error'] = 'evidence: ' + str(error)
                        unchanged(directory, original)
                        check_boundary()
                        if m['phase'] == 'measurement':
                            live_pins(m)
                            observed_files = hook_snapshot(directory)
                            observer = execute(m['hooks']['observe'], context, directory, 'observe', [])
                            require(succeeded(observer) and observer.get('containment_proven') is True, 'observation failed')
                            receipt = proof(directory, 'observation.json', {'host_id': m['environment']['host_id']}, previous=observed_files)
                            state['resources'] = resource_observations(receipt.get('resources'))
                            unchanged(directory, observed_files)
                            check_boundary()
                except (Exception, KeyboardInterrupt) as error:
                    state['error'] = str(error)
                    if isinstance(error, KeyboardInterrupt):
                        stop_reason = 'collector interrupted'
                        state['interrupted'] = True
                finally:
                    def checkpoint():
                        nonlocal stop_reason
                        # Never follow a replaced active path during recovery.
                        # Keep the pre-existing active checkpoint for fail-closed resume.
                        check_lock(marker=False)
                        check_directory()
                        if state['cleanup_proven'] is not True and stop_reason is None:
                            stop_reason = 'allocation cleanup unproven'
                        # A host/fixture cleanup proof cannot establish that the
                        # collector's own command groups have stopped writing.
                        # Include warm-up and every infrastructure command, even
                        # when an exception interrupted its normal return path.
                        for name in ('prepare', 'warmup/run', 'warm-cleanup', 'run', 'observe', 'cleanup'):
                            path = directory / (name + '.command.json')
                            if not path.exists(): continue
                            try:
                                command = command_receipt(read_json(path))
                                require(command.get('started') is not True
                                        or command.get('containment_proven') is True,
                                        'owned process group may remain alive')
                            except EVIDENCE_ERRORS as error:
                                stop_reason = 'process containment unproven: ' + name + ': ' + str(error)
                                errors = state.setdefault('containment_errors', [])
                                if stop_reason not in errors: errors.append(stop_reason)
                        for check in (lambda: live_pins(m), check_boundary):
                            try: check()
                            except (ValueError, OSError) as error:
                                stop_reason = str(error)
                                errors = state.setdefault('integrity_errors', [])
                                if stop_reason not in errors: errors.append(stop_reason)
                        state['ended_at'] = utc()
                        stopped = {'reason': stop_reason, 'slot': slot['id'], 'at': utc()} if stop_reason else None
                        state['stop'] = stopped
                        check_directory()
                        write_json(retained_directory / 'slot.json', state)
                        check_directory()
                        seal(retained_directory)
                        consumed[slot['id']] = digest(retained_directory / 'SHA256SUMS')
                        check_lock(marker=False)
                        save_progress(bundle, consumed, stopped=stopped)
                        if stopped: write_json(bundle / 'stopped.json', stopped)
                    try:
                        # Deliver cleanup/checkpoint signals only after both seals
                        # exist. A delivered interrupt then seals the stop without
                        # running cleanup again or admitting another slot.
                        with defer_interrupts():
                            if m['phase'] == 'measurement' and allocation_started:
                                try:
                                    check_lock(marker=False)
                                    check_directory()
                                    trusted = cleanup_dependencies(m)
                                    live_pins({'pins': {p: m['pins'][p] for p in trusted}})
                                    before_cleanup = hook_snapshot(directory)
                                    cleanup_context = dict(context, execution_pins={p: m['pins'][p] for p in trusted},
                                                           execution_directories=execution_directories(
                                                               {'hooks': {'cleanup': m['hooks']['cleanup']}}))
                                    cleanup = execute(m['hooks']['cleanup'], cleanup_context, directory, 'cleanup', [])
                                    require(succeeded(cleanup) and cleanup.get('containment_proven') is True,
                                            'cleanup command failed')
                                    proof(directory, 'cleanup.json', {'proven': True}, previous=before_cleanup)
                                    unchanged(directory, before_cleanup)
                                    state['cleanup_proven'] = True
                                except (Exception, KeyboardInterrupt) as error:
                                    state['cleanup_proven'] = False
                                    state['cleanup_error'] = str(error)
                            checkpoint()
                    except KeyboardInterrupt:
                        stop_reason = 'collector interrupted'
                        state['interrupted'] = True
                        with defer_interrupts():
                            checkpoint()
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
