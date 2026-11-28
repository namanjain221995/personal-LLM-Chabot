"""sf_assistant.tools.record_tools: the 14 record tools on the synthetic warehouse (facts, errors, safety, robustness, evidence)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from sf_assistant.config import QUESTIONS_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine.types import DATA_TOOLS, ORIENTATION_TOOLS  # noqa: E402
from sf_assistant.tools.record_base import MAX_RESULT_CHARS  # noqa: E402
from sf_assistant.tools.record_tools import TOOL_ORDER  # noqa: E402
from warehouse_fixture import FACTS, open_fixture  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")


def _open(tmp, **kw):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        return open_fixture(tmp, **kw)


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    t = _open(tmp_path_factory.mktemp("t"))
    yield t
    t.close()


def ok(tools, name, args):
    out = tools.call(name, args)
    assert out["ok"], out["error"]
    assert set(out) == {"ok", "result", "error", "chunk_ids"}
    assert len(json.dumps(out)) <= MAX_RESULT_CHARS + 2000
    return out["result"]


def err(tools, name, args) -> str:
    out = tools.call(name, args)
    assert out["ok"] is False and out["result"] is None and out["chunk_ids"] == [], out
    return out["error"]


F = lambda f, v, op="eq": {"field": f, "op": op, "value": v}  # noqa: E731
_RECORDS_TXT = QUESTIONS_DIR / "records.txt"   # git-ignored (.private/questions): read only when it is there
GOLDEN_QUESTIONS = ([ln.strip() for ln in _RECORDS_TXT.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")] if _RECORDS_TXT.is_file() else [])
needs_records_txt = pytest.mark.skipif(not _RECORDS_TXT.is_file(),
                                       reason="private question file .private/questions/records.txt is not there")


# ----------------------------------------------------------------------------------- registry surface

def test_registry_surface(tools):
    assert list(tools.tools) == list(TOOL_ORDER) and len(TOOL_ORDER) == 14
    assert set(TOOL_ORDER) == (DATA_TOOLS - {"find_references"}) | ORIENTATION_TOOLS   # find_references: alias
    mcp = tools.list_tools()
    assert all(set(t) == {"name", "description", "inputSchema"} for t in mcp)
    oa = tools.openai_tools()
    assert all(t["type"] == "function" and set(t["function"]) == {"name", "description", "parameters"} for t in oa)
    # the §9.1 fragments inlined in 14 schemas: about 41k characters, 42.6k with trend_records part_filters (2026-11-16),
    # 44.1k with the at-most count of a related condition (round 2 of the pre-release fixes, 2026-11-28)
    assert len(json.dumps(oa)) < 44_500
    assert "$ref" not in json.dumps(oa)


def test_status_line_and_warning(tools):
    st = tools.status()
    assert st.origin == "preprod SANDBOX copy" and st.org_is_sandbox is True
    assert st.time_zone == "America/New_York" and st.time_zone_from == "Organization.TimeZoneSidKey"
    assert st.today == "2026-10-13" and st.today_pinned and st.objects == 16
    line = st.line()
    assert line.startswith("records: ") and "| preprod SANDBOX copy |" in line and "(file time)" in line
    assert "today 2026-10-13 pinned (America/New_York) | 16 objects" in line
    assert st.newest_sync_utc == "2026-10-13 14:30 UTC"


# ----------------------------------------------------------------------------------- each tool

def test_find_objects(tools):
    r = ok(tools, "find_objects", {"query": "client interviews"})
    assert r["objects"][0]["object"] == "Interview__c" and r["objects"][0]["records"] == FACTS["interviews"]
    assert any("Internal_Interview__c = internal mock interviews" in n for n in r["notes"])
    r = ok(tools, "find_objects", {"query": "locked deliverables"})
    assert {"object": "Deliverable__c", "field": "Status__c", "value": "Locked", "records": 3} in r["value_matches"]
    assert err(tools, "find_objects", {}).startswith("find_objects needs 'query'")


def test_describe_fields(tools):
    r = ok(tools, "describe_fields", {"object": "Interview__c", "query": "offer"})
    names = [f["field"] for f in r["fields"]]
    assert "Interview_Outcome__c" in names[:3]
    oc = next(f for f in r["fields"] if f["field"] == "Interview_Outcome__c")
    assert {"value": "Offer Received", "records": 2, "meaning": "The company made an offer."} in oc["values"]
    assert any(v["value"] == "Reference Check" and v["records"] == 0 for v in oc["values"])
    assert {"field": "Interview_Outcome__c", "value": "Offer Received", "records": 2, "in_picklist": True} in \
        r["matching_values"]
    assert r["date_fields"][0]["field"] == "Date_of_Interview__c" and r["date_fields"][0]["temporal_key"] == "interview_date"
    assert r["date_fields"][-1]["field"] in ("CreatedDate", "LastModifiedDate")
    assert "Zoom_Passcode__c" in r["not_queryable"]["sensitive"]
    assert "Old_Status__c" in r["not_queryable"]["documented_not_synced"]
    assert {"name": "Initial Call", "developer_name": "Initial_Call", "records": 1} in r["record_types"]
    r = ok(tools, "describe_fields", {"object": "Interview__c"})
    assert "pass query to see meanings and values" in r["note"] and len(r["fields"]) <= 80
    assert all(f["field"] != "Zoom_Passcode__c" for f in r["fields"])


def test_get_field_values(tools):
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Interview_Status__c"})
    vals = {v["value"]: v for v in r["values"]}
    assert vals["Completed"]["records"] == 5 and vals["Completed"]["status"] == "current"
    assert vals["Offer Received"]["status"] == "legacy"
    assert r["unused_picklist_values"] == ["Rescheduled"]
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Employment_Type__c"})
    assert any("knowledge spells it 'Full-Time'" in n for n in r["notes"])
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Recruiter__c"})
    vals = {v["value"]: v["records"] for v in r["values"]}
    assert vals == {"Recruiter A": 4, "Recruiter B (#1)": 2, "Recruiter B (#2)": 1, "Recruiter C": 1}
    assert r["unresolved"] == 1 and r["blank_records"] == 1
    r = ok(tools, "get_field_values", {"object": "Internal_Interview__c", "field": "Human_Total_Score__c"})
    assert (r["min"], r["max"], r["avg"], r["records_with_value"], r["blank_records"]) == (20.5, 30.0, 25.17, 3, 3)
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Date_of_Interview__c"})
    assert (r["min"], r["max"]) == ("2026-05-20", "2026-10-20")


def test_count_records(tools):
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "offer received")]})
    assert r["count"] == FACTS["outcome_offer_received"]
    assert r["applied"]["filters"] == ["Interview_Outcome__c = 'Offer Received'"]
    assert any("also stored in Interview_Status__c" in n for n in r["notes"])
    assert any(n.startswith("includes all record types (Interview 8, Initial_Call 1") for n in r["notes"])
    assert r["source"].startswith("preprod sandbox copy; Interview__c synced 2026-10-13 14:30 UTC")
    r = ok(tools, "count_records", {"object": "Interview__c", "count_distinct": "Candidate__c"})
    assert r["count"] == 2 and r["counted"].startswith("different Candidate__c values")
    r = ok(tools, "count_records", {"object": "Interview_Participant__c"})
    assert r["count"] == 0 and "Interview_Participant__c has 0 records in the warehouse" in r["notes"]
    r = ok(tools, "count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "this month"})
    assert r["count"] == 3 and any("runs past today" in n for n in r["notes"])
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Nobody Here")]})
    assert e.startswith("no Employee (Recruiter__c) record is named 'Nobody Here'.")  # E4h: the count never runs
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c.Name", "recruiter a")]})
    assert r["count"] == 4
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("EndTime_Text__c", None, "lt") | {
        "other_field": "StartTime_Text__c"}]})
    assert r["count"] == 1


def test_aggregate_records(tools):
    r = ok(tools, "aggregate_records", {"object": "Internal_Interview__c", "op": "avg", "field": "Human_Total_Score__c"})
    assert (r["value"], r["records_with_value"], r["records_matched"]) == (FACTS["internal_avg_score"], 3, 6)
    assert r["records_without_value"] == 3  # stated by the tool, so answers need no subtraction
    r = ok(tools, "aggregate_records", {"object": "Internal_Interview__c", "op": "median", "field": "Human_Total_Score__c"})
    assert r["value"] == 25.0
    r = ok(tools, "aggregate_records", {"object": "Interview__c", "op": "max", "field": "Date_of_Interview__c"})
    assert r["value"] == "2026-10-20"
    e = err(tools, "aggregate_records", {"object": "Interview__c", "op": "avg", "field": "Interview_Status__c"})
    assert e.startswith("avg needs a number field") and "Client_Bill_Rate__c" in e


def test_group_records(tools):
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Recruiter__c"})
    assert {g["group"]: g["value"] for g in r["groups"]} == FACTS["recruiter_groups"]
    assert r["total_across_groups"] == 10 and r["unresolved_ids"] == 1 and r["grouped_by"] == "Recruiter__c (Employee name)"
    named = [g for g in FACTS["recruiter_groups"] if g not in ("(blank)", "(unresolved id)")]
    assert r["group_count"] == len(named)  # the (blank) and (unresolved id) groups are not counted
    assert r["blank_group_value"] == FACTS["recruiter_groups"]["(blank)"]
    assert any("'Name (#k)' numbers records that share a name, in Id order" in n for n in r["notes"])
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Recruiter__c", "top": 3})
    assert [g["group"] for g in r["groups"]] == ["Recruiter A", "Recruiter B (#1)", "Recruiter B (#2)"]
    assert r["ties_at_cutoff"] == ["Recruiter C"] and r["ties_at_cutoff_count"] == 1
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Interview_Status__c",
                                    "split_by": "Employment_Type__c"})
    comp = next(g for g in r["groups"] if g["group"] == "Completed")
    assert comp["value"] == 5 and comp["by"] == {"Full Time": 4, "Contract": 1}
    r = ok(tools, "group_records", {"object": "Candidate_Training__c", "group_by": "Program_Version__c.Program__c"})
    assert {g["group"]: g["value"] for g in r["groups"]} == FACTS["trainings_by_program"]
    r = ok(tools, "group_records", {"object": "Deliverable__c", "filters": [F("Status__c", "Locked")],
                                    "group_by": "Candidate_Training__c.Program_Version__c.Program__c"})
    assert {g["group"]: g["value"] for g in r["groups"]} == FACTS["locked_by_program"]
    assert any("Program__c was last synced 2026-10-10 08:00 UTC" in n for n in r["notes"])
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Interview_Status__c", "min_value": 2})
    assert {g["group"] for g in r["groups"]} == {"Completed", "Offer Received"}
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "OwnerId"})
    assert {g["group"]: g["value"] for g in r["groups"]} == {"User One": 9, "Queue One": 1}
    e = err(tools, "group_records", {"object": "Interview__c", "group_by": "Date_of_Interview__c"})
    assert "use trend_records with grain=month" in e
    e = err(tools, "group_records", {"object": "Interview__c", "group_by": "Status__c", "split_by": "Status__c"})
    assert "Closest" in e or "split_by must be a different field" in e


def test_value_also_stored_in_another_field_states_the_overlap(tools):
    """The fixture stores 'Offer Received' in the legacy Interview_Status__c of 2 records; 1 has the outcome too.

    The overlap is counted with every condition of the result (not only the one filter), and named after it.
    """
    also = "'Offer Received' is also stored in Interview_Status__c (2 records, legacy value); "
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [
        {"field": "Interview_Outcome__c", "op": "eq", "value": "offer received"}]})
    assert also + "1 of them matches all conditions of this query, the other 1 does not" in r["notes"]
    r = ok(tools, "compare_records", {"object": "Interview__c", "segments": [
        {"label": "offers", "filters": [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]},
        {"label": "rest", "filters": [{"field": "Interview_Outcome__c", "op": "ne", "value": "Offer Received"}]}]})
    assert also + "1 of them matches segment 'offers', the other 1 does not" in r["notes"]
    assert also + "1 of them matches segment 'rest', the other 1 does not" in r["notes"]  # the blank outcome is in 'rest'
    r = ok(tools, "percentage_of_records", {"object": "Interview__c", "part_filters": [
        {"field": "Interview_Outcome__c", "op": "in", "values": ["Offer Received"]}]})
    assert also + "1 of them matches the part, the other 1 does not" in r["notes"]
    ev = tools.evidence[-1]
    assert len(ev.statements) == 2 and ev.statements[0].sql.startswith("SELECT COUNT(*) FILTER")  # main stays first
    assert 'WHERE t0."Interview_Status__c" = ? AND ' in ev.statements[1].sql
    # Other filters and the period count too: the one legacy record with the outcome is a Contract interview of August.
    # Step-2 fixes of 2026-10-30 (F12, documented expectation update): none of them match, and one more COUNT with only
    # the value filter says why: the record that meets it fails another condition (the note never blames the filter,
    # nor names the period).
    why = ("none of them matches all conditions of this query: those that meet the filter "
           "Interview_Outcome__c = 'Offer Received' fail another condition")
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [
        F("Interview_Outcome__c", "Offer Received"), F("Employment_Type__c", "Full Time")]})
    assert r["count"] == 1 and also + why in r["notes"]
    assert not any("none of them meets the filter" in n for n in r["notes"])
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Offer Received")],
                                    "date_field": "Date_of_Interview__c", "period": "last month"})
    assert r["count"] == 1 and also + why in r["notes"]
    assert not any("none of them meets the filter" in n for n in r["notes"])
    assert len(tools.evidence[-1].statements) == 3   # the main statement, the overlap, the value filter alone
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                    "period": "last 3 months", "filters": [F("Interview_Outcome__c", "Offer Received")]})
    assert also + "1 of them matches all conditions of this query, the other 1 does not" in r["notes"]  # August


def test_an_overlap_of_none_says_when_the_value_filter_alone_leaves_them_out(tmp_path):
    """Step-2 fixes of 2026-10-30 (F12): a record agent read "none of them matches <result>" as "outside the period".
    Here the value is also stored in the other field only on records whose filtered field holds another value, so one
    more COUNT with only the value filter finds none of them either: the note says the filter leaves them out (count,
    segment and part forms). The counts, the main statement and the other notes stay; the extra COUNT is the last
    statement."""
    import duckdb

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records
    from warehouse_fixture import make_knowledge, make_warehouse

    wh = make_warehouse(tmp_path)
    con = duckdb.connect(str(wh))   # I-0005: the legacy status keeps 'Offer Received', the outcome no longer does
    con.execute('UPDATE raw."Interview__c" SET "Interview_Outcome__c" = \'Rejected\' WHERE "Name" = \'I-0005\'')
    con.close()
    kd = make_knowledge(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        import datetime as dt
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kd))
    also = "'Offer Received' is also stored in Interview_Status__c (2 records, legacy value); "
    meets = ": none of them meets the filter "
    try:
        r = ok(t, "count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Offer Received")]})
        note = next(n for n in r["notes"] if n.startswith(also))
        assert r["count"] == 1 and note == (also + "none of them matches all conditions of this query" + meets
                                            + "Interview_Outcome__c = 'Offer Received'")
        assert [n for n in r["notes"] if n != note] == [n for n in r["notes"] if not n.startswith(also)]
        ev = t.evidence[-1]
        assert len(ev.statements) == 3 and ev.statements[0].sql.startswith("SELECT COUNT(*)")   # main stays first
        assert ev.statements[-1].params == ["Offer Received", "Offer Received"]   # the value filter alone
        assert "fail another condition" not in note
        r = ok(t, "compare_records", {"object": "Interview__c", "segments": [
            {"label": "offers", "filters": [F("Interview_Outcome__c", "Offer Received")]},
            {"label": "rest", "filters": [F("Interview_Outcome__c", "Offer Received", "ne")]}]})
        assert any(n.startswith(also + "none of them matches segment 'offers'" + meets) for n in r["notes"])
        r = ok(t, "percentage_of_records", {"object": "Interview__c", "part_filters": [
            {"field": "Interview_Outcome__c", "op": "in", "values": ["Offer Received"]}]})
        assert any(n.startswith(also + "none of them matches the part" + meets) for n in r["notes"])
    finally:
        t.close()


def test_the_overlap_note_keeps_its_short_form_when_the_extra_count_fails(tmp_path, monkeypatch):
    """F12's fallback (review of 2026-10-30, TP3): when the extra COUNT raises a RecordError (the call's time budget is
    spent, E10), the note keeps its short form and the call still succeeds: a slow count never fails the tool call."""
    import datetime as dt

    import duckdb

    from sf_assistant.config import load_records_config
    from sf_assistant.engine.catalog import RecordError
    from sf_assistant.tools import record_base
    from sf_assistant.tools.open_records import open_records
    from warehouse_fixture import make_knowledge, make_warehouse

    wh = make_warehouse(tmp_path)
    con = duckdb.connect(str(wh))
    con.execute('UPDATE raw."Interview__c" SET "Interview_Outcome__c" = \'Rejected\' WHERE "Name" = \'I-0005\'')
    con.close()
    kd = make_knowledge(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kd))
    real = record_base._Call.run

    def run(self, frag, **kw):   # the main statement and the overlap COUNT ran: the extra COUNT fails
        if len(self.statements) >= 2:
            raise RecordError("query stopped after 1 s.", "E10")
        return real(self, frag, **kw)

    monkeypatch.setattr(record_base._Call, "run", run)
    also = "'Offer Received' is also stored in Interview_Status__c (2 records, legacy value); "
    try:
        r = ok(t, "count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Offer Received")]})
        note = next(n for n in r["notes"] if n.startswith(also))
        assert r["count"] == 1 and note == also + "none of them matches all conditions of this query"
    finally:
        t.close()


def test_also_stored_overlap_with_a_multi_select_field(tmp_path):
    """A value also stored in a multi-select field matches records that hold it among other values."""
    import duckdb

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records
    from warehouse_fixture import make_knowledge, make_warehouse

    wh = make_warehouse(tmp_path)
    con = duckdb.connect(str(wh))
    con.execute('ALTER TABLE raw."Interview__c" ADD COLUMN "Focus_Area__c" VARCHAR')
    con.execute('UPDATE raw."Interview__c" SET "Focus_Area__c" = \'Coding\' WHERE "Name" IN (\'I-0001\', \'I-0002\')')
    con.execute('CREATE OR REPLACE VIEW main."Interview__c" AS SELECT * REPLACE (TRY_CAST("Date_of_Interview__c" AS DATE) '
                'AS "Date_of_Interview__c", TRY_CAST("CreatedDate" AS TIMESTAMP) AS "CreatedDate") FROM raw."Interview__c"')
    con.close()
    kd = make_knowledge(tmp_path)
    path = kd / "Interview__c.json"
    kn = json.loads(path.read_text(encoding="utf-8"))
    kn["fields"].append({"api_name": "Focus_Area__c", "label": "Focus Area", "type": "picklist",
                         "business_meaning": "Former focus field.", "picklist": {"values": [{"value": "Design"}]}})
    path.write_text(json.dumps(kn), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        import datetime as dt
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kd))
    try:
        r = ok(t, "count_records", {"object": "Interview__c", "filters": [F("Focus_Area__c", "Coding")]})
        assert r["count"] == 2
        # 'Communication;Coding' holds Coding too: both records match (an '=' comparison would find only one).
        assert "'Coding' is also stored in Evaluation_Parameter__c (2 records); all 2 of them match all conditions of " \
               "this query" in r["notes"]
        assert "list_contains(string_split(t0.\"Evaluation_Parameter__c\", ';'), ?)" in t.evidence[-1].statements[1].sql
    finally:
        t.close()


def test_trend_records(tools):
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "interview_date",
                                    "period": "last 6 months"})
    assert {b["bucket"]: b["value"] for b in r["buckets"]} == FACTS["interview_months_2026"]
    assert r["total"] == 9 and r["records_without_date"] == FACTS["interviews_without_date"]
    assert r["last_bucket_partial"] is True and r["highest"] == {"bucket": "2026-09", "value": 3}
    assert r["applied"]["period"]["to_exclusive"] == "2026-11-01" and r["date_field"] == "Date_of_Interview__c"
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "quarter", "date_field": "Date_of_Interview__c",
                                    "period": "this year"})
    assert [b["bucket"] for b in r["buckets"]] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "week", "date_field": "Date_of_Interview__c",
                                    "period": "since 2026-09-28"})
    assert r["buckets"][-1]["bucket"] == "week of 2026-10-12" and r["applied"]["period"]["to_exclusive"] == "2026-10-19"
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                    "period": "this year", "split_by": "Interview_Outcome__c"})
    sep = next(b for b in r["buckets"] if b["bucket"] == "2026-09")
    assert sep["by"] == {"Offer Received": 1, "Rejected": 1, "Moved to Next Round": 1}
    e = err(tools, "trend_records", {"object": "Interview__c", "grain": "day", "date_field": "Date_of_Interview__c",
                                     "period": "last 2 years"})
    assert "buckets (max 92); use grain=week or month" in e
    e = err(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                     "period": "before 2026-01-01"})
    assert "a trend needs a bounded period" in e


def test_compare_records(tools):
    r = ok(tools, "compare_records", {"object": "Interview__c", "segments": [
        {"label": "Ghosted", "filters": [F("Interview_Outcome__c", "Ghosted")]},
        {"label": "Rejected", "filters": [F("Interview_Outcome__c", "Rejected")]}]})
    assert [s["value"] for s in r["segments"]] == [FACTS["ghosted"], FACTS["rejected"]]
    assert r["higher"] == "Ghosted" and r["first_minus_second"] == 1 and r["ratio_first_to_second"] == 2.0
    assert r["change_percent_second_to_first"] == 100.0
    r = ok(tools, "compare_records", {"object": "Internal_Interview__c", "date_field": "CreatedDate", "segments": [
        {"label": "this month", "period": "this month"}, {"label": "last month", "period": "last month"}]})
    assert [s["value"] for s in r["segments"]] == [FACTS["internal_created_this_month_ny"],
                                                   FACTS["internal_created_last_month_ny"]]
    e = err(tools, "compare_records", {"object": "Interview__c", "segments": [{"label": "a"}]})
    assert e == "compare needs at least 2 segments."
    e = err(tools, "compare_records", {"object": "Interview__c", "segments": [
        {"label": "a", "filters": [F("Interview_Outcome__c", "Ghosted")]},
        {"label": "b", "filters": [F("Interview_Outcome__c", "ghosted")]}]})
    assert e == "segments 'a' and 'b' have identical conditions."
    e = err(tools, "compare_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Ghosted")],
                                       "segments": [{"label": "a", "filters": [F("Interview_Outcome__c", "Ghosted")]},
                                                    {"label": "b", "filters": [F("Interview_Outcome__c", "Rejected")]}]})
    assert "is in the shared filters and in segment 'a'; keep it only in the segments." in e
    e = err(tools, "compare_records", {"object": "Interview__c", "segments": [{"label": "x", "period": "this month"},
                                                                            {"label": "y", "period": "last month"}]})
    assert e.startswith("segment 'x' has a period; set date_field.")
    e = err(tools, "compare_records", {"object": "Interview__c", "period": "this month", "segments": [
        {"label": "x"}, {"label": "y"}]})
    assert "has no argument 'period'" in e and "segments[i].period" in e


def test_compare_in_utc(tmp_path):
    t = _open(tmp_path, tz="UTC")
    try:
        r = ok(t, "compare_records", {"object": "Internal_Interview__c", "date_field": "CreatedDate", "segments": [
            {"label": "this month", "period": "this month"}, {"label": "last month", "period": "last month"}]})
        assert r["segments"][0]["value"] == FACTS["internal_created_this_month_utc"]
        assert t.status().time_zone_from == "OKEVAL_RECORDS_TZ"
    finally:
        t.close()


def test_percentage_of_records(tools):
    r = ok(tools, "percentage_of_records", {"object": "Internal_Interview__c",
                                            "part_filters": [F("Status__c", "Completed")]})
    assert (r["part"], r["base"], r["percent"]) == (2, 6, 33.3)
    assert r["base_is"] == "all Internal_Interview__c records" and r["part_is"] == "Status__c = 'Completed'"
    assert "1 base records have a blank Status__c; they are in the base but not in the part" in r["notes"]
    r = ok(tools, "percentage_of_records", {"object": "Internal_Interview__c",
                                            "part_filters": [F("Status__c", "Completed", "ne")]})
    assert (r["part"], r["base"]) == (4, 6) and r["part_is"] == "Status__c <> 'Completed' (blank included)"
    assert "1 base records have a blank Status__c; they are counted in the part" in r["notes"]
    assert not any("not in the part" in n for n in r["notes"])
    assert "records with a blank Status__c are included by <> (1 of all Internal_Interview__c records have a blank " \
           "Status__c)" in r["notes"]
    r = ok(tools, "percentage_of_records", {"object": "Internal_Interview__c",
                                            "part_filters": [F("Status__c", None, "not_in") | {"values": ["Completed"]}]})
    assert r["part"] == 4 and "1 base records have a blank Status__c; they are counted in the part" in r["notes"]
    r = ok(tools, "percentage_of_records", {"object": "Internal_Interview__c", "part_filters": [F("Status__c", "Completed")],
                                            "base_filters": [F("Interview_Type__c", "Mock")]})
    assert (r["part"], r["base"]) == (1, 3)
    e = err(tools, "percentage_of_records", {"object": "Internal_Interview__c", "part_filters": [F("Status__c", "Completed")],
                                             "base_filters": [F("Status__c", "Completed")]})
    assert e.startswith("part_filters repeat base_filters")


def test_list_records(tools):
    r = ok(tools, "list_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Offer Received")],
                                   "fields": ["Recruiter__c", "Date_of_Interview__c"], "limit": 1})
    assert r["columns"] == ["Name", "Recruiter__c (name)", "Date_of_Interview__c"]
    assert r["returned"] == 1 and r["total_count"] == 2 and r["truncated"] is True
    assert r["order"] == "CreatedDate desc" and r["rows"][0]["Name"] == "I-0001"
    r = ok(tools, "list_records", {"object": "Interview__c", "order_by": "Date_of_Interview__c", "order": "asc", "limit": 2})
    assert r["columns"][0] == "Name" and "Interview_Status__c" in r["columns"] and "Zoom_Passcode__c" not in r["columns"]
    assert r["rows"][0]["Name"] == "I-0008" and r["total_count"] == 10
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$", r["rows"][0]["CreatedDate"])
    r = ok(tools, "list_records", {"object": "Interview__c", "filters": [F("Name", "I-0003")]})
    assert r["returned"] == 1 and r["rows"][0]["Interview_Outcome__c"] == "Ghosted"


def test_search_records(tools):
    r = ok(tools, "search_records", {"name": "recruiter b"})
    assert r["match_type"] == "exact" and len(r["matches"]) == 2
    assert {m["fields"]["Employment_Status__c"] for m in r["matches"]} == {"Active Employee", "Former Employee"}
    r = ok(tools, "search_records", {"name": "I-0003"})
    assert [m["object"] for m in r["matches"]] == ["Interview__c"]
    r = ok(tools, "search_records", {"name": "Candidate 1"})
    assert r["matches"][0]["record_type"] == "candidate (Person Account)"
    assert "example.invalid" not in json.dumps(r) and "PersonEmail" not in json.dumps(r)
    r = ok(tools, "search_records", {"name": "Alpha"})
    assert r["match_type"] == "contains" and r["matches"][0]["name"] == "Program Alpha"
    r = ok(tools, "search_records", {"name": "zzzz"})
    assert r["matches"] == [] and "no record is named 'zzzz'" in r["notes"]
    assert err(tools, "search_records", {"name": "x"}) == "search_records needs 'name' with at least 2 characters."


def test_get_field_history(tools):
    r = ok(tools, "get_field_history", {"object": "Interview__c", "record": "I-0001"})
    assert r["history_table"] == "Interview__History" and r["total_count"] == 3
    fields = [c["field"] for c in r["changes"]]
    assert fields == ["Recruiter__c", "Interview_Status__c", "record created"]
    assert r["changes"][0]["new"] == "Recruiter A" and r["changes"][0]["changed_by"] == "User One"
    assert "zp-" not in json.dumps(r) and "Zoom_Passcode__c" not in json.dumps(r)
    r = ok(tools, "get_field_history", {"object": "Interview__c", "field": "Interview_Status__c", "count_by": "new_value"})
    assert r["groups"] == [{"group": "Completed", "value": 2}]
    r = ok(tools, "get_field_history", {"object": "Interview__c", "field": "Interview_Status__c", "changed_to": "completed",
                                        "period": "September 2026"})
    assert r["total_count"] == 2 and r["applied"]["changed_to"] == "Completed"
    e = err(tools, "get_field_history", {"object": "Interview__c", "field": "Interview_Outcome__c"})
    assert "has no history rows in Interview__History. Tracked fields: Interview_Status__c (2)" in e
    e = err(tools, "get_field_history", {"object": "Recruiter__c"})
    assert "has no field-history table" in e and "LastModifiedDate" in e
    e = err(tools, "get_field_history", {"object": "Interview__c", "record": "I-9"})
    assert e.startswith("no Interview__c record named 'I-9'.")


def test_get_sync_status(tools):
    r = ok(tools, "get_sync_status", {})
    assert r["objects"][0]["object"] == "Program__c" and len(r["objects"]) == 10
    assert r["snapshot"]["origin"] == "preprod SANDBOX copy (Organization.IsSandbox = true)"
    assert r["newest_sync"] == "2026-10-13 14:30 UTC" and "sync failures" in r["not_recorded"]
    r = ok(tools, "get_sync_status", {"objects": ["Interview__c"]})
    assert r["objects"][0]["records"] == 10


# ----------------------------------------------------------------------------------- safety

def test_denylist_blocks_sensitive_columns_everywhere(tools):
    cases = [
        ("list_records", {"object": "Account", "fields": ["Passport_Number__c"]}),
        ("count_records", {"object": "Account", "filters": [F("Passport_Number__c", None, "is_not_blank")]}),
        ("group_records", {"object": "Interview__c", "group_by": "Zoom_Passcode__c"}),
        ("get_field_values", {"object": "Interview__c", "field": "Zoom_Passcode__c"}),
        ("get_field_history", {"object": "Interview__c", "field": "Zoom_Passcode__c"}),
        ("list_records", {"object": "Interview__c", "order_by": "Zoom_Passcode__c"}),
    ]
    for name, args in cases:
        e = err(tools, name, args)
        assert "is a sensitive field (credential, passcode or identity document)" in e, (name, e)
    for name, args in [("list_records", {"object": "Account"}), ("search_records", {"name": "Candidate"}),
                       ("describe_fields", {"object": "Account", "query": "passport"}),
                       ("get_field_history", {"object": "Interview__c"})]:
        res = json.dumps(tools.call(name, args))
        assert "X0000001" not in res and "zp-111" not in res


def test_credential_object(tools):
    e = err(tools, "list_records", {"object": "Candidate_Portal_Credential__c"})
    assert "holds portal credentials or sessions" in e
    assert "holds portal credentials" in err(tools, "search_records", {"name": "CCN-0001",
                                                                       "objects": ["Candidate_Portal_Credential__c"]})
    r = ok(tools, "count_records", {"object": "Candidate_Portal_Credential__c", "filters": [F("Status__c", "Active")]})
    assert r["count"] == 1
    r = ok(tools, "group_records", {"object": "Candidate_Portal_Credential__c", "group_by": "Status__c"})
    assert r["total_across_groups"] == 2
    assert "sensitive field" in err(tools, "group_records", {"object": "Candidate_Portal_Credential__c",
                                                             "group_by": "Username__c"})
    assert "not-a-real-secret" not in json.dumps(tools.call("describe_fields", {"object": "Candidate_Portal_Credential__c",
                                                                               "query": "password"}))


def test_guard_refuses_whole_rows_and_sensitive_text_of_the_credential_object(tools):
    """Second line of defence: even a builder bug cannot read a credential row through the guard."""
    from sf_assistant.engine.warehouse import WarehouseError

    t = 'main."Candidate_Portal_Credential__c"'
    for sql in (f"SELECT c FROM {t} c", f"SELECT struct_extract(c, 'Password__c') AS p FROM {t} c",
                f"SELECT to_json(c) AS j FROM {t} AS c", f"SELECT #5 FROM {t} AS c",
                f'SELECT c."Username__c" FROM {t} AS c', f'SELECT c."Password__c" FROM {t} AS c'):
        with pytest.raises(WarehouseError) as exc:
            tools.warehouse.run(sql)
        assert exc.value.kind == "guard", sql
    assert ("candidate_portal_credential__c", "username__c") in tools.catalog.blocked_fields
    st = tools.warehouse.run(f'SELECT c."Status__c" AS s, COUNT(*) AS n FROM {t} AS c GROUP BY 1 ORDER BY 1')
    assert st.rows == [["Active", 1], ["Disabled", 1]]


def test_contact_fields_only_when_the_question_asks(tools):
    """Phase 1 (2026-10-26): a contact field is shown, listed as values or grouped only when the question asks for
    contact details (tests/test_contact_rule.py has the full set); a filter on one works either way, its value is
    hidden in the applied text unless the question asks."""
    try:
        tools.begin_question("How many candidate accounts are there?")
        r = ok(tools, "list_records", {"object": "Account"})
        assert "PersonEmail" not in r["columns"] and "example.invalid" not in json.dumps(r)
        r = ok(tools, "list_records", {"object": "Account", "fields": ["PersonEmail"]})
        assert "PersonEmail" not in r["columns"] and "example.invalid" not in json.dumps(r)
        assert any(n.startswith("left out PersonEmail: contact details are shown only when") for n in r["notes"])
        assert "holds contact details" in err(tools, "get_field_values", {"object": "Account", "field": "PersonEmail"})
        assert "holds contact details" in err(tools, "group_records", {"object": "Recruiter__c", "group_by": "Email__c"})
        r = ok(tools, "count_records", {"object": "Account", "filters": [F("PersonEmail", "c1@example.invalid")]})
        assert r["count"] == 1 and "example.invalid" not in json.dumps(r)
        r = ok(tools, "count_records", {"object": "Account", "filters": [F("PersonEmail", None, "is_blank")]})
        # the question counts candidates: code keeps the Person Accounts (business-term contracts of 2026-11-24), so
        # of the client and the three accounts added for the lookup tests only the candidate Delta Person is left
        assert r["count"] == 1 and r["applied"]["term_filters"] == ["RecordTypeId = 'Person Account'"]
        tools.begin_question("What is the e-mail address of Candidate 1?")
        r = ok(tools, "list_records", {"object": "Account", "fields": ["PersonEmail"]})
        assert "PersonEmail" in r["columns"] and "example.invalid" in json.dumps(r)
        assert "contact details are shown: the question asks for contact details" in r["notes"]
        r = ok(tools, "get_field_values", {"object": "Account", "field": "PersonEmail"})
        assert "example.invalid" in json.dumps(r)
    finally:
        tools.begin_question("")


# ----------------------------------------------------------------------------------- errors name the fix

def test_errors_name_the_fix(tools):
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Outcome_Status", "Ghosted")]})
    assert "Closest: Interview_Outcome__c" in e or "Interview_Outcome__c" in e
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Offer")]})
    assert "Stored values (records):" in e and "Close: 'Offer Received'" in e
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Date_of_Interview__c", "last month")]})
    assert e == ("Date_of_Interview__c is a date field: do not put 'last month' in filters. Use "
                 "date_field='Date_of_Interview__c' with period='last month'.")
    e = err(tools, "count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "banana"})
    assert "Use one of: today" in e
    e = err(tools, "count_records", {"object": "Interview__c", "period": "this month"})
    assert e.startswith("period needs date_field. Date fields of Interview__c: Date_of_Interview__c (interview_date:")
    e = err(tools, "count_records", {"object": "Interview__c", "date_field": "Interview_Status__c", "period": "this month"})
    assert "not a date or date/time field" in e and "Date_of_Interview__c" in e
    if tools.catalog.resolver is None:
        e = err(tools, "count_records", {"object": "Interviewz"})
        assert "Closest:" in e and "find_objects" in e
    else:   # SFA_RESOLVER=on: one clear reading is used and stated (twins: tests/test_resolver_catalog.py)
        assert ok(tools, "count_records", {"object": "Interviewz"})["read_as"][0]["used"] == "Interview__c"
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Client_Bill_Rate__c", "lots", "gt")]})
    assert "is a number field; 'lots' is not a number." in e
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Interview_Status__c", "A", "gt")]})
    assert "op 'gt' does not work on picklist field Interview_Status__c; use one of:" in e
    e = err(tools, "count_records", {"object": "Interview__c", "related": [{"child_object": "Deliverable__c",
                                                                           "exists": True}]})
    assert "has no lookup to Interview__c" in e
    e = err(tools, "count_records", {"object": "Candidate_Training__c", "filters": [F("Session__c.Name", "x")]})
    assert "related=" in e


# ----------------------------------------------------------------------------------- robustness

@pytest.mark.parametrize("args", [None, "", "not json", "[1, 2]", [1, 2], 5, {"_raw": "{bad"},
                                  {"object": "Interview__c", "grain": "monthly!!"},
                                  {"object": "Interview__c", "filters": [{"field": "Status__c", "op": "eq", "value": "x",
                                                                          "period": "this month"}]},
                                  {"object": ["Interview__c"]}, {"object": "Interview__c", "filters": "Status__c=x"},
                                  {"object": "Interview__c", "limit": "many"}, {"object": "Interview__c", "top": {}},
                                  {"object": "Interview__c", "related": [{"child_object": "Session__c"}]},
                                  {"object": "Interview__c", "filters": [{"op": "eq"}]}])
def test_call_never_raises(tools, args):
    for name in TOOL_ORDER + ("no_such_tool",):
        out = tools.call(name, args)
        assert set(out) == {"ok", "result", "error", "chunk_ids"}
        json.dumps(out)


def test_argument_errors(tools):
    e = err(tools, "count_records", {"object": "Interview__c", "colour": "red"})
    assert e.startswith("count_records has no argument 'colour'. Allowed: object, filters")
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [{"field": "Interview_Status__c", "op": "eq",
                                                                            "value": "Completed", "period": "x"}]})
    assert "has unknown key 'period'" in e and "Put time windows in the top-level date_field + period." in e
    e = err(tools, "count_records", {})
    assert e.startswith("count_records needs 'object'")
    e = err(tools, "list_records", {"object": "Interview__c", "limit": "many"})
    assert e == "'limit' must be integer, got 'many'"
    assert "arguments were not valid JSON" in err(tools, "count_records", {"_raw": "{oops"})


def test_lenient_synonyms_are_mapped_and_noted(tools):
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": {"Interview_Outcome__c": "Ghosted"},
                                    "reason": "the user asked"})
    assert r["count"] == 2
    assert any("read as eq filters" in n for n in r["notes"]) and any("ignored argument(s): reason" in n for n in r["notes"])
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "==",
                                                                           "value": ["Ghosted", "Rejected"]}]})
    assert r["count"] == 3 and any("read as in" in n or "read as 'eq'" in n for n in r["notes"])
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "IS NULL"}]})
    assert r["count"] == 4
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": ["Interview_Status__c"], "top": "2"})
    assert len(r["groups"]) == 2
    r = ok(tools, "list_records", {"object": "Interview__c", "limit": 500})
    assert r["returned"] == 10 and any("clamped to 200" in n for n in r["notes"])   # 50 until development A
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "Monthly", "date_field": "Date_of_Interview__c",
                                    "period": "this year"})
    assert r["grain"] == "month"
    r = ok(tools, "count_records", {"object": "Candidate_Training__c", "related": [{"child_object": "Session__c",
                                                                                   "exists": "false"}]})
    assert r["count"] == 2
    r = ok(tools, "count_records", '{"object": "Interview__c"}')
    assert r["count"] == 10


def test_large_list_fits_the_result_cap(tools):
    out = tools.call("list_records", {"object": "Log_Entry__c", "fields": ["Message__c"], "limit": 50})
    text = json.dumps(out)
    assert out["ok"] and len(json.dumps(out["result"])) <= MAX_RESULT_CHARS
    json.loads(text)
    r = out["result"]
    assert r["total_count"] == FACTS["log_entries"] and r["shown"] < 50
    assert any(n.startswith("result trimmed to fit: showing") for n in r["notes"])
    assert all(len(row["Message__c"]) <= 200 for row in r["rows"])


# ----------------------------------------------------------------------------------- evidence and hints

def test_evidence_log(tools):
    tools.begin_question()
    tools.call("describe_fields", {"object": "Interview__c", "query": "outcome"})
    tools.call("count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Ghosted")]})
    tools.call("count_records", {"object": "Interview__c", "filters": [F("Nope__c", "x")]})
    ev = tools.evidence
    assert [e.query_id for e in ev] == ["q1", "q2", "q3"]
    assert [e.kind for e in ev] == ["orientation", "data", "data"]
    assert ev[0].statements and all(s.cached for s in ev[0].statements)
    st = ev[1].statements[0]
    assert st.sql == 'SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE t0."Interview_Outcome__c" = ?'
    assert st.params == ["Ghosted"] and st.rows == [[2]] and st.columns == ["n"]
    assert ev[1].tables == ["Interview__c"] and ev[1].synced_at == {"Interview__c": "2026-10-13 14:30 UTC"}
    assert ev[1].summary == "2" and ev[1].model_result["count"] == 2 and ev[1].ok
    assert ev[1].applied == {"filters": ["Interview_Outcome__c = 'Ghosted'"]}
    assert ev[2].ok is False and ev[2].error and ev[2].model_result is None and ev[2].statements == []
    assert ev[2].arguments == {"object": "Interview__c", "filters": [F("Nope__c", "x")]}
    tools.begin_question()
    assert tools.evidence == []
    tools.call("get_sync_status", {})
    assert tools.evidence[0].query_id == "q1"


def test_object_hints_have_no_counts(tools):
    counts = {"10", "6", "4", "2", "3", "5"}
    for q in ("How many interviews ended with an offer received?", "Which deliverables are locked?",
              "How many internal interviews are completed?", "Break down candidate trainings by status"):
        h = tools.object_hints(q)
        assert h.startswith("[Hints from code, not from the user]")
        assert not (set(re.findall(r"\b\d+\b", h)) & counts), h
    h = tools.object_hints("How many interviews ended with an offer received?")
    assert "- Interview__c (Interview)" in h and "'Offer Received' in Interview__c.Interview_Outcome__c" in h
    assert tools.object_hints("qwxzv") == ""


def test_chunk_ids_use_the_multi_registry_format(tools):
    out = tools.call("count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Ghosted")]})
    assert out["chunk_ids"] == ["Interview__c::object", "Interview__c::field:Interview_Outcome__c"]


def test_calls_are_thread_safe(tools):
    from concurrent.futures import ThreadPoolExecutor

    tools.begin_question()
    calls = [("count_records", {"object": "Interview__c", "filters": [F("Interview_Outcome__c", "Ghosted")]}),
             ("group_records", {"object": "Interview__c", "group_by": "Recruiter__c"}),
             ("trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                "period": "this year"})] * 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        outs = list(pool.map(lambda c: tools.call(*c), calls))
    assert all(o["ok"] for o in outs)
    assert {o["result"]["count"] for o in outs if "count" in o["result"]} == {2}
    ev = tools.evidence
    assert len(ev) == 60 and len({e.query_id for e in ev}) == 60
    tools.begin_question()


def test_owner_by_queue_name_and_sum_measures(tools):
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("OwnerId", "Queue One")]})
    assert r["count"] == 1 and not any(n.startswith("no User") for n in r.get("notes", []))
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("OwnerId", "Nobody")]})
    assert e.startswith("no User or Group record is named 'Nobody'.") and "No name contains it" in e
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "quarter", "date_field": "Date_of_Interview__c",
                                    "period": "this year", "measure": "sum", "measure_field": "Client_Bill_Rate__c"})
    assert [b["value"] for b in r["buckets"]] == [0, 0, 220.5, 90.0] and r["total"] == 310.5
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Interview_Status__c", "measure": "avg",
                                    "measure_field": "Client_Bill_Rate__c"})
    assert r["groups"][0] == {"group": "Completed", "value": 110.25} and "total_across_groups" not in r
    e = err(tools, "group_records", {"object": "Interview__c", "group_by": "Interview_Status__c", "measure": "sum",
                                     "measure_field": "Interview_Outcome__c"})
    assert e.startswith("sum needs a number field")


def test_owner_label_and_owner_field_get_separate_joins(tools):
    """OwnerId as a label joins User and Group; OwnerId.IsActive joins User only. Either order, same values."""
    for fields in (["OwnerId", "OwnerId.IsActive"], ["OwnerId.IsActive", "OwnerId"]):
        r = ok(tools, "list_records", {"object": "Interview__c", "fields": fields, "order_by": "Name", "order": "asc",
                                       "limit": 3})
        rows = {row["Name"]: row for row in r["rows"]}
        assert rows["I-0001"]["OwnerId (name)"] == "User One" and rows["I-0001"]["OwnerId.IsActive"] is True
        assert rows["I-0002"]["OwnerId (name)"] == "Queue One" and rows["I-0002"]["OwnerId.IsActive"] is None
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "OwnerId",
                                    "filters": [F("OwnerId.IsActive", True)]})
    assert {g["group"]: g["value"] for g in r["groups"]} == {"User One": 9}
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "OwnerId.IsActive", "split_by": "OwnerId"})
    assert {g["group"]: g["by"] for g in r["groups"]} == {"true": {"User One": 9}, "(blank)": {"Queue One": 1}}


def test_multi_select_groups_count_each_selected_value(tools):
    """'Communication;Coding' counts once for Communication and once for Coding, never as its own group."""
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Evaluation_Parameter__c"})
    assert {g["group"]: g["value"] for g in r["groups"]} == {"Coding": 2, "Communication": 1, "Design": 1, "(blank)": 7}
    assert r["group_count"] == 3 and r["records_matched"] == 10 and "total_across_groups" not in r
    assert any(n.startswith("Evaluation_Parameter__c is multi-select: a record with several values is counted once in "
                            "the group of each value") for n in r["notes"])
    assert "unnest(COALESCE(string_split(t0.\"Evaluation_Parameter__c\", ';'), ['(blank)']))" in tools.evidence[-1].statements[0].sql
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Evaluation_Parameter__c", "top": 1})
    assert r["groups"] == [{"group": "Coding", "value": 2}]
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Evaluation_Parameter__c", "min_value": 2})
    assert {g["group"]: g["value"] for g in r["groups"]} == {"Coding": 2, "(blank)": 7}
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Evaluation_Parameter__c",
                                    "measure": "sum", "measure_field": "Client_Bill_Rate__c"})
    assert {g["group"]: g["value"] for g in r["groups"]}["Coding"] == 220.5
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Employment_Type__c",
                                    "split_by": "Evaluation_Parameter__c"})
    by = {g["group"]: g["by"] for g in r["groups"]}
    assert by["Full Time"] == {"(blank)": 4, "Coding": 1, "Communication": 1, "Design": 1}
    assert by["Contract"] == {"(blank)": 2, "Coding": 1} and r["total_across_groups"] == 10
    assert any(n.startswith("split_by Evaluation_Parameter__c is multi-select") for n in r["notes"])
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                    "period": "this year", "split_by": "Evaluation_Parameter__c"})
    b = {x["bucket"]: x for x in r["buckets"]}
    assert b["2026-09"]["value"] == 3 and b["2026-09"]["by"] == {"Coding": 2, "(blank)": 1, "Communication": 1}
    assert b["2026-10"]["by"] == {"(blank)": 2, "Design": 1}


def test_ne_on_a_lookup_path_does_not_borrow_the_target_blank_count(tools):
    """Interview__c rows with no recruiter (or an unresolved one) have a blank Recruiter__c.Employment_Status__c."""
    r = ok(tools, "count_records", {"object": "Interview__c",
                                    "filters": [F("Recruiter__c.Employment_Status__c", "Former Employee", "ne")]})
    assert r["count"] == 9  # 10 minus the one interview of the former employee; 2 blank paths are included
    assert ("records whose Recruiter__c.Employment_Status__c is blank, including records with no Recruiter__c, are "
            "included by <>") in r["notes"]


def test_trend_flags_a_first_bucket_the_period_starts_inside(tools):
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "quarter", "date_field": "Date_of_Interview__c",
                                    "period": "last 6 months"})
    assert r["buckets"][0]["bucket"] == "2026-Q2" and r["first_bucket_partial"] is True
    assert any(n.startswith("the first bucket (2026-Q2) covers only 2026-05-01 onward") for n in r["notes"])
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "week", "date_field": "Date_of_Interview__c",
                                    "period": "this month"})
    assert r["buckets"][0]["bucket"] == "week of 2026-09-28" and r["first_bucket_partial"] is True
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                    "period": "last 6 months"})
    assert r["first_bucket_partial"] is False and not any("first bucket" in n for n in r["notes"])
    r = ok(tools, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                                    "period": "2026-05-15 to 2026-09-15"})
    assert r["first_bucket_partial"] is True and r["last_bucket_partial"] is False
    assert "the last bucket (2026-09) covers only up to 2026-09-15 (the period ends inside that month)" in r["notes"]


def test_top_n_counts_every_tied_group(tools):
    """60 log entries over 28 days: many days tie at the cutoff; the count is complete, the names capped at 5."""
    r = ok(tools, "group_records", {"object": "Log_Entry__c", "group_by": "Name", "top": 3})
    assert r["ties_at_cutoff_count"] == 57 and len(r["ties_at_cutoff"]) == 5
    assert any(n.startswith("57 more group(s) have the same value as the last one shown (1): ") and
               n.endswith(" and 52 more") for n in r["notes"])


def test_min_count_is_never_clamped(tools):
    """'at least 1 session' is exists=true; below 1 is refused with the fix, never silently raised to 2."""
    rel = lambda n: [{"child_object": "Session__c", "exists": True, "min_count": n}]  # noqa: E731
    r = ok(tools, "count_records", {"object": "Candidate_Training__c", "related": rel(1)})
    assert r["count"] == 2 and r["applied"]["related"] == ["has a Session__c (via Session__c.Candidate_Training__c)"]
    assert "min_count 1 read as exists=true" in r["notes"] and not any("clamped" in n for n in r["notes"])
    r = ok(tools, "count_records", {"object": "Candidate_Training__c", "related": rel(2)})
    assert r["count"] == FACTS["trainings_with_2_sessions"]
    e = err(tools, "count_records", {"object": "Candidate_Training__c", "related": rel(0)})
    assert e.startswith("'related item 1.min_count' must be at least 1, got 0.") and "exists=false" in e


def test_field_values_of_a_multi_select_count_records_once(tools):
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Evaluation_Parameter__c"})
    assert {v["value"]: v["records"] for v in r["values"]} == {"Coding": 2, "Communication": 1, "Design": 1}
    assert (r["value_selections"], r["records_with_value"], r["blank_records"]) == (4, 3, 7)  # 3 + 7 = 10 interviews
    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Evaluation_Parameter__c", "contains": "od"})
    assert [v["value"] for v in r["values"]] == ["Coding"]  # not Communication, stored next to Coding


def test_field_values_contains_runs_and_shows_the_filtering_statement(tools):
    from sf_assistant.agent.render import applied_text

    r = ok(tools, "get_field_values", {"object": "Interview__c", "field": "Interview_Status__c", "contains": "comp"})
    assert [(v["value"], v["records"]) for v in r["values"]] == [("Completed", 5)]
    assert r["applied"] == {"contains": "comp"}
    ev = tools.evidence[-1]
    assert "ILIKE ?" in ev.statements[0].sql and ev.statements[0].params == ["%comp%"]
    assert not getattr(ev.statements[0], "cached", False)
    assert applied_text(ev.applied) == "value contains 'comp'"


def test_date_versus_date_time_uses_the_business_day(tools):
    """A UTC date/time is turned into the business-zone calendar day (bound parameter) before the comparison."""
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [
        {"field": "CreatedDate", "op": "eq", "other_field": "Date_of_Interview__c"}]})
    assert "date/time compared with a date as calendar days in America/New_York" in r["notes"]
    ev = tools.evidence[-1]
    assert "AT TIME ZONE 'UTC') AT TIME ZONE ? AS DATE) = t0.\"Date_of_Interview__c\"" in ev.statements[0].sql
    assert ev.statements[0].params == ["America/New_York"]
    assert r["count"] == 2  # I-0006 (2026-07-01 12:00 UTC) and I-0007


# ----------------------------------------------------------------------------------- lookup disambiguation (§21)

DELTA_Q = "how many mock interviews has delta person taken"


def ev_last(tools):
    return tools.evidence[-1]


def test_unknown_names_are_errors_that_list_the_names_that_fit(tools):
    """E4h in the F and F.Name forms, in filters, compare segments and percentage parts; the count never runs."""
    cases = [("Echo", "First name 'Echo' fits: Echo One, Echo Two."),
             ("Delta Persn", "Close spellings: Delta Person."),
             ("Nobody Here", "No name contains it and no name is close.")]
    for value, part in cases:
        for field in ("Interviewer__c", "Interviewer__c.Name"):
            calls = [("count_records", {"object": "Internal_Interview__c", "filters": [F(field, value)]}),
                     ("compare_records", {"object": "Internal_Interview__c", "segments": [
                         {"label": "a", "filters": [F(field, value)]}, {"label": "b", "filters": [F("Status__c", "Mock")]}]}),
                     ("percentage_of_records", {"object": "Internal_Interview__c", "part_filters": [F(field, value)]})]
            for name, args in calls:
                tools.begin_question()
                e = err(tools, name, args)
                assert e.startswith(f"no Employee (Recruiter__c) record is named '{value}'.") and part in e, (name, e)
                assert "answer that no Employee has this name, without a count" in e
                ev = ev_last(tools)
                assert ev.error_code == "E4h"
                assert not any("Internal_Interview__c" in s.sql for s in ev.statements)   # the main statement never ran


def test_negative_and_in_filters_with_a_missing_name_get_n9(tools):
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Nobody Here", "ne")]})
    assert r["count"] == 6 and "no Employee is named 'Nobody Here'; this filter excludes nothing for it" in r["notes"]
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [
        {"field": "Interviewer__c", "op": "in", "values": ["Delta Person", "Nobody Here"]}]})
    assert r["count"] == FACTS["delta_interviewer"]
    assert "no Employee is named 'Nobody Here'; the other names matched" in r["notes"]


def test_names_match_double_and_non_breaking_spaces(tools):
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "delta   PERSON")]})
    assert r["count"] == FACTS["delta_interviewer"]                     # stored 'Delta  Person'
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Candidate__c", "Delta Person")]})
    assert r["count"] == FACTS["delta_as_candidate"]                     # stored with a non-breaking space
    r = ok(tools, "search_records", {"name": "delta person"})
    assert {m["object"] for m in r["matches"]} == {"Recruiter__c", "Account"}


EXPECTED_CHECK = {
    "filter": "Recruiter__c.Name = 'Delta Person'",
    "object": "Internal_Interview__c",
    "field": "Recruiter__c", "target": "Recruiter__c", "target_label": "Employee", "person_kind": "employee",
    "role": "recruiter", "fits": None, "cues": ["taken"],
    "records": 0,
    "any_role": 3,
    "others": [
        {"field": "Interviewer__c", "target": "Recruiter__c", "group": "same_target", "role": "interviewer",
         "fits": "taken", "records": 3, "same_person": True},
        {"field": "Interview_Readiness_Person__c", "target": "Recruiter__c", "group": "same_target",
         "role": "undocumented", "fits": None, "records": 1, "same_person": True},
        {"field": "Interview_Support_Person__c", "target": "Recruiter__c", "group": "same_target",
         "role": "support employee", "fits": None, "records": 0, "same_person": True},
        {"field": "Candidate__c", "target": "Account", "group": "same_name",
         "via": "a different Account record with the same name (Person Account)", "role": "candidate", "fits": None,
         "records": 2, "same_person": False, "own_kind": "candidate", "own_fits": "taken"}],
    "not_in_warehouse": ["Final_Decision_By__c"],
    "scope": "all other conditions of this query"}


def test_lookup_check_of_the_design_example(tools):
    tools.begin_question(DELTA_Q)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Recruiter__c", "Delta Person")]})
    assert r["count"] == FACTS["delta_recruiter"]
    assert r["lookup_check"] == [EXPECTED_CHECK]
    n2 = next(n for n in r["notes"] if n.startswith("'Delta Person' through other lookups of Internal_Interview__c"))
    assert "never add these counts" in n2 and "Interviewer__c (interviewer) 3" in n2
    assert ("in any of the Employee lookups (Recruiter__c and the Employee lookups above; creator, last editor and owner "
            "not included): 3; this result used Recruiter__c (recruiter): 0") in n2
    assert "not only the records this result counted" in n2
    assert "Final_Decision_By__c (final decision maker) also points to Employee but is not in the warehouse" in n2
    ev = ev_last(tools)
    assert ev.statements[0].sql.startswith('SELECT COUNT(*) AS n FROM main."Internal_Interview__c"')  # main first
    assert ev.fields_used == [("Internal_Interview__c", "Recruiter__c")]
    assert ev.error_code is None and ev.model_result["lookup_check"][0]["records"] == 0
    # Without a question: no cues and no fits.
    tools.begin_question()
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Recruiter__c", "Delta Person")]})
    lc = r["lookup_check"][0]
    assert lc["cues"] == [] and lc["fits"] is None and all(o["fits"] is None for o in lc["others"])
    # The cached name rows are reused within the question (as cached evidence).
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    reused = [s for s in ev_last(tools).statements if s.reused and 'trim(x."Name") AS name' in s.sql]
    assert reused and reused[0].cached
    from sf_assistant.agent.render import _statement_lines
    assert "(reused from an earlier call of this question)" in "\n".join(_statement_lines(reused[0], 1, 5, 9))


def test_lookup_check_records_equal_the_main_count(tools):
    tools.begin_question(DELTA_Q)
    flt = [F("Interviewer__c", "Delta Person")]
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": flt})
    assert r["lookup_check"][0]["records"] == r["count"] == FACTS["delta_interviewer"]
    r = ok(tools, "list_records", {"object": "Internal_Interview__c", "filters": flt, "limit": 1})
    assert r["lookup_check"][0]["records"] == r["total_count"] == FACTS["delta_interviewer"]
    r = ok(tools, "aggregate_records", {"object": "Internal_Interview__c", "op": "avg", "field": "Human_Total_Score__c",
                                        "filters": flt})
    assert r["lookup_check"][0]["records"] == r["records_matched"]
    r = ok(tools, "group_records", {"object": "Internal_Interview__c", "group_by": "Status__c", "filters": flt,
                                    "date_field": "CreatedDate", "period": "this year"})
    assert r["lookup_check"][0]["records"] == r["total_across_groups"]
    assert {g["group"]: g["value"] for g in r["groups"]} == FACTS["delta_interviewer_by_status"]
    r = ok(tools, "trend_records", {"object": "Internal_Interview__c", "grain": "month", "date_field": "CreatedDate",
                                    "period": "this year", "filters": flt})
    assert r["lookup_check"][0]["records"] == r["total"]
    # An OwnerId filter: n0 is the filter's own condition (a queue would count as in the main statement).
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("OwnerId", "User One")]})
    assert r["lookup_check"][0]["records"] == r["count"] == 9
    assert any(o["field"] == "CreatedById" and o["group"] == "system" for o in r["lookup_check"][0]["others"])


def test_same_name_people_are_numbered_and_split(tools):
    tools.begin_question("how many mocks did recruiter b take")
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Recruiter B")]})
    lc = r["lookup_check"][0]
    got = {b["record"]: b["records"] for b in lc["by_record"]}
    assert got == FACTS["recruiter_b_interviewer_by_record"]
    assert {b["detail"] for b in lc["by_record"]} == {"Active Employee", "Former Employee"}
    assert lc["cues"] == ["take"] and lc["fits"] == "take"              # the name's own words are no role words
    n3 = next(n for n in r["notes"] if n.startswith("'Recruiter B' matches 2 Employee records with the same name"))
    assert "per record: #1 0, #2 1; filter 'Recruiter B (#k)' for one of them" in n3
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Recruiter B (#2)")]})
    assert r["count"] == 1 and "by_record" not in r["lookup_check"][0]
    assert r["applied"]["filters"] == ["Interviewer__c.Name = 'Recruiter B (#2)'"]


def test_two_logins_of_one_employee_are_one_person(tools):
    tools.begin_question("how many interviews did echo one create")
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("CreatedById", "Echo One")]})
    assert r["count"] == 0
    assert any(n.startswith("'Echo One' matches 2 User records that are logins of the same Employee 'Echo One'")
               for n in r["notes"])
    assert not any("records with the same name" in n for n in r["notes"])
    lc = r["lookup_check"][0]
    assert "by_record" not in lc and lc["fits"] == "create" and lc["person_kind"] == "employee"
    assert {e["field"] for e in lc["elsewhere"]} == {"Interviewer__c", "Interview_Readiness_Person__c"}
    assert all(e["via"] != "same record" for e in lc["elsewhere"])      # reached through the employee's links


def test_linked_identities_and_system_lookups(tools):
    tools.begin_question()
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Recruiter A")]})
    lc = r["lookup_check"][0]
    created = next(o for o in lc["others"] if o["field"] == "CreatedById")
    assert created["group"] == "system" and created["records"] == FACTS["interviews"]
    assert "Recruiter_User__c (employee login user)" in created["via"]
    assert not any(o["group"] == "same_name" for o in lc["others"])    # the own account is linked, never same_name
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Candidate__c", "Recruiter A")]})
    assert r["count"] == 0
    assert ("'Recruiter A' matches 1 Account record(s) of record type 'Recruiter', which are not candidates (candidates "
            "are Person Accounts); it is the own account of Employee 'Recruiter A' (Recruiter_Contact__c (employee "
            "person account))") in r["notes"]
    linked = next(o for o in r["lookup_check"][0]["others"] if o["group"] == "linked")
    assert linked["field"] == "Recruiter__c" and linked["records"] == 4
    assert linked["via"] == "Recruiter_Contact__c (employee person account)"


def test_elsewhere_when_no_lookup_of_the_object_holds_the_person(tools):
    tools.begin_question("how many interviews did delta person take")
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Delta Person")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 0 and lc["records"] == 0
    assert lc["elsewhere"] == [
        {"object": "Internal_Interview__c", "field": "Interviewer__c", "role": "interviewer", "fits": "take",
         "records": FACTS["delta_interviewer"], "via": "same record", "same_person": True},
        {"object": "Internal_Interview__c", "field": "Interview_Readiness_Person__c", "role": "undocumented",
         "fits": None, "records": FACTS["delta_readiness"], "via": "same record", "same_person": True}]
    n2 = next(n for n in r["notes"] if "through other lookups of Interview__c" in n)
    assert "no lookup of Interview__c holds 'Delta Person'; it is referenced elsewhere" in n2
    assert "count the object the question's noun names" in n2


def test_other_conditions_zero_looks_at_all_records_of_the_object_first(tools):
    # Cancelled mocks: Delta is in no lookup of these records, but the same-name candidate account is (INT-0006). The
    # same-name count does not block the check (it is not this person). Over ALL records Internal_Interview__c holds
    # Delta (Interviewer__c 3), so the 0 comes from the other condition: no elsewhere scan, no "referenced elsewhere".
    tools.begin_question("how many cancelled mocks did delta person take")
    r = ok(tools, "count_records", {"object": "Internal_Interview__c",
                                    "filters": [F("Recruiter__c", "Delta Person"), F("Status__c", "Cancelled")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 0 and lc["records"] == 0
    assert all(o["records"] == 0 for o in lc["others"] if o["group"] == "same_target")
    same_name = next(o for o in lc["others"] if o["group"] == "same_name")
    assert same_name["field"] == "Candidate__c" and same_name["records"] == 1 and same_name["same_person"] is False
    assert "elsewhere" not in lc
    assert lc["all_records"] == {"Interviewer__c": FACTS["delta_interviewer"],
                                 "Interview_Readiness_Person__c": FACTS["delta_readiness"]}
    n2 = next(n for n in r["notes"] if "through other lookups of Internal_Interview__c" in n)
    assert ("'Delta Person' is on no Internal_Interview__c record under the other conditions of this query; over all "
            "Internal_Interview__c records (no other conditions; never add these): Interviewer__c (interviewer) 3, "
            "Interview_Readiness_Person__c (undocumented) 1") in n2
    assert "referenced elsewhere" not in n2 and "other object" not in n2
    # different records that only share the name have their own clause, never mixed with the person's counts
    assert ("; different records that only share the name (not this person; never present them as this person and never "
            "add them): Candidate__c (Account, a different Account record with the same name (Person Account), "
            "candidate) 1") in n2
    ev = ev_last(tools)
    assert ev.statements[0].sql.startswith('SELECT COUNT(*) AS n FROM main."Internal_Interview__c"')  # main first


def test_a_failing_or_late_lookup_check_gives_n12(tools, monkeypatch):
    def boom(*a, **k):
        from sf_assistant.engine.catalog import RecordError
        raise RecordError("boom", "E13")

    tools.begin_question()
    monkeypatch.setattr(tools, "_lookup_check", boom)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert r["count"] == 3 and "lookup_check" not in r
    assert "other lookups of Internal_Interview__c were not checked (the check failed)" in r["notes"]
    monkeypatch.undo()
    from sf_assistant.tools.record_base import _Call
    monkeypatch.setattr(_Call, "remaining", lambda self: 1.0)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert r["count"] == 3 and "lookup_check" not in r
    assert "other lookups of Internal_Interview__c were not checked (time budget)" in r["notes"]


def test_empty_almost_empty_and_unresolved_lookups(tools):
    tools.begin_question()
    r = ok(tools, "count_records", {"object": "Deliverable__c", "filters": [F("Approved_By__c", "Delta Person")]})
    assert r["count"] == 0
    assert ("Deliverable__c.Approved_By__c is blank on every Deliverable__c record in the warehouse (0 of 4), so no record "
            "can match 'Delta Person': the data does not record this; do not report it as 0 for 'Delta Person'") in r["notes"]
    ii = tools.catalog.objects["Internal_Interview__c"]
    rows = ii.rows
    try:
        ii.rows = 1000
        r = ok(tools, "count_records", {"object": "Internal_Interview__c",
                                        "filters": [F("Interview_Support_Person__c", "Recruiter C")]})
        n5b = next(n for n in r["notes"] if n.startswith("Internal_Interview__c.Interview_Support_Person__c is filled on "
                                                         "only 1 of 1000"))
        assert "the other lookups of Internal_Interview__c to Employee are Interviewer__c (interviewer)" in n5b
    finally:
        ii.rows = rows
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Recruiter C")]})
    assert ("1 of the 9 values of Interview__c.Recruiter__c point to Employee records that are not in the warehouse; "
            "records holding them cannot match a name and group as '(unresolved id)'") in r["notes"]


def test_lookup_check_marks_an_almost_empty_lookup_of_the_asked_role(tools):
    ii = tools.catalog.objects["Internal_Interview__c"]
    rows = ii.rows
    try:
        ii.rows = 1000   # Interview_Support_Person__c (1 row) is almost empty: never "fits", but "fits_sparse"
        tools.begin_question("how many mock interviews has recruiter c supported")
        r = ok(tools, "count_records", {"object": "Internal_Interview__c",
                                        "filters": [F("Interviewer__c", "Recruiter C")]})
        lc = r["lookup_check"][0]
        assert (lc["field"], lc["records"], lc["fits"], lc["cues"]) == ("Interviewer__c", 0, None, ["supported"])
        assert "fits_sparse" not in lc
        sup = next(o for o in lc["others"] if o["field"] == "Interview_Support_Person__c")
        assert (sup["records"], sup["fits"], sup["fits_sparse"]) == (1, None, "supported")
        assert all("fits_sparse" not in o for o in lc["others"] if o["field"] != "Interview_Support_Person__c")
        r = ok(tools, "count_records", {"object": "Internal_Interview__c",
                                        "filters": [F("Interview_Support_Person__c", "Recruiter C")]})
        lc = r["lookup_check"][0]
        assert (lc["records"], lc["fits"], lc["fits_sparse"]) == (1, None, "supported")
    finally:
        ii.rows = rows


def test_a_lookup_that_copies_a_path_is_marked_and_noted(tools):
    tools.begin_question()
    r = ok(tools, "describe_fields", {"object": "Candidate_Training__c", "query": "program"})
    row = next(f for f in r["fields"] if f["field"] == "Program__c")
    assert row["mirror_of"] == "Program_Version__c.Program__c"
    r = ok(tools, "group_records", {"object": "Candidate_Training__c", "group_by": "Program__c"})
    assert ("Candidate_Training__c.Program__c copies Program_Version__c.Program__c (The training's Program must equal "
            "the version's Program); Program_Version__c.Program__c is the source: group or filter by "
            "Program_Version__c.Program__c (the copy can be blank or out of date)") in r["notes"]
    assert r["copied_lookups"] == [{"field": "Program__c", "source": "Program_Version__c.Program__c"}]
    path = ok(tools, "group_records", {"object": "Candidate_Training__c", "group_by": "Program_Version__c.Program__c"})
    assert not any("copies" in n for n in path.get("notes") or []) and "copied_lookups" not in path
    by = {g["group"]: g["value"] for g in path["groups"]}
    assert by["Program Alpha"] == 2 and {g["group"]: g["value"] for g in r["groups"]}["Program Alpha"] == 1
    r = ok(tools, "count_records", {"object": "Candidate_Training__c", "filters": [F("Status__c", "Active")]})
    assert not any("copies" in n for n in r.get("notes") or [])


def test_a_text_field_compared_with_a_persons_name(tools):
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Interviewer_s_Name__c", "Delta Person")]})
    assert r["count"] == 0
    n10 = next(n for n in r["notes"] if n.startswith("'Delta Person' is the name of Employee (Recruiter__c) record(s)"))
    assert "Interview__c.Interviewer_s_Name__c is a text field (Name of the client's interviewer.)" in n10
    assert "lookups of Interview__c to Employee: Recruiter__c (recruiter)" in n10
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Interviewer_s_Name__c", "Client Person")]})
    assert r["count"] == 2 and not any("is a text field" in n for n in r.get("notes", []))


def test_a_user_lookup_with_an_employee_name(tools):
    tools.begin_question()
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("CreatedById", "Delta Person")]})
    assert e.startswith("no User record is named 'Delta Person'.")
    assert ("'Delta Person' is Employee (Recruiter__c) with no linked user (Recruiter_User__c (employee login user), "
            "portal user (User.AccountId = Recruiter_Contact__c) and Portal_User__c (employee portal login user) find no "
            "user), so User lookups (CreatedById, OwnerId) cannot match this Employee.") in e
    assert ev_last(tools).error_code == "E4h"
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("CreatedById", "Recruiter A")]})
    assert ("'Recruiter A' is Employee (Recruiter__c): User lookups hold Salesforce users, and this Employee is linked to "
            "'User One' (Recruiter_User__c (employee login user)); filter with that name.") in e


def test_group_and_trend_keys_for_lookups(tools):
    tools.begin_question()
    r = ok(tools, "group_records", {"object": "Session__c", "group_by": "Host_User__c"})
    assert r["same_target_lookups"] == [{"field": "Host_User__c", "role": "host employee", "target": "Recruiter__c",
                                         "target_label": "Employee", "others": [{"field": "Co_Host__c", "role": "co host"}]}]
    assert not any("Co_Host__c" in n for n in r.get("notes", []))       # a key, not a note
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Support_Ref__c"})
    assert ("Support_Ref__c is blank on 8 of the 10 matching records; if the question means another field, describe_fields "
            "shows how many records each field fills (filled)") in r["notes"]
    tools.begin_question("how many interviews delta person has taken type wise")
    r = ok(tools, "group_records", {"object": "Internal_Interview__c", "group_by": "Interview_Type__c", "split_by": "Status__c",
                                    "filters": [F("Interviewer__c", "Delta Person")]})
    assert {g["group"]: g["value"] for g in r["groups"]} == FACTS["delta_interviewer_by_type"]
    assert r["split_totals"] == FACTS["delta_split_totals"] and r["lookup_check"][0]["fits"] == "taken"
    r = ok(tools, "trend_records", {"object": "Internal_Interview__c", "grain": "month", "date_field": "CreatedDate",
                                    "period": "this year", "split_by": "Status__c",
                                    "filters": [F("Interviewer__c", "Delta Person")]})
    assert r["split_totals"] == FACTS["delta_split_totals"]
    r = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Recruiter__c"})
    assert any(n.startswith("1 of the 9 values of Interview__c.Recruiter__c point to Employee records") for n in r["notes"])


def test_describe_fields_shows_roles_and_lookup_groups(tools):
    r = ok(tools, "describe_fields", {"object": "Internal_Interview__c", "query": "interviewer"})
    row = next(f for f in r["fields"] if f["field"] == "Interviewer__c")
    assert row["role"] == "interviewer" and row["concept"] == "Employee conducting the interview."
    assert row["filled"] == 5 and row["from_target"] == "interviews this employee conducts"
    assert row["same_target"] == ["Recruiter__c", "Interview_Readiness_Person__c", "Interview_Support_Person__c"]
    assert all("filled" in f for f in r["fields"])
    grp = r["lookup_groups"][0]
    assert grp["target"] == "Recruiter__c" and grp["note"] == "several lookups to the same object: pick by role"
    assert grp["fields"][-1] == {"field": "Final_Decision_By__c", "role": "final decision maker", "not_in_warehouse": True}
    r = ok(tools, "describe_fields", {"object": "Interview__c", "query": "recruiter"})
    rec = next(f for f in r["fields"] if f["field"] == "Recruiter__c")
    assert rec["points_to_missing"] == 1
    r = ok(tools, "describe_fields", {"object": "Internal_Interview__c"})
    assert "lookup_groups" in r and all(set(f) == {"field", "label", "type"} for f in r["fields"])
    assert all("meaning" not in c for c in r["children"])
    names = [f["field"] for f in r["fields"]]
    assert names.index("Interview_Readiness_Person__c") < names.index("Meeting_Passcode_IS__c") if "Meeting_Passcode_IS__c" in \
        names else True


def test_fit_keeps_the_lookup_keys_and_trims_children_first(tools):
    big = {"query_id": "q1", "object": "X", "fields": [{"f": "x" * 300}] * 10, "children": [{"c": "y" * 300}] * 30,
           "lookup_groups": [{"target": "T"}], "lookup_check": [{"records": 0}], "notes": ["n"],
           "same_target_lookups": [1], "split_totals": {"a": 1}, "filler": "z" * 1000, "source": "s"}
    out = tools._fit(dict(big))
    assert len(json.dumps(out)) <= MAX_RESULT_CHARS
    assert len(out["children"]) < 30 and len(out["fields"]) == 10           # children are cut before fields
    huge = dict(big, filler="z" * 20000, fields=[{"f": "x" * 300}] * 30)
    out = tools._fit(huge)
    for key in ("query_id", "object", "notes", "lookup_check", "lookup_groups", "same_target_lookups", "split_totals",
                "source"):
        assert key in out
    assert out.get("trimmed") is True and "filler" not in out


def test_find_objects_lists_lookup_groups(tools):
    r = ok(tools, "find_objects", {"query": "internal mock interviews"})
    row = next(o for o in r["objects"] if o["object"] == "Internal_Interview__c")
    assert row["lookup_groups"][0].startswith("Recruiter__c (Employee): Interviewer__c (interviewer), Recruiter__c "
                                              "(recruiter)")


def _digits(text: str) -> bool:
    return bool(re.search(r"\d", text))


def test_object_hints_name_people_and_where_they_are_referenced(tools):
    tools.begin_question(DELTA_Q)
    h = tools.object_hints(DELTA_Q)
    assert ("People named in the question (code matched these words to stored names; filter with the full name):\n"
            "- 'delta person': Employee (Recruiter__c) 'Delta Person' (Active Employee); Account 'Delta Person' (Person "
            "Account, a different record)") in h
    where = h.split("Where these people are referenced", 1)[1].split("\nOther lookups", 1)[0]
    assert "- 'Delta Person' (Employee): Internal_Interview__c.Interviewer__c (interviewer: Employee conducting the " \
           "interview.) <- fits 'taken'; Internal_Interview__c.Interview_Readiness_Person__c (undocumented)" in where
    assert "- 'Delta Person' (Account, a different record): Internal_Interview__c.Candidate__c (candidate" in where
    other = h.split("Other lookups to these people's objects", 1)[1].split("\nStored values", 1)[0]
    assert "- Internal_Interview__c -> Employee: Recruiter__c (recruiter" in other
    assert "Interview_Support_Person__c (legacy; support employee" in other
    assert "- Interview__c -> Employee: Recruiter__c (recruiter" in other and "Support_Ref__c (undocumented)" in other
    assert h.index("Objects whose knowledge") < h.index("People named") < h.index("Stored values")
    assert not _digits(h), h
    assert len(h.split("People named", 1)[1].split("\nStored values", 1)[0]) <= 2000
    # "taken" can mean all assigned or only completed records: the hints ask for the status split up front
    assert ("'taken' can mean every record the person is assigned to or only the completed ones: in the same query "
            "keep the question's group_by and add split_by the counted object's status field (Status__c on "
            "Internal_Interview__c) (group_by it when the question asks for no grouping), then give both numbers.") in where


def test_object_hints_ask_for_the_status_split_only_for_completion_verbs(tools):
    h = tools.object_hints("how many interviews did echo one create")
    assert "can mean every record the person is assigned to" not in h        # "create" is not ambiguous
    h = tools.object_hints("how many mocks has delta person attended or taken")
    assert "can mean every record the person is assigned to or only the completed ones" in h


@needs_records_txt
def test_object_hints_stay_unchanged_without_a_person(tools):
    for q in ["interviews per recruiter", "internal interviews created this month"] + GOLDEN_QUESTIONS:
        h = tools.object_hints(q)
        assert "People named" not in h and "Other lookups" not in h and "(system)" not in h, q
    h = tools.object_hints("how many interviews did echo one create")
    assert "- Interview__c -> User (system): CreatedById (record creator) <- fits 'create'" in h
    assert "linked: User 'Echo One' (standard user)" in h and not _digits(h)
    h = tools.object_hints("how many internal interviews did echo one take this month")
    assert "(system)" not in h


def test_find_references_for_a_person(tools):
    tools.begin_question()
    r = ok(tools, "find_references", {"name": "Delta Person"})
    recs = {m["ref"]: (m["object"], m["how"]) for m in r["matched_records"]}
    assert recs == {"R1": ("Recruiter__c", "name"), "R2": ("Account", "name")}
    refs = {(x["record"], x["object"], x["field"]): x["records"] for x in r["references"]}
    assert refs == {("R1", "Internal_Interview__c", "Interviewer__c"): FACTS["delta_interviewer"],
                    ("R1", "Internal_Interview__c", "Interview_Readiness_Person__c"): FACTS["delta_readiness"],
                    ("R2", "Internal_Interview__c", "Candidate__c"): FACTS["delta_as_candidate"]}
    assert r["references"][0]["meaning"] == "interviews this employee conducts" and r["references"][0]["role"] == "interviewer"
    notes = " | ".join(r["notes"])
    assert "never add these counts" in notes                                         # F1
    assert "R1 (Employee) has no linked user (Recruiter_User__c (employee login user)" in notes   # F2
    assert "'Delta Person': P1 (employee), P2 (candidate) have the same name, not linked" in notes  # F3 (§22)
    assert r["applied"]["object"] == "person objects"
    assert r["not_in_warehouse"] == ["Internal_Interview__c.Final_Decision_By__c (final decision maker)"]
    ev = ev_last(tools)
    assert ev.tool == "about_people" and ev.kind == "data" and ev.summary == "3 lookups with records"   # alias logged
    assert ev.arguments == {"name": "Delta Person"} and r["applied"]["names"] == ["Delta Person"]
    assert ev.statements[0].sql.startswith("SELECT a1.") and "CROSS JOIN" in ev.statements[0].sql
    r = ok(tools, "find_references", {"name": "Echo One"})
    objs = [(m["object"], m["how"]) for m in r["matched_records"]]
    assert objs[0] == ("Recruiter__c", "name") and len(objs) == 4                   # one employee, 2 users, 1 account
    assert all(h.endswith(" of R1") for _o, h in objs[1:])
    assert any(n.endswith("are logins of the same Employee R1") for n in r["notes"])  # F5
    r = ok(tools, "find_references", {"name": "Recruiter A"})
    host = next(x for x in r["references"] if x["field"] == "Host_User__c")
    assert host["by_field"] == "Purpose__c" and host["by"] == {"Training": 2}
    assert any(n.startswith("Session__c.Host_User__c rows mix roles or purposes (Purpose__c)") for n in r["notes"])  # F4
    assert {(x["field"], x["records"]) for x in r["system_references"]} >= {("CreatedById", FACTS["interviews"])}
    r = ok(tools, "find_references", {"name": "Echo"})   # a first name lists the people who have it (§22)
    assert [(p["label"], p["kind"], p["match"]) for p in r["people"]] == [("Echo One", "employee", "first name"),
                                                                          ("Echo Two", "employee", "first name")]
    e = err(tools, "find_references", {"name": "Zulu Echo"})
    assert e.startswith("no Employee, User or Account record is named 'Zulu Echo'.")
    assert ev_last(tools).error_code == "E15a"
    from warehouse_fixture import REC_B2, REC_D
    r = ok(tools, "find_references", {"name": REC_D})
    assert r["matched_records"][0]["how"] == "Id" and r["references"][0]["records"] == FACTS["delta_interviewer"]
    r = ok(tools, "find_references", {"name": "Recruiter B (#2)"})
    assert r["matched_records"][0]["label"] == "Recruiter B (#2)" and r["matched_records"][0]["detail"] == "Former Employee"
    assert {x["field"]: x["records"] for x in r["references"]} == {"Interviewer__c": 1, "Recruiter__c": 1}
    assert REC_B2 not in json.dumps(r)                                               # Ids are not shown


def test_new_statements_pass_the_guard_and_results_fit(tools):
    tools.begin_question(DELTA_Q)
    for name, args in [("count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Delta Person")]}),
                       ("find_references", {"name": "Echo One"}),
                       ("group_records", {"object": "Interview__c", "group_by": "Recruiter__c",
                                          "split_by": "Interview_Status__c"}),
                       ("describe_fields", {"object": "Internal_Interview__c"})]:
        out = tools.call(name, args)
        assert out["ok"], out["error"]
        assert len(json.dumps(out["result"])) <= MAX_RESULT_CHARS
        assert not ev_last(tools).error_code


def test_n1_where_no_lookup_check_runs_and_n7_for_partial_names(tools):
    tools.begin_question()
    n1 = ("Interviewer__c is one of 4 lookups from Internal_Interview__c to Employee; this result uses Interviewer__c "
          "(interviewer); the others are Recruiter__c (recruiter), Interview_Readiness_Person__c (undocumented), "
          "Interview_Support_Person__c (support employee)")
    r = ok(tools, "percentage_of_records", {"object": "Internal_Interview__c",
                                            "part_filters": [F("Interviewer__c", "Delta Person")]})
    assert r["part"] == FACTS["delta_interviewer"] and n1 in r["notes"] and "lookup_check" not in r
    r = ok(tools, "compare_records", {"object": "Internal_Interview__c", "segments": [
        {"label": "d", "filters": [F("Interviewer__c", "Delta Person")]},
        {"label": "e", "filters": [F("Interviewer__c", "Echo One")]}]})
    assert [s["value"] for s in r["segments"]] == [FACTS["delta_interviewer"], FACTS["echo_one_interviewer"]]
    assert n1 in r["notes"]
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "any_of": [
        F("Interviewer__c", "Delta Person"), F("Status__c", "Cancelled")]})
    assert n1 in r["notes"] and "lookup_check" not in r
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Echo", "contains")]})
    assert r["count"] == FACTS["echo_one_interviewer"]
    assert "'Echo' matches 2 different Employee names (Echo One, Echo Two); the counts combine them" in r["notes"]
    e = err(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Zulu", "contains")]})
    assert e.startswith("no Employee (Recruiter__c) record name contains 'Zulu'.") and ev_last(tools).error_code == "E4h"
    err(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Nope__c", "x")]})
    assert ev_last(tools).error_code == "E2"


def test_find_references_with_object_and_sensitive_objects(tools):
    # §22: no E15b (a name held by many records lists them, capped); object limits the search to one person object
    tools.begin_question()
    r = ok(tools, "find_references", {"name": "Delta Person", "object": "Account"})
    assert [(m["object"], m["how"]) for m in r["matched_records"]] == [("Account", "name")]
    assert [p["kind"] for p in r["people"]] == ["candidate"]
    assert "holds portal credentials" in err(tools, "find_references", {"name": "CCN-0001",
                                                                       "object": "Candidate_Portal_Credential__c"})


# ----------------------------------------------------------------------------------- review fixes (2026-10-14)

def test_a_period_zero_is_not_sent_elsewhere_when_the_object_holds_the_person(tools):
    # Recruiter A is the recruiter of 4 client interviews, none dated last year: the 0 comes from the period.
    from sf_assistant.agent.record_session import lookup_nudges
    tools.begin_question("how many client interviews did recruiter a recruit for last year")
    r = ok(tools, "count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "last year",
                                    "filters": [F("Recruiter__c", "Recruiter A")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 0 and lc["records"] == 0 and lc["fits"] == "recruit"
    assert "elsewhere" not in lc and lc["all_records"] == {"Recruiter__c": 4}
    n2 = next(n for n in r["notes"] if "through other lookups of Interview__c" in n)
    assert "over all Interview__c records (no other conditions; never add these): Recruiter__c (recruiter) 4" in n2
    assert lookup_nudges("Recruiter A recruited for 0 client interviews last year (`Recruiter__c`).", tools.evidence) == []
    # Without other conditions the object's own count is the answer: no all-records statement.
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", "Recruiter A")]})
    assert r["count"] == 4 and "all_records" not in r["lookup_check"][0]


def test_non_person_lookups_get_no_person_wording_and_no_reference_scan(tools):
    # 'Recruiter A' as Candidate__c is the employee's own Account (record type Recruiter): not a candidate, kind other.
    from sf_assistant.agent.record_session import lookup_nudges
    tools.begin_question("how many client interviews has recruiter a next year")
    r = ok(tools, "count_records", {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "next year",
                                    "filters": [F("Candidate__c", "Recruiter A")]})
    lc = r["lookup_check"][0]
    assert lc["person_kind"] == "other" and lc["records"] == 0
    assert "elsewhere" not in lc and "all_records" not in lc     # the linked employee hosts sessions: never scanned
    assert not any("other object" in n or "referenced elsewhere" in n for n in r["notes"])
    assert lookup_nudges("Recruiter A has 0 client interviews next year.", tools.evidence) == []
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Candidate__c", "Recruiter A")]})
    n2 = next(n for n in r["notes"] if n.startswith("'Recruiter A' through other lookups of Interview__c"))
    assert "a record can hold the same value in several lookups" in n2 and "same person" not in n2


def test_ids_of_fifteen_characters_and_unknown_ids(tools):
    from sf_assistant.engine.values import id18
    from warehouse_fixture import ORPHAN_REC, REC_D, sid
    assert id18("001A0000006Vm9r") == "001A0000006Vm9rIAC" and id18(REC_D[:15]) == REC_D and id18(REC_D) == REC_D
    assert id18("Delta Person") == "Delta Person"
    tools.begin_question()
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", REC_D[:15])]})
    assert r["count"] == FACTS["delta_interviewer"] and r["lookup_check"][0]["records"] == FACTS["delta_interviewer"]
    r = ok(tools, "list_records", {"object": "Recruiter__c", "filters": [F("Id", REC_D[:15])], "fields": ["Name"]})
    assert r["total_count"] == 1
    r = ok(tools, "find_references", {"name": REC_D[:15]})
    assert r["matched_records"][0]["how"] == "Id" and r["references"][0]["records"] == FACTS["delta_interviewer"]
    # An Id that is no record of the target: E4h, not a clean 0.
    e = err(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", sid("a06", 50))]})
    assert e.startswith("no Employee (Recruiter__c) record has Id") and ev_last(tools).error_code == "E4h"
    # A lookup that also holds Ids of records that are not synced: the Id is still counted, with a note.
    r = ok(tools, "count_records", {"object": "Interview__c", "filters": [F("Recruiter__c", ORPHAN_REC)]})
    assert r["count"] == 1 and any(n.startswith("no Employee (Recruiter__c) record in the warehouse has Id")
                                   for n in r["notes"])


def test_several_people_get_no_elsewhere_scan(tools):
    tools.begin_question("how many mocks did the recruiters take next year")
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "date_field": "CreatedDate", "period": "next year",
                                    "filters": [F("Interviewer__c", "Recruiter", "contains")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 0 and "elsewhere" not in lc and "all_records" not in lc
    assert any("other objects were not checked ('Recruiter' matches several people)" in n for n in r["notes"])
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "date_field": "CreatedDate", "period": "next year",
                                    "filters": [F("Interviewer__c", "Recruiter B")]})
    assert "elsewhere" not in r["lookup_check"][0] and "by_record" in r["lookup_check"][0]


def test_no_reference_anywhere_is_scoped_to_what_was_scanned(tools):
    tools.begin_question()
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Echo Two")]})
    n2 = next(n for n in r["notes"] if "through other lookups of Internal_Interview__c" in n)
    assert re.search(r"; no lookup of Internal_Interview__c or of any other object the record tools can query holds "
                     r"'Echo Two' \(\d+ other objects scanned; objects without a knowledge file and sensitive objects are "
                     r"not scanned\)", n2), n2
    r = ok(tools, "find_references", {"name": "Echo Two"})
    assert "objects without a knowledge file and sensitive objects are not scanned" in r["not_scanned"]


def test_group_min_value_check_counts_before_having(tools):
    tools.begin_question("which interview types has delta person taken at least twice")
    r = ok(tools, "group_records", {"object": "Internal_Interview__c", "group_by": "Interview_Type__c", "min_value": 2,
                                    "filters": [F("Interviewer__c", "Delta Person")]})
    lc = r["lookup_check"][0]
    assert r["total_across_groups"] == 2 and lc["records"] == FACTS["delta_interviewer"]
    assert lc["scope"] == "all other conditions of this query, before min_value"
    assert any("this result used Interviewer__c (interviewer): 3 (all matching records, before min_value removed groups)"
               in n for n in r["notes"])


def test_a_same_name_user_is_never_counted_as_the_person(tools):
    # Foxtrot Person: one login user (Recruiter_User__c) and a different, unlinked User with the same name.
    tools.begin_question("how many mock interviews did foxtrot person create")
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Recruiter__c", "Foxtrot Person")]})
    lc = r["lookup_check"][0]
    created = [o for o in lc["others"] if o["field"] == "CreatedById"]
    assert len(created) == 2
    own = next(o for o in created if o["group"] == "system")
    other = next(o for o in created if o["group"] == "same_name")
    assert own["records"] == FACTS["foxtrot_created_by_login"] and own["same_person"] is True
    assert "Recruiter_User__c (employee login user)" in own["via"]
    assert other["records"] == FACTS["foxtrot_created_by_other_user"] and other["same_person"] is False
    assert other["via"] == "a different User record with the same name (standard user)"
    assert "Foxtrot" not in other["via"]
    n2 = next(n for n in r["notes"] if "through other lookups of Internal_Interview__c" in n)
    mine, alike = n2.split("; different records that only share the name", 1)
    assert "CreatedById (User, 'Foxtrot Person' (Recruiter_User__c (employee login user)), record creator) 2" in mine
    assert "CreatedById (User, a different User record with the same name (standard user), record creator) 1" in alike


def test_find_references_numbered_labels_and_sensitive_objects(tools):
    from warehouse_fixture import USER_3, sid
    tools.begin_question()
    r = ok(tools, "find_references", {"name": "Echo One"})
    label = next(m["label"] for m in r["matched_records"] if m["how"].startswith("Recruiter_User__c"))
    assert label == "Echo One (#2)"
    r = ok(tools, "find_references", {"name": label})            # the label works without object
    user = next(m for m in r["matched_records"] if m["label"] == label)
    assert user["object"] == "User" and user["detail"] == "standard user"
    assert USER_3 not in json.dumps(r)
    e = err(tools, "find_references", {"name": "Echo One (#3)"})
    assert e.startswith("'Echo One' has no #3 in any object that has the name (Employee: #1") and "User: #1" in e
    assert ev_last(tools).error_code == "E15a"
    e = err(tools, "find_references", {"name": "Delta Person", "in_objects": ["Candidate_Portal_Credential__c"]})
    assert "holds portal credentials" in e and ev_last(tools).error_code == "E1d"
    e = err(tools, "find_references", {"name": sid("a0H", 1)})
    assert "holds portal credentials" in e and ev_last(tools).error_code == "E1d"


def test_name_index_and_hint_identities_are_time_bounded(tmp_path, monkeypatch):
    import time as _time

    import sf_assistant.tools.names as N   # the name index reads its budget from tools.names
    from sf_assistant.engine.catalog import RecordError
    t = _open(tmp_path)
    try:
        monkeypatch.setattr(N, "NAME_INDEX_BUDGET_S", 0.0)
        t.begin_question()
        assert t.names_person("where does delta person appear") is False   # the index stopped before any statement
        assert t._names_partial is True
        with pytest.raises(RecordError):
            t._identities(None, [("Recruiter__c", "a06000000000006AAA")], deadline=_time.monotonic() - 1)
    finally:
        t.close()
    monkeypatch.undo()
    t = _open(tmp_path / "b")
    try:
        assert t.names_person("where does delta person appear") is True
        assert t.names_person("where does Delta  PERSON appear?") is True
        assert t.names_person("what values can Status__c take") is False
        assert t.names_person("how many mocks has delta taken") is False             # a first name only
    finally:
        t.close()


# ------------------------------------------------------------------------------- eval round 1 fixes (2026-10-14)

def test_a_picked_record_still_gets_the_lookup_check_with_the_same_name_employee(tools):
    """'Name (#k)' on a candidate lookup: the Employee with the same name (a different record) is still counted, and its
    lookup is marked as fitting the verb for an employee (own_kind, own_fits)."""
    tools.begin_question(DELTA_Q)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Candidate__c", "Delta Person (#1)")]})
    assert r["count"] == FACTS["delta_as_candidate"]
    lc = r["lookup_check"][0]
    assert lc["field"] == "Candidate__c" and lc["person_kind"] == "candidate" and lc["fits"] == "taken"
    emp = next(o for o in lc["others"] if o["field"] == "Interviewer__c")
    assert emp["group"] == "same_name" and emp["records"] == FACTS["delta_interviewer"] and emp["same_person"] is False
    assert emp["own_kind"] == "employee" and emp["own_fits"] == "taken" and emp["fits"] is None
    n2 = next(n for n in r["notes"] if "through other lookups of Internal_Interview__c" in n
              or "held by no other lookup of Internal_Interview__c" in n)
    assert "different records that only share the name" in n2 and "Interviewer__c (Employee" in n2
    # the same filter without the number gives the same check
    r2 = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Candidate__c", "Delta Person")]})
    assert {o["field"]: o["records"] for o in r2["lookup_check"][0]["others"]} == \
        {o["field"]: o["records"] for o in lc["others"]}


def test_hints_say_the_employee_is_meant_when_the_verb_fits_both_records(tools):
    tools.begin_question(DELTA_Q)
    h = tools.object_hints(DELTA_Q)
    assert ("'taken' fits both the Employee 'Delta Person' (Internal_Interview__c.Interviewer__c) and a different Account "
            "record with the same name (Internal_Interview__c.Candidate__c): unless the question says candidate, answer "
            "for the Employee with Interviewer__c") in h
    assert not _digits(h)
    h = tools.object_hints("how many interviews did echo one create")
    assert "fits both" not in h


def test_stored_values_before_an_object_word_are_no_role_cues(tools):
    cat = tools.catalog
    q = "how many training sessions did delta person host"
    assert cat.value_words(q) == {"training"}
    assert cat.question_cues(q, "employee") == ["host"]
    host = cat.objects["Session__c"].fields["Host_User__c"]
    assert cat.role_fits(q, cat.objects["Session__c"], host, "employee") == "host"
    # a verb stays a cue: no object word follows it
    assert cat.question_cues("which candidates is delta person training", "employee") == ["training"]
    assert cat.value_words("which candidates is delta person training") == frozenset()
    q = "how many training sessions did recruiter a host"
    tools.begin_question(q)
    h = tools.object_hints(q)
    assert ("- 'Training' in Session__c.Purpose__c <- the question says 'training' right before 'session': a condition "
            "of the question, filter on it") in h
    assert "fits 'training'" not in h and not _digits(h)
    r = ok(tools, "count_records", {"object": "Session__c", "filters": [F("Host_User__c", "Recruiter A"),
                                                                      F("Purpose__c", "Training")]})
    assert r["count"] == 2 and r["lookup_check"][0]["cues"] == ["host"] and r["lookup_check"][0]["fits"] == "host"


DECISION_Q = "how many interview decisions has delta person made"


def test_a_documented_lookup_the_question_fits_that_is_not_synced(tools):
    cat = tools.catalog
    ii = cat.objects["Internal_Interview__c"]
    assert cat.not_synced_fits(DECISION_Q, ii) == [{"field": "Final_Decision_By__c", "role": "final decision maker",
                                                    "target": "Recruiter__c", "target_label": "Employee",
                                                    "fits": "decisions"}]
    assert cat.not_synced_fits(DELTA_Q, ii) == []
    tools.begin_question(DECISION_Q)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert r["unsynced_fits"] == [{"field": "Final_Decision_By__c", "role": "final decision maker",
                                   "target": "Recruiter__c", "target_label": "Employee", "fits": "decisions",
                                   "proxy": ["Internal_Interview__c.Interviewer__c"]}]
    assert any(n.startswith("Internal_Interview__c.Final_Decision_By__c (final decision maker) fits the question's word "
                            "'decisions' but is documented only, not synced to the warehouse") and "proxy" in n
               for n in r["notes"])
    # a grouping on a person lookup reaches the person too
    r = ok(tools, "group_records", {"object": "Internal_Interview__c", "group_by": "Interviewer__c"})
    assert r["unsynced_fits"][0]["field"] == "Final_Decision_By__c"
    # no person in the query, or a question the lookup does not fit: nothing
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Status__c", "Completed")]})
    assert "unsynced_fits" not in r
    tools.begin_question(DELTA_Q)
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert "unsynced_fits" not in r
    # the hints name it too
    tools.begin_question(DECISION_Q)
    h = tools.object_hints(DECISION_Q)
    assert ("Lookups the question's words fit that are not in the warehouse:\n- Internal_Interview__c.Final_Decision_By__c "
            "(final decision maker) fits 'decisions' but is documented only, not in the warehouse") in h
    assert not _digits(h)


def test_related_with_a_parent_object_says_how_to_filter_through_it(tools):
    tools.begin_question()
    for item in ({"child_object": "Candidate_Training__c", "exists": True},
                 {"child_object": "Candidate_Training__c", "exists": True, "via_field": "Candidate_Training__c"}):
        e = err(tools, "count_records", {"object": "Session__c", "related": [item]})
        assert e.startswith("Candidate_Training__c is not a child of Session__c: Session__c.Candidate_Training__c points "
                            "to it, so it is a parent.") and '"field": "Candidate_Training__c.<field of' in e, e
        assert ev_last(tools).error_code == "E9c"
    # a real child still works
    r = ok(tools, "count_records", {"object": "Candidate_Training__c", "related": [{"child_object": "Session__c",
                                                                                   "exists": True}]})
    assert r["count"] == 2


def test_unknown_field_suggestions_put_the_objects_own_suffix_match_first(tools, monkeypatch):
    """'Status__c' on an object without it: fields whose API name ends with '_Status__c' come first (difflib alone
    misses a long name such as Candidate_Background_Check_Status__c), the object's own <Object>_Status__c first."""
    import dataclasses
    cat = tools.catalog
    spec = cat.objects["Interview__c"]
    long_name = dataclasses.replace(spec.fields["Interview_Status__c"], api="Candidate_Background_Check_Status__c",
                                    label="Candidate Background Check Status", meaning="Pipeline step of the check.")
    monkeypatch.setitem(spec.fields, "Candidate_Background_Check_Status__c", long_name)
    got = [f.api for f in cat.suggest_fields(spec, "Status__c")]
    assert got[:2] == ["Interview_Status__c", "Candidate_Background_Check_Status__c"], got
    monkeypatch.undo()
    e = err(tools, "count_records", {"object": "Interview__c", "filters": [F("Status__c", "Completed")]})
    assert "Closest: Interview_Status__c (" in e


