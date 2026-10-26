"""Own process groups and persist command receipts, including interruptions."""
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from common import canonical, digest, sha, utc, write_json
from redaction import redact_metadata, validate_redaction_secrets


class GroupTerminationError(OSError):
    """An owned command group did not terminate within its KILL deadline."""


class ReceiptIntegrityError(ValueError):
    """A hook changed the collector's in-progress receipt or output paths."""


class LaunchContainmentError(OSError):
    """A launcher spawned a supervisor but could not establish workload ownership."""


def verify_output(path, stream):
    """Authenticate the path against the still-open collector descriptor."""
    try:
        if os.path.samestat(os.fstat(stream.fileno()), os.stat(path, follow_symlinks=False)):
            return
    except OSError:
        pass
    raise ReceiptIntegrityError('hook replaced or removed command output: ' + path.name)


def expand_command(spec, context):
    def expand(value):
        return re.sub(r'\{([^{}]+)\}',
                      lambda match: str(context.get(match[1], match[0])), value)
    return ([expand(a) for a in spec['argv']], expand(spec['cwd']),
            {key: expand(value) for key, value in spec['env'].items()})


@contextmanager
def defer_interrupts():
    """Finish an owned checkpoint before delivering SIGINT/SIGTERM.

    Temporary handlers defer delivery without inheriting a blocked signal mask
    in the child. Collection and command execution run on the main thread.
    """
    previous = {}
    pending = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            if previous[signum] != signal.SIG_IGN:
                signal.signal(signum, lambda number, frame: pending.append(number))
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        for signum in pending:
            handler = previous[signum]
            if callable(handler):
                handler(signum, None)  # Nested scopes forward to the outer deferral.
            else:
                raise KeyboardInterrupt('collection interrupted by signal %d' % signum)


def group_running(group):
    """Linux group liveness excludes exited zombies, which parents must reap."""
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            continue  # A process exited during the kernel inventory read.
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            return True
    return False


def wait_group(group, deadline):
    # Poll only termination state; this does not coordinate measured workloads.
    wake = threading.Event()
    while group_running(group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        wake.wait(min(.01, remaining))
    return True


def kill_group(process, grace):
    deadline = time.monotonic() + grace
    try:
        signal_group(process, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise GroupTerminationError('command process group leader remains alive after SIGKILL') from None
    if not wait_group(process.pid, deadline):
        raise GroupTerminationError('command process group remains alive after SIGKILL')
    return code


def signal_group(process, signum):
    """A launch adapter may supervise a distinct workload session."""
    # Only a declared launch-adapter method changes ownership. Dynamic
    # attributes (including unconstrained process test doubles) are not an opt-in.
    if callable(getattr(type(process), 'signal_group', None)):
        process.signal_group(signum)
    else:
        os.killpg(process.pid, signum)


def execute(spec, context, directory, step, secrets, *, launch=None):
    validate_redaction_secrets(secrets)
    argv, cwd, env = expand_command(spec, context)
    # Secrets are passed by named environment variable, never logged as values.
    for key in context['redact_env']:
        env[key] = os.environ[key]
    started = utc()
    result = {'step': step, 'argv': argv, 'cwd': cwd,
              'env': {k: v for k, v in env.items() if k not in context['redact_env']},
              'redacted_env_names': context['redact_env'], 'started_at': started,
              'started': False, 'exit': None, 'timeout': False, 'error': None,
              'interrupted': False, 'finished': False, 'containment_proven': True,
              'redactions': 0}
    receipt = directory / (step + '.command.json')
    if receipt.exists():
        raise ReceiptIntegrityError('reserved command receipt already exists: ' + str(receipt))
    write_json(receipt, result)
    checkpoints = {digest(receipt)}
    start = time.monotonic_ns()
    # Consumers in this collector may align receipt observations to this exact
    # invocation origin. This clock is never compared with a child process.
    result['collector_start_ns'] = start
    completed_ns = None
    process = None
    try:
        with (directory / (step + '.stdout')).open('wb') as out, \
                (directory / (step + '.stderr')).open('wb') as err:
            try:
                with defer_interrupts():
                    process = (launch or subprocess.Popen)(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                                           start_new_session=True)
                    result['started'] = True
                    result['containment_proven'] = False
                if digest(receipt) not in checkpoints:
                    raise ReceiptIntegrityError('hook changed in-progress command receipt')
                # A signal can arrive on either side of os.replace. Both our
                # old and intended new bytes are owned until the write returns.
                updated = sha(canonical(result))
                checkpoints.add(updated)
                write_json(receipt, result)
                checkpoints = {updated}
                try:
                    remaining = spec['timeout_seconds'] - (time.monotonic_ns() - start) / 1e9
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, spec['timeout_seconds'])
                    result['exit'] = process.wait(timeout=remaining)
                    completed_ns = time.monotonic_ns()
                    # A successful leader exit cannot leave an unowned writer
                    # modifying retained evidence while the next hook runs.
                    if group_running(process.pid):
                        kill_group(process, spec.get('cancel_grace_seconds', 25))
                        result['error'] = 'command left live descendants after leader exit'
                    result['containment_proven'] = True
                except subprocess.TimeoutExpired:
                    result['timeout'] = True
                    grace = spec.get('cancel_grace_seconds', 25)
                    deadline = time.monotonic() + grace
                    try:
                        signal_group(process, signal.SIGTERM)
                    except ProcessLookupError:
                        pass  # The leader may exit between the watchdog and TERM.
                    try:
                        result['exit'] = process.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
                    if not wait_group(process.pid, deadline):
                        kill_group(process, grace)
                    result['exit'] = process.wait(timeout=0)
                    result['containment_proven'] = True
            except GroupTerminationError:
                raise  # The KILL deadline has already expired; do not restart its budget.
            except BaseException:
                if process is not None:
                    result['exit'] = kill_group(process, spec.get('cancel_grace_seconds', 25))
                    result['containment_proven'] = True
                raise
            finally:
                verify_output(directory / (step + '.stdout'), out)
                verify_output(directory / (step + '.stderr'), err)
    except OSError as error:
        if isinstance(error, LaunchContainmentError):
            result['started'] = True
            result['containment_proven'] = False
        result['error'] = str(error)
    except BaseException:
        result['interrupted'] = True
        raise
    finally:
        end = completed_ns if (completed_ns is not None and result['error'] is None
                               and not result['interrupted']) else time.monotonic_ns()
        result['seconds'] = (end - start) / 1e9
        result['ended_at'] = utc()
        result['finished'] = True
        # Preserve the started receipt on Ctrl-C as well as ordinary failures.
        # Detect hook tampering before replacing our own progress checkpoint.
        if not receipt.is_file() or digest(receipt) not in checkpoints:
            raise ReceiptIntegrityError('hook changed in-progress command receipt')
        replacements = [0]
        result = redact_metadata(result, secrets, step + '.command.json', replacements)
        result['redactions'] = replacements[0]
        write_json(receipt, result)
    return result


def succeeded(command):
    return (command.get('started') is True and command.get('finished') is True
            and command.get('interrupted') is False and command.get('exit') == 0
            and command.get('timeout') is False and command.get('error') is None)
