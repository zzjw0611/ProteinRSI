# SPDX-License-Identifier: MIT
"""Real bubblewrap acceptance with synthetic inputs, never provider/science calls.

Run with PROTEINRSI_REQUIRE_BWRAP=1 to make missing bwrap/kernel support a
failure. There are no mocked enforcement/probe/execution results. The default
suite skips an unsupported host; explicit acceptance is never allowed to skip.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.replay import bwrap_backend, sandbox
from proteinrsi.replay.broker import reader_roots
from proteinrsi.research.code import execute_code
from proteinrsi.storage import Store


@pytest.fixture(scope="module", autouse=True)
def require_actual_bwrap():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap")
        state = sandbox.probe()
        if not state["available"]:
            message = "Real bwrap enforcement unavailable: " + str(state)
            if os.environ.get("PROTEINRSI_REQUIRE_BWRAP") == "1":
                pytest.fail(message)
            pytest.skip(message)
        assert sandbox.selected_backend() == "bwrap"
        yield state


def _execute(tmp_path, code, inputs=None):
    return execute_code(Store(tmp_path / "campaign"), None,
                        {"code": code, "inputs": inputs or {}}, pure=True)


def _success(result):
    assert result["status"] == "ok", result
    assert result["execution_backend"] == "bwrap_seccomp_generated_v1"
    assert result["measurement_authority"] is False
    return result["output"]


def test_both_production_probe_policies_are_actually_enforced(require_actual_bwrap):
    state = require_actual_bwrap
    assert state["backend"] == state["sandbox_backend"] == "bwrap"
    assert state["enforcement"] == [
        {"confined": True, "generated": False, "denials": 9},
        {"confined": True, "generated": True, "denials": 9},
    ]
    assert len(state["bwrap_sha256"]) == len(state["bwrap_policy_sha256"]) == 64


# This trusted harness changes only the module imported by the launcher. The
# production mount builder, boundary verifier and seccomp filter run unchanged.
_KERNEL_HARNESS = r'''
import ctypes, errno, json, os, socket, sys
from pathlib import Path
from proteinrsi.replay.sandbox import restrict
request = json.loads(sys.stdin.readline())
restrict(request['roots'], request['work'], generated_code=request['generated'], backend='bwrap')
assert os.getpid() == 2 and os.getppid() == 1
assert not Path('/proc').exists()
results = {}
def denied(name, operation, expected=(errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS)):
    try:
        operation()
    except OSError as exc:
        assert exc.errno in expected, (name, exc.errno)
        results[name] = exc.errno
    else:
        raise AssertionError(name + ' unexpectedly succeeded')
work = Path(request['work'])
reference = Path(request['reference'])
assert reference.read_text() == 'READONLY SYNTHETIC REFERENCE'
assert (work / 'input.json').read_text() == '{"synthetic": true}'
for index, path in enumerate(request['hidden'] + [str(work / 'symlink-escape'), '/proc/self/environ']):
    denied('hidden_read_' + str(index), lambda path=path: Path(path).read_bytes())
for index, path in enumerate(request['write_targets']):
    denied('immutable_root_' + str(index), lambda path=path: os.open(path, os.O_CREAT | os.O_WRONLY, 0o600),
           (errno.EROFS, errno.EACCES, errno.EPERM))
denied('reference_write', lambda: os.open(reference, os.O_WRONLY), (errno.EROFS, errno.EACCES, errno.EPERM))
denied('readonly_open_trunc', lambda: os.open(reference, os.O_RDONLY | os.O_TRUNC), (errno.EPERM,))
denied('truncate', lambda: os.truncate(reference, 0), (errno.EPERM,))
denied('chmod', lambda: os.chmod(reference, 0o777), (errno.EPERM,))
denied('utime', lambda: os.utime(reference, (1, 1)), (errno.EPERM,))
denied('hardlink', lambda: os.link(reference, work / 'hardlink'), (errno.EPERM,))
denied('rename', lambda: os.rename(reference, work / 'renamed'), (errno.EPERM,))
if request['generated']:
    denied('work_write', lambda: os.open(work / 'scratch', os.O_CREAT | os.O_WRONLY, 0o600),
           (errno.EROFS, errno.EACCES, errno.EPERM))
else:
    fd = os.open(work / 'scratch', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, b'SYNTHETIC SCRATCH')
    denied('ftruncate', lambda: os.ftruncate(fd, 0), (errno.EPERM,))
    os.close(fd)
    assert (work / 'scratch').read_bytes() == b'SYNTHETIC SCRATCH'
for name, family in [('ipv4', socket.AF_INET), ('ipv6', socket.AF_INET6), ('unix', socket.AF_UNIX)]:
    denied(name, lambda family=family: socket.socket(family, socket.SOCK_STREAM), (errno.EPERM,))
denied('socketpair', socket.socketpair, (errno.EPERM,))
denied('fork', os.fork, (errno.EPERM,))
# Invalid arguments would yield EINVAL/EFAULT/ENOENT without the filter. Exact
# EPERM proves denial without ever asking a raw syscall to create a real process
# or execute a program, even if the tested filter is defective.
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
sec = ctypes.CDLL('libseccomp.so.2')
sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
sec.seccomp_syscall_resolve_name.restype = ctypes.c_int
def raw_denied(name, *args):
    nr = sec.seccomp_syscall_resolve_name(name.encode())
    if nr < 0:
        assert name in ('fork', 'vfork', 'setrlimit'), (name, nr)
        return
    ctypes.set_errno(0)
    value = libc.syscall(ctypes.c_long(nr), *args)
    assert value == -1 and ctypes.get_errno() == errno.EPERM, (name, value, ctypes.get_errno())
    results['raw_' + name] = errno.EPERM
raw_denied('clone', ctypes.c_ulong(-1), ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_ulong(0))
raw_denied('clone3', ctypes.c_void_p(0), ctypes.c_ulong(88))
raw_denied('execve', ctypes.c_char_p(b'/nonexistent-synthetic-executable'), ctypes.c_void_p(0), ctypes.c_void_p(0))
raw_denied('execveat', ctypes.c_int(-100), ctypes.c_char_p(b'/nonexistent-synthetic-executable'),
           ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_int(0))
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    raw_denied('socket', ctypes.c_int(family), ctypes.c_int(socket.SOCK_STREAM), ctypes.c_int(0))
raw_denied('prctl', ctypes.c_int(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
# Null pointers deliberately avoid changing limits if a regression removes the filter.
raw_denied('prlimit64', ctypes.c_int(0), ctypes.c_int(7), ctypes.c_void_p(0), ctypes.c_void_p(0))
raw_denied('setrlimit', ctypes.c_int(7), ctypes.c_void_p(0))
print(json.dumps({'denied': results, 'pid': os.getpid(), 'generated': request['generated']}))
'''


@pytest.mark.parametrize("generated", [False, True])
def test_real_mounts_raw_syscalls_and_symlink_escape(tmp_path, generated):
    work = tmp_path / "work"
    work.mkdir()
    harness = tmp_path / "trusted-harness"
    harness.mkdir()
    (harness / "synthetic_bwrap_probe.py").write_text(_KERNEL_HARNESS)
    reference = tmp_path / "reference.txt"
    reference.write_text("READONLY SYNTHETIC REFERENCE")
    before = reference.stat()
    hidden = [tmp_path / "hidden-labels.csv", tmp_path / "campaign.sqlite3"]
    for path in hidden:
        path.write_text("PRIVATE SYNTHETIC CANARY")
    (work / "symlink-escape").symlink_to(hidden[0])
    (work / "input.json").write_text('{"synthetic": true}')
    roots = [*reader_roots(), str(reference), str(harness)]
    write_targets = ["/bwrap-synthetic-escape", "/tmp/bwrap-synthetic-escape",
                     "/dev/bwrap-synthetic-escape", str(tmp_path / "mount-parent-escape"),
                     str(harness / "code-mutation")]
    request = {"roots": roots, "work": str(work), "reference": str(reference),
               "hidden": list(map(str, hidden)), "write_targets": write_targets,
               "generated": generated}
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep.join([
               str(Path(__file__).resolve().parents[1] / "src"), str(harness)]),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
           "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}
    command = bwrap_backend.command(roots, work, "synthetic_bwrap_probe", generated_code=generated)
    assert "--unshare-net" not in command
    completed = subprocess.run(command, input=json.dumps(request) + "\n", text=True,
                               capture_output=True, timeout=15, env=env, cwd=work,
                               close_fds=True, start_new_session=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["pid"] == 2 and receipt["generated"] is generated
    assert {"raw_clone", "raw_clone3", "raw_execve", "raw_execveat", "raw_socket",
            "raw_prctl", "raw_prlimit64", "ipv4", "ipv6", "unix"} <= set(receipt["denied"])
    assert len(receipt["denied"]) >= 28
    assert reference.read_text() == "READONLY SYNTHETIC REFERENCE"
    assert (reference.stat().st_mode, reference.stat().st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    assert all(path.read_text() == "PRIVATE SYNTHETIC CANARY" for path in hidden)
    assert all(not Path(path).exists() for path in write_targets)


def test_production_generated_numpy_and_artifact_input(tmp_path):
    store = Store(tmp_path / "campaign")
    source = tmp_path / "synthetic-input.json"
    source.write_text('{"values": [2, 4, 6]}')
    registry = ArtifactStore(store)
    artifact = registry.put(source, "json", origin="synthetic-bwrap-acceptance")
    code = """
