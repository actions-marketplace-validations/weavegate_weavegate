"""Append-only hook boundaries and explicit collector file ownership.

This detects accidental or observable hook writes, not hostile processes running
as the collector's user. A violation is sticky even if a later hook restores bytes.
"""
import os
from pathlib import Path

from common import bundle_inventory, child, digest, write_json
from execution import defer_interrupts
from plan import STEPS
from redaction import validate_paths

CONTROL = {'attempt.json', 'trial.json', 'redactions.json', 'cleanup-inputs.json'}
COMMAND_FILES = {step + suffix for step in STEPS
                 for suffix in ('.command.json', '.stdout', '.stderr')}


class BundleGuard:
    def __init__(self, root, secrets=()):
        self.root = Path(root)
        self.secrets = secrets
        self.lock_stat = os.stat(self.root / 'collection.lock', follow_symlinks=False)
        self.hashes = self.snapshot()
        self.active = None
        self.error = None

    def snapshot(self):
        return bundle_inventory(self.root)

    def check(self, writing=()):
        if self.error is not None:
            raise ValueError(self.error)
        try:
            if not os.path.samestat(self.lock_stat, os.stat(self.root / 'collection.lock', follow_symlinks=False)):
                raise ValueError('protected campaign lock replaced during collection')
            if self.active is not None:
                validate_paths(self.active, self.secrets, self.owned_paths(self.active))
            writing = {str(Path(p).relative_to(self.root)) for p in writing}
            actual = self.snapshot()
            for name, checksum in self.hashes.items():
                if name not in writing and (name not in actual or actual[name] != checksum):
                    raise ValueError('protected evidence changed during collection: ' + name)
            for name in actual.keys() - self.hashes.keys() - writing:
                path = child(self.root, name)
                if self.active is None or self.active not in path.parents:
                    raise ValueError('unregistered evidence added during collection: ' + name)
                if path.parent == self.active and path.name in CONTROL | COMMAND_FILES:
                    raise ValueError('hook wrote collector-owned file: ' + name)
        except (OSError, ValueError) as error:
            self.error = str(error)
            raise

    def mkdir(self, path):
        """Admit only the collector's next assigned arm directory."""
        self.check()
        with defer_interrupts():
            path.mkdir(mode=0o700)
            self.hashes[str(path.relative_to(self.root))] = None

    def own(self, path, value):
        """Only collector writes may update an already retained control file."""
        path = Path(path)
        self.check()
        with defer_interrupts():
            write_json(path, value)
            self.hashes[str(path.relative_to(self.root))] = digest(path)

    def checkpoint(self):
        self.check()
        self.hashes = self.snapshot()

    def command_complete(self, directory, step):
        paths = [directory / (step + suffix)
                 for suffix in ('.command.json', '.stdout', '.stderr')]
        self.check(writing=paths)
        # A step owns only its own receipts, once. Earlier commands and originals
        # are immutable; reserved future receipts must still be absent.
        with defer_interrupts():
            for path in paths:
                name = str(path.relative_to(self.root))
                if name in self.hashes:
                    self.error = 'command executed twice: ' + name
                    raise ValueError(self.error)
                if path.exists():
                    self.hashes[name] = digest(path)

    def owned_paths(self, directory):
        return {directory / name for name in CONTROL | COMMAND_FILES}
