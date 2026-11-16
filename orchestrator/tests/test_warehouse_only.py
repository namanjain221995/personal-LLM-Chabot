"""Owner's order, 2026-11-04: every answer reads the warehouse. Nothing in the
orchestrator may reach Salesforce. sync-worker alone talks to it, to fill the
warehouse. These tests run with every switch that could reopen the door turned on.
"""
import ast
import asyncio
import pathlib
import re
import socket

import pytest
import yaml

from app.config import Settings, settings
from app.core import salesforce as sf
from app.core.sf_intel import tools
from app.core.sf_intel.models import SalesforceQueryPlan
from app.engines import agent, sf_intel
from app.engines import sql as sql_engine
from app.engines.sql import NoSuchTable, WarehouseBusy, run_sql_engine

APP = pathlib.Path(sf.__file__).resolve().parents[1]
REPO = APP.parents[1]
SECRETS = ("SF_CLIENT_ID", "SF_CLIENT_SECRET", "SF_USERNAME", "SF_PRIVATE_KEY_B64", "SF_PRIVATE_KEY_FILE")
LIVE_STACK = (
    "compose.yaml",
    "compose/compose.dgx-spark.yaml",
    "compose/compose.published-dgx-spark.yaml",
    "compose/compose.cluster-dgx-spark.yaml",
)


