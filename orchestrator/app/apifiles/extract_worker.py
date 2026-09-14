"""The extraction subprocess, and the parent's one call to run it (design §4.4).

EVERY PARSER OF UNTRUSTED BYTES RUNS HERE, in a child started as

    python -m app.apifiles.extract_worker <op> '<spec json>'

with stdin closed, cwd = the directory holding the `app` package (/app in the
container) and a SCRUBBED environment. It writes only under the blob's
`derived/` directory and prints exactly one JSON line on stdout.

THE CHILD'S FIRST STATEMENTS (design finding #13, 2026-09-13). Before any
module that parses bytes is imported, the child makes itself non-dumpable and
sets RLIMIT_CORE to (1, 1). A parser crash on hostile bytes is exactly the
case this process exists for, and its core must never be written or handed to
a crash collector: a large core costs the shared host memory and minutes of
I/O at the worst moment. Both calls are needed, and the value is 1, not 0:

* PR_SET_DUMPABLE(0) alone is not enough — depending on `fs.suid_dumpable`,
  the kernel still dumps a non-dumpable process when `core_pattern` is a pipe;
* for a PIPE `core_pattern` the kernel ignores RLIMIT_CORE = 0 but skips the
  dump outright when the limit is exactly 1 (the value its own usermode
  helper uses to recognise itself). The availability programme measured this.

They are set by the child itself, not through `preexec_fn`, which is unsafe
in the threaded orchestrator. If the hard core limit is already below 1 the
child cannot guarantee "no core" and refuses to parse anything.

THEN THE SANDBOX (review finding, 2026-09-13). A scrubbed environment alone
is not isolation: a child sharing the parent's identity, mounts and network
can still reach whatever the parent can. So before any parser is imported
the child, in this order:

1. drops EVERY capability (effective, permitted, inheritable, ambient) and
   locks SECBIT_NOROOT when it may. Root without capabilities is an ordinary
   owner of root-owned files — it can still read its source and write its
   `derived/`, but DAC_OVERRIDE, CHOWN and the ptrace capability are gone,
   and the kernel refuses a less-capable task access to a more-capable
   one's /proc/<pid>/environ;
2. sets PR_SET_NO_NEW_PRIVS (nothing it executes can regain privileges);
3. applies a Landlock ruleset: read-only beneath the interpreter, its
   libraries, the `app` package, /usr, /lib, /etc and its own /proc/self;
   read-write (but no symlinks, device nodes, sockets, FIFOs or cross-dir
   links) ONLY beneath its blob's `derived/`, a render `out_dir` and a
   private temp directory the parent creates and removes; no TCP bind or
   connect (ABI ≥ 4); no signals to, and no abstract sockets of, processes
   outside the sandbox (ABI ≥ 6). Landlock also scopes ptrace, so
   /proc/<ppid>/environ is refused even to a root child;
4. installs a seccomp filter that refuses socket()/socketpair() of every
   family, io_uring, ptrace, process_vm_*, bpf, userfaultfd, perf_event_open,
   the keyring calls and unshare/setns, and kills a foreign-ABI syscall.

Measured 2026-09-13 on this box (kernel 6.17, Landlock ABI 7) and as root in
the orchestrator image under Docker's default seccomp profile: every attempt
of the `test-escape` op was refused (EACCES/EPERM, EXDEV for a cross-directory
link) while its derived/ and temp writes succeeded; the extractor suite and
pdf/docx/pptx/xlsx/csv/json/html/png ops in the image all pass sandboxed.
Landlock refused every escape even with the capability drop disabled, so the
layers are independent: each one alone closes the parent's environment. PUBLIC_API_FILES_EXTRACT_SANDBOX
= `required` (default) refuses to parse when any layer cannot be applied —
that ends as a deferral (`processing_unavailable` after the attempts), not a
verdict on the file; `best_effort` applies what the kernel offers (a
development box without Landlock).

THEN THE CEILINGS. RLIMIT_AS (PUBLIC_API_FILES_EXTRACT_RLIMIT_AS_GIB, 8 GiB),
RLIMIT_CPU (the kind's CPU seconds; SIGXCPU at the soft limit, SIGKILL 5 s
later), RLIMIT_NOFILE 256 and RLIMIT_FSIZE. The design says FSIZE = 4 × the
original's bytes; this uses max(4 ×, 256 MiB) because the limit is per FILE
and a fully-compressible DOCX measured 34.8× its size as JSONL — a 64 KiB
document would otherwise be killed writing a 2 MiB pages table. CPython
ignores SIGXFSZ, so an oversized write raises OSError(EFBIG) and becomes
`file_too_complex`.

THE PARENT (`run`). Spawns the child in its own session, enforces the kind's
wall deadline, kills the child when the job is cancelled (DELETE, lost lease,
shutdown) through `video.media._stop_child` — the chat pipeline's own
terminate/wait/kill that is shielded against the caller's cancellation — and
maps how the child ended onto the closed error vocabulary:

    exit 0 + {"ok": true}            facts
    exit 0 + {"ok": false, code}     that verdict
    SIGSEGV / SIGBUS / SIGABRT / SIGILL / SIGFPE   file_corrupt
    SIGXCPU / SIGXFSZ / wall deadline / EFBIG / MemoryError   file_too_complex
    SIGKILL (from outside: the host OOM killer)   retry (deferral)
    sandbox not applicable in `required` mode    retry (deferral)
    anything else                    internal_error
"""
from __future__ import annotations

