"""Long record lists come back complete (development A of 2026-11-27, the owner's choices): a question that asks to list
records gets every list_records row written by code (``agent.display.asks_list``, ``agent.tables.list_rows``,
``prompts/records_list_note.md``), list_records returns up to 200 rows per call (``LIMITS.list_max_rows``) while the
model reads its result cut to ``record_result_chars`` (``QueryEvidence.full_rows`` keeps the rest), the answer caps are
2,500 tokens (under the 120 s call timeout at about 24 tokens/s), and an answer whose model call stopped at its token limit gets the cut line
(``prompts/answer_cut_line.md``).

Offline and model-free: stub record tools and scripted fakes of the model, the fake warehouse of
``tests/warehouse_fixture.py``; every name, team and value is made up.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import sf_assistant.pipeline as pipeline  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant import prompts as P  # noqa: E402
from sf_assistant.agent import display as D  # noqa: E402
from sf_assistant.agent import merge  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent import tables as T  # noqa: E402
from sf_assistant.agent import timeline  # noqa: E402
from sf_assistant.agent.knowledge_agent import AgentResult, answer_question  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.agent.question import Question  # noqa: E402
from sf_assistant.agent.record_session import RecordSession  # noqa: E402
from sf_assistant.config import load_config  # noqa: E402
from sf_assistant.engine.contact import EMAIL_SHOWN  # noqa: E402
from sf_assistant.engine.types import QueryEvidence, RecordsStatus  # noqa: E402
from sf_assistant.knowledge.search import KnowledgeIndex  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge  # noqa: E402
from sf_assistant.tools.knowledge_tools import Tool, ToolRegistry  # noqa: E402
from sf_assistant.tools.registry import SCHEMAS  # noqa: E402
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
OBJ = "Staff__c"
TEAM = "Team Zeta"
QUESTION = "list out all the staff of team zeta"
#: 103 made-up people; one note cell holds a made-up e-mail address (the contact rule on code's rows)
PEOPLE = [f"Person {i:03d}" for i in range(1, 104)]
COLUMNS = ["Name", "Team__c (name)", "Notes__c"]


def _row(i: int) -> dict:
    note = "write to person007@example.test" if i == 7 else f"desk {i}"
    return {"Name": PEOPLE[i - 1], "Team__c (name)": TEAM, "Notes__c": note}


ROWS = [_row(i) for i in range(1, 104)]


def _cfg(steps: int = 12):
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=steps)


@pytest.fixture(autouse=True)
def _closed_timeline():
    timeline.end()
    yield
    timeline.end()


def _ev(qid: str, tool: str, result: dict, full: list | None = None) -> QueryEvidence:
    res = {"query_id": qid, "object": OBJ, **result}
    ev = QueryEvidence(query_id=qid, tool=tool, kind="data", object=OBJ, arguments={}, ok=True, error=None,
                       applied={}, notes=[], statements=[], tables=[OBJ], synced_at={OBJ: "2026-10-13 14:30 UTC"},
                       summary="", model_result=res, elapsed_ms=1.0)
    if full is not None:
        ev.full_rows = full
    return ev


def _list_result(rows: list, total: int, shown: int | None = None) -> tuple[dict, list | None]:
    """A list_records result as the record tools give it: ``shown`` rows for the model (the result cap's cut, with its
    note), every row in ``full_rows`` when it cut some."""
    res = {"columns": COLUMNS, "rows": rows, "returned": len(rows), "total_count": total,
           "truncated": total > len(rows), "order": "Name asc",
           "source": f"preprod sandbox copy; {OBJ} synced 2026-10-13 14:30 UTC"}
    if shown is None or shown >= len(rows):
        return res, None
    cut = {**res, "rows": rows[:shown], "shown": shown, "notes": [f"result trimmed to fit: showing {shown} of "
                                                                   f"{len(rows)}"]}
    return cut, list(rows)


class Tools:
    """The record tools surface RecordSession uses: ``results`` maps a tool name to its results, one per call (the
    last repeating), as ``(model_result, full_rows)``; ``sent`` keeps every call's arguments and ``limits`` the
    ``list_limit`` each call saw (``RecordTools.list_limit``: a list question's page for a call without a limit)."""

    def __init__(self, results: dict):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {}) for n in results}
        self.results = results
        self._evidence: list[QueryEvidence] = []
        self.sent: list[tuple[str, dict]] = []
        self.limits: list = []
        self.list_limit = None

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        self.sent.append((name, dict(arguments or {})))
        self.limits.append(self.list_limit)
        k = sum(1 for n, _a in self.sent if n == name)
        res, full = self.results[name][min(k, len(self.results[name])) - 1]
        ev = _ev(f"q{len(self._evidence) + 1}", name, res, full)
        ev.arguments = dict(arguments or {})
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def begin_question(self, question="", people=None, values=None):
        self._evidence, self.sent, self.limits, self.list_limit = [], [], [], None

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question, missing=()):
        return ""

    def status(self):
        return STATUS

    def close(self):
        pass