import json, numpy as np
assert context == {'synthetic': True}
values = json.load(open(artifacts[inputs['ref']]))['values']
result = {'sum': float(np.asarray(values).sum()), 'pid': __import__('os').getpid()}
"""
    view = SimpleNamespace(model_dump=lambda **kwargs: {"synthetic": True})
    result = execute_code(store, view, {"code": code, "inputs": {"ref": artifact["ref"]},
                                       "artifact_refs": [artifact["ref"]]})
    assert _success(result) == {"sum": 12.0, "pid": 2}
    assert registry.resolve(artifact["ref"]).read_bytes() == source.read_bytes()


def test_production_generated_private_files_environment_and_inheritable_fds(tmp_path, monkeypatch):
    hidden = tmp_path / "hidden-labels.csv"
    hidden.write_text("SYNTHETIC LABELS NEVER SHARED")
    store = Store(tmp_path / "campaign")
    store.put("private", "canary", {"secret": "SYNTHETIC CAMPAIGN CANARY"})
    source_fd = os.open(hidden, os.O_RDONLY)
    pipe_read, pipe_write = os.pipe()
    left, right = socket.socketpair()
    inherited = []
    try:
        os.write(pipe_write, b"SYNTHETIC PIPE CANARY")
        right.sendall(b"SYNTHETIC SOCKET CANARY")
        for fd in (source_fd, pipe_read, left.fileno()):
            copied = fcntl.fcntl(fd, fcntl.F_DUPFD, 200)
            os.set_inheritable(copied, True)
            assert os.get_inheritable(copied)
            inherited.append(copied)
        monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC KEY CANARY")
        monkeypatch.setenv("PROTEINRSI_API_KEY", "SYNTHETIC KEY CANARY")
        monkeypatch.setenv("PROTEINRSI_FD_CANARY", "SYNTHETIC ENV CANARY")
        code = """
