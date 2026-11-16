"""Latency logging (2026-10-29): the answer's timeline (agent/timeline.py), the entries that LLM.chat, the agent loop
and the record session add, the JSONL keys, and the reader (``python -m sf_assistant.evaluation timeline``).

Offline: a scripted fake OpenAI client behind the real :class:`LLM`, stub record tools and made-up logs; no
warehouse, no model. Fake data only: the department and its count are made up.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import sf_assistant.pipeline as pipeline
from sf_assistant.agent import record_prompts as rprompts
from sf_assistant.agent import timeline
from sf_assistant.agent.knowledge_agent import MAX_TOOL_RESULT_CHARS, answer_question
from sf_assistant.agent.lean import LeanRegistry
from sf_assistant.agent.llm import LLM
from sf_assistant.agent.mock_llm import MockClient
from sf_assistant.agent.question import Question
from sf_assistant.agent.record_session import RecordSession
from sf_assistant.config import load_config
from sf_assistant.engine.types import QueryEvidence, RecordsStatus, Statement
from sf_assistant.evaluation import __main__ as evaluation_main
from sf_assistant.evaluation import timeline_report as report
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.tools.knowledge_tools import Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
QUESTION = "how many employees are in team zorvex?"
HINTS = "[Hints from code, not from the user]\nRecords named in the question: (fake)"
STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")


def _cfg(steps: int = 6):
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=steps)


@pytest.fixture(autouse=True)
def _closed_timeline():
    timeline.end()   # no timeline left open by another test
    yield
    timeline.end()


# ------------------------------------------------------------------------------------------- a scripted fake client
def _completion(content=None, calls=(), finish=None, tok_in=100, tok_out=10):
    tcs = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish or ("tool_calls" if tcs
                                                                                           else "stop"))],
                           usage=SimpleNamespace(prompt_tokens=tok_in, completion_tokens=tok_out))


class ScriptedClient:
    """``chat.completions.create`` of a fake OpenAI client: ``reply(kwargs)`` makes each completion (thread-safe)."""

    def __init__(self, reply):
        self.reply, self.requests, self._lock = reply, [], threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        with self._lock:
            self.requests.append(kwargs)
        return self.reply(kwargs)


def _prompt_tokens(kwargs) -> int:
    return sum(len(str(m.get("content") or "")) for m in kwargs["messages"]) // 4 + 50 * len(kwargs.get("tools") or [])


# ------------------------------------------------------------------------------------------------ the module
def test_a_closed_timeline_does_nothing_and_nothing_raises():
    assert timeline.end() == {}
    timeline.add("model", time.perf_counter(), round=1)
    timeline.add("tool", "not a number")                 # its own error is swallowed
    with timeline.stage("x"):
        pass
    assert timeline.run_in("x", lambda a, b=0: a + b, 1, b=2) == 3
    with pytest.raises(KeyError):                         # the block's own exception goes on unchanged
        with timeline.stage("x"):
            raise KeyError("boom")
    timeline.begin()
    with pytest.raises(KeyError):
        with timeline.stage("y"):
            raise KeyError("boom")
    timeline.add("tool", "not a number")
    out = timeline.end()
    assert [e["stage"] for e in out["entries"]] == ["y"] and timeline.end() == {}
    assert out["t0_utc"].endswith("+00:00") and "." in out["t0_utc"]


def test_labels_follow_blocks_and_pool_threads():
    def note(word: str) -> str:
        timeline.add("check", time.perf_counter(), name=word, nudge=False)
        return word

    timeline.begin()
    note("before")
    with timeline.stage("main"):
        note("inside")
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            labelled = pool.submit(timeline.run_in, "side", note, "pool")
            bare = pool.submit(note, "bare")              # a pool thread has no label of its own
            assert labelled.result() == "pool" and bare.result() == "bare"
        finally:
            pool.shutdown(wait=True)
    note("after")
    entries = timeline.end()["entries"]
    where = {e["name"]: e["stage"] for e in entries if e["kind"] == "check"}
    assert where == {"before": "other", "inside": "main", "pool": "side", "bare": "other", "after": "other"}
    assert sorted(e["stage"] for e in entries if e["kind"] == "stage") == ["main", "side"]
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)
    assert all(set(e) >= {"kind", "stage", "at", "s"} for e in entries)


def test_entries_past_the_cap_are_counted(monkeypatch):
    monkeypatch.setattr(timeline, "MAX_ENTRIES", 3)
    timeline.begin()
    for _ in range(5):
        timeline.add("tool", time.perf_counter(), name="x")
    out = timeline.end()
    assert len(out["entries"]) == 3 and out["dropped"] == 2


# ------------------------------------------------------------------------------------------------ LLM.chat
def test_llm_chat_adds_one_model_entry_per_call(monkeypatch):
    monkeypatch.setattr(LLM, "backoff_base_s", 0.0)
    llm = LLM(_cfg(), client=MockClient())
    secret = "a question text that must never reach the timeline"
    timeline.begin()
    res = llm.chat([{"role": "system", "content": "Reply RECORDS or NO_RECORDS."}, {"role": "user", "content": secret}],
                   max_tokens=8)
    assert set(res) == {"content", "tool_calls", "usage", "latency_s", "raw"}   # the result dict as before
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    llm.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
              {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "{}"}], tools=tools)
    llm.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
              {"role": "assistant", "content": "draft"}, {"role": "user", "content": "check this"}])
    entries = timeline.end()["entries"]
    first, second, third = entries
    assert first["kind"] == "model" and first["stage"] == "other"
    assert {k: first[k] for k in ("round", "after", "offered", "max", "finish", "tries")} == \
        {"round": 1, "after": "question", "offered": 0, "max": 8, "finish": "stop", "tries": 1}
    assert first["tok_in"] > 0 and first["tok_out"] > 0 and "failed" not in first and "error" not in first
    assert (second["round"], second["after"], second["offered"]) == (2, "tools", 1)
    assert (third["round"], third["after"], third["offered"]) == (2, "nudge", 0)
    assert secret not in json.dumps(entries) and "check this" not in json.dumps(entries)

    timeline.begin()   # a transient failure, then success: two tries and the failed attempt
    LLM(_cfg(), client=MockClient(fail_first=1)).chat([{"role": "user", "content": "q"}])
    (e,) = timeline.end()["entries"]
    assert e["tries"] == 2 and [(f["error"], f["status"]) for f in e["failed"]] == [("APIConnectionError", None)]

    timeline.begin()   # every attempt fails: the call raises as before and the entry says so, without its text
    with pytest.raises(Exception) as caught:
        LLM(_cfg(), client=MockClient(fail_first=5)).chat([{"role": "user", "content": "q"}])
    (e,) = timeline.end()["entries"]
    assert type(caught.value).__name__ == "APIConnectionError" and e["error"] == "APIConnectionError"
    assert e["tries"] == 3 and len(e["failed"]) == 3 and "tok_in" not in e
    assert "mock transient" not in json.dumps(e)


# ------------------------------------------------------------------------------------------------ the agent loop
class _Registry:
    """A tool registry with ``get_field`` (a small result) and ``big`` (a result over the cut)."""

    def __init__(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {})
                      for n in ("get_field", "big")}

    def openai_tools(self):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
                for n in self.tools]

    def call(self, name, arguments=None):
        if name == "big":
            return {"ok": True, "result": {"text": "x" * (MAX_TOOL_RESULT_CHARS + 500)}, "error": None, "chunk_ids": []}
        if (arguments or {}).get("name") == "Missing__c":
            return {"ok": False, "result": None, "error": "no such field", "chunk_ids": []}
        return {"ok": True, "result": {"field": (arguments or {}).get("name")}, "error": None, "chunk_ids": []}


def test_the_agent_loop_adds_tool_and_check_entries():
    script = iter([
        _completion("I know it already."),                                         # no tool call: tools_first
        _completion(calls=[("get_field", {"name": "A"})]),
        _completion(calls=[("get_field", {"name": "A"}), ("nope", {}), ("get_field", {"name": "A", "k": 2}),
                           ("big", {}), ("get_field", {"name": "Missing__c"})]),
        _completion(calls=[("get_field", {"name": "B"})]),                         # the step cap: forced answer
        _completion("The answer.", finish="length", tok_out=1500),
    ])
    client = ScriptedClient(lambda kw: next(script))
    timeline.begin()
    with timeline.stage("knowledge"):
        r = answer_question(Question(qid="q", category="c", question="q", expected_facts=[], expected_tools=[],
                                     answerable=True, source="t", gold_chunk_ids=[]),
                            LLM(_cfg(), client=client), _Registry(), _cfg(steps=4), require_tools=True)
    entries = timeline.end()["entries"]
    assert r.answer == "The answer." and r.steps == 5 and len(r.tool_calls) == 7
    kinds = [(e["kind"], e.get("name")) for e in entries if e["kind"] != "stage"]
    assert kinds == [("model", None), ("check", "tools_first"), ("model", None), ("tool", "get_field"),
                     ("model", None), ("tool", "get_field"), ("tool", "<unknown>"), ("tool", "get_field"),
                     ("tool", "big"), ("tool", "get_field"), ("model", None), ("tool", "get_field"),
                     ("check", "step_cap"), ("model", None)]
    tools = [e for e in entries if e["kind"] == "tool"]
    assert [e["round"] for e in tools] == [2, 3, 3, 3, 3, 3, 4]
    assert [(e.get("again_of"), e.get("same")) for e in tools] == \
        [(None, None), (1, True), (None, None), (2, False), (None, None), (None, None), (None, None)]
    assert [e.get("error") for e in tools] == [None, None, "not_run", None, None, "failed", None]
    assert [e["cut"] for e in tools] == [False, False, False, False, True, False, False]
    sent = [m["content"] for m in client.requests[-1]["messages"] if m["role"] == "tool"]
    assert [e["chars"] for e in tools] == [len(t) for t in sent]   # the characters fed back to the model
    models = [e for e in entries if e["kind"] == "model"]
    assert [(e["round"], e["after"], e["offered"]) for e in models] == \
        [(1, "question", 2), (2, "nudge", 2), (3, "tools", 2), (4, "tools", 2), (5, "nudge", 0)]
    assert models[-1]["finish"] == "length" and all(e["stage"] == "knowledge" for e in entries)


def _question() -> Question:
    return Question(qid="q", category="c", question="q", expected_facts=[], expected_tools=[], answerable=True,
                    source="t", gold_chunk_ids=[])


def test_a_tool_result_that_cannot_be_written_keeps_its_record_as_before():
    """Review of 2026-10-29: the record is appended before the tool message is built, as before the logging, so a
    result whose text conversion raises (not TypeError or ValueError) still leaves its record in ``tool_calls``."""
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    class Registry(_Registry):
        def call(self, name, arguments=None):
            return {"ok": True, "result": {"x": Unprintable()}, "error": None, "chunk_ids": []}

    script = iter([_completion(calls=[("get_field", {"name": "A"})])])
    timeline.begin()
    r = answer_question(_question(), LLM(_cfg(), client=ScriptedClient(lambda kw: next(script))), Registry(), _cfg())
    timeline.end()
    assert len(r.tool_calls) == 1 and r.tool_calls[0]["ok"] and r.error == "RuntimeError: no text"


def test_a_call_past_the_lean_budget_is_not_run():
    """Review of 2026-10-29: a call the lean registry refuses past its budget never ran: ``not_run``, not ``failed``."""
    script = iter([_completion(calls=[("search_fields", {"query": "status"}), ("search_fields", {"query": "date"})]),
                   _completion("Status__c holds the interview status.")])
    timeline.begin()
    r = answer_question(_question(), LLM(_cfg(), client=ScriptedClient(lambda kw: next(script))),
                        LeanRegistry(_registry(), max_calls=1), _cfg())
    entries = timeline.end()["entries"]
    assert [c["ok"] for c in r.tool_calls] == [True, False]
    assert [(e["name"], e["ok"], e.get("error")) for e in entries if e["kind"] == "tool"] == \
        [("search_fields", True, None), ("search_fields", False, "not_run")]


# ------------------------------------------------------------------------------------------------ one answer
def _evidence(qid: str, args: dict, cached: bool = False) -> QueryEvidence:
    result = {"query_id": qid, "object": "Recruiter__c", "count": 6, "counted": "Recruiter__c records",
              "applied": {"filters": ["Department__c = 'Zorvex'"]}}
    st = Statement(sql='SELECT COUNT(*) AS n FROM main."Recruiter__c" AS t0 WHERE t0."Department__c" = ?',
                   params=["Zorvex"], columns=["n"], rows=[[6]], row_count=1, truncated=False, elapsed_ms=40.0,
                   cached=cached)
    return QueryEvidence(query_id=qid, tool="count_records", kind="data", object="Recruiter__c", arguments=dict(args),
                         ok=True, error=None, applied=result["applied"], notes=[], statements=[st],
                         tables=["Recruiter__c"], synced_at={"Recruiter__c": "2026-10-13 14:12 UTC"}, summary="6",
                         model_result=result, elapsed_ms=55.0)


class StubTools:
    """The record tools surface the triage and the record agent use (fake data): no stored person, no value spot."""

    def __init__(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {})
                      for n in ("count_records", "list_records")}
        self._evidence: list[QueryEvidence] = []

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        ev = _evidence(f"q{len(self._evidence) + 1}", arguments or {})
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def spot_people(self, question):
        return []

    def resolve_person_name(self, name):
        return None

    def spot_values(self, question, people=None):
        return []

    def begin_question(self, question="", people=None, values=None):
        self._evidence = []

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question, missing=()):
        return HINTS

    def status(self):
        return STATUS

    def close(self):
        pass


def _reply(kwargs):
    """Route RECORDS, names NONE, the record agent one count then its answer, the knowledge steps a plain reply, the
    merge the record answer."""
    system = str(kwargs["messages"][0].get("content") or "")
    n_tools = sum(1 for m in kwargs["messages"] if m.get("role") == "tool")
    tok = _prompt_tokens(kwargs)
    if system == rprompts.ROUTE_SYSTEM:
        return _completion("RECORDS", tok_in=tok, tok_out=2)
    if system == rprompts.NAMES_SYSTEM:
        return _completion("NONE", tok_in=tok, tok_out=2)
    if system.startswith("You answer questions about Salesforce RECORDS"):
        if n_tools == 0:
            return _completion(calls=[("count_records", {"object": "Recruiter__c"})], tok_in=tok, tok_out=30)
        return _completion("6 employees are in Zorvex.", tok_in=tok, tok_out=12)
    if system.startswith("You write the ONE final answer"):
        return _completion("6 employees are in Zorvex.\nSource: preprod sandbox copy, synced 2026-10-13 14:12 UTC.",
                           tok_in=tok, tok_out=25)
    if kwargs.get("tools") and n_tools == 0:   # a knowledge step looks the field up first
        return _completion(calls=[("search_fields", {"query": "department"})], tok_in=tok, tok_out=20)
    return _completion("Department__c records group employees.", tok_in=tok, tok_out=9)


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def _stages(o) -> dict[str, dict]:
    return {e["stage"]: e for e in o.timeline["entries"] if e["kind"] == "stage"}


def test_an_answer_keeps_its_timeline_and_the_jsonl_gets_it(monkeypatch):
    monkeypatch.setenv("SFA_MERGE", "code")   # the default since 2026-10-29: the lean merge makes no model call
    client = ScriptedClient(_reply)
    cfg = _cfg()
    session = RecordSession(StubTools(), cfg, llm=LLM(cfg, client=client))
    o = pipeline.answer(QUESTION, cfg, _registry(), None, LLM(cfg, client=client), 1, session)
    assert o.mode == "lean" and o.rec is not None and not o.rec.no_records
    entries = o.timeline["entries"]
    stages = _stages(o)
    assert set(stages) >= {"spelling", "people_spots", "route", "names", "value_spots", "triage", "records", "hints",
                           "lean", "wait_records", "post", "merge", "assemble"}
    by_stage: dict[str, list[dict]] = {}
    for e in entries:
        if e["kind"] == "model":
            by_stage.setdefault(e["stage"], []).append(e)
    # the merge stage stays, without a model call (the merge by code; SFA_MERGE=model: tests/test_code_merge.py)
    assert {k: len(v) for k, v in by_stage.items() if k in ("route", "names", "merge")} == \
        {"route": 1, "names": 1}
    assert o.merge_by == "code" and not any(e["stage"] == "merge" for e in entries if e["kind"] != "stage")
    assert len(by_stage["records"]) >= 2 and by_stage["lean"]
    assert stages["hints"]["chars"] == len(HINTS)
    (tool,) = [e for e in entries if e["kind"] == "tool" and e["stage"] == "records"]
    assert (tool["name"], tool["ok"], tool["round"]) == ("count_records", True, 1)
    assert [e["name"] for e in entries if e["kind"] == "tool" and e["stage"] == "lean"] == ["search_fields"]
    assert any(e["kind"] == "check" and e["name"] == "draft" and e["stage"] == "records" for e in entries)
    # the triage before the records branch and the lean step; the merge after the wait for the records
    rec, lean, tri = stages["records"], stages["lean"], stages["triage"]
    assert tri["at"] + tri["s"] <= min(rec["at"], lean["at"]) + 0.002
    assert stages["merge"]["at"] >= stages["wait_records"]["at"] + stages["wait_records"]["s"] - 0.002
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)
    dumped = json.dumps(o.timeline)
    assert QUESTION not in dumped and "Zorvex" not in dumped and "zorvex" not in dumped and HINTS not in dumped

    row = pipeline.result_row(o, session)
    assert row["jsonl"]["timeline"] == o.timeline and row["jsonl"]["merge_by"] == "code"
    (q,) = row["jsonl"]["queries"]
    assert q["seconds"] == 0.055 and q["sql_seconds"] == 0.04


def test_records_only_and_records_off_answers():
    client = ScriptedClient(_reply)
    cfg = _cfg()
    session = RecordSession(StubTools(), cfg, llm=LLM(cfg, client=client))
    o = pipeline.answer(QUESTION, cfg, _registry(), None, LLM(cfg, client=client), 1, session, records_only=True)
    stages = _stages(o)
    assert "lean" not in stages and "knowledge" not in stages and "merge" not in stages
    rec, tri = stages["records"], stages["triage"]   # the triage runs inside the records stage here
    assert rec["at"] <= tri["at"] and tri["at"] + tri["s"] <= rec["at"] + rec["s"] + 0.002
    o = pipeline.answer("what does Status__c mean?", cfg, _registry(), None, LLM(cfg, client=client), 2, None)
    stages = _stages(o)
    assert set(stages) == {"spelling", "knowledge", "assemble"}
    assert all(e["stage"] == "knowledge" for e in o.timeline["entries"] if e["kind"] in ("model", "tool"))


def test_the_records_query_seconds_count_only_statements_run_in_the_question():
    ev = _evidence("q1", {}, cached=True)
    (q,) = pipeline._queries(SimpleNamespace(evidence=[ev]))
    assert q["seconds"] == 0.055 and q["sql_seconds"] == 0.0


# ------------------------------------------------------------------------------------------------ the reader
def _t(stage, at, s, **kw):
    return {"kind": "stage", "stage": stage, "at": at, "s": s, **kw}


def _m(stage, at, s, rnd, after, offered, tok_in, tok_out, **kw):
    return {"kind": "model", "stage": stage, "round": rnd, "after": after, "offered": offered, "max": 1500, "at": at,
            "s": s, "tok_in": tok_in, "tok_out": tok_out, "finish": kw.pop("finish", "tool_calls"), "tries": 1, **kw}


ENTRIES = [
    _t("spelling", 0.001, 0.004), _t("people_spots", 0.006, 0.012),
    _m("route", 0.019, 0.23, 1, "question", 0, 431, 2, finish="stop"), _t("route", 0.019, 0.231),
    _m("names", 0.019, 0.317, 1, "question", 0, 468, 11, finish="stop"), _t("names", 0.019, 0.318),
    _t("value_spots", 0.338, 0.061), _t("triage", 0.006, 0.402),
    _t("records", 0.409, 30.0), _t("lean", 0.409, 9.0), _t("hints", 0.412, 0.618, chars=1240),
    _m("records", 1.033, 6.8, 1, "question", 14, 17240, 96),
    {"kind": "tool", "stage": "records", "round": 1, "name": "count_records", "at": 7.84, "s": 0.2, "chars": 2410,
     "cut": False, "ok": True},
    _m("lean", 0.41, 8.0, 1, "question", 5, 4783, 92, finish="stop"),
    _m("records", 8.05, 9.0, 2, "tools", 14, 18262, 131),
    {"kind": "tool", "stage": "records", "round": 2, "name": "count_records", "at": 17.06, "s": 0.23, "chars": 5980,
     "cut": False, "ok": True, "again_of": 1, "same": False},
    {"kind": "tool", "stage": "lean", "round": 1, "name": "list_fields", "at": 8.42, "s": 0.002, "chars": 16034,
     "cut": True, "ok": True},
    _t("wait_records", 9.41, 21.0),
    _m("records", 17.3, 10.0, 3, "tools", 14, 21004, 412, finish="stop"),
    {"kind": "check", "stage": "records", "name": "draft", "at": 27.31, "s": 0.006, "nudge": True},
    _m("records", 27.32, 2.8, 4, "nudge", 14, 21541, 88, finish="stop"),
    _t("post", 30.2, 0.2), _t("merge", 30.45, 5.0),
    _m("merge", 30.46, 4.95, 1, "question", 0, 6120, 402, finish="stop", max=1600),
    _t("assemble", 35.46, 0.04),
]
JSONL = {"question": "SECRET-QUESTION", "answer": "SECRET-ANSWER", "answer_kind": "merged", "seconds": 35.53,
         "knowledge_mode": "lean", "route_seconds": 0.41, "record_seconds": 30.0, "merge_seconds": 5.0,
         "merge_error": None, "rejected_numbers": [],
         "queries": [{"query_id": "q1", "tool": "count_records", "sql": ["SELECT secret_column"], "params": [["P"]],
                      "seconds": 0.2, "sql_seconds": 0.14},
                     {"query_id": "q2", "tool": "count_records", "sql": ["SELECT secret_column"], "params": [["P"]],
                      "seconds": 0.23, "sql_seconds": 0.16}],
         "timeline": {"t0_utc": "2026-10-29T09:14:03.512+00:00", "entries": ENTRIES}}


def _log(tmp_path) -> Path:
    lines = [
        {"ts": "2026-10-29T09:00:00+0000", "event": "startup", "status": {"records_ok": True}},
        {"ts": "2026-10-29T09:10:00+0000", "event": "ask", "ticket": 6, "mode": "merged", "status": "ok",
         "question": "SECRET-OLD", "answer": "SECRET-OLD-ANSWER", "seconds": 12.5, "queue_wait_s": 0.0,
         "detail": {"jsonl": {"seconds": 12.4, "route_seconds": 0.4, "record_seconds": 11.0}}},
        {"ts": "2026-10-29T09:14:42+0000", "event": "ask", "ticket": 7, "mode": "merged", "status": "ok",
         "question": "SECRET-QUESTION", "answer": "SECRET-ANSWER", "seconds": 40.2, "queue_wait_s": 1.5,
         "detail": {"jsonl": JSONL, "reopen": {"close_s": 0.42, "open_s": 4.18, "s": 4.62}}},
        {"ts": "2026-10-29T09:20:00+0000", "event": "ask", "ticket": 8, "mode": "records", "status": "busy",
         "question": "SECRET-BUSY", "queue_wait_s": 180.0},
        {"ts": "2026-10-29T09:30:00+0000", "event": "ask", "ticket": 9, "mode": "merged", "status": "timeout",
         "question": "SECRET-LATE", "seconds": 240.0, "queue_wait_s": 0.3},
        {"ts": "2026-10-29T09:31:10+0000", "event": "late_finish", "ticket": 9, "mode": "merged",
         "question": "SECRET-LATE", "seconds": 310.0, "answer": "SECRET-LATE-ANSWER",
         "detail": {"jsonl": {**JSONL, "seconds": 309.9, "timeline": {"t0_utc": "2026-10-29T09:26:00.000+00:00",
                                                                      "entries": ENTRIES}}, "retry_s": 2.5}},
    ]
    path = tmp_path / "web_log.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n", encoding="utf-8")
    return path


def test_the_reader_loads_page_lines_and_late_finishes(tmp_path):
    asks = report.load(_log(tmp_path))
    assert [(a.label, a.status) for a in asks] == [("t6", "ok"), ("t7", "ok"), ("t9", "timeout, finished late")]
    late = asks[-1]
    assert late.page_s == 310.0 and late.queue_s == 0.3 and late.retry_s == 2.5
    assert asks[1].when == "2026-10-29T09:14:03Z" and asks[0].when == "2026-10-29T09:10:00Z"
    assert [a.label for a in report.select(asks, ticket=7)] == ["t7"]
    assert [a.label for a in report.select(asks, at="2026-10-29T09:14")] == ["t7"]
    assert [a.label for a in report.select(asks, slowest=2)] == ["t9", "t7"]
    assert [a.label for a in report.select(asks, last=2)] == ["t7", "t9"] and report.select(asks)[0].label == "t9"


def test_the_waterfall_nests_sums_and_joins(tmp_path):
    asks = report.load(_log(tmp_path))
    text = "\n".join(report.waterfall(asks[1]))
    lines = text.splitlines()
    assert lines[0].startswith("t7  2026-10-29T09:14:03Z  ok  mode merged  knowledge lean")
    assert lines[1] == ("waited 1.50 s; page 40.20 s = reopen 4.62 (close 0.42, open 4.18) + answer 35.53 + "
                        "hand-off 0.05")
    def line_of(fragment):
        return next(x for x in lines if fragment in x)

    assert line_of(" triage").index("triage") < line_of(" route").index("route")      # route nests in triage
    assert line_of("    hints (1240 chars)")                                             # hints nest in records
    assert "model r2 after tools: 18262 in (+926), 131 out" in text                   # 18262 - 17240 - 96
    assert "model r3 after tools: 21004 in (+2611)" in text and "model r4 after nudge: 21541 in (+125)" in text
    assert "tool count_records: 2410 chars (~709 tok), sql 0.14" in text
    assert "tool count_records: 5980 chars (~1759 tok), sql 0.16, again of tool 1 (narrowed)" in text
    assert "tool list_fields: 16034 chars (~4716 tok), CUT at 16,000" in text
    assert "check draft -> nudge" in text
    assert "long pole: records 30.00 s (lean ended 21.00 s earlier); main thread waited 21.00 s" in text
    assert "rounds after a check: 1 (2.80 s): draft 2.80 s" in text
    assert "tool calls: 3 (0 failed, 0 not run, 1 cut); repeated 0, narrowed 1" in text
    assert "SECRET" not in text and "secret_column" not in text
    old = "\n".join(report.waterfall(asks[0]))
    assert ("no timeline in this line (answers before 2026-10-29 have none); stage totals: route 0.4, records 11.0, "
            "answer 12.4") in old
    late = "\n".join(report.waterfall(asks[2]))
    assert "page 310.00 s = retry 2.50 + answer 309.90 + hand-off -2.40" in late


def test_damaged_lines_late_failures_and_a_failed_reopen(tmp_path):
    """Review of 2026-10-29: a line cut inside a character is skipped; a late finish that failed shows its exception
    class only (never its message) and an abandoned ask is named so; a failed reopen after a swap error is counted in
    the page seconds."""
    lines = [
        {"ts": "2026-10-29T10:00:00+0000", "event": "ask", "ticket": 1, "status": "abandoned", "seconds": 5.0,
         "queue_wait_s": 0.1, "question": "SECRET-1"},
        {"ts": "2026-10-29T10:01:00+0000", "event": "late_finish", "ticket": 1, "seconds": 9.0, "question": "SECRET-1",
         "error": "RuntimeError: SECRET-DETAIL in the message"},
        {"ts": "2026-10-29T10:02:00+0000", "event": "ask", "ticket": 2, "status": "ok", "seconds": 12.0,
         "queue_wait_s": 0.0, "question": "SECRET-2",
         "detail": {"jsonl": {"seconds": 10.0}, "failed_reopen": {"close_s": 0.3, "open_s": 1.2, "s": 1.6}}},
    ]
    path = tmp_path / "web_log.jsonl"
    with open(path, "wb") as fh:
        fh.write((json.dumps(lines[0]) + "\n").encode())
        fh.write('{"event": "ask", "question": "caf\u00e9'.encode()[:-1] + b"\n")   # cut inside a character
        for x in lines[1:]:
            fh.write((json.dumps(x) + "\n").encode())
    asks = report.load(path)
    assert [(a.label, a.status) for a in asks] == [("t1", "abandoned, finished late, failed (RuntimeError)"),
                                                   ("t2", "ok")]
    head = report.waterfall(asks[1])[1]
    assert head == ("waited 0.00 s; page 12.00 s = answer 10.00 + failed reopen 1.60 (close 0.3, open 1.2) + "
                    "hand-off 0.40")
    assert "SECRET" not in "\n".join("\n".join(report.waterfall(a)) for a in asks)


def test_stats_and_the_command(tmp_path, capsys):
    path = _log(tmp_path)
    text = "\n".join(report.stats(report.load(path)))
    assert text.startswith("answers: 3, 2 with a timeline")
    assert "model calls by stage:" in text and "tool calls:" in text and "SECRET" not in text
    assert evaluation_main.main(["timeline", str(path), "--ticket", "7"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("t7  ") and "SECRET" not in out
    assert evaluation_main.main(["timeline", str(path), "--stats"]) == 0
    assert "stage:" in capsys.readouterr().out
    assert evaluation_main.main(["timeline", str(path), "--ticket", "99"]) == 1
    assert evaluation_main.main(["timeline", str(tmp_path / "none.jsonl")]) == 2
    sidecar = tmp_path / "run.jsonl"   # an ask --file sidecar: rows n1, n2
    sidecar.write_text(json.dumps({"n": 1, **JSONL}) + "\n" + json.dumps({"n": 2, "answer_kind": "none",
                                                                          "seconds": 3.0}) + "\n")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        assert evaluation_main.main(["timeline", str(sidecar), "--slowest", "1"]) == 0
    assert buf.getvalue().startswith("n1  2026-10-29T09:14:03Z  ok  knowledge lean\nanswer 35.53 s")
