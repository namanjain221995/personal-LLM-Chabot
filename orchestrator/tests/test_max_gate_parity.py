"""THE TWO MAX GATES SPLIT ON THE SAME CONDITION, OR THIS FILE FAILS.

engines/chat.py has two gates that both decide what a Max turn gets: the
plan/draft/check/revise loop (core/max_loop.py) and best-of-N
(core/best_of.py). They were written on two different branches —
fix/max-loop-r3 and fix/max-effort-model-entangled — and merged here. Each
branch was right on its own and the pair was not: max-effort dropped
`model_choice == "smart"` from the best-of-N gate (llm.resolve_model_choice
returns the main model for EVERY choice, so the clause selected nothing and
only took Max away from a client holding the legacy `model: "fast"`
preference) while max-loop-r3 kept it on the loop gate. Measured on the two
branches merged, before the fix, with a sectioned ask and the real engine
functions:

    wants_loop=True, max_loop_enabled=True, extra_high_samples=3
    model_choice='smart' -> max_loop path: True,  best_of_N: False
    model_choice='fast'  -> max_loop path: False, best_of_N: True

A legacy `model: "fast"` Max turn naming sections was routed AWAY from the
loop and INTO the 4,000-character judge the loop exists to keep away from
exactly that ask — and `_effort_degraded` had no reason for it, so nothing on
the turn said so. Neither branch alone can show this, so neither branch's own
verifier reported it.

Two guards, because one is not enough:

- the SOURCE guard reads the gate conditions out of the file with `ast` and
  fails if they ever disagree about anything but their own feature switch. It
  catches the divergence in both directions, including a future branch adding
  a clause to the best-of-N gate instead of removing one from the loop gate.
- the BEHAVIOUR guards drive the real run_chat_engine and pin the routing and
  the metadata that the source guard cannot see.
"""
import ast
import asyncio
import json
import pathlib
import re

import pytest

from app import continuation, llm
from app.config import settings
from app.core import best_of, contract, max_loop
from app.engines import chat as chat_engine


#: The owner's own report prompt — four numbered sections, so
#: max_loop.wants_loop is True (LOOP_MIN_SECTIONS = 2). Same string as
#: tests/test_max_loop.py's OWNER_PROMPT; kept local so this file's premise
#: cannot be changed from another file.
SECTIONED_ASK = (
    "Create a professional technical report titled:\n"
    '"Enterprise Local AI Platform – Technical Overview"\n'
    "Requirements: 1. Executive Summary 2. Architecture Overview 3. Hardware "
    "Layer 4. Conclusion\n"
    "Use professional Markdown. Use headings, subheadings, tables, bullet "
    "points, numbered steps, bold text, code blocks, warnings, notes, and "
    "recommendations where appropriate. Do not skip any section."
)
#: A short ask. best-of-N IS the better shape for it (its whole candidate fits
#: in the judge's window), so being sent there is not a downgrade.
SHORT_ASK = "Why does a thinking model answer more slowly than a fast one?"

DRAFT = "## Executive Summary\n\nThe platform runs on local hardware.\n"


def test_the_premise_this_file_rests_on():
    """If these two stop being true the guards below prove nothing."""
    assert max_loop.wants_loop(contract.extract_rules(SECTIONED_ASK)) is True
    assert max_loop.wants_loop(contract.extract_rules(SHORT_ASK)) is False


# ---------------------------------------------------------------------------
# GUARD 1 — the source: the two gates may not disagree
# ---------------------------------------------------------------------------


def _normalise(cond: str) -> str:
    """`ast.unparse` writes 'max'; the file writes "max". One spelling."""
    return cond.replace("'", '"')


def _max_gates() -> list:
    """Every `if`/`elif` inside run_chat_engine whose test reads `effort`
    against "max", as source text."""
    source = pathlib.Path(chat_engine.__file__).read_text()
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_chat_engine"
    )
    return [
        _normalise(ast.unparse(node.test))
        for node in ast.walk(fn)
        if isinstance(node, ast.If) and 'effort == "max"' in _normalise(ast.unparse(node.test))
    ]


def _clauses(cond: str) -> list:
    """The top-level `and` clauses of a gate condition."""
    return [c.strip() for c in re.split(r"\band\b", cond)]