import errno, os
for name in ['OPENAI_API_KEY', 'PROTEINRSI_API_KEY', 'PROTEINRSI_FD_CANARY']:
    assert name not in os.environ
for fd in inputs['fds']:
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF, exc.errno
    else:
        raise AssertionError('Synthetic inheritable descriptor leaked')
for path in inputs['paths']:
    try:
        with open(path, 'rb') as f:
            f.read()
    except OSError as exc:
        assert exc.errno in (errno.EPERM, errno.EACCES, errno.ENOENT), exc.errno
    else:
        raise AssertionError('Private synthetic file was readable')
result = {'closed_fds': len(inputs['fds']), 'private_paths': len(inputs['paths'])}
"""
        result = execute_code(store, None, {"code": code, "inputs": {"fds": inherited,
            "paths": [str(hidden), str(store.path), "/proc/self/environ", "/proc/self/fd"]}}, pure=True)
        assert _success(result) == {"closed_fds": 3, "private_paths": 4}
        assert hidden.read_text() == "SYNTHETIC LABELS NEVER SHARED"
        assert store.get("private", "canary") == {"secret": "SYNTHETIC CAMPAIGN CANARY"}
    finally:
        for fd in [source_fd, pipe_read, pipe_write, *inherited]:
            os.close(fd)
        left.close()
        right.close()


def test_production_generated_memory_and_descriptor_bounds(tmp_path):
    code = """
import errno, mmap, os
try:
    allocation = mmap.mmap(-1, 3 * 1024 ** 3)
except OSError as exc:
    assert exc.errno == errno.ENOMEM, exc.errno
else:
    allocation.close()
    raise AssertionError('Three-GiB virtual allocation exceeded the two-GiB limit')
