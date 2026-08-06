"""The Salesforce toggle decides where an answer comes FROM.

ON  → this org's synced data (sql / rag / report), via the Salesforce router.
OFF → general assistant: model knowledge, the web, uploaded files.

The bug these guard against was silent and bad: auto-orchestration runs BEFORE
the route chain, so with Salesforce ON a classifier that fancied a web search
hijacked the request and the Salesforce router never ran. Live, "what problems
do customers describe in their support cases?" came back with web articles
about IT ticketing instead of this org's cases — confidently, with citations,
and completely unrelated to the user's data.
"""
import pytest
from fastapi.testclient import TestClient

from app import llm
from app.config import settings
from app.main import app


def _parse_sse(text):
    out, event = [], None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            out.append((event, line[6:]))
    return out


@pytest.fixture()
def spy(monkeypatch):
    """Record which decision helpers the request consulted."""
    calls = {"decide": 0, "should_search": 0, "graph": 0, "chat": 0, "agent": 0}

    async def fake_decide(message, history, effort):
        calls["decide"] += 1
        from app.engines.orchestrate import Plan

        # Maximally eager by default; tests that care set calls["plan"].
        return calls.get("plan") or Plan(agent=True, search=True)

    async def fake_should_search(message):
        calls["should_search"] += 1
        return True

    async def fake_stream(messages, **kwargs):
        calls["chat"] += 1
        yield "token", "ok"

    class FakeGraph:
        async def ainvoke(self, state):
            calls["graph"] += 1
            await state["emit"]("meta", {"route": "sql"})
            return {"answer": "from salesforce"}

    monkeypatch.setattr("app.engines.orchestrate.decide", fake_decide)
    monkeypatch.setattr("app.engines.search.should_search", fake_should_search)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    async def fake_agent(message, history, emit, **kwargs):
        calls["agent"] += 1
        await emit("meta", {"route": "agent"})
        return "from the agent"

    monkeypatch.setattr("app.engines.agent.run_agent_engine", fake_agent)
    monkeypatch.setattr("app.main.get_graph", lambda: FakeGraph())
    monkeypatch.setattr(settings, "search_enabled", True)
    return calls


def ask(mode, **extra):
    with TestClient(app) as c:
        return c.post("/chat", json={
            "message": "what problems do customers describe in their cases?",
            "mode": mode, "effort": "medium", **extra,
        })


# ---------------------------------------------------------------------------
# Salesforce ON
# ---------------------------------------------------------------------------


def test_salesforce_mode_reaches_the_salesforce_engines(spy):
    """With no escalation, the Salesforce router handles the request."""
    from app.engines.orchestrate import Plan

    spy["plan"] = Plan(agent=False, search=False)
    resp = ask("salesforce")
    assert resp.status_code == 200
    assert spy["graph"] == 1, "the Salesforce router must run"


def test_salesforce_mode_can_escalate_to_the_agent(spy):
    """The agent is the path to a LIVE Salesforce lookup, so it must be
    reachable with the toggle on — disabling it made that unreachable."""
    ask("salesforce")
    assert spy["agent"] == 1
    assert spy["should_search"] == 0, "still no automatic web search"


def test_salesforce_mode_does_not_auto_escalate_to_the_web(spy):
    """THE BUG: this ran first and answered a CRM question from the internet."""
    ask("salesforce")
    assert spy["should_search"] == 0, "auto search-detection must not run"


def test_salesforce_mode_still_allows_the_agent(spy):
    """The agent is how a question reaches a LIVE Salesforce lookup, so
    disabling it here would make that path unreachable from the UI."""
    ask("salesforce")
    assert spy["decide"] == 1, "the agent classifier must still run"


def test_an_explicit_web_search_is_refused_in_salesforce_mode(spy):
    """Salesforce ON means NO web search at any effort level — even an
    explicit web_search="on" (owner request 2026-08-05; until then "on" was
    an escape hatch). The composer no longer offers the toggle in this mode,
    and this guards the promise against any other client that still sends it:
    the Salesforce router must handle the request as if "on" were never said.
    """
    from app.engines.orchestrate import Plan

    spy["plan"] = Plan(agent=False, search=False)
    resp = ask("salesforce", web_search="on")
    routes = [d for e, d in _parse_sse(resp.text) if e == "meta"]
    assert spy["graph"] == 1, "the Salesforce router must still run"
    assert spy["should_search"] == 0, "no auto-detection either"
    assert routes, "the request still produced an answer"


def test_salesforce_mode_never_searches_when_the_toggle_is_off(spy):
    from app.engines.orchestrate import Plan

    spy["plan"] = Plan(agent=False, search=False)
    ask("salesforce", web_search="off")
    assert spy["should_search"] == 0
    assert spy["graph"] == 1


# ---------------------------------------------------------------------------
# Salesforce OFF
# ---------------------------------------------------------------------------


def test_assistant_mode_still_auto_escalates(spy):
    """Turning Salesforce off is how you ask about the wider world."""
    ask("assistant")
    assert spy["decide"] == 1
    assert spy["graph"] == 0, "the Salesforce engines must not run"


def test_assistant_mode_never_touches_the_warehouse(spy):
    ask("assistant", web_search="off")
    assert spy["graph"] == 0


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_the_gate_is_keyed_on_mode_not_on_a_flag_that_could_drift():
    import inspect

    src = inspect.getsource(app.routes[0].endpoint.__module__ and __import__(
        "app.main", fromlist=["main"]))
    assert 'auto_web_search_allowed = request.mode == "assistant"' in src