def test_describe_fields_brings_in_the_fields_a_mostly_blank_fields_meaning_names(tools, monkeypatch):
    spec = tools.catalog.objects["Interview__c"]
    f = spec.fields["Client_Bill_Rate__c"]
    monkeypatch.setattr(f, "meaning", "Bill rate. Rarely set; the contract kind is in Employment_Type__c.")
    r = ok(tools, "describe_fields", {"object": "Interview__c", "query": "bill rate"})
    names = [x["field"] for x in r["fields"]]
    assert names[names.index("Client_Bill_Rate__c") + 1] == "Employment_Type__c"
    assert any(n.startswith(f"Client_Bill_Rate__c is filled on only {f.filled} of 10 Interview__c records; its meaning "
                            "names Employment_Type__c") for n in r["notes"])
    # several named fields: the one whose label or meaning has a query word (object words aside) comes first
    monkeypatch.setattr(f, "meaning", "Bill rate. Rarely set; see Interview_Outcome__c or Employment_Type__c.")
    r = ok(tools, "describe_fields", {"object": "Interview__c", "query": "interview bill rate type"})
    names = [x["field"] for x in r["fields"]]
    i = names.index("Client_Bill_Rate__c")
    assert names[i + 1] == "Employment_Type__c" and "Interview_Outcome__c" in names, names   # already listed: not moved
    assert any("names Employment_Type__c (filled 9; its label or meaning says 'type'), Interview_Outcome__c (filled"
               in n for n in r["notes"]), r["notes"]