fds = []
try:
    for i in range(80):
        try:
            fds.append(os.open('/dev/null', os.O_RDONLY))
        except OSError as exc:
            assert exc.errno == errno.EMFILE, exc.errno
            break
    else:
        raise AssertionError('Descriptor limit was not enforced')
finally:
    for fd in fds:
        os.close(fd)
assert 1 <= len(fds) <= 61, len(fds)
result = {'memory_denied': True, 'fd_bound': True}
"""
    assert _success(_execute(tmp_path, code)) == {"memory_denied": True, "fd_bound": True}


@pytest.mark.parametrize("code,error", [
    ("print('x' * 17000)\nresult = {}", "ValueError"),
    ("result = {'padding': 'x' * 600000}", "ValueError"),
    ("import os,json\nos.write(1,json.dumps({'status':'ok','output':{'padding':'x'*600000}}).encode())\nos._exit(0)",
     "InvalidWorkerOutput"),
    ("import os\nos.write(1,b'x' * (2 * 1024 ** 2))\nresult = {}", "WorkerExit"),
])
def test_production_output_and_file_size_bounds(tmp_path, code, error):
    result = _execute(tmp_path, code)
    assert result["status"] == "failed", result
    assert result["error_type"] == error, result


@pytest.mark.parametrize("code,error,minimum,maximum", [
    ("while True:\n    pass", "WorkerExit", 7, 19),
    ("import time\ntime.sleep(120)\nresult = {}", "Timeout", 19, 27),
])
def test_production_cpu_and_wall_bounds(tmp_path, code, error, minimum, maximum):
    started = time.monotonic()
    result = _execute(tmp_path, code)
    elapsed = time.monotonic() - started
    assert result["status"] == "failed" and result["error_type"] == error, result
    if error == "WorkerExit":
        # bubblewrap conventionally forwards the fatal signal as 128+signum.
        assert result["returncode"] in (-signal.SIGKILL, 128 + signal.SIGKILL), result
    assert minimum <= elapsed < maximum, (elapsed, result)


_CONTROLLER = r'''
import json, os, sys
from proteinrsi.research import code
from proteinrsi.storage import Store
# Observe the real Popen return, preserving every argument and executing all
# production code. No confinement primitive or execution result is substituted.
original = code.subprocess.Popen
def observed(*args, **kwargs):
    child = original(*args, **kwargs)
    if args and 'proteinrsi.research.code_worker' in args[0]:
        print(json.dumps({'supervisor': child.pid, 'work': kwargs['cwd']}), flush=True)
    return child
code.subprocess.Popen = observed
source = "import fcntl,os,time\nassert os.getpid() == 2\nfcntl.lockf(1,fcntl.LOCK_EX)\nos.write(1,b'ISOLATED_INNER_LOCKED\\n')\ntime.sleep(8)\nresult = {}"
code.execute_code(Store(sys.argv[1]), None, {'code': source, 'inputs': {}}, pure=True)
'''


def _lock_available(handle):
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        assert exc.errno in (errno.EAGAIN, errno.EACCES), exc.errno
        return False
    else:
        fcntl.lockf(handle, fcntl.LOCK_UN)
        return True


@pytest.mark.parametrize('interruption', [signal.SIGKILL, signal.SIGINT],
                         ids=['controller-death', 'controller-interrupt'])
def test_inner_generated_worker_dies_on_controller_loss(tmp_path, interruption):
    env = {**os.environ, 'PROTEINRSI_SANDBOX_BACKEND': 'bwrap',
           'PYTHONPATH': os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)}
    controller = subprocess.Popen([sys.executable, '-c', _CONTROLLER, str(tmp_path / 'controller')],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=env, close_fds=True, start_new_session=True)
    lock_probe = None
    try:
        assert select.select([controller.stdout], [], [], 10)[0], 'Controller launch timed out'
        line = controller.stdout.readline()
        assert line, 'Controller exited before launch: ' + controller.stderr.read()
        observed = json.loads(line)
        output = Path(observed['work']) / 'stdout'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if output.exists() and b'ISOLATED_INNER_LOCKED\n' in output.read_bytes():
                break
            time.sleep(0.02)
        else:
            pytest.fail('The actual isolated inner worker never acquired its lifetime lock')
        # A process-owned POSIX lock is acquired by the actual isolated PID 2.
        # Unlike /proc polling this works when host process views hide nested PIDs.
        # Controller/supervisor death alone cannot release the inner process lock.
        # The sleeping inner code never unlocks or closes the file. Keep this file
        # open across controller cleanup/unlink, so path deletion is not evidence.
        lock_probe = output.open('r+b')
        assert not _lock_available(lock_probe), 'Inner worker lifetime lock was not held'
        started = time.monotonic()
        controller.send_signal(interruption)
        controller.wait(timeout=3)
        while time.monotonic() - started < 3:
            if _lock_available(lock_probe):
                break
            time.sleep(0.02)
        else:
            pytest.fail('Actual inner worker retained its process lock after controller loss')
        assert time.monotonic() - started < 3  # Its natural sleep lasts eight seconds.
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        if lock_probe is not None:
            # In a defective build the synthetic worker exits naturally after its
            # bounded eight-second sleep; do not leave it running beyond this test.
            deadline = time.monotonic() + 9
            while not _lock_available(lock_probe) and time.monotonic() < deadline:
                time.sleep(0.05)
            lock_probe.close()
        controller.stdout.close()
        controller.stderr.close()


def test_production_file_size_limit_returns_precise_efbig(tmp_path):
    code = """