import ctypes
import os
import resource
import stat
import struct
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3
#: The one RLIMIT_CORE value a pipe `core_pattern` honours (module docstring).
CORE_LIMIT = 1


def harden_crash_dumps() -> dict:
    """Non-dumpable, RLIMIT_CORE = (1, 1). Returns what is now in force."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")
    _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    if hard != resource.RLIM_INFINITY and hard < CORE_LIMIT:
        raise OSError("the hard core limit is below 1; a crash could not be kept from the crash collector")
    resource.setrlimit(resource.RLIMIT_CORE, (CORE_LIMIT, CORE_LIMIT))
    return {
        "dumpable": int(libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)),
        "core": list(resource.getrlimit(resource.RLIMIT_CORE)),
    }


if __name__ == "__main__":
    # The child: these run before any module that parses bytes is imported.
    try:
        _HARDENED = harden_crash_dumps()
    except Exception:  # noqa: BLE001
        sys.stdout.write('{"ok": false, "code": "internal_error", "ceiling": "", "why": "unhardened"}\n')
        sys.stdout.flush()
        os._exit(70)

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, Optional  # noqa: E402

from .extractors import ExtractError, FileCorrupt, FileTooComplex, Spec, error_from_json  # noqa: E402

log = logging.getLogger(__name__)

_MIB = 1024 * 1024
NOFILE_LIMIT = 256
FSIZE_FLOOR_BYTES = 256 * _MIB
FSIZE_CEILING_BYTES = 64 * 1024 * _MIB
#: Seconds between SIGXCPU (soft) and SIGKILL (hard) for the CPU ceiling.
CPU_HARD_GRACE_S = 5
#: The child's stdout is one JSON line; anything beyond this is not ours.
STDOUT_MAX_BYTES = 4 * _MIB
STDERR_TAIL_BYTES = 8192

#: Exit code the child uses for a verdict it printed itself.
EXIT_VERDICT = 3

#: Environment the child inherits. Everything else — database URLs, API keys,
#: webhook secrets, session secrets — stays in the parent: a parser exploit in
#: the child must not be one `os.environ` away from the credentials.
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "HOME", "PYTHONPATH")
_ENV_PREFIXES = ("ARCHIVE_", "PROFILE_", "PUBLIC_API_FILES_")

_CORRUPT_SIGNALS = {signal.SIGSEGV, signal.SIGBUS, signal.SIGABRT, signal.SIGILL, signal.SIGFPE}
#: SIGKILL is NOT a ceiling (review finding, 2026-09-13): the child's own
#: ceilings never send it first — RLIMIT_CPU sends SIGXCPU at the soft limit
#: (CPython's default action ends the child there), RLIMIT_AS raises
#: MemoryError, the wall deadline is handled before signals are mapped. A bare
#: SIGKILL comes from outside, and on the 121 GiB unified-memory host the
#: model engines share that is the kernel OOM killer: a deferral, bounded by
#: the attempt counter, not a permanent verdict on a valid document.
_CEILING_SIGNALS = {signal.SIGXCPU, signal.SIGXFSZ}
_RETRY_SIGNALS = {signal.SIGKILL}

#: Test-only ops, refused unless the parent set this variable in the CHILD's
#: environment (it is not in `_ENV_PREFIXES`, so production never passes it).
TEST_OPS_ENV = "APIFILES_WORKER_TEST_OPS"


# ================================================================ sandbox ==

def app_root() -> str:
    """The directory that holds the `app` package (/app in the container)."""
    here = os.path.dirname(os.path.abspath(__file__))  # …/app/apifiles
    return os.path.dirname(os.path.dirname(here))



#: PUBLIC_API_FILES_EXTRACT_SANDBOX values (module docstring).
SANDBOX_REQUIRED = "required"
SANDBOX_BEST_EFFORT = "best_effort"
SANDBOX_MODES = (SANDBOX_REQUIRED, SANDBOX_BEST_EFFORT)

_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_SET_SECUREBITS = 28
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_SECBIT_NOROOT_AND_LOCK = 0x3
_CAP_VERSION_3 = 0x20080522

_LANDLOCK_CREATE_RULESET = 444  # the same number on every architecture
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1

#: Rights a rule on a non-directory may carry (the kernel refuses the rest).
_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE
_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
#: Read-write for derived/, out_dir and the private temp dir. Deliberately
#: WITHOUT make_sym (a planted symlink in derived/ would be followed by a
#: later download), make_char/block/fifo/sock and refer (no link or rename
#: across directories, so nothing outside can be hard-linked in).
_WRITE = FS_READ_FILE | FS_READ_DIR | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_DIR | FS_MAKE_REG | FS_TRUNCATE

_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06

#: Per architecture: AUDIT_ARCH, seccomp(), capset(), and the refused calls.
_ARCH = {
    "aarch64": {
        "audit": 0xC00000B7,
        "seccomp": 277,
        "capset": 91,
        "x32": False,
        # socket, socketpair, io_uring_setup/enter/register, ptrace,
        # process_vm_readv/writev, bpf, userfaultfd, perf_event_open,
        # add_key, request_key, keyctl, unshare, setns
        "deny": (198, 199, 425, 426, 427, 117, 270, 271, 280, 282, 241, 217, 218, 219, 97, 268),
    },
    "x86_64": {
        "audit": 0xC000003E,
        "seccomp": 317,
        "capset": 126,
        "x32": True,
        "deny": (41, 53, 425, 426, 427, 101, 310, 311, 321, 323, 298, 248, 249, 250, 272, 308),
    },
}


class SandboxUnavailable(OSError):
    """A sandbox layer could not be applied on this kernel or runtime."""


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _fail(what: str) -> "SandboxUnavailable":
    return SandboxUnavailable(ctypes.get_errno(), what)


def drop_capabilities(libc) -> None:
    """Empty effective/permitted/inheritable/ambient capability sets. Locking
    SECBIT_NOROOT needs CAP_SETPCAP, so it is tried first and may be refused
    (an unprivileged parent has nothing to drop anyway)."""
    arch = _ARCH.get(os.uname().machine)
    if arch is None:
        raise SandboxUnavailable(0, "unknown architecture")
    libc.prctl(ctypes.c_int(_PR_SET_SECUREBITS), ctypes.c_ulong(_SECBIT_NOROOT_AND_LOCK), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    header = ctypes.create_string_buffer(struct.pack("=Ii", _CAP_VERSION_3, 0), 8)
    data = ctypes.create_string_buffer(bytes(24), 24)
    if libc.syscall(ctypes.c_long(arch["capset"]), header, data) != 0:
        raise _fail("capset")
    libc.prctl(ctypes.c_int(_PR_CAP_AMBIENT), ctypes.c_ulong(_PR_CAP_AMBIENT_CLEAR_ALL), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))


def set_no_new_privs(libc) -> None:
    if libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        raise _fail("no_new_privs")


def landlock_abi(libc=None) -> int:
    """The kernel's Landlock ABI version, 0 when Landlock is absent/disabled."""
    libc = libc or _libc()
    abi = libc.syscall(
        ctypes.c_long(_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION)
    )
    return int(abi) if abi > 0 else 0