# ---------------------------------------------------------------------------
# Salesforce booleans are TEXT, and getting that wrong answers "0" confidently
# ---------------------------------------------------------------------------


def test_the_sql_prompt_warns_that_checkboxes_are_lowercase_text():
    """`WHERE IsWon = 'True'` matched nothing and the app answered "there are 0
    Closed Won opportunities" — with 18 in the table. A confidently wrong
    number is worse than an error, because nobody goes looking."""
    from app.engines.sql import _SQL_SYSTEM

    assert "'true'" in _SQL_SYSTEM and "IsWon" in _SQL_SYSTEM
    assert "Never compare" in _SQL_SYSTEM


def test_the_sql_prompt_prefers_the_column_the_user_named():
    from app.engines.sql import _SQL_SYSTEM

    assert "StageName" in _SQL_SYSTEM


# ---------------------------------------------------------------------------
# A data answer must never arrive with no words
# ---------------------------------------------------------------------------


def test_the_sql_narrative_does_not_pay_for_a_reasoning_pass():
    """THE BUG: "Give me employee details" returned 314 rows and an EMPTY
    answer. Reasoning comes out of the same budget, so with hundreds of rows in
    the prompt the model spent all 3000 tokens thinking and streamed nothing —
    the UI showed a table with blank prose above it."""
    import inspect

    from app.engines.sql import run_sql_engine

    src = inspect.getsource(run_sql_engine)
    assert "thinking=False" in src
    assert "max_tokens=6000" in src


def test_an_empty_narrative_falls_back_to_a_factual_line():
    """Whatever else happens, never leave the user staring at a silent table."""
    import inspect

    from app.engines.sql import run_sql_engine

    src = inspect.getsource(run_sql_engine)
    assert "if not answer:" in src
    assert "row(s) across" in src


def test_stream_chat_completion_can_turn_thinking_off():
    import inspect

    from app import llm

    sig = inspect.signature(llm.stream_chat_completion)
    assert sig.parameters["thinking"].default is True


def test_the_narrative_is_told_the_real_row_count():
    """THE BUG: 314 rows came back and the summary said "29 employee records"
    — it described the 30-row sample it was shown as if that were everything."""
    from app.engines.sql import _narrative_messages

    msgs = _narrative_messages("q", ["Id"], [[i] for i in range(30)], [], total_rows=314)
    system, user = msgs[0]["content"], msgs[-1]["content"]
    assert "Total rows in the result: 314" in user
    assert "never the number of rows you can see" in system


def test_the_agent_cannot_search_the_web_in_salesforce_mode(spy, monkeypatch):
    """Salesforce ON means no web at ANY level. The agent is allowed to run,
    so its own web steps are the remaining way out — they must be closed."""
    seen = {}

    async def fake_agent(message, history, emit, **kwargs):
        seen.update(kwargs)
        await emit("meta", {"route": "agent"})
        return "ok"

    monkeypatch.setattr("app.engines.agent.run_agent_engine", fake_agent)
    ask("salesforce")
    assert seen.get("web") is False, "the agent must not be given web access"


def test_the_agent_may_search_the_web_with_salesforce_off(spy, monkeypatch):
    seen = {}

    async def fake_agent(message, history, emit, **kwargs):
        seen.update(kwargs)
        await emit("meta", {"route": "agent"})
        return "ok"

    monkeypatch.setattr("app.engines.agent.run_agent_engine", fake_agent)
    ask("assistant", web_search="auto")
    assert seen.get("web") is True


# ---------------------------------------------------------------------------
# Short follow-ups, and never claiming we cannot see Salesforce
# ---------------------------------------------------------------------------


def test_a_short_follow_up_carries_the_previous_question(monkeypatch):
    """THE BUG: "just tell me if it exists, yes or no?" classified alone looks
    like small talk, landed on "chat", and got a canned apology instead of a
    lookup."""
    import asyncio

    from app.engines import router as router_engine

    seen = {}

    async def capture(messages, **kwargs):
        seen["text"] = messages[-1]["content"]
        return '{"route": "sql"}'

    monkeypatch.setattr("app.llm.router_chat_completion", capture)
    history = [{"role": "user", "content": "does the interview for Dev Panchal exist?"}]
    asyncio.run(router_engine.route_request("just tell me yes or no", False, history))
    assert "Dev Panchal" in seen["text"]


def test_a_long_question_is_not_padded_with_history(monkeypatch):
    """Only SHORT follow-ups need the context; a full question stands alone."""
    import asyncio

    from app.engines import router as router_engine

    seen = {}

    async def capture(messages, **kwargs):
        seen["text"] = messages[-1]["content"]
        return '{"route": "sql"}'

    monkeypatch.setattr("app.llm.router_chat_completion", capture)
    history = [{"role": "user", "content": "earlier thing about Dev Panchal"}]
    long_q = "how many interviews were completed last month broken down by recruiter and status"
    asyncio.run(router_engine.route_request(long_q, False, history))
    assert "Dev Panchal" not in seen["text"]


def test_salesforce_chat_never_claims_it_cannot_see_the_data():
    """It told a user "I don't have direct access to your live Salesforce org"
    after querying that org all session."""
    from app.engines.chat import SALESFORCE_CHAT_SYSTEM

    assert "you DO have Salesforce access" in SALESFORCE_CHAT_SYSTEM
    assert "never suggest" in SALESFORCE_CHAT_SYSTEM.lower()
