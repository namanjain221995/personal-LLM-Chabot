"""Record agent session, merge, number audit, facts and rendering (offline; stub tools, scripted LLM)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.config import load_config  # noqa: E402
from sf_assistant.tools.knowledge_tools import Tool  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent.render import applied_text, format_records_block, record_trace_lines  # noqa: E402
from sf_assistant.agent.merge import MERGE_FACTS_CHARS, MERGE_MAX_TOKENS  # noqa: E402
from sf_assistant.agent.record_session import (NOT_FOUND_CODES, MergeResult, RecordAnswer, RecordSession,  # noqa: E402
                           audit_numbers, combine, facts_json, field_check_lines, field_checks, is_local_endpoint,
                           lookup_checks, lookup_nudges, not_found_reply, numbers_in, people_nudges, strip_reasoning,
                           strip_thinking, text_tool_call, unsynced_checks, with_lookup_footer, working_notes,
                           overlap_nudges, date_nudges, merge_scope_issues, mirror_nudges, quoted_spans)
from sf_assistant.engine.types import QueryEvidence, RecordsStatus, RecordsUnavailable, Statement  # noqa: E402

STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")

COUNT_ARGS = {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "eq",
                                                      "value": "Offer Received"}]}


def _count_evidence(qid: str = "q2", count: int = 37, args: dict | None = None) -> QueryEvidence:
    result = {"query_id": qid, "object": "Interview__c", "count": count, "counted": "Interview__c records",
              "applied": {"filters": ["Interview_Outcome__c = 'Offer Received'"]},
              "notes": ["'Offer Received' is also stored in Interview_Status__c (5 records, legacy value)"],
              "source": "preprod sandbox copy; Interview__c synced 2026-10-13 14:12 UTC"}
    st = Statement(sql='SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE t0."Interview_Outcome__c" = ?',
                   params=["Offer Received"], columns=["n"], rows=[[count]], row_count=1, truncated=False,
                   elapsed_ms=2.5)
    return QueryEvidence(query_id=qid, tool="count_records", kind="data", object="Interview__c",
                         arguments=dict(args or COUNT_ARGS), ok=True, error=None, applied=result["applied"],
                         notes=result["notes"], statements=[st], tables=["Interview__c"],
                         synced_at={"Interview__c": "2026-10-13 14:12 UTC"}, summary=str(count),
                         model_result=result, elapsed_ms=3.1)


def _describe_evidence(qid: str = "q1") -> QueryEvidence:
    result = {"query_id": qid, "object": "Interview__c", "fields": [{"field": f"F{i}__c"} for i in range(30)]}
    st = Statement(sql='SELECT t0."Interview_Outcome__c" AS value, COUNT(*) AS n FROM main."Interview__c" AS t0 '
                       "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 501", params=[], columns=["value", "n"],
                   rows=[["Offer Received", 37]], row_count=1, truncated=False, elapsed_ms=0.4, cached=True)
    return QueryEvidence(query_id=qid, tool="describe_fields", kind="orientation", object="Interview__c",
                         arguments={"object": "Interview__c", "query": "offer"}, ok=True, error=None, applied={},
                         notes=[], statements=[st], tables=["Interview__c"],
                         synced_at={"Interview__c": "2026-10-13 14:12 UTC"}, summary="3 fields", model_result=result,
                         elapsed_ms=1.0)


def _failed_evidence(qid: str = "q3") -> QueryEvidence:
    return QueryEvidence(query_id=qid, tool="group_records", kind="data", object="Interview__c",
                         arguments={"object": "Interview__c", "group_by": "Recruiter"}, ok=False,
                         error="Interview__c has no field 'Recruiter'. Closest: Recruiter__c", applied={}, notes=[],
                         statements=[], tables=[], synced_at={}, summary="error", model_result=None, elapsed_ms=0.5)


class StubTools:
    """The RecordTools surface with canned evidence: count_records -> 37."""

    def __init__(self):
        self._evidence: list[QueryEvidence] = []
        self.began = 0
        self.closed = False
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {name: Tool(name=name, description=name, input_schema=schema, handler=lambda **_: {})
                      for name in ("describe_fields", "count_records")}

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        qid = f"q{len(self._evidence) + 1}"
        ev = _count_evidence(qid, args=arguments) if name == "count_records" else _describe_evidence(qid)
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def begin_question(self, question=""):
        self.began += 1
        self.question = question
        self._evidence = []

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question):
        return "[Hints from code, not from the user]\nObjects whose knowledge matches the question words:\n- Interview__c"

    def status(self):
        return STATUS

    def close(self):
        self.closed = True


class ScriptedLLM:
    """Replies by the number of tool results already in the conversation: script[n] (last one repeats)."""

    def __init__(self, script, fail=False, route="RECORDS", names="NONE"):
        self.script, self.fail, self.calls, self.route, self.route_calls = list(script), fail, [], route, []
        self.names, self.names_calls = names, []
        self.choices: list = []   # the tool_choice of each record agent call (prerequisite P2: one forced round)

    def chat(self, messages, tools=None, max_tokens=None, **_):
        if messages[0].get("content") == rprompts.NAMES_SYSTEM:  # the names call (triage), kept apart as well
            self.names_calls.append({"messages": [dict(m) for m in messages], "max_tokens": max_tokens})
            if isinstance(self.names, Exception):
                raise self.names
            return {"content": self.names, "tool_calls": [], "usage": {}}
        if messages[0].get("content") == rprompts.ROUTE_SYSTEM:  # the route call is kept apart from the script
            self.route_calls.append({"messages": [dict(m) for m in messages], "max_tokens": max_tokens})
            if isinstance(self.route, Exception):
                raise self.route
            return {"content": self.route, "tool_calls": [], "usage": {}}
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        self.choices.append(_.get("tool_choice"))
        if self.fail:
            raise RuntimeError("model down")
        n = sum(1 for m in messages if m.get("role") == "tool")
        step = self.script[min(n, len(self.script) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{n}", "name": step["name"], "arguments": step["args"]}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        return {"content": step, "tool_calls": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def _cfg(url="http://localhost:8000/v1"):
    return load_config(base_url=url, api_key="test-key", max_tool_steps=6)


def _rec(answer="37 client interviews ended with an offer.", evidence=None, no_records=False) -> RecordAnswer:
    ev = [_count_evidence()] if evidence is None else evidence
    return RecordAnswer(question="How many interviews ended with an offer?", answer=answer, no_records=no_records,
                        used_data=bool(ev), evidence=ev, agent=None, error=None, unsupported_numbers=[], seconds=1.0)


# ------------------------------------------------------------------------------------ helpers

def test_is_local_endpoint():
    for url in ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "https://LOCALHOST/v1"):
        assert is_local_endpoint(url), url
    for url in ("https://api.openai.com/v1", "http://localhost.example.com/v1", "http://10.0.0.5:8000/v1", "", "not a url"):
        assert not is_local_endpoint(url), url


def test_numbers_in_normalises():
    text = "2,562 then 6.80% and 05, Q2, Ref_1_Email__c, REC-00024 on 2026-10-13; total 1,209.50."
    assert numbers_in(text) == ["2562", "6.8", "5", "24", "2026", "10", "13", "1209.5"]
    assert numbers_in("") == []


def test_audit_numbers_rounding_small_ints_and_dates():
    sources = ['{"value": 27.25, "percent": 6.8, "from": "2026-05-01"}', "last 6 months"]
    assert audit_numbers("Average 27.25 (about 27.3 or 27), 6.8% or 7%, since 2026-05-01, top 3.", sources) == []
    assert audit_numbers("27.2 is half-even, 27.26 is not, 13 is not small, 2026-05-20 is wrong.", sources) == \
        ["27.26", "13", "20"]
    assert audit_numbers("37 and 37 again and 85", ["37"]) == ["85"]


def test_facts_json_orders_and_trims():
    big = _count_evidence("q2")
    big.model_result = dict(big.model_result, groups=[{"group": f"G{i}", "value": i} for i in range(400)])
    ev = [_describe_evidence("q1"), big, _failed_evidence("q3")]
    full = json.loads(facts_json(ev, 10**9))
    assert [f["query_id"] for f in full] == ["q2", "q1"]  # data first, failed entries left out
    assert set(full[0]) == {"query_id", "tool", "object", "result"}
    text = facts_json(ev, 3000)
    assert len(text) <= 3000
    small = json.loads(text)
    assert [f["query_id"] for f in small] == ["q2"]  # orientation dropped first
    assert small[0]["result"]["trimmed"] is True and len(small[0]["result"]["groups"]) < 400
    assert small[0]["result"]["count"] == 37
    assert facts_json([], 100) == "[]"


def test_strip_thinking():
    assert strip_thinking("<think>a</think> B ") == "B"
    assert strip_thinking("reasoning</think>Answer") == "Answer"
    assert strip_thinking(None) == ""


def test_prompts_render_without_placeholders():
    text = rprompts.render_records_system(STATUS)
    assert "<<" not in text and ">>" not in text
    assert "preprod sandbox copy" in text and "2026-10-13 14:56 UTC" in text and "America/New_York" in text
    assert "NO_RECORDS" in rprompts.RECORDS_REQUIRE_TOOLS_MESSAGE
    assert "do NOT mention SOQL" in rprompts.KNOWLEDGE_RECORDS_NOTE
    msg = rprompts.combine_user_message("q?", "K", "R", "[]")
    assert msg == "QUESTION:\nq?\n\nKNOWLEDGE ANSWER:\nK\n\nRECORD ANSWER:\nR\n\nRECORD FACTS:\n[]"


# ------------------------------------------------------------------------------------ session

def test_session_refuses_a_remote_endpoint():
    with pytest.raises(RecordsUnavailable, match="not on this machine"):
        RecordSession(StubTools(), _cfg("https://api.openai.com/v1"), llm=ScriptedLLM(["x"]))


def test_session_no_records_reply():
    tools, llm = StubTools(), ScriptedLLM(["NO_RECORDS"])
    rec = RecordSession(tools, _cfg(), llm=llm, max_steps=4).ask("What values can Status take?")
    assert rec.no_records and rec.answer == "" and rec.error is None and not rec.used_data
    assert tools.began == 1 and rec.evidence == []
    assert tools.question == "What values can Status take?"  # C1: the question gives the role cues
    first = llm.calls[0]["messages"]
    assert first[0]["content"].startswith("You answer questions about Salesforce RECORDS")
    assert "<<" not in first[0]["content"]
    assert first[1]["content"].startswith("What values can Status take?\n\n[Hints from code")
    # prerequisite P2 (2026-11-25): NO_RECORDS on a RECORDS route gets the one forced round (tool_choice required)
    assert any(m.get("content") == rprompts.RECORDS_TOOLS_FORCED_MESSAGE for m in llm.calls[-1]["messages"])
    assert llm.choices == [None, "required"] and rec.tool_retry == {"at": "tools_first", "why": "no_records",
                                                                   "choice": "required"}
    # an unclear route keeps the require-tools nudge, unforced
    tools, llm = StubTools(), ScriptedLLM(["NO_RECORDS"], route="MAYBE")
    rec = RecordSession(tools, _cfg(), llm=llm, max_steps=4).ask("What values can Status take?")
    assert any(m.get("content") == rprompts.RECORDS_REQUIRE_TOOLS_MESSAGE for m in llm.calls[-1]["messages"])
    assert "required" not in llm.choices and rec.tool_retry is None


def test_session_numbers_without_tools_are_never_used():
    llm = ScriptedLLM(["There are 37."])
    rec = RecordSession(StubTools(), _cfg(), llm=llm).ask("How many offers?")
    assert rec.no_records and rec.answer == ""
    assert rec.error == "the record agent answered without calling a record tool"
    # prerequisite P2: one forced round first (the fake ignores it), then the same error, and no_data for the clarifying
    # question; the draft check asks for the record tools, never for the final form "from the tool results above"
    assert llm.choices.count("required") == 1 and rec.no_data == "no_call"
    assert not any(m.get("content") == rprompts.RECORDS_FINAL_FORM_MESSAGE for c in llm.calls for m in c["messages"])


def test_session_normal_answer_and_audit():
    script = [{"name": "count_records", "args": COUNT_ARGS},
              "<think>x</think>37 client interviews ended with an offer (99 more were pending)."]
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask("How many interviews ended with an offer?")
    assert not rec.no_records and rec.used_data and rec.error is None
    assert rec.answer.startswith("37 client interviews")
    assert rec.unsupported_numbers == ["99"]
    assert rec.data_queries == 1 and rec.evidence[0].query_id == "q1"
    assert rec.agent is not None and len(rec.agent.tool_calls) == 1


def test_tool_call_written_as_text_is_run():
    """Qwen sometimes writes the call it means as a ```json block (and repeats it after the nudge)."""
    text = '```json\n{"name": "count_records", "arguments": ' + json.dumps(COUNT_ARGS) + '}\n```'
    script = [text, "37 client interviews ended with an offer."]
    tools, llm = StubTools(), ScriptedLLM(script)
    rec = RecordSession(tools, _cfg(), llm=llm).ask("How many interviews ended with an offer?")
    assert not rec.no_records and rec.used_data and rec.error is None and rec.data_queries == 1
    assert rec.evidence[0].arguments == COUNT_ARGS and rec.answer.startswith("37 client interviews")


def test_text_tool_call_needs_one_fitting_tool():
    from sf_assistant.tools.registry import SCHEMAS

    offered = [{"type": "function", "function": {"name": n, "description": d, "parameters": sch}}
               for n, (d, sch) in SCHEMAS.items()]
    trend = {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c", "period": "last 6 months"}
    assert text_tool_call("```json\n" + json.dumps(trend, indent=2) + "\n```", offered) == ("trend_records", trend)
    assert text_tool_call("<think>x</think><tool_call>" + json.dumps(trend) + "</tool_call>", offered)[0] == "trend_records"
    assert text_tool_call(json.dumps({"object": "Interview__c", "filters": []}), offered) is None  # count or list
    assert text_tool_call(json.dumps({"object": "Interview__c", "grain": "month", "colour": 1}), offered) is None
    for reply in ("NO_RECORDS", "There are 37 offers.", '{"broken": ', "```json\n[1, 2]\n```", ""):
        assert text_tool_call(reply, offered) is None
    assert text_tool_call(json.dumps(trend), None) is None
    # a name that is no string is no call (it raised TypeError: unhashable type: 'list' before)
    for bad in ({"name": ["trend_records"], "arguments": trend}, {"name": {"x": 1}, "arguments": trend}):
        assert text_tool_call(json.dumps(bad), offered) is None


def test_check_draft_messages():
    tools = StubTools()
    session = RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"]))
    q = "How many interviews ended with an offer in the last 6 months?"
    assert session.check_draft("NO_RECORDS", q, STATUS) is None
    assert session.check_draft("No offer data could be queried.", q, STATUS) is None  # no numbers
    assert session.check_draft("In the last 6 months: 3 offers.", q, STATUS) is None  # small numbers and the question's
    tools.call("describe_fields", {"object": "Interview__c"})
    assert session.check_draft("37 interviews ended with an offer.", q, STATUS) == rprompts.RECORDS_RUN_QUERY_MESSAGE
    tools.call("count_records", COUNT_ARGS)
    assert session.check_draft("37 interviews ended with an offer.", q, STATUS) is None
    msg = session.check_draft("37 interviews, an average of 7.32 per month.", q, STATUS)
    assert msg == rprompts.RECORDS_UNSUPPORTED_MESSAGE.replace("<<NUMBERS>>", "7.32")


def test_session_orientation_only_answer_is_sent_back_for_a_query():
    class InOrder(ScriptedLLM):  # replies script[k] on the k-th call
        def chat(self, messages, tools=None, max_tokens=None, **_):
            if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
                return {"content": "RECORDS", "tool_calls": [], "usage": {}}
            self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
            step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
            if isinstance(step, dict):
                return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                         "arguments": step["args"]}], "usage": {}}
            return {"content": step, "tool_calls": [], "usage": {}}

    llm = InOrder([{"name": "describe_fields", "args": {"object": "Interview__c", "query": "offer"}},
                   "37 interviews ended with an offer.",  # from describe_fields counts: sent back once
                   {"name": "count_records", "args": COUNT_ARGS}, "37 client interviews ended with an offer."])
    rec = RecordSession(StubTools(), _cfg(), llm=llm).ask("How many interviews ended with an offer?")
    assert [c["name"] for c in rec.agent.tool_calls] == ["describe_fields", "count_records"]
    assert rec.used_data and rec.data_queries == 1 and rec.answer == ("37 client interviews ended with an offer.\n"
                                                                      "Source: preprod sandbox copy, synced 2026-10-13 "
                                                                      "14:12 UTC.")   # Source line added by code
    sent = [m["content"] for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert rprompts.RECORDS_RUN_QUERY_MESSAGE in sent


def test_route_call_decides_whether_the_record_agent_runs():
    tools = StubTools()
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."], route="NO_RECORDS")
    rec = RecordSession(tools, _cfg(), llm=llm).ask("What values can Interview_Outcome__c take?")
    assert rec.no_records and rec.routed_out and rec.answer == "" and rec.error is None and rec.agent is None
    assert llm.calls == [] and tools.began == 1
    assert llm.route_calls[0]["max_tokens"] == 8
    assert llm.route_calls[0]["messages"][1] == {"role": "user", "content": "What values can Interview_Outcome__c take?"}
    # value spots (2026-10-28): the not-run line also names the stored record values that could overrule the route
    assert record_trace_lines(rec) == ["-- record agent: not run (the route call found no record data in the "
                                       "question, and no name or stored record value in it overrules that)"]
    for route in ("RECORDS", "**RECORDS**", "maybe", "", RuntimeError("model down")):
        llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."], route=route)
        rec = RecordSession(StubTools(), _cfg(), llm=llm).ask("How many offers?")
        assert not rec.routed_out and rec.answer.split("\n")[0] == "37 offers." and rec.data_queries == 1, route
    session = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(["x"], route=" no_records."))
    assert session.route("q") is False


def test_a_route_result_from_the_caller_skips_the_route_call():
    """ask.py routes first in merged mode (§21.19) and passes the result: no second route call."""
    for routed in (True, None):
        llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."], route="NO_RECORDS")
        rec = RecordSession(StubTools(), _cfg(), llm=llm).ask("How many offers?", routed=routed)
        assert llm.route_calls == [] and not rec.routed_out and rec.data_queries == 1, routed
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."], route="RECORDS")
    rec = RecordSession(StubTools(), _cfg(), llm=llm).ask("What values can X take?", routed=False)
    assert llm.route_calls == [] and llm.calls == [] and rec.routed_out and rec.no_records
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."])
    RecordSession(StubTools(), _cfg(), llm=llm).ask("How many offers?")   # default: routes here, as before
    assert len(llm.route_calls) == 1


def test_session_never_raises():
    class Broken(StubTools):
        def object_hints(self, question):
            raise ValueError("boom")

    rec = RecordSession(Broken(), _cfg(), llm=ScriptedLLM(["x"])).ask("q")
    assert rec.no_records and rec.error == "ValueError: boom"


# ------------------------------------------------------------------------------------ combine

def test_combine_accepted():
    llm = ScriptedLLM(["<think>x</think>37 interviews ended with `Offer Received`.\n"
                       "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."])
    m = combine(llm, "How many interviews ended with an offer?", "`Interview_Outcome__c` holds it.", _rec(), STATUS)
    assert isinstance(m, MergeResult) and m.merged and m.error is None and m.rejected_numbers == []
    assert m.text.startswith("37 interviews")
    call = llm.calls[0]
    assert call["max_tokens"] == MERGE_MAX_TOKENS == 2500 and call["tools"] is None   # 1600 until development A
    assert call["messages"][0]["content"] == rprompts.COMBINE_SYSTEM
    user = call["messages"][1]["content"]
    assert user.startswith("QUESTION:\nHow many interviews ended with an offer?\n\nKNOWLEDGE ANSWER:\n")
    facts = json.loads(user.split("RECORD FACTS:\n", 1)[1])
    assert facts[0]["result"]["count"] == 37


def test_combine_rejects_an_invented_number():
    rec = _rec()
    m = combine(ScriptedLLM(["There are 85 offers."]), "q?", "knowledge", rec, STATUS)
    assert not m.merged and m.text == rec.answer and m.rejected_numbers == ["85"]


def test_combine_without_knowledge_numbers_takes_only_quoted_numbers_from_the_knowledge():
    """The lean facts block carries no record numbers (§21.19): a snapshot count it quotes cannot pass the audit as a
    record count; a number in a quoted value or API name still can."""
    k = ("- `Interview_Outcome__c` = 'Offer Received' marks an offer; it is filled on 1,212 of 3,000 records.\n"
         "- The last stage value is `Round 15`.")
    merged = ("37 client interviews ended with an offer.\n- `Interview_Outcome__c` = 'Offer Received' (filled on 1,212 "
              "records), after `Round 15`.\n" + SOURCE_56)
    m = combine(ScriptedLLM([merged]), "How many interviews ended with an offer?", k, _rec(), STATUS)
    assert m.merged and m.rejected_numbers == []                       # default: every knowledge number counts
    m = combine(ScriptedLLM([merged]), "How many interviews ended with an offer?", k, _rec(), STATUS,
                knowledge_numbers=False)
    assert not m.merged and m.rejected_numbers == ["1212"] and m.text == _rec().answer
    quoted = "37 client interviews ended with an offer.\n- They come after `Round 15`.\n" + SOURCE_56
    m = combine(ScriptedLLM([quoted]), "How many interviews ended with an offer?", k, _rec(), STATUS,
                knowledge_numbers=False)
    assert m.merged, m.error
    assert quoted_spans("the recruiter's 'Offer Received', `Round 15`, \"Week 20\"") == [
        "'Offer Received'", "`Round 15`", '"Week 20"']


def test_combine_empty_knowledge_answer_makes_no_call():
    llm = ScriptedLLM(["unused"])
    m = combine(llm, "q?", "  ", _rec(), STATUS)
    assert llm.calls == [] and not m.merged and m.text == _rec().answer


def test_combine_llm_exception_and_no_records():
    m = combine(ScriptedLLM([""], fail=True), "q?", "k", _rec(), STATUS)
    assert not m.merged and m.text == _rec().answer and m.error == "RuntimeError: model down"
    m = combine(ScriptedLLM([""]), "q?", "k", _rec(), STATUS)
    assert not m.merged and m.error == "empty merged answer"
    llm = ScriptedLLM(["x"])
    m = combine(llm, "q?", "k", _rec(answer="", no_records=True, evidence=[]), STATUS)
    assert m.text == "" and not m.merged and llm.calls == []


# ------------------------------------------------------------------------------------ render

def test_records_block_layout():
    rec = _rec(evidence=[_describe_evidence("q1"), _count_evidence("q2"), _failed_evidence("q3")])
    text = format_records_block(rec, STATUS, 20, rows_of=lambda t: 3000)
    lines = text.splitlines()
    assert lines[0] == "-" * 72 + " RECORDS"
    assert lines[1] == ("source  : okeval_run/warehouse.duckdb, read-only local copy of "
                        "sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb")
    assert lines[2] == "origin  : preprod SANDBOX copy (Organization.IsSandbox = true), not production"
    assert lines[3] == "copied  : 2026-10-13 14:56 UTC (file time)   newest sync 2026-10-13 14:56 UTC"
    assert lines[4] == "today   : 2026-10-13 pinned (America/New_York)"
    assert "[q2] count_records  Interview__c  -> 37   (3 ms, SQL built by code)" in lines
    assert "     applied : Interview_Outcome__c = 'Offer Received'" in lines
    assert "     tables  : Interview__c (3000 records, synced 2026-10-13 14:12 UTC)" in lines
    assert ('     sql     : SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 '
            'WHERE t0."Interview_Outcome__c" = ?') in lines
    assert "     params  : ['Offer Received']" in lines
    i = lines.index("     rows    : n")
    assert lines[i + 1] == " " * 15 + "37"
    assert "[q1] describe_fields  Interview__c  (orientation; 1 cached value statement; -v shows them)" in lines
    assert lines[-1].startswith("[q3] FAILED group_records  Interview__c  error: Interview__c has no field")
    assert lines.index("[q1] describe_fields  Interview__c  (orientation; 1 cached value statement; -v shows them)") \
        < len(lines) - 1  # orientation before failed
    verbose = format_records_block(rec, STATUS, 20, verbose=True)
    assert "(cached at open)" in verbose and "Offer Received  37" in verbose
    assert format_records_block(_rec(answer="", no_records=True, evidence=[_describe_evidence()]), STATUS) == ""
    only = format_records_block(_rec(evidence=[_describe_evidence()]), STATUS)  # answer rests on value counts
    assert "[q1] describe_fields  Interview__c  -> 3 fields" in only and "(cached at open)" in only


def test_records_block_rows_cap_and_wide_tables():
    ev = _count_evidence()
    cols = [f"very_long_column_alias_name_number_{i}" for i in range(8)]
    ev.statements = [Statement(sql="SELECT 1", params=[], columns=["n"], rows=[[i] for i in range(30)], row_count=30,
                               truncated=False, elapsed_ms=1.0),
                     Statement(sql="SELECT 2", params=["x"], columns=cols, rows=[["y" * 60] * 8], row_count=1,
                               truncated=True, elapsed_ms=1.0)]
    text = format_records_block(_rec(evidence=[ev]), STATUS, 5)
    assert "sql[1]    : SELECT 1" in text and "params[2] : ['x']" in text
    assert "... showing 5 of 30 rows" in text
    assert "... more rows existed; fetching stopped at 1" in text
    assert max(len(ln) for ln in text.splitlines()) <= 200
    assert "[1]" in text and "very_long_column_alias_name_number_0" in text


def test_applied_text_shapes():
    applied = {"filters": ["A = 'x'"], "any_of": ["B = 'y'", "C = 'z'"], "related": ["has no Session__c"],
               "period": {"date_field": "CreatedDate", "phrase": "this month", "from": "2026-10-01",
                          "to_exclusive": "2026-11-01", "time_zone": "America/New_York",
                          "utc_from": "2026-10-01 04:00", "utc_to_exclusive": "2026-11-01 04:00"}}
    assert applied_text(applied) == ("A = 'x'; any of (B = 'y' OR C = 'z'); has no Session__c; CreatedDate in "
                                     "[2026-10-01, 2026-11-01) (this month, America/New_York, UTC [2026-10-01 04:00, "
                                     "2026-11-01 04:00))")
    segs = {"segments": [{"label": "G", "applied": {"filters": ["O = 'Ghosted'"]}}]}
    assert applied_text({}, segs) == "segment 'G': O = 'Ghosted'"
    assert applied_text({}) == "all records"


def test_trace_lines_map_calls_to_query_ids():
    script = [{"name": "count_records", "args": COUNT_ARGS}, "37 interviews."]
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask("How many?")
    lines = record_trace_lines(rec, verbose=True)
    assert lines[0].startswith("-- record agent: 1 tool call(s), 2 model call(s)")
    assert lines[1] == ('   count_records({"object":"Interview__c","filters":[{"field":"Interview_Outcome__c",'
                        '"op":"eq","value":"Offer Received"}]})  ok=True  -> q1')
    assert lines[2].startswith('     {"query_id": "q1"')


def test_agent_modules_do_not_import_duckdb():
    import subprocess

    code = ("import sys; import sf_assistant.agent.record_prompts, sf_assistant.agent.record_session, "
            "sf_assistant.agent.render; "
            "print('duckdb' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ------------------------------------------------------------------- lookup disambiguation (§21.10)
# Stub evidence with the lookup_check keys of RECORDS_SPEC §21.2 C4.1; synthetic fixture names only.

II = "Internal_Interview__c"
PERSON_Q = "how many mock interviews has delta person taken"
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."


def _other(field, records, fits=None, group="same_target", role=None, target="Recruiter__c"):
    o = {"field": field, "target": target, "group": group, "role": role or field.lower().replace("__c", ""),
         "fits": fits, "records": records}
    if group != "same_target":
        o["via"] = "a different record with the same name"
    return o


def _check(records=0, field="Recruiter__c", role="recruiter", fits=None, cues=("taken",), others=None,
           elsewhere=None, obj=II):
    lc = {"filter": f"{field}.Name = 'Delta Person'", "object": obj, "field": field, "target": "Recruiter__c",
          "target_label": "Employee", "person_kind": "employee", "role": role, "fits": fits, "cues": list(cues),
          "records": records,
          "others": [_other("Interviewer__c", 3, fits="taken", role="interviewer"),
                     _other("Interview_Readiness_Person__c", 1, role="undocumented")] if others is None else others,
          "scope": "all other conditions of this query"}
    if elsewhere is not None:
        lc["elsewhere"] = elsewhere
    return lc


def _person_ev(qid, count, check=None, field="Recruiter__c", obj=II, fields_used=None, tool="count_records"):
    result = {"query_id": qid, "object": obj, "count": count, "counted": f"{obj} records",
              "applied": {"filters": [f"{field}.Name = 'Delta Person'"]}, "notes": [],
              "source": f"preprod sandbox copy; {obj} synced 2026-10-13 14:12 UTC"}
    if check is not None:
        result["lookup_check"] = [check]
    st = Statement(sql=f'SELECT COUNT(*) AS n FROM main."{obj}" AS t0', params=["Delta Person"], columns=["n"],
                   rows=[[count]], row_count=1, truncated=False, elapsed_ms=1.0)
    ev = QueryEvidence(query_id=qid, tool=tool, kind="data", object=obj,
                       arguments={"object": obj, "filters": [{"field": field, "op": "eq", "value": "Delta Person"}]},
                       ok=True, error=None, applied=result["applied"], notes=[], statements=[st], tables=[obj],
                       synced_at={obj: "2026-10-13 14:12 UTC"}, summary=str(count), model_result=result, elapsed_ms=2.0)
    ev.fields_used = [(obj, field)] if fields_used is None else list(fields_used)  # C2 (set after construction)
    ev.error_code = None
    return ev


def _error_ev(qid, code, error="no Employee (Recruiter__c) record is named 'Nobody Here'. Close spellings: none.",
              tool="count_records", kind="data"):
    ev = QueryEvidence(query_id=qid, tool=tool, kind=kind, object=II,
                       arguments={"object": II, "filters": [{"field": "Interviewer__c", "value": "Nobody Here"}]},
                       ok=False, error=error, applied={}, notes=[], statements=[], tables=[], synced_at={},
                       summary="error", model_result=None, elapsed_ms=0.5)
    ev.error_code = code
    ev.fields_used = []
    return ev


class LookupTools(StubTools):
    """StubTools whose evidence log is set by the test."""

    def __init__(self, evidence=()):
        super().__init__()
        self._evidence = list(evidence)


def _session(evidence=()):
    tools = LookupTools(evidence)
    return RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"])), tools


def test_lookup_checks_add_query_id_and_tool():
    ev = [_describe_evidence("q1"), _person_ev("q2", 0, _check()), _error_ev("q3", "E4h"),
          _person_ev("q4", 3, _check(records=3, field="Interviewer__c", role="interviewer", fits="taken"),
                     tool="group_records")]
    checks = lookup_checks(ev)
    assert [(c["query_id"], c["tool"], c["field"]) for c in checks] == \
        [("q2", "count_records", "Recruiter__c"), ("q4", "group_records", "Interviewer__c")]
    assert checks[0]["records"] == 0 and checks[0]["others"][0]["field"] == "Interviewer__c"
    assert lookup_checks([]) == [] and lookup_checks([_count_evidence()]) == []


def test_other_lookup_nudge_fires_for_a_zero_whose_role_does_not_fit():
    """Follow-up round 1: the question's word fits another lookup, not the used one: the firm form, no "answer 0"."""
    session, _ = _session([_person_ev("q1", 0, _check())])
    msg = session.check_draft("Delta Person has taken 0 mock interviews.", PERSON_Q, STATUS)
    expected = (rprompts.RECORDS_OTHER_LOOKUP_FIRM_MESSAGE.replace("<<SPARSE>>", "").replace("<<FIELD>>", "Recruiter__c")
                .replace("<<ROLE>>", "recruiter").replace("<<WORD>>", "taken").replace("<<OBJECT>>", II)
                .replace("<<OTHERS>>", "Interviewer__c (interviewer) 3").replace("<<OTHER_FIELD>>", "Interviewer__c"))
    assert msg == expected  # Interview_Readiness_Person__c has no fitting role, so it is not listed
    assert "never add them" in msg and "<<" not in msg and "answer 0" not in msg
    assert "taken / conducted = interviewer" not in msg   # the role hint comes from the data, not a fixed list


def test_other_lookup_nudge_fires_when_the_question_has_no_role_word():
    check = _check(cues=(), others=[_other("Interviewer__c", 3, role="interviewer"),
                                    _other("Interview_Readiness_Person__c", 1, role="undocumented")])
    session, _ = _session([_person_ev("q1", 0, check)])
    msg = session.check_draft("Delta Person has 0 mock interviews.", "mock interviews of delta person", STATUS)
    assert msg and "Interviewer__c (interviewer) 3, Interview_Readiness_Person__c (undocumented) 1" in msg
    del check["cues"]  # keys that would be empty may be left out: a missing cues key is "no role word" too
    assert lookup_nudges("Delta Person has 0 mock interviews.", [_person_ev("q1", 0, check)])


@pytest.mark.parametrize("case", ["used_fits", "only_other_groups", "names_field", "has_count", "used_later",
                                  "count_not_zero_no_role_word", "other_has_no_records", "not_a_person"])
def test_other_lookup_nudge_does_not_fire(case):
    check, draft, later = _check(), "Delta Person has taken 0 mock interviews.", []
    if case == "used_fits":  # the question's verb fits the lookup the count used
        check = _check(field="Interviewer__c", role="interviewer", fits="taken",
                       others=[_other("Recruiter__c", 2, role="recruiter")])
    elif case == "only_other_groups":  # linked, same-name and system lookups never trigger it
        check = _check(others=[_other("Interviewer__c", 0, fits="taken", role="interviewer"),
                               _other("Candidate__c", 2, fits="taken", group="same_name", target="Account"),
                               _other("Recruiter_Login__c", 4, fits="taken", group="linked", target="User"),
                               _other("CreatedById", 5, fits="taken", group="system", target="User")])
    elif case == "names_field":
        draft = "Delta Person has 0 as recruiter; `Interviewer__c` was not checked."
    elif case == "has_count":  # the count in the same clause as the other lookup's role
        draft = "Delta Person has taken 0 mock interviews as recruiter; 3 as interviewer."
    elif case == "used_later":  # a later query already used Interviewer__c
        later = [_person_ev("q2", 3, None, field="Interviewer__c")]
    elif case == "count_not_zero_no_role_word":  # above 0 only a role that strictly fits nudges
        check = _check(records=2, cues=(), others=[_other("Interviewer__c", 3, role="interviewer")])
    elif case == "other_has_no_records":
        check = _check(others=[_other("Interviewer__c", 0, fits="taken", role="interviewer")])
    elif case == "not_a_person":  # a lookup to a niche or a department: person_kind "other"
        check = dict(_check(cues=(), others=[_other("Hired_Niche__c", 7, role="hired niche", target="Niche__c")]),
                     person_kind="other", field="Niche__c", target="Niche__c")
    session, _ = _session([_person_ev("q1", 0, check)] + later)
    assert session.check_draft(draft, PERSON_Q, STATUS) is None, case


def test_used_later_needs_a_successful_data_query_after_the_check():
    failed = _error_ev("q2", "E2")
    failed.fields_used = [(II, "Interviewer__c")]
    before = _person_ev("q0", 3, None, field="Interviewer__c")  # an earlier query does not count
    for evidence in ([_person_ev("q1", 0, _check()), failed], [before, _person_ev("q1", 0, _check())]):
        assert lookup_nudges("Delta Person has 0.", evidence), evidence
    other_object = _person_ev("q2", 3, None, field="Interviewer__c", obj="Interview__c")
    assert lookup_nudges("Delta Person has 0.", [_person_ev("q1", 0, _check()), other_object])
    as_lists = _person_ev("q2", 3, None, fields_used=[[II, "Interviewer__c"]])  # JSON-shaped pairs work too
    assert lookup_nudges("Delta Person has 0.", [_person_ev("q1", 0, _check()), as_lists]) == []


def test_nudges_run_before_the_number_checks_and_are_joined():
    session, _ = _session([_person_ev("q1", 0, _check())])
    msg = session.check_draft("Delta Person has taken 0 mock interviews (out of 620).", PERSON_Q, STATUS)
    parts = msg.split("\n\n")
    assert len(parts) == 2 and parts[0].startswith("Your count used Recruiter__c (recruiter) and found 0")
    assert parts[1] == rprompts.RECORDS_UNSUPPORTED_MESSAGE.replace("<<NUMBERS>>", "620")
    # the same check twice (count, then group) gives one message: the last check per (object, field)
    session, _ = _session([_person_ev("q1", 0, _check()), _person_ev("q2", 0, _check(), tool="group_records")])
    assert session.check_draft("Delta Person has taken 0.", PERSON_Q, STATUS).count("Your count used") == 1
    assert session.check_draft("NO_RECORDS", PERSON_Q, STATUS) is None
    assert session.check_draft("", PERSON_Q, STATUS) is None


ELSEWHERE = [{"object": II, "field": "Interviewer__c", "role": "interviewer", "fits": "taken", "records": 3,
              "via": "same record"},
             {"object": "Session__c", "field": "Host_User__c", "role": "host employee", "fits": None, "records": 2,
              "via": "same record"}]


def _elsewhere_ev(qid="q1"):
    check = _check(obj="Interview__c", others=[_other("Support_Ref__c", 0, role="undocumented")],
                   elsewhere=ELSEWHERE)
    return _person_ev(qid, 0, check, obj="Interview__c")


def test_elsewhere_nudge():
    session, _ = _session([_elsewhere_ev()])
    msg = session.check_draft("Delta Person has taken 0 client interviews.", PERSON_Q, STATUS)
    expected = (rprompts.RECORDS_ELSEWHERE_MESSAGE.replace("<<FIELD>>", "Recruiter__c").replace("<<ROLE>>", "recruiter")
                .replace("<<OBJECT>>", "Interview__c")
                .replace("<<ELSEWHERE>>", f"{II}.Interviewer__c (interviewer; fits 'taken') 3, Session__c.Host_User__c (host employee) 2"))
    assert msg == expected and "<<" not in msg
    # not when the draft names one of those objects, or a later query ran on one of them
    assert session.check_draft(f"0 client interviews; `{II}` holds more.", PERSON_Q, STATUS) is None
    session, _ = _session([_elsewhere_ev(), _person_ev("q2", 3, None, field="Interviewer__c")])
    assert session.check_draft("Delta Person has taken 0 client interviews.", PERSON_Q, STATUS) is None


def _not_found_tools(evidence):
    class Failing(StubTools):
        def call(self, name, arguments=None):
            ev = evidence[len(self._evidence)]
            self._evidence.append(ev)
            return {"ok": ev.ok, "result": ev.model_result, "error": ev.error, "chunk_ids": []}
    return Failing()


def test_ask_accepts_a_no_such_person_reply_after_a_not_found_error():
    q = "How many mock interviews has Nobody Here taken?"
    reply = "No employee is named 'Nobody Here'.\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."
    script = [{"name": "count_records", "args": {"object": II}}, reply]
    rec = RecordSession(_not_found_tools([_error_ev("q1", "E4h")]), _cfg(), llm=ScriptedLLM(script)).ask(q)
    assert not rec.no_records and rec.answer == reply and not rec.used_data and rec.error is None
    assert rec.unsupported_numbers == [] and rec.data_queries == 0
    for bad in ("Nobody Here has taken 0 mock interviews.", "Nobody Here has taken 3 mock interviews."):
        rec = RecordSession(_not_found_tools([_error_ev("q1", "E4h")]), _cfg(),
                            llm=ScriptedLLM([script[0], bad])).ask(q)
        assert rec.no_records and rec.answer == "" and rec.error == "no successful record query", bad
    rec = RecordSession(_not_found_tools([_error_ev("q1", "E2", error="Internal_Interview__c has no field 'X'.")]),
                        _cfg(), llm=ScriptedLLM(script)).ask(q)  # not a "not there" error: refused as before
    assert rec.no_records and rec.answer == ""


def test_not_found_reply_rules():
    q = "How many mock interviews has Nobody Here taken in 2026?"
    ok = "No employee is named 'Nobody Here' (2026).\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."
    assert NOT_FOUND_CODES == frozenset({"E4h", "E15a", "E1b", "E2b"})
    for code in sorted(NOT_FOUND_CODES):
        assert not_found_reply(ok, [_error_ev("q1", code)], q, STATUS), code
    assert not not_found_reply(ok, [_error_ev("q1", "E4")], q, STATUS)
    assert not not_found_reply(ok, [_error_ev("q1", "E4h", kind="orientation", tool="describe_fields")], q, STATUS)
    assert not not_found_reply(ok, [], q, STATUS)
    assert not not_found_reply("NO_RECORDS", [_error_ev("q1", "E4h")], q, STATUS)
    assert not not_found_reply("0 interviews.", [_error_ev("q1", "E4h")], q, STATUS)
    no_code = _error_ev("q1", None)
    del no_code.error_code  # evidence built before C2 has no error_code attribute
    assert not not_found_reply(ok, [no_code], q, STATUS)


def test_facts_json_not_available_entries_by_error_code():
    big = _describe_evidence("q1")
    ev = [big, _person_ev("q2", 0, _check()), _error_ev("q3", "E4h", error="x " * 400),
          _error_ev("q4", "E2b", error="Interview_Evaluation__c.Evaluator__c is documented in the knowledge but is "
                                        "not synced to the warehouse, so it cannot be queried."),
          _error_ev("q5", "E2"), _failed_evidence("q6")]
    full = json.loads(facts_json(ev, 10**9))
    assert [f["query_id"] for f in full] == ["q2", "q3", "q4", "q1"]  # data, not_available, orientation
    na = [f for f in full if "not_available" in f]
    assert [set(f) for f in na] == [{"query_id", "tool", "object", "not_available"}] * 2
    assert len(na[0]["not_available"]) <= 300 and "not synced" in na[1]["not_available"]
    small = json.loads(facts_json(ev, len(facts_json(ev[1:2], 10**9)) + 10))  # dropped with the orientation entries
    assert [f["query_id"] for f in small] == ["q2"]
    mid = json.loads(facts_json(ev, len(facts_json(ev[1:4], 10**9)) + 10))  # orientation entries go first
    assert [f["query_id"] for f in mid] == ["q2", "q3", "q4"]
    only = json.loads(facts_json([_error_ev("q1", "E15a")], 10))  # the first entry is always kept
    assert [f["query_id"] for f in only] == ["q1"]


def test_facts_json_trims_find_references_lists():
    ev = _count_evidence("q1")
    ev.tool = "find_references"
    ev.model_result = {"query_id": "q1", "name": "Delta Person", "references": [
        {"record": "R1", "object": f"Object_{i}__c", "field": "Person__c", "role": "person", "records": i}
        for i in range(200)], "lookups_checked": 241}
    text = facts_json([ev], 3000)
    res = json.loads(text)[0]["result"]
    assert len(text) <= 3000 and res["trimmed"] is True and 0 < len(res["references"]) < 200


KNOWLEDGE = ("Mock interviews an employee took are held by `Internal_Interview__c.Interviewer__c` (the interviewer); "
             "`Recruiter__c` is the candidate's recruiter.")


def test_field_check_lines():
    ev = [_person_ev("q1", 0, _check())]
    lines = field_check_lines(KNOWLEDGE, ev)
    assert lines == [f"The question's verb fits {II}.Interviewer__c (interviewer), which the knowledge answer names; "
                     f"the record count used {II}.Recruiter__c (recruiter): 0. With the same conditions, "
                     "Interviewer__c has 3 (RECORD FACTS, query q1)."]
    c = field_checks(KNOWLEDGE, ev)[0]
    assert (c["object"], c["field"], c["other_field"], c["other_records"], c["query_id"]) == \
        (II, "Recruiter__c", "Interviewer__c", 3, "q1")
    # the used lookup fits ("the recruiter"): no line
    fits = _check(field="Recruiter__c", fits="recruiter", cues=("recruiter",),
                  others=[_other("Interviewer__c", 3, role="interviewer")])
    assert field_check_lines(KNOWLEDGE, [_person_ev("q1", 0, fits)]) == []
    # no role word in the question: every fits is null, no line
    no_cue = _check(cues=(), others=[_other("Interviewer__c", 3, role="interviewer")])
    assert field_check_lines(KNOWLEDGE, [_person_ev("q1", 0, no_cue)]) == []
    # the knowledge answer does not name the other lookup; or a later query used it
    assert field_check_lines("`Recruiter__c` holds it.", ev) == []
    assert field_check_lines(KNOWLEDGE, ev + [_person_ev("q2", 3, None, field="Interviewer__c")]) == []
    assert field_check_lines("", ev) == []
    # system lookups named by the knowledge answer count too; at most 3 lines
    created = _check(others=[_other("CreatedById", 4, fits="created", role="record creator", target="User")])
    assert field_check_lines("`CreatedById` is the creator.", [_person_ev("q1", 0, created)])
    many = _check(others=[_other(f"Role_{k}__c", k, fits="taken") for k in range(1, 6)])
    assert len(field_check_lines(" ".join(f"Role_{k}__c" for k in range(1, 6)), [_person_ev("q1", 0, many)])) == 3


def test_with_lookup_footer():
    ev = [_person_ev("q1", 0, _check())]
    text = "Delta Person has taken 0 mock interviews.\n" + SOURCE
    out = with_lookup_footer(text, ev)
    assert out == ("Delta Person has taken 0 mock interviews.\n"
                   f"Lookup used: `{II}.Recruiter__c` (recruiter). Same person through other lookups of {II} "
                   "(overlapping, not to be added): `Interviewer__c` (interviewer) 3, "
                   "`Interview_Readiness_Person__c` (undocumented) 1.\n" + SOURCE)
    # nothing when the text names the lookups
    named = "0 as recruiter (`Recruiter__c`); `Interviewer__c` 3, `Interview_Readiness_Person__c` 1.\n" + SOURCE
    assert with_lookup_footer(named, ev) == named
    # an abandoned first check is not listed: the second check's count (3) is in the text
    fixed = _check(records=3, field="Interviewer__c", role="interviewer", fits="taken",
                   others=[_other("Recruiter__c", 0, role="recruiter")])
    ev2 = ev + [_person_ev("q2", 3, fixed, field="Interviewer__c")]
    assert with_lookup_footer("Delta Person took 3 mock interviews.", ev2) == \
        f"Delta Person took 3 mock interviews.\nLookup used: `{II}.Interviewer__c` (interviewer)."
    assert with_lookup_footer("Delta Person took 3 mock interviews as `Interviewer__c`.", ev2) == \
        "Delta Person took 3 mock interviews as `Interviewer__c`."
    # no lookup check, or an empty text: unchanged
    assert with_lookup_footer(text, [_count_evidence()]) == text
    assert with_lookup_footer("", ev) == ""


def test_combine_passes_field_check_to_the_merge():
    rec = _rec(answer="Delta Person has taken 0 mock interviews.", evidence=[_person_ev("q1", 0, _check())])
    llm = ScriptedLLM(["Delta Person took 3 mock interviews as interviewer (`Interviewer__c`).\n" + SOURCE])
    lines = field_check_lines(KNOWLEDGE, rec.evidence)
    m = combine(llm, PERSON_Q, KNOWLEDGE, rec, STATUS, field_check=lines)
    assert m.merged, m
    user = llm.calls[0]["messages"][1]["content"]
    assert "\n\nFIELD CHECK:\n- The question's verb fits Internal_Interview__c.Interviewer__c" in user
    assert user.index("FIELD CHECK:") < user.index("RECORD FACTS:")
    llm = ScriptedLLM(["x"])
    combine(llm, PERSON_Q, KNOWLEDGE, rec, STATUS)  # no field check: no FIELD CHECK section
    assert "FIELD CHECK" not in llm.calls[0]["messages"][1]["content"]


def test_lookup_prompts():
    text = rprompts.render_records_system(STATUS)
    assert "<<" not in text and ">>" not in text
    assert "taken / conducted / done      interviewer                       candidate (interviewed)" in text
    assert "employee or user is the ..." in text and "candidate or lead is the ..." in text
    assert "Never add counts of\n   different lookups or objects" in text
    assert "about_people" in text and "split_totals" in text and "find_references" not in text
    assert "Never write none, no records, not associated or 0" in rprompts.COMBINE_SYSTEM
    assert "FIELD CHECK" in rprompts.COMBINE_SYSTEM and '"not_available"' in rprompts.COMBINE_SYSTEM
    assert "name the one lookup field whose role fits" in rprompts.KNOWLEDGE_RECORDS_NOTE
    assert "Never state record counts or\nrecord values yourself." in rprompts.KNOWLEDGE_RECORDS_NOTE
    assert rprompts.KNOWLEDGE_RECORDS_NOTE.rstrip().endswith("leads `Lead`).")   # §22: the people objects
    for msg, keys in ((rprompts.RECORDS_OTHER_LOOKUP_MESSAGE, ("FIELD", "ROLE", "OBJECT", "OTHERS", "FITS")),
                      (rprompts.RECORDS_OTHER_LOOKUP_FITS_MESSAGE, ("FIELD", "ROLE", "COUNT", "WORD", "OBJECT",
                                                                    "OTHERS")),
                      (rprompts.RECORDS_ELSEWHERE_MESSAGE, ("FIELD", "ROLE", "OBJECT", "ELSEWHERE"))):
        for k in keys:
            msg = msg.replace(f"<<{k}>>", "x")
        assert "<<" not in msg and "never add" in msg
    assert rprompts.combine_user_message("q?", "K", "R", "[]", ["a", "b"]) == \
        "QUESTION:\nq?\n\nKNOWLEDGE ANSWER:\nK\n\nRECORD ANSWER:\nR\n\nFIELD CHECK:\n- a\n- b\n\nRECORD FACTS:\n[]"
    assert rprompts.combine_user_message("q?", "K", "R", "[]", []) == rprompts.combine_user_message("q?", "K", "R", "[]")


def test_session_default_max_steps_is_12():
    session = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(["x"]))
    assert session.cfg.max_tool_steps == 12


# ------------------------------------------------------------------------------------ review fixes (2026-10-14)

SOURCE_56 = "Source: preprod sandbox copy, synced 2026-10-13 14:56 UTC."   # the time of STATUS


def test_a_zero_after_describe_fields_and_a_not_found_error_is_refused():
    class InOrder(ScriptedLLM):  # replies script[k] on the k-th call
        def chat(self, messages, tools=None, max_tokens=None, **_):
            if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
                return {"content": "RECORDS", "tool_calls": [], "usage": {}}
            self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
            step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
            if isinstance(step, dict):
                return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                         "arguments": step["args"]}], "usage": {}}
            return {"content": step, "tool_calls": [], "usage": {}}

    q = "How many mock interviews has Nobody Here taken?"
    calls = [{"name": "describe_fields", "args": {"object": II}}, {"name": "count_records", "args": {"object": II}}]
    for reply, accepted in (("Nobody Here has taken 0 mock interviews.", False),
                            ("No employee is named 'Nobody Here'.\n" + SOURCE_56, True)):
        llm = InOrder(calls + [reply])
        rec = RecordSession(_not_found_tools([_describe_evidence("q1"), _error_ev("q2", "E4h")]), _cfg(), llm=llm).ask(q)
        assert not rec.used_data and rec.data_queries == 0, reply
        if accepted:
            assert not rec.no_records and rec.answer == reply and rec.error is None
        else:
            assert rec.no_records and rec.answer == "" and rec.error == "no successful record query"
            sent = [m["content"] for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
            assert rprompts.RECORDS_NOT_FOUND_MESSAGE in sent      # the draft was sent back once
    session, tools = _session([_describe_evidence("q1"), _error_ev("q2", "E4h")])
    assert session.check_draft("Nobody Here has taken 0 mock interviews.", q, STATUS) == rprompts.RECORDS_NOT_FOUND_MESSAGE
    assert session.check_draft("No employee is named 'Nobody Here'.\n" + SOURCE_56, q, STATUS) is None
    # an orientation-only run without a not-found error keeps its earlier behaviour (the number check)
    session, _ = _session([_describe_evidence("q1")])
    assert session.check_draft("37 interviews ended with an offer.", q, STATUS) == rprompts.RECORDS_RUN_QUERY_MESSAGE


def test_nudge_for_a_count_above_zero_through_a_role_that_does_not_fit():
    check = _check(records=2, others=[_other("Interviewer__c", 16, fits="taken", role="interviewer"),
                                      _other("Interview_Readiness_Person__c", 10, role="undocumented")])
    ev = [_person_ev("q1", 2, check)]
    msgs = lookup_nudges("Delta Person has taken 2 mock interviews (`Recruiter__c`).", ev)
    assert msgs == [rprompts.RECORDS_OTHER_LOOKUP_FITS_MESSAGE.replace("<<FIELD>>", "Recruiter__c")
                    .replace("<<ROLE>>", "recruiter").replace("<<COUNT>>", "2").replace("<<WORD>>", "taken")
                    .replace("<<OBJECT>>", II).replace("<<OTHERS>>", "Interviewer__c (interviewer) 16")]
    assert lookup_nudges("2 as recruiter (`Recruiter__c`); 16 as interviewer (`Interviewer__c`).", ev) == []
    assert lookup_nudges("Delta Person has 2.", ev + [_person_ev("q2", 16, None, field="Interviewer__c")]) == []
    out = with_lookup_footer("Delta Person has taken 2 mock interviews.\n" + SOURCE, ev)
    assert out == ("Delta Person has taken 2 mock interviews.\n"
                   f"Lookup used: `{II}.Recruiter__c` (recruiter). The question's word 'taken' fits "
                   f"`{II}.Interviewer__c` (interviewer): 16 under the same conditions (overlapping, not to be "
                   "added).\n" + SOURCE)


def test_dates_and_the_source_line_do_not_hide_a_nudge():
    check = _check(others=[_other("Interviewer__c", 13, fits="taken", role="interviewer")])
    ev = [_person_ev("q1", 0, check)]
    draft = ("Delta Person has taken 0 mock interviews as recruiter, counted with Scheduled_Date__c from 2026-07-01 to "
             "2026-09-30.\nSource: preprod sandbox copy, synced 2026-10-13 14:31 UTC.")
    assert len(lookup_nudges(draft, ev)) == 1
    assert lookup_nudges(draft.replace("recruiter,", "recruiter (13 as interviewer),"), ev) == []


def test_listing_values_is_no_later_use_of_the_lookup():
    values = _person_ev("q2", 3, None, field="Interviewer__c", tool="get_field_values")
    assert len(lookup_nudges("Delta Person has taken 0 mock interviews.", [_person_ev("q1", 0, _check()), values])) == 1
    later_ref = _person_ev("q2", 3, None, field="Interviewer__c", obj=II, tool="get_field_values")
    assert len(lookup_nudges("Delta Person has taken 0 client interviews.", [_elsewhere_ev(), later_ref])) == 1


def test_elsewhere_nudge_only_when_the_object_does_not_hold_the_person():
    held = _check(obj="Interview__c", others=[_other("Support_Ref__c", 0, role="undocumented")], elsewhere=ELSEWHERE)
    held["all_records"] = {"Recruiter__c": 4}   # the 0 comes from a period: no elsewhere nudge
    assert lookup_nudges("0 client interviews last year.", [_person_ev("q1", 0, held, obj="Interview__c")]) == []
    # the used lookup fits the verb: the elsewhere nudge only when a reference elsewhere fits too
    fits = _check(obj="Interview__c", fits="taken", others=[_other("Support_Ref__c", 0, role="undocumented")],
                  elsewhere=[dict(ELSEWHERE[1])])
    assert lookup_nudges("0 client interviews.", [_person_ev("q1", 0, fits, obj="Interview__c")]) == []
    fits["elsewhere"] = ELSEWHERE
    assert len(lookup_nudges("0 client interviews.", [_person_ev("q1", 0, fits, obj="Interview__c")])) == 1


def test_footer_chooses_checks_by_the_lookups_the_text_names():
    readiness = _check(records=1, field="Interview_Readiness_Person__c", role="undocumented",
                       others=[_other("Interviewer__c", 3, fits="taken", role="interviewer")])
    interviewer = _check(records=3, field="Interviewer__c", role="interviewer", fits="taken",
                         others=[_other("Interview_Readiness_Person__c", 1, role="undocumented")])
    ev = [_person_ev("q1", 1, readiness, field="Interview_Readiness_Person__c"),
          _person_ev("q2", 3, interviewer, field="Interviewer__c", tool="group_records")]
    text = "Delta Person has taken 3 internal interviews as interviewer (`Interviewer__c`): Mock 2, Intake 1.\n" + SOURCE
    assert with_lookup_footer(text, ev) == text            # the abandoned readiness count (1) is never listed
    # a zero with a count above 0 on the same object: no "same person" line
    rec0 = _check()
    ev2 = [_person_ev("q1", 0, rec0), _person_ev("q2", 3, interviewer, field="Interviewer__c")]
    text2 = "Delta Person took 3 as interviewer (`Interviewer__c`); 0 as recruiter (`Recruiter__c`).\n" + SOURCE
    assert with_lookup_footer(text2, ev2) == text2
    # a check of a non-person lookup gets no footer
    niche = dict(_check(records=59, cues=(), others=[]), person_kind="other", field="Niche__c", target="Niche__c")
    assert with_lookup_footer("59 internal interviews.", [_person_ev("q1", 59, niche, field="Niche__c")]) == \
        "59 internal interviews."


def test_the_employee_object_is_not_the_recruiter_lookup():
    ev = [_person_ev("q1", 0, _check())]
    text = "Delta Person (Employee, `Recruiter__c`) has taken 0 mock interviews.\n" + SOURCE
    out = with_lookup_footer(text, ev)
    assert f"Lookup used: `{II}.Recruiter__c` (recruiter)." in out
    for named in ("0 as recruiter (`Recruiter__c`).", "`Recruiter__c` (recruiter): 0.", f"`{II}.Recruiter__c`: 0.",
                  "`Recruiter__c.Name` = 'Delta Person': 0."):
        assert "Lookup used" not in with_lookup_footer(named, ev), named
    # the nudge is not silenced when the draft names only the Employee object
    check = _check(field="Interviewer__c", role="interviewer", others=[_other("Recruiter__c", 2, fits="recruit",
                                                                                role="recruiter")])
    ev = [_person_ev("q1", 0, check, field="Interviewer__c")]
    assert len(lookup_nudges("Delta Person (Employee, `Recruiter__c`) recruited 0.", ev)) == 1
    assert lookup_nudges("Delta Person recruited 2 as recruiter (`Recruiter__c`).", ev) == []


def _strong(words):
    """A spot_people entry: a stored full name with a word that is no design word."""
    return {"words": words, "recs": [("Recruiter__c", "a06000000000005AAA", words.title(), "Active Employee")],
            "strength": "strong", "first_only": False, "asked": None, "close": []}


class _Spotting(StubTools):
    """Stub tools whose name index holds 'delta person' (RECORDS_SPEC §22: the triage reads spot_people)."""

    def spot_people(self, question):
        return [_strong("delta person")] if "delta person" in question.lower() else []

    def names_person(self, question):
        return "delta person" in question.lower()


def test_a_full_name_keeps_a_no_records_route_with_the_record_agent():
    Named = _Spotting
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers."], route="NO_RECORDS")
    rec = RecordSession(Named(), _cfg(), llm=llm).ask("Where does Delta Person appear?")
    assert not rec.routed_out and rec.data_queries == 1
    assert rec.triage["people"] == ["delta person"] and rec.triage["route"] is False and rec.triage["routed"] is True
    rec = RecordSession(Named(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS")).ask("What values can X take?")
    assert rec.routed_out and rec.no_records
    class Broken(StubTools):
        def spot_people(self, question):
            raise RuntimeError("boom")

    rec = RecordSession(Broken(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS")).ask("Where does Delta appear?")
    assert rec.routed_out


def test_review_prompt_texts():
    text = rprompts.render_records_system(STATUS)
    assert "owns / responsible / handles  recruiter or owner (as marked)    -" in text
    assert '"same_person": false counts a different record' in text and '"same_person": false' in rprompts.COMBINE_SYSTEM
    assert "Internal_Interview__c\",\"group_by\":\"<type field>\"" not in text   # the worked example is not the Q1 shape
    assert "Jane Doe" not in rprompts.ROUTE_SYSTEM   # §22: one class sentence for people, no example phrasings
    assert "anything about people named in the question: who they are, their roles" in rprompts.ROUTE_SYSTEM
    assert "not even 0" in rprompts.RECORDS_NOT_FOUND_MESSAGE
    assert "Do not mention those numbers, the tools or this check" in rprompts.RECORDS_UNSUPPORTED_MESSAGE


# ------------------------------------------------------------------------------- eval round 1 fixes (2026-10-14)

def test_strip_reasoning_keeps_the_final_answer():
    working = ("The related parameter filters by children, so I cannot use it here.\nI grouped instead and found "
               "nothing.\n\n" + SOURCE + "\n\n**Answer:**\nDelta Person has 3 evaluations as interviewer.\n\n" + SOURCE)
    assert strip_reasoning(working) == "Delta Person has 3 evaluations as interviewer.\n\n" + SOURCE
    marker = ("Let me check the other lookup first.\nThe success lookup holds the person.\n\n**Answer: 6 mock "
              "interviews**\n\nDelta Person supported 6 mock interviews.\n" + SOURCE)
    assert strip_reasoning(marker) == "6 mock interviews\n\nDelta Person supported 6 mock interviews.\n" + SOURCE
    # unchanged: a normal answer, an answer that opens with "Answer:", one Source line
    plain = "Delta Person took 3 mock interviews (`Interviewer__c`).\n" + SOURCE
    assert strip_reasoning(plain) == plain
    opens = "**Answer:** 3 mock interviews.\n- counted on `Interviewer__c`\n" + SOURCE
    assert strip_reasoning(opens) == opens
    assert strip_reasoning("<think>x</think>\n**Answer:**\n3 mocks.") == "3 mocks."
    lead = ("Now I have both numbers.\nThe verb fits the interviewer.\n\nLet me provide the answer:\n\nDelta Person took 3 "
            "mock interviews.\n" + SOURCE)
    assert strip_reasoning(lead) == "Delta Person took 3 mock interviews.\n" + SOURCE
    assert strip_reasoning("Here is the answer:\nDelta Person took 3.") == "Here is the answer:\nDelta Person took 3."
    assert strip_reasoning(None) == "" and strip_reasoning("<think>only</think>") == ""


def test_session_answer_and_merge_drop_working_text():
    working = "I need to check the lookup.\n**Final answer:** 37 client interviews ended with an offer.\n" + SOURCE_56
    script = [{"name": "count_records", "args": COUNT_ARGS}, working]
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask("How many interviews ended with an offer?")
    assert rec.answer == "37 client interviews ended with an offer.\n" + SOURCE_56
    m = combine(ScriptedLLM(["Thinking aloud.\nAnswer: 37 offers.\n" + SOURCE_56]), "q?", "K", _rec(), STATUS)
    assert m.merged and m.text == "37 offers.\n" + SOURCE_56


def _same_name_check(records=7, own_fits="take"):
    """A candidate record named like an employee: the count used Candidate__c; the Employee with the same name holds
    Interviewer__c (a different record, group same_name)."""
    emp = _other("Interviewer__c", 3, group="same_name", role="interviewer")
    emp.update({"same_person": False, "own_kind": "employee", "own_fits": own_fits})
    lc = _check(records=records, field="Candidate__c", role="candidate", fits="take", cues=("take",), others=[emp])
    lc.update({"target": "Account", "target_label": "Account", "person_kind": "candidate"})
    return lc


def test_same_name_employee_nudge_for_a_count_on_a_candidate_record():
    session, _ = _session([_person_ev("q1", 2, _same_name_check(records=2), field="Candidate__c")])
    msg = session.check_draft("Delta Person (as a candidate) took 2 mock interviews.", PERSON_Q, STATUS)
    expected = (rprompts.RECORDS_SAME_NAME_EMPLOYEE_MESSAGE.replace("<<FIELD>>", "Candidate__c")
                .replace("<<ROLE>>", "candidate").replace("<<COUNT>>", "2").replace("<<OBJECT>>", II)
                .replace("<<OTHERS>>", "Interviewer__c (interviewer) 3").replace("<<WORD>>", "take")
                .replace("<<OTHER_FIELD>>", "Interviewer__c"))
    assert msg == expected and "<<" not in msg and "Never add" in msg
    # not when the draft names the employee's lookup, when a later query used it, without own_fits, or for an
    # employee check (the employee is the default reading)
    assert session.check_draft("As interviewer (`Interviewer__c`) 3; as a candidate 2.", PERSON_Q, STATUS) is None
    session, _ = _session([_person_ev("q1", 2, _same_name_check(records=2), field="Candidate__c"),
                           _person_ev("q2", 3, None, field="Interviewer__c")])
    assert session.check_draft("Delta Person took 2 mock interviews.", PERSON_Q, STATUS) is None
    session, _ = _session([_person_ev("q1", 2, _same_name_check(records=2, own_fits=None), field="Candidate__c")])
    assert session.check_draft("Delta Person took 2 mock interviews.", PERSON_Q, STATUS) is None
    emp_check = _same_name_check(records=2)
    emp_check["person_kind"] = "employee"
    assert lookup_nudges("Delta Person took 2 mock interviews.", [_person_ev("q1", 2, emp_check)]) == []


def _unsynced_ev(qid="q1", count=0):
    ev = _person_ev(qid, count, None, field="Internal_Interview__c.Interviewer__c", obj="Interview_Evaluation__c")
    ev.model_result = dict(ev.model_result, unsynced_fits=[{
        "field": "Evaluator__c", "role": "evaluator employee", "target": "Recruiter__c", "target_label": "Employee",
        "fits": "evaluations", "proxy": ["Internal_Interview__c.Interviewer__c"]}])
    return ev


def test_unsynced_lookup_nudge_footer_and_field_check():
    ev = [_unsynced_ev(), _unsynced_ev("q2")]
    assert [(x["object"], x["field"], x["query_id"]) for x in unsynced_checks(ev)] == \
        [("Interview_Evaluation__c", "Evaluator__c", "q1")]                        # once per (object, field)
    session, _ = _session(ev)
    msg = session.check_draft("Delta Person has done 0 interview evaluations.", "how many evaluations has delta done",
                              STATUS)
    assert msg == (rprompts.RECORDS_NOT_SYNCED_MESSAGE.replace("<<WORD>>", "evaluations")
                   .replace("<<OBJECT>>", "Interview_Evaluation__c").replace("<<FIELD>>", "Evaluator__c")
                   .replace("<<ROLE>>", "evaluator employee").replace("<<PROXY>>", "Internal_Interview__c.Interviewer__c"))
    ok = ("The evaluator (`Evaluator__c`) is not recorded in the warehouse; as a proxy, 0 evaluations of interviews "
          "Delta Person conducted.")
    assert session.check_draft(ok, "q", STATUS) is None
    # footer and FIELD CHECK (no knowledge answer needed)
    out = with_lookup_footer("Delta Person has done 0 interview evaluations.\n" + SOURCE, ev)
    assert ("Not in the warehouse: `Interview_Evaluation__c.Evaluator__c` (evaluator employee), which the question's "
            "word 'evaluations' fits, is documented but not synced") in out
    assert out.endswith("\n" + SOURCE) and with_lookup_footer(ok, ev) == ok
    checks = field_checks("", ev)
    assert len(checks) == 1 and checks[0]["kind"] == "unsynced" and checks[0]["other_field"] == "Evaluator__c"
    assert "not synced to the warehouse: the data does not record it" in checks[0]["line"]
    assert "proxy" in checks[0]["line"]


PEOPLE_FIRST = [{"words": "echo", "names": ["Echo One", "Echo Two"], "first_only": True, "asked": None}]
PEOPLE_ASKED = [{"words": "delta persn", "names": ["Delta Person"], "first_only": False, "asked": "delta persn"}]


def test_people_nudges():
    msg = people_nudges("Echo One took 1 mock interview.", PEOPLE_FIRST)
    assert msg == [rprompts.RECORDS_SEVERAL_PEOPLE_MESSAGE.replace("<<WORD>>", "echo")
                   .replace("<<NAMES>>", "'Echo One', 'Echo Two'").replace("<<USED>>", "'Echo One'")]
    assert people_nudges("**Echo One** 1, **Echo Two** 0.", PEOPLE_FIRST) == []          # each person answered
    assert people_nudges("Which Echo do you mean: Echo One or Echo Two?", PEOPLE_FIRST) == []
    assert people_nudges("Which Echo do you mean?", PEOPLE_FIRST) == []                  # names none: asks
    one = [{"words": "delta", "names": ["Delta Person"], "first_only": True, "asked": None}]
    assert people_nudges("Delta Person took 3.", one) == []                               # one person only
    msg = people_nudges("Delta Person took 3 mock interviews.", PEOPLE_ASKED)
    assert msg == [rprompts.RECORDS_NOT_EXACT_NAME_MESSAGE.replace("<<ASKED>>", "delta persn")
                   .replace("<<USED>>", "'Delta Person'")]
    said = "No employee is named 'Delta Persn'; the closest stored name, Delta Person, took 3 mock interviews."
    assert people_nudges(said, PEOPLE_ASKED) == []
    assert people_nudges("x", None) == [] and people_nudges("x", [{"bad": 1}]) == []


def test_check_draft_uses_the_tools_question_people():
    class PeopleTools(LookupTools):
        def question_people(self):
            return PEOPLE_FIRST

    tools = PeopleTools([_person_ev("q1", 1, None, field="Interviewer__c")])
    session = RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"]))
    msg = session.check_draft("Echo One took 1 mock interview.\n" + SOURCE_56, "how many mocks did echo take", STATUS)
    assert msg is not None and "only a first name" in msg
    # tools without question_people (or failing): no people check
    session, _ = _session([_person_ev("q1", 1, None, field="Interviewer__c")])
    assert session.check_draft("Echo One took 1 mock interview.", "q", STATUS) is None


def test_merge_gets_more_facts_and_a_rule_for_trimmed_lists():
    big = _person_ev("q1", 3, None)
    big.model_result = dict(big.model_result, references=[{"record": "R1", "object": f"Obj_{i}__c", "field": "F__c",
                                                           "role": "interviewer", "records": i} for i in range(120)])
    size = len(facts_json([big], 10**9))
    assert 8000 < size < MERGE_FACTS_CHARS
    llm = ScriptedLLM(["Delta Person appears in many places.\n" + SOURCE])
    combine(llm, "where does delta person appear", "K", _rec(answer="x", evidence=[big]), STATUS)
    user = llm.calls[0]["messages"][1]["content"]
    assert '"trimmed": true' not in user and "Obj_119__c" in user
    assert '"trimmed": true shows only part of its lists' in rprompts.COMBINE_SYSTEM


def test_round1_prompt_rules():
    text = rprompts.render_records_system(STATUS)
    assert "<<" not in text
    assert "RecordTypeId (the Salesforce record type) only when the question says record type" in text
    assert "answer for each of them (one\n   query: the lookup with op in and their full names" in text
    assert "answer for the Employee unless the question says candidate" in text
    assert '("training\n   sessions") is a condition' in text
    assert "not only the records you counted" in text and "not creator or owner" in text
    for msg, keys in ((rprompts.RECORDS_SAME_NAME_EMPLOYEE_MESSAGE, ("FIELD", "ROLE", "COUNT", "OBJECT", "OTHERS", "WORD",
                                                                     "OTHER_FIELD")),
                      (rprompts.RECORDS_NOT_SYNCED_MESSAGE, ("WORD", "OBJECT", "FIELD", "ROLE", "PROXY")),
                      (rprompts.RECORDS_SEVERAL_PEOPLE_MESSAGE, ("WORD", "NAMES", "USED")),
                      (rprompts.RECORDS_NOT_EXACT_NAME_MESSAGE, ("ASKED", "USED"))):
        for k in keys:
            assert f"<<{k}>>" in msg
            msg = msg.replace(f"<<{k}>>", "x")
        assert "<<" not in msg


def test_working_notes_written_as_the_reply_are_sent_back():
    notes = ("The result source says the object synced at 14:31. I will use that.\n\nWait, the prompt says newest sync.\n"
             "Final check of the numbers:\n- Completed: 4\n\nI will write the response.\n\nOne detail: so all")
    assert working_notes(notes)
    for ok_text in ("Delta Person took 3 mock interviews (`Interviewer__c`).\n" + SOURCE,
                    "I will not count the legacy field.\n- **Source**: preprod sandbox copy, synced 2026-10-13 14:12 UTC.",
                    "Delta Person took 3 mock interviews.", ""):
        assert not working_notes(ok_text), ok_text
    session, _ = _session([_person_ev("q1", 3, None, field="Interviewer__c")])
    assert session.check_draft(notes, PERSON_Q, STATUS).startswith(rprompts.RECORDS_FINAL_FORM_MESSAGE)
    assert session.check_draft("Delta Person took 3 mock interviews (`Interviewer__c`).\n" + SOURCE_56, PERSON_Q,
                               STATUS) is None


def test_footer_lists_every_role_the_answer_reports_when_it_names_no_lookup():
    cand = _same_name_check(records=2)
    emp = _check(records=3, field="Interviewer__c", role="interviewer", fits="taken",
                 others=[_other("Recruiter__c", 0, role="recruiter")])
    ev = [_person_ev("q1", 3, emp, field="Interviewer__c"), _person_ev("q2", 2, cand, field="Candidate__c")]
    text = "As interviewer, Delta Person took 3 mock interviews; as the candidate, a different record took 2.\n" + SOURCE
    out = with_lookup_footer(text, ev)
    assert f"Lookup used: `{II}.Interviewer__c` (interviewer); `{II}.Candidate__c` (candidate)." in out
    # no role word in the text: the last check, as before
    assert f"Lookup used: `{II}.Candidate__c` (candidate)." in with_lookup_footer("Delta Person: 3 and 2.", ev)



# ------------------------------------------------------------------------------- eval round 2 fixes (2026-10-14)

def _search_ev(qid, matches=()):
    result = {"query_id": qid, "match_type": "exact", "matches": list(matches), "objects_searched": 4,
              "truncated": False, "source": "preprod sandbox copy"}
    ev = QueryEvidence(query_id=qid, tool="search_records", kind="data", object=None, arguments={"text": "Nobody Here"},
                       ok=True, error=None, applied={}, notes=[], statements=[], tables=[], synced_at={},
                       summary=f"{len(matches)} matches", model_result=result, elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, []
    return ev


def test_a_search_that_matched_nothing_does_not_defeat_the_not_found_check():
    q = "How many mock interviews has Nobody Here conducted?"
    zero = "0 internal interviews: there is no Employee record named 'Nobody Here'.\n" + SOURCE_56
    session, _ = _session([_error_ev("q1", "E4h"), _search_ev("q2")])
    assert session.check_draft(zero, q, STATUS) == rprompts.RECORDS_NOT_FOUND_MESSAGE
    clean = "No Employee record is named 'Nobody Here', so no count can be given.\n" + SOURCE_56
    assert session.check_draft(clean, q, STATUS) is None
    # ask(): the reply with a 0 is refused, the number-free one kept
    script = [{"name": "count_records", "args": {"object": II}}, {"name": "count_records", "args": {"object": II}}]
    for reply, kept in ((zero, False), (clean, True)):
        rec = RecordSession(_not_found_tools([_error_ev("q1", "E4h"), _search_ev("q2")]), _cfg(),
                            llm=ScriptedLLM(script + [reply])).ask(q)
        assert (rec.answer == reply) is kept and rec.no_records is not kept, reply
    # a search that found other records sharing a word of the name supports no count either
    session, _ = _session([_error_ev("q1", "E4h"), _search_ev("q2", [{"object": "Account", "name": "Nobody Else"}])])
    assert session.check_draft(zero, q, STATUS) == rprompts.RECORDS_NOT_FOUND_MESSAGE
    # a counting query after the error is data: the usual number checks apply
    session, _ = _session([_error_ev("q1", "E4h"), _person_ev("q2", 0, None, field="Interviewer__c")])
    assert session.check_draft(zero, q, STATUS) is None


def test_footer_leaves_out_objects_the_answer_does_not_report():
    attendee = dict(_check(obj="Session_Attendee__c", field="Attendee_Recruiter__c", role="attending employee",
                           others=[]), records=0)
    support = _check(records=4, obj="Interview__c", field="Interview_Support_Person__c", role="interview support person",
                     fits="supported", others=[_other("Recruiter__c", 0, role="recruiter")])
    ev = [_person_ev("q1", 0, attendee, field="Attendee_Recruiter__c", obj="Session_Attendee__c"),
          _person_ev("q2", 4, support, field="Interview_Support_Person__c", obj="Interview__c")]
    text = "Delta Person supported 4 client interviews (`Interview_Support_Person__c`).\n" + SOURCE
    assert with_lookup_footer(text, ev) == text                  # Session_Attendee__c is not what the answer reports
    text2 = "Delta Person supported 4 client interviews; 0 rows in `Session_Attendee__c`.\n" + SOURCE
    assert "Lookup used: `Session_Attendee__c.Attendee_Recruiter__c` (attending employee); " in with_lookup_footer(text2, ev)
    # a count above 0 of the object in the text selects its last check too
    text3 = "Delta Person supported 4 client interviews.\n" + SOURCE
    assert with_lookup_footer(text3, ev[1:2] + ev[:1]).count("Lookup used:") == 1
    assert "`Interview__c.Interview_Support_Person__c`" in with_lookup_footer(text3, ev)
    assert "Session_Attendee__c" not in with_lookup_footer(text3, ev)


def _named_check(name, records, field="Interview_Support_Person__c", others=None):
    lc = _check(records=records, field=field, role="support employee", fits="supported" if records else None,
                cues=("supported",), others=others or [])
    lc["filter"] = f"{field}.Name = '{name}'"
    return lc


def test_one_check_per_person_and_the_footer_says_for_whom():
    one = _named_check("Echo One", 3, others=[_other("Recruiter__c", 2, role="recruiter")])
    two = _named_check("Echo Two", 0, others=[_other("Recruiter__c", 5, role="recruiter")])
    ev = [_person_ev("q1", 3, one, field="Interview_Support_Person__c"),
          _person_ev("q2", 0, two, field="Interview_Support_Person__c")]
    text = "Echo One supported 3 mock interviews; Echo Two 0 (`Interview_Support_Person__c`).\n" + SOURCE
    out = with_lookup_footer(text, ev)
    assert (f"Same person through other lookups of {II} for 'Echo Two' (overlapping, not to be added): "
            "`Recruiter__c` (recruiter) 5.") in out
    assert "`Recruiter__c` (recruiter) 2" not in out          # Echo One counted above 0: no line for that person
    # the nudges see each person's own check: Echo Two's 0 with a recruiter count is no nudge (the support role fits)
    assert [c["filter"] for c in lookup_checks(ev)] == [one["filter"], two["filter"]]


def _linked_check(records=0):
    lc = _check(records=records, field="Candidate__c", role="candidate", cues=(), obj="Application__c", others=[
        dict(_other("Recruiter__c", 7, group="linked", role="recruiter"), via="Recruiter_Contact__c (employee account)",
             same_person=True)])
    lc.update({"target": "Account", "target_label": "Account", "person_kind": "other"})
    return lc


def test_linked_nudge_for_an_employee_account_matched_through_a_candidate_lookup():
    ev = [_person_ev("q1", 0, _linked_check(), field="Candidate__c", obj="Application__c")]
    draft = ("Delta Person has made **0** applications as a candidate.\n- As recruiter (`Recruiter__c`) Delta Person "
             "has 7 applications.\n" + SOURCE)
    msgs = lookup_nudges(draft, ev)
    assert msgs == [rprompts.RECORDS_LINKED_LOOKUP_MESSAGE.replace("<<FIELD>>", "Candidate__c")
                    .replace("<<ROLE>>", "candidate").replace("<<VIA>>", "Recruiter_Contact__c (employee account)")
                    .replace("<<OBJECT>>", "Application__c").replace("<<OTHERS>>", "Recruiter__c (recruiter) 7")]
    assert "<<" not in msgs[0]
    assert lookup_nudges("Delta Person made 7 applications as recruiter (`Recruiter__c`); 0 as a candidate.", ev) == []
    assert lookup_nudges(draft, ev + [_person_ev("q2", 7, None, field="Recruiter__c", obj="Application__c")]) == []
    # the footer gives the linked count with its link
    out = with_lookup_footer("Delta Person has made 0 applications.\n" + SOURCE, ev)
    assert ("Same person through other lookups of Application__c (overlapping, not to be added): `Recruiter__c` "
            "(recruiter; via Recruiter_Contact__c (employee account)) 7.") in out
    # an employee check never nudges for linked lookups (spec §21.10); a niche check without linked lookups gets nothing
    emp = dict(_linked_check(), person_kind="employee")
    assert lookup_nudges(draft, [_person_ev("q1", 0, emp, field="Candidate__c", obj="Application__c")]) == []


def test_a_zero_that_leads_while_the_fitting_count_is_a_side_note_is_sent_back():
    ev = [_person_ev("q1", 0, _check())]
    side = "Delta Person has taken 0 mock interviews.\n- As interviewer (`Interviewer__c`) the count is 3.\n" + SOURCE
    msg = lookup_nudges(side, ev)
    assert len(msg) == 1 and msg[0].startswith("Your count used Recruiter__c (recruiter) and found 0, but the "
                                               "question's word 'taken' fits another lookup")
    assert "lead with its count" in msg[0] and "may follow only as a side note" in msg[0]
    # with no role word the model decides: the choice form, plus the lead-zero note for a side-note count
    nocue = [_person_ev("q1", 0, _check(cues=(), others=[_other("Interviewer__c", 3, role="interviewer")]))]
    msg = lookup_nudges(side, nocue)
    assert len(msg) == 1 and msg[0].endswith(rprompts.RECORDS_LEAD_ZERO_NOTE)
    for ok_draft in ("Delta Person took 3 mock interviews as interviewer (`Interviewer__c`); 0 as recruiter.",
                     "Delta Person has 0 as recruiter; `Interviewer__c` holds 3.",   # the first sentence names it
                     "**Summary:**\nDelta Person took 3 (`Interviewer__c`); 0 as recruiter.\n" + SOURCE):
        assert lookup_nudges(ok_draft, ev) == [], ok_draft


def _unfit_check(**kw):
    others = [_other("Interview_Success_Person__c", 6, role="undocumented"),
              dict(_other("Interview_Support_Person__c", 0, role="support employee"), fits_sparse="supported"),
              _other("Recruiter__c", 0, role="recruiter")]
    lc = _check(field="Interviewer__c", role="interviewer", cues=("supported",), others=others)
    lc.update(kw)
    return lc


def test_unfit_zero_nudge_for_a_zero_through_a_role_the_question_does_not_ask():
    q = "how many mock interviews has delta person supported"
    ev = [_person_ev("q1", 0, _unfit_check(), field="Interviewer__c")]
    draft = f"Delta Person supported **0** mock interviews (`{II}`).\n- The success lookup holds 6.\n" + SOURCE
    msgs = lookup_nudges(draft, ev)
    expected = (rprompts.RECORDS_UNFIT_ZERO_MESSAGE.replace("<<FIELD>>", "Interviewer__c").replace("<<ROLE>>", "interviewer")
                .replace("<<WORD>>", "supported")
                .replace("<<SPARSE>>", " The lookup whose role fits it, Interview_Support_Person__c (support employee), is "
                                       f"filled on almost no {II} records.")
                .replace("<<OBJECT>>", II).replace("<<OTHERS>>", "Interview_Success_Person__c (undocumented) 6"))
    assert msgs == [expected] and "<<" not in expected
    session, _ = _session(ev)
    assert session.check_draft(draft, q, STATUS) == expected
    no_fire = {
        "lead has the count": ("Delta Person supported 0 as support person; 6 through `Interview_Success_Person__c`.", ev),
        "used lookup fits": (draft, [_person_ev("q1", 0, _unfit_check(fits="supported"))]),
        "no role word": (draft, [_person_ev("q1", 0, _unfit_check(cues=[]))]),
        "an other fits": (draft, [_person_ev("q1", 0, _unfit_check(others=[_other("Interview_Success_Person__c", 6,
                                                                                   fits="supported")]))]),
        "lead about another object": ("Delta Person supported 0 rows of `Session__c`.",
                                      ev + [_person_ev("q2", 0, _check(obj="Session__c", others=[]), obj="Session__c")]),
        "lead without a 0": ("Delta Person appears in 6 mock interviews.", ev),
    }
    for case, (text, evidence) in no_fire.items():   # neither the unfit-zero nor the sparse-zero message
        assert not any(m.startswith("Your answer leads with 0") for m in lookup_nudges(text, evidence)), case
    # the used lookup is the sparse support lookup itself: the sparse-zero nudge's message, not this one
    sparse = _unfit_check(field="Interview_Support_Person__c", role="support employee", fits_sparse="supported",
                          others=[_other("Interview_Success_Person__c", 6, role="undocumented"),
                                  _other("Interviewer__c", 0, role="interviewer")])
    msgs = lookup_nudges(draft, [_person_ev("q1", 0, sparse, field="Interview_Support_Person__c")])
    assert len(msgs) == 1 and msgs[0].startswith("Your answer leads with 0 through Interview_Support_Person__c "
                                                 "(support employee). Its role fits the question's word 'supported'")


def test_overlap_nudge_for_made_up_relations_between_counts():
    check = _check(records=11, field="Interview_Readiness_Person__c", role="readiness person", fits=None, cues=(),
                   others=[_other("Interviewer__c", 15, role="interviewer"), _other("Recruiter__c", 4, role="recruiter")])
    ev = [_person_ev("q1", 11, check, field="Interview_Readiness_Person__c")]
    bad = ("Delta Person is the readiness person on 11 mock interviews (`Interview_Readiness_Person__c`).\n- These 15 "
           "interviewer records include the 11 readiness interviews plus 4 additional ones.\n" + SOURCE)
    msg = overlap_nudges(bad, ev)
    assert msg == [rprompts.RECORDS_OVERLAP_MESSAGE.replace(
        "<<CLAUSE>>", "- These 15 interviewer records include the 11 readiness interviews plus 4 additional ones.")]
    assert overlap_nudges("Delta Person is also the recruiter on 4 of these interviews.", ev)
    assert overlap_nudges("Interviewer 11 + 15 in all.", ev)
    for ok_text in ("Delta Person: 11 as readiness person; 15 as interviewer (`Interviewer__c`); 4 as recruiter.",
                    "Of these 11, none were cancelled.",     # 11 is the result's own count
                    ""):
        assert overlap_nudges(ok_text, ev) == [], ok_text
    # a count that is also a number of the result itself (a group) is no side count
    grouped = _person_ev("q1", 11, check, field="Interview_Readiness_Person__c")
    grouped.model_result = dict(grouped.model_result, groups=[{"group": "Completed", "value": 4}])
    assert overlap_nudges("11 readiness interviews, including 4 Completed.", [grouped]) == []
    session, _ = _session(ev)
    assert rprompts.RECORDS_OVERLAP_MESSAGE.split("<<")[0] in session.check_draft(bad, PERSON_Q, STATUS)


def _trend_ev(qid="q2", date_field="Offer_Letter_Date__c", total=56, nodate=26, measure=None):
    result = {"query_id": qid, "object": "Interview__c", "grain": "month", "date_field": date_field,
              "buckets": [{"bucket": "2026-01", "value": total}], "total": total, "records_without_date": nodate,
              "applied": {"filters": ["Interview_Outcome__c = 'Offer Received'"]}, "notes": [],
              "source": "preprod sandbox copy; Interview__c synced 2026-10-13 14:12 UTC"}
    if measure:
        result["measure"] = measure
    ev = QueryEvidence(query_id=qid, tool="trend_records", kind="data", object="Interview__c", arguments={}, ok=True,
                       error=None, applied=result["applied"], notes=[], statements=[], tables=["Interview__c"],
                       synced_at={}, summary="1 buckets", model_result=result, elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, [("Interview__c", date_field)]
    return ev


def _dates_ev(qid="q1"):
    ev = _describe_evidence(qid)
    ev.model_result = dict(ev.model_result, date_fields=[{"field": "Date_of_Interview__c"},
                                                         {"field": "Offer_Letter_Date__c"}])
    return ev


def test_date_coverage_nudge_for_a_trend_on_a_mostly_blank_date():
    ev = [_dates_ev(), _trend_ev()]
    msg = date_nudges(ev)
    assert msg == [rprompts.RECORDS_DATE_COVERAGE_MESSAGE.replace("<<FIELD>>", "Offer_Letter_Date__c")
                   .replace("<<MISSING>>", "26")
                   .replace("<<MAIN>>", "Date_of_Interview__c (the first of Interview__c's date_fields)")]
    assert "<<" not in msg[0]
    assert "the first of Interview__c's date_fields in describe_fields" in date_nudges([_trend_ev()])[0]
    for case, evidence in {"few undated": [_dates_ev(), _trend_ev(nodate=1)],
                           "under 10%": [_dates_ev(), _trend_ev(total=300, nodate=20)],
                           "the main date": [_dates_ev(), _trend_ev(date_field="Date_of_Interview__c")],
                           "a later trend on the main date": [_dates_ev(), _trend_ev(), _trend_ev("q3", "Date_of_Interview__c",
                                                                                                  82, 1)],
                           "a sum": [_dates_ev(), _trend_ev(measure="sum")]}.items():
        assert date_nudges(evidence) == [], case
    session, _ = _session(ev)
    assert session.check_draft("56 offers this year.\n" + SOURCE_56, "offers per month", STATUS) == msg[0]


def test_strip_reasoning_cuts_after_a_planning_line_that_ends_with_a_period():
    working = ("The question could mean two objects.\nI should probably report both.\n\nLet me provide both answers:\n\n"
               "- client 12\n- internal 3\n\nLet me format the answer clearly.\n\nDelta Person has 12 client interviews "
               "and 3 internal interviews.\n" + SOURCE)
    assert strip_reasoning(working) == "Delta Person has 12 client interviews and 3 internal interviews.\n" + SOURCE
    present = ("**Internal (as interviewer):** 3\n**Client (as recruiter):** 12\n\nLet me present this clearly:\n\n"
               "Delta Person has 3 internal and 12 client interviews.\n" + SOURCE)
    assert strip_reasoning(present) == "Delta Person has 3 internal and 12 client interviews.\n" + SOURCE
    listing = "Delta Person took 3 mock interviews.\n\nI'll list the candidates below:\n- Alpha\n- Beta\n" + SOURCE
    assert strip_reasoning(listing) == listing          # no planning before it and no repeated draft: kept
    # "Let me know ..." is no planning; "Here's the summary:" after a plain answer sentence cuts nothing
    for keep in ("Delta Person took 3 mock interviews.\nLet me know if you need the full answer.\n" + SOURCE,
                 "Delta Person took 3 mock interviews.\n\nHere's the answer:\n- Completed 2\n" + SOURCE,
                 # a planning line with a period but no whole answer after it (cut off): left for the working-notes check
                 "I will check.\nLet me write the answer.\nDelta Person took"):
        assert strip_reasoning(keep) == keep, keep
    assert working_notes("I should report both. Let me check the counts.\n- 3 interviews\n" + SOURCE)
    assert not working_notes("Delta Person took 3 mock interviews. Let me know if you need more.\n" + SOURCE)


def _merge_rec(answer, evidence=None):
    return _rec(answer=answer, evidence=[_person_ev("q1", 7, _check(records=7, field="Interviewer__c", role="interviewer",
                                                                      fits="taken"), field="Interviewer__c")]
                if evidence is None else evidence)


def test_merge_scope_issues_and_combine_falls_back_to_the_record_answer():
    rec = _merge_rec("Delta Person took 7 mock interviews as interviewer (`Interviewer__c`): Completed 4.\n" + SOURCE)
    invented = ("Delta Person took 7 mock interviews.\n- Counted via `Interviewer__c` on records where "
                "`Candidate_Training__c` is populated (training mocks).\n" + SOURCE)
    assert merge_scope_issues(invented, rec) == ["a condition on Candidate_Training__c that no query applied"]
    relabelled = "Delta Person supported 495 mock interviews.\n" + SOURCE
    assert merge_scope_issues(relabelled, rec) == ["lead numbers 495 that the record answer does not give"]
    for fine in ("Delta Person took 7 mock interviews (Completed 4) as interviewer (`Interviewer__c`).\n" + SOURCE,
                 "**Answer**\nDelta Person took 7 mock interviews, with `Interviewer__c` = 'Delta Person'.\n" + SOURCE,
                 "Mock interviews are `Internal_Interview__c` records.\nDelta Person took 7.\n" + SOURCE,
                 "Delta Person took 7 mock interviews in 2026-10 (`Interviewer__c`).\n" + SOURCE):
        assert merge_scope_issues(fine, rec) == [], fine
    # a FIELD CHECK count may lead; a condition on a field that a query used (fields_used) or the record answer names
    assert merge_scope_issues("Delta Person took 9 as interviewer.\n" + SOURCE, rec, "q",
                              ["... With the same conditions, Interviewer__c has 9 (RECORD FACTS, query q1)."]) == []
    assert merge_scope_issues("7 interviews where `Interviewer__c` is Delta Person.\n" + SOURCE, rec) == []
    m = combine(ScriptedLLM([invented]), PERSON_Q, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.text == rec.answer and m.rejected_numbers == []
    assert m.error == "the merged answer states a condition on Candidate_Training__c that no query applied"
    m = combine(ScriptedLLM(["Delta Person took 7 mock interviews (`Interviewer__c`).\n" + SOURCE]), PERSON_Q, KNOWLEDGE,
                rec, STATUS)
    assert m.merged


def test_round2_prompt_rules():
    text = rprompts.render_records_system(STATUS)
    assert "<<" not in text
    assert 'also for an outcome or status\n   reached on the record ("offers received", "completed")' in text
    assert "records_without_date" in text
    assert "A lookup with mirror_of copies that path: group or filter by the path" in text
    assert "made (applications)           recruiter or submitter (as marked) candidate (applicant)" in text
    assert "is no candidate: count\n   through the employee's lookup" in text
    assert ('When the question says only "interviews", has no role word,\n   and the hints show the person (the same '
            'record) referenced on both objects, count both') in text
    combine_text = rprompts.COMBINE_SYSTEM
    assert "Never add a filter, condition, subset or discriminator that no query applied" in combine_text
    assert "Interview__c = client interviews; Internal_Interview__c = internal mock interviews" in combine_text
    assert "Lead with what the RECORD ANSWER leads with (the numbers; for people" in combine_text
    for msg, keys in ((rprompts.RECORDS_LINKED_LOOKUP_MESSAGE, ("FIELD", "ROLE", "VIA", "OBJECT", "OTHERS")),
                      (rprompts.RECORDS_UNFIT_ZERO_MESSAGE, ("FIELD", "ROLE", "WORD", "SPARSE", "OBJECT", "OTHERS")),
                      (rprompts.RECORDS_OVERLAP_MESSAGE, ("CLAUSE",)),
                      (rprompts.RECORDS_DATE_COVERAGE_MESSAGE, ("FIELD", "MISSING", "MAIN"))):
        for k in keys:
            assert f"<<{k}>>" in msg
            msg = msg.replace(f"<<{k}>>", "x")
        assert "<<" not in msg


class _SequenceLLM(ScriptedLLM):
    """Record-agent replies in call order (route calls kept apart)."""

    def chat(self, messages, tools=None, max_tokens=None, **kw):
        if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
            return super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": {}}
        return {"content": step, "tool_calls": [], "usage": {}}


def test_no_records_for_a_named_person_routed_to_records_is_sent_back_once():
    Named = _Spotting
    q = "How many interview evaluations has Delta Person done?"
    script = ["NO_RECORDS", "NO_RECORDS", {"name": "count_records", "args": COUNT_ARGS},
              "37 interviews as a proxy.\n" + SOURCE_56]
    llm = _SequenceLLM(script)
    rec = RecordSession(Named(), _cfg(), llm=llm).ask(q)
    assert rec.data_queries == 1 and rec.answer.startswith("37 interviews as a proxy")
    pushback = rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "'delta person'")
    assert llm.calls[1]["messages"][-1]["content"] == \
        rprompts.RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE.replace("<<PEOPLE>>", "'delta person'")
    assert llm.calls[2]["messages"][-1]["content"] == pushback
    assert rec.nudges == [pushback]      # kept for the JSONL (record_nudges)
    # without a triage (direct callers): the full name and a RECORDS route; else NO_RECORDS stands
    session = RecordSession(Named(), _cfg(), llm=ScriptedLLM(["x"]))
    assert session.check_draft("NO_RECORDS", q, STATUS, routed=True) == \
        rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "a stored person's full name")
    for question, routed in ((q, None), (q, False), ("How many evaluations has Nobody done?", True)):
        assert session.check_draft("NO_RECORDS", question, STATUS, routed=routed) is None, (question, routed)
    rec = RecordSession(StubTools(), _cfg(), llm=_SequenceLLM(["NO_RECORDS"])).ask(q)
    assert rec.no_records and not rec.routed_out


def test_an_object_named_lookup_is_named_by_its_own_name_word_or_a_lookup_label():
    check = dict(_check(field="Recruiter__c", role="undocumented", others=[]), records=7)
    ev = [_person_ev("q1", 7, check, field="Recruiter__c", obj="Application__c")]
    for named in ("Delta Person made 7 applications as a recruiter (`Recruiter__c`).",
                  "Delta Person made 7 applications.\n- **Lookup:** `Recruiter__c` (recruiter role)",
                  "7 applications through the `Recruiter__c` lookup."):
        assert "Lookup used" not in with_lookup_footer(named, ev), named
    for not_named in ("Delta Person (Employee, `Recruiter__c`) made 7 applications.",
                      "Delta Person (`Recruiter__c`) recruited 7."):
        assert "Lookup used" in with_lookup_footer(not_named, ev), not_named


def test_merge_that_claims_an_absence_on_an_object_no_query_counted_is_rejected():
    interviewer = _check(records=12, field="Interviewer__c", role="interviewer", fits=None, cues=(), others=[])
    rec = _merge_rec("Delta Person has 12 internal interviews (`Interviewer__c`).\n" + SOURCE,
                     evidence=[_describe_evidence("q1"), _person_ev("q2", 12, interviewer, field="Interviewer__c")])
    bad = ("Delta Person has 12 internal interviews (`Interviewer__c`).\n- `Interview__c` is a different object; "
           "Delta Person is not linked to client interviews (`Interview__c`).\n" + SOURCE)
    assert merge_scope_issues(bad, rec) == ["an absence on Interview__c, which no query counted"]
    for fine in ("Delta Person has 12 internal interviews (`Interviewer__c`).\n- `Interview__c` holds client "
                 "interviews, a different object.\n" + SOURCE,
                 f"Delta Person has 12 internal interviews; none are cancelled (`{II}`).\n" + SOURCE):
        assert merge_scope_issues(fine, rec) == [], fine
    # the record answer makes the same claim, a find_references scan ran, or a 0 check may have scanned: no issue
    same = _merge_rec("Delta Person has 12 internal interviews; not linked to `Interview__c`.\n" + SOURCE,
                      evidence=rec.evidence)
    assert merge_scope_issues(bad, same) == []
    zero = _merge_rec(rec.answer, evidence=[_describe_evidence("q1"), _person_ev("q2", 0, _check(), field="Recruiter__c")])
    assert merge_scope_issues(bad.replace("12", "0"), zero) == []


def _group_ev(qid, group_by, copied=None, obj="Candidate_Training__c"):
    result = {"query_id": qid, "object": obj, "grouped_by": group_by, "groups": [{"group": "Program Alpha", "value": 1}],
              "applied": {}, "notes": [], "source": "preprod sandbox copy"}
    if copied:
        result["copied_lookups"] = copied
    ev = QueryEvidence(query_id=qid, tool="group_records", kind="data", object=obj, arguments={}, ok=True, error=None,
                       applied={}, notes=[], statements=[], tables=[obj], synced_at={}, summary="1 groups",
                       model_result=result, elapsed_ms=1.0)
    ev.error_code = None
    ev.fields_used = [(obj, part) for part in group_by.split(".")[:1]] + (
        [("Program_Version__c", "Program__c")] if group_by.startswith("Program_Version__c.") else [])
    return ev


def test_mirror_nudge_for_a_query_through_a_lookup_that_copies_a_path():
    copied = [{"field": "Program__c", "source": "Program_Version__c.Program__c"}]
    ev = [_group_ev("q1", "Program__c", copied)]
    msg = rprompts.RECORDS_MIRROR_MESSAGE.replace("<<OBJECT>>", "Candidate_Training__c").replace(
        "<<FIELD>>", "Program__c").replace("<<SOURCE>>", "Program_Version__c.Program__c")
    assert mirror_nudges(ev) == [msg] and "<<" not in msg
    session, _ = _session(ev)
    assert session.check_draft("Program Alpha: 1.\n" + SOURCE_56, "trainings per program", STATUS) == msg
    # a later query through the path, or no copied lookup: nothing
    assert mirror_nudges(ev + [_group_ev("q2", "Program_Version__c.Program__c")]) == []
    assert mirror_nudges([_group_ev("q1", "Program_Version__c.Program__c")]) == []


# ------------------------------------------------------------------- round 3 fixes (2026-10-14; RECORDS_SPEC status block)
# Synthetic names only (fixture people); the shapes copy the round-3 failures.
from types import SimpleNamespace  # noqa: E402

from sf_assistant.agent.record_session import (availability_issues, breakdown_nudges, clean_knowledge_answer,  # noqa: E402
                           drop_working_sentences, no_data_nudges, noun_nudges, shared_name_nudges,
                           warehouse_availability, with_source_line)
from sf_assistant.agent.nudges import with_no_data_line  # noqa: E402

SUPPORT_Q = "how many mock interviews did delta person support"


def _support_check(field="Interviewer__c", role="interviewer", **kw):
    """A support question counted through ``field``: the support lookup is almost empty, an undocumented lookup holds 6."""
    others = [_other("Interview_Success_Person__c", 6, role="undocumented"),
              dict(_other("Interview_Support_Person__c", 0, role="support employee"), fits_sparse="support"),
              _other("Recruiter__c", 0, role="recruiter")]
    if field == "Interview_Support_Person__c":
        others = [_other("Interview_Success_Person__c", 6, role="undocumented"),
                  _other("Interviewer__c", 0, role="interviewer"), _other("Recruiter__c", 0, role="recruiter")]
    lc = _check(field=field, role=role, cues=("support",), others=others)
    lc.update(kw)
    return lc


def test_footer_names_no_abandoned_lookup_when_the_answer_reports_an_other_entry():
    """Issue 1: the count came from the lookup check of an abandoned 0 query (its others entry)."""
    ev = [_person_ev("q1", 0, _support_check(), field="Interviewer__c", tool="group_records")]
    text = ("Delta Person supported 6 mock interviews, recorded in `Interview_Success_Person__c` (undocumented role). "
            "The support lookup `Interview_Support_Person__c` has 0 for her.\n" + SOURCE)
    assert with_lookup_footer(text, ev) == text
    # the answer gives the check's own 0 with its role: the lookup is still listed
    zero = "Delta Person supported 0 mock interviews as interviewer; `Interview_Success_Person__c` 6.\n" + SOURCE
    assert "Lookup used: `Internal_Interview__c.Interviewer__c` (interviewer)." in with_lookup_footer(zero, ev)
    # the lead gives the check's 0 (lookup unnamed) and the other lookup's count is only context: the footer says
    # where the 0 came from
    lead0 = (f"Delta Person supported **0** mock interviews (`{II}`).\n- Delta Person appears as "
             "`Interview_Success_Person__c` (undocumented role) on 6 records, which is not a support role.\n" + SOURCE)
    assert "Lookup used: `Internal_Interview__c.Interviewer__c` (interviewer)." in with_lookup_footer(lead0, ev)
    # a count above 0 that the text gives (the lookup unnamed) keeps the footer, even with another lookup named
    above = _check(records=9, field="Interviewer__c", role="interviewer", fits="taken",
                   others=[_other("Recruiter__c", 5, role="recruiter")])
    ev2 = [_person_ev("q1", 9, above, field="Interviewer__c")]
    text2 = "Delta Person took 9 mock interviews; recruiter (`Recruiter__c`) on 5.\n" + SOURCE
    assert "Lookup used: `Internal_Interview__c.Interviewer__c` (interviewer)." in with_lookup_footer(text2, ev2)


def test_sparse_zero_nudge_for_a_leading_zero_through_an_almost_empty_lookup():
    """Issue 1b: the used lookup fits the word but is almost empty; an undocumented lookup holds the person."""
    lc = _support_check(field="Interview_Support_Person__c", role="support employee", fits_sparse="support")
    ev = [_person_ev("q1", 0, lc, field="Interview_Support_Person__c")]
    draft = ("Delta Person has 0 mock interviews as support person (`Interview_Support_Person__c`). She appears in "
             "`Interview_Success_Person__c` on 6, but that is not the support role.\n" + SOURCE)
    expected = (rprompts.RECORDS_SPARSE_ZERO_MESSAGE.replace("<<FIELD>>", "Interview_Support_Person__c")
                .replace("<<ROLE>>", "support employee").replace("<<WORD>>", "support").replace("<<OBJECT>>", II)
                .replace("<<OTHERS>>", "Interview_Success_Person__c (undocumented) 6"))
    assert lookup_nudges(draft, ev) == [expected] and "<<" not in expected
    session, _ = _session(ev)
    assert session.check_draft(draft, SUPPORT_Q, STATUS) == expected
    # no nudge: the lead gives the holder's count; the lookup has no data at all (the no-data nudge's case); another
    # lookup fits (the other-lookup nudge's case); the 0 is not the lead
    lead6 = "Delta Person appears on 6 mock interviews through `Interview_Success_Person__c` (role not documented).\n"
    assert lookup_nudges(lead6 + SOURCE, ev) == []
    ev_empty = [_person_ev("q1", 0, lc, field="Interview_Support_Person__c")]
    ev_empty[0].model_result = dict(ev_empty[0].model_result, no_data_fields=[f"{II}.Interview_Support_Person__c"])
    assert not any(m.startswith("Your answer leads with 0 through") for m in lookup_nudges(draft, ev_empty))
    fits = dict(lc, others=[_other("Interview_Success_Person__c", 6, role="success person", fits="support")])
    other = lookup_nudges(draft, [_person_ev("q1", 0, fits, field="Interview_Support_Person__c")])
    assert len(other) == 1 and other[0].startswith("Your count used Interview_Support_Person__c (support employee) and "
                                                   "found 0, but the question's word 'support' fits another lookup")
    assert f"Interview_Support_Person__c is filled on almost no {II} records." in other[0]
    assert lookup_nudges("The support lookup is almost empty.\nDelta Person: 0 through it.\n" + SOURCE, ev) == []
    # a lookup with a documented role that does not fit the word (interviewer for "supported") is no holder: leading
    # with its count would relabel those records; next to an undocumented holder only that one is listed
    documented = dict(lc, others=[_other("Interviewer__c", 9, role="interviewer"),
                                  _other("Interview_Success_Person__c", 0, role="undocumented")])
    assert lookup_nudges(draft, [_person_ev("q1", 0, documented, field="Interview_Support_Person__c")]) == []
    both = dict(lc, others=[_other("Interviewer__c", 9, role="interviewer"),
                            _other("Interview_Success_Person__c", 6, role="undocumented")])
    assert lookup_nudges(draft, [_person_ev("q1", 0, both, field="Interview_Support_Person__c")]) == [expected]


def _by_record_check(kinds=("employee", "employee"), counts=(0, 16)):
    lc = _check(records=sum(counts), field="Interviewer__c", role="interviewer", fits="conducted", cues=("conducted",),
                others=[_other("Interview_Readiness_Person__c", 10, role="undocumented")])
    lc["by_record"] = [{"record": f"Delta Person (#{i})", "detail": "Active Employee", "records": n, "kind": k}
                       for i, (k, n) in enumerate(zip(kinds, counts), 1)]
    return lc


def test_shared_name_nudge_when_several_records_of_the_asked_kind_share_the_name():
    """Issue 2: the note gave per-record counts; the draft never says that two employees share the name."""
    ev = [_person_ev("q1", 16, _by_record_check(), field="Interviewer__c")]
    draft = "Delta Person conducted 16 internal interviews as interviewer (`Interviewer__c`).\n" + SOURCE
    msgs = shared_name_nudges(draft, ev)
    assert msgs == [rprompts.RECORDS_SHARED_NAME_MESSAGE.replace("<<NAME>>", "Delta Person").replace("<<N>>", "2")
                    .replace("<<LABEL>>", "Employee").replace("<<FIELD>>", f"{II}.Interviewer__c")
                    .replace("<<RECORDS>>", "Delta Person (#1) (Active Employee) 0, Delta Person (#2) (Active Employee) 16")]
    session, _ = _session(ev)
    assert session.check_draft(draft, "how many internal interviews has delta person conducted", STATUS) == msgs[0]
    for fine in ("Two Employee records are named Delta Person: Delta Person (#2) conducted 16, #1 none.\n" + SOURCE,
                 "Delta Person conducted 16 (two employees share the name; all 16 are on the second record).\n" + SOURCE,
                 "16 internal interviews for Delta Person (#2).\n" + SOURCE):
        assert shared_name_nudges(fine, ev) == [], fine
    # fill-rate or status wording ("N records with ...") says nothing about a shared name: the nudge still fires
    for silent in ("Delta Person conducted 16 internal interviews (`Interviewer__c`). `Interviewer__c` is filled on "
                   "490 of 620 records with a value.\n" + SOURCE,
                   "Delta Person conducted 16 internal interviews (`Interviewer__c`); 4 records with a blank status.\n"
                   + SOURCE):
        assert shared_name_nudges(silent, ev) == msgs, silent
    assert shared_name_nudges("Delta Person conducted 16 (Delta Person #2; #1 has none).\n" + SOURCE, ev) == []
    # an employee's own account next to a candidate record is no shared name of the asked person; older evidence
    # without "kind" counts as the check's kind; no record with records: nothing
    other = _by_record_check(kinds=("other", "employee"))
    assert shared_name_nudges(draft, [_person_ev("q1", 16, other, field="Interviewer__c")]) == []
    legacy = _by_record_check()
    for b in legacy["by_record"]:
        b.pop("kind")
    assert len(shared_name_nudges(draft, [_person_ev("q1", 16, legacy, field="Interviewer__c")])) == 1
    assert shared_name_nudges(draft, [_person_ev("q1", 0, _by_record_check(counts=(0, 0)), field="Interviewer__c")]) == []
    text = rprompts.render_records_system(STATUS)
    assert "Several records with the person's name (lookup_check by_record" in text


def _fake_catalog():
    def fs(filled):
        return SimpleNamespace(filled=filled)
    return SimpleNamespace(objects={
        "Interview_Evaluation__c": SimpleNamespace(has_table=True, rows=16, fields={"Score__c": fs(16)},
                                                   not_synced={"Evaluator__c": {"role": "evaluator"}}),
        II: SimpleNamespace(has_table=True, rows=620, fields={"Interviewer__c": fs(490), "Recruiter__c": fs(200),
                                                              "Approved_By__c": fs(0)},
                            not_synced={"Final_Decision_By__c": {}}),
        "Recruiter__c": SimpleNamespace(has_table=True, rows=260, fields={}, not_synced={}),
        "Ghost__c": SimpleNamespace(has_table=False, rows=0, fields={}, not_synced={}),
    })


def test_warehouse_availability_reads_the_catalog():
    avail = warehouse_availability(_fake_catalog())
    assert avail("Interview_Evaluation__c") is True and avail("Ghost__c") is False
    assert avail("Evaluator__c", ["Interview_Evaluation__c"]) is False
    assert avail("Interview_Evaluation__c.Evaluator__c") is False and avail(f"{II}.Interviewer__c") is True
    assert avail("Approved_By__c", [II]) is False and avail("Interviewer__c") is True and avail("Nope__c") is None
    assert avail("Recruiter__c") is True and avail("Recruiter__c", [II]) is True   # object and filled field
    session = RecordSession(SimpleNamespace(catalog=_fake_catalog(), close=lambda: None), _cfg(), llm=ScriptedLLM(["x"]))
    assert session.availability()("Interview_Evaluation__c") is True
    assert RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(["x"])).availability() is None


def test_merge_that_calls_an_available_object_not_available_is_rejected():
    """Issue 3: only the evaluator field is unsynced, not the evaluation object."""
    lc = _check(records=9, field="Interviewer__c", role="interviewer", fits="done", cues=("done",), others=[])
    lc["not_in_warehouse"] = ["Final_Decision_By__c"]
    rec = _merge_rec("The data does not record who evaluated; as a proxy, Delta Person conducted 9 internal interviews "
                     "(`Interviewer__c`).\n" + SOURCE, evidence=[_person_ev("q1", 9, lc, field="Interviewer__c")])
    avail = warehouse_availability(_fake_catalog())
    bad = ("The data does not record evaluations: the `Interview_Evaluation__c` object and its `Evaluator__c` field are "
           "not available in the warehouse. As a proxy, Delta Person conducted 9 (`Interviewer__c`).\n" + SOURCE)
    assert availability_issues(bad, rec, None, avail) == \
        ["that Interview_Evaluation__c is not available, but the warehouse holds it"]
    assert merge_scope_issues(bad, rec, "q", None, avail) == \
        ["that Interview_Evaluation__c is not available, but the warehouse holds it"]
    for fine in ("The data does not record evaluations: `Interview_Evaluation__c.Evaluator__c` is not synced. As a "
                 "proxy, Delta Person conducted 9 (`Interviewer__c`).\n" + SOURCE,
                 "Delta Person conducted 9 (`Interviewer__c`); `Final_Decision_By__c` is not in the warehouse.\n" + SOURCE,
                 "Delta Person conducted 9 (`Interviewer__c`). `Approved_By__c` is not available.\n" + SOURCE,
                 # a field named with its object: the claim is about the pair, not the object
                 "The data does not record evaluations: the `Evaluator__c` field on the `Interview_Evaluation__c` "
                 "object is not available in the warehouse. As a proxy, Delta Person conducted 9 (`Interviewer__c`).\n"
                 + SOURCE,
                 "`Evaluator__c` (on `Interview_Evaluation__c`) is not in the warehouse; Delta Person conducted 9.\n"
                 + SOURCE,
                 "The `Interview_Evaluation__c` object's `Evaluator__c` field is not synced. Delta Person conducted "
                 "9.\n" + SOURCE):
        assert availability_issues(fine, rec, None, avail) == [], fine
    # a claim the record answer makes too is the merge repeating it (even about the whole object)
    rec_same = _merge_rec(bad, evidence=rec.evidence)
    assert availability_issues(bad, rec_same, None, avail) == []
    # a pair in the record answer does not cover the whole object
    rec_pair = _merge_rec("The `Evaluator__c` field on `Interview_Evaluation__c` is not in the warehouse. Delta Person "
                          "conducted 9 (`Interviewer__c`).\n" + SOURCE, evidence=rec.evidence)
    assert availability_issues(bad, rec_pair, None, avail) == \
        ["that Interview_Evaluation__c is not available, but the warehouse holds it"]
    # without a catalog only a queried object is known to be there
    queried = f"`{II}` is not available, so nothing was counted.\n" + SOURCE
    assert availability_issues(queried, rec) == [f"that {II} is not available, but the warehouse holds it"]
    assert availability_issues(bad, rec) == []
    m = combine(ScriptedLLM([bad]), "how many interview evaluations has delta person done", KNOWLEDGE, rec, STATUS,
                availability=avail)
    assert not m.merged and m.text == rec.answer and "Interview_Evaluation__c is not available" in m.error
    assert "an object is available even when one of its fields\n  is not" in rprompts.COMBINE_SYSTEM


def test_working_notes_with_one_planning_phrase_are_caught():
    """Issue 4: one planning phrase plus a restatement, talk about the instructions or the tools, or a second draft."""
    for notes in ("Per the instructions, I should answer for the Employee first.\n- 7 as interviewer\n" + SOURCE,
                  "Delta Person took 7.\n\nThe question asks about mocks she supported. Let me also check the other "
                  "lookup.\n- 7 as interviewer\n" + SOURCE,
                  "The question asks specifically about support. The lookup_check also shows 6 elsewhere.\n" + SOURCE,
                  "All 10 internal interviews are of type Mock. Let me verify this is complete.\n\nThe data shows:\n"
                  "- Total: 10\n- By type: all 10 Mock\n" + SOURCE):
        assert working_notes(notes), notes
    for ok_text in ("I will not count the legacy field.\n- **Source**: preprod sandbox copy, synced 2026-10-13 14:12 UTC.",
                    "Delta Person took 7 mock interviews. Let me know if you need more.\n" + SOURCE,
                    "Delta Person took 7 mock interviews (`Interviewer__c`).\n" + SOURCE,
                    # business wording with "guidelines" / "rules" is no talk about the prompt
                    "Delta Person has 5 interviews according to the guidelines field.\n" + SOURCE,
                    "12 sessions were held following the guidelines in `Session__c`.\n" + SOURCE,
                    "- Counted per the rules of the interview assignment.\n" + SOURCE,
                    "7 interviews were cancelled; the validation rules require a reason.\n" + SOURCE):
        assert not working_notes(ok_text), ok_text
    assert working_notes('Delta Person took 7; the rule says "answer for the Employee".\n' + SOURCE)
    assert drop_working_sentences("Delta Person has 5 interviews according to the guidelines field.\n" + SOURCE) == \
        "Delta Person has 5 interviews according to the guidelines field.\n" + SOURCE
    # strip_reasoning: a first draft that ends with a planning sentence, then the whole answer again
    two = ("All 10 internal interviews are of type Mock. Let me verify this is complete.\n\nThe data shows:\n"
           "- Total: 10\n- By type: all 10 Mock\n" + SOURCE)
    assert strip_reasoning(two) == "The data shows:\n- Total: 10\n- By type: all 10 Mock\n" + SOURCE
    keep = "Delta Person took 7. Let me know if you need more.\n- 7 completed\n" + SOURCE
    assert strip_reasoning(keep) == keep
    # a lead line that ends by announcing its list, then the list repeating the total: one answer, not two drafts
    for listed in ("Delta Person conducted 16 internal interviews (`Interviewer__c`). Let me break this down by "
                   "status:\n- Unscheduled: 7\n- Scheduled: 9\n- Total: 16\n" + SOURCE,
                   "Delta Person conducted **16** internal interviews as interviewer (`Interviewer__c`). I'll list "
                   "them by status below.\n\n- Unscheduled: 7\n- Scheduled: 9 (16 in all)\n" + SOURCE,
                   "Delta Person conducted 16 internal interviews (`Interviewer__c`). Let me verify the split.\n"
                   "- Unscheduled: 7\n- Scheduled: 9\n- Total: 16\n" + SOURCE):
        assert strip_reasoning(listed) == listed and not working_notes(listed), listed
    # "Let me present this clearly:" on its own line, then the whole answer again in prose: still two drafts
    again = ("- Total: 12\n- By type: Mock 7, Technical 5\n\nLet me present this clearly:\n\nDelta Person has 12 "
             "internal interviews (`Interviewer__c`): Mock 7, Technical 5.\n" + SOURCE)
    assert strip_reasoning(again) == ("Delta Person has 12 internal interviews (`Interviewer__c`): Mock 7, Technical "
                                      "5.\n" + SOURCE)
    # drop_working_sentences: planning sentences without numbers and "Per the instructions, " openings
    text = ("Delta Person has records in two roles. Per the instructions, I should answer for the Employee first.\n"
            "The question asks about mocks she took.\n- 7 as interviewer. Let me check the rest.\n"
            "Per the rules, the candidate record is counted apart: 7.\n" + SOURCE)
    assert drop_working_sentences(text) == ("Delta Person has records in two roles.\n- 7 as interviewer.\n"
                                            "The candidate record is counted apart: 7.\n" + SOURCE)
    assert drop_working_sentences("I will not count the legacy field: 7.\n" + SOURCE) == \
        "I will not count the legacy field: 7.\n" + SOURCE
    assert drop_working_sentences("Let me check.\n" + SOURCE) == "Let me check.\n" + SOURCE   # nothing would be left
    # a planning sentence that carries content keeps it (the clause after ";", a reported fact); a plan that only
    # says what to do next is dropped even with an API name in it
    lead = "Delta Person conducted 9 internal interviews (`Interviewer__c`)."
    assert drop_working_sentences(lead + " I should answer for the Employee record; the Account record with the same "
                                  "name is a different record.\n" + SOURCE) == \
        lead + " The Account record with the same name is a different record.\n" + SOURCE
    reported = ("No employee is named Nobody Here. I need to report that no close spelling exists in `Recruiter__c` "
                "either.\n" + SOURCE)
    assert drop_working_sentences(reported) == reported
    assert drop_working_sentences(lead + " I should answer for the Employee with `Interviewer__c` first, then give the "
                                  "other record's count on its own.\n" + SOURCE) == lead + "\n" + SOURCE
    session, _ = _session([_person_ev("q1", 7, _check(records=7, field="Interviewer__c", role="interviewer",
                                                         fits="taken", others=[]), field="Interviewer__c")])
    assert session.check_draft("Per the instructions, Delta Person took 7 (`Interviewer__c`).\n" + SOURCE, PERSON_Q,
                               STATUS) == rprompts.RECORDS_FINAL_FORM_MESSAGE
    # the merge: working sentences are dropped; a merged text that still reads as notes falls back to the record answer
    rec = _merge_rec("Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n" + SOURCE)
    m = combine(ScriptedLLM(["Per the instructions, I should answer for the Employee first.\nDelta Person took 7 mock "
                             "interviews as interviewer (`Interviewer__c`).\n" + SOURCE]), PERSON_Q, KNOWLEDGE, rec, STATUS)
    assert m.merged and m.text == "Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n" + SOURCE
    m = combine(ScriptedLLM(["Delta Person took 7. I will check 7 again.\nI'll verify 7 once more.\n" + SOURCE]),
                PERSON_Q, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.error == "the merged answer reads as working notes" and m.text == rec.answer
    assert "no mention of these instructions" in rprompts.render_records_system(STATUS)


def _refs_ev(qid="q1", count=223):
    """find_references first (its counts are over all records), then the count."""
    refs = {"query_id": qid, "object": "Recruiter__c", "name": "Delta Person",
            "references": [{"record": "R1", "object": "Interview__c", "field": "Recruiter__c", "role": "recruiter",
                            "records": 220}],
            "system_references": [{"record": "R2", "object": "Interview__c", "field": "CreatedById",
                                   "role": "record creator", "records": count}], "source": "preprod sandbox copy"}
    ev = QueryEvidence(query_id=qid, tool="find_references", kind="data", object="Recruiter__c", arguments={}, ok=True,
                       error=None, applied={}, notes=[], statements=[], tables=[], synced_at={}, summary="2 lookups",
                       model_result=refs, elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, []
    return ev


def test_overlap_nudge_reads_find_references_counts_and_more_wordings():
    """Issue 5: "220 of these" from find_references, "on these records", and a difference given as extra records."""
    created = _check(records=223, field="CreatedById", role="record creator", fits="create", cues=("create",),
                     others=[_other("Recruiter__c", 0, role="recruiter")], obj="Interview__c")
    ev = [_refs_ev(), _person_ev("q2", 223, created, field="CreatedById", obj="Interview__c")]
    draft = ("Delta Person created 223 client interviews (`CreatedById`).\n- Delta Person is also the recruiter on 220 "
             "of these interviews.\n" + SOURCE)
    msgs = overlap_nudges(draft, ev)
    assert len(msgs) == 1 and "220 of these interviews" in msgs[0]
    # a role breakdown of a find_references count is no subset claim
    assert overlap_nudges("Delta Person is referenced on 223 client interviews; roles include recruiter 220.\n" + SOURCE,
                          [_refs_ev()]) == []
    taken = _check(records=9, field="Interviewer__c", role="interviewer", fits="taken",
                   others=[_other("Recruiter__c", 5, role="recruiter")])
    ev2 = [_person_ev("q1", 9, taken, field="Interviewer__c")]
    on_these = ("Delta Person took 9 (`Interviewer__c`).\n- Delta Person also appears in other roles on these records "
                "(as `Recruiter__c` on 5 interviews).\n" + SOURCE)
    assert len(overlap_nudges(on_these, ev2)) == 1
    assert overlap_nudges("Delta Person took 9 (`Interviewer__c`); as recruiter (`Recruiter__c`) 5.\n" + SOURCE, ev2) == []
    assert overlap_nudges("Delta Person took 9; recruiter (`Recruiter__c`) 5 under the same conditions.\n" + SOURCE,
                          ev2) == []
    # each count on its own with its lookup, worded with "these lookups / roles" or "N other roles": no subset claim
    three = _check(records=9, field="Interviewer__c", role="interviewer", fits="taken",
                   others=[_other("Recruiter__c", 5, role="recruiter"),
                           _other("Interview_Readiness_Person__c", 2, role="undocumented")])
    ev_three = [_person_ev("q1", 9, three, field="Interviewer__c")]
    for fine in ("- Delta Person also appears in these lookups: `Recruiter__c` (5), `Interview_Readiness_Person__c` "
                 "(2).",
                 "- Counts for these roles, each on its own: recruiter (`Recruiter__c`) 5, readiness "
                 "(`Interview_Readiness_Person__c`) 2.",
                 "- In 3 other roles: recruiter (`Recruiter__c`) 5, readiness 2."):
        text = "Delta Person took 9 mock interviews (`Interviewer__c`).\n" + fine + "\n" + SOURCE
        assert overlap_nudges(text, ev_three) == [], fine
    # any_role 24 - interviewer 18 = 6, given as extra records next to a lookup that holds 12
    conducted = _check(records=18, field="Interviewer__c", role="interviewer", fits="conducted", cues=("conducted",),
                       others=[_other("Interview_Readiness_Person__c", 12, role="undocumented")])
    conducted["any_role"] = 24
    ev3 = [_person_ev("q1", 18, conducted, field="Interviewer__c")]
    ev3[0].model_result = dict(ev3[0].model_result, split_totals={"(blank)": 6, "Scheduled": 12})   # 6 is also a result
    extra = ("Delta Person conducted 18 (`Interviewer__c`).\n- Delta Person also appears in other roles on 6 additional "
             "records (`Interview_Readiness_Person__c`).\n" + SOURCE)
    assert len(overlap_nudges(extra, ev3)) == 1
    assert overlap_nudges("Delta Person conducted 18 (`Interviewer__c`).\n- 6 have a blank status.\n" + SOURCE, ev3) == []
    assert overlap_nudges("Delta Person conducted 18; `Interview_Readiness_Person__c` holds 12 more.\n" + SOURCE,
                          ev3) == []
    # a named lookup's own count put inside the counted records, although another result also holds that number (12)
    subset = ("Delta Person conducted 18 (`Interviewer__c`).\n- Delta Person also appears in other roles on these "
              "records: `Interview_Readiness_Person__c` (12 records).\n" + SOURCE)
    assert len(overlap_nudges(subset, ev3)) == 1
    assert overlap_nudges("Delta Person conducted 18; also readiness person (`Interview_Readiness_Person__c`) on 12, "
                          "including other mocks.\n" + SOURCE, ev3) == []


def test_no_data_nudge_for_a_leading_zero_through_an_empty_lookup():
    """Issue 6: the tool note said the data does not record it; the draft led with 0."""
    lc = _check(records=0, field="Approved_By__c", role="approver employee", fits=None, cues=("approved",),
                others=[_other("Uploaded_By__c", 5, role="uploader employee")], obj="Resume__c")
    lc["fits_sparse"] = "approved"
    ev = [_person_ev("q1", 0, lc, field="Approved_By__c", obj="Resume__c")]
    ev[0].model_result = dict(ev[0].model_result, no_data_fields=["Resume__c.Approved_By__c"])
    draft = "**0 resumes** have been approved by Delta Person.\n- `Approved_By__c` is blank on all resumes.\n" + SOURCE
    expected = rprompts.RECORDS_NO_DATA_MESSAGE.replace("<<FIELDS>>", "Resume__c.Approved_By__c")
    assert no_data_nudges(draft, ev) == [expected] and "<<" not in expected
    session, _ = _session(ev)
    msg = session.check_draft(draft, "how many resumes has delta person approved", STATUS)
    assert msg == expected   # the sparse-zero nudge leaves the empty lookup to this one
    for fine in ("The data does not record approvals: `Approved_By__c` is blank on all 25 resumes (0 of 25).\n" + SOURCE,
                 "Delta Person uploaded 5 resumes (`Uploaded_By__c`); approvals are not recorded.\n" + SOURCE):
        assert no_data_nudges(fine, ev) == [], fine
        assert with_no_data_line(fine, ev) == fine
    assert no_data_nudges(draft, [_person_ev("q1", 0, lc, field="Approved_By__c", obj="Resume__c")]) == []
    # the code backstop: the retry is sent once only (and not after another message came first), so a final answer
    # that still leads with 0 gets the no-data line first, and its lead sentence (0 its only number) goes: "has
    # approved 0" under "a 0 through it is no answer" contradicts itself (remaining round 1, Q32)
    line = ("The data does not record this: `Resume__c.Approved_By__c` is blank on every record in the warehouse, so "
            "a 0 through it is no answer.\n\n")
    rest = "- `Approved_By__c` is blank on all resumes.\n" + SOURCE
    assert with_no_data_line(draft, ev) == line + rest
    assert with_no_data_line(draft, [_person_ev("q1", 0, lc, field="Approved_By__c", obj="Resume__c")]) == draft
    other = "Delta Person has 0 resumes through `Uploaded_By__c`.\n" + SOURCE   # the lead's 0 is about another field
    assert with_no_data_line(other, ev) == other
    assert with_no_data_line("", ev) == ""
    # a lead with another number stays (it may carry a count of another field)
    mixed = "Delta Person has approved 0 resumes and uploaded 5.\n" + SOURCE
    assert with_no_data_line(mixed, ev) == line + mixed
    # the lead sentence goes from its line; the rest of the line stays
    two = "Delta Person has approved **0** resumes. She uploaded 5 (`Uploaded_By__c`).\n" + SOURCE
    assert with_no_data_line(two, ev) == line + "She uploaded 5 (`Uploaded_By__c`).\n" + SOURCE
    rec = RecordSession(_not_found_tools(ev), _cfg(), llm=ScriptedLLM([{"name": "count_records", "args": {}}, draft])
                        ).ask("How many resumes has Delta Person approved?")
    assert rec.nudges == [expected] and rec.answer == line + rest and rec.unsupported_numbers == []


QOBJ = [{"name": "Delta Person", "word": "interviews",
         "objects": [{"object": "Interview__c", "label": "Interview",
                      "refs": [{"field": "Recruiter__c", "role": "recruiter", "records": 383}]},
                     {"object": II, "label": "Internal Interview",
                      "refs": [{"field": "Interviewer__c", "role": "interviewer", "records": 12}]}]}]


def test_both_objects_nudge_for_a_noun_without_qualifier_or_role_word():
    """Issue 7: "interviews" with no qualifier and no role word, the person on both objects, one counted."""
    client = _check(records=383, field="Recruiter__c", role="recruiter", fits=None, cues=(), others=[], obj="Interview__c")
    ev = [_person_ev("q1", 383, client, field="Recruiter__c", obj="Interview__c", tool="group_records")]
    draft = "Delta Person has 383 client interviews (`Recruiter__c`), by `Round_Info__c`.\n" + SOURCE
    msgs = noun_nudges(draft, ev, QOBJ)
    expected = (rprompts.RECORDS_BOTH_OBJECTS_MESSAGE.replace("<<WORD>>", "interviews").replace("<<NAME>>", "Delta Person")
                .replace("<<OBJECTS>>", f"Interview__c (Interview; Recruiter__c (recruiter)); {II} (Internal Interview; "
                                        "Interviewer__c (interviewer))")
                .replace("<<DONE>>", "Interview__c").replace("<<MISSING>>", II))
    assert msgs == [expected] and "<<" not in expected and "383" not in expected   # hint counts are no result numbers
    tools = LookupTools(ev)
    tools.question_objects = lambda: QOBJ
    session = RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"]))
    assert session.check_draft(draft, "how many interviews does delta person have type wise", STATUS) == expected
    both = ev + [_person_ev("q2", 12, _check(records=12, field="Interviewer__c", role="interviewer", cues=(), others=[]),
                            field="Interviewer__c", tool="group_records")]
    assert noun_nudges(draft, both, QOBJ) == []
    assert noun_nudges(draft, ev, []) == [] and noun_nudges(draft, [], QOBJ) == []
    # follow-up round 1: naming the left-out object with a reason of its own is not enough (run A named it with a
    # false reason); a reason the results back is: a count of the person on it or a not-found error on it (a counting
    # query on it, even one that finds the lookup empty, counts the object anyway)
    named = ("Delta Person has 383 client interviews (`Recruiter__c`), by `Round_Info__c`. Internal mocks "
             f"(`{II}`) are a different object and are not counted here.\n" + SOURCE)
    assert noun_nudges(named, ev, QOBJ) == [expected]
    with_count = draft.replace("\n", f" Delta Person also appears on 12 internal mocks (`{II}`).\n", 1)
    assert noun_nudges(with_count, ev, QOBJ) == []
    # a field that is not synced (E2b) says nothing about the person's records there; an object with no table (E1b)
    # or the person's name not found through it (E4h / E15a naming the person) does
    assert noun_nudges(named, ev + [_error_ev("q2", "E2b")], QOBJ) == [expected]
    assert noun_nudges(named, ev + [_error_ev("q2", "E1b")], QOBJ) == []
    person_missing = _error_ev("q2", "E4h")
    person_missing.not_found = [{"name": "Delta  person", "target": "Employee (Recruiter__c)", "object": II,
                                 "field": "Interviewer__c", "match": "exact", "close": []}]
    assert noun_nudges(named, ev + [person_missing], QOBJ) == []
    other_missing = _error_ev("q2", "E4h")
    other_missing.not_found = [dict(person_missing.not_found[0], name="Nobody Here")]
    assert noun_nudges(named, ev + [other_missing], QOBJ) == [expected]
    no_data = _person_ev("q2", 0, None, field="Interviewer__c")
    no_data.model_result = dict(no_data.model_result, no_data_fields=[f"{II}.Interviewer__c"])
    assert noun_nudges(named, ev + [no_data], QOBJ) == []
    assert "Leave an object out only when a result says so" in rprompts.RECORDS_BOTH_OBJECTS_MESSAGE
    assert "The same for any noun the hints say fits several objects" in rprompts.render_records_system(STATUS)


def test_knowledge_answer_is_cleaned_before_the_merge():
    """Issue 8: "does not contain record data", SOQL and "you would need to query" never reach the merge."""
    k = ("Mock interviews an employee took are held by `Internal_Interview__c.Interviewer__c` (the interviewer).\n"
         "The object knowledge does not contain record data. To get the count, you would need to query "
         "`Internal_Interview__c` grouped by `Status__c`.\n"
         "**To get the count, you would run a query like:**\n"
         "`SELECT COUNT() FROM Internal_Interview__c WHERE Interviewer__c = :id`\n"
         "```sql\nSELECT Id FROM Internal_Interview__c\n```\n"
         "*(Note: I cannot provide the specific count as I do not have access to record data.)*\n"
         "- `Status__c` = Completed means the mock took place.")
    cleaned = clean_knowledge_answer(k)
    assert cleaned == ("Mock interviews an employee took are held by `Internal_Interview__c.Interviewer__c` (the "
                       "interviewer).\n- `Status__c` = Completed means the mock took place.")
    assert clean_knowledge_answer("The `Evaluator__c` field is unused, so it likely holds no data.") == \
        "The `Evaluator__c` field is unused, so it likely holds no data."
    # metadata prose that looks like SQL or access talk stays: lowercase "select ... from", "I cannot see a field",
    # "query by `X`: it holds ..."
    for meta in ("Recruiters select a status from the `Status__c` picklist when the interview ends.",
                 "I cannot see a field that records who approved a resume; `Approved_By__c` is the only candidate.",
                 "You can query by `Interviewer__c`: it holds the employee who conducts the interview."):
        assert clean_knowledge_answer(meta) == meta, meta
    for noise in ("I cannot provide the exact count of interviews.", "I cannot count how many resumes she approved.",
                  "I cannot provide the specific background check records or candidate names.",
                  "The object knowledge does not contain actual record data (counts or current values).",
                  "You would need to run a query."):
        assert clean_knowledge_answer(noise) == "", noise
    rec = _merge_rec("Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n" + SOURCE)
    llm = ScriptedLLM(["Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n" + SOURCE])
    m = combine(llm, PERSON_Q, k, rec, STATUS)
    assert m.merged
    user = llm.calls[0]["messages"][1]["content"]
    assert "SOQL" not in user and "SELECT" not in user and "you would need to query" not in user
    assert "does not contain record data" not in user and "`Status__c` = Completed means" in user
    llm = ScriptedLLM(["unused"])
    m = combine(llm, PERSON_Q, "You would need to run a SOQL query.", rec, STATUS)
    assert llm.calls == [] and not m.merged and m.text == rec.answer
    note = rprompts.KNOWLEDGE_RECORDS_NOTE
    assert "never write a query" in note and "if no lookup's role clearly fits,\nname none" in note
    # the brevity and no-filter rules hold for record questions only; a metadata-only answer is printed unchanged
    assert "For such record questions only, answer only the metadata part,\nbriefly (at most 5" in note
    assert "only asks about metadata" in note and "gets your normal full answer, with no\nlength limit" in note
    assert "any lookup it proposes\n  for a person" in rprompts.COMBINE_SYSTEM


def test_answers_without_a_source_line_get_one():
    """Issue 9: code adds the Source line (sync time of the last data query's object, else the newest sync)."""
    ev = [_person_ev("q1", 7, None, field="Interviewer__c")]
    assert with_source_line("Delta Person took 7.", ev, STATUS) == \
        "Delta Person took 7.\nSource: preprod sandbox copy, synced 2026-10-13 14:12 UTC."
    assert with_source_line("No record matches.", [], STATUS) == \
        "No record matches.\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."
    for has in ("Delta Person took 7.\n" + SOURCE, "Delta Person took 7.\n- **Source**: preprod sandbox copy.", ""):
        assert with_source_line(has, ev, STATUS) == has
    script = [{"name": "count_records", "args": COUNT_ARGS}, "37 client interviews ended with an offer."]
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask("How many interviews ended with an offer?")
    assert rec.answer.endswith("\nSource: preprod sandbox copy, synced 2026-10-13 14:12 UTC.") and \
        rec.unsupported_numbers == []
    m = combine(ScriptedLLM(["37 interviews ended with an offer."]), "q?", "K", _rec(), STATUS)
    assert m.merged and m.text == "37 interviews ended with an offer.\nSource: preprod sandbox copy, synced 2026-10-13 " \
                                  "14:12 UTC."


def test_drop_working_sentences_keeps_statements_with_content():
    for keep in ("The question asks about approvals, which the data does not record.\n" + SOURCE,
                 "Let me clarify: `Recruiter__c` here is the recruiter role.\n" + SOURCE,
                 "I should note that `Approved_By__c` is blank on every resume.\n" + SOURCE,
                 "Delta Person took 7.\nLet me know if you need the list.\n" + SOURCE):
        assert drop_working_sentences(keep) == keep, keep
    assert drop_working_sentences("Delta Person took 7. Let me quickly verify the status split.\n" + SOURCE) == \
        "Delta Person took 7.\n" + SOURCE


def _status_groups_ev(qid="q2"):
    result = {"query_id": qid, "object": II, "grouped_by": "Status__c", "measure": "count",
              "groups": [{"group": "Unscheduled", "value": 7}, {"group": "Scheduled", "value": 5},
                         {"group": "(blank)", "value": 4}], "group_count": 2, "shown": 3, "total_across_groups": 16,
              "applied": {"filters": ["Interviewer__c.Name = 'Delta Person'"]}, "source": "preprod sandbox copy"}
    ev = QueryEvidence(query_id=qid, tool="group_records", kind="data", object=II, arguments={}, ok=True, error=None,
                       applied=result["applied"], notes=[], statements=[], tables=[II], synced_at={}, summary="3 groups",
                       model_result=result, elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, [(II, "Status__c"), (II, "Interviewer__c")]
    return ev


def _status_describe_ev(qid="q1"):
    result = {"query_id": qid, "object": II, "fields": [{"field": "Status__c", "values": [
        {"value": v, "records": n} for v, n in (("Completed", 120), ("Scheduled", 80), ("Unscheduled", 70),
                                                ("Rescheduled", 40), ("Cancelled", 30))]}]}
    ev = QueryEvidence(query_id=qid, tool="describe_fields", kind="orientation", object=II, arguments={}, ok=True,
                       error=None, applied={}, notes=[], statements=[], tables=[II], synced_at={}, summary="1 fields",
                       model_result=result, elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, []
    return ev


def test_breakdown_nudge_for_small_counts_no_result_gives():
    """Issue 5 (the audit's 0-12 allowance): a status breakdown with values the grouped result does not have."""
    ev = [_status_describe_ev(), _status_groups_ev()]
    draft = ("Delta Person conducted 16 internal interviews (`Interviewer__c`).\n- **Unscheduled**: 7\n- **Scheduled**: 5"
             "\n- **Completed**: 3\n- **Rescheduled**: 2\n- **Blank (no status)**: 4\n" + SOURCE)
    expected = rprompts.RECORDS_BREAKDOWN_MESSAGE.replace("<<ITEMS>>", "Completed: 3; Rescheduled: 2")
    assert breakdown_nudges(draft, ev) == [expected] and "<<" not in expected
    session, _ = _session(ev)
    assert session.check_draft(draft, "how many internal interviews has delta person conducted", STATUS) == expected
    assert breakdown_nudges(draft.replace("**Scheduled**: 5", "**Scheduled**: 6").replace("- **Completed**: 3\n", "")
                            .replace("- **Rescheduled**: 2\n", ""), ev) == [
        rprompts.RECORDS_BREAKDOWN_MESSAGE.replace("<<ITEMS>>", "Scheduled: 6")]
    for fine in ("Delta Person conducted 16.\n- Unscheduled: 7\n- Scheduled: 5\n- (blank): 4\n- Completed: 0\n" + SOURCE,
                 "Delta Person conducted 16.\n- Total: 16\n- Week 2: 3\n" + SOURCE,
                 "Delta Person conducted 16; Completed share: 12%.\n" + SOURCE):
        assert breakdown_nudges(fine, ev) == [], fine
    # a count query filtered on the value backs its count; without a grouped result nothing is checked
    counted = _person_ev("q3", 3, None, field="Interviewer__c")
    counted.model_result = dict(counted.model_result, applied={"filters": ["Interviewer__c.Name = 'Delta Person'",
                                                                           "Status__c = 'Completed'"]})
    assert breakdown_nudges("- Unscheduled: 7\n- Completed: 3\n" + SOURCE, ev + [counted]) == []
    assert breakdown_nudges(draft, [_status_describe_ev()]) == []


# ------------------------------------------------------------------- follow-up round 1 fixes (2026-10-14)
# Synthetic names only; the shapes copy the follow-up round 1 failures (grades_followup_r1.md causes 2 to 7).
from sf_assistant.agent.record_session import (draft_needs_final_form, drop_side_caveats, drop_tool_talk, not_found_answer,  # noqa: E402
                           side_caveats, unfit_lead_zero_nudges)


class _InOrderScript(ScriptedLLM):
    """The record agent replies script[k] on its k-th call (so a nudge gets a new reply); route calls answer RECORDS."""

    def chat(self, messages, tools=None, max_tokens=None, **_):
        if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
            return {"content": "RECORDS", "tool_calls": [], "usage": {}}
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": {}}
        return {"content": step, "tool_calls": [], "usage": {}}


def _preset_tools(evidence):
    """StubTools whose k-th call logs evidence[k] (ok or failed as given)."""
    class Preset(StubTools):
        def __init__(self):
            super().__init__()
            schema = {"type": "object", "properties": {}, "additionalProperties": True}
            self.tools["list_records"] = Tool(name="list_records", description="list_records", input_schema=schema,
                                              handler=lambda **_: {})

        def call(self, name, arguments=None):
            ev = evidence[len(self._evidence)]
            self._evidence.append(ev)
            return {"ok": ev.ok, "result": ev.model_result, "error": ev.error, "chunk_ids": []}
    return Preset()


def _list_ev(qid, obj="Recruiter__c", lookup_check=None, tool="list_records"):
    result = {"query_id": qid, "object": obj, "rows": [{"Name": "Zulu Other"}], "total_count": 1,
              "source": f"preprod sandbox copy; {obj} synced 2026-10-13 14:20 UTC"}
    if lookup_check is not None:
        result["lookup_check"] = [lookup_check]
    ev = QueryEvidence(query_id=qid, tool=tool, kind="data", object=obj, arguments={"object": obj}, ok=True,
                       error=None, applied={}, notes=[], statements=[], tables=[obj],
                       synced_at={obj: "2026-10-13 14:20 UTC"}, summary="1 of 1 rows", model_result=result,
                       elapsed_ms=1.0)
    ev.error_code, ev.fields_used = None, []
    return ev


def _missing_ev(qid="q1", code="E4h"):
    ev = _error_ev(qid, code, error="no Employee (Recruiter__c) record is named 'Zulu Nobody'. Close spellings: none.")
    ev.not_found = [{"name": "Zulu Nobody", "target": "Employee (Recruiter__c)", "object": II, "field": "Interviewer__c",
                     "match": "exact"}]
    return ev


def test_the_firm_other_lookup_message_offers_no_zero_answer_and_the_second_draft_is_checked():
    """Cause 2 (Q7 run B): after the other-lookup message the model kept the unfit 0 as the lead. The message for a
    word that fits another lookup has no "answer 0" branch, and the draft after it is checked once more."""
    for k in ("FIELD", "ROLE", "WORD", "OBJECT", "OTHERS", "OTHER_FIELD", "SPARSE"):
        assert f"<<{k}>>" in rprompts.RECORDS_OTHER_LOOKUP_FIRM_MESSAGE
    assert "answer 0" not in rprompts.RECORDS_OTHER_LOOKUP_FIRM_MESSAGE and "if <<FIELD>> is" not in \
        rprompts.RECORDS_OTHER_LOOKUP_FIRM_MESSAGE
    ev = [_person_ev("q1", 0, _check())]
    lead0 = ("Delta Person has taken 0 mock interviews as recruiter (`Recruiter__c`).\n- As interviewer "
             "(`Interviewer__c`): 3\n" + SOURCE)
    good = "Delta Person took 3 mock interviews as interviewer (`Interviewer__c`); 0 as recruiter.\n" + SOURCE
    firm = unfit_lead_zero_nudges(lead0, ev)
    assert len(firm) == 1 and firm[0].startswith("Your count used Recruiter__c (recruiter) and found 0, but the "
                                                 "question's word 'taken' fits another lookup")
    assert unfit_lead_zero_nudges(good, ev) == []
    # a later query through the fitting lookup whose count leads: no message
    later = _person_ev("q2", 3, _check(records=3, field="Interviewer__c", role="interviewer", fits="taken", others=[]),
                       field="Interviewer__c")
    assert unfit_lead_zero_nudges(good, ev + [later]) == []
    # with no role word nothing is checked again (the model decides the role)
    nocue = [_person_ev("q1", 0, _check(cues=(), others=[_other("Interviewer__c", 3, role="interviewer")]))]
    assert unfit_lead_zero_nudges(lead0, nocue) == []
    # end to end: draft 1 gets the full check, draft 2 (still leading with the 0) the recheck, draft 3 is accepted
    llm = _InOrderScript([{"name": "count_records", "args": {"object": II}}, "Delta Person has taken 0 mock "
                          "interviews.\n" + SOURCE, lead0, good])
    session = RecordSession(_preset_tools(ev), _cfg(), llm=llm)
    rec = session.ask(PERSON_Q)
    assert rec.answer == good and len(rec.nudges) == 2
    assert all(n.startswith("Your count used Recruiter__c (recruiter) and found 0, but the question's word 'taken'")
               for n in rec.nudges)
    assert session.recheck_draft(good) is None and session.recheck_draft("NO_RECORDS") is None


def test_a_list_without_the_persons_filter_does_not_hide_a_name_that_is_not_found():
    """Cause 3 (merged Q36): after an E4h, a successful list of employees made the not-found check silent and the
    draft's 0 passed. Value lists and lists without a matched person filter count nothing about the name."""
    q = "how many internal interviews did zulu nobody conduct"
    draft = "Zulu Nobody conducted 0 internal interviews.\n" + SOURCE
    for extra in (_list_ev("q2"), _list_ev("q2", obj=II, tool="get_field_values"), _list_ev("q2", tool="get_sync_status")):
        session, _ = _session([_missing_ev(), extra])
        msg = session.check_draft(draft, q, STATUS)
        assert msg and rprompts.RECORDS_NOT_FOUND_MESSAGE in msg, extra.tool
    # a list whose person filter matched (lookup_check) is data about a person: the not-found check stays out
    matched = _list_ev("q2", obj=II, lookup_check=_check(records=1, field="Interviewer__c", role="interviewer",
                                                        fits="conducted", others=[]))
    session, _ = _session([_missing_ev(), matched])
    assert rprompts.RECORDS_NOT_FOUND_MESSAGE not in (session.check_draft(draft, q, STATUS) or "")
    # a field that is not synced (E2b) keeps the earlier rule: any successful data call counts
    session, _ = _session([_error_ev("q1", "E2b"), _list_ev("q2")])
    assert rprompts.RECORDS_NOT_FOUND_MESSAGE not in (session.check_draft(draft, q, STATUS) or "")


def test_a_dropped_reply_is_logged_and_code_builds_the_not_found_answer():
    q = "how many internal interviews did zulu nobody conduct"
    first, second = ("Zulu Nobody conducted 0 internal interviews.\n" + SOURCE,
                     "Result: 0 internal interviews.\n" + SOURCE)
    llm = _InOrderScript([{"name": "count_records", "args": {"object": II}},
                          {"name": "list_records", "args": {"object": "Recruiter__c"}}, first, second])
    rec = RecordSession(_preset_tools([_missing_ev(), _list_ev("q2")]), _cfg(), llm=llm).ask(q)
    assert rec.no_records and rec.answer == "" and rec.error == "no successful record query"
    assert rec.nudges and rprompts.RECORDS_NOT_FOUND_MESSAGE in rec.nudges[0]
    assert rec.dropped_answer == second
    assert rec.not_found_answer == ("No Employee (Recruiter__c) record is named 'Zulu Nobody' in the records (checked "
                                    f"through `{II}.Interviewer__c`). There is no count for this name.\n"
                                    "Source: preprod sandbox copy, synced 2026-10-13 14:20 UTC.")
    trace = record_trace_lines(rec)
    assert any(t.startswith("   record reply dropped (no successful record query): Result: 0 internal interviews.")
               for t in trace)
    # the not-found answer comes from the evidence only: several names, no name, a contains match
    contains = _missing_ev("q3")
    contains.not_found = [dict(contains.not_found[0], name="Zulu", match="contains", object=None, field=None)]
    assert not_found_answer([_missing_ev(), _missing_ev("q2"), contains]) == (
        "No Employee (Recruiter__c) record is named 'Zulu Nobody' in the records (checked through "
        f"`{II}.Interviewer__c`). There is no count for this name. No Employee (Recruiter__c) record name contains "
        "'Zulu' in the records. There is no count for this name.")
    assert not_found_answer([_error_ev("q1", "E2b")]) == "" and not_found_answer([]) == ""


def _host_check():
    return _check(records=79, field="Host_User__c", role="host employee", fits="hosted", cues=("hosted",),
                  others=[_other("CreatedById", 37, group="system", target="User", role="created by")], obj="Session__c")


def test_the_merge_may_not_relate_counts_the_record_answer_keeps_apart():
    """Cause 4 (merged Q19, Q26): "also ... on these 79 sessions: 37 records" added by the merge."""
    ev = [_person_ev("q1", 79, _host_check(), field="Host_User__c", obj="Session__c")]
    rec = _rec("Delta Person hosted 79 sessions (`Host_User__c`).\n" + SOURCE, evidence=ev)
    q = "how many sessions has delta person hosted"
    bad = ("Delta Person hosted 79 sessions (`Host_User__c`). She also appears as record creator on these 79 sessions: "
           "37 records.\n" + SOURCE)
    assert merge_scope_issues(bad, rec, q) == ['an overlap of counts that no result states ("She also appears as record '
                                               'creator on these 79 sessions: 37 records.")']
    m = combine(ScriptedLLM([bad]), q, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.text == rec.answer and "an overlap of counts" in m.error
    good = ("Delta Person hosted 79 sessions (`Host_User__c`).\n- As record creator (`CreatedById`): 37 records "
            "(overlapping, never added).\n" + SOURCE)
    assert merge_scope_issues(good, rec, q) == []
    # the record answer has the same clause: the merge only repeats it
    same = _rec(bad, evidence=ev)
    assert merge_scope_issues(bad, same, q) == []
    assert "never place one count inside another" in rprompts.COMBINE_SYSTEM
    assert "never explain why two counts differ" in rprompts.COMBINE_SYSTEM


def test_the_merge_drops_caveats_about_lookups_the_question_does_not_ask_about():
    """Cause 4 (merged Q1-Q3, Q5): "`Final_Decision_By__c` is not in the warehouse" on every internal-interview
    answer (lookup_check not_in_warehouse lists it for any Employee filter)."""
    check = _check(records=7, field="Interviewer__c", role="interviewer", fits="taken", others=[])
    check["not_in_warehouse"] = ["Final_Decision_By__c"]
    ev = [_person_ev("q1", 7, check, field="Interviewer__c")]
    rec = _rec("Delta Person took 7 mock interviews (`Interviewer__c`).\n" + SOURCE, evidence=ev)
    merged = ("Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n- `Status__c` gives the stage.\n"
              "- `Final_Decision_By__c` (final decision maker) is not in the warehouse.\n" + SOURCE)
    assert [t for t, _s in side_caveats(merged, rec, None, PERSON_Q)] == ["Final_Decision_By__c"]
    assert merge_scope_issues(merged, rec, PERSON_Q) == ["a caveat about Final_Decision_By__c, which the question does "
                                                         "not ask about"]
    clean = ("Delta Person took 7 mock interviews as interviewer (`Interviewer__c`).\n- `Status__c` gives the stage.\n"
             + SOURCE)
    assert drop_side_caveats(merged, rec, None, PERSON_Q) == clean
    m = combine(ScriptedLLM([merged]), PERSON_Q, KNOWLEDGE, rec, STATUS)
    assert m.merged and m.text == clean
    # with a number the sentence stays and the merge is rejected
    counted = merged.replace("is not in the warehouse.", "is not in the warehouse, so 0 decisions are counted.")
    assert drop_side_caveats(counted, rec, None, PERSON_Q) == counted
    assert not combine(ScriptedLLM([counted]), PERSON_Q, KNOWLEDGE, rec, STATUS).merged
    # asked: the question's words, unsynced_fits, or the record answer says it too
    assert side_caveats(merged, rec, None, "who made the final decision on delta person's mocks") == []
    fits = _person_ev("q1", 7, check, field="Interviewer__c")
    fits.model_result = dict(fits.model_result, unsynced_fits=[{"field": "Final_Decision_By__c", "fits": "decided"}])
    assert side_caveats(merged, _rec(rec.answer, evidence=[fits]), None, PERSON_Q) == []
    said = _rec("Delta Person took 7 (`Interviewer__c`); `Final_Decision_By__c` is not in the warehouse.\n" + SOURCE,
                evidence=ev)
    assert side_caveats(merged, said, None, PERSON_Q) == []
    # a sentence that also states a caveat the question is about stays
    both = merged.replace("`Final_Decision_By__c` (final decision maker) is", "`Evaluator__c` and `Final_Decision_By__c` "
                          "are")
    fits2 = _person_ev("q1", 7, check, field="Interviewer__c")
    fits2.model_result = dict(fits2.model_result, unsynced_fits=[{"field": "Evaluator__c", "fits": "evaluated"}])
    assert side_caveats(both, _rec(rec.answer, evidence=[fits2]), None, PERSON_Q) == []
    assert ('lookup_check "not_in_warehouse" and "empty_lookups" list other lookups of the object\n  that the question '
            'does not ask about') in rprompts.COMBINE_SYSTEM
    assert '"unsynced_fits", "not_in_warehouse"' not in rprompts.COMBINE_SYSTEM


def test_working_note_openers_and_tool_talk_are_removed_or_sent_back():
    """Causes 5 and 6: "Perfect! I now have the complete breakdown ..." and "(`any_role`)", "Lookup Check Counts",
    "the `describe_fields` result" reached answers."""
    opener = ("Perfect! I now have the complete breakdown of the attendance records.\n\nDelta Person has 36 attendance "
              "records.\n- Interviewer: 3\n" + SOURCE)
    assert drop_working_sentences(opener) == "Delta Person has 36 attendance records.\n- Interviewer: 3\n" + SOURCE
    assert working_notes(opener)   # two planning signs
    assert drop_working_sentences("Great! Delta Person has 36 records.\n" + SOURCE) == \
        "Delta Person has 36 records.\n" + SOURCE
    late = ("Delta Person has 36. Let me check the roles.\nPerfect! I now have the complete breakdown:\nDelta Person has "
            "36 attendance records:\n- Interviewer: 3\n" + SOURCE)
    assert strip_reasoning(late) == "Delta Person has 36 attendance records:\n- Interviewer: 3\n" + SOURCE
    for keep in ("Delta Person has 36 attendance records; she now has 3 as interviewer.\n" + SOURCE,
                 "The Perfect Attendance badge was given 3 times.\n" + SOURCE):
        assert drop_working_sentences(keep) == keep and not working_notes(keep), keep
    talk = ("Delta Person supported 456 mock interviews in any capacity (`any_role`).\n- **Lookup Check Counts:** "
            "`Interviewer__c` 3\n- The `describe_fields` result shows `Approved_By__c` is blank on every record.\n"
            "- according to the lookup_check, the readiness person holds 1.\n" + SOURCE)
    assert working_notes(talk)
    assert drop_tool_talk(talk) == ("Delta Person supported 456 mock interviews in any capacity.\n- **Counts through "
                                    "other lookups:** `Interviewer__c` 3\n- `Approved_By__c` is blank on every record.\n"
                                    "- The readiness person holds 1.\n" + SOURCE)
    assert not working_notes(drop_tool_talk(talk))
    assert drop_tool_talk("456 in any role (any_role count).\n" + SOURCE) == "456 in any role.\n" + SOURCE
    assert drop_tool_talk("- Total: 300 (from describe_fields), so 296 have none.\n" + SOURCE) == \
        "- Total: 300, so 296 have none.\n" + SOURCE
    assert drop_tool_talk("Delta Person supported 456 (`any_role`).\n" + SOURCE) == \
        "Delta Person supported 456 (in any role).\n" + SOURCE
    # a line that only announces the answer is dropped after working text; one with a count never
    assert strip_reasoning("Delta Person has 36.\nI now have the data: 36 records by role.\n" + SOURCE) == \
        "Delta Person has 36.\nI now have the data: 36 records by role.\n" + SOURCE
    keep = "Delta Person took 7 (`Interviewer__c`); the role lookup (`Role__c`) holds 3.\n" + SOURCE
    assert drop_tool_talk(keep) == keep and not working_notes(keep)
    # the draft check: tool words and openers that code rewrites cost no model call; what code cannot clean does
    session, _ = _session([_person_ev("q1", 456, None, field="Interviewer__c")])
    for cleanable in ("Delta Person supported 456 (`any_role`).\n" + SOURCE,
                      "Delta Person supported 456.\n- **Lookup check**: `Interviewer__c` 456\n" + SOURCE,
                      "Delta Person supported 456 (from `lookup_check` on the same query).\n" + SOURCE,
                      "Perfect! I now have the complete breakdown:\nDelta Person supported 456.\n" + SOURCE):
        assert working_notes(cleanable) and not draft_needs_final_form(cleanable), cleanable
        assert rprompts.RECORDS_FINAL_FORM_MESSAGE not in (session.check_draft(cleanable, SUPPORT_Q, STATUS) or "")
    for notes in ("Delta Person supported 456, counted with group_records.\n" + SOURCE,
                  "Let me check the other lookups.\nDelta Person supported 456."):
        assert draft_needs_final_form(notes), notes
        msg = session.check_draft(notes, SUPPORT_Q, STATUS)
        assert msg and rprompts.RECORDS_FINAL_FORM_MESSAGE in msg, notes
    # two drafts around a planning line still read as working notes (strip_reasoning keeps the second one)
    assert draft_needs_final_form("Delta Person supported 456.\nLet me present this clearly:\nDelta Person supported "
                                  "456 (`Interviewer__c`).\n" + SOURCE)
    assert "result keys such as any_role" in rprompts.RECORDS_FINAL_FORM_MESSAGE
    # the merge: tool talk is rewritten; a tool name that cannot be rewritten rejects the merge
    rec = _rec("Delta Person supported 456 mock interviews (`Interviewer__c`).\n" + SOURCE,
               evidence=[_person_ev("q1", 456, None, field="Interviewer__c")])
    m = combine(ScriptedLLM(["Perfect! Delta Person supported 456 mock interviews (`any_role`).\n" + SOURCE]), SUPPORT_Q,
                KNOWLEDGE, rec, STATUS)
    assert m.merged and m.text == "Delta Person supported 456 mock interviews (in any role).\n" + SOURCE
    m = combine(ScriptedLLM(["Delta Person supported 456 mock interviews, counted with group_records.\n" + SOURCE]),
                SUPPORT_Q, KNOWLEDGE, rec, STATUS)
    assert not m.merged and m.error == "the merged answer reads as working notes"
    text = rprompts.render_records_system(STATUS)
    assert 'no openers ("Perfect! I now have ...")' in text and '("in any role", not\n   any_role)' in text


# ------------------------------------------------------------------- review of the follow-up round 1 fixes
from sf_assistant.agent.record_session import fix_any_role_scope, lookup_nudges as _lookup_nudges  # noqa: E402


def _client_support_check(records=456, any_role=474):
    """A client-interview support count with any_role and system lookups (follow-up round 1 merged Q13 shape)."""
    lc = _check(records=records, field="Interview_Support_Person__c", role="support person", fits="supported",
                cues=("supported",), obj="Interview__c",
                others=[_other("Recruiter__c", 17, role="owning recruiter"),
                        _other("Interviewer_Assignment_Constraint__c", 1, role="assignment constraint employee"),
                        _other("varPreferred_support_person__c", 0, role="tl preferred support person"),
                        _other("CreatedById", 16, group="system", target="User", role="record creator"),
                        _other("LastModifiedById", 13, group="system", target="User", role="last editor")])
    lc["any_role"] = any_role
    return lc


def test_the_any_role_count_never_includes_creator_last_editor_or_owner():
    """Merged Q13: "In any of the Employee lookups (support, recruiter, ..., creator, last editor): 474 (any_role)" kept
    its wrong scope: any_role leaves out the system lookups. Code takes them out of the bracketed list; a clause it
    cannot fix rejects the merge."""
    ev = [_person_ev("q1", 456, _client_support_check(), field="Interview_Support_Person__c", obj="Interview__c")]
    line = ("- In any of the Employee lookups on `Interview__c` (support, recruiter, constraint, preferred, previous, "
            "creator, last editor): 474 (in any role)")
    fixed = fix_any_role_scope(line + "\n" + SOURCE, ev)
    assert fixed == ("- In any of the Employee lookups on `Interview__c` (support, recruiter, constraint, preferred, "
                     "previous): 474 (in any role)\n" + SOURCE)
    for fine in ("- In any role (creator, last editor and owner not included): 474\n" + SOURCE,
                 "- In any role: 474; record creator (`CreatedById`): 16\n" + SOURCE,
                 "- 456 as support person; record creator: 16 (`CreatedById`)\n" + SOURCE):
        assert fix_any_role_scope(fine, ev) == fine, fine
    rec = _rec("Delta Person supported 456 client interviews (`Interview_Support_Person__c`).\n- In any role: 474\n"
               + SOURCE, evidence=ev)
    q = "status wise breakdown of interviews supported by delta person"
    bad = ("Delta Person supported 456 client interviews (`Interview_Support_Person__c`).\n- Counting creator and owner "
           "too, in any role: 474\n" + SOURCE)
    assert any(i.startswith("the count in any role with creator") for i in merge_scope_issues(bad, rec, q))
    assert merge_scope_issues(fixed.replace(line[:2], "Delta Person supported 456 client interviews "
                                            "(`Interview_Support_Person__c`).\n- ", 1), rec, q) == []
    m = combine(ScriptedLLM([line + "\n" + SOURCE]), q, KNOWLEDGE, _rec(line + "\n" + SOURCE, evidence=ev), STATUS)
    assert m.merged and "creator" not in m.text and "(support, recruiter, constraint, preferred, previous): 474" in m.text


def test_the_merge_may_not_place_the_person_in_a_lookup_whose_count_is_0():
    """Merged Q14: "Other lookups for <P> on `Interview__c` (e.g., `Interview_Support_Person__c`, `CreatedById`) exist"
    while the lookup check counted 0 through Interview_Support_Person__c."""
    check = _check(records=201, field="Recruiter__c", role="owning recruiter", fits="recruiter", cues=("recruiter",),
                   obj="Interview__c", others=[_other("Interview_Support_Person__c", 0, role="support person"),
                                               _other("CreatedById", 201, group="system", target="User",
                                                      role="record creator")])
    ev = [_person_ev("q1", 201, check, obj="Interview__c", tool="group_records")]
    rec = _rec("Delta Person owns 201 client interviews in 2026 (`Recruiter__c`).\n" + SOURCE, evidence=ev)
    q = "month wise count of interviews for recruiter delta person in 2026"
    bad = ("Delta Person owns 201 client interviews in 2026 (`Recruiter__c`). Other lookups for Delta Person on "
           "`Interview__c` (e.g., `Interview_Support_Person__c`, `CreatedById`) exist but are not the primary filter.\n"
           + SOURCE)
    assert any(i.startswith("the person in Interview_Support_Person__c, whose count is 0")
               for i in merge_scope_issues(bad, rec, q))
    for fine in ("Delta Person owns 201 client interviews in 2026 (`Recruiter__c`); she is not the support person "
                 "(`Interview_Support_Person__c`) on any of them.\n" + SOURCE,
                 "Delta Person owns 201 client interviews in 2026 (`Recruiter__c`). As support person "
                 "(`Interview_Support_Person__c`): 0.\n" + SOURCE,
                 "Delta Person owns 201 client interviews in 2026 (`Recruiter__c`), linked to the Employee record "
                 "(`Recruiter__c`).\n" + SOURCE,
                 "Delta Person owns 201 client interviews in 2026 (`Recruiter__c`). `Interview_Support_Person__c` holds "
                 "the support person of a client round.\n" + SOURCE):
        assert merge_scope_issues(fine, rec, q) == [], fine
    assert any(i.startswith("the person in Interview_Support_Person__c") for i in merge_scope_issues(
        rec.answer.replace("\n", " She also appears as support person (`Interview_Support_Person__c`).\n", 1), rec, q))
    # a result that shows her in that lookup (another query) makes the claim a fact
    later = _person_ev("q2", 5, _check(records=5, field="Interview_Support_Person__c", role="support person",
                                       cues=(), others=[], obj="Interview__c"),
                       field="Interview_Support_Person__c", obj="Interview__c")
    assert merge_scope_issues(bad, _rec(rec.answer, evidence=ev + [later]), q) == []


def test_a_merged_clause_that_restates_the_record_answer_is_no_made_up_overlap():
    """lookup_merged8_after_3 Q5: "associated with 5 resumes as the uploader (`Uploaded_By__c`) ..., none of these
    records have an approver recorded" restates the record answer; "on these 79 sessions: 37" (Q19) is still made up."""
    check = _check(records=0, field="Approved_By__c", role="approver", fits="approved", cues=("approved",),
                   obj="Resume__c", others=[_other("Uploaded_By__c", 5, role="uploader")])
    ev = [_person_ev("q1", 0, check, field="Approved_By__c", obj="Resume__c")]
    rec = _rec("The data does not record which resumes Delta Person approved. She has uploaded 5 resumes (as "
               "`Uploaded_By__c`), but none have an approver recorded.\n" + SOURCE, evidence=ev)
    q = "how many resumes has delta person approved"
    merged = ("The data does not record which resumes Delta Person has approved. While Delta Person is associated with 5 "
              "resumes as the uploader (`Uploaded_By__c`), none of these records have an approver recorded.\n" + SOURCE)
    assert merge_scope_issues(merged, rec, q) == []
    placed = ("The data does not record which resumes Delta Person has approved. Of these, 5 resumes were uploaded by "
              "her (`Uploaded_By__c`).\n" + SOURCE)
    assert any(i.startswith("an overlap of counts") for i in merge_scope_issues(placed, rec, q))
    other = _rec("The data does not record which resumes Delta Person approved.\n" + SOURCE, evidence=ev)
    assert any(i.startswith("an overlap of counts") for i in merge_scope_issues(merged, other, q))


def test_the_firm_other_lookup_message_is_not_sent_when_the_draft_leads_with_another_object():
    """Review: a draft that correctly leads with another object's count got "The 0 of <FIELD> is no answer ... lead with
    its count" for the internal-interview check; it now gets the soft message, as the recheck already does."""
    internal = _check(records=0, field="Interview_Support_Person__c", role="legacy support", fits=None,
                      cues=("supported",), others=[_other("Interview_Success_Person__c", 6, fits="supported",
                                                          role="undocumented")])
    client = _check(records=456, field="Interview_Support_Person__c", role="support person", fits="supported",
                    cues=("supported",), obj="Interview__c", others=[])
    ev = [_person_ev("q1", 456, client, field="Interview_Support_Person__c", obj="Interview__c"),
          _person_ev("q2", 0, internal, field="Interview_Support_Person__c")]
    draft = ("Delta Person supported **456** client interviews (`Interview__c`, `Interview_Support_Person__c`).\n"
             + SOURCE)
    msgs = _lookup_nudges(draft, ev)
    assert len(msgs) == 1 and msgs[0].startswith("Your count used Interview_Support_Person__c (legacy support) and "
                                                 "found 0, but the same person has records through other lookups")
    assert unfit_lead_zero_nudges(draft, ev) == []
    lead0 = "Delta Person supported 0 mock interviews (`Internal_Interview__c`).\n" + SOURCE
    assert _lookup_nudges(lead0, ev)[0].startswith("Your count used Interview_Support_Person__c (legacy support) and "
                                                   "found 0, but the question's word 'supported' fits another lookup")


def test_tool_labels_and_bracketed_tool_notes_are_rewritten():
    """Review: "**Lookup check**:" labels, "(from `lookup_check` on the same query)" and "the `find_references` query"
    stayed in record answers and rejected correct merges."""
    assert drop_tool_talk("- **Lookup check**:\n  - `Interviewer__c` 3\n" + SOURCE) == \
        "- **Other lookups**:\n  - `Interviewer__c` 3\n" + SOURCE
    assert drop_tool_talk("*   **Lookup Check:** `Interviewer__c` fits 'taken'.\n" + SOURCE) == \
        "*   **Other lookups:** `Interviewer__c` fits 'taken'.\n" + SOURCE
    assert drop_tool_talk("- **Lookup counts** (from `lookup_check` on the same query):\n" + SOURCE) == \
        "- **Lookup counts**:\n" + SOURCE
    assert drop_tool_talk("The count of 6 comes from the `find_references` query on `Recruiter__c`.\n" + SOURCE) == \
        "The count of 6 comes from the records on `Recruiter__c`.\n" + SOURCE
    # a bracket with a number, or any_role with words of its own, keeps its content
    assert drop_tool_talk("Delta took 7 (from `lookup_check`: 3 other).\n" + SOURCE).startswith("Delta took 7 (from")
    assert drop_tool_talk("456 (any_role: creator and owner not included)\n" + SOURCE) == \
        "456 (any role: creator and owner not included)\n" + SOURCE


# ------------------------------------------------------------- remaining round 1 fixes (RECORDS_SPEC status block)

from sf_assistant.agent.record_session import (drop_overlap_clauses, drop_record_side_caveats, drop_unsupported,  # noqa: E402
                           name_notes, with_name_note)


def test_drop_unsupported_removes_a_sum_that_no_result_holds():
    """Remaining round 1, Q15 (A and B): the record agent used its 12 model calls, so no check ran on its last answer,
    and "Total: 257" (251 client + 6 mock, two objects added up) went out. Code removes the list item or sentence
    that holds a number no result has; a label whose items all went goes too."""
    facts = json.dumps({"counts": [251, 6, 0]})
    q = "how many interviews has echo supported"
    a = ("Only **Echo One** has supported interviews.\n\n**Echo One:**\n- **251 client interviews** "
         "(`Interview_Support_Person__c`)\n- **6 internal mock interviews** (`Interview_Success_Person__c`)\n"
         "- **Total: 257 interviews**\n\n" + SOURCE)
    out, removed = drop_unsupported(a, [facts, q, STATUS.line()])
    assert removed == ["257"] and "257" not in out
    assert "**Echo One:**\n- **251 client interviews**" in out and "- **6 internal mock interviews**" in out
    b = ("**Total (client + internal):**\n- Echo One: 251 + 6 = 257\n\nOnly Echo One has support records, with a total "
         "of **257 interviews** (251 client and 6 mock). Echo Two has 0.\n" + SOURCE)
    out, removed = drop_unsupported(b, [facts, q, STATUS.line()])
    assert removed == ["257"] and out == "Echo Two has 0.\n" + SOURCE
    # nothing left that states a number, or nothing to remove: unchanged
    only = "Echo One supported 257 interviews.\n" + SOURCE
    assert drop_unsupported(only, [facts, q, STATUS.line()]) == (only, [])
    fine = "Echo One supported 251 client interviews and 6 mock interviews.\n" + SOURCE
    assert drop_unsupported(fine, [facts, q, STATUS.line()]) == (fine, [])


def test_the_last_answer_after_the_tool_budget_loses_unsupported_numbers():
    """okeval's loop checks a final answer only while steps remain: with the budget used up, the answer is not checked
    by a model call, so code removes the sum (RecordAnswer.dropped_numbers) and the audit finds nothing."""
    answer = "37 client interviews ended with an offer.\n- With the 5 legacy ones: 42 in total\n" + SOURCE_56
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, answer])
    rec = RecordSession(StubTools(), _cfg(), llm=llm, max_steps=2).ask("How many interviews ended with an offer?")
    assert len(llm.calls) == 2   # no check message: the last step was the budget's last
    assert rec.dropped_numbers == ["42"] and rec.unsupported_numbers == []
    assert rec.answer == "37 client interviews ended with an offer.\n" + SOURCE_56


def test_working_openers_rules_and_planning_sentences_with_people_counts_are_dropped():
    """Remaining round 1: A Q15 opened with "---", B Q29 with "`X` has 9 distinct values and is the correct field for
    'type'. Based on the query results:", B Q15 kept "The question asks ... - since there are 4 people named X, I should
    report per person." (a sentence with a number was always kept)."""
    assert drop_working_sentences("---\n\nOnly Echo One has 6.\n" + SOURCE) == "Only Echo One has 6.\n" + SOURCE
    assert drop_working_sentences("Echo One has 6.\n\n---\n" + SOURCE) == "Echo One has 6.\n\n" + SOURCE
    opener = ("The `Interview_Type__c` field has 9 distinct values and is the correct field for \"type\" of internal "
              "interview.\n\nBased on the query results:\n\n**Delta Person has had 10 internal interviews, all of type "
              "\"Upcoming Interview\".**\n" + SOURCE)
    assert drop_working_sentences(opener) == ("**Delta Person has had 10 internal interviews, all of type \"Upcoming "
                                              "Interview\".**\n" + SOURCE)
    assert drop_working_sentences("Based on the data, Delta Person has 10.\n" + SOURCE) == \
        "Delta Person has 10.\n" + SOURCE
    plan = ('The question asks "How many interviews has Echo supported?" - since there are 4 people named Echo, I should '
            "report per person. Only Echo One has 251.\n" + SOURCE)
    assert drop_working_sentences(plan) == "Only Echo One has 251.\n" + SOURCE
    for keep in ("Based on the data from `Deliverable_Result__c`:\n- Pass: 5\n" + SOURCE,
                 "Delta Person conducted 16. `Interviewer__c` is the correct lookup for interviews conducted.\n" + SOURCE,
                 "I need to report 4 interviews as cancelled.\n" + SOURCE,
                 "---\n" + SOURCE):
        assert drop_working_sentences(keep) == keep, keep


def _resume_check():
    lc = _check(records=0, field="Approved_By__c", role="approver employee", fits=None, cues=("approved",),
                others=[_other("Uploaded_By__c", 5, role="uploader employee"),
                        _other("CreatedById", 5, group="system", role="record creator", target="User"),
                        _other("LastModifiedById", 5, group="system", role="last editor", target="User")],
                obj="Resume__c")
    return [_person_ev("q1", 0, lc, field="Approved_By__c", obj="Resume__c")]


def test_overlap_after_a_nudge_is_rechecked_and_removed_by_code():
    """Remaining round 1, merged Q32 and Q34: the second draft (after another nudge) placed lookup-check counts inside
    the counted records ("of those same 5 records", "(covering all `Interview__c` records created by this user):" over
    a list); no check ran on it. The recheck now sends the overlap message, and code removes what is left."""
    ev = _resume_check()
    draft = ("The data does not record which employee approved a resume.\n\n- Delta Person is the uploader "
             "(`Uploaded_By__c`) on 5 resume records.\n- She is also the creator (`CreatedById`) and last editor "
             "(`LastModifiedById`) of those same 5 records.\n" + SOURCE)
    session, _ = _session(ev)
    msg = session.recheck_draft(draft)
    assert msg and msg.startswith(rprompts.RECORDS_OVERLAP_MESSAGE.split("<<")[0])
    out = drop_overlap_clauses(draft, ev)
    assert "those same" not in out and "- Delta Person is the uploader (`Uploaded_By__c`) on 5 resume records." in out
    created = _check(records=223, field="CreatedById", role="record creator", fits="created", cues=("create",),
                     others=[_other("Recruiter__c", 220, group="linked", role="owning recruiter"),
                             _other("LastModifiedById", 27, group="system", role="last editor", target="User"),
                             _other("Account_Manager__c", 0, role="account manager", target="User")],
                     obj="Interview__c")
    ev2 = [_person_ev("q1", 223, created, field="CreatedById", obj="Interview__c")]
    block = ("Delta Person created 223 client interviews.\n\n- **Object counted**: `Interview__c`\n- **Lookup counts** "
             "(covering all `Interview__c` records created by this user):\n  - `Recruiter__c` (owning recruiter): 220\n"
             "  - `LastModifiedById` (last editor): 27\n\n" + SOURCE)
    assert len(overlap_nudges(block, ev2)) == 1
    assert drop_overlap_clauses(block, ev2) == ("Delta Person created 223 client interviews.\n\n- **Object counted**: "
                                                "`Interview__c`\n\n" + SOURCE)
    # only the items with other records' counts go; an item with the result's own count keeps the label
    mixed = ("Delta Person created 223 client interviews.\n\n**On these records:**\n- `Recruiter__c` (owning "
             "recruiter): 220\n- In any of the User lookups: 223\n" + SOURCE)
    assert drop_overlap_clauses(mixed, ev2) == ("Delta Person created 223 client interviews.\n\n**On these records:**\n"
                                                "- In any of the User lookups: 223\n" + SOURCE)
    scoped = mixed.replace("**On these records:**", "**Lookup counts** (covering all `Interview__c` records created by "
                                                    "this user):")
    assert drop_overlap_clauses(scoped, ev2) == ("Delta Person created 223 client interviews.\n\n**Lookup counts**:\n"
                                                 "- In any of the User lookups: 223\n" + SOURCE)
    # a label without scope words keeps its list, whatever an item says ("including" belongs to its own item)
    for label in ("**Other roles on the same object (`Interview__c`):**", "For additional context:", "**Other lookups:**"):
        fine = (f"Delta Person created 223 client interviews.\n\n{label}\n- `Recruiter__c` (owning recruiter): 220\n"
                f"- In any of the lookups (including `Recruiter__c`): 223\n" + SOURCE)
        assert overlap_nudges(fine, ev2) == [] and drop_overlap_clauses(fine, ev2) == fine, label
    # the lead sentence is never removed
    lead = "Delta Person created 223 client interviews, 220 of these as recruiter (`Recruiter__c`).\n" + SOURCE
    assert overlap_nudges(lead, ev2) and drop_overlap_clauses(lead, ev2) == lead


def test_a_disjointness_claim_is_an_overlap():
    """Remaining round 1, merged Q9: "appears in 12 other records as the interview readiness employee, but these are
    distinct from the 18 interviews" (the lookup check never says so; 18 + 12 > the 24 in any role)."""
    lc = _check(records=18, field="Interviewer__c", role="interviewer", fits="take", cues=("take",),
                others=[_other("Interview_Readiness_Person__c", 12, role="interview readiness employee")])
    ev = [_person_ev("q1", 18, lc, field="Interviewer__c")]
    text = ("Delta Person took 18 internal interviews as the interviewer (`Interviewer__c`).\n- Delta Person appears "
            "in 12 other `Internal_Interview__c` records as the interview readiness employee "
            "(`Interview_Readiness_Person__c`), but these are distinct from the 18 interviews.\n" + SOURCE)
    assert len(overlap_nudges(text, ev)) == 1
    rec = _rec("Delta Person took 18 internal interviews as the interviewer (`Interviewer__c`).\n" + SOURCE, evidence=ev)
    assert any(i.startswith("an overlap of counts") for i in merge_scope_issues(text, rec, "how many did delta take"))


def test_record_answer_loses_side_caveats_the_question_is_not_about():
    """Remaining round 1, merged Q8: the record agent wrote "`Final_Decision_By__c` is not in the warehouse." on a count
    of interviews conducted; the merge kept it because the record answer said so. It is now dropped from the record
    answer (number-free sentences only); a question about decisions keeps it."""
    lc = _check(records=18, field="Interviewer__c", role="interviewer", fits="conducted", cues=("conducted",),
                others=[_other("Interview_Readiness_Person__c", 12, role="interview readiness employee")])
    lc["not_in_warehouse"] = ["Final_Decision_By__c"]
    ev = [_person_ev("q1", 18, lc, field="Interviewer__c")]
    text = ("Delta Person conducted 18 internal interviews (`Interviewer__c`).\n- `Final_Decision_By__c` is not in the "
            "warehouse.\n" + SOURCE)
    assert drop_record_side_caveats(text, ev, "how many internal interviews did delta person conduct") == \
        "Delta Person conducted 18 internal interviews (`Interviewer__c`).\n" + SOURCE
    assert drop_record_side_caveats(text, ev, "how many final decisions did delta person make") == text


def test_the_merge_keeps_the_not_stored_name_first():
    """Remaining round 1, merged Q9: the record answer began "The name '<asked>' is not stored ...; the closest match is
    <stored>"; the merge moved it into the third bullet. Code puts it first and removes the repeat."""
    q = "How many internal interviews did Delta Persen take?"
    note = 'The name "Delta Persen" is not stored in the system; the closest match is **Delta Person**.'
    rec_answer = note + "\n\nDelta Person took **18** internal interviews (`Interviewer__c`).\n" + SOURCE_56
    assert name_notes(rec_answer, q) == [("Delta Persen", note)]
    value = ('The `Interview_Attendee__c` records (Role = "Support") are not queryable: `Role__c` is not in the '
             "warehouse.\n" + SOURCE_56)   # a quoted value, not a name (lookup_merged8 Q3)
    assert name_notes(value, "How many mock interviews did Delta Person support?") == []
    merged = ("Delta Person took 18 internal interviews as the interviewer (`Interviewer__c`).\n\n*   **Name Note:** The "
              'name "Delta Persen" is not stored in the system; the closest match is Delta Person.\n\n' + SOURCE_56)
    out = with_name_note(merged, rec_answer, q)
    assert out == (note + "\n\nDelta Person took 18 internal interviews as the interviewer (`Interviewer__c`).\n\n"
                   + SOURCE_56)
    assert with_name_note(out, rec_answer, q) == out                        # already first: unchanged
    assert with_name_note(merged, "Delta Person took 18.\n" + SOURCE_56, q) == merged   # no note in the record answer
    counted = 'Delta Persen is not stored; Delta Person took 18 internal interviews.\n' + SOURCE_56
    assert "took 18" in with_name_note("Interviews: 18.\n" + counted, rec_answer, q)    # a sentence with a count stays
    ev = [_person_ev("q1", 18, None, field="Interviewer__c")]
    rec = RecordAnswer(question=q, answer=rec_answer, no_records=False, used_data=True, evidence=ev)
    m = combine(ScriptedLLM([merged]), q, "- `Interviewer__c` is the employee who conducts the mock.", rec, STATUS)
    assert m.merged and m.text.startswith(note + "\n\nDelta Person took 18")


def test_the_merge_gives_no_count_after_a_name_was_not_found():
    """Remaining round 1, merged Q36: the record answer was a clean "no such person"; the merge added "the count ... is
    0" (0 to 12 pass the number audit). A merged text with a count after a not-found answer is rejected."""
    q = "How many internal interviews has Nobody Here conducted?"
    reply = "There is no Employee record named 'Nobody Here' in the warehouse.\n" + SOURCE_56
    rec = RecordAnswer(question=q, answer=reply, no_records=False, used_data=False, evidence=[_error_ev("q1", "E4h")])
    merged = ("No employee is named 'Nobody Here'.\n- The count of internal interviews conducted by Nobody Here is 0.\n"
              + SOURCE_56)
    m = combine(ScriptedLLM([merged]), q, "- `Interviewer__c` is the employee who conducts the mock.", rec, STATUS)
    assert not m.merged and m.text == reply and "gives a count (0)" in (m.error or "")
    clean = "No employee is named 'Nobody Here', so there is no count.\n" + SOURCE_56
    m = combine(ScriptedLLM([clean]), q, "- `Interviewer__c` is the employee who conducts the mock.", rec, STATUS)
    assert m.merged and "no count" in m.text


# ------------------------------------------------------------- questions about named people (RECORDS_SPEC §22.11)

from sf_assistant.agent.record_session import PERSON_TOOLS, Triage, parse_names, people_answer, people_tool_nudges  # noqa: E402


@pytest.fixture()
def ptools(tmp_path):
    pytest.importorskip("duckdb")
    sys.path.insert(0, str(ROOT / "tests"))
    from warehouse_fixture import open_people_fixture
    t = open_people_fixture(tmp_path)
    yield t
    t.close()


class _PeopleStub(StubTools):
    """Stub tools with a name index: 'kilo zephyr' (strong), 'kilo' (weak first name), 'session purpose' (weak, design
    words); resolve_person_name knows 'kilo' and 'kilo zephyr'."""

    STORED = {"kilo zephyr": ("Recruiter__c", "a06000000000020AAA", "Kilo Zephyr", "Active Employee")}

    def spot_people(self, question):
        q = question.lower()
        out = []
        if "kilo zephyr" in q:
            out.append(_strong("kilo zephyr"))
        elif "kilo" in q:
            out.append({"words": "kilo", "recs": [self.STORED["kilo zephyr"]], "strength": "weak", "first_only": True,
                        "asked": None, "close": []})
        if "session purpose" in q:
            out.append({"words": "session purpose", "recs": [("Lead", "00Q000000000003AAA", "Session Purpose", None)],
                        "strength": "weak", "first_only": False, "asked": None, "close": []})
        return out

    def resolve_person_name(self, name):
        key = " ".join(name.lower().replace("'s", "").split())
        if key in ("kilo", "kilo zephyr"):
            return {"words": key, "recs": [self.STORED["kilo zephyr"]], "strength": "strong", "first_only": key == "kilo",
                    "asked": None, "close": [], "from": "names"}
        return None

    def is_design_word(self, word):
        return word in ("session", "purpose", "sessions")


def test_names_call_parse_none_garbage_and_containment():
    q = "is Kilo involved in the zulu's training?"
    assert parse_names("NONE", q) == [] and parse_names(" none. ", q) == [] and parse_names("RECORDS", q) == []
    assert parse_names("", q) == [] and parse_names(None, q) == []
    assert parse_names("Kilo | Zulu's", q) == ["Kilo", "Zulu's"]             # possessive kept as written
    assert parse_names("`Kilo` | \"Zulu\"", q) == ["Kilo", "Zulu"]           # quotes and backticks stripped
    assert parse_names("Kilo | Mike Bravo", q) == ["Kilo"]                    # a made-up name: not in the question
    assert parse_names("The question mentions Kilo and Zulu as people", q) == []   # prose is no name list
    assert parse_names("Kilo\nZulu", q) == ["Kilo", "Zulu"]
    long_q = "a b c d e f g kilo zulu x y w"
    assert parse_names("a b c d e f g", long_q) == []                         # more than 6 words
    assert parse_names("a | b | c | kilo | zulu", long_q) == ["a", "b", "c", "kilo"]   # at most 4


def test_triage_strong_overrules_no_records():
    llm = ScriptedLLM(["x"], route="NO_RECORDS", names="Kilo Zephyr")
    tri = RecordSession(_PeopleStub(), _cfg(), llm=llm).triage("tell me about kilo zephyr")
    assert tri.route is False and tri.routed is True and [p["words"] for p in tri.people] == ["kilo zephyr"]
    assert len(llm.names_calls) == 1 and llm.names_calls[0]["max_tokens"] == rprompts.NAMES_MAX_TOKENS
    assert tri.as_dict()["people"] == ["kilo zephyr"] and tri.asked() == ["kilo zephyr"]
    # unclear route: a strong spot is a person even when the names call names nobody
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="maybe", names="NONE")).triage(
        "tell me about kilo zephyr")
    assert tri.routed is True and [p["words"] for p in tri.people] == ["kilo zephyr"]


def test_triage_strong_spot_in_a_design_question_is_only_hinted():
    """Review §22 (client company or system user name in a design question): the route said NO_RECORDS and the names
    call ran and named none of its words, so the stored name is only hinted and the route call decides."""
    q = "what does the kilo zephyr integration user do?"
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS", names="NONE")).triage(q)
    assert tri.routed is False and tri.people == [] and [p["words"] for p in tri.hinted] == ["kilo zephyr"]
    llm = ScriptedLLM(["x"], route="NO_RECORDS", names=RuntimeError("model down"))   # no names call: as before
    assert RecordSession(_PeopleStub(), _cfg(), llm=llm).triage(q).routed is True


def test_triage_weak_needs_names_call():
    q = "how many sessions did kilo host"
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS", names="NONE")).triage(q)
    assert tri.people == [] and tri.routed is False and tri.hinted == []      # not confirmed, routed NO_RECORDS
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="RECORDS", names="NONE")).triage(q)
    assert tri.people == [] and tri.routed is True and tri.hinted == []      # the names call said NONE: no people line
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS", names="kilo")).triage(q)
    assert [p["words"] for p in tri.people] == ["kilo"] and tri.routed is True


def test_triage_names_failure_fallback():
    q = "how many sessions did kilo host"
    for route, people in (("RECORDS", ["kilo"]), ("NO_RECORDS", []), ("maybe", [])):
        llm = ScriptedLLM(["x"], route=route, names=RuntimeError("model down"))
        tri = RecordSession(_PeopleStub(), _cfg(), llm=llm).triage(q)
        assert tri.names is None and [p["words"] for p in tri.people] == people, route
    llm = ScriptedLLM(["x"], route="NO_RECORDS", names=RuntimeError("model down"))
    tri = RecordSession(_PeopleStub(), _cfg(), llm=llm).triage("what does kilo zephyr do")
    assert [p["words"] for p in tri.people] == ["kilo zephyr"]               # strong spots never need the call


def test_triage_missing_name_is_person():
    q = "who is zorblax quentin"
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="RECORDS",
                                                               names="Zorblax Quentin")).triage(q)
    assert tri.missing == ["Zorblax Quentin"] and tri.people == [] and tri.routed is True
    assert tri.people_data() is False and tri.as_dict()["people_data"] is False
    # a stored first name followed by a surname nobody has: the whole name is missing, the first name no person
    q = "how many sessions did kilo quorz host"
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="RECORDS", names="Kilo Quorz")).triage(q)
    assert tri.missing == ["Kilo Quorz"] and tri.people == [] and tri.hinted == []


def test_triage_missing_design_word_ignored():
    q = "what does session purpose mean"
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS", names="NONE")).triage(q)
    assert tri.people == [] and tri.missing == [] and tri.routed is False    # weak, unconfirmed: a design question
    tri = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"], route="NO_RECORDS", names="Sessions")).triage(
        "what do sessions mean")
    assert tri.missing == [] and tri.routed is False                          # only design words: no missing name


def test_route_returns_cached_triage():
    llm = ScriptedLLM([{"name": "count_records", "args": COUNT_ARGS}, "37 offers.\n" + SOURCE], route="NO_RECORDS",
                      names="Kilo Zephyr")
    session = RecordSession(_PeopleStub(), _cfg(), llm=llm)
    q = "how many offers did kilo zephyr make"
    assert session.route(q) is True
    rec = session.ask(q, routed=True)
    assert len(llm.route_calls) == 1 and len(llm.names_calls) == 1          # the triage ran once
    assert rec.triage["routed"] is True and rec.triage["route"] is False and rec.named_people == ["kilo zephyr"]


def test_no_spots_no_records_returns_early():
    llm = ScriptedLLM(["x"], route="NO_RECORDS", names="NONE")
    rec = RecordSession(_PeopleStub(), _cfg(), llm=llm).ask("What values can Status__c take?")
    assert rec.routed_out and rec.no_records and rec.agent is None and llm.calls == []


def test_person_no_records_pushback_any_route():
    session = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"]))
    for route in (True, False, None):
        tri = Triage(routed=True, route=route, people=[_strong("kilo zephyr")])
        msg = session.check_draft("NO_RECORDS", "q", STATUS, routed=route, triage=tri)
        assert msg == rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "'kilo zephyr'"), route
    tri = Triage(routed=True, route=False, missing=["Zorblax Quentin"])
    assert "'Zorblax Quentin'" in session.check_draft("NO_RECORDS", "q", STATUS, routed=True, triage=tri)
    assert session.check_draft("NO_RECORDS", "q", STATUS, routed=True, triage=Triage(routed=True, route=True)) is None


def test_people_require_tools_message():
    llm = _SequenceLLM(["Kilo Zephyr is an employee.", {"name": "count_records", "args": COUNT_ARGS},
                        "37 offers.\n" + SOURCE_56])
    rec = RecordSession(_PeopleStub(), _cfg(), llm=llm).ask("who is kilo zephyr")
    assert llm.calls[1]["messages"][-1]["content"] == \
        rprompts.RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE.replace("<<PEOPLE>>", "'kilo zephyr'")
    assert rec.data_queries == 1


def _person_tool_ev(qid="q1", names=("Kilo Zephyr",), tool="about_people"):
    result = {"query_id": qid, "asked": list(names), "people": [], "applied": {"names": list(names)}}
    return QueryEvidence(query_id=qid, tool=tool, kind="data", object="Recruiter__c", arguments={"names": list(names)},
                         ok=True, error=None, applied={"names": list(names)}, notes=[], statements=[], tables=[],
                         synced_at={}, summary="0 lookups with records", model_result=result, elapsed_ms=1.0)


def test_people_tool_nudge_two_names():
    tri = Triage(routed=True, route=True, people=[_strong("kilo zephyr"), _strong("sierra tango")])
    msgs = people_tool_nudges("draft", [_person_tool_ev(names=("Kilo Zephyr",))], tri)
    assert msgs == [rprompts.RECORDS_PEOPLE_TOOL_MESSAGE.replace("<<NAMES>>", "'kilo zephyr' and 'sierra tango'")
                    .replace("<<MISSING>>", "'sierra tango'").replace("<<NAMES_JSON>>", '["kilo zephyr", "sierra tango"]')]
    assert people_tool_nudges("draft", [_person_tool_ev(names=("Kilo Zephyr", "Sierra Tango"))], tri) == []
    assert people_tool_nudges("draft", [], None) == []


def test_people_tool_nudge_uncovered_name():
    tri = Triage(routed=True, route=True, people=[_strong("kilo zephyr")])
    msgs = people_tool_nudges("draft", [_count_evidence()], tri)      # a count that matched no name of the person
    assert len(msgs) == 1 and "'kilo zephyr'" in msgs[0]
    missing = Triage(routed=True, route=True, missing=["Zorblax Quentin"])
    nf = _error_ev("q1", "E15a", tool="about_people")
    nf.not_found = [{"name": "Zorblax Quentin", "target": "Employee", "object": None, "field": None, "match": "exact"}]
    assert people_tool_nudges("draft", [nf], missing) == []           # a failed call reported it not found


def test_no_people_tool_nudge_when_count_filter_matched_name():
    tri = Triage(routed=True, route=True, people=[_strong("kilo zephyr")])
    ev = _count_evidence()
    ev.names_matched = [{"filter": "Recruiter__c", "names": ["Kilo Zephyr"], "match": "exact"}]
    assert people_tool_nudges("Kilo Zephyr recruited 37.", [ev], tri) == []


def test_code_answer_after_second_no_records(ptools):
    llm = ScriptedLLM(["NO_RECORDS"], route="NO_RECORDS", names="Lima Nobel")
    rec = RecordSession(ptools, _cfg(), llm=llm).ask("who is lima nobel")
    assert rec.code_answer and not rec.no_records and rec.used_data and rec.error is None
    assert rec.agent_error == "NO_RECORDS reply" and rec.named_people == ["lima nobel"]
    # step-2 fixes of 2026-10-30 (F1, documented update): the code-written people answer without its P/R codes
    assert rec.answer.startswith("Lima Nobel is an employee (Active Employee): Employment Status: Active Employee.")
    assert "(P1)" not in rec.answer
    assert "Linked records: User 'Lima Nobel' (standard user; IsActive: true; UserType: Standard" in rec.answer
    assert "No business record references them." in rec.answer and "Source: preprod sandbox copy" in rec.answer
    ran = [e for e in rec.evidence if e.tool == "about_people"]
    assert ran and "run by code after the record agent gave no answer" in ran[-1].notes
    assert rec.nudges[0] == rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "'lima nobel'")
    assert any("code wrote the record answer from about_people" in ln for ln in record_trace_lines(rec))


def test_code_answer_numbers_pass_audit(ptools):
    llm = ScriptedLLM(["NO_RECORDS"], route="RECORDS", names="Sierra Tango | Kilo Quill")
    rec = RecordSession(ptools, _cfg(), llm=llm).ask("how is sierra tango linked to kilo quill?")
    assert rec.code_answer and rec.unsupported_numbers == []
    assert "Sierra Tango and Kilo Quill: " in rec.answer and "Candidate Training records" in rec.answer
    res = next(e.model_result for e in rec.evidence if e.tool == "about_people")
    text = people_answer(res)
    assert audit_numbers(text, [facts_json(rec.evidence, 10**9)]) == []
    assert people_answer({}) == ""


def test_forced_answer_other_people_line(ptools):
    class Budget(ScriptedLLM):   # always another tool call; the forced call (no tools) names one of the people
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
                return super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)
            if tools is None:
                return {"content": "Kilo Zephyr hosted 0 sessions (`Host_User__c`).", "tool_calls": [], "usage": {}}
            return {"content": None, "tool_calls": [{"id": f"c{len(messages)}", "name": "count_records", "arguments": {
                "object": "Session__c", "filters": [{"field": "Host_User__c", "op": "eq", "value": "Kilo Zephyr"}]}}],
                "usage": {}}

    llm = Budget(["x"], route="RECORDS", names="kilo")
    rec = RecordSession(ptools, _cfg(), llm=llm, max_steps=2).ask("how many sessions did kilo host")
    assert ("Other stored people named 'kilo': Kilo Quill (candidate), Kilo Vortex (lead); this answer does not cover "
            "them.") in rec.answer
    assert rec.answer.index("Other stored people") < rec.answer.index("Source:")


def test_not_found_answer_without_count_wording():
    nf = _error_ev("q1", "E15a", tool="about_people")
    nf.not_found = [{"name": "Zorblax Quentin", "target": "Employee, User, Account or Lead", "object": None,
                     "field": None, "match": "exact", "close": ["Zora Quinn", "Zorba Quill"]}]
    assert not_found_answer([nf]) == ("No Employee, User, Account or Lead record is named 'Zorblax Quentin' in the "
                                      "records. Closest stored names: Zora Quinn, Zorba Quill.")


def test_person_tools_constant_used_by_checks():
    from sf_assistant.agent.nudges import _reference_side_counts, _result_numbers
    assert PERSON_TOOLS == frozenset({"about_people", "find_references"})
    for tool in ("about_people", "find_references"):
        ev = _person_tool_ev(tool=tool)
        ev.model_result = {"references": [{"record": "R1", "object": II, "field": "Interviewer__c", "records": 7}],
                           "people": [{"ref": "P1", "business_reference_total": 9}],
                           "links": [{"between": ["P1", "P2"], "records": 3}]}
        assert _reference_side_counts([ev]) == {"7", "9", "3"}, tool
        assert _result_numbers([ev], references=False) == set(), tool


def test_prompts_have_no_question_phrasings():
    """§22: prompts describe the class of person questions once; no list of phrasings. A banned phrase may appear in
    ROUTE_SYSTEM only inside a design example (a line that ends with -> NO_RECORDS)."""
    banned = ("who is", "involved in", "relation between", "tell me about", "connected to", "what does")
    texts = {"NAMES_SYSTEM": rprompts.NAMES_SYSTEM, "RECORDS_AGENT_SYSTEM": rprompts.RECORDS_AGENT_SYSTEM,
             "RECORDS_PERSON_NO_RECORDS_MESSAGE": rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE,
             "RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE": rprompts.RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE,
             "RECORDS_PEOPLE_TOOL_MESSAGE": rprompts.RECORDS_PEOPLE_TOOL_MESSAGE,
             "COMBINE_SYSTEM": rprompts.COMBINE_SYSTEM, "KNOWLEDGE_RECORDS_NOTE": rprompts.KNOWLEDGE_RECORDS_NOTE}
    for name, text in texts.items():
        low = " ".join(text.lower().split())
        assert not [b for b in banned if b in low], name
    for line in rprompts.ROUTE_SYSTEM.split("\n"):
        if any(b in line.lower() for b in banned):
            assert line.rstrip().endswith("-> NO_RECORDS"), line
    assert "anything about people the question names: who they are, their roles" in \
        " ".join(rprompts.RECORDS_AGENT_SYSTEM.split())


def test_people_tool_call_with_the_full_name_covers_the_first_name():
    """A first name asked, about_people called with the stored full name it found: the name is covered (no nudge)."""
    spot = {"words": "kilo", "recs": [("Recruiter__c", "a06000000000020AAA", "Kilo Zephyr", None)], "strength": "weak",
            "first_only": True, "asked": None, "close": []}
    tri = Triage(routed=True, route=True, people=[spot])
    ev = _person_tool_ev(names=("Kilo Zephyr",))
    ev.model_result["people"] = [{"ref": "P1", "label": "Kilo Zephyr", "kind": "employee"}]
    assert people_tool_nudges("draft", [ev], tri) == []
    ev.model_result["people"] = [{"ref": "P1", "label": "Zulu Other (#2)", "kind": "employee"}]
    assert len(people_tool_nudges("draft", [ev], tri)) == 1


def test_only_missing_names_get_the_not_found_answer_after_a_namesake_result():
    """§22.8: the question names only a person nobody has; about_people found namesakes (first name only); a draft with
    a 0 for the asked name is no answer: code builds the not-found answer."""
    from sf_assistant.agent.nudges import _no_data_but_not_found
    nf = _error_ev("q1", "E15a", tool="about_people")
    nf.not_found = [{"name": "Zorblax Quentin", "target": "Employee, User, Account or Lead", "object": None,
                     "field": None, "match": "exact", "close": ["Zorblax Prime"]}]
    namesakes = _person_tool_ev("q2", names=("Zorblax",))
    assert not _no_data_but_not_found([nf, namesakes])                       # without the triage: data found
    assert _no_data_but_not_found([nf, namesakes], people_data=False)        # only missing names: none


# ------------------------------------------------------------- review fixes of §22 (fake names only)
def test_is_no_records_first_or_last_line():
    """A justification in prose followed by a last NO_RECORDS line is a NO_RECORDS reply (never merged as an answer)."""
    from sf_assistant.agent.checks import _is_no_records
    assert _is_no_records("NO_RECORDS") and _is_no_records("**NO_RECORDS**") and _is_no_records("`NO_RECORDS`.")
    assert _is_no_records("The question asks for a definition, not record data.\nTherefore, the answer is:\n"
                          "NO_RECORDS\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC.")
    assert not _is_no_records("Kilo Zephyr hosted 3 sessions.\nSource: preprod sandbox copy, synced 2026-10-13.")
    assert not _is_no_records("No records were found for this name.") and not _is_no_records("")
    session = RecordSession(_PeopleStub(), _cfg(), llm=ScriptedLLM(["x"]))
    tri = Triage(routed=True, route=True, people=[_strong("kilo zephyr")])
    msg = session.check_draft("This is a definition question.\nNO_RECORDS", "q", STATUS, routed=True, triage=tri)
    assert msg == rprompts.RECORDS_PERSON_NO_RECORDS_MESSAGE.replace("<<PEOPLE>>", "'kilo zephyr'")


def test_not_found_reply_takes_no_number_from_the_status_line():
    """The status line's copy time ("00:07"), age and object count are no counts: a reply with a 0 after a name was not
    found is no not-found reply at any hour (the suite failed between 00:00 and 00:59 UTC)."""
    import dataclasses as _dc
    late = _dc.replace(STATUS, copied_at_utc="2026-10-15 00:07 UTC", age_hours=10.0)
    nf = [_error_ev("q1", "E4h")]
    src = "\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC (10.0 h ago)."
    assert not not_found_reply("Zulu Nobody conducted 0 internal interviews." + src, nf, "q", late)
    assert not not_found_reply("Zulu Nobody conducted 10 internal interviews." + src, nf, "q", late)
    assert not_found_reply("No employee is named 'Zulu Nobody'." + src, nf, "q", late)
    assert not_found_reply("No employee is named 'Zulu Nobody' in 2026." + src, nf, "how many in 2026", late)


class _NamesStub(_PeopleStub):
    """_PeopleStub with the name index answers of the real tools: a staff first-name close spelling, a last name, a
    client company and a name that is only partly stored."""

    def resolve_person_name(self, name):
        key = " ".join(name.lower().replace(".", "").split())
        if key == "zephyr":   # a last name alone
            return {"words": key, "recs": [self.STORED["kilo zephyr"]], "strength": "strong", "first_only": True,
                    "asked": None, "close": [], "from": "names", "match": "last name"}
        if key == "mr kilo":   # only a word of it is a stored first name
            return {"words": key, "recs": [self.STORED["kilo zephyr"]], "strength": "strong", "first_only": True,
                    "asked": key, "close": [], "partial": "kilo", "from": "names", "match": "partial first name"}
        return super().resolve_person_name(name)

    def staff_close_name(self, name):
        return name.lower() == "kilu"

    def company_name(self, name):
        return name.lower() == "client co"


def test_triage_missing_name_overrules_the_route_only_when_data_backs_it():
    """Review §22: a product or vendor name the names call lists ("Einstein", "Ceipal") is no stored person and no
    close spelling of a staff first name: the route call decides. A close spelling of a staff first name, a person
    named by a first name, or a name of which only a word is stored overrule NO_RECORDS; a lone last name does not."""
    def tri(names, route="NO_RECORDS", q="what is the einstein score"):
        return RecordSession(_NamesStub(), _cfg(), llm=ScriptedLLM(["x"], route=route, names=names)).triage(q)
    t = tri("Einstein")
    assert t.missing == ["Einstein"] and t.routed is False and t.route is False
    assert tri("Einstein", route="RECORDS").routed is True                      # the route call still decides
    assert tri("maybe").missing == [] and tri("Einstein", route="maybe").routed is None
    t = tri("Kilu", q="who is kilu")
    assert t.missing == ["Kilu"] and t.routed is True                          # close to a staff first name
    t = tri("Zorblax Quentin", q="who is zorblax quentin")
    assert t.missing == ["Zorblax Quentin"] and t.routed is True               # the shape of a full name
    t = tri("Zephyr", q="who is zephyr")
    assert [p["words"] for p in t.people] == ["zephyr"] and t.routed is False  # a lone last name: route decides
    assert tri("Zephyr", route="RECORDS", q="who is zephyr").routed is True
    t = tri("Mr Kilo", q="who is mr. kilo")
    assert [p["words"] for p in t.people] == ["mr kilo"] and t.routed is True and t.people_data() is False
    t = tri("Client Co", route="RECORDS", q="how many interviews did client co schedule")
    assert t.missing == [] and t.people == []                                  # a client company: no person


def test_triage_names_call_failure_keeps_only_staff_records():
    """No names call: a weak spot counts only when the route said RECORDS, and only its Employee and User records
    (the first-name index before §22); a first name held only by leads is no person then."""
    class LeadOnly(_PeopleStub):
        def spot_people(self, question):
            return [{"words": "react", "recs": [("Lead", "00Q000000000009AAA", "React Js", None)], "strength": "weak",
                     "first_only": True, "asked": None, "close": []}]
    llm = ScriptedLLM(["x"], route="RECORDS", names=RuntimeError("model down"))
    t = RecordSession(LeadOnly(), _cfg(), llm=llm).triage("how many react trainings were scheduled")
    assert t.people == [] and t.hinted == []
    llm = ScriptedLLM(["x"], route="RECORDS", names="NONE")   # the names call ran and named nobody
    t = RecordSession(LeadOnly(), _cfg(), llm=llm).triage("how many react trainings were scheduled")
    assert t.people == [] and t.hinted == [] and t.routed is True


def test_triage_real_index_partial_and_glued_names(ptools):
    llm = ScriptedLLM(["x"], route="NO_RECORDS", names="Mr. Kilo")
    t = RecordSession(ptools, _cfg(), llm=llm).triage("who is mr. kilo?")
    p = t.people[0]
    assert p["partial"] == "kilo" and p["asked"] == "mr kilo" and p["words"] == "mr kilo" and t.routed is True
    assert t.people_data() is False and t.missing == []
    llm = ScriptedLLM(["x"], route="RECORDS", names="Sierra | Kilo")
    t = RecordSession(ptools, _cfg(), llm=llm).triage("sierra,kilo: how are they related?")
    assert [p["words"] for p in t.people] == ["sierra", "kilo"]
    llm = ScriptedLLM(["x"], route="RECORDS", names="Zephyr Kilo")
    t = RecordSession(ptools, _cfg(), llm=llm).triage("who is zephyr kilo")
    assert t.people[0]["close"] == ["Kilo Zephyr"] and t.people_data() is True


def test_people_answer_says_when_a_close_name_or_a_record_name_is_used():
    res = {"asked": ["kilo zephir"], "people": [{"ref": "P1", "asked": "kilo zephir", "label": "Kilo Zephyr",
                                                 "kind": "employee", "match": "closest stored name", "records": ["R1"]}],
           "matched_records": [{"ref": "R1", "person": "P1", "label": "Kilo Zephyr", "object": "Recruiter__c",
                                "object_label": "Employee"}], "references": []}
    assert people_answer(res).startswith("No record is named 'kilo zephir'; the closest stored name is 'Kilo Zephyr'. "
                                         "Kilo Zephyr (P1) is an employee")
    res = {"asked": ["company one"], "people": [{"ref": "P1", "asked": "company one", "label": "Company One",
                                                 "kind": "record", "match": "record name", "records": ["R1"]}],
           "matched_records": [{"ref": "R1", "person": "P1", "label": "Company One", "object": "Company__c",
                                "object_label": "Company"}], "references": []}
    assert people_answer(res).startswith("'company one' is not a person; a Company record has this name.")


def _look_alike(label="Quinta Zelder", **person):
    """An about_people result for the asked 'Quorra Zeld' (not stored) whose closest stored name is ``label``, a lead
    with an unasked profile (fake data)."""
    p = {"ref": "P1", "label": label, "kind": "lead", "detail": "Contacted", "match": "closest stored name",
         "asked": "Quorra Zeld", "records": ["R1"], "no_business_references": True,
         "profile": {"Status": "Contacted", "LeadSource": "Web", "Plan__c (Plan)": "Basic",
                     "Visa_Kind__c (Visa Kind)": "W1"}, **person}
    return {"asked": ["Quorra Zeld"], "people": [p], "references": [],
            "matched_records": [{"ref": "R1", "person": "P1", "label": label, "object": "Lead", "object_label": "Lead"}]}


def test_people_answer_gives_a_look_alike_closest_name_only_its_name_and_kind():
    """Step-2 fixes of 2026-10-30 (F9, HP03): a closest stored name spelled unlike the asked name (difflib ratio below
    CLOSE_NAME_PROFILE_RATIO) is a look-alike: the not-found lead, its name and its kind, and no detail, profile,
    linked records or references. A typo (one wrong letter: ratio 0.91), a numbered label and an exact match keep the
    profile (the spelling help answers for that person; the profile core rule of 2026-10-22)."""
    from difflib import SequenceMatcher

    from sf_assistant.agent import people_answer as PA

    assert PA.CLOSE_NAME_PROFILE_RATIO == 0.85
    assert SequenceMatcher(None, "quorra zeld", "quinta zelder").ratio() < 0.85
    text = people_answer(_look_alike())
    assert text == ("No record is named 'Quorra Zeld'; the closest stored name is 'Quinta Zelder'. Quinta Zelder (P1) "
                    "is a lead.")
    for unasked in ("Status:", "LeadSource:", "Plan:", "Visa Kind:", "(Contacted)", "Linked records", "Referenced as",
                    "No business record references them."):
        assert unasked not in text, unasked
    for label in ("Quorra Zelt", "Quorra Zelt (#2)"):   # a typo, also with a number suffix: the profile stays
        typo = people_answer(_look_alike(label))
        assert typo.startswith(f"No record is named 'Quorra Zeld'; the closest stored name is '{label}'. ")
        assert "Status: Contacted" in typo and "Plan: Basic" in typo and "(Contacted)" in typo, label
    exact = _look_alike()
    del exact["people"][0]["match"]
    assert "Status: Contacted" in people_answer(exact) and not people_answer(exact).startswith("No record")


def _asked_as(asked: str, label: str) -> dict:
    """:func:`_look_alike` for another asked spelling ``asked`` of the stored ``label``."""
    res = _look_alike(label, asked=asked)
    res["asked"] = [asked]
    return res


def test_people_answer_keeps_the_profile_of_the_asked_name_written_another_way():
    """Q2 of the review of 2026-10-30: about_people also gives "closest stored name" when the stored name is the asked
    person's name written another way (the words in another order, an initial, fewer or more words: the rule it picked
    the person by), and the spelling help reads a one-word ask only for doubled letters or an identical sound. Their
    spelling ratio is below 0.85, but they are the person asked about, so the profile stays; a look-alike (HP03's
    shape) and a one-letter typo in a short full name (ratio 0.83, no word pairs up exactly with 3 letters) still get
    the name and kind only."""
    from difflib import SequenceMatcher

    from sf_assistant.agent import people_answer as PA

    same = [("Zeld Quorra", "Quorra Zeld"),            # another order (ratio 0.55)
            ("Q Zeld", "Quorra Zeld"),                 # an initial (0.71)
            ("Quorra Amon", "Quorra Amon Zeld"),       # fewer words (0.81)
            ("Quorra Amon Zeld", "Quorra Zeld"),       # an extra middle name (0.81)
            ("Quorra", "Quorraa Zeld")]                # one word, doubled letters (resolver reading)
    for asked, label in same:
        assert SequenceMatcher(None, asked.lower(), label.lower()).ratio() < PA.CLOSE_NAME_PROFILE_RATIO, asked
        p = _asked_as(asked, label)["people"][0]
        text = people_answer(_asked_as(asked, label))
        assert not PA._look_alike(p) and "Status: Contacted" in text and "Plan: Basic" in text, asked
        assert text.startswith(f"No record is named '{asked}'; the closest stored name is '{label}'. "), asked
    for asked, label in (("Quorra Zeld", "Quinta Zelder"), ("Zek Qo", "Zak Qo"), ("Quorra", "Quinta Zelder")):
        text = people_answer(_asked_as(asked, label))
        assert PA._look_alike(_asked_as(asked, label)["people"][0]), asked
        assert text.endswith(f"{label} (P1) is a lead.") and "Status:" not in text, asked


def test_people_answer_never_reads_a_cut_or_stopped_result_as_none():
    res = {"asked": ["a", "b"], "people": [{"ref": "P1", "asked": "a", "label": "Alfa One", "kind": "employee",
                                            "records": ["R1"]},
                                           {"ref": "P2", "asked": "b", "label": "Bravo Two", "kind": "employee",
                                            "records": ["R2"], "references_shown": "1 of 3 lookups with records"}],
           "references": [{"person": "P2", "record": "R2", "object": II, "field": "Interviewer__c", "role": "interviewer",
                           "records": 4}],
           "links": [], "pairs_without_links": [["P1", "P2"]], "links_checked": "12 lookups on 5 objects",
           "partial": True, "not_checked": ["Interview__c (time budget)"], "references_partial": True}
    text = people_answer(res)
    assert "No link was found in what was checked; the check stopped early or hit a limit" in text
    assert "No stored record links" not in text and "No business record references them" not in text
    assert "among others" in text and "The reference scan stopped early" in text


def test_code_answer_for_a_name_nobody_has_is_the_not_found_answer(ptools):
    """Review §22: the agent replied NO_RECORDS twice and code ran about_people, which read the records and found no
    one by that name: that is the not-found answer, never "could not be read"."""
    llm = ScriptedLLM(["NO_RECORDS"], route="RECORDS", names="Zorblax Quentin")
    rec = RecordSession(ptools, _cfg(), llm=llm).ask("who is zorblax quentin")
    assert rec.no_records and not rec.code_answer and rec.error == "NO_RECORDS reply after a name was not found"
    assert rec.not_found_answer.startswith("No Employee, User, Account or Lead record is named 'Zorblax Quentin'")
    assert "could not" not in rec.not_found_answer


def test_a_reply_after_a_name_nobody_has_is_replaced_by_the_code_answer(ptools):
    """Smoke run of the review fixes: after an E15a that listed no close name, the agent's reply offered "closest stored
    names" that no result held. When a name is not found and nothing else was found, code's not-found answer stands."""
    src = "\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."
    llm = ScriptedLLM([{"name": "about_people", "args": {"names": ["Zorblax Quentin"]}},
                       "No record is named 'Zorblax Quentin'. The closest stored names are: Zorblax, Quentin "
                       "(Employee)." + src], route="RECORDS", names="Zorblax Quentin")
    rec = RecordSession(ptools, _cfg(), llm=llm).ask("who is zorblax quentin")
    assert rec.no_records and rec.error == "a name was not found; code wrote the not-found answer"
    assert rec.not_found_answer.startswith("No Employee, User, Account or Lead record is named 'Zorblax Quentin' in "
                                           "the records.") and "(Employee)" not in rec.not_found_answer
    assert "closest stored names are" in rec.dropped_answer


def test_only_unstored_names_follow_the_not_found_rules_without_a_failed_call(ptools):
    """Smoke run (lookup Q36 shape): the question names a first name plus a word no stored name has; the agent read
    the people who share the first name (a successful call) and wrote "0 interviews". Those results are no data about
    the asked name, so code's not-found answer stands (code asks about_people for the asked name)."""
    src = "\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."
    llm = ScriptedLLM([{"name": "about_people", "args": {"names": ["kilo"]}},
                       "The name is not stored. 0 internal interviews have been conducted by mr quorz kilo." + src],
                      route="RECORDS", names="Kilo Quorz")
    rec = RecordSession(ptools, _cfg(), llm=llm).ask("how many internal interviews did kilo quorz conduct?")
    assert rec.triage["people_data"] is False and rec.no_records and not rec.code_answer
    assert rec.not_found_answer.startswith("No Employee, User, Account or Lead record is named 'kilo quorz'")
    assert "Kilo Zephyr" in rec.not_found_answer and "0 internal" in rec.dropped_answer
    assert rec.error == "no successful record query"


# ------------------------------------------------------------- people set round 2 (RECORDS_SPEC §22.16)

def _linked_employee_check(records=13, used_fits=None, emp_fits=None):
    """A count through Candidate__c whose value matched an employee's own Account: the Employee lookup of the object
    holds the same person (lookup_check ``linked`` other on Recruiter__c)."""
    emp = _other("Recruiter__c", 168, fits=emp_fits, group="linked", role="responsible marketing recruiter")
    emp.update({"via": "Recruiter_Contact__c (employee person account)", "same_person": True})
    lc = _check(records=records, field="Candidate__c", role="candidate", fits=used_fits, cues=(), others=[emp])
    lc.update({"target": "Account", "target_label": "Account", "person_kind": "candidate"})
    return lc


def test_linked_employee_nudge_for_a_count_on_the_employees_own_account():
    session, _ = _session([_person_ev("q1", 13, _linked_employee_check(), field="Candidate__c")])
    msg = session.check_draft("Delta Person made 13 applications as the candidate.", PERSON_Q, STATUS)
    expected = (rprompts.RECORDS_LINKED_EMPLOYEE_MESSAGE.replace("<<FIELD>>", "Candidate__c")
                .replace("<<ROLE>>", "candidate").replace("<<COUNT>>", "13").replace("<<OBJECT>>", II)
                .replace("<<VIA>>", "Recruiter_Contact__c (employee person account)")
                .replace("<<OTHERS>>", "Recruiter__c (responsible marketing recruiter) 168")
                .replace("<<OTHER_FIELD>>", "Recruiter__c"))
    assert msg == expected and "<<" not in msg and "never add them" in msg
    # also for a 0 through the candidate lookup
    session, _ = _session([_person_ev("q1", 0, _linked_employee_check(records=0), field="Candidate__c")])
    assert session.check_draft("Delta Person made 0 applications.", PERSON_Q, STATUS).startswith(
        "Your count used Candidate__c (candidate): 0.")
    # also when the draft gives the employee's count only after leading with the candidate count
    session, _ = _session([_person_ev("q1", 13, _linked_employee_check(), field="Candidate__c")])
    assert session.check_draft("Delta Person made 13 applications as a candidate.\n- Also responsible marketing "
                               "recruiter on 168 applications.", PERSON_Q, STATUS).startswith("Your count used Candidate__c")
    # not when the draft leads with the employee's count, when a later query used it, or when only the candidate role
    # fits the question's word
    assert session.check_draft("168 as recruiter (`Recruiter__c`); 13 as a candidate (`Candidate__c`).", PERSON_Q,
                               STATUS) is None
    session, _ = _session([_person_ev("q1", 13, _linked_employee_check(), field="Candidate__c"),
                           _person_ev("q2", 168, None, field="Recruiter__c")])
    assert "Your count used Candidate__c" not in (session.check_draft("Delta Person made 13 applications.", PERSON_Q,
                                                                       STATUS) or "")
    assert lookup_nudges("Delta Person made 13 applications.",
                         [_person_ev("q1", 13, _linked_employee_check(used_fits="made"), field="Candidate__c")]) == []
    assert lookup_nudges("Delta Person made 13 applications.",
                         [_person_ev("q1", 13, _linked_employee_check(used_fits="made", emp_fits="made"),
                                     field="Candidate__c")]) != []


def test_breakdown_nudge_reads_a_count_breakdown():
    ev = _person_ev("q1", 8, None, field="Interviewer__c")
    ev.model_result = dict(ev.model_result, breakdown={"Status__c": {"Unscheduled": 4, "(blank)": 4}})
    session, _ = _session([ev])
    msg = session.check_draft("Delta Person has 8 mocks:\n- Unscheduled: 8\n" + SOURCE_56, PERSON_Q, STATUS)
    assert msg and msg.startswith(rprompts.RECORDS_BREAKDOWN_MESSAGE.split("<<")[0]) and "Unscheduled: 8" in msg
    assert session.check_draft("Delta Person has 8 mocks (`Interviewer__c`):\n- Unscheduled: 4\n- blank status: 4\n"
                               + SOURCE_56, PERSON_Q, STATUS) is None


def test_round2_messages():
    assert "reply exactly NO_RECORDS" in rprompts.RECORDS_RUN_QUERY_MESSAGE
    assert "Keep every count and record your answer already gives" in rprompts.RECORDS_ELSEWHERE_MESSAGE
    assert "Keep every record and count your answer already gives" in rprompts.RECORDS_BOTH_OBJECTS_MESSAGE
    assert "another word of the question already names one of them" in rprompts.RECORDS_BOTH_OBJECTS_MESSAGE
    text = rprompts.render_records_system(STATUS)
    assert "gives a breakdown (records per status" in text and "give every link" in text
    assert "not only the first few" in text and "one short bullet per role, link or person" in text


class _StatusTools(LookupTools):
    def status_values(self, objects):
        return ["Completed", "Scheduled", "Unscheduled", "In Progress"] if II in objects else []


def test_breakdown_nudge_without_any_split_result_reads_the_status_values():
    """A status split written after a count that split nothing (the 0-12 audit allowance let it through)."""
    ev = [_person_ev("q1", 18, None, field="Interviewer__c")]
    session = RecordSession(_StatusTools(ev), _cfg(), llm=ScriptedLLM(["x"]))
    draft = ("Delta Person took 18 internal interviews (`Interviewer__c`).\n*   **Completed:** 10 internal interviews\n"
             "*   **In Progress:** 8 internal interviews\n" + SOURCE_56)
    msg = session.check_draft(draft, PERSON_Q, STATUS)
    assert msg == rprompts.RECORDS_BREAKDOWN_MESSAGE.replace("<<ITEMS>>", "Completed: 10; In Progress: 8")
    # a 0, a label that is no status value, or no status values: nothing
    assert breakdown_nudges("- Completed: 0\n- Week 2: 3\n" + SOURCE_56, ev, {"completed"}) == []
    assert breakdown_nudges(draft, ev) == [] and breakdown_nudges(draft, ev, set()) == []


# ------------------------------------------------------------- quick fixes after round 2 (RECORDS_SPEC §22.17)

from sf_assistant.agent.checks import drop_ref_codes, ref_codes_left, reference_codes  # noqa: E402
from sf_assistant.agent.merge import merge_cut_off  # noqa: E402
from sf_assistant.agent.nudges import covers_all_named  # noqa: E402


def _people_ev(qid="q1", people=(("P1", "Kilo Zephyr", "employee"), ("P2", "Kilo Zephyr", "user")),
               records=(("R1", "P1", "Kilo Zephyr"), ("R2", "P2", "Kilo Zephyr"))):
    """An about_people result with its reference codes (P<n> people, R<n> their records)."""
    result = {"query_id": qid, "asked": ["kilo zephyr"],
              "people": [{"ref": r, "label": lab, "kind": k} for r, lab, k in people],
              "matched_records": [{"ref": r, "person": p, "label": lab, "object": "Recruiter__c"} for r, p, lab in records],
              "applied": {"names": ["kilo zephyr"]}}
    return QueryEvidence(query_id=qid, tool="about_people", kind="data", object="Recruiter__c",
                         arguments={"names": ["kilo zephyr"]}, ok=True, error=None, applied={"names": ["kilo zephyr"]},
                         notes=[], statements=[], tables=[], synced_at={}, summary="2 people", model_result=result,
                         elapsed_ms=1.0)


class _FinishLLM:
    """One merge reply with a given finish reason and completion token count."""

    def __init__(self, content, finish="stop", completion_tokens=50):
        self.content, self.finish, self.tokens, self.calls = content, finish, completion_tokens, []

    def chat(self, messages, tools=None, max_tokens=None, **_):
        self.calls.append({"max_tokens": max_tokens})
        return {"content": self.content, "tool_calls": [], "finish_reason": self.finish,
                "usage": {"prompt_tokens": 10, "completion_tokens": self.tokens}}


def test_merge_input_names_are_working_notes():
    """P23 merged: '... potentially misattributed in draft, but RECORD FACTS show P4/...' passed every cleaner."""
    for text in ("Kilo Zephyr has 3 trainings (or potentially misattributed in draft, but RECORD FACTS show 3).\n"
                 + SOURCE_56, "The record answer says 37 offers.\n" + SOURCE_56,
                 "37 offers, as the KNOWLEDGE ANSWER explains.\n" + SOURCE_56):
        assert working_notes(text), text
    for text in ("3 quotes are in Draft status.\n" + SOURCE_56, "The flow is Draft in production; 37 offers.\n" + SOURCE_56,
                 "37 records hold a field check value.\n" + SOURCE_56):
        assert not working_notes(text), text


def test_combine_rejects_merge_input_names():
    llm = ScriptedLLM(["37 client interviews ended with an offer, but RECORD FACTS show the same 37.\n" + SOURCE_56])
    m = combine(llm, "How many interviews ended with an offer?", "`Interview_Outcome__c` holds it.", _rec(), STATUS)
    assert not m.merged and m.text == _rec().answer and m.error == "the merged answer reads as working notes"


def test_reference_codes_dropped_or_named():
    ev = [_count_evidence(), _people_ev()]
    codes = reference_codes(ev)
    assert codes == {"P1": "Kilo Zephyr", "P2": "Kilo Zephyr", "R1": "Kilo Zephyr", "R2": "Kilo Zephyr"}
    text = ("**P1 (Employee)**: Kilo Zephyr (Employee, P1) has 37 offers; the Lead record (R2) was converted.\n"
            "P4/Kilo Zephyr and P1/Kilo Zephyr; Q2 and REC-P1 stay.\n" + SOURCE_56)
    out = drop_ref_codes(text, codes)
    assert out.startswith("**Kilo Zephyr (Employee)**: Kilo Zephyr (Employee) has 37 offers; the Lead record was converted.")
    assert "P4/Kilo Zephyr and Kilo Zephyr;" in out and "Q2 and REC-P1 stay." in out    # P4 is no code of the facts
    assert ref_codes_left(out, codes) == [] and out.endswith(SOURCE_56)
    assert drop_ref_codes(text, {}) == text                           # no about_people result: nothing changes
    # one code for two different labels (two about_people calls): no safe name, the code stays and is reported
    two = reference_codes([_people_ev(), _people_ev("q3", people=(("P1", "Sierra Tango", "candidate"),), records=())])
    assert two["P1"] == "" and ref_codes_left(drop_ref_codes("P1 owns 37.", two), two) == ["P1"]
    assert ref_codes_left("P1 owns 37.", two, question="who is P1") == []


def test_combine_cleans_or_rejects_reference_codes():
    ev = [_count_evidence(), _people_ev()]
    llm = ScriptedLLM(["Kilo Zephyr (Employee, P1) handled 37 client interviews that ended with an offer.\n" + SOURCE_56])
    m = combine(llm, "How many interviews ended with an offer?", "`Interview_Outcome__c` holds it.",
                _rec(evidence=ev), STATUS)
    assert m.merged and m.text.startswith("Kilo Zephyr (Employee) handled 37") and "P1" not in m.text
    ev2 = ev + [_people_ev("q3", people=(("P1", "Sierra Tango", "candidate"),), records=())]
    llm = ScriptedLLM(["P1 handled 37 client interviews that ended with an offer.\n" + SOURCE_56])
    m = combine(llm, "How many interviews ended with an offer?", "`Interview_Outcome__c` holds it.",
                _rec(evidence=ev2), STATUS)
    assert not m.merged and m.error == "the merged answer names internal reference codes (P1)"


def test_merge_cut_off():
    """P38 merged ended '... with a joining date of 2024-0' (the call had max_tokens=900)."""
    whole = "37 client interviews ended with an offer.\n" + SOURCE_56
    assert not merge_cut_off({"finish_reason": "stop", "usage": {"completion_tokens": 40}}, whole)
    assert merge_cut_off({"finish_reason": "length"}, whole)
    assert merge_cut_off({"usage": {"completion_tokens": MERGE_MAX_TOKENS}}, whole)

    class Raw:   # the okeval client's raw SDK response
        choices = [type("C", (), {"finish_reason": "length"})()]

    assert merge_cut_off({"raw": Raw()}, whole)
    for cut in ("- Sierra Tango is an employee with a joining date of 2024-0", "Kilo Zephyr handled 37 offers and",
                "- Kilo Zephyr (`Interviewer__c", "**Kilo Zephyr** handled 37 offers in the"):
        assert merge_cut_off({}, "37 offers.\n" + cut), cut
    for fine in ("37 offers.\n- **Total count**: 37", "37 offers.\n- 8 Completed", "37 offers ended with an offer."):
        assert not merge_cut_off({}, fine), fine                  # no Source line, but the last line is whole
    assert not merge_cut_off({}, "37 offers and\n" + SOURCE_56)    # a Source line: the reply ended