def _fs_handled(abi: int) -> int:
    mask = (1 << 13) - 1  # ABI 1: execute … make_sym
    if abi >= 2:
        mask |= FS_REFER
    if abi >= 3:
        mask |= FS_TRUNCATE
    return mask


def _readable_roots() -> list:
    """Where the interpreter, its libraries and the app's code live."""
    roots = ["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc/cpuinfo", "/proc/meminfo", "/proc/stat",
             "/sys/devices/system/cpu", "/sys/fs/cgroup", "/dev/null", "/dev/urandom", "/dev/zero"]
    for prefix in {sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix}:
        if prefix:
            roots.append(prefix)
    roots.append(os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))))
    root = app_root()
    for entry in sys.path:
        # The app root itself is listable (the import system scans it) but
        # only its `app` package is readable: nothing else next to the code
        # is the child's business.
        if entry and os.path.isdir(entry) and os.path.realpath(entry) != os.path.realpath(root):
            roots.append(entry)
    roots.append(os.path.join(root, "app"))
    return roots


def _add_rule(libc, ruleset_fd: int, path: str, access: int, handled: int) -> bool:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return False  # not on this system: nothing to allow
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            access &= _FILE_RIGHTS
        access &= handled
        if not access:
            return False
        attr = ctypes.create_string_buffer(struct.pack("=Qi", access, fd), 12)
        if libc.syscall(ctypes.c_long(_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset_fd),
                        ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH), attr, ctypes.c_uint32(0)) != 0:
            raise _fail(f"landlock_add_rule {os.path.basename(path) or path}")
        return True
    finally:
        os.close(fd)