class Script:
    """A scripted model: the route call says RECORDS, the names call NONE; the record agent's calls get ``steps`` in
    order (a dict: one tool call; a str: a reply; the last repeating) and its final reply ends with ``finish``."""

    def __init__(self, *steps, finish: str = "stop"):
        self.steps, self.finish, self.calls = list(steps), finish, []

    def chat(self, messages, tools=None, max_tokens=None, **_):
        system = messages[0].get("content")
        if system == rprompts.ROUTE_SYSTEM:
            return {"content": "RECORDS", "tool_calls": [], "usage": {}}
        if system == rprompts.NAMES_SYSTEM:
            return {"content": "NONE", "tool_calls": [], "usage": {}}
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        step = self.steps[min(len(self.calls) - 1, len(self.steps) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": {}, "finish_reason":
                    "tool_calls"}
        return {"content": step, "tool_calls": [], "usage": {}, "finish_reason": self.finish}


LIST_CALL = {"name": "list_records", "args": {"object": OBJ, "fields": ["Name", "Team__c", "Notes__c"],
                                              "filters": [{"field": "Team__c", "op": "eq", "value": TEAM}]}}
LEAD = f"There are 103 staff in {TEAM} (`{OBJ}` with `Team__c` = {TEAM})."
TABLE_HEAD = ["| Name | Team | Notes |", "|---|---|---|"]


def _table_rows(answer: str) -> list[str]:
    return [ln for ln in answer.split("\n") if ln.startswith("| Person ")]


def _session(tools, *steps, finish: str = "stop"):
    llm = Script(*steps, finish=finish)
    return RecordSession(tools, _cfg(), llm=llm, max_steps=12), llm


# ------------------------------------------------------------------------------------------------ the limits
def test_the_caps_and_the_page_size():
    L = C.LIMITS
    assert (L.model_max_tokens, L.merge_max_tokens, L.list_max_rows) == (2500, 2500, 200)
    assert C.Config().max_tokens == 2500 and merge.MERGE_MAX_TOKENS == 2500
    assert L.model_max_tokens / 24 < L.model_timeout_s   # a full reply at about 24 tokens/s fits one call attempt
    assert L.record_result_chars == 12_000   # the result the model reads stays capped
    limit = SCHEMAS["list_records"][1]["properties"]["limit"]   # (description, input schema)
    assert (limit["minimum"], limit["maximum"]) == (1, 200) and "at most 200" in limit["description"]
    assert T.TABLE_MAX_ROWS >= L.list_max_rows   # code never cuts a page it was given
    assert "list_max_rows" in C.IMPORT_BOUND_LIMITS
    assert "limit 200" in T.RECORDS_LIST_NOTE   # the note names the page the limits allow


@pytest.fixture(scope="module")
def fixture_tools(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        mp.setenv("SFA_RESOLVER", "off")
        t = open_fixture(tmp_path_factory.mktemp("long_lists"))
    yield t
    t.close()


def test_list_records_takes_200_rows_and_code_keeps_the_rows_the_result_cap_cut(fixture_tools, monkeypatch):
    import sf_assistant.tools.record_base as B

    tools = fixture_tools
    tools.begin_question("a made-up question")
    out = tools.call("list_records", {"object": "Interview__c", "limit": 500})
    assert out["ok"] and any("clamped to 200" in n for n in out["result"]["notes"])
    out = tools.call("list_records", {"object": "Interview__c", "limit": 200})
    assert out["ok"] and not any("clamped" in n for n in out["result"].get("notes") or [])
    whole = out["result"]["rows"]
    assert len(whole) == out["result"]["returned"] == out["result"]["total_count"] == 10
    assert tools.evidence[-1].full_rows is None   # nothing was cut: no second copy
    monkeypatch.setattr(B, "MAX_RESULT_CHARS", 1500)   # read at call time: the model reads fewer rows
    out = tools.call("list_records", {"object": "Interview__c", "limit": 200})
    seen = out["result"]["rows"]
    assert out["ok"] and len(json.dumps(out["result"])) <= 1500 and 0 < len(seen) < 10
    ev = tools.evidence[-1]
    assert ev.model_result["rows"] == seen and ev.full_rows == whole   # the same cells, every row
    block = T.render(ev, T.column_labels(tools.catalog))[0].split("\n")
    assert len(block) == 2 + 10 and "Showing" not in "\n".join(block)
    # a list question's page: a call without a limit returns list_limit rows, its arguments stay the model's; a limit
    # the model set stays; begin_question resets it
    monkeypatch.setattr(B, "MAX_RESULT_CHARS", 12_000)
    assert tools.call("list_records", {"object": "Interview__c"})["result"]["returned"] == 10
    tools.list_limit = 4
    assert tools.call("list_records", {"object": "Interview__c"})["result"]["returned"] == 4
    assert tools.evidence[-1].arguments == {"object": "Interview__c"}
    assert tools.call("list_records", {"object": "Interview__c", "limit": 2})["result"]["returned"] == 2
    tools.begin_question("another made-up question")
    assert tools.list_limit is None


# ------------------------------------------------------------------------------------------------ list questions
@pytest.mark.parametrize("q", ["list out all the recruiters of marketing team", "show me all interviews last month",
                               "give me a list of candidates", "can you list the jobs", "sabhi recruiters dikhao",
                               "name all the trainers", "who are all the trainers?"])
def test_list_asks(q):
    assert D.asks_list(q)


@pytest.mark.parametrize("q", ["how many recruiters in marketing", "how many interviews in all", "list view of jobs",
                               "what is a list view", "checklist items", "which candidates are placed",
                               "how many jobs per status?", 'what does "list all" mean', ""])
def test_no_list_asks(q):
    assert not D.asks_list(q)


def test_the_table_flag_alone_is_no_list_ask_and_a_display_only_ask_is_none():
    for q in ("interviews by month table", "show me a table of interviews by status"):   # grouped tables
        d = D.split(q)
        assert d.table and not D.asks_list(d.search, d), q
    d = D.split("list all interviews as a table")
    assert d.table and D.asks_list(d.search, d)
    assert not D.asks_list("list all jobs", D.Display(search="", empty=True))


# ------------------------------------------------------------------------------------------------ RecordSession
def test_the_recruiters_case_gives_all_103_rows_once_from_one_call(monkeypatch):
    """The live failure's shape: 103 people. The model reads 60 rows (the result cap), writes the lead and one table
    line; code writes all 103 rows, the list call gets 200 rows though the model set no limit."""
    monkeypatch.delenv("SFA_TABLES", raising=False)
    res, full = _list_result(ROWS, 103, shown=60)
    tools = Tools({"list_records": [(res, full)]})
    reply = LEAD + "\n\n{{table q1}}\n\n- Listed: `Staff__c` with `Team__c` = " + TEAM + ".\n\n" + SOURCE
    session, llm = _session(tools, LIST_CALL, reply)
    rec = session.ask(QUESTION)
    system = llm.calls[0]["messages"][0]["content"]
    assert system == rprompts.render_records_system(STATUS) + T.RECORDS_LIST_NOTE   # no code-tables note
    assert tools.sent == [("list_records", LIST_CALL["args"])] and tools.limits == [200]   # the model's arguments
    rows = _table_rows(rec.answer)
    assert len(rows) == 103 and len(set(rows)) == 103
    assert [r.split(" | ")[0][2:] for r in rows] == PEOPLE   # every person, in the stored order, once
    assert rec.answer.startswith(LEAD + "\n\n" + "\n".join(TABLE_HEAD) + "\n| Person 001 |")
    assert rec.answer.rstrip().endswith(SOURCE) and "{{" not in rec.answer
    assert "Showing" not in rec.answer and rec.list_ask and not rec.cut_off
    assert rec.table_blocks and rec.code_tables["rows"] == 103 and rec.code_tables["kept"] == 1
    assert len(llm.calls) == 2   # one list call, one answer: no paging rounds
    # contact details only when asked: the made-up e-mail in a note cell is hidden, its row stays
    assert "person007@example.test" not in rec.answer and f"| Person 007 | {TEAM} | write to {EMAIL_SHOWN} |" in \
        rec.answer
    assert rec.dropped_numbers == [] and rec.unsupported_numbers == []


def test_an_asked_contact_detail_stays():
    res, full = _list_result(ROWS, 103, shown=60)
    reply = LEAD + "\n\n{{table q1}}\n\n" + SOURCE
    session, _llm = _session(Tools({"list_records": [(res, full)]}), LIST_CALL, reply)
    rec = session.ask("list out all the staff of team zeta with their email addresses")
    assert "| Person 007 | Team Zeta | write to person007@example.test |" in rec.answer


def test_three_pages_without_a_table_line_and_a_cut_typed_list_give_all_103_rows_once():
    """The model paged three times (50, 50 and 3 rows), typed 45 names and stopped at its token limit in the middle
    of a name: code joins the pages, drops the typed names and the broken line, and the list is whole, so no cut
    line is due."""
    pages = [(_list_result(ROWS[:50], 103)[0], None), (_list_result(ROWS[50:100], 103)[0], None),
             (_list_result(ROWS[100:], 103)[0], None)]
    typed = "\n".join(f"{i}. {p}" for i, p in enumerate(PEOPLE[:45], 1)) + "\n46. Person 04"
    reply = f"Here are the 103 staff of {TEAM}:\n\n" + typed
    session, _llm = _session(Tools({"list_records": pages}), LIST_CALL, LIST_CALL, LIST_CALL, reply, finish="length")
    rec = session.ask(QUESTION)
    rows = _table_rows(rec.answer)
    assert len(rows) == 103 and [r.split(" | ")[0][2:] for r in rows] == PEOPLE
    assert not any(ln.startswith(("1. ", "45. ", "46. ")) for ln in rec.answer.split("\n"))
    assert rec.answer.startswith(f"Here are the 103 staff of {TEAM}:") and "Showing" not in rec.answer
    assert rec.agent.finish == "length" and not rec.cut_off and rec.code_tables["list_added"] == 1
    assert rec.code_tables["list_cut_rows"] == 1


@pytest.mark.parametrize("marker", ["", "{{table q1}}\n\n"])
def test_a_copy_the_model_typed_gives_way_to_codes_list(monkeypatch, marker):
    """The model typed every row (with or without a table line, in model or code mode): code's list is the one
    shown, once; a bullet that names no row or several rows stays."""
    monkeypatch.setenv("SFA_TABLES", "code" if marker else "model")
    few = ROWS[:3]
    res, _full = _list_result(few, 3)
    typed = "\n".join(f"- {r['Name']} ({TEAM})" for r in few)
    reply = (f"There are 3 staff.\n\n{marker}{typed}\n\n- Person 001 and Person 002 share a desk row.\n\n"
             f"- Listed: `{OBJ}`.\n\n{SOURCE}")
    session, _llm = _session(Tools({"list_records": [(res, None)]}), LIST_CALL, reply)
    rec = session.ask(QUESTION)
    table = "\n".join(TABLE_HEAD + [f"| {r['Name']} | {TEAM} | {r['Notes__c']} |" for r in few])
    assert rec.answer == (f"There are 3 staff.\n\n{table}\n\n- Person 001 and Person 002 share a desk row.\n\n"
                          f"- Listed: `{OBJ}`.\n\n{SOURCE}")   # the rows right after the lead
    assert len(_table_rows(rec.answer)) == 3 and rec.answer.count("- Person 00") == 1


def test_a_non_list_question_is_unchanged(monkeypatch):
    monkeypatch.delenv("SFA_TABLES", raising=False)
    group = {"object": OBJ, "grouped_by": "Team__c", "measure": "count", "groups": [{"group": TEAM, "value": 103}],
             "group_count": 1, "shown": 1, "total_across_groups": 103,
             "source": f"preprod sandbox copy; {OBJ} synced 2026-10-13 14:30 UTC"}
    tools = Tools({"group_records": [(group, None)]})
    call = {"name": "group_records", "args": {"object": OBJ, "group_by": "Team__c"}}
    reply = f"{TEAM} has 103 staff (`{OBJ}`, `Team__c`).\n\n{SOURCE}"
    session, llm = _session(tools, call, reply)
    rec = session.ask(f"how many staff are in {TEAM.lower()}?")
    assert llm.calls[0]["messages"][0]["content"] == rprompts.render_records_system(STATUS)   # byte for byte
    assert tools.sent == [("group_records", call["args"])] and tools.limits == [None]
    assert rec.answer == reply and rec.code_tables == {} and rec.table_blocks == {}
    assert not rec.list_ask and not rec.cut_off


def test_a_list_question_keeps_a_limit_the_model_set():
    res, _full = _list_result(ROWS[:5], 103)
    call = {"name": "list_records", "args": {**LIST_CALL["args"], "limit": 5}}
    tools = Tools({"list_records": [(res, None)]})
    session, _llm = _session(tools, call, "The latest 5 staff:\n\n{{table q1}}\n\n" + SOURCE)
    rec = session.ask("list the latest 5 staff of team zeta")
    assert tools.sent == [("list_records", call["args"])] and len(_table_rows(rec.answer)) == 5
    assert "Showing 5 of 103 records." in rec.answer   # the cut the limit made is said, from the result's numbers


def test_finish_length_marks_a_record_answer_cut():
    group = {"object": OBJ, "grouped_by": "Team__c", "measure": "count", "groups": [{"group": TEAM, "value": 103}],
             "group_count": 1, "shown": 1, "total_across_groups": 103,
             "source": f"preprod sandbox copy; {OBJ} synced 2026-10-13 14:30 UTC"}
    call = {"name": "group_records", "args": {"object": OBJ, "group_by": "Team__c"}}
    session, _llm = _session(Tools({"group_records": [(group, None)]}), call,
                             f"{TEAM} has 103 staff (`{OBJ}`), and most of them", finish="length")
    rec = session.ask(f"how many staff are in {TEAM.lower()}?")
    assert rec.answer and rec.cut_off and rec.agent.finish == "length"


def test_the_agent_keeps_the_finish_reason_out_of_its_exports():
    class One:
        def chat(self, messages, tools=None, **_):
            raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
            return {"content": "A long answer that", "tool_calls": [], "usage": {}, "raw": raw}

    q = Question(qid="x", category="c", question="q", expected_facts=[], expected_tools=[], answerable=True,
                 source="s", gold_chunk_ids=[])
    reg = SimpleNamespace(tools={}, openai_tools=lambda: [], call=lambda *a: {})
    r = answer_question(q, One(), reg, _cfg())
    assert r.finish == "length" and "finish" not in r.to_dict() and "_finish" not in r.to_dict()
    assert AgentResult(qid="a", question="b", answer="").finish == ""


# ------------------------------------------------------------------------------------------------ pipeline.answer
def _completion(content=None, calls=(), finish=None):
    tcs = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish or ("tool_calls" if tcs
                                                                                           else "stop"))],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


