"""Best-of-N for extra_high: concurrency, the judge, and the safety net.

Pinned claims:
- candidates are generated CONCURRENTLY, never sequentially;
- the judge runs thinking-OFF with a guided-JSON verdict and its choice is
  honored; a judge that fails, or names a candidate that does not exist,
  degrades to the longest usable answer;
- losing candidates are logged, never emitted;
- the chat engine streams the winner's thinking + answer and stamps
  best_of metadata; zero usable candidates falls through to the ordinary
  single-stream path so extra_high can never be WORSE than high.
"""
import asyncio
import json

import pytest

from app import llm
from app.config import settings
from app.core import best_of
from app.engines import chat


# ---------------------------------------------------------------------------
# generate_candidates: concurrency
# ---------------------------------------------------------------------------


def test_candidates_are_generated_concurrently(monkeypatch):
    """With three 50ms generations, sequential would take ≥150ms and, more
    decisively, no generation would START before the previous FINISHED."""
    starts, finishes = [], []

    async def fake_gen(messages, *, effort, temperature, max_tokens):
        starts.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.05)
        finishes.append(asyncio.get_event_loop().time())
        return "thought", f"answer {len(finishes)}"

    monkeypatch.setattr(llm, "chat_completion_with_reasoning", fake_gen)

    candidates = asyncio.run(
        best_of.generate_candidates(
            [{"role": "user", "content": "q"}], n=3, temperature=0.3, max_tokens=100
        )
    )
    assert [c.index for c in candidates] == [1, 2, 3]
    assert all(c.usable for c in candidates)
    # Every generation started before the FIRST one finished — overlap proof.
    assert max(starts) < min(finishes)


def test_a_failed_candidate_becomes_empty_not_fatal(monkeypatch):
    calls = {"n": 0}

    async def flaky(messages, *, effort, temperature, max_tokens):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("backend hiccup")
        return "r", "fine"

    monkeypatch.setattr(llm, "chat_completion_with_reasoning", flaky)
    candidates = asyncio.run(
        best_of.generate_candidates(
            [{"role": "user", "content": "q"}], n=3, temperature=0.3, max_tokens=100
        )
    )
    assert [c.usable for c in candidates] == [True, False, True]
    assert "hiccup" in candidates[1].error


# ---------------------------------------------------------------------------
# select_best: the judge and its fallbacks
# ---------------------------------------------------------------------------


def _candidates(*answers):
    return [
        best_of.Candidate(index=i + 1, reasoning=f"r{i+1}", answer=a)
        for i, a in enumerate(answers)
    ]


def test_the_judge_verdict_is_honored_and_runs_thinking_off(monkeypatch):
    captured = {}

    async def fake_judge(messages, *, json_schema, schema_name, temperature,
                         max_tokens, thinking):
        captured.update(thinking=thinking, schema=json_schema)
        return json.dumps({"winner": 2, "reason": "more complete"})

    monkeypatch.setattr(llm, "json_completion", fake_judge)
    winner, reason = asyncio.run(
        best_of.select_best("q", _candidates("short", "a much longer answer", "mid"))
    )
    assert winner.index == 2
    assert reason == "more complete"
    assert captured["thinking"] is False
    assert captured["schema"]["required"] == ["winner"]


def test_a_nonexistent_winner_falls_back_to_longest(monkeypatch):
    async def confused(messages, **kwargs):
        return json.dumps({"winner": 9})

    monkeypatch.setattr(llm, "json_completion", confused)
    winner, reason = asyncio.run(
        best_of.select_best("q", _candidates("aa", "the longest answer here", "bbb"))
    )
    assert winner.index == 2
    assert "longest" in reason


def test_a_dead_judge_falls_back_to_longest(monkeypatch):
    async def dead(messages, **kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(llm, "json_completion", dead)
    winner, reason = asyncio.run(best_of.select_best("q", _candidates("aa", "bbbb")))
    assert winner.index == 2
    assert "longest" in reason


def test_single_usable_candidate_skips_the_judge(monkeypatch):
    async def must_not_run(messages, **kwargs):  # pragma: no cover
        raise AssertionError("judge called with one usable candidate")

    monkeypatch.setattr(llm, "json_completion", must_not_run)
    candidates = _candidates("only answer", "")
    winner, reason = asyncio.run(best_of.select_best("q", candidates))
    assert winner.index == 1
    assert "only one" in reason


# ---------------------------------------------------------------------------
# Chat engine wiring
# ---------------------------------------------------------------------------


def _collect_emit():
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    return events, emit


def test_extra_high_streams_the_winner_and_stamps_meta(monkeypatch, caplog):
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def fake_generate(prompt, *, n, temperature, max_tokens):
        assert n == 3
        return _candidates("loser one", "the winning answer", "loser two")

    async def fake_select(question, candidates):
        return candidates[1], "clearest"

    monkeypatch.setattr(best_of, "generate_candidates", fake_generate)
    monkeypatch.setattr(best_of, "select_best", fake_select)

    events, emit = _collect_emit()
    with caplog.at_level("INFO"):
        answer = asyncio.run(chat.run_chat_engine(
            "hard question", [], emit, mode="assistant", effort="extra_high",
        ))

    assert answer == "the winning answer"
    assert "".join(d["text"] for k, d in events if k == "token") == "the winning answer"
    # The winner's thinking streamed on the reasoning channel.
    assert "".join(d["text"] for k, d in events if k == "reasoning") == "r2"
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["best_of"] == 3
    assert meta["best_of_winner"] == 2
    assert meta["best_of_reason"] == "clearest"
    # Losers hit the log, not the UI.
    assert any("losing candidate 1" in r.message for r in caplog.records)
    assert not any("loser one" in d.get("text", "") for _, d in events)


def test_no_usable_candidates_falls_through_to_single_stream(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 2)

    async def all_dead(prompt, *, n, temperature, max_tokens):
        return [best_of.Candidate(index=1, error="x"), best_of.Candidate(index=2, error="y")]

    async def fake_stream(messages, *, model_choice, effort, temperature, max_tokens):
        yield "token", "plain answer"

    monkeypatch.setattr(best_of, "generate_candidates", all_dead)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", effort="extra_high",
    ))
    assert answer == "plain answer"
    assert [d for k, d in events if k == "meta"] == [{"route": "chat"}]


def test_samples_of_one_disables_best_of(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 1)

    async def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("best-of ran with EXTRA_HIGH_SAMPLES=1")

    async def fake_stream(messages, *, model_choice, effort, temperature, max_tokens):
        yield "token", "single"

    monkeypatch.setattr(best_of, "generate_candidates", must_not_run)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", effort="extra_high",
    ))
    assert answer == "single"