def test_shared_first_names_and_misspelled_names_in_the_hints(tools):
    q = "how many mock interviews did echo take"
    tools.begin_question(q)
    h = tools.object_hints(q)
    assert ("- 'echo' is only a first name, and several people have it: 'Echo One', 'Echo Two'. Answer for each of them "
            "(one query: filter the lookup with op in and these full names, group_by that lookup) or ask which one") in h
    assert tools.question_people() == [{"words": "echo", "names": ["Echo One", "Echo Two"], "first_only": True,
                                        "asked": None}]
    q = "how many mock interviews did delta persn take"
    tools.begin_question(q)
    h = tools.object_hints(q)
    assert ("- 'delta persn' is not a stored name; the closest stored name is 'Delta Person'. If you answer for it, say "
            "that no record is named 'delta persn' and which stored name you used.") in h
    read = {"read_as": "Delta Person"} if tools.catalog.resolver is not None else {}   # SFA_RESOLVER=on: the reading
    assert tools.question_people() == [{"words": "delta persn", "names": ["Delta Person"], "first_only": False,
                                        "asked": "delta persn", **read}]
    assert not _digits(h)
    # a word after the first name that is no close spelling of a surname is not read as one
    q = "how many mock interviews did echo really take"
    tools.begin_question(q)
    assert tools.question_people()[0]["asked"] is None
    tools.begin_question("how many mock interviews has delta person taken")
    assert tools.question_people() == [{"words": "delta person", "names": ["Delta Person"], "first_only": False,
                                        "asked": None}]
    tools.begin_question()
    assert tools.question_people() == []
    # a name at the end of a question ("... delta person?") is still found as the full name
    tools.begin_question("how many mock interviews were taken by delta person?")
    assert [p["words"] for p in tools.question_people()] == ["delta person"]
    assert tools.names_person("where does delta person appear?")


