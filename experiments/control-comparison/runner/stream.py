"""Observe durable baseline evaluations at 100 ms; never coordinate workers."""
import threading
import time
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support',)})
from support import digest, pinned_directory, read_json, write_json


class Monitor:
    def __init__(self, directory, identity=None):
        self.directory = directory
        self.identity = identity
        self.receipts = {}
        self.error = None
        self.stop = threading.Event()

    def scan(self):
        for path in sorted((self.directory / 'raw').glob('execution-*.json')):
            name = str(path.relative_to(self.directory))
            if name in self.receipts: continue
            # Atomic rename in the driver makes partial JSON unavailable.
            value = read_json(path)
            if value.get('evaluation') is not None:
                checksum = digest(path)
                self.receipts[name] = {'received_ns': time.monotonic_ns() - self.start,
                                       'sha256': checksum}

    def watch(self):
        try:
            while not self.stop.wait(.1): self.scan()
        except Exception as error:
            self.error = str(error)

    def __enter__(self):
        self.ownership = pinned_directory(self.directory, self.identity)
        self.check_directory, self.directory = self.ownership.__enter__()
        self.start = time.monotonic_ns()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.stop.set(); self.thread.join()
        try:
            self.check_directory()
            try: self.scan()
            except Exception as error: self.error = str(error)
            self.check_directory()
            write_json(self.directory / 'stream.json', {'clock': 'collector-monotonic',
                'start_monotonic_ns': self.start,
                'envelope_ns': time.monotonic_ns() - self.start, 'cadence_ms': 100,
                'limitation': 'receipt may lag atomic publication by polling and validation time',
                'receipts': self.receipts, 'error': self.error})
        finally:
            self.ownership.__exit__(*unused)
