"""The record rows code writes (latency plan step 3, the owner's go of 2026-10-29: the record agent writes 1-3 sentences
and a table line, code fills in the rows from the query result): the ``SFA_TABLES`` switch (``config.tables_settings``),
``agent.tables`` (the table lines, the rows of each result shape from what the model saw, their placement, the
typed-copy rule, the merge's put-back), ``RecordSession.ask`` in model mode (the default: as before) and in code mode
(the note, the expanded draft checks and the marker message, the reply expanded after the cleaners, the audits and the
contact rule), ``pipeline.answer`` (the merges, the last-resort guard, the JSONL keys), the trace line and the page's
render of the rows.

Offline and model-free: the fake warehouse (``tests/warehouse_fixture.py``, synthetic names), stub record tools and
scripted fakes of the model; every name, value and count is made up. Every test whose subject depends on the switch
sets ``SFA_TABLES`` itself, so the suites pass with it unset, ``model`` or ``code`` in the shell; the unset case of a
test of the default mode is skipped once the default is no longer model (the go-live flips one constant).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import sf_assistant.pipeline as pipeline  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant.agent import merge  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent import tables as T  # noqa: E402
from sf_assistant.agent import timeline  # noqa: E402
from sf_assistant.agent.checks import (drop_ref_codes, drop_tool_talk, drop_unsupported,  # noqa: E402
                                       drop_working_sentences, facts_json, numbers_in, strip_reasoning)
from sf_assistant.agent.knowledge_agent import AgentResult  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.agent.merge import drop_record_side_caveats, fix_any_role_scope  # noqa: E402
from sf_assistant.agent.nudges import drop_overlap_clauses, with_no_data_line, with_source_line  # noqa: E402
from sf_assistant.agent.record_session import RecordSession  # noqa: E402
from sf_assistant.agent.render import record_trace_lines  # noqa: E402
from sf_assistant.agent.results import RecordAnswer  # noqa: E402
from sf_assistant.config import load_config  # noqa: E402
from sf_assistant.engine.contact import drop_contact_details  # noqa: E402
from sf_assistant.engine.types import QueryEvidence, RecordsStatus, Statement  # noqa: E402
from sf_assistant.knowledge.search import KnowledgeIndex  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge  # noqa: E402
from sf_assistant.tools.knowledge_tools import Tool, ToolRegistry  # noqa: E402
from warehouse_fixture import open_fixture  # noqa: E402

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
F = lambda f, v, op="eq": {"field": f, "op": op, "value": v}  # noqa: E731


def _cfg(steps: int = 6):
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=steps)


@pytest.fixture(autouse=True)
def _closed_timeline():
    timeline.end()   # no timeline left open by another test
    yield
    timeline.end()


def _default_mode(monkeypatch, value):
    """``SFA_TABLES`` for a test of the model mode: ``value`` ("model"), or unset (None) while the default is model."""
    if value is None:
        if C.TABLES_DEFAULT != "model":
            pytest.skip("the default of SFA_TABLES is code (after the go-live): the unset case is code mode")
        monkeypatch.delenv("SFA_TABLES", raising=False)
    else:
        monkeypatch.setenv("SFA_TABLES", value)


# ------------------------------------------------------------------------------------------------ the switch
def test_the_switch_is_read_at_each_call_and_never_stops_a_run(monkeypatch):
    monkeypatch.delenv("SFA_TABLES", raising=False)
    assert C.tables_settings() == C.TablesConfig() == C.TablesConfig(by=C.TABLES_DEFAULT, problem="")
    for raw in ("", " ", "\t"):
        monkeypatch.setenv("SFA_TABLES", raw)
        assert C.tables_settings() == C.TablesConfig(), repr(raw)
    for raw, by in (("model", "model"), ("MODEL", "model"), ("code", "code"), (" Code ", "code")):
        monkeypatch.setenv("SFA_TABLES", raw)
        assert C.tables_settings() == C.TablesConfig(by=by), raw
    for raw in ("on", "llm", "code,model"):
        monkeypatch.setenv("SFA_TABLES", raw)
        cfg = C.tables_settings()
        assert cfg == C.TablesConfig(problem="SFA_TABLES must be model or code") and cfg.by == C.TABLES_DEFAULT
        assert raw not in cfg.problem   # the value is never repeated
    monkeypatch.setenv("SFA_TABLES", "code")   # read at each call, never at import
    assert C.tables_settings().by == "code"
    monkeypatch.setenv("SFA_TABLES", "model")
    assert C.tables_settings().by == "model"
    assert C.TABLES_ENV_VAR == "SFA_TABLES" and C.TABLES_MODES == ("model", "code")
    assert C.TABLES_DEFAULT in C.TABLES_MODES and C.TABLES_ENV_VAR not in C.ENV_ALIASES.values()
    doc = {n: d for n, d, _ in C.ENV_DOC}
    assert doc["SFA_TABLES"] == C.TABLES_DEFAULT == C.TablesConfig().by   # one constant: the go-live is one line


# ------------------------------------------------------------------------------------------------ the prompt files
def test_the_four_texts_of_the_rows_are_new_files_bound_in_agent_tables():
    from sf_assistant import prompts as P

    names = {"records_tables_note": "RECORDS_TABLES_NOTE", "combine_tables_note": "COMBINE_TABLES_NOTE",
             "records_tables_marker_message": "RECORDS_TABLES_MARKER_MESSAGE",
             "records_table_words": "RECORDS_TABLE_WORDS"}
    for pid, name in names.items():
        p = P.load(pid)
        version = 2 if pid == "records_tables_note" else 1   # the note's rewrite (step-3 fix T2)
        assert (p.version, p.meta["name"], p.meta["module"]) == (version, name, "sf_assistant.agent.tables"), pid
        assert p.meta["source"].startswith("new in the code-built tables of 2026-10-29"), pid
        assert getattr(T, name) == p.text and type(getattr(T, name)) is str, pid
        assert not P.TOKEN_RE.search("{{table q3}}")   # the example table line is no placeholder
    assert P.load("records_tables_marker_message").placeholders == ("<<QUERY_IDS>>",)
    assert set(P.load("records_table_words").placeholders) == {"<<SHOWN>>", "<<TOTAL>>", "<<UNIT>>", "<<LABEL>>"}
    # appended texts: the note starts a paragraph after the record prompt's examples, the merge note continues the
    # merge prompt's "- " rules
    assert T.RECORDS_TABLES_NOTE.startswith("\n\nRows by code: ") and "{{table q3}}" in T.RECORDS_TABLES_NOTE
    # review of 2026-10-30 (quality-2): one bullet (the counted field and lookup in backticks) stays, after the
    # table lines and before the Source line, so a lookup answer still names its field
    note = T.RECORDS_TABLES_NOTE
    assert note.index("{{table q3}}") < note.index("one bullet") < note.index("the Source line")
    assert "field and lookup API names in backticks" in note
    # step-3 fix T2 (note version 2): the rules of the 10-30 faults, and none of version 1's trigger phrases
    for phrase in ("Never type those rows in any form, even in a redraft", "Always start with 1-3 sentences",
                   "State the numbers the question asks for", "add nothing the rows show", "(keep its bullets)",
                   "find_references or get_field_history", "No total for a top-N ranking unless asked",
                   "total_across_groups includes the (blank) group", "describe_fields or find_objects count",
                   "no line per row, month name, range, split or pivot", "never a table line or list",
                   "with 2 or more rows to show", "as the result gives them", ", any caveat,",
                   "list_records, group_records, trend_records or compare_records result or count_records breakdown",
                   "Code copies its rows and any Total line there exactly: that is your copy of them",
                   "Example answer:\nThere are 61 gadgets.\n{{table q3}}\n- Counted: `Gadget__c` by `Color__c`, "
                   "`Status__c` = Open, `CreatedDate` from 2031-05-01 to 2031-05-31."):
        assert phrase in note, phrase
    for gone in ("the key numbers", "the total", "your usual short bullets"):
        assert gone not in note, gone
    assert T.MARKER_RE.search(note.split("Example answer:")[1])
    assert T.COMBINE_TABLES_NOTE.startswith("\n- A line such as {{table q3}} ")
    assert T._WORDS == {"total": "Total", "no_value": "(no value)", "shown": "Showing <<SHOWN>> of <<TOTAL>> <<UNIT>>.",
                        "not_all": "Not all <<UNIT>> are shown.", "records": "records", "groups": "groups",
                        "periods": "periods", "partial": "(partial)", "by": "By <<LABEL>>:", "compared": "Compared:"}
    assert T.RECORDS_TABLES_NOTE not in rprompts.render_records_system(STATUS)
    assert T.COMBINE_TABLES_NOTE.strip() not in rprompts.COMBINE_SYSTEM


# ------------------------------------------------------------------------------------------------ the fixture results
@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        mp.setenv("SFA_RESOLVER", "off")   # the calls name objects, fields and values exactly
        t = open_fixture(tmp_path_factory.mktemp("tables"))
    yield t
    t.close()


def _run(tools, *calls, question: str = "a made-up question about interviews") -> list[QueryEvidence]:
    tools.begin_question(question)
    for name, args in calls:
        out = tools.call(name, copy.deepcopy(args))
        assert out["ok"] or name == "group_records" and "Nope__c" in json.dumps(args), out["error"]
    return list(tools.evidence)


GROUP = ("group_records", {"object": "Interview__c", "group_by": "Recruiter__c"})
SPLIT = ("group_records", {"object": "Internal_Interview__c", "group_by": "Interviewer__c", "split_by": "Status__c"})
TREND = ("trend_records", {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
                           "period": "last 6 months", "split_by": "Employment_Type__c"})
AVG = ("trend_records", {"object": "Internal_Interview__c", "grain": "month", "date_field": "CreatedDate",
                         "period": "last 3 months", "measure": "avg", "measure_field": "Human_Total_Score__c"})
COMPARE = ("compare_records", {"object": "Internal_Interview__c", "segments": [
    {"label": "Cancelled", "filters": [F("Status__c", "Cancelled")]},
    {"label": "Completed", "filters": [F("Status__c", "Completed")]}]})
COUNT = ("count_records", {"object": "Internal_Interview__c", "filters": [F("Interviewer__c", "Delta Person")]})
LIST = ("list_records", {"object": "Interview__c", "fields": ["Name", "Recruiter__c", "Interview_Status__c",
                                                             "Date_of_Interview__c", "Client_Bill_Rate__c",
                                                             "Candidate__c.Name", "Recruiter__c.Name"], "limit": 4})
GROUP_LINES = ["- Recruiter A: 4", "- Recruiter B (#1): 2", "- (blank): 1", "- (unresolved id): 1",
               "- Recruiter B (#2): 1", "- Recruiter C: 1", "- Total: 10"]
SPLIT_LINES = ["- Delta Person: 3 (Completed: 2, Scheduled: 1)", "- (blank): 1 ((blank): 1)",
               "- Echo One: 1 (Unscheduled: 1)", "- Recruiter B (#2): 1 (Cancelled: 1)",
               "- Total: 6 (Completed: 2, (blank): 1, Cancelled: 1, Scheduled: 1, Unscheduled: 1)"]
TREND_LINES = ["- 2026-05: 1 (Full Time: 1)", "- 2026-06: 0", "- 2026-07: 1 (Full Time: 1)",
               "- 2026-08: 1 (Contract: 1)",
               "- 2026-09: 3 (Full Time: 2, Contract: 1)", "- 2026-10: 3 (Full Time: 2, (blank): 1) (partial)",
               "- Total: 9 (Full Time: 6, Contract: 2, (blank): 1)"]   # today is 2026-10-13: October is still running
LIST_LINES = ["| Interview Number | Recruiter | Interview Status | Date of Interview | Client Bill Rate | Name | "
              "Recruiter Name |", "|---|---|---|---|---|---|---|",
              "| I-0007 |  | Cancelled | 2026-10-13 |  |  |  |",
              "| I-0004 | Recruiter B | Scheduled | 2026-10-20 | 90.0 | Candidate 2 | Recruiter B |",
              "| I-0003 | Recruiter B | Completed | 2026-10-02 |  | Candidate 2 | Recruiter B |",
              "| I-0010 | Recruiter A | Completed | 2026-09-28 |  | Candidate 1 | Recruiter A |",
              "", "Showing 4 of 10 records."]


def _block(e, labels=None, said=()):
    out = T.render(e, labels, said)
    assert out is not None, e.tool
    return out[0]


def test_group_lines_keep_the_result_order_labels_and_total(tools):
    (e,) = _run(tools, GROUP)
    text, rows, form = T.render(e)
    assert text.split("\n") == GROUP_LINES and (rows, form) == (6, "lines")


def test_a_split_goes_inline_in_its_stored_order_and_a_missing_split_value_is_left_out(tools):
    (e,) = _run(tools, SPLIT)
    text = _block(e)
    assert text.split("\n") == SPLIT_LINES   # "Delta  Person" is stored with two spaces: one line, one space
    assert ": 0" not in text   # a split value a group lacks is left out, never written as 0


def test_trend_lines_keep_a_zero_bucket_and_an_average_shows_no_value_and_no_total(tools):
    (trend, avg) = _run(tools, TREND, AVG)
    assert trend.model_result["last_bucket_partial"] is True and trend.model_result["first_bucket_partial"] is False
    assert _block(trend).split("\n") == TREND_LINES
    text = _block(avg)
    assert text.split("\n") == ["- 2026-08: (no value)", "- 2026-09: 20.5", "- 2026-10: 27.5 (partial)"]
    assert "none" not in text.lower() and "Total" not in text


def test_a_partial_period_says_so_on_its_own_line():
    """Review of 2026-10-30 (quality-4): the result's first_bucket_partial / last_bucket_partial mark that bucket's
    line with a fixed word (no number: the audits and graders read the same numbers); no mark without the flag."""
    buckets = [{"bucket": "2026-08", "value": 12}, {"bucket": "2026-09", "value": 30},
               {"bucket": "2026-10", "value": 4}]
    res = {"grain": "month", "buckets": buckets, "total": 46}
    plain = numbers_in(_block(_evidence("q1", "trend_records", res)))
    for first, last, want in ((False, False, ["- 2026-08: 12", "- 2026-09: 30", "- 2026-10: 4"]),
                              (True, True, ["- 2026-08: 12 (partial)", "- 2026-09: 30", "- 2026-10: 4 (partial)"]),
                              (False, True, ["- 2026-08: 12", "- 2026-09: 30", "- 2026-10: 4 (partial)"])):
        e = _evidence("q1", "trend_records", {**res, "first_bucket_partial": first, "last_bucket_partial": last})
        block = _block(e)
        assert block.split("\n") == want + ["- Total: 46"], (first, last)
        assert numbers_in(block) == plain   # the mark adds no number


def test_compare_lines_keep_the_models_segment_order_and_have_no_total(tools):
    (e,) = _run(tools, COMPARE)
    assert _block(e).split("\n") == ["- Cancelled: 1", "- Completed: 2"]


def test_a_count_breakdown_gives_lines_and_several_fields_get_label_lines(tools):
    (e,) = _run(tools, COUNT)
    assert _block(e).split("\n") == ["- Completed: 2", "- Scheduled: 1"]
    two = dataclasses.replace(e, model_result={**e.model_result, "breakdown": {
        "Status__c": {"Completed": 2, "Scheduled": 1}, "Interview_Type__c": {"Mock": 2, "Intake": 1}}})
    labels = T.column_labels(tools.catalog)
    assert _block(two, labels).split("\n") == ["Status:", "- Completed: 2", "- Scheduled: 1", "",
                                               "Interview Type:", "- Mock: 2", "- Intake: 1"]
    assert _block(two).split("\n")[4] == "Interview Type:"   # without a catalog: the humanized name


def test_a_list_is_a_table_with_catalog_headers_and_a_showing_line(tools):
    (e,) = _run(tools, LIST)
    text, rows, form = T.render(e, T.column_labels(tools.catalog))
    assert text.split("\n") == LIST_LINES and (rows, form) == (4, "table")
    # the lead gives both numbers: no "Showing" line
    assert T.render(e, T.column_labels(tools.catalog), said={"4", "10"})[0].split("\n") == LIST_LINES[:-2]


def _f(api, label, kind="text", targets=()):
    return SimpleNamespace(api=api, label=label, kind=kind, ref_targets=list(targets))


#: A made-up catalog: Job__c (Name labelled "Name", an owner lookup labelled "Owner ID", notes, CreatedDate with no
#: label of its own) and Staff__c.
CATALOG = SimpleNamespace(
    objects={"Job__c": SimpleNamespace(fields={f.api: f for f in (
        _f("Name", "Name"), _f("Owner__c", "Owner ID", "reference", ["Staff__c"]), _f("Notes__c", "Notes"),
        _f("CreatedDate", "CreatedDate"))}),
        "Staff__c": SimpleNamespace(fields={"Name": _f("Name", "Name")})},
    target_of=lambda f: f.ref_targets[0] if f.kind == "reference" and len(f.ref_targets) == 1 else None)


def _evidence(qid, tool, result, obj="Job__c", ok=True, kind="data", **kw) -> QueryEvidence:
    res = {"query_id": qid, "object": obj, **result} if ok else None
    return QueryEvidence(query_id=qid, tool=tool, kind=kind, object=obj, arguments={}, ok=ok,
                         error=None if ok else "E2: a made-up failure", applied={}, notes=[], statements=[],
                         tables=[obj], synced_at={obj: "2026-10-13 14:30 UTC"}, summary="", model_result=res,
                         elapsed_ms=1.0, error_code=None if ok else "E2", **kw)


JOBS = {"columns": ["Name", "Owner__c (name)", "Notes__c", "Owner__c.Name", "CreatedDate"], "rows": [
    {"Name": "J-1", "Owner__c (name)": "Zed Quill", "Notes__c": "a|b\nnext line", "Owner__c.Name": "Zed Quill",
     "CreatedDate": "2026-10-01 09:00 UTC"},
    {"Name": "J-2", "Owner__c (name)": None, "Notes__c": None, "Owner__c.Name": None,
     "CreatedDate": "2026-10-02 09:00 UTC"}], "returned": 2, "total_count": 2, "truncated": False}
JOB_TABLE = ["| Name | Owner | Notes | Owner Name | Created Date |", "|---|---|---|---|---|",
             "| J-1 | Zed Quill | a¦b next line | Zed Quill | 2026-10-01 09:00 UTC |",
             "| J-2 |  |  |  | 2026-10-02 09:00 UTC |"]


def test_table_headers_cells_and_the_header_rules():
    e = _evidence("q1", "list_records", JOBS)
    # " (name)" stripped, the lookup's " ID" dropped, a later equal header prefixed with its first hop's label, a
    # label equal to the API name humanized; a null is an empty cell, "|" reads "¦", a line break one space
    assert _block(e, T.column_labels(CATALOG)).split("\n") == JOB_TABLE
    assert _block(e).split("\n") == JOB_TABLE   # no catalog (stub tools): every header humanized, the same here
    labels = T.column_labels(CATALOG)
    assert labels("Job__c", "Owner__c.Name") == "Name" and labels("Job__c", "CreatedById") == "Created By"
    assert labels("Job__c", "Team_Lead__c") == "Team Lead" and labels("Job__c", "OwnerId") == "Owner"
    # the result cap took the columns: the keys of the first row
    cut = _evidence("q1", "list_records", {k: v for k, v in JOBS.items() if k != "columns"})
    assert _block(cut, labels).split("\n") == JOB_TABLE


def test_a_header_never_holds_an_api_name_backticks_or_a_tool_word():
    odd = SimpleNamespace(objects={"Job__c": SimpleNamespace(fields={
        "A__c": _f("A__c", "`Code`"), "B__c": _f("B__c", "Lookup Check"), "C__c": _f("C__c", "C__c label")})},
        target_of=lambda f: None)
    labels = T.column_labels(odd)
    assert [labels("Job__c", c) for c in ("A__c", "B__c", "C__c")] == ["Code", "B", "C"]

    class Broken:
        @property
        def objects(self):
            raise RuntimeError("catalog down")

    assert T.column_labels(Broken())("Job__c", "Owner__c (name)") == "Owner"   # never raises


def test_two_fields_with_one_label_get_their_own_headers():
    """Review of 2026-10-30 (quality-5): a later field whose catalog label an earlier one has is headed by its API name
    in words (no "__", no backticks), so the reader can tell the columns apart."""
    same = SimpleNamespace(objects={"Job__c": SimpleNamespace(fields={f.api: f for f in (
        _f("Scheduled_Start__c", "Start Time"), _f("Local_Start__c", "Start Time"), _f("Niche__c", "Niche"),
        _f("Niches__c", "Niche"))})}, target_of=lambda f: None)
    rows = [{"Scheduled_Start__c": "09:00", "Local_Start__c": "14:30", "Niche__c": "Data", "Niches__c": "Data; Cloud"}]
    e = _evidence("q1", "list_records", {"columns": list(rows[0]), "rows": rows, "total_count": 1})
    head = _block(e, T.column_labels(same)).split("\n")[0]
    assert head == "| Start Time | Local Start | Niche | Niches |"


def test_no_rows_for_the_other_tools_a_failed_call_and_a_count_without_breakdown(tools):
    ev = _run(tools, ("aggregate_records", {"object": "Internal_Interview__c", "op": "avg",
                                            "field": "Human_Total_Score__c"}),
              ("percentage_of_records", {"object": "Interview__c",
                                         "part_filters": [F("Interview_Status__c", "Completed")]}),
              ("search_records", {"name": "recruiter b"}),
              ("get_field_history", {"object": "Interview__c", "field": "Interview_Status__c"}),
              ("get_sync_status", {}),
              ("get_field_values", {"object": "Interview__c", "field": "Interview_Status__c"}),
              ("about_people", {"names": ["Delta Person"]}),
              ("find_references", {"name": "Echo One"}),
              ("describe_fields", {"object": "Interview__c", "query": "offer"}),
              ("group_records", {"object": "Interview__c", "group_by": "Nope__c"}),
              ("count_records", {"object": "Interview__c"}),
              ("list_records", {"object": "Interview__c", "filters": [F("Interview_Status__c", "Rescheduled")]}))
    assert [e.tool for e in ev][6:8] == ["about_people", "about_people"]   # find_references runs about_people
    assert all(T.render(e) is None for e in ev)
    text = "Made-up lead.\n\n" + "\n".join("{{table q%d}}" % i for i in range(1, 14)) + "\n\n" + SOURCE
    x = T.expand(text, ev)
    assert x.text == "Made-up lead.\n\n" + SOURCE and x.blocks == {} and x.sources == []
    assert x.counts["dropped"] == {"no_rows": 10, "failed": 1, "empty": 1, "unknown": 1}
    assert x.bad == [f"q{i}" for i in range(1, 14)]


def _ev_list(tools):
    return _run(tools, GROUP, SPLIT, TREND, AVG, COMPARE, COUNT, LIST)


def test_rows_come_from_model_result_only(tools):
    """Mutation-sensitive (a)-(e): the statements (record Ids, the limit+1 row, raw values) are never read; a changed
    result value changes the rows; the result order is kept; no Total line unless the rows add up to the result's own
    total of them; every number of a block is the result's."""
    ev = _ev_list(tools)
    group, lst = ev[0], ev[6]
    sentinel = Statement(sql="SELECT 1", params=[], columns=["Id", "Name", "n"],
                         rows=[["a0AZZZZZZZZZZZZZZZ", "I-SENTINEL", 3.14159]] * 6, row_count=6, truncated=True,
                         elapsed_ms=1.0)
    for e in (group, lst):
        mut = dataclasses.replace(e, statements=[sentinel])
        assert T.render(mut, T.column_labels(tools.catalog)) == T.render(e, T.column_labels(tools.catalog))
        assert not any(s in T.render(mut)[0] for s in ("a0AZZZZZZZZZZZZZZZ", "I-SENTINEL", "3.14"))
    changed = copy.deepcopy(group.model_result)
    changed["groups"][0]["value"] = 5
    out = _block(dataclasses.replace(group, model_result=changed)).split("\n")
    assert out[0] == "- Recruiter A: 5" and not out[-1].startswith("- Total")   # 11 != 10: no total
    unordered = _evidence("q1", "group_records", {"measure": "count", "groups": [
        {"group": "Beta", "value": 1}, {"group": "Alpha", "value": 5}], "total_across_groups": 6})
    assert _block(unordered).split("\n") == ["- Beta: 1", "- Alpha: 5", "- Total: 6"]
    # no Total: top=N, the 50-group cap, a trim, a multi-select field, a total that is not the rows' sum
    (top, multi) = _run(tools, ("group_records", {"object": "Interview__c", "group_by": "Recruiter__c", "top": 2}),
                        ("group_records", {"object": "Interview__c", "group_by": "Evaluation_Parameter__c"}))
    assert _block(top).split("\n") == ["- Recruiter A: 4", "- Recruiter B (#1): 2"]
    assert "records_matched" in multi.model_result and "Total" not in _block(multi)
    base = {"measure": "count", "groups": [{"group": "Alpha", "value": 3}, {"group": "Beta", "value": 2}], "shown": 2}
    capped = _evidence("q1", "group_records", {**base, "total_across_groups": 9, "notes": ["showing 2 of 4 groups"]})
    assert _block(capped).split("\n") == ["- Alpha: 3", "- Beta: 2", "", "Showing 2 of 4 groups."]
    trimmed = _evidence("q1", "group_records", {**base, "total_across_groups": 6,
                                                "notes": ["result trimmed to fit: showing 2 of 3"]})
    assert _block(trimmed).split("\n") == ["- Alpha: 3", "- Beta: 2", "", "Showing 2 of 3 groups."]
    other_list = _evidence("q1", "group_records", {**base, "total_across_groups": 5,   # a trim of another list
                                                   "notes": ["result trimmed to fit: showing 1 of 4 also_held"]})
    assert _block(other_list).split("\n") == ["- Alpha: 3", "- Beta: 2", "- Total: 5"]
    off = _evidence("q1", "group_records", {**base, "total_across_groups": 6})
    assert _block(off).split("\n") == ["- Alpha: 3", "- Beta: 2"]
    for e in ev:   # (e) every number of a block is the result's, or 0-12
        block = _block(e, T.column_labels(tools.catalog))
        held = set(numbers_in(json.dumps(e.model_result, ensure_ascii=False)))
        assert all(n in held or (n.isdigit() and int(n) <= 12) for n in numbers_in(block)), (e.tool, block)