@pytest.fixture(autouse=True)
def salesforce_dials(monkeypatch):
    """Every switch on, and any lookup of a Salesforce host recorded and refused.

    Recorded, not only raised: most callers catch Exception, so a raise alone
    would be swallowed and the test would pass.
    """
    for name, value in {
        "SF_LIVE_ENABLED": "true",
        "SF_CLIENT_ID": "id",
        "SF_CLIENT_SECRET": "secret",
        "SF_LOGIN_URL": "https://test.my.salesforce.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(settings, "sf_live_enabled", True)
    monkeypatch.setattr(settings, "sf_client_id", "id", raising=False)
    monkeypatch.setattr(settings, "sf_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "sf_login_url", "https://test.my.salesforce.com", raising=False)
    tools.clear_caches()
    sf.clear_describe_cache()
    dials = []
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if re.search(r"salesforce|force\.com", str(host), re.I):
            dials.append(host)
            raise OSError("Salesforce is off limits here")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    yield dials
    assert dials == [], f"something tried to reach Salesforce: {dials}"


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, event, data):
        self.events.append((event, data))

    def tokens(self):
        return "".join(d["text"] for e, d in self.events if e == "token")


@pytest.mark.parametrize(
    "call",
    [
        lambda: sf.run_soql("SELECT Id FROM Account"),
        lambda: sf.run_soql_page("SELECT Id FROM Account"),
        lambda: sf.query_more("/services/data/v61.0/query/01g000"),
        lambda: sf.search_records("Acme", ["Account"], {}),
        lambda: sf.org_key(),
        lambda: sf.list_objects(),
        lambda: sf.describe_object("Account", refresh=True),
    ],
)
def test_every_client_function_refuses(call):
    with pytest.raises(sf.SalesforceUnavailable):
        asyncio.run(call())


def test_no_environment_value_turns_it_back_on():
    assert sf.configured() is False
    assert Settings().sf_live_enabled is False


def test_the_client_module_has_no_way_to_open_a_connection():
    tree = ast.parse((APP / "core" / "salesforce.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"httpx", "requests", "aiohttp", "urllib", "urllib3", "http", "socket", "net"}


def test_no_orchestrator_file_holds_a_way_in():
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "/services/oauth2" not in text, path
        assert not re.search(r"SF_CLIENT_|SF_PRIVATE_KEY|SF_USERNAME|sf_client_|sf_private_key", text), path
        if path.name != "salesforce.py":
            assert "/services/data" not in text, path


def _env_files(body):
    entries = body.get("env_file") or []
    entries = [entries] if isinstance(entries, (str, dict)) else entries
    return [entry if isinstance(entry, str) else entry["path"] for entry in entries]


def test_only_the_sync_worker_gets_salesforce_credentials():
    services = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))["services"]
    for service, body in services.items():
        if service == "orchestrator":
            # env_file also feeds this service, so a missing line is not enough:
            # only a literal blank overrides what .env and secrets.env hold.
            env = body["environment"]
            assert all(env.get(key) == "" for key in SECRETS), env.keys()
            assert "SF_LIVE_ENABLED" not in env
        elif service != "sync-worker":
            env = body.get("environment") or {}
            assert not set(env) & set(SECRETS), service
            # env_file folds every key of the file in, so .env or secrets.env here
            # would hand over the credentials with no line naming them.
            for file in _env_files(body):
                assert "generated.env" in file, (service, file)
    # The overlays use `!reset`, which yaml.safe_load refuses, so these are read as text.
    for name in LIVE_STACK[1:]:
        text = (REPO / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(%s)\s*[:=]" % "|".join(SECRETS), text, re.M), name
        # The same hole, read as text: an env_file entry naming .env or secrets.env.
        assert not re.search(r"^\s*(env_file:|-)\s*(path:\s*)?\S*(?<![a-z])(\.env|secrets\.env)\b", text, re.M), name


def test_the_superseded_compose_file_keeps_the_same_rule():
    """docker-compose.yml is the old stack's rollback path. Its orchestrator has
    no env_file, so a missing line is enough there."""
    services = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for service, body in services.items():
        if service != "sync-worker":
            env = body.get("environment") or {}
            assert not set(env) & set(SECRETS), service
            assert "SF_LIVE_ENABLED" not in env, service
            assert not _env_files(body), service


def test_the_current_docs_promise_no_live_read():
    """The pages people read as current. The 2026-07-31 audit pages keep their
    descriptions of the old path, under a banner."""
    for name in (
        "README.md",
        "docs/FLOWS.md",
        "docs/AUTH.md",
        "docs/ARCHITECTURE_CURRENT.md",
        "docs/06-agent-design/README.md",
    ):
        text = (REPO / name).read_text(encoding="utf-8")
        promise = r"\bsf_live\b|SF_LIVE_ENABLED|Toggle \*\*Live Salesforce|falls? back to live|live SOQL|the live org"
        assert not re.search(promise, text), name


def test_the_planner_tools_stay_off_the_org():
    plan = SalesforceQueryPlan(object_api_name="Account", select_fields=["Name"])
    _summary, available = asyncio.run(tools.get_salesforce_schema("q", objects=["Account"]))
    assert available == []
    assert asyncio.run(tools.search_salesforce_entities("Acme", objects=["Account"])) == []
    with pytest.raises(tools.PlanRejected):
        asyncio.run(tools.compile_and_validate(plan))
    assert sf_intel._live_available() is False


def _warehouse_engine(monkeypatch, result):
    from app import llm

    calls = []

    async def fake_sql(question, **kwargs):
        calls.append(question)
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_stream(messages, **kwargs):
        calls.append(messages)
        yield "token", "an answer."

    async def fake_tokens(messages, **kwargs):
        calls.append(messages)
        yield "an answer."

    monkeypatch.setattr(sql_engine, "generate_and_run_sql", fake_sql)
    monkeypatch.setattr(sql_engine.os.path, "exists", lambda path: True)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(llm, "stream_chat_completion", fake_tokens)
    return calls


@pytest.mark.parametrize(
    "question",
    [
        "Check the live Salesforce org directly: how many accounts?",
        "how many accounts right now?",
        "is that up to date?",
    ],
)
def test_asking_for_live_data_reads_the_warehouse(monkeypatch, question):
    calls = _warehouse_engine(monkeypatch, ("SELECT 1", ["c"], [[1]]))
    asyncio.run(run_sql_engine(question, [], Rec().emit))
    assert calls[0] == question


def test_the_sql_engine_has_no_force_live_door():
    with pytest.raises(TypeError):
        asyncio.run(run_sql_engine("q", [], Rec().emit, force_live=True))


@pytest.mark.parametrize(
    "error, expected",
    [(NoSuchTable("x"), "synced Salesforce copy"), (WarehouseBusy("locked"), "being refreshed")],
)
def test_missing_or_busy_data_is_one_truthful_line(monkeypatch, error, expected):
    _warehouse_engine(monkeypatch, error)
    rec = Rec()
    asyncio.run(run_sql_engine("how many widgets do we hold?", [], rec.emit))
    assert expected in rec.tokens()
    assert "live" not in rec.tokens().lower()
    assert "syncworker" not in rec.tokens()


def test_a_schema_question_is_answered_from_the_warehouse_catalogue(monkeypatch):
    question = "how many objects do we have, and which fields does Account have?"
    calls = _warehouse_engine(monkeypatch, NoSuchTable("information_schema"))
    monkeypatch.setattr(
        sql_engine.schema_cache,
        "get",
        lambda path: {
            "Account": [("Id", "VARCHAR"), ("Name", "VARCHAR")],
            "Case": [("Id", "VARCHAR")],
            "_sync_meta": [("object_name", "VARCHAR")],
        },
    )
    rec = Rec()
    asyncio.run(run_sql_engine(question, [], rec.emit))
    assert "holds 2 tables" in rec.tokens()
    assert "Account has 2 synced fields: Id, Name" in rec.tokens()
    assert "live" not in rec.tokens().lower()
    assert calls == [question], "the catalogue answer spends no model call"


@pytest.mark.parametrize(
    "question, fields",
    [
        ("which fields does Task have?", "Task has 2 synced fields: Id, Subject"),
        ("list the fields on Product2", "Product2 has 2 synced fields: Id, Family"),
        ("what fields does account have?", "Account has 2 synced fields: Id, Name"),
        ("what fields does interview__c have?", "Interview__c has 2 synced fields: Id, Status__c"),
    ],
)
def test_a_named_object_gets_its_fields_however_it_is_written(monkeypatch, question, fields):
    """The catalogue's own table names decide what counts as named: not a list of
    a few standard objects, and not the capital letters the person typed."""
    _warehouse_engine(monkeypatch, NoSuchTable("information_schema"))
    monkeypatch.setattr(
        sql_engine.schema_cache,
        "get",
        lambda path: {
            "Account": [("Id", "VARCHAR"), ("Name", "VARCHAR")],
            "Task": [("Id", "VARCHAR"), ("Subject", "VARCHAR")],
            "Product2": [("Id", "VARCHAR"), ("Family", "VARCHAR")],
            "Interview__c": [("Id", "VARCHAR"), ("Status__c", "VARCHAR")],
            "_sync_meta": [("object_name", "VARCHAR")],
        },
    )
    rec = Rec()
    asyncio.run(run_sql_engine(question, [], rec.emit))
    assert fields in rec.tokens()
    assert "Name an object" not in rec.tokens()


def test_a_schema_question_with_no_readable_catalogue_says_so(monkeypatch):
    question = "how many objects do we have?"
    calls = _warehouse_engine(monkeypatch, NoSuchTable("information_schema"))

    def locked(path):
        raise RuntimeError("locked, with nothing cached")

    monkeypatch.setattr(sql_engine.schema_cache, "get", locked)
    rec = Rec()
    asyncio.run(run_sql_engine(question, [], rec.emit))
    assert "cannot be read right now" in rec.tokens()
    assert calls == [question]


def test_a_step_that_says_salesforce_reads_the_warehouse(monkeypatch):
    calls = _warehouse_engine(monkeypatch, ("SELECT 1", ["c"], [[1]]))
    step = agent.PlanStep(id=1, title="t", kind="salesforce", input="today's cases")
    output, _detail, meta = asyncio.run(agent._run_step_impl(step, [], True))
    assert calls and meta["sql"] == "SELECT 1"
    assert "live" not in output.lower()


@pytest.mark.parametrize("error", [NoSuchTable("x"), WarehouseBusy("locked")])
def test_an_agent_sql_step_never_falls_back_to_the_org(monkeypatch, error):
    from app.engines import live_sf

    async def boom(*args, **kwargs):
        raise AssertionError("fetch_live called")

    monkeypatch.setattr(live_sf, "fetch_live", boom)
    _warehouse_engine(monkeypatch, error)
    step = agent.PlanStep(id=1, title="t", kind="sql", input="x")
    output, _detail, meta = asyncio.run(agent._run_step_impl(step, [], True))
    assert "synced" in output and meta == {}


def test_no_prompt_offers_or_promises_a_live_query():
    from app.engines.chat import SALESFORCE_ASSISTANT_SYSTEM, SALESFORCE_CHAT_SYSTEM

    for prompt in (SALESFORCE_ASSISTANT_SYSTEM, SALESFORCE_CHAT_SYSTEM, agent._PLAN_SYSTEM):
        assert not re.search(r"\blive\b", prompt, re.I), prompt
    assert '"salesforce"' not in agent._PLAN_SYSTEM


def test_the_starter_card_never_asks_the_org_for_its_objects(monkeypatch):
    # Recorded, not raised: starter_options catches Exception, as the fixture warns.
    calls = []

    async def fake(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(tools, "list_objects", fake)
    card = asyncio.run(sf_intel.starter_options("conv-warehouse-only"))
    assert calls == []
    assert [option["id"] for option in card["options"]] == ["find_record", "pipeline", "service", "recent"]


def test_a_chat_turn_sending_sf_live_true_answers_from_the_warehouse(monkeypatch):
    """The whole /chat door, with every switch on: the old browser's field is
    ignored and the turn reads the warehouse. The graph is the one fake, because
    its router needs a model; the sql engine and the Salesforce client are real."""
    from fastapi.testclient import TestClient

    from app.engines import live_sf
    from app.engines.orchestrate import Plan
    from app.main import app

    calls = _warehouse_engine(monkeypatch, ("SELECT 1", ["c"], [[1]]))
    question = "how many accounts right now? check the live org"

    class WarehouseGraph:
        async def ainvoke(self, state):
            return {"answer": await run_sql_engine(state["message"], state["history"], state["emit"])}

    async def no_escalation(*args, **kwargs):
        return Plan(agent=False, search=False)

    async def boom(*args, **kwargs):
        raise AssertionError("fetch_live called")

    monkeypatch.setattr("app.engines.orchestrate.decide", no_escalation)
    monkeypatch.setattr("app.main.get_graph", lambda: WarehouseGraph())
    monkeypatch.setattr(live_sf, "fetch_live", boom)

    with TestClient(app) as client:
        response = client.post(
            "/chat", json={"message": question, "mode": "salesforce", "sf_live": True, "effort": "medium"}
        )
    assert response.status_code == 200
    assert question in calls, "the warehouse query never ran"
    assert "an answer." in response.text
    assert '"live_salesforce' not in response.text
    assert "event: error" not in response.text


@pytest.mark.parametrize(
    "link",
    [
        "https://techsara--dev9.sandbox.my.salesforce.com/lightning/r/Lead/00QXx0000001AAA/view",
        "https://techsara.lightning.force.com/lightning/r/Lead/00QXx0000001AAA/view",
    ],
)
def test_a_pasted_link_to_the_org_is_not_opened(monkeypatch, link):
    """The link alone, through the real /chat. It used to reach robots.txt and then
    the page, store what came back in the shared corpus, and queue a crawl."""
    from fastapi.testclient import TestClient

    from app import db, llm
    from app.core import net, robots
    from app.engines import url
    from app.engines.orchestrate import Plan
    from app.main import app

    robots.reset_cache()
    monkeypatch.setattr(settings, "sf_lightning_base_url", "https://techsara.lightning.force.com")
    fetched = []
    real_fetch = net.safe_fetch

    async def spy(address, **kwargs):
        fetched.append(address)
        return await real_fetch(address, **kwargs)

    async def no_model(*_a, **_k):
        raise AssertionError("no model call is needed to say a link is not opened")
        yield

    async def no_escalation(*_a, **_k):
        return Plan(agent=False, search=False)

    monkeypatch.setattr(net, "safe_fetch", spy)
    monkeypatch.setattr(llm, "stream_chat_events", no_model)
    monkeypatch.setattr("app.engines.orchestrate.decide", no_escalation)
    with TestClient(app) as client:
        reply = client.post("/chat", json={"message": link, "conversation_id": "org-link", "mode": "salesforce"})
    assert reply.status_code == 200
    assert "that link was not fetched" in reply.text.replace('\\"', '"')
    assert fetched == []
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM web_pages").fetchone()["n"] == 0
        assert con.execute("SELECT count(*) AS n FROM web_crawls").fetchone()["n"] == 0
    assert url.ORG_LINK_LINE in reply.text


def test_an_old_browser_sending_sf_live_is_ignored():
    from app.main import ChatRequest

    body = ChatRequest.model_validate({"message": "how many accounts?", "sf_live": True})
    assert not hasattr(body, "sf_live")
    assert "sf_live" not in body.model_dump()
    assert "request.sf_live" not in (APP / "main.py").read_text(encoding="utf-8")
