"""Finding the person a question is about, however the user spelled them.

THE BUG (owner report, 2026-09-01). "give me entire information about samyukt
challa candidate" answered:

    "I could not write a valid query for that — I tried twice and the second
     attempt was still wrong, so nothing was run."

The org holds that person three times over — Account and Contact as
"Samyukth - challa", Lead and Opportunity as "Samyukth challa". The warehouse
lookup that exists precisely to tell the SQL model who a name refers to never
ran, because `people_in_question` required two adjacent CAPITALISED words and
the user typed lowercase. With nobody resolved the model had no idea which
object held the person, wrote SQL against a table that does not exist, failed,
retried, failed again.

Two independent gaps, so two independent fixes and two sets of tests:
detection must not depend on capitalisation, and matching must survive a
misspelling.
"""
from __future__ import annotations

import pytest

from app.engines import sql as sqleng


# ── Detection ───────────────────────────────────────────────────────────────


def test_a_lowercase_name_is_detected():
    """THE regression. Users do not capitalise; the lookup must not care."""
    found = sqleng.name_candidates(
        "give me entire information about samyukt challa candidate ??"
    )
    assert "samyukt challa" in [n.lower() for n in found]


def test_the_capitalised_path_still_works():
    found = sqleng.people_in_question("How many interviews has Monica Challa sat?")
    assert "Monica Challa" in found


def test_org_vocabulary_is_never_offered_as_a_name():
    """"interview slot", "training program" are things, not people."""
    for question in (
        "how many interview slots are open",
        "show me the training program list",
        "candidate report for this week",
    ):
        offered = [n.lower() for n in sqleng.name_candidates(question)]
        for junk in ("interview slot", "training program", "candidate report"):
            assert junk not in offered, f"{junk!r} proposed for {question!r}"


def test_english_filler_is_never_offered_as_a_name():
    """The generous lowercase pass must not propose "entire information"."""
    offered = [
        n.lower()
        for n in sqleng.name_candidates(
            "give me entire information about samyukt challa candidate"
        )
    ]
    for junk in ("entire information", "information about", "give entire"):
        assert junk not in offered, f"{junk!r} was proposed"


def test_a_metric_question_names_nobody():
    """Detection is generous, but not indiscriminate.

    Over-proposing costs a warehouse query per pair; proposing on EVERY
    question would put that cost on the metric path, which has no names in it.
    """
    for question in (
        "how many payments were made last month",
        "total revenue by quarter",
        "count interviews by status",
    ):
        assert sqleng.name_candidates(question) == [], question


def test_detection_is_bounded():
    """A question listing many names is a report, not a lookup."""
    question = " ".join(f"Firstname{i} Lastname{i}" for i in range(20))
    assert len(sqleng.name_candidates(question)) <= sqleng._MAX_RESOLVED_PEOPLE


# ── Resolution against the warehouse ────────────────────────────────────────


class _FakeCon:
    """Stands in for the DuckDB connection.

    `like_rows` answers the substring queries; `fuzzy_rows` answers the
    Jaro-Winkler fallback, which is recognised by the function name appearing
    in the SQL text.
    """

    def __init__(self, like_rows=None, fuzzy_rows=None):
        self.like_rows = like_rows or {}
        self.fuzzy_rows = fuzzy_rows or []
        self.saw_fuzzy = False

    def execute(self, sql, params=None):
        self.last_sql = sql
        if "jaro_winkler_similarity" in sql:
            self.saw_fuzzy = True
            self._rows = list(self.fuzzy_rows)
            return self
        for table, rows in self.like_rows.items():
            if f'FROM {table}' in sql or f'FROM "{table}"' in sql:
                self._rows = list(rows)
                return self
        self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


@pytest.fixture()
def fake_warehouse(monkeypatch):
    def install(con):
        monkeypatch.setattr(sqleng, "_connect_warehouse", lambda _duckdb: con)
        return con

    return install


def test_the_exact_production_question_now_resolves(fake_warehouse):
    fake_warehouse(_FakeCon(like_rows={"Account": [("Samyukth - challa",)]}))
    found = sqleng.resolve_people(
        "give me entire information about samyukt challa candidate ??"
    )
    assert found, "the person must be resolved"
    assert found[0]["matches"] == ["Samyukth - challa"]
    assert found[0]["object"] == "Account"


def test_a_misspelling_falls_back_to_fuzzy(fake_warehouse):
    """Substring matching is exact about spelling; people are not."""
    con = fake_warehouse(
        _FakeCon(
            like_rows={},  # nothing matches the literal pattern
            fuzzy_rows=[("Lead", "Samyukth challa", 0.96)],
        )
    )
    found = sqleng.resolve_people("tell me about samyukth chala")
    assert con.saw_fuzzy, "the fuzzy fallback should have run"
    assert found and found[0]["matches"] == ["Samyukth challa"]
    assert found[0].get("fuzzy") is True