def test_one_row_gets_no_total_line():
    """Fidelity preview of 2026-10-30: under one group (or one period) a Total line only repeats that row, split and
    all; 2 or more rows keep it."""
    one = _evidence("q1", "group_records", {"measure": "count", "groups": [
        {"group": "Delta Person", "value": 3, "by": {"Completed": 2, "Scheduled": 1}}], "total_across_groups": 3,
        "split_totals": {"Completed": 2, "Scheduled": 1}})
    assert _block(one).split("\n") == ["- Delta Person: 3 (Completed: 2, Scheduled: 1)"]
    month = _evidence("q1", "trend_records", {"grain": "month", "buckets": [{"bucket": "2026-09", "value": 7}],
                                              "total": 7})
    assert _block(month).split("\n") == ["- 2026-09: 7"]
    two = _evidence("q1", "trend_records", {"grain": "month", "total": 7, "buckets": [
        {"bucket": "2026-09", "value": 7}, {"bucket": "2026-10", "value": 0}]})
    assert _block(two).split("\n") == ["- 2026-09: 7", "- 2026-10: 0", "- Total: 7"]


def test_a_cut_list_gets_a_showing_line_from_the_results_numbers():
    rows = [{"Name": f"J-{i}"} for i in range(1, 19)]
    trimmed = _evidence("q1", "list_records", {"columns": ["Name"], "rows": rows, "returned": 50, "shown": 18,
                                               "total_count": 54, "notes": ["result trimmed to fit: showing 18 of 50"]})
    assert _block(trimmed).split("\n")[-2:] == ["", "Showing 18 of 54 records."]
    x = T.expand("18 of the 54 jobs are shown (`Job__c`).\n\n{{table q1}}\n\n" + SOURCE, [trimmed])
    assert "Showing" not in x.text and x.text.endswith("| J-18 |\n\n" + SOURCE)   # the lead gives both numbers
    unknown = _evidence("q1", "list_records", {"columns": ["Name"], "rows": rows + rows[:2], "returned": 50,
                                               "total_count": 54})   # 20 rows, neither returned nor shown: no number
    assert _block(unknown).split("\n")[-1] == "Not all records are shown."
    small = _evidence("q1", "list_records", {"columns": ["Name"], "rows": rows[:3], "total_count": 54})
    assert _block(small).split("\n")[-1] == "Showing 3 of 54 records."   # 3 <= 12
    # the numbers of a date or of the Source line state nothing: "10" and "13" of "2026-10-13" keep the line
    ten = _evidence("q1", "list_records", {"columns": ["Name"], "rows": rows[:10], "returned": 10, "total_count": 13})
    x = T.expand("The latest jobs (`Job__c`), as of 2026-10-13.\n\n{{table q1}}\n\n" + SOURCE, [ten])
    assert "\n\nShowing 10 of 13 records.\n\n" + SOURCE in x.text
    # a trend the result cap trimmed counts periods (mutation-sensitive: the unit word of the trend)
    trend = _evidence("q1", "trend_records", {"grain": "month", "buckets": [
        {"bucket": "2026-09", "value": 7}, {"bucket": "2026-10", "value": 3}], "shown": 2, "total": 31,
        "notes": ["result trimmed to fit: showing 2 of 5"]})
    assert _block(trend).split("\n") == ["- 2026-09: 7", "- 2026-10: 3", "", "Showing 2 of 5 periods."]


