"""Physical resources of this process: open files and free disk (2026-09-13).

WHY THIS EXISTS. The no-timeout /v1 design (revision 2) removes every clock
that used to end a public request: generations run for hours, capacity waits
have no limit, a sync call holds its socket for as long as the answer takes.
Queues then grow without an error, so the only things that may still refuse
new public work are PHYSICAL — the ones whose exhaustion takes the whole
process down, chat included:

- FILE DESCRIPTORS. Measured 2026-09-13: this Python process ran with a soft
  RLIMIT_NOFILE of 1024 (hard 524288, /proc/<pid>/limits) because Python,
  unlike Node, never raises its own soft limit. Every held /v1 connection is a
  socket, and a flood of patient waiters would reach 1024 and fail accept(),
  the database pool and the engine client all at once. So the soft limit is
  raised to the hard one at start-up (`raise_nofile`), and above
  PUBLIC_API_FD_GUARD_RATIO (0.70) of it new /v1 requests are refused
  503 Retry-After 30 BEFORE headers (`fd_guard_tripped`, applied in
  main.py's body-size middleware). Chat routes and requests already admitted
  are never refused: the 30 % above the guard is theirs.
- FREE DISK. /data holds public audio, files, durable specs and blobs, and it
  is the device PostgreSQL's data directory lives on (reviewer df; operator
  check). `disk_free_bytes`/`disk_guard_tripped` are the shared statvfs read a
  launch guard refuses on below PUBLIC_API_MIN_FREE_DISK_BYTES (20 GiB).

COST. Counting lists /proc/self/fd, which is O(open fds): measured
2026-09-14 on this host, 27.6 ms at 50k descriptors, 74.9 ms at 150k and
143.6 ms at 300k — and the guard trips near 367k of a 524,288 limit. Done on
the event loop from the middleware (and uncached from /health), that stalled
every chat and public stream by ~170 ms each second exactly when the process
was busiest (T1 review). So a sample is cached for FD_SAMPLE_TTL_S (1 s) and,
on a running loop, a stale one is refreshed on ONE dedicated worker thread
while the last value keeps answering (`fd_pressure`); only the very first
sample, or a call with no running loop, counts inline. /health shows the
cached sample. The guard is therefore up to ~1 s plus one listing behind —
nothing for a 30 % margin.

Nothing here performs I/O at import time.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from typing import Optional, Tuple

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

#: How long one /proc/self/fd count is reused.
FD_SAMPLE_TTL_S = 1.0

#: Retry-After the fd guard sends (design: 503 Retry-After 30).
FD_RETRY_AFTER_S = 30

#: Retry-After the disk guard sends (design: 503 Retry-After 60).
DISK_RETRY_AFTER_S = 60

_sample: Optional[Tuple[float, int, int]] = None  # (monotonic, open, soft limit)

#: The one thread that lists /proc/self/fd off the loop, created on first use
#: (never at import), and whether a refresh is already running: a burst of
#: requests schedules one listing, not one each.
_sampler: Optional[concurrent.futures.ThreadPoolExecutor] = None
_sampler_lock = threading.Lock()
#: Monotonic start of the refresh in flight, or None. A refresh whose loop
#: closed before its callback ran would otherwise block every later one; past
#: _REFRESH_STUCK_S it no longer counts as in flight.
_refreshing: Optional[float] = None
_REFRESH_STUCK_S = 30.0


def nofile_limits() -> Tuple[int, int]:
    """(soft, hard) RLIMIT_NOFILE; (-1, -1) where the platform has none."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft), int(hard)
    except Exception:  # noqa: BLE001 — no resource module (not Linux)
        return -1, -1


