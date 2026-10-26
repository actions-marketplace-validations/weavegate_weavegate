"""Execute registered bytes in a private mount tree, retaining original paths.

Linux memfd seals prevent writes through every descriptor. Bubblewrap copies
those sealed bytes into read-only mounts; private ancestors prevent a host
rename from redirecting their paths. Unpinned OS/runtime inputs and writable
outputs remain host resources. This is input integrity, not a hostile-code sandbox.
"""
import fcntl
import json
import math
import os
import select
import shutil
import signal
import stat
import subprocess
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path


def runtime_identity():
    from support import digest, require
    path = shutil.which('bwrap')
    require(path is not None, 'bubblewrap (bwrap) is required for frozen execution')
    path = str(Path(path).resolve())
    return {'path': path, 'sha256': digest(path)}


@contextmanager
def frozen_launch(pins, runtime, directories=(), visible=(), timeout=30):
    from support import digest, require
    with ExitStack() as owned:
        descriptors = []

        def descriptor(fd):
            owned.callback(os.close, fd)
            descriptors.append(fd)
            return fd

        def sealed(name, source=None, checksum=None, data=None):
            fd = descriptor(os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING))
            with os.fdopen(os.dup(fd), 'wb') as output:
                if source is not None:
                    with open(source, 'rb') as original:
                        shutil.copyfileobj(original, output)
                else:
                    output.write(data)
            os.fchmod(fd, 0o555)
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW |
                        fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
            if checksum is not None:
                require(digest('/proc/self/fd/%d' % fd) == checksum, 'execution snapshot differs: ' + str(source))
            os.lseek(fd, 0, os.SEEK_SET)
            return fd

        launcher = sealed('e5-bwrap', runtime['path'], runtime['sha256'])
        tree = {}
        for name, checksum in sorted(pins.items()):
            path = Path(name)
            require(path.is_absolute() and '..' not in path.parts, 'absolute normalized execution pin required')
            require(path.parts[1] != '.e5-host', 'execution pin overlaps private host backing')
            node = tree
            for part in path.parts[1:-1]:
                node = node.setdefault(part, {})
                require(isinstance(node, dict), 'execution file/directory overlap')
            require(path.name not in node, 'execution file/directory overlap')
            node[path.name] = sealed('e5-input', name, checksum)
        # An explicit input directory is a closed inventory: added host files
        # must not appear on classpaths, Python paths or migration scans.
        closed = {str(Path(p)) for p in directories}
        for path in closed:
            node = tree
            for part in Path(path).parts[1:]:
                node = node.setdefault(part, {})
                require(isinstance(node, dict), 'execution input directory overlaps file')

        host = object()
        exposed = {str(Path(p)) for p in visible if p}
        for path in exposed:
            if path == '/': continue
            node = tree
            for part in Path(path).parts[1:-1]:
                value = node.setdefault(part, {})
                if value is host: break
                require(isinstance(value, dict), 'writable path overlaps execution pin')
                node = value
            else:
                node.setdefault(Path(path).name, host)

        args = ['--die-with-parent', '--new-session', '--tmpfs', '/', '--dir', '/.e5-host']

        def mount(path, node, confined=False, host_access=True):
            confined = confined or str(path) in closed
            # Do not enumerate other campaigns' temporary directories or home
            # trees. Their creation/removal cannot affect this invocation.
            host_access = (host_access and str(path) not in ('/tmp', '/home', '/root')) or str(path) in exposed
            parent = descriptor(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
            backing = '/.e5-host/%d' % parent
            args.extend(('--bind', '/proc/self/fd/%d' % parent, backing))
            names = set(node) if confined or not host_access else set(os.listdir(parent)) | set(node)
            for name in sorted(names):
                target = str(path / name)
                value = node.get(name)
                if isinstance(value, dict):
                    args.extend(('--dir', target))
                    mount(path / name, value, confined, host_access)
                elif isinstance(value, int):
                    args.extend(('--perms', '0555', '--ro-bind-data', str(value), target))
                else:
                    try:
                        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        require(value is not host, 'required execution path disappeared: ' + target)
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        args.extend(('--symlink', os.readlink(name, dir_fd=parent), target))
                    elif value is host or target in ('/dev', '/proc', '/sys', '/run'):
                        fd = descriptor(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent))
                        source = '/proc/self/fd/%d' % fd
                        args.extend(('--dev-bind' if target == '/dev' else '--bind', source, target))
                    else:
                        # Unpinned ambient siblings need no individual mount.
                        # Keep pinned paths on private directories while using
                        # the retained host directory for ordinary OS inputs.
                        args.extend(('--symlink', backing + '/' + name, target))

        mount(Path('/'), tree)
        # Private directory lists are fixed, but outputs under their unpinned
        # sibling mounts retain the host's normal writable behavior.
        arguments = sealed('e5-mount-args', data=b'\0'.join(os.fsencode(a) for a in args) + b'\0')

        def launch(argv, **kwargs):
            require(argv[0] in pins, 'execution executable is unpinned')
            reader, writer = os.pipe()
            process = None
            child = None
            try:
                process = subprocess.Popen(['/proc/self/fd/%d' % launcher, '--args', str(arguments),
                                            '--info-fd', str(writer), '--', *argv],
                                           pass_fds=(*descriptors, writer), **kwargs)
                os.close(writer); writer = None
                deadline = time.monotonic() + timeout
                poller = select.poll(); poller.register(reader, select.POLLIN | select.POLLHUP)
                data = b''
                while True:
                    ready = poller.poll(math.ceil(max(0, deadline-time.monotonic()) * 1000))
                    require(ready, 'execution namespace startup deadline exceeded')
                    chunk = os.read(reader, 4096)
                    if not chunk: break
                    data += chunk
                    require(len(data) <= 16384, 'execution namespace receipt exceeds limit')
                if not data:
                    # Setup failed before a child existed. Preserve its normal
                    # command receipt, stderr and original exit code.
                    return process
                child = json.loads(data)['child-pid']
                require(type(child) is int and child > 0, 'invalid execution namespace child')
                return MountedProcess(process, child)
            except BaseException as error:
                if process is not None:
                    from execution import LaunchContainmentError
                    try:
                        if child is not None:
                            try: os.killpg(child, signal.SIGKILL)
                            except ProcessLookupError: pass
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        process.wait(timeout=5)
                    finally:
                        # The executor must retain the actual spawn and stop
                        # reuse even when this callback cannot return an owner.
                        raise LaunchContainmentError('namespace ownership unproven: ' + str(error)) from error
                raise
            finally:
                os.close(reader)
                if writer is not None: os.close(writer)

        yield launch


class MountedProcess:
    """Wait for bubblewrap but signal the workload's separate session.

    Sending TERM to the supervisor would trigger die-with-parent and kill the
    workload before its detached cleanup. KILL still owns both groups.
    """
    def __init__(self, process, child):
        self.process, self.pid = process, child

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def signal_group(self, signum):
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            # The child may still be entering its session after the info pipe
            # closes. Its PID remains owned while its supervisor is alive.
            if self.process.poll() is None:
                try: os.kill(self.pid, signum)
                except ProcessLookupError: pass
        finally:
            if signum == signal.SIGKILL:
                try: os.killpg(self.process.pid, signum)
                except ProcessLookupError: pass
