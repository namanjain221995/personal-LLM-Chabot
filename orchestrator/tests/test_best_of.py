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

    async def fake_generate(prompt, *, n, temperature, max_tokens, **_):
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

    async def all_dead(prompt, *, n, temperature, max_tokens, **_):
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
    # ...and it SAYS so. This used to assert meta == [{"route": "chat"}]: Max
    # was asked for, one generation was delivered, and nothing on the turn
    # recorded the difference (2026-09-27). A downgrade may happen; being
    # silent about it may not.
    meta = [d for k, d in events if k == "meta"]
    assert len(meta) == 1 and meta[0]["route"] == "chat"
    assert meta[0]["effort_degraded"] == {
        "asked": "max",
        "delivered": "single_generation",
        "reason": "candidates_failed",
        "detail": "all 2 Max drafts failed; answered with a single generation",
    }


# ---------------------------------------------------------------------------
# GUARD (2026-09-27): effort decides effort, and a downgrade is never silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_choice", ["smart", "fast", "", "gpt-4o"])
def test_max_gets_best_of_n_whatever_the_model_value_says(monkeypatch, model_choice):
    """The defect this pins: /app/app/engines/chat.py gated best-of-N on
    `model_choice == "smart"`, so a client holding the legacy
    `model: "fast"` preference (frontend/lib/prefs.ts keeps a stored 'fast'
    on load; today's effort picker can neither produce nor clear it) chose Max
    and silently got the ordinary single stream. Measured on origin/dev
    (4164bb8): 0 candidates, thinking off, meta {"route": "chat"}.

    Restore the `model_choice == "smart"` clause and every case but "smart"
    fails here — generate_candidates is never reached and `must_not_stream`
    fires instead.
    """
    monkeypatch.setattr(settings, "extra_high_samples", 3)
    seen = {}

    async def fake_generate(prompt, *, n, temperature, max_tokens, **_):
        seen["n"] = n
        return _candidates("loser one", "the winning answer", "loser two")

    async def fake_select(question, candidates):
        return candidates[1], "clearest"

    async def must_not_stream(messages, **kwargs):  # pragma: no cover
        raise AssertionError(
            f"Max with model={model_choice!r} fell through to a single stream"
        )
        yield  # make it an async generator

    monkeypatch.setattr(best_of, "generate_candidates", fake_generate)
    monkeypatch.setattr(best_of, "select_best", fake_select)
    monkeypatch.setattr(llm, "stream_chat_events", must_not_stream)

    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine(
        "hard question", [], emit, mode="assistant",
        model_choice=model_choice, effort="max",
    ))

    assert seen["n"] == 3, "best-of-N must run for every model value at Max"
    assert answer == "the winning answer"
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["best_of"] == 3 and meta["best_of_winner"] == 2
    # Nothing was downgraded, so nothing claims it was.
    assert "effort_degraded" not in meta


@pytest.mark.parametrize("model_choice", ["smart", "fast"])
def test_max_thinks_whatever_the_model_value_says(monkeypatch, model_choice):
    """The other half of the same entanglement: `llm.wants_thinking` returned
    False for every choice but "smart", so the fall-through stream ran with
    the reasoning pass off on a turn the person had set to Max."""
    monkeypatch.setattr(settings, "extra_high_samples", 1)  # force the stream
    seen = {}

    async def fake_stream(messages, *, model_choice, effort, temperature, max_tokens):
        seen["thinking"] = llm.wants_thinking(model_choice, effort)
        yield "token", "single"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events, emit = _collect_emit()
    asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", model_choice=model_choice, effort="max",
    ))
    assert seen["thinking"] is True