def raise_nofile() -> Tuple[int, int]:
    """Raise the soft RLIMIT_NOFILE to the hard limit; return (soft, hard) as
    they are afterwards. Idempotent, never raises: a container that forbids it
    keeps its limit and the guard works against that limit instead.

    An unlimited hard limit (RLIM_INFINITY) is capped at the kernel's
    fs.nr_open, which is the largest value setrlimit accepts for NOFILE.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = hard
        if hard == resource.RLIM_INFINITY:
            try:
                with open("/proc/sys/fs/nr_open", "r", encoding="ascii") as fh:
                    target = int(fh.read().strip())
            except (OSError, ValueError):
                target = 1_048_576
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.info("raised RLIMIT_NOFILE soft limit %d -> %d (hard %s)", soft, target, hard)
    except Exception as exc:  # noqa: BLE001 — the guard still works on the old limit
        log.warning("could not raise RLIMIT_NOFILE: %s: %s", type(exc).__name__, exc)
    _reset_sample()
    return nofile_limits()


def _reset_sample() -> None:
    global _sample, _refreshing
    _sample = None
    _refreshing = None


def open_fds() -> int:
    """How many file descriptors this process holds; -1 when unknown."""
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            # The listing itself opens one descriptor: not counted.
            return max(0, len(os.listdir(path)) - 1)
        except OSError:
            continue
    return -1


def _take_sample(at: float) -> Tuple[float, int, int]:
    """Count now and store the sample (any thread: one tuple assignment)."""
    global _sample
    soft, _hard = nofile_limits()
    sample = (at, open_fds(), soft)
    _sample = sample
    return sample


def _publish(sample: Tuple[float, int, int]) -> float:
    ratio = _ratio(sample)
    metrics.set_gauge("orchestrator_open_fds_ratio", ratio,
                      "Open file descriptors as a share of the soft RLIMIT_NOFILE.")
    return ratio


def _refresh_off_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Schedule one listing on the sampler thread; the loop publishes it."""
    global _sampler, _refreshing
    started = time.monotonic()
    if _refreshing is not None and started - _refreshing < _REFRESH_STUCK_S:
        return
    with _sampler_lock:
        if _sampler is None:
            _sampler = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fd-sample")
    _refreshing = started

    def done(fut: "asyncio.Future") -> None:
        global _refreshing
        _refreshing = None
        if not fut.cancelled() and fut.exception() is None:
            _publish(fut.result())

    try:
        future = loop.run_in_executor(_sampler, lambda: _take_sample(time.monotonic()))
    except RuntimeError:  # the loop is closing
        _refreshing = None
        return
    future.add_done_callback(done)


def fd_pressure(now: Optional[float] = None) -> float:
    """Open descriptors as a share of the soft limit, 0.0 when either is
    unknown. Cached for FD_SAMPLE_TTL_S; publishes orchestrator_open_fds_ratio.
    On a running event loop a stale sample is refreshed off the loop and the
    last one answers meanwhile (module docstring, COST)."""
    at = time.monotonic() if now is None else float(now)
    cached = _sample
    if cached is not None and cached[0] <= at < cached[0] + FD_SAMPLE_TTL_S:
        return _ratio(cached)
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if cached is None or loop is None or now is not None:
        # No sample yet, no loop to protect, or a caller driving the clock.
        return _publish(_take_sample(at))
    _refresh_off_loop(loop)
    return _ratio(cached)


def _ratio(sample: Tuple[float, int, int]) -> float:
    _, used, soft = sample
    if used < 0 or soft <= 0:
        return 0.0
    return float(used) / float(soft)


def fd_guard_ratio() -> float:
    """PUBLIC_API_FD_GUARD_RATIO; 0 (or less) turns the guard off."""
    return float(getattr(settings, "public_api_fd_guard_ratio", 0.70) or 0.0)


def fd_guard_tripped(now: Optional[float] = None) -> bool:
    """True while new /v1 work must be refused for open-file pressure."""
    limit = fd_guard_ratio()
    return limit > 0 and fd_pressure(now) > limit


def disk_free_bytes(path: Optional[str] = None) -> Optional[int]:
    """Free bytes available to this process on the filesystem holding `path`
    (default /data); None when it cannot be read."""
    target = path or "/data"
    try:
        st = os.statvfs(target)
    except OSError:
        return None
    return int(st.f_bavail) * int(st.f_frsize)


def min_free_disk_bytes() -> int:
    """PUBLIC_API_MIN_FREE_DISK_BYTES (20 GiB), read at call time exactly as
    publicapi/disk_ledger.py reads it: a Settings attribute when one is set,
    else the environment with config.py's `_int` rule (blank = default)."""
    value = getattr(settings, "public_api_min_free_disk_bytes", None)
    if value is not None:
        return max(0, int(value))
    from .config import _int

    return max(0, int(_int("PUBLIC_API_MIN_FREE_DISK_BYTES", 20 * 1024 ** 3)))


def disk_guard_tripped(path: Optional[str] = None) -> bool:
    """True when free space under `path` is below PUBLIC_API_MIN_FREE_DISK_BYTES.
    An unreadable filesystem does not trip it (unknown is not full)."""
    floor = min_free_disk_bytes()
    free = disk_free_bytes(path)
    return floor > 0 and free is not None and free < floor


def describe() -> dict:
    """What /health shows: the limits and the last pressure reading — the
    cached sample, never a listing of its own on the loop (module docstring,
    COST)."""
    soft, hard = nofile_limits()
    pressure = fd_pressure()
    sample = _sample
    return {
        "nofile_soft": soft,
        "nofile_hard": hard,
        "open_fds": sample[1] if sample is not None else -1,
        "fd_pressure": round(pressure, 6),
        "fd_guard_ratio": fd_guard_ratio(),
    }