def test_the_safety_cap_is_read_at_call_time(monkeypatch):
    rows = [{"Name": f"J-{i}"} for i in range(1, 6)]
    lst = _evidence("q1", "list_records", {"columns": ["Name", "Notes__c"], "rows": rows, "total_count": 5})
    grp = _evidence("q2", "group_records", {"measure": "count", "groups": [{"group": f"G{i}", "value": i}
                                                                           for i in range(1, 6)],
                                            "total_across_groups": 15})
    assert T.TABLE_MAX_ROWS == 200 and "…" not in _block(lst) + _block(grp)
    monkeypatch.setattr(T, "TABLE_MAX_ROWS", 3)
    assert _block(lst).split("\n")[-1] == "| … | … |" and len(_block(lst).split("\n")) == 6
    assert _block(grp).split("\n") == ["- G1: 1", "- G2: 2", "- G3: 3", "- …"]   # number-free, no Total


# ------------------------------------------------------------------------------------------------ table lines
GROUP_EV = _evidence("q1", "group_records", {"measure": "count", "groups": [{"group": "Zed Quill", "value": 4},
                                                                           {"group": "Ana Bo", "value": 2}],
                                             "total_across_groups": 6})
ROWS = "- Zed Quill: 4\n- Ana Bo: 2\n- Total: 6"
LEAD = "6 interviews in all (`Interview__c`)."


@pytest.mark.parametrize("line", ["{{table q1}}", "{{ Table: Q1 }}", "{{table 1}}", "{{table=q01}}", "**{{table q1}}**",
                                  "`{{table q1}}`", "- {{table q1}}", "### {{table q1}}", "> {{table q1}}"])
def test_every_form_of_a_table_line_is_filled(line):
    x = T.expand(LEAD + "\n\n" + line + "\n\n" + SOURCE, [GROUP_EV])
    assert x.text == LEAD + "\n\n" + ROWS + "\n\n" + SOURCE
    assert x.blocks == {"{{table q1}}": ROWS} and x.sources == [ROWS] and x.bad == []
    assert x.counts == {"markers": 1, "built": 1, "rows": 2, "dropped": {}, "partly_typed": 0,
                        "items": [{"query_id": "q1", "tool": "group_records", "rows": 2, "form": "lines"}]}


def test_malformed_spans_are_removed_and_named_and_other_braces_stay():
    x = T.expand(LEAD + "\n\n{{table}}\n{{tables q3 and q4}} \n{{q3}}\n\nHello {{FirstName}}.\n\n" + SOURCE, [GROUP_EV])
    assert x.text == LEAD + "\n\nHello {{FirstName}}.\n\n" + SOURCE
    assert x.bad == ["table", "tables q3 and q4", "q3"] and x.counts["dropped"] == {"malformed": 3}
    assert x.counts["markers"] == 3 and x.blocks == {}


NEAR_MISSES = ["{{table q1}", "{table q1}", "{{table_q1}}", "{{Table-Q1}}", "{{tbl q1}}", "{{query q1}}",
               "{{result q1}}", "{{rows q1}}", "{{q_1}}", "{{q-1}"]


@pytest.mark.parametrize("line", NEAR_MISSES)
def test_a_near_miss_of_a_table_line_never_reaches_the_user(line):
    """Mutation-sensitive (review of 2026-10-30, behaviour-3 / quality-3): one or two braces around a table, tbl,
    list, rows, query or result word, or a query id joined by "_" or "-", is malformed: removed, named for the draft
    message (no braces) and counted; the pipeline's last resort removes it too."""
    x = T.expand(LEAD + "\n\n" + line + "\n\n" + SOURCE, [GROUP_EV])
    assert x.text == LEAD + "\n\n" + SOURCE and x.counts["dropped"] == {"malformed": 1} and x.counts["markers"] == 1
    assert len(x.bad) == 1 and "{" not in x.bad[0] and "}" not in x.bad[0]
    inline = "See " + line + " below."
    assert T.strip_markers(LEAD + "\n" + inline + "\n" + SOURCE) == LEAD + "\nSee below.\n" + SOURCE
    assert T.left_over(T.strip_markers(line)) == 0 and T.left_over(line) == 1


def test_left_over_counts_the_braces_a_final_answer_still_holds():
    assert T.left_over("Dear {{FirstName}}, see {table") == 2 and T.left_over("no braces {a: 1}") == 0
    assert T.left_over(None) == 0 and T.left_over("[[table q1]]") == 0


def test_each_drop_reason(tools, monkeypatch):
    ev = _run(tools, GROUP, ("group_records", {"object": "Interview__c", "group_by": "Nope__c"}),
              ("count_records", {"object": "Interview__c"}),
              ("list_records", {"object": "Interview__c", "filters": [F("Interview_Status__c", "Rescheduled")]}))
    reply = "10 interviews.\n\n{{table q9}}\n{{table q2}}\n{{table q3}}\n{{table q4}}\n{{table q1}}\n{{table q1}}\n\n"
    x = T.expand(reply + SOURCE, ev)
    assert x.counts["dropped"] == {"unknown": 1, "failed": 1, "no_rows": 1, "empty": 1, "repeated": 1}
    assert x.counts["no_rows_by"] == {"count_records": 1}   # the no_rows drops only
    assert x.bad == ["q9", "q2", "q3", "q4"] and list(x.blocks) == ["{{table q1}}"]   # repeated: not for the model
    assert x.text == "10 interviews.\n\n" + "\n".join(GROUP_LINES) + "\n\n" + SOURCE
    # unseen: a result longer than the model's tool message could hold (patched limit)
    monkeypatch.setattr(T, "LIMITS", dataclasses.replace(C.LIMITS, knowledge_tool_result_chars=3000))
    x = T.expand("10 interviews.\n\n{{table q1}}\n\n" + SOURCE, ev)
    assert x.counts["dropped"] == {"unseen": 1} and x.bad == [] and x.text == "10 interviews.\n\n" + SOURCE
    monkeypatch.undo()
    # typed: the model also typed the rows; its own copy stays
    typed = LEAD + "\n\n- Zed Quill: 4 interviews\n- Ana Bo: 2\n\n{{table q1}}\n\n" + SOURCE
    x = T.expand(typed, [GROUP_EV])
    assert x.counts["dropped"] == {"typed": 1} and x.text == typed.replace("\n\n{{table q1}}", "")
    # one typed row of two is no copy: the rows are filled, and counted as typed in part
    x = T.expand(LEAD + "\n- Zed Quill: 4\n\n{{table q1}}\n\n" + SOURCE, [GROUP_EV])
    assert x.counts["built"] == 1 and x.counts["partly_typed"] == 1


#: Six made-up departments (20/12/8/5/3/2 of 50 open jobs).
DEPTS = [("Engineering", 20), ("Sales", 12), ("HR", 8), ("Finance", 5), ("Legal", 3), ("Ops", 2)]
DEPT_EV = _evidence("q1", "group_records", {"grouped_by": "Department__c", "measure": "count", "groups": [
    {"group": g, "value": v} for g, v in DEPTS], "total_across_groups": 50, "shown": 6})
DEPT_ROWS = "\n".join(f"- {g}: {v}" for g, v in DEPTS) + "\n- Total: 50"


def test_a_bullet_that_names_some_rows_types_none_and_a_partial_copy_keeps_every_row():
    """Mutation-sensitive (review of 2026-10-30, behaviour-1 / quality-1): a row is typed only by a line of its own
    (a summary bullet naming the top 3 of 6 groups types none), and only a copy of every row keeps the model's own
    copy; rows typed in part are filled all the same (``partly_typed``), so no row is lost."""
    summary = "- Engineering (20), Sales (12) and HR (8) hold most of the 50 open jobs (`Job__c`)."
    x = T.expand(summary + "\n\n{{table q1}}\n\n" + SOURCE, [DEPT_EV])
    assert x.blocks == {"{{table q1}}": DEPT_ROWS} and x.text == summary + "\n\n" + DEPT_ROWS + "\n\n" + SOURCE
    assert x.counts["dropped"] == {} and x.counts["partly_typed"] == 0
    # the top 3 typed one per line: half of the rows, the other 3 would be lost without the block
    top = "50 open jobs (`Job__c`).\n- Engineering: 20\n- Sales: 12\n- HR: 8\n\n{{table q1}}\n\n" + SOURCE
    x = T.expand(top, [DEPT_EV])
    assert all(g in x.text for g, _v in DEPTS) and x.counts["built"] == 1 and x.counts["partly_typed"] == 1
    # a "top 5" list types 5 rows of 6: still no copy
    five = "50 open jobs.\n" + "\n".join(f"- {g}: {v}" for g, v in DEPTS[:5]) + "\n\n{{table q1}}\n\n" + SOURCE
    assert "- Ops: 2" in T.expand(five, [DEPT_EV]).text
    # every row typed, one per line: the model's own copy stays and the table line goes
    full = "50 open jobs.\n" + "\n".join(f"- {g}: {v} jobs" for g, v in DEPTS) + "\n\n{{table q1}}\n\n" + SOURCE
    x = T.expand(full, [DEPT_EV])
    assert x.counts["dropped"] == {"typed": 1} and x.text == full.replace("\n\n{{table q1}}", "")
    # a label inside a longer one ("Open" in "Open - Hold") does not make the longer one's line name two rows
    nested = _evidence("q1", "group_records", {"measure": "count", "groups": [
        {"group": "Open", "value": 5}, {"group": "Open - Hold", "value": 2}], "total_across_groups": 7})
    assert T.expand("7 jobs.\n- Open: 5\n- Open - Hold: 2\n\n{{table q1}}\n\n" + SOURCE,
                    [nested]).counts["dropped"] == {"typed": 1}
    # the merge's put-back: a summary bullet next to the kept table line, and a merged text that types half the rows
    # without the line, both get every row back
    blocks = {"{{table q1}}": DEPT_ROWS}
    merged = ("Engineering has the most open jobs, 20 of 50.\n\n- Engineering (20), Sales (12) and HR (8) lead.\n\n"
              "{{table q1}}\n\n" + SOURCE)
    text, counts = T.restore(merged, blocks)
    assert DEPT_ROWS in text and counts == {"lines": 1, "lost": 0, "retyped": 0, "repeated": 0, "partly_retyped": 0}
    half = "Engineering leads.\n- Engineering: 20\n- Sales: 12\n- HR: 8\n" + SOURCE
    text, counts = T.restore(half, blocks)
    assert text.endswith(DEPT_ROWS + "\n\n" + SOURCE)
    assert counts == {"lines": 0, "lost": 1, "retyped": 0, "repeated": 0, "partly_retyped": 1}


def test_placement_keeps_the_lead_first_and_the_source_line_last():
    two = [GROUP_EV, _evidence("q2", "compare_records", {"segments": [{"label": "Open", "value": 3},
                                                                       {"label": "Closed", "value": 1}]})]
    cmp_rows = "- Open: 3\n- Closed: 1"
    cases = {   # reply -> expected text
        "There were 6 interviews ({{table q1}}).\n" + SOURCE: "There were 6 interviews.\n\n" + ROWS + "\n\n" + SOURCE,
        LEAD + "\n{{table q2}}\n{{table q1}}\n" + SOURCE: LEAD + "\n\n" + cmp_rows + "\n\n" + ROWS + "\n\n" + SOURCE,
        LEAD + "\n" + SOURCE + "\n{{table q1}}": LEAD + "\n\n" + ROWS + "\n\n" + SOURCE,
        "{{table q1}}\n\n" + LEAD + "\nA second line.\n\nMore.\n\n" + SOURCE:
            LEAD + "\nA second line.\n\n" + ROWS + "\n\nMore.\n\n" + SOURCE,
        "{{table q1}}\n{{table q2}}": ROWS + "\n\nCompared:\n" + cmp_rows,   # only table lines: rows only
        LEAD + "\n```\n{{table q1}}\n```\n" + SOURCE: LEAD + "\n\n" + ROWS + "\n\n" + SOURCE,
        LEAD + "\n{{table q1}}": LEAD + "\n\n" + ROWS,
        "{{table q9}}\n\n" + LEAD + "\n\n" + SOURCE: LEAD + "\n\n" + SOURCE,
        # review of 2026-10-30 (behaviour-5): a Source line written first is no end of the text (the lead stays above
        # the rows), and numbered or joined table lines leave no "1.", "and" or "," behind
        SOURCE + "\n\n" + LEAD + "\n\n{{table q1}}": SOURCE + "\n\n" + LEAD + "\n\n" + ROWS,
        SOURCE + "\n\n{{table q1}}\n\n" + LEAD: SOURCE + "\n\n" + LEAD + "\n\n" + ROWS,
        LEAD + "\n1. {{table q1}}\n2) {{table q2}}\n" + SOURCE: LEAD + "\n\n" + ROWS + "\n\nCompared:\n" + cmp_rows
        + "\n\n" + SOURCE,
        LEAD + "\n{{table q1}} and {{table q2}}\n" + SOURCE: LEAD + "\n\n" + ROWS + "\n\nCompared:\n" + cmp_rows
        + "\n\n" + SOURCE,
        LEAD + "\nTables: {{table q1}}, {{table q2}}.\n" + SOURCE: LEAD + "\nTables:\n\n" + ROWS + "\n\nCompared:\n"
        + cmp_rows + "\n\n" + SOURCE,
        LEAD + "\nTables: {{table q1}}, {{table q2}}\n" + SOURCE: LEAD + "\nTables:\n\n" + ROWS + "\n\nCompared:\n"
        + cmp_rows + "\n\n" + SOURCE,
    }
    for reply, want in cases.items():
        assert T.expand(reply, two).text == want, reply
    code = LEAD + "\n```\nSELECT 1\n{{table q1}}\n```\n" + SOURCE   # a fence with other text keeps its fence
    assert T.expand(code, two).text.count("```") == 2