def test_no_max_gate_reads_the_model_value():
    """`model_choice` names WEIGHTS (llm.resolve_model_choice), not effort. A
    Max gate that reads it is the defect this file exists for — restore
    `model_choice == "smart"` on either gate and this fails.
    """
    gates = _max_gates()
    assert len(gates) >= 2, f"expected both Max gates, found {gates}"
    offenders = [g for g in gates if "model_choice" in g]
    assert offenders == [], (
        "a Max gate decides effort from the model value again: "
        f"{offenders}. llm.resolve_model_choice returns the main model for "
        "every choice, so the clause selects nothing and only takes Max away "
        "from a client holding the legacy model:\"fast\" preference."
    )


def test_the_two_gates_differ_only_by_their_own_feature_switch():
    """The loop gate and the best-of-N gate must be the SAME condition plus
    each one's own kill switch. Add any other clause to either and this fails
    with the clause named — which is how the 2026-09-27 split would have been
    caught on the day it was made.
    """
    gates = _max_gates()
    loop = [g for g in gates if "max_loop_enabled" in g]
    best = [g for g in gates if "extra_high_samples" in g]
    assert len(loop) == 1, f"expected exactly one max_loop gate, got {loop}"
    assert len(best) == 1, f"expected exactly one best-of-N gate, got {best}"

    loop_rest = [c for c in _clauses(loop[0]) if "max_loop_enabled" not in c]
    best_rest = [c for c in _clauses(best[0]) if "extra_high_samples" not in c]
    assert loop_rest == ['effort == "max"'], (
        f"the max_loop gate carries an extra clause: {loop_rest}"
    )
    assert best_rest == ['effort == "max"'], (
        f"the best-of-N gate carries an extra clause: {best_rest}"
    )
    assert loop_rest == best_rest


# ---------------------------------------------------------------------------
# GUARD 2 — the behaviour: routing, and what the metadata admits
# ---------------------------------------------------------------------------


def _stub_every_model_call(monkeypatch, recorded, *, candidates=None):
    """Both paths, fully offline. `recorded` names which one ran."""

    async def fake_plan(messages, **kwargs):
        recorded.append("planner")
        return "plan"

    async def fake_json(messages, **kwargs):
        recorded.append("critique")
        return json.dumps({"findings": []})

    async def fake_router(messages, **kwargs):
        recorded.append("proposer")
        return json.dumps({"items": []})

    async def fake_candidates(prompt, *, n, temperature, max_tokens, **_):
        recorded.append("best_of")
        if candidates is not None:
            return candidates
        return [
            best_of.Candidate(index=i + 1, reasoning=f"r{i+1}", answer=DRAFT)
            for i in range(n)
        ]

    async def fake_select(question, cands):
        recorded.append("judge")
        usable = [c for c in cands if c.usable]
        return usable[0], "only one"

    async def fake_long(messages, *, on_delta=None, **kwargs):
        recorded.append("draft")
        if on_delta is not None:
            await on_delta("token", DRAFT)
        return continuation.LongResult(text=DRAFT, stop_reason="complete")

    async def fake_stream(messages, **kwargs):
        recorded.append("single_stream")
        yield "token", DRAFT

    monkeypatch.setattr(llm, "chat_completion", fake_plan)
    monkeypatch.setattr(llm, "json_completion", fake_json)
    monkeypatch.setattr(llm, "router_chat_completion", fake_router)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(continuation, "stream_long_completion", fake_long)
    monkeypatch.setattr(best_of, "generate_candidates", fake_candidates)
    monkeypatch.setattr(best_of, "select_best", fake_select)


def _run(monkeypatch, message, *, model_choice="smart", candidates=None):
    recorded, events = [], []
    _stub_every_model_call(monkeypatch, recorded, candidates=candidates)

    async def emit(kind, data):
        events.append((kind, dict(data)))

    asyncio.run(
        chat_engine.run_chat_engine(
            message, [], emit, mode="assistant",
            model_choice=model_choice, effort="max",
        )
    )
    metas = [d for k, d in events if k == "meta"]
    return recorded, (metas[0] if metas else {})


