"""In-conversation recall under a failing or busy embedding sidecar.

2026-09-13: recall.retrieve_block moved from `llm.embed_texts` (a 90 s batch
timeout plus the sidecar recovery window) to the cached, bounded
`llm.embed_query` (EMBED_WAIT_S for a slot, EMBED_TIMEOUT_S for the call). The
latency win stays; what the prover flagged is that a burst or a sidecar blip
now made the recall block — the folded turns the summary dropped — vanish from
the prompt without a trace. These tests pin the repair:

* a drop is counted as recall_block_dropped_total{reason=embed_busy|
  embed_timeout|embed_error} and logged at INFO with the conversation's hash,
  never its id;
* a failure that leaves budget is retried ONCE, and the retry can never push
  the recall read past EMBED_WAIT_S (1 s by default) from its first attempt,
  so time to first token stays inside a second.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app import db, llm, metrics, recall
from app.config import settings

CONV = "conv-recall-budget"


@pytest.fixture()
def folded(monkeypatch):
    uid = db.create_user("recall-budget", "hash")
    db.create_conversation(uid, CONV, "chat")
    monkeypatch.setattr(llm, "embed_texts", _vectors)
    asyncio.run(recall.index_folded(CONV, [{"role": "user", "content": "the badger protocol uses port 8443"}], 0))
    llm.embed_cache_clear()
    metrics.reset()
    return uid


async def _vectors(texts, **_kwargs):
    return [[1.0, 0.0, 0.0] for _ in texts]


def _dropped() -> dict:
    return {dict(key)["reason"]: value for key, value in (metrics._counters.get("recall_block_dropped_total") or {}).items()}


def _sidecar(monkeypatch, *outcomes):
    """embed_texts that plays `outcomes` in order: an exception class to
    raise, a float to sleep before answering, or "ok"."""
    calls: list = []

    async def embed(texts, **kwargs):
        step = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(kwargs)
        if isinstance(step, float):
            await asyncio.sleep(step)
        elif step != "ok":
            raise step("sidecar says no")
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    return calls


class APITimeoutError(Exception):
    """Named like the OpenAI client's timeout, which is what a slow sidecar raises."""


def test_a_fast_transient_embedding_failure_is_retried_once_and_the_recall_block_survives(folded, monkeypatch):
    calls = _sidecar(monkeypatch, ConnectionResetError, "ok")
    block = asyncio.run(recall.retrieve_block(CONV, "badger", effort="fast"))
    assert block is not None and "badger protocol" in block
    assert len(calls) == 2
    assert _dropped() == {}