#: Results of every lines shape and a list (made-up jobs), for the label lines of blocks that follow one another.
SHAPES = [
    _evidence("q1", "group_records", {"grouped_by": "Status__c", "measure": "count", "groups": [
        {"group": "Open", "value": 45}, {"group": "Closed", "value": 16}], "total_across_groups": 61}),
    _evidence("q2", "group_records", {"grouped_by": "Employment_Type__c (Type name)", "measure": "count", "groups": [
        {"group": "Full Time", "value": 40}, {"group": "Contract", "value": 21}], "total_across_groups": 61}),
    _evidence("q3", "trend_records", {"grain": "month", "buckets": [
        {"bucket": "2026-09", "value": 30}, {"bucket": "2026-10", "value": 31}], "total": 61}),
    _evidence("q4", "count_records", {"count": 61, "breakdown": {"Stage__c": {"Screening": 50, "Offer": 11}}}),
    _evidence("q5", "compare_records", {"segments": [{"label": "Remote", "value": 9},
                                                     {"label": "On site", "value": 7}]}),
    _evidence("q6", "list_records", JOBS)]
SHAPES_TEXT = ("61 jobs (`Job__c`) by status, type and month.\n\n"
               "- Open: 45\n- Closed: 16\n- Total: 61\n\n"
               "By Employment Type:\n- Full Time: 40\n- Contract: 21\n- Total: 61\n\n"
               "By month:\n- 2026-09: 30\n- 2026-10: 31\n- Total: 61\n\n"
               "By Stage:\n- Screening: 50\n- Offer: 11\n\n"
               "Compared:\n- Remote: 9\n- On site: 7\n\n" + "\n".join(JOB_TABLE) + "\n\n" + SOURCE)


def test_a_lines_block_right_after_another_gets_its_label_line():
    """Mutation-sensitive (review of 2026-10-30, behaviour-2): the note asks for one table line per result, so blocks
    follow one another, and the page (like Markdown) reads two lists that only a blank line separates as one. A lines
    block right after another block starts with a number-free label line of its result ("By <field label>:", "By
    <grain>:", "Compared:"); the first block, a table and a block after text of the model's get none. The label is
    part of the stored block, so holds, marked and restore keep it."""
    reply = ("61 jobs (`Job__c`) by status, type and month.\n\n{{table q1}}\n{{table q2}}\n{{table q3}}\n{{table q4}}\n"
             "{{table q5}}\n{{table q6}}\n\n" + SOURCE)
    x = T.expand(reply, SHAPES)
    assert x.text == SHAPES_TEXT
    assert x.blocks["{{table q2}}"].startswith("By Employment Type:\n- Full Time: 40") and x.sources == [
        "\n\n".join(x.blocks.values())]
    assert all(T.holds(x.text, b) for b in x.blocks.values()) and not any(
        numbers_in(b.split("\n")[0]) for b in x.blocks.values() if not b.startswith(("- ", "|")))
    back = T.marked(x.text, x.blocks)   # the merge call's record answer: the label lines go with their rows
    assert back == reply.replace("}}\n{{", "}}\n\n{{") and T.restore(back, x.blocks)[0] == x.text
    # text of the model's own between two blocks: no label line
    x = T.expand("61 jobs.\n\n{{table q1}}\n\nBy type:\n{{table q2}}\n\n" + SOURCE, SHAPES)
    assert x.text == ("61 jobs.\n\n- Open: 45\n- Closed: 16\n- Total: 61\n\nBy type:\n\n- Full Time: 40\n- Contract: 21"
                      "\n- Total: 61\n\n" + SOURCE)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_shows_blocks_that_follow_one_another_as_separate_lists():
    proc = subprocess.run(["node", str(ROOT / "tests" / "render_check.js")], capture_output=True, text=True,
                          input=json.dumps({"shapes": SHAPES_TEXT}), timeout=30)
    assert proc.returncode == 0, proc.stderr
    nodes = _tree(json.loads(proc.stdout)["shapes"]["tree"])
    assert [n[0] if isinstance(n, tuple) else n for n in nodes] == ["p", "ul", "p", "ul", "p", "ul", "p", "ul", "p",
                                                                    "ul", "div", "p"]
    assert [len(n[1]) for n in nodes if n[0] == "ul"] == [3, 3, 3, 2, 2]   # one list per result
    assert [n[1] for n in nodes[2:9:2]] == [["By Employment Type:"], ["By month:"], ["By Stage:"], ["Compared:"]]


def test_no_table_line_is_left_and_every_block_has_one_blank_line_around_it():
    two = [GROUP_EV, _evidence("q2", "list_records", JOBS)]
    forms = ["{{table q1}}", "**{{ table: q2 }}**", "`{{table 2}}`", "- {{table q1}}", "{{q7}}", "{{tables 1 and 2}}",
             "x {{table q2}} y", "{{table q9}}"]
    shells = ["{}", "Lead.\n{}", "Lead.\n\n\n{}\n\n\n" + SOURCE, "{}\n" + SOURCE, "Lead.\n" + SOURCE + "\n{}",
              "{}\nLead one.\nLead two.\n- a bullet\n{}", "Lead.\n{}\n{}\n" + SOURCE]
    for shell in shells:
        for a in forms:
            for b in forms:
                text = shell.replace("{}", a, 1).replace("{}", b, 1)
                x = T.expand(text, two)
                assert not T.MARKER_RE.search(x.text) and not T.LOOSE_RE.search(x.text), text
                for block in x.blocks.values():
                    k = x.text.index(block)
                    before, after = x.text[:k], x.text[k + len(block):]
                    assert before == "" or (before.endswith("\n\n") and not before.endswith("\n\n\n")), text
                    assert after == "" or (after.startswith("\n\n") and not after.startswith("\n\n\n")), text


def test_the_fast_path_returns_the_same_object():
    text = LEAD + "\n\n- typed: 4\n\n" + SOURCE
    x = T.expand(text, [GROUP_EV])
    assert x.text is text and x.blocks == {} and x.bad == [] and x.sources == []
    assert x.counts == {"markers": 0, "built": 0, "rows": 0, "dropped": {}, "partly_typed": 0, "items": []}
    assert T.strip_markers(text) is text and T.strip_markers("Hello {{FirstName}}") == "Hello {{FirstName}}"
    braces = "\n" + LEAD + "\n\nHello {{FirstName}} {a: 1}\n"   # braces but no table line: the same object too
    assert T.expand(braces, [GROUP_EV]).text is braces and T.strip_markers(braces) is braces
    assert T.own_lines(text) is text and T.own_lines(braces) is braces


def test_nothing_here_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("renderer down")

    text = LEAD + "\n\n{{table q1}}\n\n" + SOURCE
    monkeypatch.setattr(T, "_render", boom)
    x = T.expand(text, [GROUP_EV])
    assert (x.text, x.counts, x.blocks, x.sources, x.bad) == (T.strip_markers(text), {"error": "RuntimeError"}, {},
                                                               [], [])
    assert x.text == LEAD + "\n\n" + SOURCE
    monkeypatch.undo()
    # malformed results: rows as a string and groups None give no rows; "by" as a list falls back
    for result, dropped in (({"columns": ["Name"], "rows": "J-1"}, {"empty": 1}),):
        x = T.expand(text, [_evidence("q1", "list_records", result)])
        assert x.counts["dropped"] == dropped and "{{" not in x.text
    x = T.expand(text, [_evidence("q1", "group_records", {"groups": None})])
    assert x.counts["dropped"] == {"empty": 1} and "{{" not in x.text
    x = T.expand(text, [_evidence("q1", "group_records", {"groups": [{"group": "A", "value": 1, "by": ["x"]}]})])
    assert x.counts == {"error": "AttributeError"} and x.text == LEAD + "\n\n" + SOURCE
    assert T.render(_evidence("q1", "group_records", {"groups": [{"group": "A", "value": 1, "by": ["x"]}]})) is None
    assert T.strip_markers(None) is None and T.strip_markers(123) == "123"
    assert T.gap_before_source(None, {"{{table q1}}": ROWS}) is None and T.marked(None, {"{{table q1}}": ROWS}) == ""
    assert T.holds(None, ROWS) is False and T.marker_message(None).startswith("Code cannot fill the table lines for :")
    assert T.answer_counts(None, None, None, 0) == {"kept": 0, "nudged": 0, "chars": 0, "cut": 0}
    typed = T.expand(LEAD + "\n\n- Zed Quill: 4\n- Ana Bo: 2\n\n{{table q1}}\n\n" + SOURCE, [GROUP_EV])
    monkeypatch.setattr(T, "_layout", boom)
    assert T.restore(LEAD + "\n{{table q1}}\n" + SOURCE, {"{{table q1}}": ROWS}) is None
    assert T.put_back("nothing typed", typed) == "nothing typed" and typed.blocks == {}   # restore failed: as it is
    from sf_assistant.agent import nudges as N
    monkeypatch.setattr(T, "_matches", boom)
    assert T.line_count("x") == 0
    monkeypatch.setattr(N, "_fills", boom)
    assert N.nudge_kinds("x") == []


def test_marked_holds_gap_and_restore():
    x = T.expand(LEAD + "\n\n{{table q1}}\n\n" + SOURCE, [GROUP_EV])
    assert T.holds(x.text, ROWS) and not T.holds(x.text, "- Zed Quill: 4\n- Ana Bo")
    assert T.marked(x.text, x.blocks) == LEAD + "\n\n{{table q1}}\n\n" + SOURCE
    assert T.marked(x.text.replace("Ana Bo", "Ana B"), x.blocks) == ""   # a block not there whole
    bare = LEAD + "\n\n" + ROWS + "\n" + SOURCE   # with_source_line adds the Source line without a blank line
    assert T.gap_before_source(bare, x.blocks) == x.text == T.gap_before_source(x.text, x.blocks)
    blocks = {"{{table q1}}": ROWS}
    kept = "Zed Quill leads with 4.\n\n{{table q1}}\n\n" + SOURCE
    assert T.restore(kept, blocks) == ("Zed Quill leads with 4.\n\n" + ROWS + "\n\n" + SOURCE,
                                       {"lines": 1, "lost": 0, "retyped": 0, "repeated": 0, "partly_retyped": 0})
    assert T.restore("Zed Quill leads.\n" + SOURCE, blocks) == (
        "Zed Quill leads.\n\n" + ROWS + "\n\n" + SOURCE,
        {"lines": 0, "lost": 1, "retyped": 0, "repeated": 0, "partly_retyped": 0})
    assert T.restore("Zed Quill leads.", blocks)[0] == "Zed Quill leads.\n\n" + ROWS   # no Source line: at the end
    assert T.restore("Lead.\n{{table q1}}\n{{table q1}}\n{{table q7}}\n" + SOURCE, blocks) == (
        "Lead.\n\n" + ROWS + "\n\n" + SOURCE, {"lines": 1, "lost": 0, "retyped": 0, "repeated": 1,
                                               "partly_retyped": 0})
    retyped = "Lead.\n- Zed Quill: 4\n- Ana Bo: 2\n{{table q1}}\n" + SOURCE
    assert T.restore(retyped, blocks) == ("Lead.\n- Zed Quill: 4\n- Ana Bo: 2\n" + SOURCE,
                                          {"lines": 0, "lost": 0, "retyped": 1, "repeated": 0, "partly_retyped": 0})
    # review of 2026-10-30 (behaviour-4): a table line the merge kept in a code fence comes out of it, as in expand;
    # the last resort drops such a fence with its line
    fenced = "Lead.\n\n```\n{{table q1}}\n```\n\n- `Status__c` is the status.\n\n" + SOURCE
    assert T.restore(fenced, blocks)[0] == "Lead.\n\n" + ROWS + "\n\n- `Status__c` is the status.\n\n" + SOURCE
    assert T.strip_markers(fenced) == "Lead.\n\n- `Status__c` is the status.\n\n" + SOURCE
    assert T.marker_message(["q9", "tables q3 and q4", "q9"]) == T.RECORDS_TABLES_MARKER_MESSAGE.replace(
        "<<QUERY_IDS>>", "q9, tables q3 and q4")
    assert len(T.marker_message(["q%d" % i for i in range(200)])) < len(T.RECORDS_TABLES_MARKER_MESSAGE) + 200