def test_combine_rejects_a_cut_off_merge():
    for llm in (_FinishLLM("37 client interviews ended with an offer.\n" + SOURCE_56, finish="length"),
                _FinishLLM("37 client interviews ended with an offer; the first one on 2026-0", finish="stop")):
        m = combine(llm, "How many interviews ended with an offer?", "`Interview_Outcome__c` holds it.", _rec(), STATUS)
        assert not m.merged and m.text == _rec().answer and m.error.startswith("the merged answer was cut off")
        assert llm.calls[0]["max_tokens"] == MERGE_MAX_TOKENS
    m = combine(_FinishLLM("37 client interviews ended with an offer.\n" + SOURCE_56), "How many interviews ended with an "
                "offer?", "`Interview_Outcome__c` holds it.", _rec(), STATUS)
    assert m.merged


def test_openers_and_self_talk_dropped():
    """P15 RO opened 'Good - X resolves to the same Employee record ... So the answer is:'; P20 RO kept "Let's double
    check if 'make' could mean ..."."""
    text = ('Good — "Kilo Zephyr" (capital Z) resolves to the same Employee record "Kilo zephyr". There is only one '
            "Employee record for this person.\n\nSo the answer is:\n\n**Kilo Zephyr** hosted **23 sessions** last month:\n"
            "- 18 Training sessions\n- 5 Internal Interview sessions\n\n" + SOURCE_56)
    out = drop_working_sentences(strip_reasoning(text))
    assert out.startswith("**Kilo Zephyr** hosted **23 sessions**") and "answer is" not in out and "Good" not in out
    assert drop_working_sentences('Good - "Kilo Zephyr" is one Employee record. It hosted 23 sessions.\n' + SOURCE_56) \
        .startswith('"Kilo Zephyr" is one Employee record. It hosted 23 sessions.')
    assert drop_working_sentences("Perfect, the count is 23.\n" + SOURCE_56).startswith("The count is 23.")
    draft = ("Kilo Zephyr has 168 application records as the recruiter.\n\nLet's double check if \"make\" could mean "
             "\"submit as a candidate\".\n\nKilo Zephyr made **168** applications.\n" + SOURCE_56)
    assert working_notes(draft) and "double check" not in drop_working_sentences(draft)
    assert working_notes("Let's re-check the counts.\n37 offers.") and working_notes("Good — 37 offers.")
    assert drop_working_sentences('The question "how many applications did kilo make" most likely refers to the '
                                  "recruiter. Kilo made 168.\n" + SOURCE_56).startswith("Kilo made 168.")
    assert drop_working_sentences("Based on the queries: 37 offers.\n" + SOURCE_56).startswith("37 offers.")
    # business wording stays: a value named Good, a rating list, "Good-standing"
    for keep in ("Good-standing candidates: 23.", "Good, Fair and Poor are the rating values.", "- Good: 23"):
        assert drop_working_sentences(keep + "\n" + SOURCE_56).startswith(keep), keep
        assert not working_notes(keep + "\n" + SOURCE_56), keep


