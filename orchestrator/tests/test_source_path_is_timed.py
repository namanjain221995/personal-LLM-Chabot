"""The Fast source path reports how long it made the person wait.

MEASURED 2026-09-28, on a Fast turn whose pre-pass went to the network:
the pre-pass was p50 4,072 ms / p95 7,740 ms, of which the live lookup was
p50 3,014 ms / p95 6,267 ms — the single most expensive stage in front of
the first token. NEITHER WAS RECORDED.

`knowledge_prepare_seconds` existed and was observed in exactly one place:
main.py's small-talk lane, with a hardcoded 0.0 and outcome="skipped" — a
lane that runs no pre-pass at all. So the histogram held nothing but zeros
for turns that never waited, while every turn that did wait was invisible.
`_fast_lookup` had no histogram of its own at all.

These tests pin the emission, not the numbers: a histogram that is defined
and never observed is indistinguishable from a fast system.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import metrics

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"
KNOWLEDGE = Path(__file__).resolve().parents[1] / "app" / "living_knowledge.py"


def _series(text: str, name: str) -> list[str]:
    return [l for l in text.splitlines() if l.startswith(f"{name}_count{{")]


def test_the_fast_lookup_histogram_emits_every_stage_and_outcome_it_can_reach():
    """Both stages are separate series, and the labels survive the allowlist.

    `_LABELS_BY_METRIC` strips any label NAME it does not know, so a metric
    added without registering its labels renders as one undifferentiated
    series — which looks like a working metric and answers nothing."""
    for stage in ("fetch", "readback"):
        for outcome in ("ok", "deadline", "error", "skipped"):
            metrics.knowledge_fast_lookup(0.125, stage=stage, outcome=outcome)
    text = metrics.render()
    got = _series(text, "knowledge_fast_lookup_seconds")
    assert got, "the fast-lookup histogram rendered nothing"
    for stage in ("fetch", "readback"):
        for outcome in ("ok", "deadline", "error", "skipped"):
            assert any(f'stage="{stage}"' in l and f'outcome="{outcome}"' in l for l in got), (
                f"{stage}/{outcome} did not survive the label allowlist"
            )


def test_the_readback_is_its_own_stage_because_it_is_outside_the_deadline():
    """`_fast_lookup` bounds the FETCH with `asyncio.timeout(deadline)` and
    then runs the readback retrieve OUTSIDE it. Reporting them as one number
    would say the whole lookup is bounded, which it is not."""
    source = KNOWLEDGE.read_text(encoding="utf-8")
    body = source[source.index("async def _fast_lookup"):]
    body = body[: body.index("\nasync def ", 1)] if "\nasync def " in body[1:] else body
    assert 'stage="fetch"' in body and 'stage="readback"' in body
    # The readback's own observation must come AFTER the timeout block closes.
    assert body.index("asyncio.timeout(deadline)") < body.index('stage="readback"')


@pytest.mark.parametrize("decision", ["fast_lookup", "static_model", "local", "none"])
def test_the_prepass_decisions_a_real_turn_produces_are_all_accepted(decision):
    """A decision outside `KNOWLEDGE_DECISIONS` is dropped by the allowlist,
    so the series would silently lose exactly the turns being investigated."""
    metrics.knowledge_prepare(2.0, effort="fast", decision=decision, outcome="ok")
    assert any(f'decision="{decision}"' in l for l in _series(metrics.render(), "knowledge_prepare_seconds"))


def test_the_prepass_is_observed_at_the_await_and_not_only_in_the_small_talk_lane():
    """THE DEFECT THIS FILE EXISTS FOR. Until 2026-09-28 the only call site
    was the small-talk lane's hardcoded 0.0."""
    main = MAIN.read_text(encoding="utf-8")
    # The whole argument list, not up to the first comma: the elapsed
    # expression is `max(0.0, time.perf_counter() - ...)` and has one inside.
    calls = [main[m.end(): m.end() + 200]
             for m in re.finditer(r"_latency_metrics\.knowledge_prepare\(", main)]
    assert len(calls) >= 2, f"knowledge_prepare is observed {len(calls)} time(s); the real pre-pass needs one"
    assert any("perf_counter" in c for c in calls), (
        "every observation is a constant — no call site measures elapsed time"
    )
    # And it is timed from the DISPATCH, not from the await: the task overlaps
    # memory recall and compaction, and what the person waits for is the join.
    assert "knowledge_started_at = time.perf_counter()" in main
    assert main.index("knowledge_started_at = time.perf_counter()") < main.index(
        "_latency_metrics.knowledge_prepare(\n                    max(0.0,"
    )


def test_a_degraded_prepass_is_recorded_with_why_and_not_dropped():
    """A pre-pass that hit its deadline is the turn a latency investigation
    most wants to see; `outcome` has to tell it apart from a clean one."""
    for outcome in ("ok", "deadline", "error"):
        metrics.knowledge_prepare(9.0, effort="fast", decision="none", outcome=outcome)
    got = _series(metrics.render(), "knowledge_prepare_seconds")
    for outcome in ("ok", "deadline", "error"):
        assert any(f'outcome="{outcome}"' in l and 'decision="none"' in l for l in got), outcome