def apply_landlock(libc, *, writable: list, readable: list) -> int:
    """Restrict this process; returns the ABI applied (0 = not available)."""
    abi = landlock_abi(libc)
    if abi < 1:
        return 0
    handled = _fs_handled(abi)
    net = (_NET_BIND_TCP | _NET_CONNECT_TCP) if abi >= 4 else 0
    scoped = (_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if abi >= 6 else 0
    attr = ctypes.create_string_buffer(struct.pack("=QQQ", handled, net, scoped), 24)
    size = 24 if abi >= 6 else (16 if abi >= 4 else 8)
    ruleset_fd = libc.syscall(ctypes.c_long(_LANDLOCK_CREATE_RULESET), attr, ctypes.c_size_t(size), ctypes.c_uint32(0))
    if ruleset_fd < 0:
        raise _fail("landlock_create_ruleset")
    try:
        for path in readable:
            _add_rule(libc, int(ruleset_fd), path, _READ, handled)
        # /proc/self resolves to THIS process's directory: its own status,
        # maps and limits, never another process's.
        _add_rule(libc, int(ruleset_fd), f"/proc/{os.getpid()}", FS_READ_FILE | FS_READ_DIR, handled)
        root = app_root()
        _add_rule(libc, int(ruleset_fd), root, FS_READ_DIR, handled)
        for path in writable:
            if not _add_rule(libc, int(ruleset_fd), path, _WRITE, handled):
                raise SandboxUnavailable(0, "a writable directory of this job does not exist")
        if libc.syscall(ctypes.c_long(_LANDLOCK_RESTRICT_SELF), ctypes.c_int(int(ruleset_fd)), ctypes.c_uint32(0)) != 0:
            raise _fail("landlock_restrict_self")
    finally:
        os.close(int(ruleset_fd))
    return abi


def _seccomp_program(arch: dict) -> bytes:
    def op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
        return struct.pack("=HBBI", code, jt, jf, k & 0xFFFFFFFF)

    errno_eperm = _SECCOMP_RET_ERRNO | 1
    prog = [
        op(_BPF_LD_W_ABS, 4),  # seccomp_data.arch
        op(_BPF_JEQ_K, arch["audit"], jt=1, jf=0),
        op(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),
        op(_BPF_LD_W_ABS, 0),  # seccomp_data.nr
    ]
    if arch["x32"]:
        prog += [op(_BPF_JGE_K, 0x40000000, jt=0, jf=1), op(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS)]
    for number in arch["deny"]:
        prog += [op(_BPF_JEQ_K, number, jt=0, jf=1), op(_BPF_RET_K, errno_eperm)]
    prog.append(op(_BPF_RET_K, _SECCOMP_RET_ALLOW))
    return b"".join(prog)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def apply_seccomp(libc) -> None:
    arch = _ARCH.get(os.uname().machine)
    if arch is None:
        raise SandboxUnavailable(0, "unknown architecture")
    program = _seccomp_program(arch)
    buf = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.addressof(buf))
    if libc.syscall(ctypes.c_long(arch["seccomp"]), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                    ctypes.c_uint(_SECCOMP_FILTER_FLAG_TSYNC), ctypes.byref(fprog)) != 0:
        raise _fail("seccomp")


