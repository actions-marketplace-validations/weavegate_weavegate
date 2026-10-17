"""Freeze plans, execute a one-shot block, and commit immutable evidence boundaries."""
import json
import os
import signal
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from common import (campaign_lock, canonical, child, digest, order, read_json, seal, sha,
                    timestamp, utc, verify, write_bytes, write_csv, write_json)
from evidence import EVIDENCE_ERRORS, cleanup_inventory, cleanup_valid, observation, trial
from execution import ReceiptIntegrityError, defer_interrupts, execute, expand_command, succeeded
from plan import STEPS, bind_executables, validate_manifest, validate_registration, verify_execution_pins
from redaction import (redact_metadata, sanitize, validate_public_evidence,
                       validate_redaction_secrets)
from storage import BundleGuard


def freeze(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    m = read_json(source)
    validate_manifest(m)
    bind_executables(m)
    validate_manifest(m)
    # Validate every prerequisite before creating a campaign.
    verify_execution_pins(m)
    contents = {}
    for pin in m['pins']:
        path = child(source.parent, pin['path'])
        data = path.read_bytes()
        if sha(data) != pin['sha256'] or not pin.get('role'):
            raise ValueError('artifact pin mismatch: ' + pin['path'])
        name = 'pins/' + pin['path']
        if name in contents:
            raise ValueError('duplicate pin: ' + pin['path'])
        contents[name] = data
    for name, pin in m['protocols'].items():
        if '\0' in name:
            raise ValueError('invalid protocol filename')
        child(destination / 'protocol', name)  # Check output paths before creating any bundle.
        data = child(source.parent, pin['path']).read_bytes()
        if sha(data) != pin['sha256']:
            raise ValueError('protocol pin mismatch: ' + name)
        if m['experiment'] in ('E1', 'E2'):
            current = Path(__file__).resolve().parents[1] / name
            if data != current.read_bytes():
                raise ValueError('protocol differs from this collector revision: ' + name)
        contents['protocol/' + name] = data
    targets = {child(destination, name) for name in contents}
    if len(targets) != len(contents) or any(parent in targets for path in targets for parent in path.parents):
        raise ValueError('conflicting retained artifact paths')
    rows = order(m)
    campaign_bytes = canonical(m)
    destination.mkdir(mode=0o700)  # Never overwrite or extend a frozen campaign.
    for name, data in contents.items():
        path = child(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / 'collection.lock').touch(mode=0o600)
    write_json(destination / 'freeze-command.json', {'argv': [sys.executable, '-B',
               str(Path(__file__).with_name('tool.py')), 'freeze', str(source), str(destination.resolve())]})
    (destination / 'campaign.json').write_bytes(campaign_bytes)
    write_json(destination / 'order.json', rows)
    write_csv(destination / 'order.csv', [dict(r, arms=','.join(r['arms'])) for r in rows],
              ['block', 'block_id', 'workload', 'date_slot', 'date', 'arms'])
    # Archive the executable implementation; analysis requires no live checkout.
    for path in sorted(Path(__file__).parent.glob('*.py')):
        target = destination / 'analysis' / 'source' / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    write_json(destination / 'analysis' / 'dependencies.json',
               {'bundle_format': 5, 'runtime': 'Python >= 3.9', 'dependencies': 'standard library only'})
    inventory(destination, m)
    seal(destination)


def collect(root, block, registration):
    def interrupted(signum, frame):
        raise KeyboardInterrupt('collection interrupted by signal %d' % signum)
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with exclusive(root):
            return collect_locked(root, block, registration)
    finally:
        signal.signal(signal.SIGTERM, previous)


def collect_locked(root, block, registration):
    root = Path(root).resolve()
    verify(root)
    if (root / 'stopped.json').exists():
        raise ValueError('campaign stopped; register a new campaign')
    m = read_json(root / 'campaign.json')
    validate_manifest(m)
    verify_execution_pins(m)
    for source in sorted(Path(__file__).parent.glob('*.py')):
        if digest(source) != digest(root / 'analysis/source' / source.name):
            raise ValueError('collector source changed since campaign freeze')
    if read_json(root / 'order.json') != order(m):
        raise ValueError('registered order differs')
    reg = read_json(registration)
    validate_registration(reg)
    if (reg['campaign_sha256'] != digest(root / 'campaign.json') or
            reg['order_sha256'] != digest(root / 'order.json') or
            timestamp(reg['registered_at']) >= timestamp(utc())):
        raise ValueError('invalid public registration receipt')
    receipt_path = root / 'registration.json'
    if receipt_path.exists() and read_json(receipt_path) != reg:
        raise ValueError('registration receipt cannot change')
    slots = [r for r in order(m) if r['block'] == block]
    if len(slots) != 1:
        raise ValueError('unknown block')
    slot = slots[0]
    prior = [r for r in order(m) if r['workload'] == slot['workload']
             and r['date_slot'] == slot['date_slot']]
    for earlier in prior[:prior.index(slot)]:
        if any(not (root / 'blocks' / earlier['block'] / arm / 'trial.json').exists()
               for arm in earlier['arms']):
            raise ValueError('earlier assigned block is missing/incomplete')
    if slot['date'] != datetime.now(timezone.utc).strftime('%Y-%m-%d'):
        raise ValueError('block must execute on its registered UTC date')
    if timestamp(reg['registered_at']).date().isoformat() > slot['date']:
        raise ValueError('registration postdates block')
    # Public audit identifiers; credential values are read separately below.
    redaction_env_names = m['redact_env']
    if any(not os.environ.get(name) for name in redaction_env_names):
        raise ValueError('missing required redaction environment variable')
    secrets = [os.environ[name].encode() for name in redaction_env_names]
    validate_redaction_secrets(secrets)
    pair_dir = root / 'blocks' / block
    collection_command = {'argv': [sys.executable, '-B', str(Path(__file__).with_name('tool.py')),
                                  'collect', str(root), '--block', block,
                                  '--registration', str(receipt_path)]}
    invocations = []
    for arm in slot['arms']:
        context = {'campaign_dir': root, 'trial_dir': pair_dir / arm,
                   'block_id': block, 'arm': arm, 'redact_env': redaction_env_names}
        for spec in m['commands'][slot['workload']][arm].values():
            invocations.append(expand_command(spec, context))
    validate_public_evidence(root, secrets, [reg, collection_command, invocations])
    session = None
    safe = True
    interrupted = False
    try:
        with defer_interrupts():
            pair_dir.parent.mkdir(exist_ok=True)
            pair_dir.mkdir(mode=0o700)  # Existing or interrupted blocks cannot be rerun.
            write_json(receipt_path, reg)
            write_json(pair_dir / 'collection-command.json', collection_command)
            write_json(pair_dir / 'slot.json', slot)
            seal(root)  # A crash consumes the slot, never creates a replacement attempt.
            session = Collection(root, m, secrets)
        for arm in slot['arms']:
            directory = pair_dir / arm
            session.guard.mkdir(directory)
            session.guard.active = directory
            state = {'collection_root': str(root), 'state': 'entered', 'reason': '',
                     'cleanup_inputs_valid': False}
            session.save(directory / 'attempt.json', state)
            if not safe or not current_date(slot):
                state.update(state='finished', reason='cleanup_unproven' if not safe
                             else 'registered_date_expired')
                session.finish(directory, state, slot, arm)
                continue
            # Until cleanup has been validated, any interruption quarantines use.
            safe = False
            try:
                safe = session.arm(directory, state, slot, arm)
            finally:
                # Even BaseException paths have a durable, reduced arm receipt.
                safe = state['cleanup_inputs_valid']
    except BaseException:
        interrupted = True
        raise
    finally:
        if session is not None:
            finalize(session, pair_dir, block, safe, interrupted)
    return safe


def finalize(session, pair_dir, block, safe, interrupted):
    root, m = session.root, session.manifest
    try:
        with defer_interrupts():
            if not safe or interrupted:
                session.save(root / 'stopped.json', {'reason': 'cleanup_unproven' if not safe
                             else 'interrupted_collection', 'block': block})
            if session.guard.error is not None:
                write_json(pair_dir / 'integrity-error.json',
                           {'reason': redact_metadata(session.guard.error, session.secrets, '')})
                raise ValueError('protected campaign evidence changed; refusing to reseal')
            session.guard.check()
            inventory(root, m)
            seal(root)
    except KeyboardInterrupt:
        # Delivery occurs after the aggregate checkpoint. Record a signal
        # received even during seal(), before propagating it to the CLI.
        with defer_interrupts():
            if session.guard.error is not None:
                raise ValueError('protected campaign evidence changed; refusing to reseal')
            write_json(root / 'stopped.json', {'reason': 'cleanup_unproven' if not safe
                       else 'interrupted_collection', 'block': block})
            inventory(root, m)
            seal(root)
        raise


def current_date(slot):
    return slot['date'] == datetime.now(timezone.utc).strftime('%Y-%m-%d')


class Collection:
    """Linear step lifecycle. The evidence reducer owns outcome classification."""
    def __init__(self, root, manifest, secrets):
        self.root, self.manifest, self.secrets = root, manifest, secrets
        self.guard = BundleGuard(root, secrets)
        self.redactions = {}
        self.contained = True

    def save(self, path, value):
        replacements = [0]
        value = redact_metadata(value, self.secrets, path.name, replacements)
        if path.parent == self.guard.active:
            self.redactions[path.parent] = self.redactions.get(path.parent, 0) + replacements[0]
        if self.guard.error is None:
            self.guard.own(path, value)
        else:
            # Preserve failure evidence, but never make a damaged bundle valid.
            write_json(path, value)

    def sanitize(self, directory):
        try:
            removed = sanitize(directory, self.secrets, self.guard.owned_paths(directory))
        except (OSError, ValueError) as error:
            # Cleanup may remove the offending payload, but cannot turn failed
            # sanitization into a publishable evidence checkpoint.
            self.guard.error = self.guard.error or str(error)
            raise
        self.redactions[directory] = self.redactions.get(directory, 0) + removed

    def command(self, directory, context, step, spec, teardown=False):
        if not teardown:
            self.guard.check()
            verify_execution_pins(self.manifest)
        try:
            return execute(spec, context, directory, step, self.secrets)
        except ReceiptIntegrityError as error:
            self.guard.error = str(error)
            raise
        finally:
            # First detect rewrites, then sanitize only new payloads, then seal
            # this boundary. Cleanup still executes after an integrity violation.
            try:
                if self.guard.error is None:
                    paths = [directory / (step + suffix) for suffix in
                             ('.command.json', '.stdout', '.stderr')]
                    self.guard.check(writing=paths)
            finally:
                self.sanitize(directory)
            if self.guard.error is None:
                self.guard.command_complete(directory, step)
                path = directory / (step + '.command.json')
                if path.exists():
                    receipt = read_json(path)
                    if receipt.get('containment_proven') is not True:
                        self.contained = False
                    self.redactions[directory] += receipt.get('redactions', 0)
                if not teardown:
                    self.save(directory / 'redactions.json',
                              {'replacements': self.redactions[directory]})
                # Cleanup keeps pre-teardown controls live through proof
                # validation; finish() persists the final replacement count.
                self.guard.checkpoint()

    def finish(self, directory, state, slot, arm):
        self.sanitize(directory)
        state['state'] = 'finished'
        self.save(directory / 'attempt.json', state)
        self.save(directory / 'trial.json', trial(self.root, self.manifest, slot, arm))
        self.save(directory / 'redactions.json', {'replacements': self.redactions.get(directory, 0)})
        if self.guard.error is None:
            self.guard.checkpoint()

    def arm(self, directory, state, slot, arm):
        commands = self.manifest['commands'][slot['workload']][arm]
        context = {'campaign_dir': self.root, 'trial_dir': directory,
                   'block_id': slot['block'], 'arm': arm, 'redact_env': self.manifest['redact_env']}
        try:
            prepare = self.command(directory, context, 'prepare', commands['prepare'])
            if succeeded(prepare):
                if not current_date(slot):
                    state['reason'] = 'registered_date_expired'
                else:
                    run = self.command(directory, context, 'run', commands['run'])
                    self.command(directory, context, 'observe', commands['observe'])
                    try:
                        observation(self.manifest, dict(slot, arm=arm), directory, run['seconds'])
                    except EVIDENCE_ERRORS as error:
                        state['observation_error'] = str(error)
                        raise
                    self.command(directory, context, 'check', commands['check'])
        except EVIDENCE_ERRORS as error:
            state['reason'] = str(error)
        except BaseException:
            state['reason'] = 'interrupted_collection'
            raise
        finally:
            with defer_interrupts():
                self.cleanup(directory, state, slot, arm, commands, context)
        return state['cleanup_inputs_valid']

    def cleanup(self, directory, state, slot, arm, commands, context):
        # Teardown has its own frozen dependency set and runs regardless of
        # outcome. A valid proof plus all live pins is required for reuse.
        try:
            pins_match = True
            try:
                verify_execution_pins(self.manifest)
            except (OSError, ValueError):
                pins_match = False
            verify_execution_pins(self.manifest, commands['cleanup'].get('execution_pins'))
            try:
                if self.guard.error is None:
                    self.save(directory / 'cleanup-inputs.json', cleanup_inventory(directory))
            finally:
                self.command(directory, context, 'cleanup', commands['cleanup'], teardown=True)
            cleanup_valid(directory)
            state['cleanup_inputs_valid'] = pins_match and self.contained and self.guard.error is None
        except EVIDENCE_ERRORS:
            state['cleanup_inputs_valid'] = False
        finally:
            self.finish(directory, state, slot, arm)


def inventory(root, m):
    """Include every planned arm, including blocks never collected."""
    rows, commands, events, environments = [], [], [], []
    for slot in order(m):
        for arm in slot['arms']:
            directory = root / 'blocks' / slot['block'] / arm
            row = trial(root, m, slot, arm)
            rows.append({key: row[key] for key in ('block', 'workload', 'date_slot', 'arm',
                         'attempt', 'started', 'classification', 'reason', 'exit', 'elapsed_seconds')})
            for step in STEPS:
                path = directory / (step + '.command.json')
                if path.exists():
                    command = dict(read_json(path), block=slot['block'], arm=arm)
                    commands.append(command)
                    events.append({key: command.get(key) for key in
                                   ('block', 'arm', 'step', 'seconds', 'started_at', 'ended_at')})
            path = directory / 'receipt.json'
            if path.exists():
                try:
                    receipt = read_json(path)
                    if isinstance(receipt, dict):
                        environments.append({'block': slot['block'], 'arm': arm,
                                             'observed': receipt.get('environment')})
                except ValueError:
                    pass
    write_csv(root / 'trials.csv', rows, ['block', 'workload', 'date_slot', 'arm', 'attempt',
                                        'started', 'classification', 'reason', 'exit', 'elapsed_seconds'])
    for name, items in [('commands', commands), ('events', events), ('environments', environments)]:
        write_bytes(root / (name + '.jsonl'), ''.join(
            json.dumps(item, sort_keys=True, allow_nan=False) + '\n' for item in items).encode())


@contextmanager
def exclusive(root):
    with campaign_lock(root):
        yield
