"""The per-user search window counts SEARCHES, not turns (2026-09-29).

WHAT WAS MEASURED. main.py called `engines.search.rate_ok` for every
assistant text turn, before anything had decided to search, and `rate_ok`
records a slot. So from the 11th Fast message in a minute:
  * the Fast live lookup was silently switched off (the pre-pass got
    allow_network=False) for a person who had not searched once;
  * "Search rate limit reached — answering from model knowledge." went out
    at 2-4 ms and was counted as the turn's first visible text (16 such
    chat_first_visible{kind="status"} observations in the live process on
    the morning of 2026-09-29).

Pinned here, through the real /chat handler with every engine stubbed:
  * eleven Fast turns that never search spend no slot and say nothing, and
    the twelfth still may look up;
  * a turn whose pre-pass enters the live lookup takes exactly one slot, and
    a turn that does not takes none;
  * with the window full, a real search is still refused, with the line.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import living_knowledge, main
from app.config import settings
from app.engines import search as search_engine
from app.living_knowledge import Prepared
from tests.conftest import _materialize_test_user
from tests.test_fast_lane_route import _send, wired  # noqa: F401

_REFUSED = "Search rate limit reached — answering from model knowledge."


@pytest.fixture
def window(monkeypatch, wired):  # noqa: F811
    """An empty per-user search window of ten, and the viewer's key."""
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "search_rate_per_min", 10)
    monkeypatch.setattr(search_engine, "_rate", {})
    return str(_materialize_test_user("local")["id"])


def _statuses(events):
    return [d.get("text") for k, d in events if k == "status"]


def _spent(key: str) -> int:
    return len(search_engine._rate.get(key, []))


def test_eleven_fast_turns_that_never_search_spend_no_slot_and_the_twelfth_may_still_look_up(
    window, monkeypatch
):
    seen: list = []

    async def prepare(question, *, allow_network, emit=None, **kw):
        seen.append(allow_network)
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        for i in range(12):
            events, _meta = _send(
                client, f"rate-conv-{i}", [{"role": "user", "content": f"what is a balance sheet, part {i}?"}]
            )
            assert _REFUSED not in _statuses(events), (i + 1, _statuses(events))

    assert len(seen) == 12
    assert seen[-1] is True, "the 12th turn's pre-pass was denied the network"
    assert all(seen), seen
    assert _spent(window) == 0


def test_a_lookup_takes_exactly_one_slot_and_a_turn_without_one_takes_none(window, monkeypatch):
    mode = {"lookup": True}

    async def prepare(question, *, allow_network, emit=None, **kw):
        if mode["lookup"]:
            assert allow_network is True
            # The real pre-pass says so once; a second emit must not take a
            # second slot.
            await emit("status", {"text": "Checking recent sources…"})
            await emit("status", {"text": "Checking recent sources…"})
            return Prepared(decision="fast_lookup")
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        _send(client, "slot-conv-1", [{"role": "user", "content": "what is the latest vLLM release?"}])
        assert _spent(window) == 1, "a turn that entered the lookup takes exactly one slot"
        mode["lookup"] = False
        _send(client, "slot-conv-2", [{"role": "user", "content": "what is a balance sheet?"}])
        assert _spent(window) == 1, "a turn that did not look anything up took a slot"


def test_with_the_window_full_a_forced_search_is_refused_with_the_status(window, monkeypatch):
    ran: list = []

    async def run_search_engine(*args, **kwargs):
        ran.append(args)
        return "searched"

    monkeypatch.setattr(search_engine, "run_search_engine", run_search_engine)

    async def prepare(question, *, allow_network, emit=None, **kw):
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    for _ in range(10):
        search_engine.rate_take(window)

    with TestClient(main.app) as client:
        events, _meta = _send(
            client, "full-conv-1", [{"role": "user", "content": "search the web for today's gold price"}],
            web_search="on",
        )

    assert ran == [], "a search ran with the window full"
    assert _statuses(events).count(_REFUSED) == 1, _statuses(events)
    assert _spent(window) == 10


def test_with_the_window_full_a_fast_lookup_is_refused_with_the_status(window, monkeypatch):
    """The pre-pass needed the network for a time-sensitive question the
    store could not answer fresh (stale_offline), and only the full window
    closed it: the person is told, once, after the pre-pass."""
    seen: list = []

    async def prepare(question, *, allow_network, emit=None, **kw):
        seen.append(allow_network)
        return Prepared(decision="stale_offline" if not allow_network else "fast_lookup")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    for _ in range(10):
        search_engine.rate_take(window)

    with TestClient(main.app) as client:
        events, _meta = _send(client, "full-conv-2", [{"role": "user", "content": "what is the gold price today?"}])

    assert seen == [False]
    statuses = _statuses(events)
    assert statuses.count(_REFUSED) == 1, statuses
    kinds = [k for k, _ in events]
    assert kinds.index("status") < kinds.index("token")
    assert _spent(window) == 10