# ---------------------------------------------------------------------------------------- the record session's steps
def test_the_cleaners_keep_every_table_line_and_the_post_steps_keep_every_block(tools):
    ev = _ev_list(tools)
    forms = "{{table q1}}\n**{{table q2}}**\n- {{table q3}}\n`{{table q4}}`\n{{ Table: Q5 }}\n{{table 6}}\n{{table q7}}"
    reply = "Wait, let me check. 10 client interviews (`Interview__c`, any_role P1).\n\n" + forms + "\n\n" + SOURCE
    cleaned = fix_any_role_scope(drop_ref_codes(drop_tool_talk(drop_working_sentences(strip_reasoning(reply))),
                                                {"P1": "Zed Quill"}), ev)
    assert len(T.MARKER_RE.findall(cleaned)) == 7
    x = T.expand(cleaned, ev, T.column_labels(tools.catalog))
    assert x.counts["built"] == 7 and len(x.blocks) == 7
    question = "a made-up question about interviews"
    sources = [facts_json(ev, 10**9), question, STATUS.line()] + x.sources
    answer = drop_record_side_caveats(drop_overlap_clauses(with_no_data_line(x.text, ev), ev), ev, question)
    answer, removed = drop_unsupported(answer, sources)
    answer = T.gap_before_source(with_source_line(answer, ev, STATUS), x.blocks)
    answer = drop_contact_details(answer)[0]
    assert removed == [] and all(T.holds(answer, b) for b in x.blocks.values())
    assert T.marked(answer, x.blocks).count("{{table q") == 7 and T.restore(T.marked(answer, x.blocks), x.blocks)[0] \
        == answer
    # a made-up sum in the lead is still removed; the rows stay
    made_up = T.expand("In total 379 client interviews (3 + 376).\n\n{{table q1}}\n\n" + SOURCE, ev)
    answer, removed = drop_unsupported(made_up.text, [facts_json(ev, 10**9), question, STATUS.line()]
                                       + made_up.sources)
    assert removed == ["379", "376"] and answer == "\n".join(GROUP_LINES) + "\n\n" + SOURCE


def test_the_block_text_is_an_audit_source_for_a_stored_line_break():
    """Mutation-sensitive: facts_json escapes a stored line break, so "Call back\\n15 Oct" reads as "n15" in the facts
    and as "15" in the cell; without the block's own text among the sources the whole row would go."""
    e = _evidence("q1", "list_records", {"columns": ["Name", "Notes__c"], "rows": [
        {"Name": "J-1", "Notes__c": "Call back\n15 Oct"}, {"Name": "J-2", "Notes__c": "Open"}], "total_count": 2})
    x = T.expand("2 jobs (`Job__c`).\n\n{{table q1}}\n\n" + SOURCE, [e])
    facts = [facts_json([e], 10**9), "list the jobs", STATUS.line()]
    assert "15" not in numbers_in(facts[0])
    assert drop_unsupported(x.text, facts + x.sources) == (x.text, [])
    assert drop_unsupported(x.text, facts)[1] == ["15"]   # the fix is what keeps the row


# ------------------------------------------------------------------------------------------------ RecordSession
class Script:
    """A scripted model: the route call says RECORDS and the names call NONE; the record agent's calls get ``steps`` in
    order (a dict: one tool call; a str: a reply), the last one repeating, and a forced answer (no tools) gets
    ``forced``. ``calls`` keeps every record agent request."""

    def __init__(self, *steps, forced: str = ""):
        self.steps, self.forced, self.calls = list(steps), forced, []

    def chat(self, messages, tools=None, max_tokens=None, **_):
        system = messages[0].get("content")
        if system == rprompts.ROUTE_SYSTEM:
            return {"content": "RECORDS", "tool_calls": [], "usage": {}}
        if system == rprompts.NAMES_SYSTEM:
            return {"content": "NONE", "tool_calls": [], "usage": {}}
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        usage = {"prompt_tokens": 10, "completion_tokens": 2}
        if tools is None:
            return {"content": self.forced, "tool_calls": [], "usage": usage}
        step = self.steps[min(len(self.calls) - 1, len(self.steps) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": usage}
        return {"content": step, "tool_calls": [], "usage": usage}


class StubTools:
    """The record tools surface RecordSession uses, over canned results of fake data: ``results`` maps a tool name to
    its result (a list: one per call, the last repeating; None: the call fails with ``error_code``)."""

    def __init__(self, results: dict, catalog=None, error_code: str = "E2", not_found=None):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {}) for n in results}
        self.results, self.error_code, self.not_found = results, error_code, not_found or []
        if catalog is not None:
            self.catalog = catalog
        self._evidence: list[QueryEvidence] = []
        self.calls: dict[str, int] = {}

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        qid = f"q{len(self._evidence) + 1}"
        res = self.results.get(name)
        k = self.calls[name] = self.calls.get(name, 0) + 1
        if isinstance(res, list):
            res = res[min(k, len(res)) - 1]
        if res is None:
            ev = _evidence(qid, name, {}, ok=False, not_found=list(self.not_found))
            ev.error_code = self.error_code
            self._evidence.append(ev)
            return {"ok": False, "result": None, "error": ev.error, "chunk_ids": []}
        ev = _evidence(qid, name, res, obj=res.get("object", "Job__c"))
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def begin_question(self, question="", people=None, values=None):
        self._evidence = []
        self.calls = {}

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question, missing=()):
        return ""

    def status(self):
        return STATUS

    def close(self):
        pass


JOB_GROUP = {"object": "Job__c", "grouped_by": "Status__c", "measure": "count", "groups": [
    {"group": "Open", "value": 45}, {"group": "Closed", "value": 16}], "group_count": 2, "shown": 2,
    "total_across_groups": 61, "source": "preprod sandbox copy; Job__c synced 2026-10-13 14:30 UTC"}
JOB_ROWS = "- Open: 45\n- Closed: 16\n- Total: 61"
JOB_LEAD = "61 jobs (`Job__c`), by status (`Status__c`)."
JOB_REPLY = JOB_LEAD + "\n\n{{table q1}}\n\n" + SOURCE
GROUP_CALL = {"name": "group_records", "args": {"object": "Job__c", "group_by": "Status__c"}}
LIST_CALL = {"name": "list_records", "args": {"object": "Job__c"}}
QUESTION = "how many jobs per status?"


def _session(tools, *steps, max_steps: int = 12, forced: str = ""):
    llm = Script(*steps, forced=forced)
    return RecordSession(tools, _cfg(), llm=llm, max_steps=max_steps), llm


@pytest.mark.parametrize("value", [None, "model"])
def test_model_mode_is_as_before(monkeypatch, value):
    _default_mode(monkeypatch, value)
    calls = []
    real = T.expand
    monkeypatch.setattr(T, "expand", lambda *a, **k: calls.append(a) or real(*a, **k))
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_REPLY)
    drafts = []
    real_check = session.check_draft
    monkeypatch.setattr(session, "check_draft", lambda d, *a, **k: drafts.append(d) or real_check(d, *a, **k))
    rec = session.ask(QUESTION)
    assert llm.calls[0]["messages"][0]["content"] == rprompts.render_records_system(STATUS)   # byte for byte
    assert rec.answer == JOB_REPLY and "{{table q1}}" in rec.answer   # a literal table line stays as written
    assert calls == [] and drafts == [JOB_REPLY]
    assert rec.code_tables == {} and rec.table_blocks == {}


def test_code_mode_appends_the_note_checks_the_expanded_draft_and_keeps_the_models_own_draft(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_REPLY)
    seen = []
    real = session.check_draft

    def spy(draft, *a, **k):
        seen.append(draft)
        return "Name the department as well." if len(seen) == 1 else real(draft, *a, **k)

    monkeypatch.setattr(session, "check_draft", spy)
    rec = session.ask(QUESTION)
    system = llm.calls[0]["messages"][0]["content"]
    assert system == rprompts.render_records_system(STATUS) + T.RECORDS_TABLES_NOTE and system.startswith(
        rprompts.render_records_system(STATUS))   # appended, never prepended
    assert seen == [JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE]   # rows in place, no table line
    history = llm.calls[2]["messages"]   # the model's history keeps its own draft, with its table line
    assert history[-2] == {"role": "assistant", "content": JOB_REPLY}
    assert history[-1] == {"role": "user", "content": "Name the department as well."}
    assert rec.answer == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE   # lead, blank line, rows, blank line, Source
    assert rec.table_blocks == {"{{table q1}}": JOB_ROWS} and rec.nudges == ["Name the department as well."]
    assert rec.code_tables == {"markers": 1, "built": 1, "rows": 2, "dropped": {}, "partly_typed": 0, "items": [
        {"query_id": "q1", "tool": "group_records", "rows": 2, "form": "lines"}], "kept": 1, "nudged": 0,
        "chars": len(JOB_ROWS), "cut": 0}


NOTES = {"object": "Job__c", "columns": ["Name", "Notes__c"], "rows": [
    {"Name": "J-1", "Notes__c": "Wait, I will call back. P1"}, {"Name": "J-2", "Notes__c": "Call 555 010 2030 back"}],
    "returned": 2, "total_count": 2, "truncated": False,
    "source": "preprod sandbox copy; Job__c synced 2026-10-13 14:30 UTC"}
PEOPLE = {"object": "Staff__c", "people": [{"ref": "P1", "label": "Zed Quill", "kind": "Employee"}],
          "source": "preprod sandbox copy; Staff__c synced 2026-10-13 14:30 UTC"}
PEOPLE_CALL = {"name": "about_people", "args": {"names": ["Zed Quill"]}}
NOTES_LEAD = "2 jobs (`Job__c`) with their notes (`Notes__c`)."


def test_code_rows_come_after_the_cleaners_and_under_the_contact_rule(monkeypatch):
    """Mutation-sensitive: a cell the cleaners would rewrite (a "Wait, I will ..." sentence, the reference code P1 an
    about_people result defines) stands verbatim; a phone number inside a cell goes unless the question asks for
    contact details, the row keeps its cell count and the stored block equals its text in the answer."""
    monkeypatch.setenv("SFA_TABLES", "code")
    reply = NOTES_LEAD + "\n\n{{table q2}}\n\n" + SOURCE
    for question, phone in (("list the jobs and their notes", "[phone number not shown]"),
                            ("list the jobs with their phone numbers", "555 010 2030")):
        session, _llm = _session(StubTools({"about_people": PEOPLE, "list_records": NOTES}), PEOPLE_CALL, LIST_CALL,
                                 reply)
        rec = session.ask(question)
        table = ["| Name | Notes |", "|---|---|", "| J-1 | Wait, I will call back. P1 |",
                 f"| J-2 | Call {phone} back |"]
        assert rec.answer == NOTES_LEAD + "\n\n" + "\n".join(table) + "\n\n" + SOURCE, question
        assert rec.table_blocks == {"{{table q2}}": "\n".join(table)} and rec.code_tables["kept"] == 1
        assert rec.dropped_numbers == [] and rec.unsupported_numbers == []


def test_a_stored_line_break_keeps_its_row_in_the_session(monkeypatch):
    """Mutation-sensitive (the newline quirk): without the block text among the audit sources of RecordSession.ask,
    drop_unsupported removes the row and the final audit lists 15; without it among the first draft check's (review
    of 2026-10-30, tests-2), the draft gets a number message about a cell the model never typed (one more round)."""
    monkeypatch.setenv("SFA_TABLES", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "Call back\n15 Oct"}, {"Name": "J-2", "Notes__c": "Open"}]}
    session, llm = _session(StubTools({"list_records": res}), LIST_CALL, NOTES_LEAD + "\n\n{{table q1}}\n\n" + SOURCE)
    rec = session.ask("list the jobs and their notes")
    assert "| J-1 | Call back 15 Oct |" in rec.answer and rec.dropped_numbers == [] and rec.unsupported_numbers == []
    assert rec.nudges == [] and len(llm.calls) == 2   # the tool call and one draft


@pytest.mark.parametrize("cell", ["I will share the CV by Monday. I'll call after notice", "Rescheduled as per "
                                  "instructions from client", "Offer letter in draft", "Expected CTC:\n4500000"])
def test_code_rows_set_off_no_message_about_the_models_words(monkeypatch, cell):
    """Mutation-sensitive (review of 2026-10-30, latency-1): a stored note in a code-built cell (first-person plans,
    prompt talk, "in draft", a line break before a number) is not the model's text: the first draft check sends no
    working-notes or number message about it, so it costs no record-agent round."""
    monkeypatch.setenv("SFA_TABLES", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": cell}, {"Name": "J-2", "Notes__c": "Open"}]}
    session, llm = _session(StubTools({"list_records": res}), LIST_CALL, NOTES_LEAD + "\n\n{{table q1}}\n\n" + SOURCE)
    rec = session.ask("list the jobs and their notes")
    assert rec.nudges == [] and len(llm.calls) == 2
    assert f"| J-1 | {' '.join(cell.split())} |" in rec.answer


def test_the_models_own_working_notes_and_numbers_still_get_their_messages(monkeypatch):
    """The other side of the rule above: a plan of the model's own still gets RECORDS_FINAL_FORM_MESSAGE, and a number
    of the model's own that no result holds still gets the number message, which names that number only (not the
    code-built cell's 15)."""
    monkeypatch.setenv("SFA_TABLES", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "Call back\n15 Oct"}, {"Name": "J-2", "Notes__c": "Open"}]}
    good = NOTES_LEAD + "\n\n{{table q1}}\n\n" + SOURCE
    plan = "Let me list the jobs. I will check their notes first.\n\n{{table q1}}\n\n" + SOURCE
    session, llm = _session(StubTools({"list_records": res}), LIST_CALL, plan, good)
    rec = session.ask("list the jobs and their notes")
    assert len(llm.calls) == 3 and rprompts.RECORDS_FINAL_FORM_MESSAGE in rec.nudges[0]
    made_up = "379 jobs (`Job__c`) with their notes (`Notes__c`).\n\n{{table q1}}\n\n" + SOURCE
    session, llm = _session(StubTools({"list_records": res}), LIST_CALL, made_up, good)
    rec = session.ask("list the jobs and their notes")
    assert rec.nudges == [rprompts.RECORDS_UNSUPPORTED_MESSAGE.replace("<<NUMBERS>>", "379")] and len(llm.calls) == 3


