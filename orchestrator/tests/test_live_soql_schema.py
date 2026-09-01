"""The live path must not invent schema.

THE BUG (found 2026-09-01 while verifying the ILIKE fix on the deployed
container). With ILIKE repaired, the owner's question got one step further and
Salesforce rejected it again:

    Salary__c, Assigned_Recruiter__r.Name, Marketing__r.Name, ...
                                           ^
    Didn't understand relationship 'Marketing__r'

Three separate faults behind one error:

1. `sf_dictionary.hint_for()` returned NOTHING (0 characters) for the
   question, so `write_soql` fell back to `_object_hint()` — a bare list of
   object NAMES. The model was writing SOQL against a 278-field object knowing
   only that the object existed.
2. It therefore guessed: Marketing__c and Candidate_Training__c do not exist
   on Account, and Salary__c is a percent field, not a lookup. `X__c` is not
   automatically a relationship — only 14 of Account's fields are.
3. The self-repair pass never fired. It matched only "No such column", and a
   relationship error is not that, so the query failed once and gave up.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import salesforce as sf
from app.engines import live_sf
from app.engines import sql as sqleng


ACCOUNT = {
    "name": "Account",
    "fields": [
        {"name": "Id", "type": "id", "relationshipName": ""},
        {"name": "Name", "type": "string", "relationshipName": ""},
        {"name": "Salary__c", "type": "percent", "relationshipName": ""},
        {
            "name": "Assigned_Recruiter__c",
            "type": "reference",
            "relationshipName": "Assigned_Recruiter__r",
        },
        {
            "name": "Default_Vendor__c",
            "type": "reference",
            "relationshipName": "Default_Vendor__r",
        },
    ],
}


@pytest.fixture()
def described(monkeypatch):
    """Stub the describe API and count the calls it receives."""
    calls = []

    async def fake(name, **kw):
        calls.append(name)
        if name != "Account":
            raise RuntimeError(f"no such object {name}")
        return ACCOUNT

    monkeypatch.setattr(sf, "describe_object", fake)
    return calls


# ── Reading the object out of the query ────────────────────────────────────


def test_the_outer_object_is_found():
    assert live_sf._from_object("SELECT Id FROM Account WHERE Name = 'x'") == "Account"


def test_a_child_subquery_does_not_hijack_the_object():
    """`(SELECT Id FROM Contacts)` has its own FROM; repairing against
    "Contacts" would describe the wrong thing."""
    soql = "SELECT Id, (SELECT Id FROM Contacts) FROM Account WHERE Name = 'x'"
    assert live_sf._from_object(soql) == "Account"


def test_nested_subqueries_are_stripped():
    soql = "SELECT Id, (SELECT Id, (SELECT Id FROM Notes) FROM Contacts) FROM Lead"
    assert live_sf._from_object(soql) == "Lead"


def test_an_unreadable_query_yields_no_object():
    assert live_sf._from_object("nonsense") == ""
    assert live_sf._from_object("") == ""


# ── Grounding: the real fields and the real traversals ─────────────────────


def test_the_real_fields_are_supplied(described):
    block = asyncio.run(live_sf._fields_and_relationships(["Account"]))
    assert "Salary__c" in block and "Assigned_Recruiter__c" in block
    assert "EXACTLY these 5" in block


def test_only_real_relationships_are_offered(described):
    block = asyncio.run(live_sf._fields_and_relationships(["Account"]))
    assert "Assigned_Recruiter__r" in block
    assert "Default_Vendor__r" in block
    # THE regression: the two the model invented, and the percent field it
    # traversed, must not appear as relationships.
    assert "Marketing__r" not in block
    assert "Candidate_Training__r" not in block
    assert "Salary__r" not in block


def test_the_model_is_told_that_c_is_not_automatically_a_lookup(described):
    """The specific reasoning error: Salary__c -> Salary__r."""
    block = asyncio.run(live_sf._fields_and_relationships(["Account"]))
    assert "not automatically" in block.lower()


def test_duplicate_objects_are_described_once(described):
    asyncio.run(live_sf._fields_and_relationships(["Account", "Account"]))
    assert described == ["Account"]


def test_the_number_of_described_objects_is_bounded(described):
    """A describe is thousands of characters; an unbounded list would bury the
    question under a schema dump."""
    asyncio.run(live_sf._fields_and_relationships(["Account", "Contact", "Lead"]))
    assert len(described) <= live_sf._MAX_DESCRIBED_OBJECTS


def test_a_failing_describe_is_not_fatal(described):
    """Grounding is an optimisation — a describe that 404s must not stop the
    query being written."""
    block = asyncio.run(live_sf._fields_and_relationships(["Nope__c"]))
    assert block == ""


def test_no_objects_means_no_block(described):
    assert asyncio.run(live_sf._fields_and_relationships([])) == ""
    assert described == []


# ── The repair pass ────────────────────────────────────────────────────────


def test_a_relationship_error_is_repairable(described):
    """THE regression: this error used to fall straight through to raise."""
    exc = sf.SalesforceUnavailable(
        "Salesforce rejected the query: Didn't understand relationship "
        "'Marketing__r' in field path"
    )
    hint = asyncio.run(
        live_sf._repair_hint("SELECT Marketing__r.Name FROM Account", exc)
    )
    assert hint, "the relationship error produced no correction"
    assert "Marketing__r' is not a relationship on Account" in hint
    assert "Assigned_Recruiter__r" in hint


def test_a_missing_column_is_still_repairable(described):
    """The behaviour that already worked must survive the refactor."""
    exc = sf.SalesforceUnavailable("No such column 'Nope__c' on entity 'Account'")
    hint = asyncio.run(live_sf._repair_hint("SELECT Nope__c FROM Account", exc))
    assert "Account has ONLY these fields" in hint
    assert "Salary__c" in hint


def test_an_unrepairable_error_produces_no_retry(described):
    """A timeout or a permissions failure is not fixed by re-prompting, and a
    blind retry costs a minute of model time to fail identically."""
    exc = sf.SalesforceUnavailable("Read timed out")
    assert asyncio.run(live_sf._repair_hint("SELECT Id FROM Account", exc)) == ""


def test_a_relationship_error_on_an_unreadable_query_is_not_repaired(described):
    exc = sf.SalesforceUnavailable("Didn't understand relationship 'X__r'")
    assert asyncio.run(live_sf._repair_hint("garbage", exc)) == ""


def test_fetch_live_retries_and_succeeds(monkeypatch, described):
    """End to end: reject once on a bad relationship, succeed on the retry."""
    attempts = []

    async def fake_write(question, history=(), correction=""):
        attempts.append(correction)
        return (
            "SELECT Marketing__r.Name FROM Account"
            if not correction
            else "SELECT Name FROM Account"
        )

    async def fake_run(soql, **kw):
        if "Marketing__r" in soql:
            raise sf.SalesforceUnavailable(
                "Didn't understand relationship 'Marketing__r' in field path"
            )
        return soql, [{"Name": "Samyukth - challa"}]

    monkeypatch.setattr(live_sf, "write_soql", fake_write)
    monkeypatch.setattr(live_sf.salesforce, "run_soql", fake_run)

    soql, rows = asyncio.run(live_sf.fetch_live("everything about samyukt challa"))
    assert rows == [{"Name": "Samyukth - challa"}]
    assert len(attempts) == 2, "expected exactly one repair pass"
    assert "Assigned_Recruiter__r" in attempts[1]


# ── write_soql wires the grounding in ──────────────────────────────────────


def test_write_soql_grounds_on_the_resolved_object_schema(monkeypatch, described):
    """The person resolves to Account, so Account's REAL fields must reach the
    model — this is what was missing entirely."""
    monkeypatch.setattr(
        sqleng,
        "resolve_people",
        lambda q: [
            {
                "asked": "samyukt challa",
                "meaning": "a Person Account",
                "matches": ["Samyukth - challa"],
                "object": "Account",
            }
        ],
    )
    seen = {}

    async def fake_chat(messages, **kw):
        seen["user"] = messages[-1]["content"]
        return "SELECT Name FROM Account"

    monkeypatch.setattr(live_sf.llm, "chat_completion", fake_chat)
    asyncio.run(live_sf.write_soql("everything about samyukt challa"))

    assert "Assigned_Recruiter__r" in seen["user"], "no relationship grounding"
    assert "EXACTLY these 5" in seen["user"], "no field grounding"
    assert "FIELDS(ALL)" in seen["user"], "no open-ended guidance"
    assert "Samyukth - challa" in seen["user"], "person grounding was lost"


# ── The describe cache ─────────────────────────────────────────────────────


def test_describes_are_cached(monkeypatch):
    """Grounding puts a describe on every person question; without a cache
    that is a REST round trip per question for data that changes on a release
    cadence."""
    sf.clear_describe_cache()
    calls = []

    async def fake_get(path):
        calls.append(path)
        return {"name": "Account", "fields": [{"name": "Id", "type": "id"}]}

    monkeypatch.setattr(sf, "_get", fake_get)
    first = asyncio.run(sf.describe_object("Account"))
    second = asyncio.run(sf.describe_object("Account"))
    assert first == second
    assert len(calls) == 1, f"describe was fetched {len(calls)} times"


def test_the_cache_can_be_bypassed(monkeypatch):
    sf.clear_describe_cache()
    calls = []

    async def fake_get(path):
        calls.append(path)
        return {"name": "Account", "fields": []}

    monkeypatch.setattr(sf, "_get", fake_get)
    asyncio.run(sf.describe_object("Account"))
    asyncio.run(sf.describe_object("Account", refresh=True))
    assert len(calls) == 2


def test_the_cache_never_serves_a_bad_object_name(monkeypatch):
    """Validation must stay in front of the cache."""
    sf.clear_describe_cache()
    with pytest.raises(sf.UnsafeSoql):
        asyncio.run(sf.describe_object("Account; DROP"))


# ── The reasoning budget ───────────────────────────────────────────────────


def test_soql_generation_does_not_spend_its_budget_thinking(monkeypatch, described):
    """Reasoning and output share one budget, and the schema grounding made
    the prompt ~9,800 tokens.

    Found on the deployed container: `write_soql` returned "the model did not
    produce a SOQL query" — the whole 6,000-token allowance went on
    deliberation and nothing came back. Re-running the IDENTICAL messages at
    the IDENTICAL budget produced a perfect query, so it fails intermittently,
    which is the worst way for it to fail.

    Writing a query from a schema is translation, not deduction. The warehouse
    path (`sql._ask_sql`) has passed thinking=False since it hit this same
    wall; the live path raised max_tokens instead, which only bought room.
    """
    seen = {}

    async def fake_chat(messages, **kw):
        seen.update(kw)
        return "SELECT Id FROM Account"

    monkeypatch.setattr(live_sf.llm, "chat_completion", fake_chat)
    asyncio.run(live_sf.write_soql("everything about samyukt challa"))
    assert seen.get("thinking") is False, "the reasoning pass is still on"


def test_the_warehouse_path_also_keeps_thinking_off():
    """Both query writers must agree; this is the invariant that was violated."""
    import inspect

    from app.engines import sql as sqlmod

    source = inspect.getsource(sqlmod._ask_sql)
    assert "thinking=False" in source


# ── FIELDS(ALL): the right way to answer "everything about X" ──────────────


def test_open_ended_questions_are_steered_to_fields_all(described):
    """Hand-picking N of 278 field names is inherently fragile.

    Measured against the live org, three runs of the owner's question: the
    model listed fields until it ran past the token limit and the query was
    cut off before FROM ("query has no FROM clause"), invented Recruiter__c on
    Account, and succeeded once with 26 columns — 1/3, at up to 82 seconds.
    With FIELDS(ALL): 3/3, 1-5 seconds, all 278 fields.
    """
    block = asyncio.run(live_sf._fields_and_relationships(["Account"]))
    assert "SELECT FIELDS(ALL) FROM Account" in block
    assert "LIMIT 200" in block
    assert "Do NOT enumerate fields" in block


def test_the_field_list_is_still_supplied_for_targeted_questions(described):
    """FIELDS(ALL) answers "everything about X"; "what is X's email" still
    needs a real field name, and inventing one is the original bug."""
    block = asyncio.run(live_sf._fields_and_relationships(["Account"]))
    assert "TARGETED" in block
    assert "Salary__c" in block and "Assigned_Recruiter__c" in block


def test_the_person_block_asks_for_fields_all_in_soql(monkeypatch):
    monkeypatch.setattr(
        sqleng,
        "resolve_people",
        lambda q: [
            {
                "asked": "samyukt challa",
                "meaning": "a Person Account",
                "matches": ["Samyukth - challa"],
                "object": "Account",
            }
        ],
    )
    block = sqleng.who_these_people_are("everything about samyukt challa", "soql")
    assert "FIELDS(ALL)" in block
    # The warehouse wording must not leak into the SOQL block.
    assert "joined to" not in block


def test_guard_soql_leaves_fields_all_intact():
    """FIELDS(ALL) REQUIRES a LIMIT of at most 200. The guard already caps at
    200, so the two agree — but a guard that rewrote the projection, or raised
    the cap, would break every open-ended live answer."""
    q = "SELECT FIELDS(ALL) FROM Account WHERE Name = 'Samyukth - challa' LIMIT 200"
    out = sf.guard_soql(q)
    assert "FIELDS(ALL)" in out
    assert out.rstrip().endswith("LIMIT 200")


def test_fields_all_without_a_limit_is_given_a_legal_one():
    out = sf.guard_soql("SELECT FIELDS(ALL) FROM Account WHERE Name = 'x'")
    assert "FIELDS(ALL)" in out
    limit = int(out.rsplit("LIMIT", 1)[1])
    assert 0 < limit <= 200, f"FIELDS(ALL) rejects LIMIT > 200, got {limit}"