def test_max_without_best_of_n_says_so_in_the_metadata(monkeypatch):
    """EXTRA_HIGH_SAMPLES=1 is an operator choice, not a bug — but the picker
    promises Max "drafts several answers in parallel, keeps the best", so the
    turn that did not carries the reason rather than looking like Max."""
    monkeypatch.setattr(settings, "extra_high_samples", 1)

    async def fake_stream(messages, *, model_choice, effort, temperature, max_tokens):
        yield "token", "single"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events, emit = _collect_emit()
    asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", effort="max",
    ))
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["effort_degraded"] == {
        "asked": "max",
        "delivered": "single_generation",
        "reason": "best_of_disabled",
        "detail": (
            "best-of-N is off on this deployment (EXTRA_HIGH_SAMPLES=1); "
            "answered with a single generation"
        ),
    }


def _mixed(*answers):
    """Candidates where a None answer is a FAILED draft, as
    generate_candidates returns it: unusable, not fatal (see
    test_a_failed_candidate_becomes_empty_not_fatal)."""
    out = []
    for i, a in enumerate(answers):
        if a is None:
            out.append(best_of.Candidate(index=i + 1, error="backend hiccup"))
        else:
            out.append(best_of.Candidate(index=i + 1, reasoning=f"r{i+1}", answer=a))
    return out


def test_meta_reports_how_many_drafts_were_actually_compared(monkeypatch):
    """GUARD (2026-09-27). `meta["best_of"]` is the N asked for, and it was
    the ONLY count reported — so a Max turn where 2 of 3 drafts failed
    claimed a best-of-3 that never happened. Drop `best_of_compared` and the
    partial branch and this fails: meta says 3 drafts, one ran.
    """
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def two_dead(prompt, *, n, temperature, max_tokens, **_):
        return _mixed(None, "the only survivor", None)

    async def must_not_stream(messages, **kwargs):  # pragma: no cover
        raise AssertionError("a usable candidate must not fall through")
        yield

    monkeypatch.setattr(best_of, "generate_candidates", two_dead)
    monkeypatch.setattr(llm, "stream_chat_events", must_not_stream)

    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", effort="max",
    ))
    assert answer == "the only survivor"
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["best_of"] == 3, "the asked-for N is still reported"
    assert meta["best_of_compared"] == 1, "...beside what was really compared"
    # One draft survived, so no comparison happened at all: that is a single
    # generation wearing a Max badge, and it says so.
    assert meta["effort_degraded"] == {
        "asked": "max",
        "delivered": "single_generation",
        "reason": "candidates_partially_failed",
        "detail": (
            "2 of 3 Max drafts failed; the one that survived was used "
            "without a comparison"
        ),
    }


def test_a_narrower_comparison_is_named_as_one_not_as_a_single_generation(monkeypatch):
    """Two of three drafts usable IS a comparison — just not the one asked
    for. `delivered` says which, so the metadata never overstates OR
    understates what ran."""
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def one_dead(prompt, *, n, temperature, max_tokens, **_):
        return _mixed("first draft", None, "second draft")

    async def fake_select(question, candidates):
        usable = [c for c in candidates if c.usable]
        return usable[-1], "clearest"

    monkeypatch.setattr(best_of, "generate_candidates", one_dead)
    monkeypatch.setattr(best_of, "select_best", fake_select)

    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine(
        "q", [], emit, mode="assistant", effort="max",
    ))
    assert answer == "second draft"
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["best_of_compared"] == 2
    assert meta["effort_degraded"]["delivered"] == "best_of_2"
    assert meta["effort_degraded"]["detail"] == (
        "1 of 3 Max drafts failed; the best of 2 was kept"
    )


def test_a_full_house_of_drafts_claims_no_downgrade(monkeypatch):
    """The other side of the guard above: when every draft the operator asked
    for was compared, nothing was lost and nothing says it was."""
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def all_good(prompt, *, n, temperature, max_tokens, **_):
        return _candidates("one", "two", "three")

    async def fake_select(question, candidates):
        return candidates[0], "clearest"

    monkeypatch.setattr(best_of, "generate_candidates", all_good)
    monkeypatch.setattr(best_of, "select_best", fake_select)

    events, emit = _collect_emit()
    asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort="max"))
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["best_of"] == 3 and meta["best_of_compared"] == 3
    assert "effort_degraded" not in meta


