# SPDX-License-Identifier: MIT
"""Explicit read-only mount-namespace alternative; network isolation remains seccomp.

Never uses --unshare-net, a permissive fallback, privileged setup, or host /proc.
The trusted launcher runs a fresh interpreter; untrusted code starts only after
restrict() has verified its boundary and synchronized the additional syscall filter.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


def _unavailable(message):
    from .sandbox import SandboxUnavailable
    return SandboxUnavailable(message)


def identity():
    binary = shutil.which("bwrap")
    if not binary:
        raise _unavailable("Explicit bwrap backend requires bubblewrap")
    path = Path(binary).resolve(strict=True)
    # A privileged binary is not required or accepted by this backend.
    if path.stat().st_mode & 0o6000:
        raise _unavailable("bwrap backend requires an unprivileged executable")
    try:
        capabilities = os.getxattr(path, "security.capability")
    except OSError as exc:
        if exc.errno not in {errno.ENODATA, errno.ENOTSUP}:
            raise _unavailable("Cannot inspect bwrap file capabilities") from exc
    else:
        if capabilities:
            raise _unavailable("bwrap backend rejects executable file capabilities")
    try:
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                 timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise _unavailable("Cannot identify bwrap executable") from exc
    if not version or len(version) > 200:
        raise _unavailable("Invalid bwrap version response")
    return {"sandbox_backend": "bwrap", "bwrap_path": str(path),
            "bwrap_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bwrap_version": version,
            "bwrap_policy_sha256": hashlib.sha256(b"bwrap\0" + Path(__file__).read_bytes() +
                b"\0seccomp\0" + Path(__file__).with_name("sandbox.py").read_bytes()).hexdigest()}


def command(read_roots, work, module, *, generated_code=False, extra_args=()):
    """Build a single mandatory policy, never broaden mounts after a launch failure."""
    binary = identity()["bwrap_path"]
    executable = str(Path(sys.executable).resolve(strict=True))
    work = str(Path(work).resolve(strict=True))
    roots = {str(Path(root).resolve(strict=True)) for root in read_roots}
    roots.add(executable)
    if "/" in roots or any(Path(work).is_relative_to(root) for root in roots):
        raise _unavailable("Sandbox work must be outside all runtime read roots")
    args = [binary, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--cap-drop", "ALL", "--die-with-parent", "--new-session"]
    # Parent-before-child ordering handles nested Python/site-package roots.
    for root in sorted(roots, key=lambda value: (len(Path(value).parts), value)):
        args += ["--ro-bind", root, root]
    # Dynamic loader aliases only; targets must already be in the exact allowlist.
    for alias in ("/lib", "/lib64"):
        path = Path(alias)
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not any(target.is_relative_to(root) for root in roots):
                raise _unavailable("Dynamic loader alias is outside runtime allowlist")
            args += ["--symlink", os.readlink(alias), alias]
    args += ["--dev-bind", "/dev/null", "/dev/null",
             "--ro-bind" if generated_code else "--bind", work, work,
             "--remount-ro", "/", "--chdir", work,
             executable, "-m", module, *extra_args]
    return args


def verify_child_boundary(work, *, generated_code):
    """Validate trusted-launch invariants before applying the additional filter.

An environment marker is deliberately insufficient: direct host invocation fails.
No generated source or scientific module has been imported at this point.
"""
    if (os.getpid() != 2 or os.getppid() != 1 or Path("/proc").exists()
            or not os.statvfs("/").f_flag & os.ST_RDONLY
            or bool(os.statvfs(work).f_flag & os.ST_RDONLY) != generated_code):
        raise _unavailable("Missing mandatory bwrap process/filesystem boundary")


def assert_private_paths(paths, read_roots):
    for item in paths:
        path = Path(item).resolve()
        if any(path.is_relative_to(Path(root).resolve()) for root in read_roots):
            raise ValueError("Move campaign, labels and private inputs outside runtime read roots")


def probe_bwrap():
    """Exercise the production mount policy and filter, using only synthetic canaries."""
    state = {"available": False, "backend": "bwrap", "profile": "bwrap-readonly-root+seccomp-v1"}
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"):
        return {**state, "reason": "Linux x86_64/aarch64 required"}
    try:
        from .broker import reader_roots
        ident = identity()
        with tempfile.TemporaryDirectory(prefix="proteinrsi-bwrap-probe-") as tmp:
            root = Path(tmp)
            work = root/"work"
            work.mkdir()
            hidden = root/"hidden-label-canary"
            hidden.write_text("SYNTHETIC PRIVATE CANARY")
            readonly = root/"reference"
            readonly.write_text("READONLY CANARY")
            roots = [*reader_roots(), str(readonly)]
            payload = {"work": str(work), "read_roots": roots, "hidden": str(hidden),
                       "readonly": str(readonly)}
            env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "LD_LIBRARY_PATH": str(Path(sys.base_prefix)/"lib")}
            # Both production policies must work, not just a less restrictive fixture.
            receipts = []
            for generated in (False, True):
                payload["generated"] = generated
                result = subprocess.run(command(roots, work, __name__, generated_code=generated),
                    input=json.dumps(payload), text=True, capture_output=True, timeout=10,
                    env=env, cwd=work, close_fds=True, start_new_session=True)
                if result.returncode != 0:
                    raise _unavailable("bwrap enforcement probe failed: " + result.stderr[-700:])
                receipt = json.loads(result.stdout)
                if receipt != {"confined": True, "generated": generated, "denials": 9}:
                    raise _unavailable("Invalid bwrap enforcement probe receipt")
                receipts.append(receipt)
            if hidden.read_text() != "SYNTHETIC PRIVATE CANARY" or readonly.read_text() != "READONLY CANARY":
                raise _unavailable("bwrap canary changed")
        return {**state, **ident, "available": True, "enforcement": receipts,
                "reason": "Explicit bwrap mount isolation and seccomp enforcement passed"}
    except Exception as exc:
        return {**state, "reason": str(exc)[:1000]}


def _probe_child():
    import errno
    import socket
    from .sandbox import restrict
    request = json.loads(sys.stdin.read())
    restrict(request["read_roots"], request["work"], generated_code=request["generated"], backend="bwrap")
    denied_count = 0
    def denied(fn):
        nonlocal denied_count
        try:
            fn()
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS}:
                raise
            denied_count += 1
        else:
            raise AssertionError("Mandatory denied operation succeeded")
    assert Path(request["readonly"]).read_text() == "READONLY CANARY"
    denied(lambda: Path(request["hidden"]).read_text())
    denied(lambda: os.open(request["readonly"], os.O_WRONLY))
    denied(lambda: os.open("/escape", os.O_CREAT | os.O_WRONLY, 0o600))
    denied(lambda: os.open(request["readonly"], os.O_RDONLY | os.O_TRUNC))
    denied(socket.socket)
    denied(socket.socketpair)
    denied(os.fork)
    denied(lambda: os.execl(sys.executable, sys.executable, "-c", "pass"))
    scratch = Path(request["work"])/"scratch"
    if request["generated"]:
        denied(lambda: os.open(scratch, os.O_CREAT | os.O_WRONLY, 0o600))
    else:
        fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"scratch")
        denied(lambda: os.ftruncate(fd, 0))
        os.close(fd)
        assert scratch.read_bytes() == b"scratch"
    print(json.dumps({"confined": True, "generated": request["generated"], "denials": denied_count}))


if __name__ == "__main__":
    _probe_child()
