"""High must actually do more than Medium.

Before this, the two levels were identical apart from how long the model
thought — same searches, same plan size, same answer ceiling. A user who picks
High for a hard question is asking for more work, so the depth knobs scale with
the level, and these tests keep them from quietly collapsing back together.
"""
import asyncio
import inspect

import pytest

from app import llm
from app.engines import agent, chat, search


# ---------------------------------------------------------------------------
# Search depth
# ---------------------------------------------------------------------------


def test_high_searches_more_than_medium_which_searches_more_than_low():
    assert (
        search.query_budget("fast")
        < search.query_budget("think")
        < search.query_budget("max")
    )


def test_fast_gets_no_search_budget_at_all():
    assert search.query_budget("fast") == 0


def test_an_unknown_level_falls_back_to_the_middle():
    assert search.query_budget("bogus") == search._MAX_QUERIES


def test_the_query_cap_is_actually_applied(monkeypatch):
    """A model returning ten queries must not run ten searches at Low."""
    async def many(messages, **kwargs):
        return '["q1","q2","q3","q4","q5","q6","q7","q8","q9","q10"]'

    monkeypatch.setattr(llm, "router_chat_completion", many)
    for effort in ("think", "max"):
        out = asyncio.run(search.rewrite_queries("q", [], effort))
        assert len(out) == search.query_budget(effort)


def test_query_rewriting_runs_on_the_small_model(monkeypatch):
    """It is a mechanical rewrite — the main model's reasoning pass on it made
    every search wait seconds before the first fetch started."""
    calls = {"router": 0, "main": 0}

    async def router(messages, **kwargs):
        calls["router"] += 1
        return '["a"]'

    async def main(messages, **kwargs):
        calls["main"] += 1
        return '["a"]'

    monkeypatch.setattr(llm, "router_chat_completion", router)
    monkeypatch.setattr(llm, "chat_completion", main)
    asyncio.run(search.rewrite_queries("anything", [], "think"))
    assert calls == {"router": 1, "main": 0}


def test_a_failed_rewrite_still_searches_the_original_question(monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "router_chat_completion", boom)
    assert asyncio.run(search.rewrite_queries("what is X", [], "max")) == ["what is X"]


# ---------------------------------------------------------------------------
# Plan depth
# ---------------------------------------------------------------------------


def test_high_plans_more_steps_than_medium():
    assert agent.step_budget("think") < agent.step_budget("max")
    # Legacy names ride the aliases to the same budgets.
    assert agent.step_budget("medium") == agent.step_budget("think")
    assert agent.step_budget("high") == agent.step_budget("think")


def test_the_plan_budget_never_exceeds_the_validated_cap():
    """The schema rejects more than MAX_STEPS, so asking for more wastes a retry."""
    for effort in ("fast", "think", "max", "low", "medium", "high", "bogus"):
        assert agent.step_budget(effort) <= agent.MAX_STEPS


@pytest.mark.parametrize("salesforce", [True, False])
def test_the_planner_is_asked_for_this_levels_step_count(monkeypatch, salesforce):
    """The substitution into the prompt must really happen — a silent no-op
    would leave both levels asking for the same plan size."""
    seen = {}

    async def capture(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return '{"steps": [{"id": 1, "title": "t", "kind": "llm", "input": "x"}]}'

    monkeypatch.setattr(llm, "chat_completion", capture)
    asyncio.run(agent.make_plan("q", [], salesforce, "think"))
    assert f"at most {agent.step_budget('medium')} " in seen["system"]
    assert f"at most {agent.MAX_STEPS} " not in seen["system"]


def test_high_gets_more_room_for_the_final_answer():
    assert agent._SYNTH_TOKENS["max"] > agent._SYNTH_TOKENS["think"]


def test_effort_reaches_the_step_runner():
    """A web step inside a High plan should search at High depth too."""
    assert "effort" in inspect.signature(agent._run_step_impl).parameters
    assert "effort" in inspect.signature(agent.execute_steps).parameters
    assert "effort, effort" not in inspect.getsource(agent._execute_node)
    # The web step must search at THIS level's depth, not a default.
    src = inspect.getsource(agent._run_step_impl)
    assert "research_step(" in src and "effort" in src.split("research_step(")[1][:120]


# ---------------------------------------------------------------------------
# Answer quality knobs
# ---------------------------------------------------------------------------


def test_thinking_levels_use_a_lower_temperature_than_chat():
    """0.6 invents API names in code; the thinking levels are used for code."""
    src = inspect.getsource(chat.run_chat_engine)
    assert 'temperature = 0.3 if effort in ("think", "max") else 0.6' in src


def test_high_gets_a_bigger_answer_ceiling():
    src = inspect.getsource(chat.run_chat_engine)
    assert "max_tokens = 16000" in src


def test_the_assistant_prompt_carries_the_code_rules():
    system = chat._messages("q", [], "assistant")[0]["content"]
    assert "```python" in system and "imports it needs" in system


def test_agent_steps_carry_the_code_rules_too():
    assert "```python" in agent._STEP_LLM_SYSTEM


def test_small_talk_gets_neither_diagrams_nor_code_rules():
    """A greeting must not come back with a mermaid graph or a code fence."""
    system = chat._messages("hi", [], "salesforce")[0]["content"]
    assert "```python" not in system and "mermaid" not in system


# ---------------------------------------------------------------------------
# The 3-level ladder and its aliases (2026-08-19 collapse)
# ---------------------------------------------------------------------------


def test_max_exists_at_every_effort_gate():
    """max must behave as at-least-Think everywhere effort branches; a
    missing dict key silently degrades it via .get fallbacks."""
    from app import llm
    from app.engines import agent, orchestrate, search

    assert llm.REASONING_EFFORTS == ("fast", "think", "max")
    assert llm.wants_thinking("smart", "max") is True
    assert search._QUERY_BUDGET["max"] >= search._QUERY_BUDGET["think"]
    assert search._SOURCE_BUDGET["max"] >= search._SOURCE_BUDGET["think"]
    assert agent._STEP_BUDGET["max"] == agent.MAX_STEPS
    assert agent._SYNTH_TOKENS["max"] >= agent._SYNTH_TOKENS["think"]
    assert orchestrate.allowances("max") == {"agent": True, "search": True}


def test_legacy_wire_values_normalize_to_the_ladder():
    """Old clients and stored prefs keep working: low->fast, medium/high->
    think, extra_high->max, unknown->think."""
    from app import llm
    from app.engines import orchestrate, search

    assert llm.normalize_effort("low") == "fast"
    assert llm.normalize_effort("medium") == "think"
    assert llm.normalize_effort("high") == "think"
    assert llm.normalize_effort("extra_high") == "max"
    assert llm.normalize_effort("bogus") == "think"
    # Aliases reach the depth gates through normalization, not dict keys.
    assert search.query_budget("high") == search.query_budget("think")
    assert search.query_budget("extra_high") == search.query_budget("max")
    assert orchestrate.allowances("extra_high") == {"agent": True, "search": True}
    assert orchestrate.allowances("fast") == {"agent": False, "search": False}


def test_max_gets_the_high_output_ceiling():
    src = inspect.getsource(chat.run_chat_engine)
    assert 'effort in ("think", "max") and mode == "assistant"' in src