@pytest.mark.parametrize("effort", ["fast", "think"])
def test_below_max_carries_no_downgrade_claim(monkeypatch, effort):
    """Fast and Think never asked for best-of-N, so telling them they did not
    get it would be noise — the key belongs to a broken promise only."""
    monkeypatch.setattr(settings, "extra_high_samples", 1)

    # **kwargs: at Fast the engine also passes `answer_plan`
    # (core/answer_sampling.fast_sampling_for), which Max has no plan for.
    async def fake_stream(messages, **kwargs):
        yield "token", "single"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events, emit = _collect_emit()
    asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort=effort))
    assert "effort_degraded" not in [d for k, d in events if k == "meta"][0]


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


# ---------------------------------------------------------------------------
# Max shows its work (2026-09-28). Measured on the real engine before this
# (run_chat_engine in-process, n=5, load 3.9-5.2): first visible frame at
# 11,228-17,415 ms (median 14,166) and it was always the LAST frame — the
# drafts and the judge ran with nothing on the wire. After: 0-56 ms. The step
# timeline (core/steps.py) is what a person now sees, and the FIRST frame of
# the turn is a running step.
# ---------------------------------------------------------------------------


def _step_frames(events):
    return [(d["id"], d["status"], d.get("detail", "")) for k, d in events if k == "step"]


def test_generate_candidates_reports_each_landing_in_the_order_they_land(monkeypatch):
    delays = {"1": 0.03, "2": 0.01, "3": 0.02}
    calls = {"n": 0}

    async def staggered(messages, *, effort, temperature, max_tokens):
        calls["n"] += 1
        me = str(calls["n"])
        await asyncio.sleep(delays[me])
        if me == "3":
            raise RuntimeError("draft three died")
        return "r", f"answer {me}"

    monkeypatch.setattr(llm, "chat_completion_with_reasoning", staggered)
    seen = []

    async def on_candidate(candidate, done, total):
        seen.append((candidate.index, candidate.usable, done, total))

    candidates = asyncio.run(
        best_of.generate_candidates(
            [{"role": "user", "content": "q"}],
            n=3, temperature=0.3, max_tokens=100, on_candidate=on_candidate,
        )
    )
    # Completion order, not index order; the dead draft still counts.
    assert seen == [(2, True, 1, 3), (3, False, 2, 3), (1, True, 3, 3)]
    # The returned list is still in index order.
    assert [c.index for c in candidates] == [1, 2, 3]


def test_max_shows_its_work_before_the_first_draft_lands(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 3)
    events, emit = _collect_emit()
    frames_before_generation = []

    async def fake_generate(prompt, *, n, temperature, max_tokens, on_candidate=None):
        frames_before_generation.extend(events)
        cands = _candidates("loser one", "the winning answer", "loser two")
        for done, c in enumerate((cands[2], cands[0], cands[1]), start=1):
            await asyncio.sleep(0.005)
            await on_candidate(c, done, n)
        return cands

    async def fake_select(question, candidates):
        return candidates[1], "clearest"

    monkeypatch.setattr(best_of, "generate_candidates", fake_generate)
    monkeypatch.setattr(best_of, "select_best", fake_select)

    answer = asyncio.run(chat.run_chat_engine("hard question", [], emit, mode="assistant", effort="max"))
    assert answer == "the winning answer"

    # The FIRST frame of the turn is a running step, on the wire before the
    # first engine call is made.
    assert events[0][0] == "step"
    assert frames_before_generation == [
        ("step", {"id": 1, "title": "Drafting 3 answers in parallel", "status": "running", "detail": "0 of 3 drafts done"})
    ]
    steps = _step_frames(events)
    assert steps[0] == (1, "running", "0 of 3 drafts done")
    # Each landing draft updates the SAME row (mergeStep by id), never opens one.
    assert steps[1][:2] == (1, "running") and steps[1][2].startswith("1 of 3 drafts done, ")
    assert steps[2][:2] == (1, "running") and steps[2][2].startswith("2 of 3 drafts done, ")
    assert steps[3][:2] == (1, "done") and steps[3][2].startswith("3 of 3 drafts done, ")
    assert steps[3][2].endswith(" s")
    assert steps[4] == (2, "running", "")
    assert steps[5] == (2, "done", "kept draft 2: clearest")
    assert [s[0] for s in steps] == [1, 1, 1, 1, 2, 2]
    # The answer still streams after the work, and nothing streamed before it.
    kinds = [k for k, _ in events]
    assert kinds.index("reasoning") > kinds.index("step")
    assert "".join(d["text"] for k, d in events if k == "token") == "the winning answer"
    # The rows persist with the turn, so a reload shows them too.
    meta = [d for k, d in events if k == "meta"][0]
    assert [s["id"] for s in meta["steps"]] == [1, 2]
    assert meta["steps"][1]["detail"] == "kept draft 2: clearest"
    assert meta["best_of_winner"] == 2 and "effort_degraded" not in meta