def test_a_table_line_inside_a_planning_sentence_keeps_its_rows(monkeypatch):
    """Mutation-sensitive (review of 2026-10-30, quality-7): the final reply's table lines go on lines of their own
    before the cleaners, so the cleaner that drops a planning sentence keeps the table line in it."""
    monkeypatch.setenv("SFA_TABLES", "code")
    reply = JOB_LEAD + "\nLet me list them by status: {{table q1}}\n\n" + SOURCE
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, reply)
    rec = session.ask(QUESTION)
    assert rec.answer == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    assert rec.code_tables["built"] == 1 and rec.table_blocks == {"{{table q1}}": JOB_ROWS}
    assert T.own_lines("Lead.\nLet me list them: {{table q1}} and {{table q2}}.") == (
        "Lead.\nLet me list them:\n{{table q1}}\n{{table q2}}")
    assert T.own_lines("Lead.\n{{table q1}}\n**{{table q2}}**") == "Lead.\n{{table q1}}\n**{{table q2}}**"


def test_only_the_first_draft_gets_the_marker_message(monkeypatch):
    """Mutation-sensitive (tests-4 c): a redraft left with nothing but a table line code cannot fill gets no marker
    message (no third model round); the answer is the empty record answer, as for any empty redraft."""
    monkeypatch.setenv("SFA_TABLES", "code")
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_REPLY, "{{table q9}}", JOB_REPLY)
    monkeypatch.setattr(session, "check_draft", lambda *a, **k: "Name the status field.")
    rec = session.ask(QUESTION)
    assert len(llm.calls) == 3 and rec.nudges == ["Name the status field."] and rec.code_tables["nudged"] == 0
    assert rec.no_records and rec.error.startswith("empty record answer")


def test_a_block_a_later_step_changed_is_no_stored_block(monkeypatch):
    """Mutation-sensitive (tests-4 e): table_blocks holds only the blocks the answer holds whole, so the merge call
    never gets a table line for rows a post step changed (marked() would give "" and the rows be retyped)."""
    from sf_assistant.agent import record_session as rs

    monkeypatch.setenv("SFA_TABLES", "code")
    real = rs.drop_overlap_clauses
    monkeypatch.setattr(rs, "drop_overlap_clauses",
                        lambda text, ev: real(text, ev).replace("- Closed: 16", "- Closed: 16 (closed)"))
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_REPLY)
    rec = session.ask(QUESTION)
    assert "- Closed: 16 (closed)" in rec.answer and rec.table_blocks == {} and rec.code_tables["kept"] == 0


def test_the_session_heads_a_list_with_catalog_labels_and_adds_its_source_line_after_a_blank_line(tools, monkeypatch):
    """Mutation-sensitive (tests-3): RecordSession.ask passes the tools' catalog to the headers (the fixture warehouse's
    labels, not humanized API names), and a reply without a Source line gets one after a blank line under its rows
    (gap_before_source)."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_RESOLVER", "off")
    session, _llm = _session(tools, {"name": LIST[0], "args": LIST[1]},
                             "10 client interviews (`Interview__c`); 4 shown.\n\n{{table q1}}\n\n" + SOURCE)
    rec = session.ask("list the 4 newest client interviews")
    assert next(ln for ln in rec.answer.split("\n") if ln.startswith("| ")) == LIST_LINES[0]
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_LEAD + "\n\n{{table q1}}")
    rec = session.ask(QUESTION)
    assert rec.answer.startswith(JOB_LEAD + "\n\n" + JOB_ROWS + "\n\nSource: ")
    assert rec.table_blocks == {"{{table q1}}": JOB_ROWS}


def test_a_draft_left_with_nothing_gets_the_marker_message_and_the_redraft_is_accepted(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, "{{table q9}}", JOB_REPLY)
    rec = session.ask(QUESTION)
    message = T.marker_message(["q9"])
    assert llm.calls[2]["messages"][-1] == {"role": "user", "content": message} and len(llm.calls) == 3
    assert rec.nudges == [message] and "q9" in message and "{{" not in message
    assert rec.answer == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    assert rec.code_tables["nudged"] == 1 and rec.code_tables["built"] == 1


def test_a_bad_table_line_joins_another_message_or_is_dropped_without_a_round(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    bad = JOB_LEAD + "\n\n{{table q9}}\n\n" + SOURCE
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, bad, JOB_REPLY)
    monkeypatch.setattr(session, "check_draft", lambda *a, **k: "Say which status field you grouped by.")
    rec = session.ask(QUESTION)
    joined = "Say which status field you grouped by.\n\n" + T.marker_message(["q9"])
    assert llm.calls[2]["messages"][-1]["content"] == joined and rec.nudges == [joined]   # one message, one round
    assert rec.code_tables["nudged"] == 1 and rec.answer == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    # a good lead and no other message: the line is dropped silently, no extra model round
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, bad)
    rec = session.ask(QUESTION)
    assert len(llm.calls) == 2 and rec.nudges == [] and rec.answer == JOB_LEAD + "\n\n" + SOURCE
    assert rec.code_tables == {"markers": 1, "built": 0, "rows": 0, "dropped": {"unknown": 1}, "partly_typed": 0,
                               "items": [], "kept": 0, "nudged": 0, "chars": 0, "cut": 0}
    assert rec.table_blocks == {}


def test_a_forced_answer_is_expanded_and_one_with_only_bad_lines_is_empty(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    tools = StubTools({"group_records": JOB_GROUP})
    session, llm = _session(tools, GROUP_CALL, max_steps=2, forced=JOB_REPLY)
    rec = session.ask(QUESTION)
    assert llm.calls[-1]["tools"] is None and rec.answer == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, max_steps=2,
                             forced="{{table q9}}\n{{tables q1 and q2}}")
    rec = session.ask(QUESTION)
    assert rec.no_records and rec.answer == "" and rec.error.startswith("empty record answer")
    assert "{{" not in rec.dropped_answer and rec.code_tables["dropped"] == {"unknown": 1, "malformed": 1}


def test_a_not_found_name_with_a_table_line_is_dropped_as_before(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    nf = [{"name": "Nobody Here", "target": "Employee (Staff__c)", "object": "Job__c", "field": "Owner__c",
           "match": "exact", "close": []}]
    reply = "0 jobs (`Job__c`) are owned by Nobody Here (`Owner__c`).\n\n{{table q1}}\n\n" + SOURCE
    session, _llm = _session(StubTools({"count_records": None}, error_code="E4h", not_found=nf),
                             {"name": "count_records", "args": {"object": "Job__c"}}, reply)
    rec = session.ask("how many jobs does nobody here own?")
    assert rec.no_records and rec.answer == "" and "Nobody Here" in rec.not_found_answer
    assert rec.dropped_answer and "{{" not in rec.dropped_answer
    assert rec.code_tables["dropped"] == {"failed": 1} and rec.table_blocks == {}


def test_code_tables_hold_only_counts_fixed_words_and_query_ids(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    session, _llm = _session(StubTools({"about_people": PEOPLE, "list_records": NOTES}), PEOPLE_CALL, LIST_CALL,
                             NOTES_LEAD + "\n\n{{table q2}}\n{{table q7}}\n{{list}}\n\n" + SOURCE)
    rec = session.ask("list the jobs and their notes")
    ct = rec.code_tables
    assert set(ct) == {"markers", "built", "rows", "dropped", "partly_typed", "items", "kept", "nudged", "chars", "cut"}
    assert all(isinstance(ct[k], int) for k in ("markers", "built", "rows", "partly_typed", "kept", "nudged", "chars",
                                                "cut"))
    assert set(ct["dropped"]) <= {"unknown", "failed", "no_rows", "empty", "unseen", "repeated", "typed", "malformed"}
    assert all(set(i) == {"query_id", "tool", "rows", "form"} and i["tool"] in T.TABLE_TOOLS for i in ct["items"])
    dumped = json.dumps(ct)   # no question, answer, cell or field text (tool names are fixed words)
    assert not any(w in dumped for w in ("J-1", "call back", "Zed", "jobs and their", "Notes", "Job__c", "{{"))
    assert ct["dropped"] == {"unknown": 1, "malformed": 1} and [i["query_id"] for i in ct["items"]] == ["q2"]


# ------------------------------------------------------------------------------------------------ pipeline.answer
def _completion(content=None, calls=(), finish=None):
    tcs = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish or ("tool_calls" if tcs
                                                                                           else "stop"))],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


FACT = "- `Job__c` holds one record per job opening."
MERGED = "Open jobs lead with 45 of the 61 (`Status__c`).\n\n{{table q1}}\n\n" + SOURCE


class Model:
    """``chat.completions.create`` of a fake OpenAI client, told apart by the system prompt: the route call (``route``),
    the names call (NONE), the record agent (one ``call``, then ``record``), the merge call (``merged``) and the
    knowledge steps (one search, then ``facts``). ``requests`` keeps every request (thread-safe)."""

    def __init__(self, record=JOB_REPLY, facts=FACT, merged=MERGED, call=("group_records", GROUP_CALL["args"]),
                 route="RECORDS"):
        self.record, self.facts, self.merged, self.call, self.route = record, facts, merged, call, route
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        with self._lock:
            self.requests.append(kwargs)
        system = str(kwargs["messages"][0].get("content") or "")
        n_tools = sum(1 for m in kwargs["messages"] if m.get("role") == "tool")
        if system == rprompts.ROUTE_SYSTEM:
            return _completion(self.route)
        if system == rprompts.NAMES_SYSTEM:
            return _completion("NONE")
        if system.startswith(rprompts.COMBINE_SYSTEM):
            return _completion(self.merged)
        if system.startswith("You answer questions about Salesforce RECORDS"):
            return _completion(calls=[self.call]) if n_tools == 0 else _completion(self.record)
        if kwargs.get("tools") and n_tools == 0:   # a knowledge step looks the field up first
            return _completion(calls=[("search_fields", {"query": "status"})])
        return _completion(self.facts)

    def merge_requests(self) -> list[dict]:
        return [r for r in self.requests if str(r["messages"][0].get("content")).startswith(rprompts.COMBINE_SYSTEM)]


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def _answer(model: Model, tools: StubTools | None = None, question: str = QUESTION, **kw):
    cfg = _cfg()
    session = RecordSession(tools or StubTools({"group_records": JOB_GROUP, "list_records": NOTES}), cfg,
                            llm=LLM(cfg, client=model))
    o = pipeline.answer(question, cfg, _registry(), None, LLM(cfg, client=model), 1, session, **kw)
    return o, pipeline.result_row(o, session)


def _no_marker(o, row):
    j = row["jsonl"]
    for text in (o.final, o.body, j["answer"], j["answer_body"], j["record_answer"], j["record_dropped_answer"] or "",
                 *j["record_nudges"]):
        assert "{{" not in text, text


def test_a_lean_merge_by_code_keeps_the_rows_verbatim_after_a_label_line(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    model = Model()
    o, row = _answer(model)
    assert o.mode == "lean" and o.merge_by == "code" and model.merge_requests() == []
    assert o.final == JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + merge.CODE_MERGE_LABEL + "\n" + FACT + "\n\n" + SOURCE
    assert o.final.count(JOB_ROWS) == 1 and o.tables_by == "code" and o.tables_switch == ""
    j = row["jsonl"]
    assert j["tables_by"] == "code" and "tables_switch" not in j and "merge" not in j["code_tables"]
    assert j["code_tables"]["built"] == 1 and j["code_tables"]["kept"] == 1
    _no_marker(o, row)


def test_a_cell_that_reads_like_a_plan_does_not_force_the_merge_call(monkeypatch):
    """Mutation-sensitive: plans_aloud reads the record answer with its rows as table lines; read with the rows, the
    cell "I will call back tomorrow" would send the answer to the merge call."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "I will call back tomorrow"}]}
    model = Model(record="1 job (`Job__c`) with its notes.\n\n{{table q1}}\n\n" + SOURCE,
                  call=("list_records", LIST_CALL["args"]))
    o, _row = _answer(model, StubTools({"list_records": res}), question="list the jobs and their notes")
    assert merge.plans_aloud(o.rec.answer) and "| J-1 | I will call back tomorrow |" in o.rec.answer
    assert o.merge_by == "code" and model.merge_requests() == []