# ------------------------------------------------------------------- round 3 fixes (2026-10-14; structured keys)

def test_a_person_filter_on_an_empty_lookup_sets_no_data_fields(tools):
    tools.begin_question("how many deliverables has delta person approved")
    r = ok(tools, "count_records", {"object": "Deliverable__c", "filters": [F("Approved_By__c", "Delta Person")]})
    assert r["count"] == 0 and r["no_data_fields"] == ["Deliverable__c.Approved_By__c"]
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert "no_data_fields" not in r


def test_by_record_entries_carry_the_person_kind(tools):
    tools.begin_question("how many mocks did recruiter b take")
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Recruiter B")]})
    assert {b["kind"] for b in r["lookup_check"][0]["by_record"]} == {"employee"}


def test_question_objects_for_a_noun_without_qualifier_or_role_word(tools, monkeypatch):
    """'Recruiter A' is referenced on Interview__c and Internal_Interview__c, whose labels both end in 'Interview'."""
    q = "how many interviews does recruiter a have"
    tools.begin_question(q)
    h = tools.object_hints(q)
    objs = tools.question_objects()
    assert [(x["name"], x["word"], [o["object"] for o in x["objects"]]) for x in objs] == \
        [("Recruiter A", "interviews", ["Interview__c", "Internal_Interview__c"])]
    assert objs[0]["objects"][0]["refs"][0]["field"] == "Recruiter__c"
    assert ("'interviews' (no qualifier, no role word) fits Interview__c (Interview) and Internal_Interview__c (Internal "
            "Interview), and 'Recruiter A' is referenced on each") in h
    # a qualifier before the noun, a role word, a noun used only as a modifier, or no person: nothing
    for other in ("how many internal interviews does recruiter a have", "how many client interviews does recruiter a have",
                  "how many interviews did recruiter a conduct", "how many interview slots does recruiter a have",
                  "how many interviews were cancelled"):
        tools.begin_question(other)
        tools.object_hints(other)
        assert tools.question_objects() == [], other
    tools.begin_question("x")
    assert tools.question_objects() == []
    # only objects documented as the same kind of record are grouped: a shared label head word alone is not enough
    import sf_assistant.tools.names as tools_module   # question_objects reads SAME_NOUN_OBJECTS there
    monkeypatch.setattr(tools_module, "SAME_NOUN_OBJECTS", ())
    tools.begin_question(q)
    tools.object_hints(q)
    assert tools.question_objects() == []