def test_a_single_survivor_is_not_called_a_comparison_in_the_timeline(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def two_dead(prompt, *, n, temperature, max_tokens, on_candidate=None):
        cands = [
            best_of.Candidate(index=1, error="x"),
            best_of.Candidate(index=2, reasoning="r", answer="the one that lived"),
            best_of.Candidate(index=3, error="y"),
        ]
        for done, c in enumerate(cands, start=1):
            await on_candidate(c, done, n)
        return cands

    async def must_not_stream(*a, **k):  # pragma: no cover
        raise AssertionError("single stream must not run")
        yield  # noqa

    monkeypatch.setattr(best_of, "generate_candidates", two_dead)
    monkeypatch.setattr(llm, "stream_chat_events", must_not_stream)
    events, emit = _collect_emit()
    asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort="max"))

    steps = _step_frames(events)
    assert steps[1][2].startswith("1 of 3 drafts done (1 failed), ")
    assert steps[-1][:2] == (1, "done") and steps[-1][2].startswith("3 of 3 drafts done (2 failed), ")
    # No "Choosing the best draft" row: there was nothing to compare.
    assert {s[0] for s in steps} == {1}
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["effort_degraded"]["reason"] == "candidates_partially_failed"
    assert meta["effort_degraded"]["delivered"] == "single_generation"
    assert [s["status"] for s in meta["steps"]] == ["done"]


def test_all_drafts_failed_closes_the_row_and_keeps_effort_degraded(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 2)

    async def all_dead(prompt, *, n, temperature, max_tokens, on_candidate=None):
        return [best_of.Candidate(index=1, error="x"), best_of.Candidate(index=2, error="y")]

    async def fake_stream(messages, *, model_choice, effort, temperature, max_tokens):
        yield "token", "plain answer"

    monkeypatch.setattr(best_of, "generate_candidates", all_dead)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    events, emit = _collect_emit()
    answer = asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort="max"))
    assert answer == "plain answer"
    steps = _step_frames(events)
    assert steps[0] == (1, "running", "0 of 2 drafts done")
    assert steps[-1][:2] == (1, "failed")
    assert steps[-1][2].startswith("2 of 2 drafts done (2 failed), ")
    assert steps[-1][2].endswith(" s; answering with a single generation")
    # The row is closed BEFORE the single stream starts: no spinner over the answer.
    kinds = [k for k, _ in events]
    assert kinds.index("token") > max(i for i, k in enumerate(kinds) if k == "step")
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["effort_degraded"]["reason"] == "candidates_failed"
    assert [s["status"] for s in meta["steps"]] == ["failed"]


def test_a_turn_that_dies_mid_draft_leaves_no_spinner(monkeypatch):
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def parked(prompt, *, n, temperature, max_tokens, on_candidate=None):
        raise RuntimeError("engine went away")

    monkeypatch.setattr(best_of, "generate_candidates", parked)
    events, emit = _collect_emit()
    with pytest.raises(RuntimeError, match="engine went away"):
        asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort="max"))
    steps = _step_frames(events)
    assert steps[0][:2] == (1, "running")
    assert steps[-1] == (1, "failed", "the turn ended before this step finished")