class Model:
    """``chat.completions.create`` of a fake client: route RECORDS, names NONE, the record agent (one ``call``, then
    ``record`` ending with ``finish``), the knowledge steps (one search, then ``facts``)."""

    def __init__(self, record, call, finish="stop", facts="- `Staff__c` holds one record per staff member.",
                 knowledge_finish="stop"):
        self.record, self.call, self.finish, self.facts = record, call, finish, facts
        self.knowledge_finish = knowledge_finish
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        with self._lock:
            self.requests.append(kwargs)
        system = str(kwargs["messages"][0].get("content") or "")
        n_tools = sum(1 for m in kwargs["messages"] if m.get("role") == "tool")
        if system == rprompts.ROUTE_SYSTEM:
            return _completion("RECORDS")
        if system == rprompts.NAMES_SYSTEM:
            return _completion("NONE")
        if system.startswith(rprompts.COMBINE_SYSTEM):
            return _completion(self.record)
        if system.startswith("You answer questions about Salesforce RECORDS"):
            return _completion(calls=[self.call]) if n_tools == 0 else _completion(self.record, finish=self.finish)
        if kwargs.get("tools") and n_tools == 0:
            return _completion(calls=[("search_fields", {"query": "team"})])
        return _completion(self.facts, finish=self.knowledge_finish)


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def _answer(model: Model, tools, question: str):
    cfg = _cfg()
    session = RecordSession(tools, cfg, llm=LLM(cfg, client=model)) if tools is not None else None
    o = pipeline.answer(question, cfg, _registry(), None, LLM(cfg, client=model), 1, session)
    return o, pipeline.result_row(o, session)


