"""Live, read-only Salesforce lookups — the guard and the merge.

The warehouse is a snapshot; this path goes and looks. That means
model-generated SOQL reaching a PRODUCTION org, so the guard gets the same
scrutiny as the DuckDB one, and the merge has to show a record found in both
places exactly once.
"""
import pytest

from app.core import salesforce as sf


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_a_plain_query_passes_and_gains_a_limit():
    out = sf.guard_soql("SELECT Id, Name FROM Account")
    assert out == f"SELECT Id, Name FROM Account LIMIT {sf.MAX_ROWS}"


def test_an_existing_small_limit_is_respected():
    assert sf.guard_soql("SELECT Id FROM Account LIMIT 5").endswith("LIMIT 5")


def test_an_oversized_limit_is_lowered():
    """A model asking for 50000 rows must not be able to pull the org."""
    out = sf.guard_soql("SELECT Id FROM Account LIMIT 50000")
    assert out.endswith(f"LIMIT {sf.MAX_ROWS}")


def test_a_missing_limit_is_added_rather_than_trusted():
    assert "LIMIT" in sf.guard_soql("SELECT Id FROM Contact WHERE Name = 'x'")


@pytest.mark.parametrize("bad", [
    "DELETE FROM Account",
    "UPDATE Account SET Name = 'x'",
    "INSERT INTO Account (Name) VALUES ('x')",
    "SELECT Id FROM Account; DELETE FROM Account",
    "SELECT Id FROM Account WHERE Name = 'x'; DROP TABLE Account",
])
def test_anything_that_is_not_a_single_select_is_refused(bad):
    with pytest.raises(sf.UnsafeSoql):
        sf.guard_soql(bad)


@pytest.mark.parametrize("bad", ["", "   ", "SELECT Id", "Name FROM Account"])
def test_malformed_queries_are_refused(bad):
    with pytest.raises(sf.UnsafeSoql):
        sf.guard_soql(bad)


def test_a_trailing_semicolon_is_tolerated():
    """Models add them habitually; it is not an injection on its own."""
    assert sf.guard_soql("SELECT Id FROM Account;").startswith("SELECT Id FROM Account")


def test_a_subquery_is_allowed_because_soql_needs_them():
    out = sf.guard_soql(
        "SELECT Id FROM Contact WHERE AccountId IN (SELECT Id FROM Account)"
    )
    assert out.startswith("SELECT Id FROM Contact")


# ---------------------------------------------------------------------------
# Merging warehouse rows with live rows
# ---------------------------------------------------------------------------


def test_a_record_found_in_both_places_is_shown_once():
    """The normal case, not an error — the warehouse holds a copy of what
    Salesforce has."""
    local = [{"Id": "001", "Name": "Acme"}]
    live = [{"Id": "001", "Name": "Acme"}]
    assert sf.merge_rows(local, live) == [{"Id": "001", "Name": "Acme"}]


def test_the_live_value_wins_because_it_is_newer():
    """A record edited since the last sync differs. Showing the stale copy is
    the one outcome worse than not answering."""
    merged = sf.merge_rows(
        [{"Id": "001", "Name": "Old Name", "Phone": "111"}],
        [{"Id": "001", "Name": "New Name"}],
    )
    assert merged == [{"Id": "001", "Name": "New Name", "Phone": "111"}]


def test_a_live_query_selecting_fewer_fields_does_not_blank_the_rest():
    """Overlay, not replace — otherwise asking live for Id+Name would erase
    every other column the warehouse had."""
    merged = sf.merge_rows(
        [{"Id": "001", "Name": "Acme", "Industry": "Tech", "Phone": "111"}],
        [{"Id": "001", "Name": "Acme Corp"}],
    )
    assert merged[0]["Industry"] == "Tech"
    assert merged[0]["Phone"] == "111"
    assert merged[0]["Name"] == "Acme Corp"


def test_records_only_in_salesforce_are_added():
    """This is the whole point: data newer than the last sync."""
    merged = sf.merge_rows([{"Id": "001"}], [{"Id": "002", "Name": "Brand New"}])
    assert [r["Id"] for r in merged] == ["001", "002"]


def test_records_only_in_the_warehouse_are_kept():
    merged = sf.merge_rows([{"Id": "001", "Name": "Local"}], [])
    assert merged == [{"Id": "001", "Name": "Local"}]


