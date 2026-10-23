"""Load runner dependencies from this directory, rejecting ambient cached code.

Entrypoints execute this file by absolute path with runpy, so a cached module
cannot replace the location guard itself. This authenticates module origins;
it is not a sandbox against arbitrary Python startup code.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_MODULES = ('support', 'analyze', 'stream', 'fixture_contract', 'tool',
                  'prepare', 'smoke', 'record_fixture_contract', 'imports', 'snapshot')
RUNNER_SOURCES = tuple(name + '.py' for name in RUNNER_MODULES) + ('test_runner.py',)
SHARED_MODULES = ('common', 'plan', 'redaction', 'execution')


def check_source_inventory(root):
    """Reject executable caches and other unregistered entries before imports."""
    for directory, names, directories in ((root, set(RUNNER_SOURCES), {'shared'}),
                                          (root/'shared', {n+'.py' for n in SHARED_MODULES}, set())):
        if (directory.is_symlink() or not directory.is_dir()
                or {p.name for p in directory.iterdir()} != names | directories
                or any(p.is_symlink() or (not p.is_file() if p.name in names else not p.is_dir())
                       for p in directory.iterdir())):
            raise ValueError('frozen source inventory differs: ' + str(directory))


class SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # -B suppresses cache writes, not reads. Compile the source directly,
        # including when PYTHONPYCACHEPREFIX points outside the frozen tree.
        return self.source_to_code(self.get_data(self.path), self.path)


def load_source(name, directory, kind='runner'):
    path = directory/(name+'.py')
    if name in sys.modules:
        module = sys.modules[name]
        if module is None or Path(getattr(module, '__file__', '') or '').resolve() != path:
            raise ValueError(kind + ' module location differs: ' + name)
        return module
    loader = SourceOnlyLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def check(name, module):
    if module is None or Path(getattr(module, '__file__', '') or '').resolve() != HERE / (name + '.py'):
        raise ValueError('runner module location differs: ' + name)


if HERE.name == 'source' and HERE.parent.name == 'frozen':
    check_source_inventory(HERE)

for name in RUNNER_MODULES:
    if name in sys.modules:
        check(name, sys.modules[name])

sys.path.insert(0, str(HERE))
try:
    for name in dependencies:
        check(name, load_source(name, HERE))
finally:
    sys.path.pop(0)