def test_covers_all_named():
    for text in ("No other Kilo or Sierra has any training records.", "No stored records link any Kilo (employee or "
                 "candidate) to any Sierra.", "None of the people named 'Kilo' hosts a session.", "Every Kilo is linked."):
        assert covers_all_named(text, "kilo"), text
    assert covers_all_named("No other Kilo or Sierra has any training records.", "sierra")
    for text in ("Kilo Zephyr hosted 0 sessions.", "There is no Kilo Zephyr session.", "No, Kilo Zephyr is not linked.",
                 "no other records", ""):
        assert not covers_all_named(text, "kilo"), text
    assert not covers_all_named("No other Kilo has any.", "")


def test_forced_answer_covering_every_namesake_gets_no_footer(ptools):
    """P23 RO: the footer 'this answer does not cover them' contradicted the body's 'No other Kilo ... has any ...'."""
    class Budget(ScriptedLLM):
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            if messages[0].get("content") in (rprompts.ROUTE_SYSTEM, rprompts.NAMES_SYSTEM):
                return super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)
            if tools is None:
                return {"content": "Kilo Zephyr hosted 0 sessions (`Host_User__c`); no other Kilo hosted a session.",
                        "tool_calls": [], "usage": {}}
            return {"content": None, "tool_calls": [{"id": f"c{len(messages)}", "name": "count_records", "arguments": {
                "object": "Session__c", "filters": [{"field": "Host_User__c", "op": "eq", "value": "Kilo Zephyr"}]}}],
                "usage": {}}

    rec = RecordSession(ptools, _cfg(), llm=Budget(["x"], route="RECORDS", names="kilo"),
                        max_steps=2).ask("how many sessions did kilo host")
    assert "no other Kilo hosted a session" in rec.answer and "Other stored people" not in rec.answer
