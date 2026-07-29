"""Agent respects the Salesforce toggle (2026-07-23 fix): with Salesforce off,
it plans/runs ONLY llm steps over the conversation context — never sql/rag."""
import asyncio

from app.engines import agent as ag
from app.engines.agent import AgentPlan, PlanStep, execute_steps, make_plan


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))


def test_no_salesforce_coerces_sql_rag_to_llm(monkeypatch):
    # planner returns a sql + rag plan; with salesforce=False it must be coerced
    async def fake_completion(msgs, **kw):
        return '{"steps":[{"id":1,"title":"a","kind":"sql","input":"count"},{"id":2,"title":"b","kind":"rag","input":"find"}]}'

    monkeypatch.setattr(ag.llm, "chat_completion", fake_completion)
    plan = asyncio.run(make_plan("q", [], salesforce=False))
    assert [s.kind for s in plan.steps] == ["llm", "llm"]


def test_no_salesforce_step_never_calls_salesforce(monkeypatch):
    # A step marked sql, run with salesforce=False, must NOT hit the sql engine.
    def boom_sql(*a, **k):
        raise AssertionError("sql engine used while Salesforce off")

    import app.engines.sql as sqlmod

    monkeypatch.setattr(sqlmod, "generate_and_run_sql", boom_sql)

    seen_history = {}

    async def fake_completion(msgs, **kw):
        seen_history["msgs"] = msgs
        return "answer from shared page: services are consulting."

    monkeypatch.setattr(ag.llm, "chat_completion", fake_completion)

    plan = AgentPlan(steps=[PlanStep(id=1, title="t", kind="sql", input="x")])
    history = [{"role": "system", "content": "Pages the user shared: careers page — we offer consulting."}]
    results = asyncio.run(execute_steps(plan, history, Rec().emit, salesforce=False))
    assert results[0]["status"] == "done"
    # the shared-page history reached the llm step
    assert any("careers page" in m.get("content", "") for m in seen_history["msgs"])


def test_salesforce_on_still_allows_sql(monkeypatch):
    async def fake_sql(inp, **k):
        return "SELECT 1", ["c"], [[1]]

    import app.engines.sql as sqlmod

    monkeypatch.setattr(sqlmod, "generate_and_run_sql", fake_sql)
    plan = AgentPlan(steps=[PlanStep(id=1, title="t", kind="sql", input="x")])
    results = asyncio.run(execute_steps(plan, [], Rec().emit, salesforce=True))
    assert results[0]["status"] == "done"
    assert results[0]["meta"]["sql"] == "SELECT 1"