def test_a_dropped_recall_block_is_counted_and_logged_by_conversation_hash_inside_one_second(
    folded, monkeypatch, caplog
):
    assert settings.embed_wait_s <= 1.0, "the retry budget is EMBED_WAIT_S; the 1 s TTFT goal needs it <= 1 s"
    calls = _sidecar(monkeypatch, RuntimeError)
    caplog.set_level(logging.INFO, logger="app.recall")
    started = time.perf_counter()
    assert asyncio.run(recall.retrieve_block(CONV, "badger", effort="fast")) is None
    elapsed = time.perf_counter() - started
    assert len(calls) == 2, "a failure is retried exactly once"
    assert _dropped() == {"embed_error": 1.0}
    assert elapsed < 1.0, f"recall took {elapsed:.2f}s"
    records = [r for r in caplog.records if r.name == "app.recall" and "recall block dropped" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert recall._conversation_ref(CONV) in message
    assert CONV not in message, "the log must carry the conversation's hash, never its id"
    assert "reason=embed_error" in message and "retried=True" in message


def test_a_sidecar_timeout_is_counted_as_embed_timeout(folded, monkeypatch):
    calls = _sidecar(monkeypatch, APITimeoutError)
    assert asyncio.run(recall.retrieve_block(CONV, "badger", effort="fast")) is None
    assert len(calls) == 2
    assert _dropped() == {"embed_timeout": 1.0}


def test_a_busy_sidecar_drops_the_block_as_embed_busy_without_a_retry_past_the_budget(folded, monkeypatch):
    monkeypatch.setattr(settings, "embed_wait_s", 0.2)
    monkeypatch.setattr(settings, "embed_max_inflight", 1)
    calls = _sidecar(monkeypatch, "ok")

    async def scenario():
        slots = llm._embed_semaphore()
        await slots.acquire()  # every slot taken by someone else's burst
        try:
            started = time.perf_counter()
            block = await recall.retrieve_block(CONV, "badger", effort="fast")
            return block, time.perf_counter() - started
        finally:
            slots.release()

    block, elapsed = asyncio.run(scenario())
    assert block is None
    assert calls == [], "the sidecar was never reached"
    assert _dropped() == {"embed_busy": 1.0}
    assert elapsed < 0.2 + 0.1, f"a busy wait of 0.2 s grew to {elapsed:.2f}s"


def test_a_retry_that_hangs_is_cut_off_when_the_budget_runs_out(folded, monkeypatch):
    monkeypatch.setattr(settings, "embed_wait_s", 0.3)
    calls = _sidecar(monkeypatch, RuntimeError, 5.0)
    started = time.perf_counter()
    assert asyncio.run(recall.retrieve_block(CONV, "badger", effort="fast")) is None
    elapsed = time.perf_counter() - started
    assert len(calls) == 2
    assert elapsed < 0.45, f"the retry ran {elapsed:.2f}s past a 0.3 s budget"
    assert _dropped() == {"embed_timeout": 1.0}


def test_no_retry_is_attempted_when_the_first_failure_already_spent_the_budget(folded, monkeypatch):
    monkeypatch.setattr(settings, "embed_wait_s", 0.2)
    # A first attempt that fails only after the whole budget (a slow reset).
    calls: list = []

    async def slow_then_fail(texts, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.25)
        raise RuntimeError("reset after a while")

    monkeypatch.setattr(llm, "embed_texts", slow_then_fail)
    assert asyncio.run(recall.retrieve_block(CONV, "badger", effort="fast")) is None
    assert len(calls) == 1
    assert _dropped() == {"embed_error": 1.0}


def test_embed_unavailable_says_why_it_happened(monkeypatch):
    async def timeout(texts, **kwargs):
        raise APITimeoutError("read timed out")

    async def broken(texts, **kwargs):
        raise RuntimeError("500")

    async def empty(texts, **kwargs):
        return [[]]

    for fake, reason in ((timeout, "timeout"), (broken, "error"), (empty, "error")):
        llm.embed_cache_clear()
        monkeypatch.setattr(llm, "embed_texts", fake)
        with pytest.raises(llm.EmbedUnavailable) as caught:
            asyncio.run(llm.embed_query("what port does badger use"))
        assert caught.value.reason == reason, fake.__name__
    with pytest.raises(llm.EmbedUnavailable) as caught:
        asyncio.run(llm.embed_query("   "))
    assert caught.value.reason == "empty"


# ── which call, at which effort (second prover pass, 2026-09-13) ─────────────


@pytest.mark.parametrize("effort", ["think", "max", ""])
def test_think_and_max_recall_keep_the_pre_change_embedding_call_recovery_included(folded, monkeypatch, effort):
    """The bounded path drops the block during a sidecar restart; HEAD waited
    through the recovery window. Only Fast is on the one-second clock."""
    calls: list = []

    async def embed(texts, **kwargs):
        calls.append((list(texts), kwargs))
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    question = "what did we say about the\n  badger   protocol?"
    block = asyncio.run(recall.retrieve_block(CONV, question, effort=effort))
    assert block is not None and "badger protocol" in block
    # Exactly HEAD's call: the raw question, no timeout, the default (batch)
    # kind, which is the one resilient() gives the sidecar recovery window.
    assert calls == [([question], {})]


def test_fast_recall_embeds_the_question_exactly_as_typed(folded, monkeypatch):
    """The vector must be the one HEAD computed, or folded-chunk rankings move
    for a question with a newline in it."""
    calls = _sidecar(monkeypatch, "ok")
    seen: list = []
    real = llm.embed_texts

    async def watch(texts, **kwargs):
        seen.append(list(texts))
        return await real(texts, **kwargs)

    monkeypatch.setattr(llm, "embed_texts", watch)
    question = "what did we say about the\n  badger   protocol?"
    assert asyncio.run(recall.retrieve_block(CONV, question, effort="fast")) is not None
    assert seen == [[question]]
    assert calls and calls[0].get("kind") == "query"
