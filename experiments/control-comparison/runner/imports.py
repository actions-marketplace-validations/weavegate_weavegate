"""Load runner dependencies from this directory, rejecting ambient cached code.

Entrypoints execute this file by absolute path with runpy, so a cached module
cannot replace the location guard itself. This authenticates module origins;
it is not a sandbox against arbitrary Python startup code.
"""
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_MODULES = ('support', 'analyze', 'stream', 'fixture_contract', 'tool',
                  'prepare', 'smoke', 'record_fixture_contract', 'imports')


def check(name, module):
    if module is None or Path(getattr(module, '__file__', '') or '').resolve() != HERE / (name + '.py'):
        raise ValueError('runner module location differs: ' + name)


for name in RUNNER_MODULES:
    if name in sys.modules:
        check(name, sys.modules[name])

sys.path.insert(0, str(HERE))
try:
    for name in dependencies:
        check(name, importlib.import_module(name))
finally:
    sys.path.pop(0)
