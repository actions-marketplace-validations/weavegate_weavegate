"""Append-only hook boundaries and explicit collector file ownership.

This detects accidental or observable hook writes, not hostile processes running
as the collector's user. A violation is sticky even if a later hook restores bytes.
"""
from pathlib import Path

from common import child, digest, files, write_json
from plan import STEPS

CONTROL = {'attempt.json', 'trial.json', 'redactions.json'}
COMMAND_FILES = {step + suffix for step in STEPS
                 for suffix in ('.command.json', '.stdout', '.stderr')}


class BundleGuard:
    def __init__(self, root):
        self.root = Path(root)
        self.hashes = self.snapshot()
        self.active = None
        self.error = None

    def snapshot(self):
        return {str(p.relative_to(self.root)): digest(p) for p in files(self.root)}

    def check(self, writing=()):
        if self.error is not None:
            raise ValueError(self.error)
        try:
            writing = {str(Path(p).relative_to(self.root)) for p in writing}
            actual = self.snapshot()
            for name, checksum in self.hashes.items():
                if name not in writing and actual.get(name) != checksum:
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

    def own(self, path, value):
        """Only collector writes may update an already retained control file."""
        path = Path(path)
        self.check()
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
        for path in paths:
            name = str(path.relative_to(self.root))
            if name in self.hashes:
                self.error = 'command executed twice: ' + name
                raise ValueError(self.error)
            if path.exists():
                self.hashes[name] = digest(path)

    def owned_paths(self, directory):
        return {directory / name for name in CONTROL | COMMAND_FILES}