def _beneath(path: str, parent: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(parent)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def sandbox_paths(spec: "Spec") -> Dict[str, list]:
    """What this job may write (its derived dir, a render out_dir, its
    private temp dir) and read (the system roots plus its source)."""
    writable = [spec.derived_dir]
    out_dir = spec.args.get("out_dir") if isinstance(spec.args, dict) else None
    if out_dir and not _beneath(str(out_dir), spec.derived_dir):
        writable.append(str(out_dir))
    tmp_dir = spec.cap("tmp_dir")
    if tmp_dir:
        writable.append(str(tmp_dir))
    readable = _readable_roots()
    if spec.source:
        readable.append(spec.source)
    return {"writable": [w for w in writable if w], "readable": readable}


def apply_sandbox(spec: "Spec") -> Dict[str, Any]:
    """Every layer, in order; returns what is in force. In `required` mode a
    layer that cannot be applied raises SandboxUnavailable."""
    mode = str(spec.cap("sandbox", SANDBOX_REQUIRED))
    if mode not in SANDBOX_MODES:
        mode = SANDBOX_REQUIRED
    required = mode == SANDBOX_REQUIRED
    libc = _libc()
    state: Dict[str, Any] = {"sandbox_mode": mode}
    drop_capabilities(libc)  # never optional: it cannot fail for lack of kernel support
    set_no_new_privs(libc)
    paths = sandbox_paths(spec)
    try:
        state["landlock_abi"] = apply_landlock(libc, writable=paths["writable"], readable=paths["readable"])
    except SandboxUnavailable:
        if required:
            raise
        state["landlock_abi"] = 0
    if required and state["landlock_abi"] < 1:
        raise SandboxUnavailable(0, "landlock is not available")
    try:
        apply_seccomp(libc)
        state["seccomp"] = True
    except SandboxUnavailable:
        if required:
            raise
        state["seccomp"] = False
    return state


def _status_fields() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key in ("CapEff", "CapPrm", "CapInh", "CapAmb", "NoNewPrivs", "Seccomp"):
                    out[key] = value.strip()
    except OSError:
        pass
    return out


# ================================================================== child ==


def _apply_ceilings(spec: Spec) -> Dict[str, Any]:
    as_bytes = int(spec.cap("rlimit_as_bytes", 8 * 1024 * _MIB))
    cpu_s = spec.cap("cpu_s")
    fsize = int(spec.cap("fsize_bytes", FSIZE_FLOOR_BYTES))
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    if cpu_s:
        soft = max(1, int(cpu_s))
        resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + CPU_HARD_GRACE_S))
    resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE_LIMIT, NOFILE_LIMIT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    return {
        "as": list(resource.getrlimit(resource.RLIMIT_AS)),
        "cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
        "nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
        "fsize": list(resource.getrlimit(resource.RLIMIT_FSIZE)),
    }


def _dispatch(spec: Spec) -> Dict[str, Any]:
    op, kind = spec.op, spec.kind
    if op == "extract":
        if kind == "pdf":
            from .extractors import pdf

            return pdf.extract(spec)
        if kind == "document":
            from .extractors import office

            return office.extract_docx(spec)
        if kind == "presentation":
            from .extractors import office

            return office.extract_pptx(spec)
        if kind == "html":
            from .extractors import text

            return text.extract_html(spec)
        if kind == "text":
            from .extractors import text

            return text.extract_text(spec)
    if op == "sheets" and kind in ("spreadsheet", "tabular"):
        from .extractors import sheets

        return sheets.sheets(spec)
    if op == "profile" and kind in ("spreadsheet", "tabular"):
        from .extractors import sheets

        return sheets.profile(spec)
    if op == "decode" and kind == "image":
        from .extractors import image

        return image.decode(spec)
    if op == "variants" and kind == "image":
        from .extractors import image

        return image.variants(spec)
    if op == "render":
        from . import render

        return render.render_in_child(spec)
    raise ValueError("unknown op")


