"""Sanitize payloads; only explicitly owned paths get metadata exemptions."""
import json
from pathlib import Path

from common import canonical, entries, files, read_json, write_bytes
from plan import STEPS

REDACTION_MARKER = b'[REDACTED]'


def validate_redaction_secrets(secrets):
    if any(not secret or secret in REDACTION_MARKER for secret in secrets):
        raise ValueError('secret must be nonempty and absent from the redaction marker')


def secret_variants(secrets):
    variants = set(secrets)
    for value in secrets:
        variants.add(json.dumps(value.decode(), ensure_ascii=True)[1:-1].encode())
        variants.add(json.dumps(value.decode(), ensure_ascii=False)[1:-1].encode())
    return sorted(variants, key=len, reverse=True)


def validate_public_evidence(root, secrets, metadata):
    """Reject leaks before admission without rewriting registered public bytes."""
    if not secrets:
        return
    variants = secret_variants(secrets)
    def check(data):
        if any(value in data for value in variants):
            # Neither the credential nor its containing path belongs in errors.
            raise ValueError('secret in public campaign evidence or metadata')
    check(canonical(metadata))
    for path in entries(root):
        check(str(path.relative_to(root)).encode())
        if path.is_file():
            check(path.read_bytes())
            if path.suffix == '.json':
                try:
                    value = read_json(path)
                except (ValueError, UnicodeError):
                    continue
                check(canonical(value))  # Catch alternate JSON string escapes.


def sanitize(root, secrets, owned=()):
    """Sanitize retained bytes before computing evidence hashes."""
    validate_redaction_secrets(secrets)
    validate_paths(root, secrets, owned)
    variants = secret_variants(secrets)
    removed = 0
    for path in files(root):
        data = path.read_bytes()
        if path.suffix == '.json':
            try:
                value = read_json(path)
            except (ValueError, UnicodeError):
                pass  # Non-JSON originals still receive byte-level redaction.
            else:
                replacements = [0]
                redacted = redact_metadata(value, secrets, path.name if path in owned else '', replacements)
                serialized = canonical(redacted)
                retained = serialized if replacements[0] else data
                # Strings can be redacted without changing their JSON type;
                # numbers/booleans/null and protected controls cannot. Check
                # both decoded and retained spellings without rewriting them.
                if any(value in serialized or value in retained for value in variants):
                    raise ValueError('secret remains in JSON after string redaction')
                if replacements[0]:
                    write_bytes(path, serialized)
                removed += replacements[0]
                continue
        original = data
        for value in variants:
            removed += data.count(value)
            data = data.replace(value, REDACTION_MARKER)
        if data != original:
            write_bytes(path, data)
    return removed


def validate_paths(root, secrets, owned=()):
    """Reject payload path leaks without renaming immutable evidence references."""
    for path in entries(root):
        if path not in owned:
            name = str(path.relative_to(root)).encode()
            if any(secret in name for secret in secrets):
                # Never include the leaking filename in a public diagnostic.
                raise ValueError('secret-bearing relative path in evidence bundle')


def redact_metadata(value, secrets, filename, replacements=None):
    """Keep numeric types and collector-owned public identity/control fields."""
    if not isinstance(value, dict):
        return redact_value(value, secrets, replacements)
    if filename in ('slot.json', 'collection-command.json', 'completed-arm-hashes.json',
                    'cleanup-inputs.json'):
        return value  # Protected collector-owned public audit identifiers.
    if filename == 'attempt.json':
        public = {'collection_root', 'cleanup_inputs_valid', 'state'}
        known = public | {'reason'}
    elif filename == 'trial.json':
        public = {'block', 'block_id', 'workload', 'date_slot', 'date', 'arms', 'arm',
                  'attempt', 'classification', 'collection_root', 'metrics', 'started',
                  'exit', 'elapsed_seconds', 'cleanup_proven'}
        known = public | {'reason', 'facts'}
    elif filename in {step + '.command.json' for step in STEPS}:
        public = {'step', 'argv', 'cwd', 'env', 'redacted_env_names', 'started_at',
                  'ended_at', 'started', 'exit', 'timeout', 'seconds', 'interrupted', 'finished',
                  'containment_proven', 'redactions'}
        known = public | {'error'}
    elif filename == 'redactions.json':
        public = known = {'replacements'}
    else:
        return redact_value(value, secrets, replacements)
    return {key if key in known else redact_value(key, secrets, replacements):
            item if key in public else redact_value(item, secrets, replacements)
            for key, item in value.items()}


def redact_value(value, secrets, replacements=None):
    validate_redaction_secrets(secrets)
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if replacements is not None:
                replacements[0] += value.count(secret.decode())
            value = value.replace(secret.decode(), REDACTION_MARKER.decode())
        return value
    if isinstance(value, list):
        return [redact_value(item, secrets, replacements) for item in value]
    if isinstance(value, dict):
        return {redact_value(key, secrets, replacements): redact_value(item, secrets, replacements)
                for key, item in value.items()}
    return value