def test_the_pipeline_shows_every_row_and_the_jsonl_says_list(monkeypatch):
    monkeypatch.delenv("SFA_TABLES", raising=False)
    monkeypatch.setenv("SFA_MERGE", "code")
    res, full = _list_result(ROWS, 103, shown=60)
    model = Model(LEAD + "\n\n{{table q1}}\n\n" + SOURCE, ("list_records", LIST_CALL["args"]))
    o, row = _answer(model, Tools({"list_records": [(res, full)]}), QUESTION)
    assert len(_table_rows(o.final)) == 103 and "{{" not in o.final and ANSWER_CUT not in o.final
    assert o.tables_by == "list" and row["jsonl"]["tables_by"] == "list" and "answer_cut" not in row["jsonl"]
    assert row["jsonl"]["code_tables"]["kept"] == 1


ANSWER_CUT = pipeline.ANSWER_CUT_LINE


def test_a_cut_record_answer_gets_the_cut_line_before_its_source_line(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")
    group = {"object": OBJ, "grouped_by": "Team__c", "measure": "count", "groups": [{"group": TEAM, "value": 103}],
             "group_count": 1, "shown": 1, "total_across_groups": 103,
             "source": f"preprod sandbox copy; {OBJ} synced 2026-10-13 14:30 UTC"}
    model = Model(f"{TEAM} has 103 staff (`{OBJ}`), and most of them work", ("group_records",
                                                                            {"object": OBJ, "group_by": "Team__c"}),
                  finish="length")
    o, row = _answer(model, Tools({"group_records": [(group, None)]}), f"how many staff are in {TEAM.lower()}?")
    lines = [ln for ln in o.final.split("\n") if ln.strip()]
    assert ANSWER_CUT in lines and lines.index(ANSWER_CUT) < len(lines) - 1   # the Source line stays last
    assert lines[-1].startswith("Source:") and ANSWER_CUT not in o.body
    assert o.answer_cut and row["jsonl"]["answer_cut"] is True and o.tables_by is None


def test_a_cut_knowledge_answer_gets_the_cut_line_with_records_off():
    model = Model("unused", ("group_records", {}), knowledge_finish="length",
                  facts="`Team__c` names the team of a staff member, and it is")
    o, row = _answer(model, None, "what does the team field mean?")
    assert o.kind == "knowledge" and o.final.endswith("\n\n" + ANSWER_CUT) and row["jsonl"]["answer_cut"] is True
    whole = Model("unused", ("group_records", {}), facts="`Team__c` names the team of a staff member.")
    o, row = _answer(whole, None, "what does the team field mean?")
    assert ANSWER_CUT not in o.final and "answer_cut" not in row["jsonl"]


# ------------------------------------------------------------------------------------------------ the prompt files
def test_the_two_texts_are_new_files_bound_where_they_are_used():
    note, cut = P.load("records_list_note"), P.load("answer_cut_line")
    assert (note.version, note.meta["name"], note.meta["module"]) == (1, "RECORDS_LIST_NOTE", "sf_assistant.agent.tables")
    assert (cut.version, cut.meta["name"], cut.meta["module"]) == (1, "ANSWER_CUT_LINE", "sf_assistant.pipeline")
    assert T.RECORDS_LIST_NOTE == note.text and pipeline.ANSWER_CUT_LINE == cut.text
    assert T.RECORDS_LIST_NOTE.startswith("\n\nLists by code: ") and "{{table q3}}" in T.RECORDS_LIST_NOTE
    assert T.RECORDS_LIST_NOTE not in rprompts.render_records_system(STATUS)
    assert "\n" not in cut.text and not P.TOKEN_RE.search(cut.text)