def _test_op(spec: Spec, ceilings: Dict[str, Any]) -> Dict[str, Any]:
    if os.environ.get(TEST_OPS_ENV) != "1":
        raise ValueError("unknown op")
    if spec.op == "test-limits":
        return {
            **_HARDENED,
            **ceilings,
            "status": _status_fields(),
            "env_keys": sorted(os.environ),
            "cwd": os.getcwd(),
        }
    if spec.op == "test-escape":
        return _test_escape(spec)
    if spec.op == "test-segfault":
        ctypes.string_at(0)  # SIGSEGV: the crash a hostile file would cause
    if spec.op == "test-sigkill":
        os.kill(os.getpid(), signal.SIGKILL)  # what the host OOM killer does
    if spec.op == "test-sleep":
        time.sleep(float(spec.args.get("seconds") or 60))
        return {}
    if spec.op == "test-allocate":
        blob = bytearray(int(spec.args.get("bytes") or 0))
        return {"allocated": len(blob)}
    if spec.op == "test-write":
        with open(os.path.join(spec.derived_dir, "big.bin"), "wb") as fh:
            fh.write(b"\0" * int(spec.args.get("bytes") or 0))
        return {}
    raise ValueError("unknown op")


def _attempt(fn) -> str:
    """"ok", or the errno name the kernel refused with."""
    import errno as errno_names

    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno_names.errorcode.get(exc.errno or 0, "OSError")


