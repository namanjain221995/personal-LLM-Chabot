"""The agent can search the web inside its own plan.

Medium and High are specified as "agent AND web search". Before the "web" step
kind existed the two were mutually exclusive — search was checked first in the
route chain, so any request the classifier marked as both lost its multi-step
plan to a one-shot search. These tests hold that fix in place, and hold the two
gates (Salesforce off, web off) that must still cut access.
"""
import asyncio

import pytest

from app import llm
from app.engines import agent


def plan_of(*kinds):
    return agent.AgentPlan(steps=[
        agent.PlanStep(id=i + 1, title=f"step {i + 1}", kind=k, input="do it")
        for i, k in enumerate(kinds)
    ])


# ---------------------------------------------------------------------------
# The plan may ask for the web
# ---------------------------------------------------------------------------


def test_web_is_a_valid_step_kind():
    plan = agent.parse_agent_plan(
        '{"steps": [{"id": 1, "title": "Market size", "kind": "web", '
        '"input": "current CRM market size"}]}'
    )
    assert plan.steps[0].kind == "web"


@pytest.mark.parametrize("system", [agent._PLAN_SYSTEM, agent._PLAN_SYSTEM_NO_SF])
def test_both_planner_prompts_offer_the_web_step(system):
    assert '"web"' in system


def test_salesforce_off_keeps_web_but_drops_sql_and_rag():
    """Assistant mode has no Salesforce — but the internet is unrelated to it."""
    out = agent._coerce_no_salesforce(plan_of("sql", "rag", "web", "llm"))
    assert [s.kind for s in out.steps] == ["llm", "llm", "web", "llm"]


def test_web_off_downgrades_web_steps_to_llm():
    out = agent.coerce_allowed(plan_of("web", "llm"), web=False)
    assert [s.kind for s in out.steps] == ["llm", "llm"]


def test_web_on_leaves_the_plan_alone():
    out = agent.coerce_allowed(plan_of("web", "sql"), web=True)
    assert [s.kind for s in out.steps] == ["web", "sql"]


# ---------------------------------------------------------------------------
# Running a web step
# ---------------------------------------------------------------------------


SOURCES = [
    {"n": 1, "title": "A", "url": "https://a.test/x", "domain": "a.test"},
    {"n": 2, "title": "B", "url": "https://b.test/y", "domain": "b.test"},
]


def run_web_step(monkeypatch, research):
    from app.engines import search

    monkeypatch.setattr(search, "research_step", research)
    step = agent.PlanStep(id=1, title="Research", kind="web", input="market size")
    return asyncio.run(agent._run_step_impl(step, [], False))


def test_a_web_step_returns_its_answer_and_sources(monkeypatch):
    async def research(question, history=(), effort="medium", emit=None):
        return "The market is large [1].", SOURCES

    output, detail, sub = run_web_step(monkeypatch, research)
    assert output == "The market is large [1]."
    assert sub["sources"] == SOURCES
    assert "2 source(s)" in detail and "a.test" in detail


def test_a_web_step_with_no_results_answers_from_knowledge(monkeypatch):
    """Search being down must not fail the step and lose that part of the plan
    — but it must SAY SO. The fallback used to be silent, so a stale-memory
    answer shipped under a trust line claiming searches go to the internet."""
    async def research(question, history=(), effort="medium", emit=None):
        return "", []

    async def fake_completion(messages, **kwargs):
        return "From my own knowledge."

    monkeypatch.setattr(llm, "chat_completion", fake_completion)
    output, detail, sub = run_web_step(monkeypatch, research)
    assert "From my own knowledge." in output
    assert "no readable sources" in output
    assert detail == "search returned nothing readable"
    assert "sources" not in sub


# ---------------------------------------------------------------------------
# Merging into the final answer
# ---------------------------------------------------------------------------


def results_with(*source_lists, outputs=None):
    return [
        {"step": agent.PlanStep(id=i + 1, title=f"s{i + 1}", kind="web", input="x"),
         "status": "done",
         "output": (outputs[i] if outputs else "o"),
         "meta": {"sources": list(srcs)}}
        for i, srcs in enumerate(source_lists)
    ]