def test_fuzzy_runs_only_after_the_cheap_path_fails(fake_warehouse):
    """A scan of every name column must not be on the common path."""
    con = fake_warehouse(_FakeCon(like_rows={"Account": [("Monica Challa",)]}))
    sqleng.resolve_people("everything about monica challa")
    assert not con.saw_fuzzy, "substring matched; the scan was unnecessary"


def test_a_weak_fuzzy_match_is_discarded(fake_warehouse):
    """A confidently wrong name is worse than no grounding at all.

    The SQL query filters below the threshold, so the fallback returning
    nothing must produce no entry rather than an empty-matches one.
    """
    con = fake_warehouse(_FakeCon(like_rows={}, fuzzy_rows=[]))
    assert sqleng.resolve_people("tell me about zzzzz qqqqq") == []
    assert con.saw_fuzzy


def test_the_prompt_block_states_the_stored_spelling(fake_warehouse):
    """What the model needs is the spelling the DATA uses, not the user's."""
    fake_warehouse(_FakeCon(like_rows={"Account": [("Samyukth - challa",)]}))
    block = sqleng.who_these_people_are(
        "give me entire information about samyukt challa candidate"
    )
    assert "Samyukth - challa" in block
    assert "Person Account" in block
    # The block used to advise matching "case-insensitively (ILIKE)". That was
    # too weak: the model kept building the LIKE pattern from the QUESTION.
    # It now hands over an exact predicate instead, which is a stronger
    # contract — so this asserts the predicate, not the old advice.
    assert "FILTER WITH:" in block


def test_no_people_means_no_prompt_block(fake_warehouse):
    fake_warehouse(_FakeCon())
    assert sqleng.who_these_people_are("how many payments last month") == ""


def test_an_unreachable_warehouse_is_not_fatal(monkeypatch):
    """Grounding is an optimisation; a locked warehouse must not break SQL."""

    def boom(_duckdb):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sqleng, "_connect_warehouse", boom)
    assert sqleng.resolve_people("everything about monica challa") == []


def test_contact_and_lead_are_searched_too():
    """The same human is a Contact, a Lead and an Account at different stages.

    "Everything about X" that resolves only to Account silently hides the rest
    of the record, which is what the owner actually asked for.
    """
    tables = {table for table, _sql, _meaning in sqleng._PERSON_SOURCES}
    assert {"Account", "Contact", "Lead"} <= tables


# ── The SQL the resolution has to produce ───────────────────────────────────


def test_the_prompt_supplies_a_ready_made_filter(fake_warehouse):
    """Naming the stored spelling was not enough.

    Told "Stored as: Samyukth - challa", the model still wrote
    `WHERE a.Name ILIKE '%samyukt challa%'` from the user's wording. That
    matches nothing (the stored form has "h - " in the middle), so the query
    RAN and returned zero rows — which reads as "this person has no records",
    the worst possible outcome. The block now hands over the predicate.
    """
    fake_warehouse(_FakeCon(like_rows={"Account": [("Samyukth - challa",)]}))
    block = sqleng.who_these_people_are("everything about samyukt challa")
    assert "FILTER WITH: Name = 'Samyukth - challa'" in block
    assert "VERBATIM" in block


def test_several_stored_spellings_become_an_IN_list(fake_warehouse):
    fake_warehouse(
        _FakeCon(like_rows={"Account": [("Samyukth - challa",), ("Samyukth challa",)]})
    )
    block = sqleng.who_these_people_are("everything about samyukt challa")
    assert "FILTER WITH: Name IN (" in block
    assert "'Samyukth - challa'" in block and "'Samyukth challa'" in block


def test_open_ended_questions_are_steered_away_from_mega_unions(fake_warehouse):
    """"Everything about X" produced 180 lines and a Binder Error on its own
    CTE alias — a UNION of a dozen objects flattened into Detail1..Detail5."""
    fake_warehouse(_FakeCon(like_rows={"Account": [("Samyukth - challa",)]}))
    block = sqleng.who_these_people_are("give me entire information about samyukt challa")
    lowered = block.lower()
    assert "do not build a union" in lowered
    assert "detail1" in lowered


def test_the_system_prompt_forbids_keyword_table_aliases():
    """DuckDB reserves AT for time travel; a trainer table aliased `at`
    produced `syntax error at or near "."` — a message that never names the
    offending word, so the retry could not fix it either."""
    system = sqleng._SQL_SYSTEM.lower()
    assert "aliases must not be sql keywords" in system
    assert " at," in system
