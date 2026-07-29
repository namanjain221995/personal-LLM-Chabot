"""Auto-orchestration: the model decides how hard to work.

The Agent toggle is gone from the UI, so these guarantees carry the weight:

- effort "low" never escalates — the user asked for a quick answer;
- an explicit user choice always wins over the classifier;
- a classifier failure degrades to a plain answer, never to a broken turn;
- escalation is reported, so it is automatic but never invisible.
"""
import asyncio
import json

import pytest

from app import llm
from app.engines import orchestrate


def fake_classifier(payload):
    async def _call(messages, **kwargs):
        return payload

    return _call


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_a_clean_decision():
    plan = orchestrate.parse_plan('{"agent": true, "search": false}')
    assert plan.agent is True and plan.search is False


def test_parses_json_wrapped_in_prose():
    plan = orchestrate.parse_plan('Sure! {"agent": false, "search": true} ok')
    assert plan.agent is False and plan.search is True


@pytest.mark.parametrize("junk", ["", "no json here", "{broken", "null"])
def test_unparseable_output_means_do_neither(junk):
    plan = orchestrate.parse_plan(junk)
    assert plan.agent is False and plan.search is False


def test_only_literal_true_counts():
    """A hedging model must not accidentally trigger a 5x slower path."""
    plan = orchestrate.parse_plan('{"agent": "maybe", "search": 1}')
    assert plan.agent is False and plan.search is False


# ---------------------------------------------------------------------------
# When it runs
# ---------------------------------------------------------------------------


def test_fast_never_escalates_and_never_even_asks(monkeypatch):
    called = {"n": 0}

    async def _spy(messages, **kwargs):
        called["n"] += 1
        return '{"agent": true, "search": true}'

    monkeypatch.setattr(llm, "router_chat_completion", _spy)
    plan = asyncio.run(orchestrate.decide("build me a whole plan", [], "fast"))
    assert plan.agent is False and plan.search is False
    assert called["n"] == 0, "Fast must not even pay for the classifier"


def test_low_may_search_but_never_runs_the_agent(monkeypatch):
    """Low is 'quick, but look it up if you must'."""
    monkeypatch.setattr(
        llm, "router_chat_completion", fake_classifier('{"agent": true, "search": true}')
    )
    plan = asyncio.run(orchestrate.decide("design a huge system", [], "low"))
    assert plan.search is True, "low may search"
    assert plan.agent is False, "low must never escalate to agent steps"


def test_the_classifier_can_only_narrow_never_escalate():
    """A level's allowance is a ceiling the model cannot argue its way past."""
    assert orchestrate.allowances("fast") == {"agent": False, "search": False}
    assert orchestrate.allowances("low") == {"agent": False, "search": True}
    assert orchestrate.allowances("medium") == {"agent": True, "search": True}
    assert orchestrate.allowances("high") == {"agent": True, "search": True}
    # An unrecognised value must not silently unlock everything or nothing.
    assert orchestrate.allowances("bogus") == orchestrate.allowances("medium")


def test_medium_and_high_effort_do_classify(monkeypatch):
    monkeypatch.setattr(
        llm, "router_chat_completion", fake_classifier('{"agent": true, "search": true}')
    )
    for effort in ("medium", "high"):
        plan = asyncio.run(orchestrate.decide("design a migration", [], effort))
        assert plan.agent is True and plan.search is True


def test_empty_message_does_not_escalate(monkeypatch):
    monkeypatch.setattr(
        llm, "router_chat_completion", fake_classifier('{"agent": true}')
    )
    plan = asyncio.run(orchestrate.decide("   ", [], "high"))
    assert plan.agent is False


def test_classifier_failure_degrades_to_a_plain_answer(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(llm, "router_chat_completion", boom)
    plan = asyncio.run(orchestrate.decide("anything", [], "high"))
    assert plan.agent is False and plan.search is False


# ---------------------------------------------------------------------------
# Prompt shape
# ---------------------------------------------------------------------------


def test_long_input_is_clipped_before_classification():
    msgs = orchestrate._messages("x" * 50_000, [])
    assert len(msgs[-1]["content"]) <= orchestrate._INPUT_CAP


def test_system_blocks_are_not_fed_to_the_classifier():
    history = [
        {"role": "system", "content": "SECRET recall block"},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    joined = json.dumps(orchestrate._messages("now what?", history))
    assert "SECRET recall block" not in joined


def test_few_shots_teach_both_directions():
    msgs = orchestrate._messages("q", [])
    answers = [
        json.loads(m["content"]) for m in msgs if m["role"] == "assistant"
    ]
    assert any(a["agent"] for a in answers), "needs a positive agent example"
    assert any(not a["agent"] for a in answers), "needs a negative agent example"
    assert any(a["search"] for a in answers)
    assert any(not a["search"] for a in answers)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_escalation_is_described_for_the_user():
    assert "step" in orchestrate.describe(orchestrate.Plan(True, False)).lower()
    assert "web" in orchestrate.describe(orchestrate.Plan(False, True)).lower()
    both = orchestrate.describe(orchestrate.Plan(True, True)).lower()
    assert "step" in both and "web" in both
    assert orchestrate.describe(orchestrate.Plan(False, False)) == ""


# ---------------------------------------------------------------------------
# High is never weaker than Medium
# ---------------------------------------------------------------------------


def test_high_plans_whenever_it_searches(monkeypatch):
    """Observed live: the same research question came back {agent, search} at
    Medium and {search} at High, so High answered with a one-shot search while
    the level below it planned. Anything worth searching for at High is worth
    planning."""
    monkeypatch.setattr(
        llm, "router_chat_completion", fake_classifier('{"agent": false, "search": true}')
    )
    high = asyncio.run(orchestrate.decide("research this", [], "high"))
    assert high.agent is True and high.search is True
    # Medium still does exactly what the classifier said.
    medium = asyncio.run(orchestrate.decide("research this", [], "medium"))
    assert medium.agent is False and medium.search is True


def test_high_still_answers_simple_questions_directly(monkeypatch):
    """"High" must not mean "always slow" — a question needing no tools gets none."""
    monkeypatch.setattr(
        llm, "router_chat_completion", fake_classifier('{"agent": false, "search": false}')
    )
    plan = asyncio.run(orchestrate.decide("what is 2+2?", [], "high"))
    assert plan.agent is False and plan.search is False


def test_high_is_never_weaker_than_medium(monkeypatch):
    """Whatever the classifier returns, High's allowance covers Medium's."""
    for payload in ('{"agent": true, "search": true}',
                    '{"agent": false, "search": true}',
                    '{"agent": true, "search": false}',
                    '{"agent": false, "search": false}'):
        monkeypatch.setattr(llm, "router_chat_completion", fake_classifier(payload))
        med = asyncio.run(orchestrate.decide("q", [], "medium"))
        high = asyncio.run(orchestrate.decide("q", [], "high"))
        assert high.agent >= med.agent and high.search >= med.search