def test_sources_from_several_web_steps_are_renumbered_contiguously():
    """Every step numbers its own sources from [1] — they must not collide."""
    a = [{"n": 1, "title": "A", "url": "https://a.test", "domain": "a.test"}]
    b = [{"n": 1, "title": "B", "url": "https://b.test", "domain": "b.test"},
         {"n": 2, "title": "C", "url": "https://c.test", "domain": "c.test"}]
    results = results_with(a, b)
    agent.renumber_web_sources(results)
    meta = agent.merge_step_meta(results)
    assert [s["n"] for s in meta["sources"]] == [1, 2, 3]
    assert [s["domain"] for s in meta["sources"]] == ["a.test", "b.test", "c.test"]


def test_the_same_url_found_twice_is_listed_once():
    dup = [{"n": 1, "title": "A", "url": "https://a.test", "domain": "a.test"}]
    results = results_with(dup, [dict(dup[0])])
    agent.renumber_web_sources(results)
    meta = agent.merge_step_meta(results)
    assert len(meta["sources"]) == 1


# --- the citation bug: prose markers must move with the metadata -------------


def test_step_prose_is_renumbered_to_match_the_source_list():
    """THE BUG: renumbering only the metadata left every marker in step 2's
    prose pointing at step 1's sources, so the finished answer cited pages that
    had nothing to do with the claim."""
    a = [{"n": 1, "title": "A", "url": "https://a.test", "domain": "a.test"}]
    b = [{"n": 1, "title": "B", "url": "https://b.test", "domain": "b.test"},
         {"n": 2, "title": "C", "url": "https://c.test", "domain": "c.test"}]
    results = results_with(
        a, b, outputs=["Alpha is true [1].", "Beta is true [1], and gamma [2]."]
    )
    agent.renumber_web_sources(results)
    assert results[0]["output"] == "Alpha is true [1]."
    # step 2's [1] and [2] are plan-wide 2 and 3
    assert results[1]["output"] == "Beta is true [2], and gamma [3]."
    meta = agent.merge_step_meta(results)
    by_n = {s["n"]: s["url"] for s in meta["sources"]}
    assert by_n == {1: "https://a.test", 2: "https://b.test", 3: "https://c.test"}


def test_a_duplicate_url_maps_both_steps_to_the_same_number():
    same = {"n": 1, "title": "A", "url": "https://a.test", "domain": "a.test"}
    results = results_with(
        [same], [dict(same)], outputs=["First saw it [1].", "So did I [1]."]
    )
    agent.renumber_web_sources(results)
    assert results[0]["output"] == "First saw it [1]."
    assert results[1]["output"] == "So did I [1]."


def test_numbers_that_are_not_citations_are_left_alone():
    """A step that quotes "[2]" from a page it read, or writes an array index,
    must not be silently rewritten."""
    a = [{"n": 1, "title": "A", "url": "https://a.test", "domain": "a.test"}]
    results = results_with(a, outputs=["Cited [1]. Unrelated [7] and [42]."])
    agent.renumber_web_sources(results)
    assert results[0]["output"] == "Cited [1]. Unrelated [7] and [42]."


def test_non_web_steps_keep_their_prose_untouched():
    results = [{"step": agent.PlanStep(id=1, title="s", kind="llm", input="x"),
                "status": "done", "output": "See figure [3].", "meta": {}}]
    agent.renumber_web_sources(results)
    assert results[0]["output"] == "See figure [3]."


def test_renumbering_happens_before_the_model_reads_the_steps():
    """If synthesis is prompted first, the model copies the OLD markers and the
    fix is invisible — order is the whole point."""
    import inspect

    src = inspect.getsource(agent._synthesize_node)
    assert src.index("renumber_web_sources") < src.index("_synthesis_messages")


def test_a_plan_with_no_web_steps_has_no_sources_key():
    meta = agent.merge_step_meta([
        {"step": agent.PlanStep(id=1, title="s", kind="llm", input="x"),
         "status": "done", "output": "o", "meta": {}}
    ])
    assert "sources" not in meta