# ------------------------------------------------------------------- follow-up round 1 fixes (2026-10-14)

def test_describe_fields_puts_the_field_the_query_names_first_with_its_redirect(tools, monkeypatch):
    """Q18 shape: a bare "type" query. The object's own type field ("Interview Type": object word + query word) is
    almost blank and its meaning sends the reader to the field that holds the type; it ranks first, the named field
    comes right after it, its "use X" clause reaches the row even when the shortened meaning cuts it off, and a field
    that only shares the word ("Rate Type") gets a note without a 'says' mark."""
    from sf_assistant.tools.discovery import _label_fits_query, _own_label_field
    spec = tools.catalog.objects["Interview__c"]
    f, g = spec.fields["Client_Bill_Rate__c"], spec.fields["Interviewer_s_Name__c"]
    monkeypatch.setattr(f, "label", "Interview Type")
    monkeypatch.setattr(f, "meaning", "Kind of client interview round, almost never filled (3 of 10 records in the "
                        "synthetic warehouse, set by no create flow and no automation of the org): Interview_Status__c "
                        "(the state) and Employment_Type__c (the position type) are the main fields, so use "
                        "Employment_Type__c for the type of an interview. Shown in a calendar event only.")
    monkeypatch.setattr(g, "label", "Rate Type")
    monkeypatch.setattr(g, "meaning", "Stored rate type; the live value is Interview_Outcome__c.")
    monkeypatch.setattr(spec, "_field_index", None)   # rebuilt from the labels above
    assert _own_label_field(spec, f, "type") and not _own_label_field(spec, g, "type")
    assert _label_fits_query(spec, f, "type") and not _label_fits_query(spec, g, "type")
    assert _label_fits_query(spec, g, "rate type") and not _own_label_field(spec, spec.fields["RecordTypeId"], "type")
    tools.begin_question("how many interviews does recruiter a have type wise")
    r = ok(tools, "describe_fields", {"object": "Interview__c", "query": "type"})
    names = [x["field"] for x in r["fields"]]
    assert names[:2] == ["Client_Bill_Rate__c", "Employment_Type__c"], names
    row = r["fields"][0]
    assert row["redirect"] == "Use Employment_Type__c for the type of an interview."
    assert "use Employment_Type__c" not in row["meaning"]   # cut off by the shortened meaning
    notes = r["notes"]
    own = next(i for i, n in enumerate(notes) if n.startswith("Client_Bill_Rate__c is filled on only 3 of 10"))
    assert ("its meaning names Employment_Type__c (filled 9; its label or meaning says 'type'), Interview_Status__c "
            "(filled 10): use the field the meaning names") in notes[own]
    shared = next(i for i, n in enumerate(notes) if n.startswith("Interviewer_s_Name__c (Rate Type) is filled on only"))
    assert own < shared   # the field the query names comes first
    assert notes[shared].endswith("its meaning names Interview_Outcome__c (filled 6): they stand in for "
                                  "Interviewer_s_Name__c only when the question asks about Rate Type, not when it only "
                                  "shares a word with it")
    assert "says" not in notes[shared]
    # a meaning whose redirect is already visible gets no redirect key
    assert "redirect" not in next(x for x in r["fields"] if x["field"] == "Interviewer_s_Name__c")


