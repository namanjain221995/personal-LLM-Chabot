"""Measurement truth for the first-token programme (2026-09-29).

Two defects in what the live histograms said:

  * knowledge_prepare_seconds is timed from the pre-pass's DISPATCH. The task
    overlaps memory recall, the context reads and compaction, so on a turn
    whose pre-pass had finished before /chat reached the await it recorded
    time nobody waited for. knowledge_blocked_seconds is timed from the start
    of the await to its return: the pre-pass's real share of the first token.
  * there was no per-class first-answer number. chat_first_visible_seconds
    counts a status line as "visible" (a 3 ms rate-limit line counted as a
    fast turn) and chat_ttft_seconds has no class. chat_first_answer_seconds
    is observed once, at the first ANSWER token, labelled with the knowledge
    decision and the prompt's shape (lane, paste, followup, plain).

Through the real /chat handler with every engine stubbed.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import living_knowledge, main, memory_semantic, metrics
from app.living_knowledge import Prepared
from tests.test_fast_lane_route import _ANSWERED, _send, wired  # noqa: F401

_PASTE = "\n".join(
    f"Clause {i}. The licensee shall keep the records described in schedule {i} for seven years."
    for i in range(1, 40)
) + "\n\nwhat does clause 3 mean?"


def _series(name: str) -> dict:
    return {dict(key).get("shape", "") + "|" + dict(key).get("decision", ""): value
            for key, value in metrics._hists.get(name, {}).items()}


def _count(name: str) -> int:
    return sum(n for _counts, _total, n in metrics._hists.get(name, {}).values())


def test_the_first_answer_is_observed_once_per_turn_with_its_decision_and_shape(wired, monkeypatch):  # noqa: F811
    async def prepare(question, *, allow_network, emit=None, **kw):
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    metrics.reset()

    with TestClient(main.app) as client:
        _send(client, "shape-plain", [{"role": "user", "content": "what is a balance sheet?"}])
        assert _count("chat_first_answer_seconds") == 1, "observed more or less than once"
        _send(client, "shape-follow", [*_ANSWERED, {"role": "user", "content": "and on a Mac?"}])
        _send(client, "shape-lane", [*_ANSWERED, {"role": "user", "content": "hi ??"}])
        _send(client, "shape-paste", [{"role": "user", "content": _PASTE}])

    series = _series("chat_first_answer_seconds")
    assert set(series) == {
        "plain|static_model",
        "followup|static_model",
        "lane|small_talk_lane",
        "paste|static_model",
    }, series
    assert all(n == 1 for _counts, _total, n in series.values())
    (key,) = [k for k in metrics._hists["chat_first_answer_seconds"] if dict(k)["shape"] == "plain"]
    assert dict(key)["effort"] == "fast"
    text = metrics.render()
    assert "# TYPE chat_first_answer_seconds histogram" in text


def test_blocked_is_at_most_prepare_when_the_prepass_finished_before_the_await(wired, monkeypatch):  # noqa: F811
    async def prepare(question, *, allow_network, emit=None, **kw):
        return Prepared(decision="static_model")  # done at once

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    real_block = memory_semantic.cross_chat_block

    async def slow_cross_chat(*args, **kwargs):
        # A context read the turn awaits BEFORE the knowledge await: the
        # pre-pass is long finished by the time the answer path asks for it.
        await asyncio.sleep(0.25)
        return await real_block(*args, **kwargs)

    monkeypatch.setattr(memory_semantic, "cross_chat_block", slow_cross_chat)
    metrics.reset()

    with TestClient(main.app) as client:
        _send(client, "blocked-1", [{"role": "user", "content": "what is a balance sheet?"}])

    ((prep_key, (_c1, prepare_sum, prepare_n)),) = metrics._hists["knowledge_prepare_seconds"].items()
    ((blocked_key, (_c2, blocked_sum, blocked_n)),) = metrics._hists["knowledge_blocked_seconds"].items()
    assert prepare_n == blocked_n == 1
    assert dict(blocked_key) == {"decision": "static_model", "effort": "fast"}
    assert blocked_sum <= prepare_sum
    assert prepare_sum >= 0.2, "the dispatch-timed histogram should include the overlapped read"
    assert blocked_sum < 0.1, f"the answer path did not wait for a finished pre-pass: {blocked_sum:.3f} s"


def test_blocked_is_the_wait_when_the_prepass_is_still_running(wired, monkeypatch):  # noqa: F811
    async def prepare(question, *, allow_network, emit=None, **kw):
        await asyncio.sleep(0.3)
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    metrics.reset()

    with TestClient(main.app) as client:
        _send(client, "blocked-2", [{"role": "user", "content": "what is a balance sheet?"}])

    ((_k1, (_c1, prepare_sum, _n1)),) = metrics._hists["knowledge_prepare_seconds"].items()
    ((_k2, (_c2, blocked_sum, _n2)),) = metrics._hists["knowledge_blocked_seconds"].items()
    assert 0.15 <= blocked_sum <= prepare_sum
