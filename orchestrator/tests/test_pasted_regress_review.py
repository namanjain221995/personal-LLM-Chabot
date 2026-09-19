"""Reviewer regression probes (hotfix 1.2): ordinary questions keep web search."""
from __future__ import annotations

import asyncio

from app import llm
from app.engines import search as search_engine
from tests.test_pasted_web_privacy import (  # noqa: F401
    _copying_router, _no_stream, _stale_store, spy,
)

#: A person TYPING a detailed question over several lines (no paste). Its
#: last line is a request that does not start with one of _QUESTION's words.
TYPED = "\n".join([
    "I run a small ML lab and we are choosing hardware this quarter.",
    "Our budget is about 50,000 US dollars in total, including power work.",
    "We mostly fine-tune 7B and 13B models, with some 70B inference.",
    "The room is limited to about 3 kW of power and has no liquid cooling.",
    "I need the current street prices of the H100 PCIe and the RTX 6000 Blackwell in 2026.",
])


async def _emit(kind, data):
    return None


def test_short_question_still_searches_fast(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    asyncio.run(lk.prepare("who is the current ceo of nvidia?", effort="fast", mode="assistant",
                           web_search_pref="auto", allow_network=True))
    assert spy.queries == ["who is the current ceo of nvidia?"]


def test_typed_multiline_question_searches_with_pill_on(monkeypatch, spy):
    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)
    asyncio.run(search_engine.run_search_engine(TYPED, [], _emit, "think"))
    print("QUERIES", spy.queries)
    assert spy.queries, "the person switched web search on and typed a question"


def test_typed_multiline_question_fast_lookup(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    asyncio.run(lk.prepare(TYPED, effort="fast", mode="assistant", web_search_pref="auto", allow_network=True))
    print("QUERIES", spy.queries)
    assert spy.queries
