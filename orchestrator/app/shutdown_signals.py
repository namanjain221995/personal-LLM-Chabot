"""Hear SIGTERM the moment it arrives, without taking it from uvicorn (2026-09-13).

WHY THIS EXISTS. A deploy recreates the orchestrator with SIGTERM. uvicorn's
handler sets `should_exit`, stops accepting connections within ~0.25 s, and
then waits up to --timeout-graceful-shutdown (90 s) for open responses before
the lifespan's `finally` runs. That grace is right for chat: a chat answer
dies with its process, and 90 s lets most of them finish. It is wrong for
durable /v1 runs (no-timeout design, revision 2): they can resume in the next
process, so every second they keep generating here is a second of work the
next process re-prefills — and a /v1 stream held open until SIGKILL delays the
gateway's re-attach by the whole grace.

So the durable subsystem must hear SIGTERM at +0 s, not at +90 s. This module
CHAINS the signal: the new handler schedules the callback on the event loop
(`loop.call_soon_threadsafe` — a signal handler runs between bytecodes on the
main thread and must not touch asyncio state directly) and then calls the
handler that was installed before it, so uvicorn's own shutdown is unchanged.

IDEMPOTENT. `install` twice (a test client that runs the lifespan twice, a
reload) chains once: the handler recognises itself and only swaps the
callback. `uninstall` restores the previous handler if ours is still the one
installed.

WHAT IT DOES NOT DO: chat is untouched (declined sub-fix (a) of the design:
aborting chat SSE on SIGTERM would kill answers that today finish inside the
grace). Only the callback — `durable.request_suspend` in main.py — decides
what a signal means.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

HANDLED = (signal.SIGTERM, signal.SIGINT)


class _Chained:
    """The installed handler: schedule the callback, then defer to the
    previous handler exactly as if we were not there."""

    def __init__(self, sig: int, previous: Any, callback: Callable[[str], Any],
                 loop: asyncio.AbstractEventLoop) -> None:
        self.sig = sig
        self.previous = previous
        self.callback = callback
        self.loop = loop
        self.fired = 0

    def __call__(self, signum: int, frame: Any) -> None:
        self.fired += 1
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        try:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(_run_callback, self.callback, name)
        except Exception:  # noqa: BLE001 — a signal handler must never raise
            pass
        prev = self.previous
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:
            # Nothing was installed: behave like the default for this signal.
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)
        # SIG_IGN or None: ignored, as before.


def _run_callback(callback: Callable[[str], Any], name: str) -> None:
    """On the loop: call the callback; schedule it when it is a coroutine."""
    try:
        result = callback(name)
    except Exception:  # noqa: BLE001
        log.exception("shutdown signal callback raised")
        return
    if asyncio.iscoroutine(result):
        task = asyncio.ensure_future(result)
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


_PENDING: "set[asyncio.Future]" = set()
_installed: Dict[int, _Chained] = {}


def install(callback: Callable[[str], Any], loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Chain `callback(signal_name)` in front of the current SIGTERM and
    SIGINT handlers. Returns False (and does nothing) off the main thread,
    where Python cannot install signal handlers. Idempotent."""
    if threading.current_thread() is not threading.main_thread():
        log.info("shutdown signals not chained: not on the main thread")
        return False
    target = loop or asyncio.get_running_loop()
    for sig in HANDLED:
        current = signal.getsignal(sig)
        if isinstance(current, _Chained):
            current.callback = callback
            current.loop = target
            _installed[sig] = current
            continue
        handler = _Chained(sig, current, callback, target)
        signal.signal(sig, handler)
        _installed[sig] = handler
    log.info("shutdown signals chained: SIGTERM/SIGINT reach the durable subsystem at once")
    return True


def uninstall() -> None:
    """Restore the previous handlers where ours is still the installed one."""
    if threading.current_thread() is not threading.main_thread():
        _installed.clear()
        return
    for sig, handler in list(_installed.items()):
        if signal.getsignal(sig) is handler:
            signal.signal(sig, handler.previous if handler.previous is not None else signal.SIG_DFL)
    _installed.clear()


def installed() -> bool:
    return bool(_installed)
