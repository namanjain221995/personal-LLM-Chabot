"""A process pool for pure-CPU work the serving loop's GIL cannot afford.

WHY (server performance track, 2026-09-15). The orchestrator is ONE process
with ONE event loop, and every Python-level CPU cycle a request spends -- in a
worker thread or not -- holds the same GIL the loop needs to write SSE tokens.
Moving text windowing into threads (web_memory ``_run_cpu``) freed the loop
from the call stack but not from the GIL: at 8 concurrent Fast turns the loop
still lagged 79 ms at p99. Worker PROCESSES are the only way this service uses
more than one core for Python work without ``uvicorn --workers`` (rejected:
admission, SSE resume, the generation registry and the health single-flight
are process-local). The prototype measured c=8 loop lag p99 79.4 -> 7.7 ms.

This module is a HELPER. Adopting it at a call site is the owning track's
change (web_memory / web_index belong to the database release).

DESIGN RULES (each has a test in tests/test_server_perf.py)

1. ``forkserver``, never ``fork``. The parent carries uvloop, anyio worker
   threads, a psycopg pool and LanceDB threads; a forked child inherits locks
   held by threads that do not exist in it. A forkserver child is a fresh
   interpreter.
2. Children run only functions from PURE modules (``PURE_MODULES``): no import
   of app.config, app.db, lancedb, duckdb or openai, no module-level
   Settings(). They never see a psycopg connection, a LanceDB table or a
   DuckDB cursor: arguments are pickled, so pass strings and ints only.
   ``run_cpu`` refuses a function from any other module, in-thread mode
   included, so a violation fails in CI (where the pool is off) too.
3. Slots are released by the concurrent future's DONE callback, not by the
   awaiting coroutine. A cancelled chat turn cancels the awaiting task, but a
   child already running keeps running; releasing on cancel would admit new
   work onto busy children. ``in_flight`` therefore never exceeds ``slots``.
4. No per-call kill: a ProcessPoolExecutor cannot kill one task. Callers bound
   input size. ``max_tasks_per_child`` recycles children to cap leaks.
5. BrokenProcessPool (a child OOM-killed or SIGKILLed): the affected calls
   re-run in a thread (pure functions, so a re-run is safe), the pool is
   rebuilt single-flight at most once per ``REBUILD_MIN_INTERVAL_S``, and
   ``cpu_pool_broken_total`` counts it.
6. ``CPU_POOL_WORKERS=0`` (the default, and what tests and CI use) runs the
   same functions in anyio worker threads under the same slot count.
7. ``stop()`` cancels queued work and joins children within a bound, inside
   uvicorn's 90 s graceful window. compose runs the orchestrator under tini
   (``init: true``), which reaps anything left.

Stdlib-only at import (plus anyio lazily), so this module is itself pure.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import multiprocessing
import os
import threading
import time
import weakref
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, Dict, Optional, Set

log = logging.getLogger(__name__)

#: Modules whose functions may run in a child. Each must import nothing heavy
#: and no process-bound handle (see rule 2). Extend with ``register_pure_module``
#: only for a module with an import-purity test.
PURE_MODULES: Set[str] = {"app.core.cpu_pool", "app.core.textwindow"}

#: Modules a child must never have imported (the purity test asserts this).
FORBIDDEN_IN_CHILD = ("app.config", "app.db", "lancedb", "duckdb", "openai")

MAX_TASKS_PER_CHILD = 200
REBUILD_MIN_INTERVAL_S = 60.0
STOP_JOIN_TIMEOUT_S = 5.0

_BROKEN_HELP = "cpu_pool: times the worker process pool broke (a child died) and was rebuilt or bypassed."


def register_pure_module(name: str) -> None:
    PURE_MODULES.add(name)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _check_pure(fn: Callable[..., Any]) -> None:
    target = fn
    while isinstance(target, functools.partial):
        target = target.func
    module = getattr(target, "__module__", None)
    if module not in PURE_MODULES:
        raise ValueError(
            f"cpu_pool refuses {getattr(target, '__qualname__', target)!r} from module "
            f"{module!r}: only functions from PURE_MODULES may run in a worker process"
        )


# ---------------------------------------------------------------------------
# Functions the tests (and the purity check) run inside a child.
# ---------------------------------------------------------------------------


def child_modules() -> list:
    """sys.modules keys as the child sees them (purity test)."""
    import sys

    return sorted(sys.modules)


def child_pid() -> int:
    return os.getpid()


def burn(seconds: float) -> int:
    """Hold a core (and the child's GIL) for ``seconds``; returns the pid."""
    end = time.perf_counter() + float(seconds)
    x = 0
    while time.perf_counter() < end:
        x += 1
    return os.getpid()


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


class CpuPool:
    """One per process. ``workers == 0`` means in-thread mode."""

    def __init__(self, workers: int, slots: Optional[int] = None, *,
                 max_tasks_per_child: int = MAX_TASKS_PER_CHILD,
                 preload: tuple = ()) -> None:
        self.workers = max(0, int(workers))
        self.slots = max(1, int(slots if slots is not None else (self.workers or 1)))
        self.max_tasks_per_child = max(1, int(max_tasks_per_child))
        self.preload = tuple(preload)
        self._executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
        self._limiters: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._rebuild_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._last_rebuild = 0.0
        self._counts_lock = threading.Lock()
        self.in_flight = 0   # submitted to a child, not yet done
        self.waiting = 0     # awaiting a slot
        self.broken_total = 0
        self.fallback_total = 0
        self.stopped = False

    # -- executor lifecycle ------------------------------------------------
    def _new_executor(self) -> concurrent.futures.ProcessPoolExecutor:
        ctx = multiprocessing.get_context("forkserver")
        # EVERY forkserver child imports the parent's __main__ as __mp_main__
        # (multiprocessing spawn preparation). uvicorn's console script, pytest
        # and the harness guard on `__name__ == "__main__"`; a main module that
        # does work at import would run it once per child.
        if self.preload:
            ctx.set_forkserver_preload(list(self.preload))
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=ctx,
            max_tasks_per_child=self.max_tasks_per_child,
        )

    def executor(self) -> Optional[concurrent.futures.ProcessPoolExecutor]:
        if self.workers == 0 or self.stopped:
            return None
        with self._executor_lock:
            if self._executor is None:
                self._executor = self._new_executor()
            return self._executor

    def prestart(self) -> int:
        """Spawn the children NOW, from a worker thread (the lifespan calls it
        through ``asyncio.to_thread``). Without it the first ``submit`` starts
        the forkserver and forks each child on the event loop: measured
        2026-09-15 at 50 ms for the first call and a 16 ms loop stall. Returns
        the number of distinct child pids seen (best effort, bounded)."""
        executor = self.executor()
        if executor is None:
            return 0
        futures = [executor.submit(child_pid) for _ in range(self.workers)]
        done, _ = concurrent.futures.wait(futures, timeout=30)
        pids = set()
        for f in done:
            try:
                pids.add(f.result())
            except Exception:  # noqa: BLE001 — a broken pool is handled on first real use
                pass
        return len(pids)

    def _semaphore(self, loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
        sem = self._sems.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self.slots)
            self._sems[loop] = sem
        return sem

    # -- running work -------------------------------------------------------
    async def _in_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        import anyio

        loop = asyncio.get_running_loop()
        limiter = self._limiters.get(loop)
        if limiter is None:
            limiter = anyio.CapacityLimiter(self.slots)
            self._limiters[loop] = limiter
        return await anyio.to_thread.run_sync(functools.partial(fn, *args), limiter=limiter)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        _check_pure(fn)
        executor = self.executor()
        if executor is None:
            return await self._in_thread(fn, *args)
        loop = asyncio.get_running_loop()
        sem = self._semaphore(loop)
        with self._counts_lock:
            self.waiting += 1
        try:
            await sem.acquire()
        finally:
            with self._counts_lock:
                self.waiting -= 1
        # From here the slot belongs to the concurrent future once submitted.
        try:
            future = executor.submit(fn, *args)
        except BaseException as exc:  # noqa: BLE001 — incl. BrokenProcessPool / RuntimeError after shutdown
            sem.release()
            if isinstance(exc, (BrokenProcessPool, RuntimeError)):
                await self._note_broken(executor, exc)
                return await self._fallback(fn, *args)
            raise
        with self._counts_lock:
            self.in_flight += 1

        def _done(_f: concurrent.futures.Future, _loop=loop, _sem=sem) -> None:
            with self._counts_lock:
                self.in_flight -= 1
            try:
                _loop.call_soon_threadsafe(_sem.release)
            except RuntimeError:  # the loop is closed: nobody left to admit
                pass

        future.add_done_callback(_done)
        try:
            return await asyncio.wrap_future(future)
        except BrokenProcessPool as exc:
            await self._note_broken(executor, exc)
            return await self._fallback(fn, *args)
        except asyncio.CancelledError:
            # Two very different cancellations arrive here. (a) The awaiting
            # task was cancelled (client disconnect, deadline): re-raise.
            # (b) Nobody cancelled the task: stop() or a rebuild shut the
            # executor down with cancel_futures=True while this call was still
            # queued. Propagating that would end an innocent task as
            # "cancelled" (verified 2026-09-15: 6 of 8 queued callers ended
            # cancelled after stop()). The function is pure, so re-run it in a
            # thread instead.
            task = asyncio.current_task()
            if future.cancelled() and task is not None and not task.cancelling():
                return await self._fallback(fn, *args)
            raise

    async def _fallback(self, fn: Callable[..., Any], *args: Any) -> Any:
        with self._counts_lock:
            self.fallback_total += 1
        return await self._in_thread(fn, *args)

    async def _note_broken(self, executor, exc: BaseException) -> None:
        """Single-flight: the first caller to see a broken executor replaces it,
        at most once per REBUILD_MIN_INTERVAL_S; later callers see it replaced."""
        if self.stopped:
            return
        loop = asyncio.get_running_loop()
        lock = self._rebuild_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._rebuild_locks[loop] = lock
        async with lock:
            with self._executor_lock:
                if self._executor is not executor:
                    return  # already rebuilt (or bypassed) by another caller
                with self._counts_lock:
                    self.broken_total += 1
                try:
                    from .. import metrics

                    metrics.inc("cpu_pool_broken_total", _BROKEN_HELP)
                except Exception:  # noqa: BLE001
                    pass
                log.error("cpu_pool: worker pool broke (%s)", type(exc).__name__)
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:  # noqa: BLE001
                    pass
                now = time.monotonic()
                if now - self._last_rebuild >= REBUILD_MIN_INTERVAL_S:
                    self._last_rebuild = now
                    self._executor = self._new_executor()
                else:
                    # Rebuilt too recently: stay in-thread until the next start().
                    log.error("cpu_pool: second break within %.0f s; staying in-thread", REBUILD_MIN_INTERVAL_S)
                    self._executor = None
                    self.workers = 0

    # -- shutdown -------------------------------------------------------------
    async def stop(self, timeout: float = STOP_JOIN_TIMEOUT_S) -> None:
        self.stopped = True
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is None:
            return
        # The child handles are private to ProcessPoolExecutor; read them
        # defensively, before shutdown drops them. They are the only way to
        # end a child pinned by a pathological input: the executor waits for
        # running work before it sends any child its exit sentinel.
        processes = list((getattr(executor, "_processes", None) or {}).values())
        # ONE shutdown call that waits: a first shutdown(wait=False) clears the
        # manager thread, and a second shutdown(wait=True) then returns at once
        # without joining anything (CPython 3.11/3.12).
        waiter = asyncio.ensure_future(
            asyncio.to_thread(functools.partial(executor.shutdown, wait=True, cancel_futures=True))
        )
        # asyncio.timeout, not asyncio.wait_for: on Python 3.11 (CI, the CPU
        # image) wait_for swallows a cancellation that lands in the same loop
        # pass as the inner result (the PR #65 CI hang). The shield keeps the
        # shutdown thread's future alive past the first bound.
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(waiter)
        except TimeoutError:
            for proc in processes:
                try:
                    if proc.is_alive():
                        proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:  # the manager thread exits once it sees the children gone
                async with asyncio.timeout(timeout):
                    await asyncio.shield(waiter)
            except TimeoutError:
                log.error("cpu_pool: executor did not shut down within %.1f s after kill", timeout)
            except Exception:  # noqa: BLE001
                log.exception("cpu_pool: executor shutdown failed after kill")
        except Exception:  # noqa: BLE001
            log.exception("cpu_pool: executor shutdown failed")

    def publish_gauges(self) -> None:
        try:
            from .. import metrics

            metrics.set_gauge("cpu_pool_workers", self.workers, "cpu_pool: worker processes (0 = in-thread).")
            metrics.set_gauge("cpu_pool_slots", self.slots, "cpu_pool: concurrent calls admitted.")
            metrics.set_gauge("cpu_pool_in_flight", self.in_flight, "cpu_pool: calls running in a child.")
            metrics.set_gauge("cpu_pool_waiting", self.waiting, "cpu_pool: calls waiting for a slot.")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The process-wide instance