def _test_escape(spec: Spec) -> Dict[str, Any]:
    """What a parser exploit would try first, each attempt reported."""
    import socket

    args = spec.args
    derived = spec.derived_dir

    def read_parent_environ() -> None:
        with open(f"/proc/{os.getppid()}/environ", "rb") as fh:
            fh.read(1)

    def read_path(path: str):
        def go() -> None:
            with open(path, "rb") as fh:
                fh.read(1)
        return go

    def write_path(path: str):
        def go() -> None:
            with open(path, "wb") as fh:
                fh.write(b"x")
        return go

    def list_dir(path: str):
        return lambda: os.listdir(path)

    def open_socket(family: int):
        def go() -> None:
            socket.socket(family, socket.SOCK_STREAM).close()
        return go

    return {
        "parent_environ": _attempt(read_parent_environ),
        "read_outside": _attempt(read_path(str(args.get("outside_file") or "/nonexistent"))),
        "list_outside": _attempt(list_dir(str(args.get("outside_dir") or "/nonexistent"))),
        "write_outside": _attempt(write_path(os.path.join(str(args.get("outside_dir") or "/nonexistent"), "planted"))),
        "symlink_in_derived": _attempt(lambda: os.symlink("/etc/passwd", os.path.join(derived, "text.txt"))),
        "hardlink_into_derived": _attempt(
            lambda: os.link(str(args.get("outside_file") or "/nonexistent"), os.path.join(derived, "linked"))
        ),
        "fifo_in_derived": _attempt(lambda: os.mkfifo(os.path.join(derived, "fifo"))),
        "write_derived": _attempt(write_path(os.path.join(derived, "ok.bin"))),
        "write_tmp": _attempt(write_path(os.path.join(os.environ.get("TMPDIR") or "/nonexistent", "ok.bin"))),
        "inet_socket": _attempt(open_socket(socket.AF_INET)),
        "unix_socket": _attempt(open_socket(socket.AF_UNIX)),
        "signal_parent": _attempt(lambda: os.kill(os.getppid(), 0)),
        "read_own_status": _attempt(read_path("/proc/self/status")),
    }


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def child_main(argv: list) -> int:
    if len(argv) != 2:
        _emit({"ok": False, "code": "internal_error", "ceiling": ""})
        return EXIT_VERDICT
    try:
        spec = Spec.from_json(argv[1])
        if spec.op != argv[0]:
            raise ValueError("op mismatch")
    except Exception:  # noqa: BLE001
        _emit({"ok": False, "code": "internal_error", "ceiling": ""})
        return EXIT_VERDICT
    try:
        sandbox = apply_sandbox(spec)
    except Exception as exc:  # noqa: BLE001 — SandboxUnavailable, or a ctypes surprise
        # Not a verdict about the file: the host cannot confine a parser right
        # now. The parent defers (`processing_unavailable` after the attempts).
        sys.stderr.write(f"extraction sandbox unavailable: {exc}\n")
        _emit({"ok": False, "code": "processing_unavailable", "ceiling": "", "retry": True, "why": "unsandboxed"})
        return EXIT_VERDICT
    try:
        ceilings = _apply_ceilings(spec)
    except Exception:  # noqa: BLE001
        _emit({"ok": False, "code": "internal_error", "ceiling": ""})
        return EXIT_VERDICT
    ceilings = {**ceilings, **sandbox}
    try:
        if spec.op.startswith("test-"):
            facts = _test_op(spec, ceilings)
        else:
            facts = _dispatch(spec)
    except ExtractError as exc:
        _emit({"ok": False, **exc.to_json()})
        return EXIT_VERDICT
    except MemoryError:
        _emit({"ok": False, "code": "file_too_complex", "ceiling": "it needs more memory than the processing ceiling"})
        return EXIT_VERDICT
    except OSError as exc:
        import errno

        if exc.errno == errno.EFBIG:
            _emit({"ok": False, "code": "file_too_complex", "ceiling": "its extracted data is larger than the processing ceiling"})
        elif exc.errno in (errno.ENOSPC, errno.EDQUOT):
            _emit({"ok": False, "code": "processing_unavailable", "ceiling": "", "retry": True})
        else:
            _emit({"ok": False, "code": "internal_error", "ceiling": ""})
        return EXIT_VERDICT
    except Exception:  # noqa: BLE001 — an unexpected parser failure is our bug, logged by the parent
        import traceback

        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "code": "internal_error", "ceiling": ""})
        return EXIT_VERDICT
    _emit({"ok": True, "facts": facts})
    return 0


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))


# ================================================================= parent ==


class WorkerCrashed(FileCorrupt):
    """The child died on a signal that means the input broke the parser."""

    code = "file_corrupt"


class RetryableWorkerError(Exception):
    """The child could not write (disk full): not a verdict about the file."""


def sandbox_mode() -> str:
    """PUBLIC_API_FILES_EXTRACT_SANDBOX: `required` (default) or `best_effort`.
    Read like every Files setting: `settings` first, then the environment,
    blank meaning the default; an unknown value reads as `required`."""
    try:
        from ..config import settings

        value = getattr(settings, "public_api_files_extract_sandbox", None)
    except Exception:  # noqa: BLE001
        value = None
    if not (isinstance(value, str) and value.strip()):
        value = os.environ.get("PUBLIC_API_FILES_EXTRACT_SANDBOX") or ""
    value = value.strip().lower()
    return value if value in SANDBOX_MODES else SANDBOX_REQUIRED


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith(_ENV_PREFIXES)}
    root = app_root()
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Pillow / numpy / DuckDB threads: one extraction is one core's work, and
    # 20 thread arenas would spend address space the RLIMIT_AS is guarding.
    env.setdefault("OMP_NUM_THREADS", "1")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def fsize_limit(original_bytes: int) -> int:
    """RLIMIT_FSIZE for a blob: max(4 × its bytes, 256 MiB), ≤ 64 GiB."""
    return int(min(FSIZE_CEILING_BYTES, max(FSIZE_FLOOR_BYTES, 4 * max(0, int(original_bytes or 0)))))


def _stop(proc: "asyncio.subprocess.Process") -> "asyncio.Future[None]":
    from ..video.media import _stop_child

    return _stop_child(proc)


