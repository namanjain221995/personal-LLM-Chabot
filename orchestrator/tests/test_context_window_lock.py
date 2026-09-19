"""The model-window lock survives more than one event loop (PR #79 CI,
2026-09-19): two generations resumed at start-up contended a module-level
asyncio.Lock that an earlier loop had waited on, and the second raised
"is bound to a different event loop"."""
from __future__ import annotations

import asyncio

from app import context


def _contend_once(monkeypatch) -> None:
    context._window_cache.clear()

    async def slow_count(base_url, model, messages):
        await asyncio.sleep(0.05)
        return 1, 8192

    monkeypatch.setattr(context, "count_tokens", slow_count)

    async def two_first_calls():
        # Both miss the cache; the second must WAIT on the lock.
        return await asyncio.gather(
            context.model_window("http://w/v1", "m"),
            context.model_window("http://w/v1", "m"),
        )

    assert asyncio.run(two_first_calls()) == [8192, 8192]


def test_the_window_lock_is_contended_in_two_loops_without_error(monkeypatch):
    _contend_once(monkeypatch)
    _contend_once(monkeypatch)  # a new loop: raised RuntimeError before the fix
    context._window_cache.clear()