import errno, os
written = os.write(2, b'x' * (2 * 1024 ** 2))
assert written == 1024 ** 2, written
try:
    os.write(2, b'x')
except OSError as exc:
    assert exc.errno == errno.EFBIG, exc.errno
else:
    raise AssertionError('One-MiB file size limit was not enforced')
result = {'file_size_limit': written}
"""
    assert _success(_execute(tmp_path, code)) == {'file_size_limit': 1024 ** 2}


def test_missing_bwrap_executable_fails_closed_before_generated_source(tmp_path, monkeypatch):
    # Actual executable discovery failure, not a mocked successful enforcement.
    monkeypatch.setenv('PATH', str(tmp_path / 'empty-path'))
    assert sandbox.selected_backend() == 'bwrap'
    state = sandbox.probe()
    assert state['available'] is False and state['backend'] == 'bwrap'
    assert 'bubblewrap' in state['reason']
    store = Store(tmp_path / 'campaign')
    with pytest.raises(sandbox.SandboxUnavailable, match='bubblewrap'):
        execute_code(store, None, {'code': "raise AssertionError('must not execute')",
                                   'inputs': {}}, pure=True)
    assert not (store.root / 'programs').exists()
    assert not store.all('code_programs')


def test_actual_bwrap_setup_failure_never_executes_requested_module(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    removed = tmp_path / 'removed-runtime-canary'
    removed.write_text('SYNTHETIC SETUP CANARY')
    roots = [*reader_roots(), str(removed)]
    command = bwrap_backend.command(roots, work, 'this_synthetic_module_must_never_execute')
    # A real mount setup failure after construction must be terminal. This does
    # not attempt another namespace configuration or weaken the failed policy.
    removed.unlink()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10,
                               close_fds=True, start_new_session=True)
    assert completed.returncode != 0
    assert not completed.stdout
    assert str(removed) in completed.stderr
    assert 'No such file or directory' in completed.stderr
    assert 'No module named' not in completed.stderr  # Python never started.


def test_direct_restrict_cannot_be_authorized_by_environment_marker(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    code = """
import json, sys
from proteinrsi.replay.sandbox import SandboxUnavailable, restrict
try:
    restrict([], sys.argv[1], generated_code=True, backend='bwrap')
except SandboxUnavailable as exc:
    assert 'Missing mandatory bwrap process/filesystem boundary' in str(exc)
    print(json.dumps({'failed_closed': True}))
else:
    raise AssertionError('Direct unsandboxed launch was accepted')
"""
    completed = subprocess.run([sys.executable, '-c', code, str(work)],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, 'PROTEINRSI_SANDBOX_BACKEND': 'bwrap'},
                               close_fds=True, start_new_session=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {'failed_closed': True}


def test_unsafe_runtime_root_configurations_are_rejected_before_launch(tmp_path):
    # Configuration validation only; this is not counted as kernel isolation.
    work = tmp_path / 'work'
    work.mkdir()
    for roots in ([*reader_roots(), '/'], [*reader_roots(), str(tmp_path)]):
        with pytest.raises(sandbox.SandboxUnavailable, match='outside all runtime read roots'):
            bwrap_backend.command(roots, work, 'proteinrsi.research.code_worker', generated_code=True)
    with pytest.raises(ValueError, match='outside runtime read roots'):
        bwrap_backend.assert_private_paths([tmp_path / 'private-campaign'], [tmp_path])