def test_a_value_lookup_check_says_which_records_hold_the_value(tools):
    """Q38 shape: a department value through Department__c (2) while Team__c, another lookup to Department__c, holds
    it on 0 records: the note must not read as "0 employees have a team"."""
    tools.begin_question("how many employees are in dept one")
    r = ok(tools, "count_records", {"object": "Recruiter__c", "filters": [F("Department__c", "Dept One")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 2 and lc["person_kind"] == "other"
    # remaining round 1, A Q38: "Team__c (team unit) = 'X' on 0 records" was still read as "Team__c is blank on all 25":
    # another lookup that does not point to the value is left out of the result and the note
    assert lc["others"] == []
    n2 = next(n for n in r["notes"] if n.startswith("'Dept One' is held by no other lookup of Recruiter__c"))
    assert n2.endswith("this result used Department__c (department) = 'Dept One': 2")
    assert "Team__c" not in json.dumps(r)
    # a person check keeps its wording
    r = ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Recruiter__c", "Delta Person")]})
    n2 = next(n for n in r["notes"] if n.startswith("'Delta Person' through other lookups of Internal_Interview__c"))
    assert "Interviewer__c (interviewer) 3" in n2 and "whose lookup points to" not in n2


def test_a_name_that_is_not_stored_is_logged_on_the_evidence(tools):
    """Q36 shape: the failed call says which name it did not find (QueryEvidence.not_found), so code can build the
    not-found answer; a call that succeeds logs none."""
    tools.begin_question("how many mock interviews did zulu nobody conduct")
    e = err(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Zulu Nobody")]})
    assert "is named 'Zulu Nobody'" in e
    ev = tools.evidence[-1]
    assert ev.error_code == "E4h" and ev.not_found == [
        {"name": "Zulu Nobody", "target": "Employee (Recruiter__c)", "object": "Internal_Interview__c",
         "field": "Interviewer__c", "match": "exact", "close": []}]
    err(tools, "find_references", {"name": "Zulu Nobody"})
    ev = tools.evidence[-1]
    assert ev.error_code == "E15a" and [x["name"] for x in ev.not_found] == ["Zulu Nobody"]
    ok(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert tools.evidence[-1].not_found == []
    # a misspelt name logs the close names the error lists; the corrected call logs the name its filter matched
    e = err(tools, "count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Persen")]})
    assert "Close spellings: Delta Person" in e and tools.evidence[-1].not_found[0]["close"] == ["Delta Person"]
    ok(tools, "list_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
    assert tools.evidence[-1].names_matched == [{"filter": "Interviewer__c.Name", "names": ["Delta Person"],
                                                 "match": "exact"}]
    ok(tools, "list_records", {"object": "Recruiter__c"})
    assert tools.evidence[-1].names_matched == []


def test_count_distinct_on_a_lookup_names_what_it_counts_and_the_lookup_the_question_means(tools):
    """Remaining round 1, A Q25: "how many candidates has X trained" counted different Candidate_Owner__c (User) values,
    8, and called them candidates (16 through Candidate__c). The counted text says which records a distinct lookup count
    counts, and a lookup whose label is another lookup's label plus a role word gets a note naming that lookup."""
    import dataclasses

    tools.begin_question("how many interviews have a candidate")
    r = ok(tools, "count_records", {"object": "Interview__c", "count_distinct": "Candidate__c"})
    assert r["counted"] == ("different Candidate__c values (records with a value): each a different Account record "
                            "(candidate)")
    assert not any("not the candidate" in n for n in r["notes"])
    spec = tools.catalog.objects["Candidate_Training__c"]
    cand = spec.fields["Candidate__c"]
    owner = dataclasses.replace(cand, api="Candidate_Owner__c", label="Candidate Owner", ref_targets=["User"],
                                ref_role="candidate_owner", filled=max(cand.filled, 1))
    login = dataclasses.replace(owner, api="Candidate_User__c", label="Candidate User")
    tools.begin_question("how many candidates has recruiter a trained")
    assert tools._distinct_alternative(spec, owner) is cand
    assert tools._distinct_alternative(spec, login) is None          # a login of the same person, not another role
    assert tools._distinct_alternative(spec, cand) is None           # the label of Candidate__c has no extra words
    tools.begin_question("how many owners does recruiter a have")
    assert tools._distinct_alternative(spec, owner) is None          # the question does not ask about candidates


# ------------------------------------------------------------- questions about named people (RECORDS_SPEC §22.11)

@pytest.fixture(scope="module")
def ptools(tmp_path_factory):
    """The larger fixture with leads, candidates, a reporting line, a training chain and company-only interviews."""
    from warehouse_fixture import open_people_fixture
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_people_fixture(tmp_path_factory.mktemp("people"))
    yield t
    t.close()


def _people(r):
    return [(p["label"], p["kind"]) for p in r["people"]]


def test_spot_first_names_cover_every_person_object(ptools):
    spots = ptools.spot_people("who is kilo")
    assert len(spots) == 1 and spots[0]["words"] == "kilo" and spots[0]["first_only"]
    assert {r[0] for r in spots[0]["recs"]} == {"Recruiter__c", "Account", "Lead"}
    assert spots[0]["strength"] == "weak"     # a first name alone needs the names call


def test_first_name_index_keeps_person_accounts_not_companies(ptools):
    _full, _fl, first = ptools._name_index()
    assert "client" not in first                                    # 'Client Co': IsPersonAccount false
    assert any(r[0] == "Account" for r in first["recruiter"])       # an employee's own (person) account stays


def test_spot_possessive_straight_and_curly(ptools):
    for q in ("is uniform vale in kilo's training?", "is uniform vale in kilo’s training?"):
        assert [d["words"] for d in ptools.spot_people(q)] == ["uniform vale", "kilo"], q
    assert [d["words"] for d in ptools.spot_people("Sierra Tango’s sessions")] == ["sierra tango"]
    assert ptools.spot_people("Sierra Tango’s sessions")[0]["strength"] == "strong"


def test_spot_first_and_last_of_three_word_name(ptools):
    d = ptools.spot_people("who is xavier zorn")
    assert [x["words"] for x in d] == ["xavier zorn"] and [r[2] for r in d[0]["recs"]] == ["Xavier Yale Zorn"]
    assert d[0]["strength"] == "strong" and not d[0]["first_only"]


def test_spot_six_word_name(ptools):
    d = ptools.spot_people("where does amara belle cora dane elise faye appear")
    assert [x["words"] for x in d] == ["amara belle cora dane elise faye"] and d[0]["recs"][0][0] == "Lead"


def test_spot_strength_design_words(ptools):
    d = {x["words"]: x["strength"] for x in ptools.spot_people("what does session purpose mean for kilo zephyr")}
    assert d == {"session purpose": "weak", "kilo zephyr": "strong"}   # a lead named only with catalog words is weak
    assert ptools.is_design_word("sessions") and ptools.is_design_word("purpose") and not ptools.is_design_word("zephyr")


def test_spot_cap_four(ptools):
    q = "kilo zephyr, kilo quill, sierra tango, uniform vale and victor wren"
    assert [d["words"] for d in ptools.spot_people(q)] == ["kilo zephyr", "kilo quill", "sierra tango", "uniform vale"]


def test_resolve_person_name_bypasses_vocab_filter(ptools):
    assert ptools.spot_people("who is session") == []          # 'session' is a catalog word: never spotted alone
    r = ptools.resolve_person_name("Session")                  # the names call read it as a name: the lead is found
    assert r is not None and r["strength"] == "strong" and r["from"] == "names" and r["first_only"]
    assert [x[2] for x in r["recs"]] == ["Session Purpose"]
    assert ptools.resolve_person_name("Kilo’s") ["words"] == "kilo"
    close = ptools.resolve_person_name("Kilo Zephir")          # a close spelling of a stored surname
    assert close["asked"] == "kilo zephir" and close["close"] == ["Kilo Zephyr"]
    assert ptools.resolve_person_name("Yankee Nobody") is None and ptools.resolve_person_name("the") is None


def test_profile_fields_role_rule_excludes_dob_contact_legacy_identity(ptools):
    apis = [f.api for f in ptools.catalog.profile_fields(ptools.catalog.objects["Recruiter__c"])]
    assert apis == ["Employment_Status__c", "Department__c", "Manager__c", "Assigned_Sr_Recruiter__c"]
    for hidden in ("Date_of_Birth__c", "Email__c", "Old_Level__c", "Recruiter_User__c", "Recruiter_Contact__c", "Name"):
        assert hidden not in apis
    r = ok(ptools, "about_people", {"names": ["Lima Nobel"]})
    assert "1990" not in json.dumps(r) and "Date_of_Birth" not in json.dumps(r)


def test_profile_standard_fields_user_lead(ptools):
    cat = ptools.catalog
    assert [f.api for f in cat.profile_fields(cat.objects["User"])] == ["IsActive", "UserType", "LastLoginDate"]
    assert [f.api for f in cat.profile_fields(cat.objects["Lead"])] == ["Status", "IsConverted", "ConvertedDate",
                                                                        "LeadSource"]


def test_profile_keeps_documented_person_lookup(ptools):
    assert [f.api for f in ptools.catalog.profile_fields(ptools.catalog.objects["Account"])] == ["Assigned_Recruiter__c"]
    r = ok(ptools, "about_people", {"names": ["Kilo Quill"]})
    assert r["people"][0]["profile"] == {"Assigned_Recruiter__c (Assigned Recruiter)": "Kilo Zephyr"}


def test_identity_converted_lead_joins_account(ptools):
    assert ("Lead", "ConvertedAccountId", "Account", "Id") in [(lk.src, lk.col, lk.dst, lk.dst_col)
                                                              for lk in ptools.catalog.links]
    r = ok(ptools, "about_people", {"names": ["Juliet Converse"]})
    assert _people(r) == [("Juliet Converse", "candidate")]           # one person: the account, then the lead
    recs = [(m["object"], m["how"]) for m in r["matched_records"]]
    assert recs[0] == ("Account", "name") and recs[1][0] == "Lead" and "ConvertedAccountId" in recs[1][1]
    lead = r["matched_records"][1]
    assert lead["profile"]["IsConverted"] is True and lead["profile"]["ConvertedDate"] == "2026-05-06"


def test_about_people_first_name_lists_all_kinds_grouped(ptools):
    r = ok(ptools, "about_people", {"names": ["kilo"]})
    assert _people(r) == [("Kilo Zephyr", "employee"), ("Kilo Quill", "candidate"), ("Kilo Vortex", "lead")]
    assert all(p["match"] == "first name" and p["asked"] == "kilo" for p in r["people"])
    assert r["scope"] == "counts over all records of each object (no period, filter or status)"
    assert "links" not in r                                            # one name: no link finder


def test_about_people_more_people_cap(ptools, monkeypatch):
    import sf_assistant.tools.people as T   # about_people reads its limits from tools.people
    monkeypatch.setattr(T, "PEOPLE_LISTED", 2)
    monkeypatch.setattr(T, "PEOPLE_DETAILED", 1)
    r = ok(ptools, "about_people", {"names": ["kilo"]})
    assert len(r["people"]) == 2
    # full detail for the first; the second has only the sum over its business lookups (it ranks the people)
    assert "business_reference_total" not in r["people"][0] and r["people"][1]["business_reference_total"] == 1
    assert "profile" not in r["people"][1]
    assert r["more_people"] == {"kilo": {"count": 1, "by_kind": {"lead": 1}, "names": ["Kilo Vortex"]}}


def test_about_people_no_business_references_flag(ptools):
    r = ok(ptools, "about_people", {"names": ["lima nobel"]})
    p = r["people"][0]
    assert p["kind"] == "employee" and p["no_business_references"] is True and "business_reference_total" not in p
    user = next(m for m in r["matched_records"] if m["object"] == "User")
    assert user["profile"]["IsActive"] is True and user["profile"]["UserType"] == "Standard"
    assert "Recruiter_User__c" in user["how"]


def test_about_people_system_references(ptools):
    r = ok(ptools, "about_people", {"names": ["User One"]})
    sys_refs = {(e["object"], e["field"]): e["records"] for e in r["system_references"]}
    assert sys_refs.get(("Interview__c", "CreatedById"), 0) >= FACTS["interviews"]
    assert all(e["field"] in ("CreatedById", "LastModifiedById", "OwnerId") for e in r["system_references"])


def test_about_people_not_found_close_from_all_person_objects(ptools):
    r = ok(ptools, "about_people", {"names": ["Kilo Qqqq", "Sierra Tango"]})
    nf = r["not_found"]
    assert [x["asked"] for x in nf] == ["Kilo Qqqq"]
    assert {"Kilo Zephyr", "Kilo Quill", "Kilo Vortex"} <= set(nf[0]["close"])   # Employee, Account and Lead names
    assert _people(r) == [("Sierra Tango", "employee")]
    e = err(ptools, "about_people", {"names": ["Yankee Qqqq"]})
    assert e.startswith("no Employee, User, Account or Lead record is named 'Yankee Qqqq'.")
    assert ev_last(ptools).error_code == "E15a" and ev_last(ptools).not_found[0]["name"] == "Yankee Qqqq"


def test_about_people_closest_stored_name_note(ptools):
    r = ok(ptools, "about_people", {"names": ["Kilo Zephir"]})
    assert _people(r) == [("Kilo Zephyr", "employee")] and r["people"][0]["match"] == "closest stored name"
    assert "no record is named 'Kilo Zephir'; the closest stored name is 'Kilo Zephyr'" in r["notes"]


def test_about_people_e16a(ptools):
    e = err(ptools, "about_people", {"names": ["a1", "b2", "c3", "d4", "e5"]})
    assert e == "about_people takes at most 4 names; call it again for the rest" and ev_last(ptools).error_code == "E16a"
    assert err(ptools, "about_people", {"names": ["x"]}).startswith("about_people needs 'names'")


def test_find_references_alias_maps_name(ptools):
    assert "find_references" not in [t["function"]["name"] for t in ptools.openai_tools()]
    r = ok(ptools, "find_references", {"name": "Kilo Quill"})
    ev = ev_last(ptools)
    assert ev.tool == "about_people" and ev.arguments == {"name": "Kilo Quill"} and r["applied"]["names"] == ["Kilo Quill"]
    r = ok(ptools, "about_people", {"name": "Kilo Quill"})     # the one-name form is read as names too
    assert r["applied"]["names"] == ["Kilo Quill"]


def test_links_same_record_own_record_parent_two_hops_shared_parent(ptools):
    r = ok(ptools, "about_people", {"names": ["Sierra Tango", "Kilo Quill", "Uniform Vale"]})
    refs = {p["label"]: p["ref"] for p in r["people"]}
    st, kq, uv = refs["Sierra Tango"], refs["Kilo Quill"], refs["Uniform Vale"]
    got = {(tuple(x["between"]), x["how"], x["path"]): x for x in r["links"]}
    same = got[((st, kq), "same record", "Candidate_Training__c.Candidate__c + Candidate_Training__c.Assigned_Trainer__c")]
    assert same["records"] == 1 and same["examples"] == ["CT-0020 (Active)"]
    parent = got[((st, uv), "parent", "Session_Attendee__c.Attendee_Recruiter__c -> Session_Attendee__c.Session__c -> "
                                      "Session__c.Host_User__c")]
    # the direction is written by code: the attendee row holds Uniform Vale, its session holds Sierra Tango as host
    assert parent["text"] == (f"{uv} (Uniform Vale) is `Attendee_Recruiter__c` (attending employee) on 1 Session "
                              f"Attendee records whose `Session__c` (Session) has {st} (Sierra Tango) as `Host_User__c` "
                              "(host employee)")
    hops = [x for x in r["links"] if x["how"] == "two hops" and x["between"] == [kq, uv]]
    assert hops and "under Session records whose `Candidate_Training__c`" in hops[0]["text"]
    assert r["pairs_without_links"] == [] and r["partial"] is False and "lookups on" in r["links_checked"]
    r = ok(ptools, "about_people", {"names": ["papa quinn", "romeo stark"]})
    texts = [x["text"] for x in r["links"]]
    assert "P2 (Romeo Stark)'s Employee record has P1 (Papa Quinn) as `Assigned_Sr_Recruiter__c` (reporting senior " \
           "recruiter)" in texts                                                                  # own record
    assert "P1 (Papa Quinn) and P2 (Romeo Stark) have the same `Manager__c` (manager): 'Oscar Prime'" in texts
    assert "P1 (Papa Quinn) and P2 (Romeo Stark) have the same `Department__c` (Department): 'Dept One'" in texts
    assert [x["how"] for x in r["links"]] == ["own record", "shared parent", "shared parent"]


def test_links_company_parent_not_a_link(ptools):
    r = ok(ptools, "about_people", {"names": ["victor wren", "whiskey yarrow"]})
    assert r["links"] == [] and r["pairs_without_links"] == [["P1", "P2"]]   # Company__c holds no person lookup


def test_links_system_only_next_to_business(ptools):
    # Kilo Vortex (lead): OwnerId = User One, CreatedById = Lima Nobel's user: two system lookups, no link
    r = ok(ptools, "about_people", {"names": ["user one", "lima nobel"]})
    assert not [x for x in r["links"] if x["path"].startswith("Lead.")]
    # a system lookup next to a business lookup is a link: User One (Recruiter A's login) created the interviews
    # Recruiter B recruits for
    r = ok(ptools, "about_people", {"names": ["user one", "recruiter b"]})
    assert any(x["how"] == "same record" and "Interview__c.CreatedById" in x["path"] and "Interview__c.Recruiter__c"
               in x["path"] for x in r["links"])
    # two names of one person: one person, said in a note
    r = ok(ptools, "about_people", {"names": ["user one", "recruiter a"]})
    assert _people(r) == [("Recruiter A", "employee")] and r["links"] == [] and r["pairs_without_links"] == []
    assert any("'user one' and 'recruiter a' are the same person: P1 (Recruiter A)" in n for n in r["notes"])


def test_links_exclude_identity_columns(ptools):
    ok(ptools, "about_people", {"names": ["lima nobel", "juliet converse"]})
    sqls = [st.sql for st in ev_last(ptools).statements]
    assert not any(f'x."{c}" = ANY' in sql for sql in sqls for c in ("Recruiter_User__c", "Recruiter_Contact__c",
                                                                       "Portal_User__c", "ConvertedAccountId"))


def test_links_bound_params_and_alias_qualified(ptools):
    from warehouse_fixture import REC_PQ, REC_RS
    ok(ptools, "about_people", {"names": ["papa quinn", "romeo stark"]})
    ev = ev_last(ptools)
    touch = [st for st in ev.statements if " = ANY(?)" in st.sql and " OR " in st.sql or 'FROM main."Department__c"' in st.sql]
    assert touch and all(REC_PQ not in st.sql and REC_RS not in st.sql for st in ev.statements)   # Ids are parameters
    dept = [st.sql for st in ev.statements if 'FROM main."Department__c" AS x' in st.sql]
    assert dept and 'x."Department__c"' in dept[0]     # a column named like its table, alias-qualified: the guard passes
    assert not ev.error


def test_links_truncated_and_hop_cap_listed_in_not_checked(ptools, monkeypatch):
    import sf_assistant.tools.people as T
    monkeypatch.setattr(T, "LINK_ROWS_MAX", 1)
    monkeypatch.setattr(T, "HOP_IDS_MAX", 0)
    r = ok(ptools, "about_people", {"names": ["user one", "recruiter b", "Sierra Tango"]})
    nc = " | ".join(r["not_checked"])
    assert "(row or size limit: only 1 of the records that hold these people were read)" in nc and "two hops via" in nc
    assert r["partial"] is True and "this check stopped early or hit a limit" in r["links_checked"]   # never silent


def test_links_deadline_partial(ptools, monkeypatch):
    import sf_assistant.tools.people as T
    monkeypatch.setattr(T, "PEOPLE_BUDGET_S", 0.0)
    r = ok(ptools, "about_people", {"names": ["Sierra Tango", "Kilo Quill"]})
    assert r["partial"] is True and r["not_checked"] and r["links"] == []


def test_fit_keeps_people_links_shape(ptools, monkeypatch):
    import sf_assistant.tools.people as T
    import sf_assistant.tools.record_base as B   # every reader reads MAX_RESULT_CHARS there at call time
    monkeypatch.setattr(B, "MAX_RESULT_CHARS", 4500)
    monkeypatch.setattr(T, "PEOPLE_LINKS_EXTRA_CHARS", 0)
    r = ok(ptools, "about_people", {"names": ["Sierra Tango", "Kilo Quill", "Uniform Vale"]})
    assert len(json.dumps(r)) <= 4500
    for key in ("people", "links", "links_checked", "pairs_without_links", "scope", "asked"):
        assert key in r, key
    assert r["links"] and any("not shown (result size)" in n for n in r["notes"])


def test_hints_compact_line_many_people(ptools, monkeypatch):
    import sf_assistant.tools.names as T   # the people hints read MANY_PEOPLE from tools.names
    monkeypatch.setattr(T, "MANY_PEOPLE", 2)
    q = "how many sessions did kilo host"
    ptools.begin_question(q)
    h = ptools.object_hints(q)
    assert "- 'kilo': first name of 3 stored people (1 employees, 1 candidates, 1 leads); about_people lists them" in h
    assert "Where these people are referenced" not in h                   # no reference scan


def test_hints_unconfirmed_weak_not_shown(ptools):
    q = "how many sessions did kilo host"
    ptools.begin_question(q, people=[])          # the triage confirmed nobody: no people in the hints
    h = ptools.object_hints(q)
    assert "People named in the question" not in h and "kilo" not in h.lower()
    assert ptools.question_people() == []
    ptools.begin_question(q)                     # without a triage: spotted here, as before
    assert "People named in the question" in ptools.object_hints(q)


def test_hints_missing_name_line(ptools):
    q = "is kilo zephyr linked to yankee doodle"
    ptools.begin_question(q)
    h = ptools.object_hints(q, missing=["Yankee Doodle"])
    assert ("- 'Yankee Doodle': no stored employee, user, candidate or lead has this name (about_people gives the "
            "closest stored names)") in h
    assert ("Two or more people are named: about_people with names [kilo zephyr, Yankee Doodle] gives who each is and "
            "every stored record that links them.") in h
    q = "who is lima nobel"
    ptools.begin_question(q)
    h = ptools.object_hints(q)
    assert ("- 'Lima Nobel' (Employee): no business record references this person; who they are is on their own record "
            "(about_people)") in h and "no queryable lookup holds" not in h


# ------------------------------------------------------------- review fixes of §22 (fake names only)
@pytest.fixture(scope="module")
def ptools_x(tmp_path_factory):
    """The people fixture plus a client interview of the candidate Kilo Quill recruited by Victor Wren (a shared
    candidate with Sierra Tango's training) and a User 'Xavier Zorn' (a 'First Last' next to 'Xavier Yale Zorn')."""
    import copy as _copy

    import warehouse_fixture as F
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records
    tables = _copy.deepcopy(F.PEOPLE_TABLES)
    F._with(tables, "Interview__c", {}, [
        {"Id": F.sid("a0A", 22), "Name": "I-0022", "IsDeleted": "false", "CreatedDate": "2025-01-01T00:00:00.000Z",
         "Interview_Status__c": "Completed", "Recruiter__c": F.REC_VW, "Candidate__c": F.CAND_KQ}])
    F._with(tables, "User", {}, [{"Id": F.sid("005", 40), "Name": "Xavier Zorn", "IsActive": "true",
                                  "UserType": "Standard"}])
    base = tmp_path_factory.mktemp("people_x")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(F.make_warehouse(base, tables=tables)),
                                             today=__import__("datetime").date(2026, 10, 13)),
                         str(F.make_knowledge(base, knowledge=F.PEOPLE_KNOWLEDGE)))
    yield t
    t.close()


def test_name_key_reads_separators_as_spaces(ptools):
    from sf_assistant.tools.record_base import name_key
    assert name_key("Kilo,Sierra") == "kilo sierra" and name_key("[Kilo]") == "kilo" and name_key("a&b") == "a b"
    assert name_key("J.K. Rowling") == "jk rowling" and name_key("Kilo’s") == name_key("Kilo's")
    assert [d["words"] for d in ptools.spot_people("sierra tango,kilo and [uniform vale]: linked?")] == \
        ["sierra tango", "kilo", "uniform vale"]


def test_resolve_person_name_general_rules(ptools):
    """No word lists: the same name written another way is the closest stored name; a name of which only a word is
    a stored first (or last) name is partial; a last name alone is a last-name match."""
    for asked in ("Zephyr Kilo", "Kilo Z.", "K. Zephyr", "Kilo Q Zephyr", "Kilo Zephry"):
        r = ptools.resolve_person_name(asked)
        assert r is not None and r["close"] == ["Kilo Zephyr"] and r["match"] == "closest stored name", asked
        assert r["asked"] == " ".join(asked.lower().replace(".", "").split()) and not r.get("partial"), asked
    r = ptools.resolve_person_name("Mr. Kilo")
    assert r["partial"] == "kilo" and r["asked"] == "mr kilo" and r["first_only"] and r["match"] == "partial first name"
    assert {ptools.kind_of(x[0], x[3]) for x in r["recs"]} == {"employee", "candidate", "lead"}
    r = ptools.resolve_person_name("Zephyr")
    assert r["match"] == "last name" and r["first_only"] and [x[2] for x in r["recs"]] == ["Kilo Zephyr"]
    assert ptools.resolve_person_name("Client Co") is None and ptools.company_name("Client Co")
    assert not ptools.company_name("Kilo Zephyr") and ptools.name_index_complete()
    assert ptools.staff_close_name("Sierre") and not ptools.staff_close_name("Juliett")   # a candidate's name
    assert not ptools.staff_close_name("Einstein") and not ptools.staff_close_name("Sierra Tango")


def test_spot_leaves_out_client_companies(ptools):
    assert ptools.spot_people("which interview fields describe the client co client?") == []


def test_first_last_never_hides_a_longer_name(ptools_x):
    spot = ptools_x.spot_people("who is xavier zorn")
    assert {r[2] for r in spot[0]["recs"]} == {"Xavier Zorn", "Xavier Yale Zorn"}
    r = ok(ptools_x, "about_people", {"names": ["xavier zorn"]})
    match = {p["label"]: p["match"] for p in r["people"]}
    assert match == {"Xavier Zorn": "full name", "Xavier Yale Zorn": "first and last name"}


def test_about_people_general_name_rules(ptools):
    r = ok(ptools, "about_people", {"names": ["zephyr kilo"]})
    assert r["people"][0]["match"] == "closest stored name" and r["people"][0]["label"] == "Kilo Zephyr"
    assert "no record is named 'zephyr kilo'; the closest stored name is 'Kilo Zephyr'" in r["notes"]
    out = ptools.call("about_people", {"names": ["mr kilo"]})
    assert not out["ok"] and "Stored people with the first name 'kilo': Kilo Zephyr" in out["error"]
    assert "Kilo Zephyr" in ptools.evidence[-1].not_found[0]["close"]
    r = ok(ptools, "about_people", {"names": ["zephyr"]})
    assert r["people"][0]["match"] == "last name" and r["people"][0]["label"] == "Kilo Zephyr"


def test_links_through_a_third_person(ptools_x):
    """Review §22: a row whose person lookup holds a third person whose own row holds B (Sierra Tango trains Kilo
    Quill, whose assigned recruiter is Kilo Zephyr), and two rows of A and B with the same candidate (Kilo Quill's
    training and Victor Wren's interview). Two people who only share a colleague are not linked."""
    r = ok(ptools_x, "about_people", {"names": ["Sierra Tango", "Kilo Zephyr"]})
    paths = {(x["how"], x["path"]) for x in r["links"]}
    assert ("parent", "Candidate_Training__c.Assigned_Trainer__c -> Candidate_Training__c.Candidate__c -> "
                      "Account.Assigned_Recruiter__c") in paths
    x = next(x for x in r["links"] if x["how"] == "parent")
    assert x["examples"] == ["CT-0020 (Active) -> Kilo Quill"] and "`Candidate__c` (Account) has" in x["text"]
    r = ok(ptools_x, "about_people", {"names": ["Sierra Tango", "Victor Wren"]})
    shared = [x for x in r["links"] if x["how"] == "shared parent" and x["path"].endswith("-> Account")]
    assert shared and shared[0]["examples"] == ["Kilo Quill"] and r["pairs_without_links"] == []
    assert "through a third person" in r["links_checked"]


def test_about_people_never_cuts_silently(ptools, monkeypatch):
    import sf_assistant.tools.people as P
    import sf_assistant.tools.record_tools as T
    from sf_assistant.engine.catalog import RecordError
    monkeypatch.setattr(P, "PERSON_REFS_ONE", 1)
    r = ok(ptools, "about_people", {"names": ["sierra tango"]})
    st = r["people"][0]
    assert st["references_shown"] == "1 of 2 lookups with records"
    assert any(n.startswith("lookups with records shown: ") and f"{st['ref']} 1 of 2" in n for n in r["notes"])
    monkeypatch.undo()
    r = ok(ptools, "about_people", {"names": ["sierra tango"]})   # one person: every lookup with records
    assert len(r["references"]) == 2 and "references_shown" not in r["people"][0]

    def stop(*a, **k):
        raise RecordError("stopped", "E10")
    monkeypatch.setattr(T.RecordTools, "_scan", stop)
    r = ok(ptools, "about_people", {"names": ["lima nobel"]})
    assert r["references_partial"] is True and "no_business_references" not in r["people"][0]


def test_fit_keeps_the_people_cut_keys():
    from sf_assistant.tools.record_tools import _PROTECTED_KEYS
    assert {"partial", "not_checked", "more_people", "references_partial"} <= _PROTECTED_KEYS


def test_hints_name_that_is_only_partly_stored(ptools):
    q = "who is mr. kilo?"
    ptools.begin_question(q, people=[ptools.resolve_person_name("Mr. Kilo")])
    h = ptools.object_hints(q)
    assert "- 'mr kilo' is not a stored name; 'kilo' is a stored first name of the people below" in h
    assert "- stored people with the first name 'kilo': 'Kilo Zephyr', 'Kilo Quill', 'Kilo Vortex'. None of them is " \
           "named 'mr kilo': never give a count for 'mr kilo'." in h and "Answer for each of them" not in h


def test_both_kinds_line_prefers_the_candidate_main_object_lookup(ptools):
    pe = {"main": ("Recruiter__c", "a06000000000099AAA", "Delta Same", "Active Employee")}
    pc = {"main": ("Account", "001000000000099AAA", "Delta Same", "Person Account")}
    kf = {"take": {"employee": [(pe, "Internal_Interview__c.Interviewer__c", 5, "Recruiter__c")],
                   "candidate": [(pc, "Internal_Interview__c.Lead__c", 2, "Lead"),
                                 (pc, "Internal_Interview__c.Candidate__c", 7, "Account")]}}
    line = ptools._both_kinds_lines(kf)[0]
    assert "(Internal_Interview__c.Candidate__c)" in line and "Lead__c" not in line
    kf["take"]["candidate"] = [(pc, "Internal_Interview__c.Lead__c", 2, "Lead")]
    assert "(Internal_Interview__c.Lead__c, a Lead lookup)" in ptools._both_kinds_lines(kf)[0]


# ------------------------------------------------------------- people set round 2 (RECORDS_SPEC §22.16)

def test_count_records_person_breakdown(ptools, tools):
    r = ok(ptools, "count_records", {"object": "Session__c", "filters": [F("Host_User__c", "Sierra Tango")]})
    assert r["count"] == 1 and r["breakdown"] == {"Purpose__c": {"Training": 1}}
    assert any(n.startswith("breakdown: the same records per value") for n in r["notes"])
    st = ev_last(ptools).statements
    grouped = [s for s in st if "GROUP BY 1" in s.sql]
    assert grouped and all(s.params and "Sierra" not in s.sql for s in grouped)          # the person is a bound value
    assert "breakdown" not in ok(ptools, "count_records", {"object": "Session__c"})               # no person filter
    assert "breakdown" not in ok(ptools, "count_records", {"object": "Session__c", "count_distinct": "Host_User__c",
                                                           "filters": [F("Host_User__c", "Sierra Tango")]})


def test_about_people_links_get_extra_room(ptools, monkeypatch):
    import sf_assistant.tools.people as T
    import sf_assistant.tools.record_base as B
    monkeypatch.setattr(B, "MAX_RESULT_CHARS", 3000)
    monkeypatch.setattr(T, "PEOPLE_LINKS_EXTRA_CHARS", 2000)
    r = ok(ptools, "about_people", {"names": ["kilo", "sierra tango"]})
    assert 3000 < len(json.dumps(r)) <= 5000 and r["links"]                      # two names: the larger limit
    r = ok(ptools, "about_people", {"names": ["kilo"]})
    assert len(json.dumps(r)) <= 3000                                             # one name: the usual limit


def test_primary_person_and_budget_keep(ptools):
    def person(key, name, side=0, match="full name"):
        return {"key": key, "side": side, "match": match, "main": ("Recruiter__c", key, name, None)}
    det = [person("a", "Kilo Zephyr"), person("b", "Kilo Zephyr Quill", match="first and last name")]
    assert ptools._primary_people([{"asked": "kilo zephyr"}], det) == {"a"}
    assert ptools._primary_people([{"asked": "kilo"}], det) == set()                  # a first name: nobody
    assert ptools._primary_people([{"asked": "kilo zephyr"}], det[:1]) == set()       # nobody shares the name
    from sf_assistant.tools.record_base import _Call
    call = _Call(ptools, "about_people")
    call.max_chars = 1000
    refs = [{"person": "P1", "record": "R1", "object": "X", "field": f"F{i}", "role": "r" * 40, "records": i}
            for i in range(5)] + [{"person": "P2", "record": "R2", "object": "X", "field": f"G{i}", "role": "r" * 40,
                                   "records": i} for i in range(5)]
    out = {"people": [{"ref": "P1"}, {"ref": "P2"}], "references": refs, "system_references": []}
    ptools._people_budget(call, out, 2, reserve=0, keep={"P1"})
    per = [e["person"] for e in out["references"]]
    assert per.count("P2") == 1 and per.count("P1") > 1                                # the other person is cut first


def test_path_ends_and_two_hop_dedupe():
    from sf_assistant.tools.people import _path_ends
    assert _path_ends("A.f -> A.p -> B.g") == ("A.f", "B.g")
    assert _path_ends("A.f -> A.p -> B.q -> C.g") == ("A.f", "C.g")


def test_about_people_user_without_employee_note(ptools):
    r = ok(ptools, "about_people", {"names": ["foxtrot person"]})
    users = [p for p in r["people"] if p["kind"] == "user"]
    assert users and any(n.startswith(f"{users[0]['ref']} is a Salesforce user with no linked Employee record")
                         for n in r["notes"])
    assert not any("is a Salesforce user with no linked" in n and n.startswith(p["ref"] + " ")
                   for p in r["people"] if p["kind"] == "employee" for n in r["notes"])


def test_hints_list_system_lookups_that_fit_the_verb(ptools):
    q = "how many leads does user one own"
    ptools.begin_question(q)
    h = ptools.object_hints(q)
    where = next(line for line in h.splitlines() if line.startswith("- 'Recruiter A' (Employee): "))
    assert "Lead.OwnerId (record owner: user or queue that owns the record; system) <- fits 'own'" in where
    assert not any(line.startswith("- Lead -> User (system)") and "OwnerId" in line for line in h.splitlines())
    q = "how many leads does user one have"                               # no system verb: no system lookups
    ptools.begin_question(q)
    assert "; system)" not in ptools.object_hints(q)


def test_question_objects_narrowed_by_a_word_of_one_object(tools):
    """A noun with no qualifier before it is still narrowed when another question word matches the knowledge of only
    one of the objects ("in mocks"); a word both objects' knowledge holds narrows nothing."""
    for q in ("which candidates did recruiter a interview in mocks", "how many interviews did recruiter a have in mocks"):
        tools.begin_question(q)
        tools.object_hints(q)
        assert tools.question_objects() == [], q
    q = "how many interviews does recruiter a have"
    tools.begin_question(q)
    tools.object_hints(q)
    assert [x["word"] for x in tools.question_objects()] == ["interviews"]
    assert tools._narrowed(["interviews", "mocks"], "interviews", ["Interview__c", "Internal_Interview__c"])
    assert not tools._narrowed(["interviews", "practice"], "interviews", ["Interview__c", "Internal_Interview__c"])


def test_question_people_one_name_per_linked_person(ptools, monkeypatch):
    """A lead converted to a candidate's account (or an employee's login) is one person: the several-people check gets
    one name for it, whatever the two records are called."""
    spot = [{"words": "zed", "first_only": True, "asked": None, "close": [],
             "recs": [("Recruiter__c", "E1", "Zed Two", "Active Employee"), ("Account", "A1", "Zed One", "Person Account"),
                      ("Lead", "L1", "Zed Uno", None)]}]
    lead = ("Lead", "L1", "Zed Uno", None)
    monkeypatch.setattr(ptools, "_question_spots", lambda q: [dict(d) for d in spot])
    monkeypatch.setattr(ptools, "_identities", lambda call, recs, deadline=None: {"A1": (lead, []), "L1": (lead, []),
                                                                                  "E1": (None, [])})
    got = ptools.question_people()
    assert len(got) == 1 and sorted(got[0]["names"]) == ["Zed One", "Zed Two"] and got[0]["first_only"] is True


# ------------------------------------------------------------- quick fixes after round 2 (RECORDS_SPEC §22.17)

def test_status_field_typed_text_in_the_catalog(ptools):
    """Lead.Status is a picklist in Salesforce but typed text in the catalog and the warehouse: with no status picklist,
    a mostly filled text status with a short value list is the status field, so a person count gets its status split."""
    lead = ptools.catalog.objects["Lead"]
    assert lead.fields["Status"].kind == "text" and ptools._status_field(lead).api == "Status"
    assert ptools.status_values(["Lead"]) == ["Open", "Converted"]
    r = ok(ptools, "count_records", {"object": "Lead", "filters": [F("OwnerId", "User One")]})
    assert r["count"] == 4 and r["breakdown"]["Status"] == {"Open": 3, "Converted": 1}
    # a status picklist still wins over a text status (Interview__c keeps its picklist)
    assert ptools._status_field(ptools.catalog.objects["Interview__c"]).kind == "picklist"


def test_status_field_text_rules(ptools):
    from sf_assistant.engine.catalog import FieldSpec, ObjectSpec, ValueEntry
    from sf_assistant.tools.record_tools import RecordTools
    from sf_assistant.tools.records import STATUS_TEXT_MAX_VALUES

    def spec(api, *fields, rows=10):
        return ObjectSpec(api=api, label=api, plural_label=api, documented=True, has_table=True, rows=rows,
                          fields={f.api: f for f in fields})

    def text(api, filled=10):
        return FieldSpec(api=api, label=api, kb_type=None, db_type="VARCHAR", kind="text", meaning="", has_data=True,
                         filled=filled)

    own = RecordTools._own_status_name
    assert own(spec("Lead"), "Status") and own(spec("Lead"), "Lead_Status__c") and own(spec("Thing__c"), "Status__c")
    assert own(spec("Job_Submission__c"), "Submission_Status__c")
    assert not own(spec("Lead"), "Visa_Status__c") and not own(spec("Job_Requirement__c"), "Required_Visa_Status__c")
    values = ptools.catalog.values
    s = spec("Zeta_Thing__c", text("Status__c"), text("Visa_Status__c"))
    try:
        values[("Zeta_Thing__c", "Status__c")] = ValueEntry("Zeta_Thing__c", "Status__c", [("Open", 6), ("Done", 4)], 0,
                                                            False, None)
        assert ptools._status_field(s).api == "Status__c"
        s.fields["Status__c"].filled = 4                     # filled on fewer than half of the records
        assert ptools._status_field(s) is None
        s.fields["Status__c"].filled = 10
        values[("Zeta_Thing__c", "Status__c")] = ValueEntry(
            "Zeta_Thing__c", "Status__c", [(f"v{i}", 1) for i in range(STATUS_TEXT_MAX_VALUES + 1)], 0, False, None)
        assert ptools._status_field(s) is None               # a free-text field, no status
        del values[("Zeta_Thing__c", "Status__c")]
        assert ptools._status_field(s) is None               # no value list at all
        s.fields["Status__c"].picklist = [("Open", "Open", ""), ("Done", "Done", "")]   # knowledge picklist values
        assert ptools._status_field(s).api == "Status__c"
    finally:
        values.pop(("Zeta_Thing__c", "Status__c"), None)


def test_members_with_fewer_than_n_related_records_include_those_with_none(tools):
    """Round 2 of the pre-release fixes (D1 shape: "how many of these recruiters have fewer than 3 interviews in 2024
    and 2025"): one count of the members with ``max_count``, those with no interview in the period included. Dept One
    holds Recruiter A (3 client interviews dated in 2026, none before) and Recruiter B #2 (1 in 2026)."""
    def members(period, most, least=None):
        rel = {"child_object": "Interview__c", "via_field": "Recruiter__c", "exists": True, "max_count": most,
               "date_field": "Date_of_Interview__c", "period": period}
        if least is not None:
            rel["min_count"] = least
        r = ok(tools, "count_records", {"object": "Recruiter__c", "related": [rel],
                                        "filters": [{"field": "Department__c", "op": "eq", "value": "Dept One"}]})
        return r["count"], r
    assert members("2024-01-01 to 2025-12-31", 2)[0] == 2        # both members have none in those years: both count
    assert members("2026", 2)[0] == 1                            # Recruiter A has 3: only Recruiter B #2
    assert members("2026", 3)[0] == 2
    assert members("2026", 0)[0] == 0                            # at most 0 is "none", as exists=false says
    count, r = members("2026", 3, least=2)
    assert count == 1 and any("has 2 to 3 Interview__c" in t for t in r["applied"]["related"])
    _c, r = members("2025", 2)
    assert any("at most 2 Interview__c" in t and "none included" in t for t in r["applied"]["related"])
    # the same through a grouping of the children leaves out the members with none: the shape the agent got wrong
    grouped = ok(tools, "group_records", {"object": "Interview__c", "group_by": "Recruiter__c",
                                          "filters": [{"field": "Recruiter__c.Department__c", "op": "eq",
                                                       "value": "Dept One"}],
                                          "date_field": "Date_of_Interview__c", "period": "2024-01-01 to 2025-12-31"})
    assert grouped["groups"] == []
    e = err(tools, "count_records", {"object": "Recruiter__c", "related": [
        {"child_object": "Interview__c", "via_field": "Recruiter__c", "exists": True, "max_count": -1}]})
    assert "max_count" in e and "at least 0" in e