def test_the_merge_by_code_reads_a_stored_line_break_in_a_cell(monkeypatch):
    """Mutation-sensitive (review of 2026-10-30, tests-1): code_merge audits with the code rows' own text as a source,
    so a cell stored as "Call back\\n15 Oct" (read as "n15" in the record facts) does not reject the merge and drop
    the knowledge part of a lean answer."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "Call back\n15 Oct"}, {"Name": "J-2", "Notes__c": "Open"}]}
    model = Model(record=NOTES_LEAD + "\n\n{{table q1}}\n\n" + SOURCE, call=("list_records", LIST_CALL["args"]))
    o, row = _answer(model, StubTools({"list_records": res}), question="list the jobs and their notes")
    assert "| J-1 | Call back 15 Oct |" in o.rec.answer and o.mode == "lean"
    assert o.merge_by == "code" and o.m.merged and o.m.rejected_numbers == [] and FACT in o.final
    assert "| J-1 | Call back 15 Oct |" in o.final and row["jsonl"]["code_tables"]["kept"] == 1


@pytest.mark.parametrize("how", ["SFA_MERGE=model", "full mode", "FIELD CHECK"])
def test_the_merge_call_gets_table_lines_and_its_note(monkeypatch, how):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "model" if how == "SFA_MERGE=model" else "code")
    tools = StubTools({"group_records": {**JOB_GROUP, "unsynced_fits": [
        {"field": "Stage__c", "fits": "status", "role": "stage"}]} if how == "FIELD CHECK" else JOB_GROUP})
    model = Model()
    o, row = _answer(model, tools, **({"lean_knowledge": False} if how == "full mode" else {}))
    (req,) = model.merge_requests()
    assert req["messages"][0]["content"] == rprompts.COMBINE_SYSTEM + T.COMBINE_TABLES_NOTE
    user = req["messages"][1]["content"]
    record_part = user.split("RECORD ANSWER:\n", 1)[1].split("\n\nRECORD FACTS:", 1)[0].split("\n\nFIELD CHECK:")[0]
    assert "{{table q1}}" in record_part and "- Open: 45" not in record_part and "Total" not in record_part
    assert o.kind == "merged" and o.m.merged and o.merge_by == "model"
    assert o.final.startswith("Open jobs lead with 45 of the 61 (`Status__c`).\n\n" + JOB_ROWS + "\n\n")
    assert o.final.count(JOB_ROWS) == 1   # mutation-sensitive (e): a lead quoting a row value is accepted
    assert row["jsonl"]["code_tables"]["merge"] == {"merged": 1, "lines": 1, "lost": 0, "retyped": 0, "repeated": 0,
                                                    "partly_retyped": 0}
    _no_marker(o, row)


def _merge_counts(lines=0, lost=0, retyped=0, repeated=0, partly_retyped=0) -> dict:
    return {"merged": 1, "lines": lines, "lost": lost, "retyped": retyped, "repeated": repeated,
            "partly_retyped": partly_retyped}


@pytest.mark.parametrize("merged,counts,body", [
    ("Open jobs lead.\n\n" + SOURCE, _merge_counts(lost=1), "Open jobs lead.\n\n" + JOB_ROWS + "\n\n" + SOURCE),
    ("Open jobs lead.\n{{table q1}}\n{{table q1}}\n" + SOURCE, _merge_counts(lines=1, repeated=1),
     "Open jobs lead.\n\n" + JOB_ROWS + "\n\n" + SOURCE),
    ("Open jobs lead.\n\n- Open: 45\n- Closed: 16\n\n{{table q1}}\n\n" + SOURCE, _merge_counts(retyped=1),
     "Open jobs lead.\n\n- Open: 45\n- Closed: 16\n\n" + SOURCE),
    # review of 2026-10-30: the line kept in a code fence comes out of it; a bullet naming both groups is no copy
    ("Open jobs lead.\n\n```\n{{table q1}}\n```\n\n" + SOURCE, _merge_counts(lines=1),
     "Open jobs lead.\n\n" + JOB_ROWS + "\n\n" + SOURCE),
    ("Open jobs lead.\n\n- Open (45) and Closed (16) jobs.\n\n{{table q1}}\n\n" + SOURCE, _merge_counts(lines=1),
     "Open jobs lead.\n\n- Open (45) and Closed (16) jobs.\n\n" + JOB_ROWS + "\n\n" + SOURCE),
])
def test_the_merged_text_gets_the_rows_back_once(monkeypatch, merged, counts, body):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "model")
    o, row = _answer(Model(merged=merged))
    assert o.m.merged and o.final == body and row["jsonl"]["code_tables"]["merge"] == counts
    _no_marker(o, row)


def test_rows_that_cannot_be_put_back_give_the_record_answer(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "model")
    monkeypatch.setattr(T, "restore", lambda text, blocks: None)
    o, row = _answer(Model())
    assert not o.m.merged and o.kind == "records" and o.final == o.rec.answer
    assert row["jsonl"]["merge_error"] == "the record answer's rows could not be put back"
    assert row["jsonl"]["code_tables"]["merge"] == {"merged": 0, "error": 1}


def test_the_counts_read_the_final_answer_and_a_rejected_merge_call_is_counted(monkeypatch):
    """Mutation-sensitive (review of 2026-10-30, behaviour-6): the JSONL's kept counts the blocks the FINAL answer
    holds whole (a merged text that typed every row itself holds none), left counts any "{{" it still holds, and a
    merge call that got table lines but whose text was not used says so (merged 0)."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "model")
    o, row = _answer(Model(merged="Open jobs lead.\n\n- Open: 45\n- Closed: 16\n\n{{table q1}}\n\n" + SOURCE))
    ct = row["jsonl"]["code_tables"]
    assert o.m.merged and JOB_ROWS not in o.final and o.rec.code_tables["kept"] == 1
    assert ct["kept"] == 0 and ct["left"] == 0 and ct["merge"]["merged"] == 1 and ct["merge"]["retyped"] == 1
    # a merged text with a number no result holds is rejected: the record answer (with its rows) is shown
    o, row = _answer(Model(merged="Open jobs lead with 999 of them.\n\n{{table q1}}\n\n" + SOURCE))
    ct = row["jsonl"]["code_tables"]
    assert not o.m.merged and o.kind == "records" and o.final.count(JOB_ROWS) == 1
    assert ct["merge"] == {"merged": 0} and ct["kept"] == 1 and ct["left"] == 0
    # a stored template value with braces in a cell stays, and left says the final answer holds one "{{"
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "Dear {{FirstName}}, welcome"}]}
    model = Model(record="1 job (`Job__c`) with its notes.\n\n{{table q1}}\n\n" + SOURCE,
                  call=("list_records", LIST_CALL["args"]))
    o, row = _answer(model, StubTools({"list_records": res}), question="list the jobs and their notes")
    assert "| J-1 | Dear {{FirstName}}, welcome |" in o.final and row["jsonl"]["code_tables"]["left"] == 1


def test_records_off_in_code_mode_writes_no_table_keys_and_leaves_the_answer(monkeypatch):
    """Mutation-sensitive (tests-4 a, b): with records off (no record session) code mode has nothing to do: no
    tables_by or code_tables in the JSONL, and the last-resort guard leaves the knowledge answer as it is."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    stray = "`Status__c` holds the stage of a job.\n\n{{table q2}}"
    model = Model(facts=stray)
    cfg = _cfg()
    o = pipeline.answer(QUESTION, cfg, _registry(), None, LLM(cfg, client=model), 1, None)
    row = pipeline.result_row(o, None)
    assert o.rec is None and o.tables_by is None and o.code_tables == {} and "{{table q2}}" in o.final
    assert not {"tables_by", "tables_switch", "code_tables"} & set(row["jsonl"])


@pytest.mark.parametrize("value", [None, "model"])
def test_model_mode_merge_request_and_jsonl_are_as_before(monkeypatch, value):
    _default_mode(monkeypatch, value)
    monkeypatch.setenv("SFA_MERGE", "model")
    typed = JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    model = Model(record=typed, merged=typed)
    o, row = _answer(model)
    (req,) = model.merge_requests()
    assert req["messages"][0]["content"] == rprompts.COMBINE_SYSTEM
    assert req["messages"][1]["content"] == rprompts.combine_user_message(
        QUESTION, FACT, typed, facts_json(o.rec.evidence, merge.MERGE_FACTS_CHARS), [])
    assert o.tables_by is None and o.code_tables == {} and o.tables_switch == ""
    # none of the new keys (canon hides them from P7, so their absence is pinned here)
    assert not {"tables_by", "tables_switch", "code_tables"} & set(row["jsonl"])
    assert o.rec.code_tables == {} and o.rec.table_blocks == {}


def test_an_invalid_switch_is_the_default_and_the_jsonl_says_so(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "zorvex")
    monkeypatch.setenv("SFA_MERGE", "code")
    o, row = _answer(Model(record=JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE))
    j = row["jsonl"]
    assert j["tables_switch"] == "SFA_TABLES must be model or code" and "zorvex" not in j["tables_switch"]
    assert QUESTION not in j["tables_switch"]
    if C.TABLES_DEFAULT == "model":
        assert "tables_by" not in j and "code_tables" not in j


def test_the_last_resort_guard_removes_a_stray_table_line_in_code_mode_only(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")
    stray = "`Status__c` holds the stage of a job.\n\n{{table q2}}"
    for value, kept in (("code", False), ("model", True)):
        monkeypatch.setenv("SFA_TABLES", value)
        o, _row = _answer(Model(route="NO_RECORDS", facts=stray))   # a design question: the knowledge answer is shown
        assert o.kind == "knowledge" and ("{{table q2}}" in o.final) is kept and ("{{table q2}}" in o.body) is kept
        assert o.final.startswith("`Status__c` holds the stage of a job.")


# ---------------------------------------------- the step-2 fixes of 2026-10-30 in code mode (docs/runbook.md)
class _TwoCalls(Model):
    """The record agent calls about_people, then list_records, then replies ``record``."""

    def _create(self, **kwargs):
        system = str(kwargs["messages"][0].get("content") or "")
        n_tools = sum(1 for m in kwargs["messages"] if m.get("role") == "tool")
        if system.startswith("You answer questions about Salesforce RECORDS") and n_tools < 2:
            with self._lock:
                self.requests.append(kwargs)
            step = PEOPLE_CALL if n_tools == 0 else LIST_CALL
            return _completion(calls=[(step["name"], step["args"])])
        return super()._create(**kwargs)


def test_f1_a_code_row_cell_that_reads_like_a_code_stays_in_an_agent_reply(monkeypatch):
    """F1 drops the P/R codes where code writes the people answer only: in the agent's own reply a code row whose cell
    is "P1" (a code about_people maps) stays, its block is held whole and counted as kept."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "P1"}, {"Name": "J-2", "Notes__c": "Open"}]}
    model = _TwoCalls(record=NOTES_LEAD + "\n\n{{table q2}}\n\n" + SOURCE)
    o, row = _answer(model, StubTools({"about_people": PEOPLE, "list_records": res}),
                     question="list the jobs and their notes")
    block = next(iter(o.rec.table_blocks.values()))
    assert not o.rec.code_answer and "| J-1 | P1 |" in block and T.holds(o.final, block)
    assert row["jsonl"]["code_tables"]["kept"] == 1 and o.merge_by == "code"


def test_f5_a_cell_that_reads_like_thinking_aloud_does_not_force_the_merge_call(monkeypatch):
    """F5 in code mode: plans_aloud reads the record answer with its rows as table lines, so a cell "Note: It seems
    fine" (thinking aloud after a colon) sends no answer to the merge call, and the table line itself is flagged by
    neither the tool-talk test nor the Hinglish test. Review of 2026-10-30: plans_aloud reads thinking aloud in prose
    lines only, so the expanded row (a table row, as in model mode) does not route either."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    res = {**NOTES, "rows": [{"Name": "J-1", "Notes__c": "Note: It seems fine"}, {"Name": "J-2", "Notes__c": "Open"}]}
    model = Model(record=NOTES_LEAD + "\n\n{{table q1}}\n\n" + SOURCE, call=("list_records", LIST_CALL["args"]))
    o, _row = _answer(model, StubTools({"list_records": res}), question="list the jobs and their notes")
    assert "| J-1 | Note: It seems fine |" in o.rec.answer and not merge.plans_aloud(o.rec.answer)
    assert o.merge_by == "code" and model.merge_requests() == [] and FACT in o.final
    marked = T.marked(o.rec.answer, o.rec.table_blocks)
    assert "{{table q1}}" in marked and not merge.plans_aloud(marked)
    assert not merge._hinglish(marked) and not merge._hinglish(o.rec.answer, o.rec.table_blocks)


def test_f8_the_counted_line_goes_after_the_code_rows(monkeypatch):
    """F8 in code mode: the record answer names no grouped field, so the counted line follows the group block (a blank
    line between), the block is held whole and counted as kept, and the bullets keep their label line."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    lead = "61 jobs (`Job__c`) by status."
    o, row = _answer(Model(record=lead + "\n\n{{table q1}}\n\n" + SOURCE))
    line = "Counted: `Job__c` records grouped by `Status__c`."
    assert o.final == (lead + "\n\n" + JOB_ROWS + "\n\n" + line + "\n\n" + merge.CODE_MERGE_LABEL + "\n" + FACT
                       + "\n\n" + SOURCE)
    assert T.holds(o.final, JOB_ROWS) and row["jsonl"]["code_tables"]["kept"] == 1 and o.kind == "merged"


def test_f11_a_hinglish_lead_over_english_code_rows_gets_nothing(monkeypatch):
    """F11 in code mode: the Hinglish test reads the record answer's prose with its code rows removed (their labels are
    English), so a Hinglish lead over a block keeps the record answer as it is: no bullet, no counted line."""
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "code")
    lead = "Status ke hisaab se 61 jobs hain, sab `Job__c` ke records."
    block = "Status:\n- Open: 45\n- Closed: 16\n\nShowing the open and closed jobs of the whole copy."
    answer = lead + "\n\n" + block + "\n\n" + SOURCE
    assert merge._prose_words(answer, {"{{table q1}}": block}) == merge._prose_words(lead)
    assert merge._hinglish(answer, {"{{table q1}}": block})
    o, row = _answer(Model(record=lead + "\n\n{{table q1}}\n\n" + SOURCE))
    assert o.kind == "records" and o.final == o.rec.answer and JOB_ROWS in o.final and FACT not in o.final
    assert o.merge_by == "code" and not o.m.merged and o.m.error is None and row["jsonl"]["code_tables"]["kept"] == 1


def test_the_trace_line_says_what_code_wrote():
    agent = AgentResult(qid="rec-001", question=QUESTION, answer="a")
    rec = RecordAnswer(question=QUESTION, answer="a", no_records=False, used_data=True, agent=agent,
                       code_tables={"markers": 3, "built": 1, "rows": 6, "dropped": {"unknown": 1, "typed": 1},
                                    "items": [], "kept": 1, "nudged": 1})
    lines = record_trace_lines(rec)
    assert ("   code rows: 1 of 3 table line(s) filled (6 rows); dropped: unknown 1, typed 1; the first draft was "
            "asked to fix its table lines") in lines
    for ct in ({}, {"markers": 0, "built": 0, "rows": 0, "dropped": {}, "items": [], "kept": 0, "nudged": 0}):
        assert not any("code rows" in ln for ln in record_trace_lines(dataclasses.replace(rec, code_tables=ct)))