def test_rows_without_an_id_are_not_deduped():
    """GROUP BY / aggregate rows have no Id; collapsing them would silently
    drop real results."""
    local = [{"StageName": "Won", "c": 3}, {"StageName": "Lost", "c": 2}]
    assert len(sf.merge_rows(local, [])) == 2


def test_the_inputs_are_not_mutated():
    local = [{"Id": "001", "Name": "Old"}]
    live = [{"Id": "001", "Name": "New"}]
    sf.merge_rows(local, live)
    assert local[0]["Name"] == "Old"


def test_a_null_from_salesforce_does_not_erase_a_known_value():
    merged = sf.merge_rows(
        [{"Id": "001", "Phone": "111"}], [{"Id": "001", "Phone": None}]
    )
    assert merged[0]["Phone"] == "111"


def test_api_bookkeeping_is_stripped_from_live_rows():
    row = sf._clean({"attributes": {"type": "Account"}, "Id": "001", "Name": "Acme"})
    assert row == {"Id": "001", "Name": "Acme"}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_live_lookups_are_off_without_credentials(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "sf_client_id", "")
    assert sf.configured() is False


def test_live_lookups_are_on_with_the_client_credentials_grant(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "sf_client_id", "cid")
    monkeypatch.setattr(settings, "sf_login_url", "https://x.my.salesforce.com")
    monkeypatch.setattr(settings, "sf_client_secret", "shhh")
    assert sf.configured() is True


# ---------------------------------------------------------------------------
# The agent's "salesforce" step
# ---------------------------------------------------------------------------


def test_the_agent_can_plan_a_live_salesforce_step():
    from app.engines import agent

    plan = agent.parse_agent_plan(
        '{"steps": [{"id": 1, "title": "Look it up live", '
        '"kind": "salesforce", "input": "cases created today"}]}'
    )
    assert plan.steps[0].kind == "salesforce"


def test_the_planner_is_told_when_to_go_live():
    from app.engines import agent

    assert '"salesforce"' in agent._PLAN_SYSTEM
    assert "stale" in agent._PLAN_SYSTEM


def test_turning_salesforce_off_downgrades_a_live_step():
    """Assistant mode must not reach the org — the toggle is the boundary."""
    from app.engines import agent

    plan = agent.AgentPlan(steps=[
        agent.PlanStep(id=1, title="s", kind="salesforce", input="x")])
    assert agent._coerce_no_salesforce(plan).steps[0].kind == "llm"


def test_a_live_step_returns_rows_and_the_query_it_ran(monkeypatch):
    import asyncio

    from app.engines import agent, live_sf

    async def fake_fetch(question, history=()):
        return ("SELECT Id, Subject FROM Case LIMIT 200",
                [{"Id": "500x", "Subject": "Printer on fire"}])

    monkeypatch.setattr(live_sf, "fetch_live", fake_fetch)
    step = agent.PlanStep(id=1, title="Live", kind="salesforce", input="today's cases")
    output, detail, meta = asyncio.run(agent._run_step_impl(step, [], True))
    assert "Printer on fire" in output
    assert meta["sql"].startswith("SELECT Id, Subject FROM Case")
    assert "1 live record" in detail


def test_a_live_step_degrades_instead_of_failing_the_plan(monkeypatch):
    """Salesforce being unreachable must not lose the rest of the plan."""
    import asyncio

    from app.core.salesforce import SalesforceUnavailable
    from app.engines import agent, live_sf

    async def boom(question, history=()):
        raise SalesforceUnavailable("org unreachable")

    monkeypatch.setattr(live_sf, "fetch_live", boom)
    step = agent.PlanStep(id=1, title="Live", kind="salesforce", input="x")
    output, detail, meta = asyncio.run(agent._run_step_impl(step, [], True))
    assert "unavailable" in output.lower() and meta == {}


# ---------------------------------------------------------------------------
# Never fabricate an answer for data we do not hold
# ---------------------------------------------------------------------------


def test_a_query_with_no_real_table_is_refused():
    """THE BUG: asked about Course__c — a real object never synced — the model
    wrote `SELECT 0 AS record_count` and reported "0 records" as fact. A query
    with no FROM cannot have counted anything."""
    from app.engines.sql import references_a_known_table

    schema = {"Account": [], "Case": []}
    assert references_a_known_table("SELECT 0 AS record_count", schema) is False
    assert references_a_known_table("SELECT COUNT(*) FROM Course__c", schema) is False


