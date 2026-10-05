# SPDX-License-Identifier: MIT
"""Linux-only fail-closed research sandbox with a stricter generated-code profile.

Landlock limits filesystem reads/writes; seccomp denies networking, process-memory
inspection and privilege changes. RPC is the only path to LLM and protein tools.
See docs/REPLAY_SECURITY.md for limits and kernel prerequisites.
"""
import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import platform
import sys


class SandboxUnavailable(RuntimeError):
    pass


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class ArgCompare(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
               ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def probe():
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"):
        return {"available": False, "reason": "Linux x86_64/aarch64 required"}
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    seccomp = ctypes.util.find_library("seccomp")
    return {"available": abi >= 1 and bool(seccomp), "landlock_abi": abi,
            "libseccomp": bool(seccomp),
            "profile": "landlock-read-write+seccomp-no-truncate-rename-metadata",
            "reason": "Requires Landlock ABI>=1 and libseccomp; missing primitives fail closed"}


def restrict(read_roots, work, *, generated_code=False):
    state = probe()
    if not state["available"]:
        raise SandboxUnavailable(state["reason"])
    # Apply before scientific libraries can spawn threads. Broker sets BLAS thread count=1.
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise SandboxUnavailable("Sandbox must start single-threaded")
    libc = ctypes.CDLL(None, use_errno=True)
    sec = ctypes.CDLL(ctypes.util.find_library("seccomp"), use_errno=True)
    sec.seccomp_init.argtypes = [ctypes.c_uint32]
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_syscall_resolve_name.restype = ctypes.c_int
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    sec.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(ArgCompare)]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    # ABI 1 already restricts reads/writes. Newer REFER/TRUNCATE rights are only
    # requested when supported; those syscalls are denied independently below.
    handled = (1 << 13) - 1
    if state["landlock_abi"] >= 2:
        handled |= 1 << 13
    if state["landlock_abi"] >= 3:
        handled |= 1 << 14
    if state["landlock_abi"] >= 5:
        handled |= 1 << 15  # device ioctl
    fd = libc.syscall(444, ctypes.byref(Ruleset(handled)), ctypes.sizeof(Ruleset), 0)
    if fd < 0:
        raise SandboxUnavailable("Cannot create Landlock ruleset")
    try:
        def allow(path, writable=False):
            path = Path(path).resolve()
            if not path.exists():
                return
            flags = (1 << 2) | ((1 << 3) if path.is_dir() else 0)
            if writable:
                flags |= (1 << 1) | (1 << 14)
                if path.is_dir():
                    flags |= (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 12) | (1 << 13)
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(flags & handled, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) < 0:
                    raise SandboxUnavailable("Cannot install Landlock path rule")
            finally:
                os.close(path_fd)
        for root in read_roots:
            allow(root)
        allow(work, writable=not generated_code)
        allow("/dev/null", writable=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, fd, 0) != 0:
            raise SandboxUnavailable("Cannot enforce Landlock")
    finally:
        os.close(fd)
    ctx = sec.seccomp_init(0x7fff0000)  # allow default; block dangerous side channels explicitly
    if not ctx:
        raise SandboxUnavailable("Cannot initialize seccomp")
    try:
        blocked = ["socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
            "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "pidfd_open",
            "io_uring_setup", "io_uring_enter", "io_uring_register", "bpf", "perf_event_open",
            "mount", "umount2", "unshare", "setns", "open_by_handle_at", "name_to_handle_at",
            "execve", "execveat", "fork", "vfork", "kill", "tkill", "tgkill",
            "keyctl", "add_key", "request_key",
            # Older Landlock does not mediate these operations sufficiently. They
            # are unnecessary in a worker whose scientific outputs travel by RPC.
            "truncate", "ftruncate", "truncate64", "ftruncate64", "creat", "openat2",
            "rename", "renameat", "renameat2", "link", "linkat", "ioctl",
            # Landlock does not restrict all metadata changes, even on newer ABIs.
            "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown",
            "fchownat", "utime", "utimes", "futimesat", "utimensat",
            "setxattr", "lsetxattr", "fsetxattr", "removexattr", "lremovexattr", "fremovexattr"]
        if generated_code:
            blocked += ["clone", "clone3", "prlimit64", "setrlimit"]
        for name in blocked:
            nr = sec.seccomp_syscall_resolve_name(name.encode())
            if nr >= 0 and sec.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, nr, 0) != 0:
                raise SandboxUnavailable("Cannot install seccomp rule")
        # O_RDONLY|O_TRUNC can truncate without WRITE_FILE on ABI <3. Filter
        # the flags, not just truncate(2); openat2's indirect flags are denied above.
        for name, index in (("open", 1), ("openat", 2)):
            nr = sec.seccomp_syscall_resolve_name(name.encode())
            condition = ArgCompare(index, 7, os.O_TRUNC, os.O_TRUNC)  # SCMP_CMP_MASKED_EQ
            if nr >= 0 and sec.seccomp_rule_add_array(ctx, 0x00050000 | errno.EPERM,
                    nr, 1, ctypes.byref(condition)) != 0:
                raise SandboxUnavailable("Cannot install O_TRUNC protection")
        if sec.seccomp_load(ctx) != 0:
            raise SandboxUnavailable("Cannot enforce seccomp")
    finally:
        sec.seccomp_release(ctx)
    return state