@pytest.mark.parametrize("model_choice", ["smart", "fast", "", "gpt-4o"])
def test_a_sectioned_max_ask_takes_the_loop_whatever_the_model_value_says(
    monkeypatch, model_choice
):
    """Restore `model_choice == "smart"` on the loop gate and every case but
    "smart" fails here: best-of-N runs and the loop's planner never does."""
    monkeypatch.setattr(settings, "max_loop_enabled", True)
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    recorded, meta = _run(monkeypatch, SECTIONED_ASK, model_choice=model_choice)

    assert "planner" in recorded, (
        f"Max with model={model_choice!r} and a sectioned ask skipped the "
        f"loop; calls were {recorded}"
    )
    assert "best_of" not in recorded, (
        f"Max with model={model_choice!r} landed in best-of-N: {recorded}"
    )
    # Nothing was lost, so nothing claims it was.
    assert "effort_degraded" not in meta


@pytest.mark.parametrize("model_choice", ["smart", "fast"])
def test_a_short_max_ask_still_gets_best_of_n_and_claims_no_loss(
    monkeypatch, model_choice
):
    """The shape decides, and for a SHORT ask best-of-N is the better shape —
    so it is not a downgrade and must not be reported as one."""
    monkeypatch.setattr(settings, "max_loop_enabled", True)
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    recorded, meta = _run(monkeypatch, SHORT_ASK, model_choice=model_choice)

    assert "best_of" in recorded and "planner" not in recorded
    assert meta["best_of"] == 3 and meta["best_of_compared"] == 3
    assert "effort_degraded" not in meta


def test_the_kill_switch_is_the_only_way_left_to_lose_the_loop_and_it_says_so(
    monkeypatch,
):
    """MAX_LOOP_ENABLED=false with a sectioned ask: the operator's choice, a
    real answer, and the one remaining silence — now named. Delete the
    `max_loop_disabled` branch and this fails.
    """
    monkeypatch.setattr(settings, "max_loop_enabled", False)
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    recorded, meta = _run(monkeypatch, SECTIONED_ASK)

    assert "planner" not in recorded and "best_of" in recorded
    assert meta["effort_degraded"] == {
        "asked": "max",
        "delivered": "best_of_3",
        "reason": "max_loop_disabled",
        "detail": (
            "the Max loop is off on this deployment (MAX_LOOP_ENABLED=false), "
            "so this ask was answered without the plan, the check and the "
            "revision it is shaped for"
        ),
    }


def test_the_kill_switch_off_is_silent_on_an_ask_the_loop_was_not_for(monkeypatch):
    """The reason belongs to a broken promise only: a short ask loses nothing
    when the loop is off, so saying it did would be noise."""
    monkeypatch.setattr(settings, "max_loop_enabled", False)
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    _, meta = _run(monkeypatch, SHORT_ASK)
    assert "effort_degraded" not in meta


def test_two_downgrades_on_one_turn_are_both_reported(monkeypatch):
    """The loop switched off AND two of three drafts failing are two facts.
    Reporting one and dropping the other is the same silence `effort_degraded`
    exists to end, so the second rides in `also`.
    """
    monkeypatch.setattr(settings, "max_loop_enabled", False)
    monkeypatch.setattr(settings, "extra_high_samples", 3)

    two_dead = [
        best_of.Candidate(index=1, error="backend hiccup"),
        best_of.Candidate(index=2, reasoning="r2", answer=DRAFT),
        best_of.Candidate(index=3, error="backend hiccup"),
    ]
    _, meta = _run(monkeypatch, SECTIONED_ASK, candidates=two_dead)

    degraded = meta["effort_degraded"]
    assert degraded["reason"] == "candidates_partially_failed"
    assert degraded["delivered"] == "single_generation"
    assert degraded["also"] == [
        {
            "reason": "max_loop_disabled",
            "detail": (
                "the Max loop is off on this deployment "
                "(MAX_LOOP_ENABLED=false), so this ask was answered without "
                "the plan, the check and the revision it is shaped for"
            ),
        }
    ]


def test_one_downgrade_keeps_exactly_the_four_keys_it_always_had(monkeypatch):
    """`also` is omitted when there is nothing to put in it, so every existing
    exact-dict assertion in tests/test_best_of.py stays true."""
    monkeypatch.setattr(settings, "max_loop_enabled", True)
    monkeypatch.setattr(settings, "extra_high_samples", 1)

    _, meta = _run(monkeypatch, SHORT_ASK)
    assert set(meta["effort_degraded"]) == {"asked", "delivered", "reason", "detail"}
    assert meta["effort_degraded"]["reason"] == "best_of_disabled"