def _verdict_from_signal(returncode: int) -> ExtractError:
    number = -int(returncode)
    try:
        sig = signal.Signals(number)
    except ValueError:
        return ExtractError()
    if sig in _CORRUPT_SIGNALS:
        return WorkerCrashed()
    if sig in _RETRY_SIGNALS:
        raise RetryableWorkerError("the extraction child was killed from outside (host memory pressure)")
    if sig in _CEILING_SIGNALS:
        return FileTooComplex("it needs more time or memory than the processing ceiling")
    return ExtractError()


def _parse_stdout(raw: bytes) -> Optional[Dict[str, Any]]:
    lines = [line for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def run(
    spec: Spec,
    *,
    wall_s: Optional[float] = None,
    env_extra: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run one op in a child; the facts dict, or raise.

    Raises `ExtractError` (a verdict about the file), `RetryableWorkerError`
    (disk full: defer, do not fail), or propagates cancellation after the
    child is gone. `wall_s` None means no wall deadline (the CPU ceiling in
    the spec still applies)."""
    import dataclasses
    import shutil
    import tempfile

    # The child's only writable place outside derived/: created here, handed
    # to the sandbox as TMPDIR and HOME, removed when the child is gone.
    tmp_dir = tempfile.mkdtemp(prefix="apifiles-child-")
    spec = dataclasses.replace(spec, caps={**spec.caps, "tmp_dir": tmp_dir, "sandbox": spec.caps.get("sandbox") or sandbox_mode()})
    env = child_env(env_extra)
    env["TMPDIR"] = env["HOME"] = tmp_dir
    argv = [sys.executable, "-m", "app.apifiles.extract_worker", spec.op, spec.to_json()]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=app_root(),
            env=env,
            start_new_session=True,
        )
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    try:
        return await _communicate(proc, spec, wall_s, started)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _communicate(
    proc: "asyncio.subprocess.Process", spec: Spec, wall_s: Optional[float], started: float
) -> Dict[str, Any]:
    try:
        if wall_s is not None and wall_s > 0:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(wall_s))
        else:
            out, err = await proc.communicate()
    except asyncio.TimeoutError:
        await _stop(proc)
        log.warning("files extraction %s/%s passed its wall deadline of %.0fs", spec.kind, spec.op, float(wall_s or 0))
        raise FileTooComplex("it needs more time than the processing ceiling") from None
    except BaseException:
        # Cancelled: a DELETE, a lost lease or a shutdown. The child goes with
        # the job, or it keeps writing into a directory that is being purged.
        await _stop(proc)
        raise
    elapsed = time.monotonic() - started
    if len(out) > STDOUT_MAX_BYTES:
        out = out[-STDOUT_MAX_BYTES:]
    payload = _parse_stdout(out)
    tail = err[-STDERR_TAIL_BYTES:].decode("utf-8", "replace") if err else ""
    if proc.returncode is not None and proc.returncode < 0:
        log.warning(
            "files extraction %s/%s ended on signal %d after %.1fs", spec.kind, spec.op, -proc.returncode, elapsed
        )
        raise _verdict_from_signal(proc.returncode)
    if payload is None:
        log.error("files extraction %s/%s exited %s with no verdict: %s", spec.kind, spec.op, proc.returncode, tail[-500:])
        raise ExtractError()
    if payload.get("ok") is True and proc.returncode == 0:
        facts = payload.get("facts")
        return facts if isinstance(facts, dict) else {}
    if payload.get("retry"):
        if payload.get("why") == "unsandboxed":
            log.error("files extraction %s/%s refused to run unconfined: %s", spec.kind, spec.op, tail[-500:])
            raise RetryableWorkerError("the extraction sandbox could not be applied")
        raise RetryableWorkerError("the extraction child could not write its output")
    error = error_from_json(payload)
    if error.code == "internal_error" and tail:
        log.error("files extraction %s/%s failed: %s", spec.kind, spec.op, tail[-2000:])
    raise error


__all__ = [
    "CORE_LIMIT",
    "RetryableWorkerError",
    "SANDBOX_BEST_EFFORT",
    "SANDBOX_REQUIRED",
    "SandboxUnavailable",
    "apply_sandbox",
    "landlock_abi",
    "sandbox_mode",
    "WorkerCrashed",
    "app_root",
    "child_env",
    "fsize_limit",
    "harden_crash_dumps",
    "run",
]