# ---------------------------------------------------------------------------

_POOL: Optional[CpuPool] = None
_collector_registered = False


def configured_workers() -> int:
    return max(0, _env_int("CPU_POOL_WORKERS", 0))


def configured_slots(workers: int) -> int:
    return max(1, _env_int("CPU_POOL_SLOTS", workers or 1))


def get_pool() -> CpuPool:
    """The process pool, created in in-thread mode if ``start()`` never ran."""
    global _POOL
    if _POOL is None:
        _POOL = CpuPool(0, 1)
    return _POOL


def start(workers: Optional[int] = None, slots: Optional[int] = None) -> CpuPool:
    """Called by the lifespan. Children start lazily on the first submit, so an
    unused pool costs no processes."""
    global _POOL, _collector_registered
    w = configured_workers() if workers is None else max(0, int(workers))
    s = configured_slots(w) if slots is None else max(1, int(slots))
    _POOL = CpuPool(w, s)
    if not _collector_registered:
        try:
            from .. import metrics

            metrics.register_collector(lambda: _POOL.publish_gauges() if _POOL else None)
            _collector_registered = True
        except Exception:  # noqa: BLE001
            pass
    log.info("cpu_pool: workers=%d slots=%d", _POOL.workers, _POOL.slots)
    return _POOL


async def stop() -> None:
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        await pool.stop()


async def run_cpu(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``fn(*args)`` on the process pool (or in a thread when it is off)."""
    return await get_pool().run(fn, *args)


def stats() -> Dict[str, int]:
    pool = get_pool()
    return {"workers": pool.workers, "slots": pool.slots, "in_flight": pool.in_flight,
            "waiting": pool.waiting, "broken_total": pool.broken_total,
            "fallback_total": pool.fallback_total}