def test_a_query_against_a_real_table_is_allowed():
    from app.engines.sql import references_a_known_table

    schema = {"Account": [], "Case": []}
    assert references_a_known_table('SELECT Id FROM "Case"', schema) is True
    assert references_a_known_table("SELECT Id FROM Account a", schema) is True


def test_generate_and_run_sql_raises_rather_than_inventing(monkeypatch):
    import asyncio

    from app.engines import sql as sql_engine

    async def fake_ask(*a, **k):
        return "SELECT 0 AS record_count"

    monkeypatch.setattr(sql_engine, "_ask_sql", fake_ask)
    monkeypatch.setattr(sql_engine.schema_cache, "get", lambda p: {"Account": []})
    with pytest.raises(sql_engine.NoSuchTable):
        asyncio.run(sql_engine.generate_and_run_sql("how many Course__c?"))


def test_an_aggregate_count_never_gets_a_limit():
    """Salesforce rejects LIMIT on COUNT(): adding one turned a working count
    into an error. A count cannot return a large result set anyway."""
    assert sf.guard_soql("SELECT COUNT() FROM Course__c") == "SELECT COUNT() FROM Course__c"
    assert "LIMIT" not in sf.guard_soql("SELECT COUNT() FROM Account LIMIT 200")


def test_count_of_a_field_is_a_normal_query_and_keeps_its_limit():
    """COUNT(Id) is not the aggregate form — it returns rows and can be capped."""
    assert "LIMIT" in sf.guard_soql("SELECT COUNT(Id) FROM Account")


# ---------------------------------------------------------------------------
# Schema questions have no SOQL answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [
    "How many Object have in saleforce ?? and with there fields ??? api ??",
    "what fields does Interview__c have",
    "list all objects and their api names",
])
def test_schema_questions_are_recognised(q):
    """THE BUG: asked this, the model wrote `SELECT ... FROM EntityDefinition`
    with an invented `ObjectFields` relationship and Salesforce refused it."""
    from app.engines.live_sf import is_schema_question

    assert is_schema_question(q) is True


@pytest.mark.parametrize("q", [
    "how many opportunities are closed won",
    "which accounts have the highest revenue",
])
def test_data_questions_are_not_mistaken_for_schema(q):
    from app.engines.live_sf import is_schema_question

    assert is_schema_question(q) is False


def test_describe_object_rejects_an_injected_name():
    import asyncio

    with pytest.raises(sf.UnsafeSoql):
        asyncio.run(sf.describe_object("Account/../../limits"))


def test_the_sql_engine_routes_schema_questions_to_describe():
    import inspect

    from app.engines.sql import run_sql_engine

    src = inspect.getsource(run_sql_engine)
    assert "is_schema_question(message)" in src
    assert "fetch_schema" in src


# ---------------------------------------------------------------------------
# Asking for live data explicitly, and never lying about the source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [
    "Check the live Salesforce org directly: does Dev Panchal exist?",
    "give me the latest from salesforce right now",
    "is that up to date in salesforce?",
])
def test_asking_for_live_data_is_honoured(q):
    """The warehouse is the default for speed, but "check Salesforce directly"
    is an instruction, not a hint."""
    from app.engines.sql import wants_live_lookup

    assert wants_live_lookup(q) is True


@pytest.mark.parametrize("q", [
    "how many opportunities are closed won",
    "does Dev Panchal exist? yes or no",
])
def test_ordinary_questions_still_use_the_fast_local_copy(q):
    from app.engines.sql import wants_live_lookup

    assert wants_live_lookup(q) is False


def test_the_warehouse_narrative_must_not_claim_to_be_live():
    """It answered "The live Salesforce check confirms…" from the synced copy.
    Claiming a source you did not read is its own kind of wrong answer."""
    from app.engines.sql import _narrative_messages

    system = _narrative_messages("q", ["Id"], [[1]], [])[0]["content"]
    assert "LOCAL SYNCED COPY" in system
    assert "Never say the result is live" in system


def test_the_sql_engine_routes_an_explicit_live_request_away_from_duckdb():
    import inspect

    from app.engines.sql import run_sql_engine

    src = inspect.getsource(run_sql_engine)
    assert "wants_live_lookup(message)" in src
