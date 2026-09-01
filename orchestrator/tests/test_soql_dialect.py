"""SOQL is not warehouse SQL, and the org brief is written in warehouse SQL.

THE BUG (owner report, 2026-09-01, reproduced live). With the "Live
Salesforce" toggle on, "give me entire information about samyukt challa
candidate" answered:

    "I could not write a valid query for that — I tried twice and the second
     attempt was still wrong, so nothing was run."

Two independent causes, both on the LIVE path only — the warehouse path was
fixed the day before and was never reached, because `run_sql_engine` diverts
to Salesforce whenever the toggle is set.

1. ILIKE. `org_brief.grounding_for(dialect="soql")` shipped the brief
   unchanged, and the brief teaches `Name ILIKE '%surname%'` in nine places.
   SOQL has no ILIKE, so Salesforce rejected the whole query:

       RecordType.Name = 'Person Account' AND Name ILIKE '%Challa%' LIMIT 200
                                                ^
       ERROR at Row:1:Column:239  unexpected token: 'Name ILIKE'

   Reproduced twice in a row with two different outcomes at temperature 0.0 —
   one generation wrote LIKE, the next wrote ILIKE. A coin flip, which is why
   it looked intermittent.

2. No person lookup. `write_soql` grounded on field names and org rules but
   never resolved WHO the question was about, so it filtered on the user's
   spelling. The org stores "Samyukth - challa"; "samyukt challa" matches
   nothing, and a query that runs and returns nothing reads as "this person
   has no records" — worse than failing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import org_brief as ob
from app.core import salesforce as sf
from app.engines import live_sf
from app.engines import sql as sqleng


# ── The sanitiser: the one place every live query passes through ────────────


def test_ilike_is_rewritten_to_like():
    """THE regression, at the choke point. SOQL has no ILIKE."""
    out = sf.guard_soql("SELECT Id FROM Account WHERE Name ILIKE '%challa%'")
    assert "ILIKE" not in out.upper()
    assert "Name LIKE '%challa%'" in out


def test_the_exact_query_salesforce_rejected_now_survives():
    soql = (
        "SELECT Id, Name FROM Account WHERE RecordType.Name = 'Person Account' "
        "AND Name ILIKE '%Challa%'"
    )
    out = sf.guard_soql(soql)
    assert "ILIKE" not in out.upper()
    assert "RecordType.Name = 'Person Account'" in out


def test_the_rewrite_is_exact_not_a_loosening():
    """SOQL's LIKE is already case-insensitive, so nothing is given up."""
    assert "case-insensitive" in ob.SOQL_TRANSLATION or "case-insensitive" in (
        ob.grounding_for("who is challa", dialect="soql")
    )


def test_a_quoted_literal_is_never_rewritten():
    """A search term is user data, not syntax.

    Someone searching for the word ILIKE must still be searching for it.
    """
    out = sf.guard_soql("SELECT Id FROM Account WHERE Name LIKE '%ILIKE%'")
    assert "'%ILIKE%'" in out


def test_an_escaped_quote_does_not_desynchronise_the_scan():
    """`O\\'Brien` must not shift the parser's idea of what is quoted."""
    out = sf.guard_soql(
        "SELECT Id FROM Account WHERE Name = 'O\\'Brien' AND City ILIKE '%pune%'"
    )
    assert "City LIKE '%pune%'" in out
    assert "ILIKE" not in out.upper()


def test_lowercase_ilike_is_caught_too():
    out = sf.guard_soql("SELECT Id FROM Lead WHERE Name ilike '%challa%'")
    assert "ilike" not in out.lower()


def test_a_similar_field_name_is_not_mangled():
    """The rewrite is word-bounded; a column called Dislike survives."""
    out = sf.guard_soql("SELECT Id, Dislike__c FROM Account WHERE Dislike__c = true")
    assert "Dislike__c" in out


# ── The brief: stop teaching ILIKE to the dialect that has no ILIKE ─────────