# ------------------------------------------------------------------------------------------------ the page's render
def _tree(s: str) -> list:
    """render_check.js's element tree ("tag(child,child)", text nodes as JSON strings) as (tag, children) and str."""
    dec = json.JSONDecoder()

    def nodes(i: int, closing: bool) -> tuple[list, int]:
        out: list = []
        while i < len(s) and not (closing and s[i] == ")"):
            if s[i] == ",":
                i += 1
            elif s[i] == '"':
                value, i = dec.raw_decode(s, i)
                out.append(value)
            else:
                j = s.index("(", i)
                tag = s[i:j]
                kids, i = nodes(j + 1, True)
                out.append((tag, kids))
                i += 1   # its ")"
        return out, i

    return nodes(0, False)[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_renders_each_block_as_a_table_or_a_list(tools):
    ev = _ev_list(tools)
    x = T.expand("10 client interviews (`Interview__c`).\n\n{{table q1}}\n\nDelta Person's, by status:\n{{table q6}}\n"
                 "{{table q7}}\n\n" + SOURCE, ev, T.column_labels(tools.catalog))
    two = dataclasses.replace(ev[5], model_result={**ev[5].model_result, "breakdown": {
        "Status__c": {"Completed": 2}, "Interview_Type__c": {"Mock": 2, "Intake": 1}}})
    cases = {"answer": x.text.rsplit("\n\n", 1)[0] + "\n\nLookup used: `Interview__c.Recruiter__c`.\n" + SOURCE,
             "jobs": _block(_evidence("q1", "list_records", JOBS)), "two": _block(two, T.column_labels(tools.catalog)),
             **{name: _block(e) for name, e in zip(("split", "trend", "avg", "compare"), ev[1:5])}}
    proc = subprocess.run(["node", str(ROOT / "tests" / "render_check.js")], capture_output=True, text=True,
                          input=json.dumps(cases), timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    for name in cases:
        assert not any("{{" in t for t in out[name]["texts"]), name
    nodes = _tree(out["answer"]["tree"])
    assert [n[0] if isinstance(n, tuple) else n for n in nodes] == ["p", "ul", "p", "ul", "div", "p", "p"]
    assert [len(n[1]) for n in (nodes[1], nodes[3])] == [len(GROUP_LINES), 2]   # one li per line, Total included
    (table,) = nodes[4][1]
    (thead, tbody) = table[1]
    width = len(thead[1][0][1])
    assert width == 7 and len(tbody[1]) == 4 and all(len(tr[1]) == width for tr in tbody[1])
    assert nodes[5] == ("p", ["Showing 4 of 10 records."])
    assert nodes[6][0] == "p" and nodes[6][1][0] == "Lookup used: " and nodes[6][1][-1] == SOURCE   # its own paragraph
    jobs = _tree(out["jobs"]["tree"])
    (table,) = jobs[0][1]
    assert [len(tr[1]) for tr in table[1][1][1]] == [5, 5] and "a¦b next line" in out["jobs"]["texts"]
    assert [n[0] for n in _tree(out["two"]["tree"])] == ["p", "ul", "p", "ul"]
    for name, lines in (("split", SPLIT_LINES), ("trend", TREND_LINES), ("avg", 3), ("compare", 2)):
        (ul,) = _tree(out[name]["tree"])   # every other shape: one list, one item per line
        assert ul[0] == "ul" and len(ul[1]) == (lines if isinstance(lines, int) else len(lines)), name
        assert all(li[0] == "li" for li in ul[1]), name


# ------------------------------------------------------------- step-3 fixes T1 and T5: counters, a kept Total line
def test_t5_kinds_and_cut_through_a_session(monkeypatch):
    from sf_assistant import prompts as P
    from sf_assistant.agent import nudges as N
    import re as _re
    relation = _re.sub(r"<<[A-Z0-9_]+>>", "x", P.text("records_relation_word_message").strip())
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setattr(RecordSession, "check_draft", lambda self, *a, **k: relation)
    bad = JOB_LEAD + "\n\n{{table q1}}\n{{table q9}}\n\n" + SOURCE
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, bad, JOB_REPLY)
    rec = session.ask(QUESTION)
    assert rec.nudge_kinds == ["records_relation_word_message", "records_tables_marker_message"]
    monkeypatch.undo()
    # model mode: the same kinds field, no code_tables
    monkeypatch.setenv("SFA_TABLES", "model")
    monkeypatch.setattr(RecordSession, "check_draft", lambda self, *a, **k: relation)
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, "61 jobs.\n\n" + SOURCE, "61 jobs.\n\n" + SOURCE)
    rec = session.ask(QUESTION)
    assert rec.nudge_kinds == ["records_relation_word_message"] and rec.code_tables == {}
    monkeypatch.undo()
    # no nudge
    monkeypatch.setenv("SFA_TABLES", "model")
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, JOB_LEAD + "\n\n- Open: 45\n- Closed: 16\n\n" + SOURCE)
    rec = session.ask(QUESTION)
    assert rec.nudge_kinds == []
    # cut: a table line strip_reasoning removes with the working text before the marker
    monkeypatch.setenv("SFA_TABLES", "code")
    session, llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, "{{table q1}}\n**Answer:** 61 jobs.\n- note\n" + SOURCE)
    rec = session.ask(QUESTION)
    assert rec.code_tables["cut"] == 1



TREND_RES = {"object": "Job__c", "grain": "month", "buckets": [
    {"bucket": "2031-01", "value": 12, "by": {"Web": 7, "Store": 5}},
    {"bucket": "2031-02", "value": 9, "by": {"Web": 4, "Store": 5}}], "total": 21, "split_totals": {"Web": 11, "Store": 10}, "minutes": 30}
TREND_COPY = "- **January 2031**: 12 jobs (Web 7, Store 5)\n- **Feb 2031**: 9 jobs (Web 4, Store 5)"
TREND_TOTAL = "- Total: 21 (Web: 11, Store: 10)"
TREND_ANSWER = "21 jobs (`Job__c`).\n\n" + TREND_COPY + "\n\n" + TREND_TOTAL + "\n\n" + SOURCE


@pytest.mark.parametrize("by", ["code", "model"])
def test_t1_a_kept_total_line_goes_through_both_merges(monkeypatch, by):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", by)
    reply = "21 jobs (`Job__c`).\n\n" + TREND_COPY + "\n\n{{table q1}}\n\n" + SOURCE
    merged = "Jobs by month.\n\n" + TREND_COPY + "\n\n{{table q1}}\n\n" + SOURCE
    model = Model(record=reply, merged=merged, call=("trend_records", {"object": "Job__c", "grain": "month"}))
    o, row = _answer(model, StubTools({"trend_records": TREND_RES}))
    assert o.rec.answer == TREND_ANSWER and o.rec.table_blocks == {"{{table q1}}": TREND_TOTAL}
    assert o.rec.code_tables["built"] == 0 and o.rec.code_tables["dropped"] == {"typed": 1}
    assert o.rec.code_tables["kept"] == 1 and o.rec.code_tables["chars"] == len(TREND_TOTAL)
    assert o.final.count(TREND_TOTAL) == 1 and row["jsonl"]["record_nudge_kinds"] == []
    _no_marker(o, row)
    if by == "model":
        assert "{{table q1}}" in model.merge_requests()[0]["messages"][1]["content"]
        assert row["jsonl"]["code_tables"]["merge"] == _merge_counts(lines=1)


def test_t5_model_mode_row_has_only_the_new_key(monkeypatch):
    _default_mode(monkeypatch, "model")
    monkeypatch.setenv("SFA_MERGE", "model")
    typed = JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    o, row = _answer(Model(record=typed, merged=typed))
    assert row["jsonl"]["record_nudge_kinds"] == [] and o.rec.nudge_kinds == []
    assert not {"tables_by", "tables_switch", "code_tables"} & set(row["jsonl"])


def _filled(pid):
    from sf_assistant import prompts as P
    import re as _re
    return _re.sub(r"<<[A-Z0-9_]+>>", "x", P.text(pid).strip())


@pytest.mark.parametrize("merge_by", ["code", "model"])
@pytest.mark.parametrize("mode", ["code", "model"])
def test_t5_kinds_reach_the_jsonl(monkeypatch, mode, merge_by):
    monkeypatch.setenv("SFA_TABLES", mode)
    monkeypatch.setenv("SFA_MERGE", merge_by)
    relation = _filled("records_relation_word_message")
    monkeypatch.setattr(RecordSession, "check_draft", lambda self, *a, **k: relation)
    typed = JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    o, row = _answer(Model(record=typed, merged=typed))
    assert o.rec.nudge_kinds == row["jsonl"]["record_nudge_kinds"] == ["records_relation_word_message"]


def test_t5_kinds_of_two_checks_stay_in_order(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "model")
    relation, overlap = _filled("records_relation_word_message"), _filled("records_overlap_message")
    monkeypatch.setattr(RecordSession, "check_draft", lambda self, *a, **k: relation)
    monkeypatch.setattr(RecordSession, "recheck_draft", lambda self, *a, **k: overlap)
    typed = JOB_LEAD + "\n\n" + JOB_ROWS + "\n\n" + SOURCE
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, typed)
    assert session.ask(QUESTION).nudge_kinds == ["records_relation_word_message", "records_overlap_message"]


@pytest.mark.parametrize("by", ["code", "model"])
def test_t3_label_through_both_merges(monkeypatch, by):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", by)
    reply = "{{table q1}}\n\n- Counted: `Job__c` by `Status__c`.\n\n" + SOURCE
    o, row = _answer(Model(record=reply, merged=reply), StubTools({"group_records": JOB_GROUP}))
    head, rest = o.rec.answer.split("\n", 1)
    assert head.startswith("By ") and head.endswith(":") and rest.startswith(JOB_ROWS)
    assert o.final.count(JOB_ROWS) == 1
    _no_marker(o, row)


def test_a_merge_label_above_the_table_line_shows_once(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", "model")
    reply = "{{table q1}}\n- Counted: `Job__c` by `Status__c`.\n" + SOURCE
    o, row = _answer(Model(record=reply, merged=None), StubTools({"group_records": JOB_GROUP}))
    head = o.rec.answer.split("\n", 1)[0]
    merged = "61 jobs in all.\n" + head + "\n{{table q1}}\n- Counted: `Job__c` by `Status__c`.\n" + SOURCE
    o, row = _answer(Model(record=reply, merged=merged), StubTools({"group_records": JOB_GROUP}))
    assert o.merge_by == "model" and o.final.count(head) == 1 and o.final.count(JOB_ROWS) == 1
    assert T.holds(o.final, o.rec.table_blocks["{{table q1}}"]) and row["jsonl"]["code_tables"]["kept"] == 1


TREND_PLAIN = {"object": "Job__c", "grain": "month", "buckets": [{"bucket": "2031-01", "value": 12},
                                                                 {"bucket": "2031-02", "value": 9}], "total": 21,
               "source": "preprod sandbox copy; Job__c synced 2026-10-13 14:30 UTC"}
TREND_CALL = {"name": "trend_records", "args": {"object": "Job__c", "grain": "month"}}
PLAIN_ROWS = "- 2031-01: 12\n- 2031-02: 9\n- Total: 21"


def _typed_copy(line):
    return "There were 21 jobs (`Job__c`).\n\n{{table q1}}\n\n" + line + "\n- Feb 2031: 9\n\n" + SOURCE


@pytest.mark.parametrize("line", ["- January 2031: 12 (up 33% on December)", "- January 2031: 12, last one on 2031-01-27"])
def test_rows_come_back_when_the_audit_cuts_a_line_of_the_typed_copy(monkeypatch, line):
    monkeypatch.setenv("SFA_TABLES", "code")
    reply = _typed_copy(line)
    # the model repeats its draft after the number message (the second draft has no number check)
    session, _llm = _session(StubTools({"trend_records": TREND_PLAIN}), TREND_CALL, reply, reply)
    rec = session.ask("how many jobs per month?")
    assert rec.dropped_numbers and line not in rec.answer and "January 2031" not in rec.answer
    assert PLAIN_ROWS in rec.answer and rec.answer.endswith("\n\n" + SOURCE) and rec.unsupported_numbers == []
    assert rec.table_blocks == {"{{table q1}}": PLAIN_ROWS}
    counts = rec.code_tables
    assert counts["dropped"] == {"typed": 1} and counts["put_back"] == 1 and counts["built"] == 0
    assert counts["kept"] == 1 and counts["chars"] == len(PLAIN_ROWS)
    assert any(ln.endswith("dropped: typed 1; put back: 1") for ln in record_trace_lines(rec))


@pytest.mark.parametrize("steps,kw", [((JOB_LEAD + "\n\n- Open: 45 (74%)\n- Closed: 16 (26%)\n\n{{table q1}}\n\n" + SOURCE,) * 2, {}),
                                      ((GROUP_CALL,), {"max_steps": 1, "forced": JOB_LEAD + "\n\n- Open: 45 (74%)\n"
                                                       "- Closed: 16 (26%)\n\n{{table q1}}\n\n" + SOURCE})],
                         ids=["second-draft", "forced"])
def test_rows_come_back_when_the_audit_cuts_every_typed_line(monkeypatch, steps, kw):
    monkeypatch.setenv("SFA_TABLES", "code")
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, *steps, **kw)
    rec = session.ask(QUESTION)
    assert rec.dropped_numbers == ["74", "26"] and "%" not in rec.answer
    assert JOB_ROWS in rec.answer and rec.answer.count("- Open: 45") == 1 and rec.code_tables["put_back"] == 1


def test_a_copy_the_checks_leave_alone_stays_dropped(monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    clean = JOB_LEAD + "\n\n- Open: 45\n- Closed: 16\n\n{{table q1}}\n\n" + SOURCE
    session, _llm = _session(StubTools({"group_records": JOB_GROUP}), GROUP_CALL, clean)
    rec = session.ask(QUESTION)
    assert rec.code_tables["dropped"] == {"typed": 1} and "put_back" not in rec.code_tables
    assert rec.table_blocks == {} and rec.answer == JOB_LEAD + "\n\n- Open: 45\n- Closed: 16\n\n" + SOURCE


@pytest.mark.parametrize("by", ["code", "model"])
def test_rows_put_back_go_through_both_merges(monkeypatch, by):
    monkeypatch.setenv("SFA_TABLES", "code")
    monkeypatch.setenv("SFA_MERGE", by)
    reply = _typed_copy("- January 2031: 12 (up 33% on December)")
    merged = "There were 21 jobs (`Job__c`).\n\n- Feb 2031: 9\n\n{{table q1}}\n\n" + SOURCE
    model = Model(record=reply, merged=merged, call=("trend_records", {"object": "Job__c", "grain": "month"}))
    o, row = _answer(model, StubTools({"trend_records": TREND_PLAIN}))
    assert PLAIN_ROWS in o.rec.answer and o.final.count(PLAIN_ROWS) == 1
    assert row["jsonl"]["code_tables"]["put_back"] == 1 and row["jsonl"]["code_tables"]["kept"] == 1
    _no_marker(o, row)