def test_synthesis_is_told_to_preserve_citations():
    assert "[2]" in agent._SYNTH_SYSTEM


# ---------------------------------------------------------------------------
# Route precedence
# ---------------------------------------------------------------------------


def test_agent_is_routed_before_plain_search():
    """Both wanted → the agent runs and searches inside its plan."""
    import inspect

    from app import main

    src = inspect.getsource(main)
    assert src.index("elif want_agent:") < src.index("elif want_search:"), (
        "search before agent means a plan-and-search request loses its plan"
    )


def test_the_agent_receives_the_web_gate():
    import inspect

    from app import main

    assert "web=want_search" in inspect.getsource(main)


# ── A forced search must actually search ─────────────────────────────────────
# The composer's web pill collapses into the same boolean as the auto
# classifier by the time it reaches the agent, so the planner stayed free to
# plan zero web steps — and did: asked about a GPT-5.6 announcement with web
# search forced ON, it planned one llm step and answered from training memory
# that no such model exists, under a trust line saying searches go to the
# internet.

def test_a_forced_search_converts_the_llm_step():
    from app.engines.agent import AgentPlan, PlanStep, ensure_web_step

    plan = AgentPlan(steps=[PlanStep(id=1, title="t", kind="llm", input="q")])
    assert ensure_web_step(plan, "msg").steps[0].kind == "web"


def test_a_plan_that_already_searches_is_untouched():
    from app.engines.agent import AgentPlan, PlanStep, ensure_web_step

    plan = AgentPlan(
        steps=[
            PlanStep(id=1, title="t", kind="web", input="q"),
            PlanStep(id=2, title="u", kind="llm", input="r"),
        ]
    )
    assert [s.kind for s in ensure_web_step(plan, "m").steps] == ["web", "llm"]


def test_a_data_only_plan_gains_a_web_step_with_the_users_words():
    from app.engines.agent import AgentPlan, PlanStep, ensure_web_step

    plan = AgentPlan(steps=[PlanStep(id=1, title="t", kind="sql", input="q")])
    out = ensure_web_step(plan, "what changed this week")
    assert out.steps[-1].kind == "web"
    assert out.steps[-1].input == "what changed this week"


def test_the_plan_node_forces_a_web_step_only_when_the_user_did(monkeypatch):
    import asyncio

    from app.engines import agent as agent_mod

    async def plan_llm_only(*_a, **_k):
        return agent_mod.AgentPlan(
            steps=[agent_mod.PlanStep(id=1, title="t", kind="llm", input="q")]
        )

    monkeypatch.setattr(agent_mod, "make_plan", plan_llm_only)

    forced = asyncio.run(
        agent_mod._plan_node(
            {"message": "m", "web": True, "web_forced": True}
        )
    )
    assert forced["plan"].steps[0].kind == "web"

    auto = asyncio.run(
        agent_mod._plan_node(
            {"message": "m", "web": True, "web_forced": False}
        )
    )
    # Auto mode keeps the planner's judgement.
    assert auto["plan"].steps[0].kind == "llm"


def test_a_web_step_with_no_sources_says_so(monkeypatch):
    """The fallback used to be silent, so a stale-memory answer shipped as if
    it had been researched."""
    import asyncio

    from app.engines import agent as agent_mod

    async def no_sources(*_a, **_k):
        return "", []

    async def canned(messages, **_k):
        return "the model's memory"

    monkeypatch.setattr(agent_mod, "llm", agent_mod.llm)
    from app.engines import search as search_mod

    monkeypatch.setattr(search_mod, "research_step", no_sources)
    monkeypatch.setattr(agent_mod.llm, "chat_completion", canned)

    step = agent_mod.PlanStep(id=1, title="t", kind="web", input="q")
    output, detail, meta = asyncio.run(
        agent_mod._run_step_impl(step, [], salesforce=False, message="q")
    )
    assert "no readable sources" in output
    assert "general knowledge" in output
    assert detail == "search returned nothing readable"