def test_the_soql_brief_never_teaches_an_ilike_predicate():
    """No worked EXAMPLE may use ILIKE.

    The one permitted mention is the prohibition itself ("There is no
    ILIKE"), which is why this checks per line rather than for the substring:
    every other occurrence is a pattern the model will copy.
    """
    for question in (
        "give me entire information about samyukt challa candidate",
        "how many oot mocks were taken today",
        "list the interviews scheduled this week",
        "how many internal interviews has rakshit bodakuntla completed",
    ):
        brief = ob.grounding_for(question, dialect="soql")
        taught = [
            line
            for line in brief.splitlines()
            if "ILIKE" in line.upper() and "there is no ilike" not in line.lower()
        ]
        assert not taught, (question, taught[:3])


def test_the_warehouse_brief_still_teaches_ilike():
    """The rewrite is dialect-scoped; DuckDB's LIKE is case-SENSITIVE, so the
    warehouse path losing ILIKE would be a real regression."""
    brief = ob.grounding_for("everything about samyukt challa", dialect="sql")
    assert "ILIKE" in brief.upper()


def test_the_person_rule_survives_the_rewrite():
    """Rewriting the operator must not drop the rule that uses it."""
    brief = ob.grounding_for("everything about samyukt challa", dialect="soql")
    assert "NEVER match a person by an equals" in brief
    assert "LIKE '%<surname>%'" in brief


def test_the_soql_brief_says_like_is_already_case_insensitive():
    """Without this the model has a rule with no operator to apply it with."""
    brief = ob.grounding_for("everything about samyukt challa", dialect="soql")
    assert "case-insensitive" in brief


# ── Person grounding on the live path ──────────────────────────────────────


class _Resolved:
    """`who_these_people_are` with the warehouse stubbed out."""

    def __init__(self):
        self.asked_with = None

    def __call__(self, question, dialect="sql"):
        self.asked_with = (question, dialect)
        return "STORED NAME: 'Samyukth - challa'\nFILTER WITH: Name = 'Samyukth - challa'"


def test_write_soql_grounds_on_who_the_person_is(monkeypatch):
    """THE second regression: the live path never looked the person up."""
    resolver = _Resolved()
    monkeypatch.setattr(sqleng, "who_these_people_are", resolver)
    seen = {}

    async def fake_chat(messages, **kw):
        seen["user"] = messages[-1]["content"]
        return "SELECT Id, Name FROM Account WHERE Name = 'Samyukth - challa'"

    monkeypatch.setattr(live_sf.llm, "chat_completion", fake_chat)
    asyncio.run(live_sf.write_soql("everything about samyukt challa"))

    assert resolver.asked_with is not None, "the live path did not resolve anybody"
    assert resolver.asked_with[1] == "soql", "resolved in the wrong dialect"
    assert "Samyukth - challa" in seen["user"], "the stored spelling never reached the model"


def test_a_broken_warehouse_does_not_break_live_salesforce(monkeypatch):
    """Grounding is an optimisation. The warehouse is write-locked by the sync
    worker for a large part of the day, and that is precisely WHY questions
    land on the live path — it must not depend on the warehouse being up."""

    def boom(question, dialect="sql"):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sqleng, "who_these_people_are", boom)

    async def fake_chat(messages, **kw):
        return "SELECT Id FROM Account"

    monkeypatch.setattr(live_sf.llm, "chat_completion", fake_chat)
    assert asyncio.run(live_sf.write_soql("everything about samyukt challa"))


def test_the_soql_person_block_does_not_tell_the_model_to_join(monkeypatch):
    """SOQL has no JOIN; it traverses relationships with __r."""
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
    soql_block = sqleng.who_these_people_are("everything about samyukt challa", "soql")
    sql_block = sqleng.who_these_people_are("everything about samyukt challa", "sql")

    assert "__r" in soql_block
    assert "UNION" not in soql_block.upper()
    # This block is appended AFTER grounding_for has done its ILIKE rewrite,
    # so it is never normalised — it has to be born correct.
    assert "ILIKE" not in soql_block.upper()
    assert "joined to" in sql_block, "the warehouse wording must be unchanged"
    # The predicate is dialect-independent and must survive in both.
    for block in (soql_block, sql_block):
        assert "FILTER WITH: Name = 'Samyukth - challa'" in block
