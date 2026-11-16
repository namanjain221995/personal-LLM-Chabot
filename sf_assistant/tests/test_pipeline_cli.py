"""cli + pipeline (was ask.py) end to end, offline: synthetic warehouse (tests/warehouse_fixture.py) and a scripted fake LLM.

Run from the ``object_knowledge_eval`` directory::

    python -m pytest -q tests
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import sf_assistant.cli
import sf_assistant.evaluation.score_records
import sf_assistant.pipeline
from sf_assistant.config import KNOWLEDGE_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.knowledge.search import KnowledgeIndex  # noqa: E402
from sf_assistant.agent.mock_llm import MockClient  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge  # noqa: E402
from sf_assistant.tools.knowledge_tools import ToolRegistry  # noqa: E402
from sf_assistant.config import load_config  # noqa: E402
from sf_assistant.agent import knowledge_prompts as prompts  # noqa: E402
from sf_assistant.agent.record_prompts import KNOWLEDGE_RECORDS_NOTE  # noqa: E402

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
OFFER_ARGS = {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "eq",
                                                      "value": "Offer Received"}]}
RECORD_ANSWER = ("2 client interviews ended with an offer (`Interview_Outcome__c` = 'Offer Received').\n"
                 "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")
MERGED_ANSWER = ("2 client interviews ended with an offer.\n- `Interview_Outcome__c` = 'Offer Received' marks an "
                 "offer.\nSource: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")
#: A lean facts block (its "- " bullets) and what the merge by code (SFA_MERGE=code, the default since 2026-10-29)
#: makes of it with RECORD_ANSWER: the record answer, a blank line and the bullet before its Source line.
FACTS = "- `Interview_Outcome__c` = 'Offer Received' marks an offer."
CODE_MERGED = ("2 client interviews ended with an offer (`Interview_Outcome__c` = 'Offer Received').\n\n" + FACTS
               + "\nSource: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")


def _load_ask():
    spec = importlib.util.spec_from_file_location("okeval_ask_cli", sf_assistant.cli.__file__)   # a fresh module per test
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeLLM:
    """One fake for every role, routed by the system prompt (record agent, merge, knowledge agent).

    The record agent replies ``record_script[n]`` where n = tool results so far (last one repeats).
    """

    def __init__(self, record_script, merge_answer=MERGED_ANSWER, knowledge_answer="`Interview_Outcome__c` holds it.",
                 route_answer="RECORDS"):
        self.record_script = list(record_script)
        self.route_answer = route_answer
        self.merge_answer = merge_answer
        self.knowledge_answer = knowledge_answer
        self.roles: list[str] = []
        self.knowledge_calls: list[dict] = []   # knowledge-agent calls (full or lean): system, tools, max_tokens
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def chat(self, messages, tools=None, **_):
        system = str(messages[0].get("content") or "")
        n = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": 5, "completion_tokens": 1}
        if system.startswith("Decide whether answering"):
            self.roles.append("route")
            return {"content": self.route_answer, "tool_calls": [], "usage": usage}
        if system.startswith("List the names of people"):   # the names call of the record session's triage
            self.roles.append("names")
            return {"content": getattr(self, "names_answer", "NONE"), "tool_calls": [], "usage": usage}
        if system.startswith("You write the ONE final answer"):
            self.roles.append("merge")
            return {"content": self.merge_answer, "tool_calls": [], "usage": usage}
        if system.startswith("You answer questions about Salesforce RECORDS"):
            self.roles.append("record")
            step = self.record_script[min(n, len(self.record_script) - 1)]
            if isinstance(step, dict):
                return {"content": None, "tool_calls": [{"id": f"r{n}", "name": step["name"], "arguments": step["args"]}],
                        "usage": usage}
            return {"content": step, "tool_calls": [], "usage": usage}
        self.roles.append("lean" if system.startswith("You add FIELD MEANINGS") else "knowledge")
        self.knowledge_calls.append({"system": system, "tools": [t["function"]["name"] for t in tools or []],
                                     "messages": [dict(m) for m in messages], "max_tokens": _.get("max_tokens")})
        if tools and n == 0:
            return {"content": None, "tool_calls": [{"id": "k0", "name": "search_knowledge",
                                                      "arguments": {"query": "status", "k": 3}}], "usage": usage}
        return {"content": self.knowledge_answer, "tool_calls": [], "usage": usage}


def _cfg(url="http://localhost:8000/v1"):
    return load_config(base_url=url, api_key="test-key", max_tool_steps=6)


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


@pytest.fixture()
def fixture_tools(tmp_path):
    pytest.importorskip("duckdb")
    from warehouse_fixture import open_fixture

    tools = open_fixture(tmp_path)
    yield tools
    tools.close()


def _run(ask_cli, text, llm, session, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        row = ask_cli.ask(text, _cfg(), _registry(), None, llm, 1, False, session, 20, **kw)
    return out.getvalue(), row


# ----------------------------------------------------------------------------- moved from test_records.py
def test_file_mode_reads_questions_and_prints_a_summary(tmp_path):
    ask_cli = _load_ask()
    qfile = tmp_path / "q.txt"
    qfile.write_text("# comment\nFirst?\n\n  Second?  \n# expect: x\n", encoding="utf-8")
    assert ask_cli.read_questions(str(qfile)) == ["First?", "Second?"]
    seen = []

    def ask_one(text, n):
        seen.append((n, text))
        return {"question": text, "seconds": 1.0, "answer": "merged" if n == 1 else "knowledge",
                "records": "1 q, 3 rows" if n == 1 else "not needed", "rq": 1 if n == 1 else 0,
                "audit": "ok" if n == 1 else "-", "ktools": 2, "rtools": 2 if n == 1 else 1,
                "error": None, "record_error": None}

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ask_cli.run_file(["First?", "Second?"], ask_one)
    assert seen == [(1, "First?"), (2, "Second?")]
    text = out.getvalue()
    assert "##### 1/2" in text and "SUMMARY" in text
    assert "  #  answer     records          rq  audit  ktools  rtools       s  question" in text
    assert "  1  merged     1 q, 3 rows       1  ok          2       2     1.0  First?" in text
    assert ("2 questions, 1 merged with records, 0 answered from records only, 1 knowledge only, "
            "0 record failures, 0 without an answer, avg 1.0 s") in text


# ----------------------------------------------------------------------------- end to end
@pytest.mark.parametrize("merge_by", ["code", "model"])
def test_merged_answer_and_records_block(fixture_tools, monkeypatch, merge_by):
    """The merged answer and the RECORDS block, with the merge by code (the default since 2026-10-29: the record
    answer plus the facts block's bullet, no merge call) and with the merge call (SFA_MERGE=model, as before)."""
    from sf_assistant.agent.record_session import RecordSession

    monkeypatch.setenv("SFA_MERGE", merge_by)
    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], knowledge_answer=FACTS)
    session = RecordSession(fixture_tools, _cfg(), llm=llm)
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, session)
    head = out.split("\n-- answer merged", 1)[0]
    assert head.rstrip().endswith(CODE_MERGED if merge_by == "code" else MERGED_ANSWER)
    assert "-- answer merged from the knowledge agent and 1 record query (" in out
    assert "-- knowledge agent (lean): 1 tool call(s), 2 model call(s)" in out   # routed RECORDS: lean step
    assert '   count_records({"object":"Interview__c","filters":' in out and "ok=True  -> q1" in out
    for part in ("-" * 72 + " RECORDS", "source  : ", "read-only local copy of sf-local-ai-orchestrator-1:/data/"
                 "warehouse.read.duckdb", "origin  : preprod SANDBOX copy (Organization.IsSandbox = true), not production",
                 "today   : 2026-10-13 pinned (America/New_York)", "[q1] count_records  Interview__c  -> 2",
                 "     applied   : Interview_Outcome__c = 'Offer Received'",
                 '     sql[1]    : SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE t0."Interview_Outcome__c" = ?\n',
                 "     params[1] : ['Offer Received']\n", "     rows[1]   : n", "Interview__c (10 records, synced ",
                 # the legacy-value note states its overlap with the filter, from a second statement
                 "(2 records, legacy value); 1 of them matches all conditions of this query, the other 1 does not",
                 '     sql[2]    : SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE t0."Interview_Status__c" = ? '
                 'AND t0."Interview_Outcome__c" = ?',
                 "     params[2] : ['Offer Received', 'Offer Received']"):
        assert part in out, part
    assert "Knowledge answer:" not in out
    assert llm.roles.count("merge") == (1 if merge_by == "model" else 0) and llm.roles.count("route") == 1
    assert "knowledge" not in llm.roles
    assert row["answer"] == "merged" and row["records"] == "1 q, 1 row" and row["rq"] == 1 and row["audit"] == "ok"
    assert row["jsonl"]["knowledge_mode"] == "lean" and row["jsonl"]["knowledge_model_calls"] == 2
    assert row["jsonl"]["record_facts"][0]["result"]["count"] == 2
    assert row["jsonl"]["merge_by"] == merge_by and "merge_switch" not in row["jsonl"]


def test_merge_with_invented_number_falls_back_to_the_record_answer(fixture_tools, monkeypatch):
    from sf_assistant.agent.record_session import RecordSession

    monkeypatch.setenv("SFA_MERGE", "model")   # the merge call's number audit (the merge by code writes no number)
    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], merge_answer="There are 77 offers.")
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert out.split("=" * 72 + "\n", 2)[2].startswith(RECORD_ANSWER)
    assert "-- merge rejected (numbers not in the record results: 77); the record agent's answer is shown" in out
    assert "\nKnowledge answer:\n`Interview_Outcome__c` holds it." in out
    assert row["answer"] == "records" and row["audit"] == "ok"


def test_no_records_reply_prints_not_needed_and_skips_the_merge(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    llm = FakeLLM(["NO_RECORDS"])
    out, row = _run(ask_cli, "What values can Status__c take?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert "-- records: not needed for this question" in out
    assert "merge" not in llm.roles and "RECORDS" not in out.replace("NO_RECORDS", "")
    assert "`Interview_Outcome__c` holds it." in out
    assert row["answer"] == "knowledge" and row["records"] == "not needed" and row["audit"] == "-"
    # the route said RECORDS (lean step), the record agent NO_RECORDS: the full knowledge agent answers after all
    assert llm.roles.count("route") == 1 and "lean" in llm.roles and llm.roles[-1] == "knowledge"
    assert llm.knowledge_calls[-1]["system"].endswith(KNOWLEDGE_RECORDS_NOTE)
    assert "-- knowledge agent (lean step, then full): 1 tool call(s), 2 model call(s)" in out
    assert row["jsonl"]["knowledge_mode"] == "lean+full"


def test_records_only_mode(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER])
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm,
                    RecordSession(fixture_tools, _cfg(), llm=llm), records_only=True)
    assert "knowledge" not in llm.roles and "lean" not in llm.roles and "merge" not in llm.roles
    assert llm.roles.count("route") == 1 and row["jsonl"]["knowledge_mode"] is None   # routed inside the session
    assert RECORD_ANSWER in out and "-- answer from the record agent only: 1 record query" in out
    assert "-- record agent: 1 tool call(s)" in out and "-- knowledge agent" not in out
    assert row["answer"] == "records" and row["ktools"] == 0 and row["rtools"] == 1


def test_route_no_records_skips_the_record_agent(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], route_answer="NO_RECORDS")
    out, row = _run(ask_cli, "What values can Interview_Outcome__c take?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert "record" not in llm.roles and "merge" not in llm.roles and llm.roles[0] == "route"
    assert "-- records: not needed for this question" in out
    # value spots (2026-10-28): the not-run line also names the stored record values that could overrule the route
    assert ("-- record agent: not run (the route call found no record data in the question, and no name or stored "
            "record value in it overrules that)") in out
    assert "-" * 72 + " RECORDS" not in out
    assert row["answer"] == "knowledge" and row["records"] == "not needed" and row["rtools"] == 0
    # a metadata-only question keeps the full knowledge agent exactly as before: same prompt, every tool, no cap
    assert llm.roles.count("route") == 1 and "lean" not in llm.roles
    full = llm.knowledge_calls[0]
    assert full["system"] == prompts.AGENT_SYSTEM + KNOWLEDGE_RECORDS_NOTE and full["max_tokens"] is None
    assert full["tools"] == [t["function"]["name"] for t in _registry().openai_tools()]
    assert "-- knowledge agent: 1 tool call(s)" in out and row["jsonl"]["knowledge_mode"] == "full"


# ----------------------------------------------------------------------------- lean knowledge step (§21.19)
def test_a_record_question_gets_the_lean_knowledge_step(fixture_tools):
    from sf_assistant.agent.lean import LEAN_MAX_TOKENS
    from sf_assistant.agent.record_prompts import render_lean_system
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    # a facts block of "- " bullets, as the lean step writes it: merged by code (the default) and by the merge call
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], knowledge_answer=FACTS)
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert llm.roles[0] == "route" and llm.roles.count("route") == 1      # routed once, before both agents
    assert "knowledge" not in llm.roles and llm.roles.count("lean") == 2
    single = ("search_knowledge", "search_fields", "get_field", "get_picklist_values")   # one knowledge file
    for call in llm.knowledge_calls:
        assert call["system"] == render_lean_system(3, single) and call["max_tokens"] == LEAN_MAX_TOKENS
    assert "for example search_knowledge with" in llm.knowledge_calls[0]["system"]
    assert sorted(llm.knowledge_calls[0]["tools"]) == ["get_field", "get_picklist_values", "search_fields",
                                                       "search_knowledge"]
    assert llm.knowledge_calls[1]["tools"] == llm.knowledge_calls[0]["tools"]  # answered on call 2 of 3
    assert "-- knowledge agent (lean): 1 tool call(s), 2 model call(s)" in out
    j = row["jsonl"]
    assert j["knowledge_mode"] == "lean" and j["knowledge_model_calls"] == 2 and j["answer_kind"] == "merged"
    assert j["route_seconds"] is not None and j["knowledge_seconds"] is not None and j["merge_seconds"] is not None


def test_the_lean_step_stays_within_its_budget_in_ask(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    class Greedy(FakeLLM):   # the knowledge side asks for 2 more tools on every call that offers tools
        def chat(self, messages, tools=None, **kw):
            system = str(messages[0].get("content") or "")
            if not system.startswith("You add FIELD MEANINGS"):
                return super().chat(messages, tools=tools, **kw)
            self.roles.append("lean")
            self.knowledge_calls.append({"tools": [t["function"]["name"] for t in tools or []]})
            if tools:
                k = len(self.knowledge_calls)
                return {"content": None, "usage": {}, "tool_calls": [
                    {"id": f"k{k}{i}", "name": "search_knowledge", "arguments": {"query": "offer", "k": 9}}
                    for i in range(2)]}
            return {"content": "- `Interview_Outcome__c` holds the outcome.", "tool_calls": [], "usage": {}}

    llm = Greedy([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER])
    out, row = _run(ask_cli := _load_ask(), "How many interviews ended with an offer?", llm,
                    RecordSession(fixture_tools, _cfg(), llm=llm))
    assert llm.roles.count("lean") == 3 and [bool(c["tools"]) for c in llm.knowledge_calls] == [True, True, False]
    assert "-- knowledge agent (lean): 4 tool call(s), 3 model call(s)" in out
    assert out.count("ok=True  chunks=") == 3 and out.count("ok=False  chunks=0") == 1   # the 4th did not run
    assert row["jsonl"]["knowledge_model_calls"] == 3 and row["answer"] == "merged" and ask_cli is not None


def test_a_lean_step_without_a_tool_result_shows_the_record_answer(fixture_tools):
    from sf_assistant.agent.lean import NO_TOOL_RESULT_ERROR
    from sf_assistant.agent.record_session import RecordSession

    class Memory(FakeLLM):   # the lean side answers from memory every time: no tool result, so no facts block
        def chat(self, messages, tools=None, **kw):
            if str(messages[0].get("content") or "").startswith("You add FIELD MEANINGS"):
                self.roles.append("lean")
                return {"content": "- `Assigned_Recruiter__c` is the support person.", "tool_calls": [], "usage": {}}
            return super().chat(messages, tools=tools, **kw)

    ask_cli = _load_ask()
    llm = Memory([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER])
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert "merge" not in llm.roles and llm.roles.count("lean") == 2      # sent back once, then not used
    assert out.split("=" * 72 + "\n", 2)[2].startswith(RECORD_ANSWER)
    assert "-- could not merge (the knowledge agent gave no answer); the record agent's answer is shown" in out
    assert f"\nerror: {NO_TOOL_RESULT_ERROR}" in out and "\nKnowledge answer:\n(no answer)" in out
    j = row["jsonl"]
    assert row["answer"] == "records" and j["answer_body"] == RECORD_ANSWER and j["knowledge_answer"] == ""
    assert j["merge_error"] == "the knowledge agent gave no answer" and j["knowledge_mode"] == "lean"
    assert row["error"] == NO_TOOL_RESULT_ERROR and j["lean_error"] is None


def test_a_lean_step_that_raises_falls_back_to_the_full_knowledge_agent(fixture_tools, monkeypatch):
    import sf_assistant.agent.lean
    from sf_assistant.agent.record_session import RecordSession

    def broken(*a, **k):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(sf_assistant.agent.lean, "lean_knowledge_answer", broken)
    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER])
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert "lean" not in llm.roles and llm.roles.count("knowledge") == 2 and llm.roles.count("merge") == 1
    assert llm.knowledge_calls[0]["system"] == prompts.AGENT_SYSTEM + KNOWLEDGE_RECORDS_NOTE
    assert "-- knowledge agent (lean step, then full): 1 tool call(s), 2 model call(s)" in out
    assert "   the lean step failed (TypeError: unhashable type: 'list'); the full knowledge agent answered" in out
    j = row["jsonl"]
    assert row["answer"] == "merged" and j["knowledge_mode"] == "lean+full"
    assert j["lean_error"] == "TypeError: unhashable type: 'list'"


def test_a_failing_record_agent_after_a_records_route_shows_the_facts_block(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    class Down(FakeLLM):   # the record agent's model calls fail
        def chat(self, messages, tools=None, **kw):
            if str(messages[0].get("content") or "").startswith("You answer questions about Salesforce RECORDS"):
                self.roles.append("record")
                raise RuntimeError("model down")
            return super().chat(messages, tools=tools, **kw)

    facts = "- `Interview_Outcome__c` holds the outcome of the interview."
    ask_cli = _load_ask()
    llm = Down([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], knowledge_answer=facts)
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert "merge" not in llm.roles and llm.roles.count("lean") == 2 and "knowledge" not in llm.roles
    assert out.split("=" * 72 + "\n", 2)[2].startswith(facts + "\n")
    assert "-- records: no answer from the records (RuntimeError: model down); knowledge answer shown" in out
    assert row["answer"] == "knowledge" and row["records"] == "error" and row["jsonl"]["knowledge_mode"] == "lean"
    assert row["jsonl"]["record_error"] == "RuntimeError: model down"


def test_the_lean_merge_does_not_take_numbers_from_the_facts_prose(fixture_tools, monkeypatch):
    """§21.19: a count the facts block still quotes cannot pass the audit; the full agent's answer keeps the old audit.
    The merge call's audit (SFA_MERGE=model); the merge by code drops such a bullet (tests/test_code_merge.py)."""
    from sf_assistant.agent.record_session import RecordSession

    monkeypatch.setenv("SFA_MERGE", "model")
    facts = "- `Interview_Outcome__c` holds the outcome; 1,212 interviews have one."   # no snapshot word: kept
    merged = ("2 client interviews ended with an offer.\n- `Interview_Outcome__c` holds the outcome (1,212 have "
              "one).\nSource: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")
    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], knowledge_answer=facts,
                  merge_answer=merged)
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert row["jsonl"]["knowledge_mode"] == "lean" and row["answer"] == "records"
    assert row["jsonl"]["rejected_numbers"] == ["1212"]
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], knowledge_answer=facts,
                  merge_answer=merged)
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm),
                    lean_knowledge=False)
    assert row["jsonl"]["knowledge_mode"] == "full" and row["answer"] == "merged"


def test_an_unclear_route_keeps_the_full_knowledge_agent(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER], route_answer="maybe")
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm))
    assert llm.roles.count("route") == 1 and "lean" not in llm.roles and "record" in llm.roles
    assert llm.knowledge_calls[0]["system"] == prompts.AGENT_SYSTEM + KNOWLEDGE_RECORDS_NOTE
    assert row["answer"] == "merged" and row["jsonl"]["knowledge_mode"] == "full"


def test_full_knowledge_keeps_the_path_before_the_lean_step(fixture_tools):
    from sf_assistant.agent.record_session import RecordSession

    ask_cli = _load_ask()
    llm = FakeLLM([{"name": "count_records", "args": OFFER_ARGS}, RECORD_ANSWER])
    out, row = _run(ask_cli, "How many interviews ended with an offer?", llm, RecordSession(fixture_tools, _cfg(), llm=llm),
                    lean_knowledge=False)
    assert llm.roles.count("route") == 1 and "lean" not in llm.roles     # routed inside the record session
    assert all(c["system"] == prompts.AGENT_SYSTEM + KNOWLEDGE_RECORDS_NOTE for c in llm.knowledge_calls)
    assert "-- knowledge agent: 1 tool call(s), 2 model call(s)" in out
    j = row["jsonl"]
    assert row["answer"] == "merged" and j["knowledge_mode"] == "full" and j["route_seconds"] is None
    with pytest.raises(SystemExit, match="--full-knowledge is for merged mode"):
        ask_cli.main(["--full-knowledge", "--records-only", "q"])


def _main(ask_cli, argv, llm_factory):
    saved = sf_assistant.pipeline.LLM   # build() lives in the pipeline module
    sf_assistant.pipeline.LLM = llm_factory
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = ask_cli.main(argv)
    finally:
        sf_assistant.pipeline.LLM = saved
    return code, out.getvalue()


def test_remote_endpoint_turns_records_off(monkeypatch, tmp_path):
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    monkeypatch.setenv("OKEVAL_BASE_URL", "https://api.example.com/v1")
    ask_cli = _load_ask()
    monkeypatch.setattr(ask_cli, "KNOWLEDGE_DIR", str(make_knowledge(tmp_path)))
    code, out = _main(ask_cli, ["--knowledge", str(FIXTURE_JSON), "--warehouse", str(make_warehouse(tmp_path)),
                                "What values can Status__c take?"], lambda cfg: LLM(cfg, client=MockClient()))
    assert code == 0
    assert "records off: OKEVAL_BASE_URL is not on this machine; record data must stay local" in out
    assert "Based on the object knowledge" in out and "-- records: off" in out
    assert "------ RECORDS" not in out


def test_missing_snapshot_turns_records_off(monkeypatch, tmp_path):
    monkeypatch.setenv("OKEVAL_BASE_URL", "http://localhost:8000/v1")
    ask_cli = _load_ask()
    missing = tmp_path / "nope" / "warehouse.duckdb"
    code, out = _main(ask_cli, ["--knowledge", str(FIXTURE_JSON), "--warehouse", str(missing),
                                "What values can Status__c take?"], lambda cfg: LLM(cfg, client=MockClient()))
    assert code == 0
    assert f"records off: no warehouse snapshot at {missing}; run ./ask.sh --refresh-warehouse" in out
    assert "Based on the object knowledge" in out


def test_flag_conflicts_exit():
    ask_cli = _load_ask()
    with pytest.raises(SystemExit, match="either --no-records or --records-only"):
        ask_cli.main(["--no-records", "--records-only", "q"])
    with pytest.raises(SystemExit, match="YYYY-MM-DD"):
        ask_cli.main(["--today", "13/10/2026", "q"])


def test_no_records_imports_neither_record_modules_nor_duckdb(tmp_path):
    script = textwrap.dedent(f"""
        import contextlib, io, json, sys
        import sf_assistant.cli as mod
        import sf_assistant.pipeline
        from sf_assistant.agent.llm import LLM
        from sf_assistant.agent.mock_llm import MockClient
        sf_assistant.pipeline.LLM = lambda cfg: LLM(cfg, client=MockClient())
        RECORD = ("okrec", "okeval", "sf_assistant.engine", "sf_assistant.tools.record_tools", "sf_assistant.tools.open_records",
                  "sf_assistant.tools.record_base", "sf_assistant.tools.discovery",
                  "sf_assistant.tools.records", "sf_assistant.tools.people", "sf_assistant.tools.names",
                  "sf_assistant.tools.lookups", "sf_assistant.tools.meta",
                  "sf_assistant.agent.record_session", "sf_assistant.agent.record_prompts", "sf_assistant.agent.lean",
                  "sf_assistant.agent.render", "sf_assistant.agent.checks", "sf_assistant.agent.results",
                  "sf_assistant.agent.nudges", "sf_assistant.agent.triage", "sf_assistant.agent.people_answer",
                  "sf_assistant.agent.merge")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(["--no-records", "--knowledge", {str(FIXTURE_JSON)!r}, "What values can Status__c take?"])
        print(json.dumps({{"code": code, "out": buf.getvalue(),
                           "okrec": sorted(m for m in sys.modules if m in RECORD or m.startswith(tuple(r + "." for r in RECORD))),
                           "duckdb": "duckdb" in sys.modules}}))
    """)
    env = dict(os.environ, OKEVAL_BASE_URL="http://localhost:8000/v1", OKEVAL_API_KEY="test-key")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["code"] == 0 and data["okrec"] == [] and data["duckdb"] is False
    out = data["out"]
    assert "records" not in out.lower().replace("record data", "")  # the old knowledge-only output
    assert "\n-- 2 tool call(s), 3 model call(s)" in out


def test_file_mode_writes_a_jsonl_sidecar_without_list_rows(monkeypatch, tmp_path):
    pytest.importorskip("duckdb")
    import sf_assistant.agent.llm
    from warehouse_fixture import make_knowledge, make_warehouse

    monkeypatch.setenv("OKEVAL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SFA_MERGE", "model")   # the merge call's reply without a Source line (below)
    ask_cli = _load_ask()
    monkeypatch.setattr(ask_cli, "KNOWLEDGE_DIR", str(make_knowledge(tmp_path)))
    list_args = {"object": "Interview__c", "fields": ["Name", "Interview_Status__c"], "limit": 3}
    llm = FakeLLM([{"name": "list_records", "args": list_args},
                   "The 3 newest client interviews are listed (10 match in total).\n"
                   "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."],
                  merge_answer="3 of 10 client interviews are shown.")
    monkeypatch.setattr(sf_assistant.agent.llm, "LLM", lambda cfg: llm)  # the record session's own client
    qfile = tmp_path / "q.txt"
    qfile.write_text("# records\nList the 3 newest client interviews.\n", encoding="utf-8")
    out_txt = tmp_path / "runs" / "t.txt"
    code, out = _main(ask_cli, ["--knowledge", str(FIXTURE_JSON), "--warehouse", str(make_warehouse(tmp_path)),
                                "--today", "2026-10-13", "--file", str(qfile), "--out", str(out_txt)], lambda cfg: llm)
    assert code == 0, out
    sidecar = tmp_path / "runs" / "t.jsonl"
    assert sidecar.is_file() and out_txt.is_file()
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["n"] == 1 and rec["question"] == "List the 3 newest client interviews."
    # the merged text had no Source line: code adds it (the list query's object and its sync time)
    assert rec["answer_kind"] == "merged" and rec["answer"] == ("3 of 10 client interviews are shown.\n"
                                                                "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")
    assert rec["merge_by"] == "model" and "merge_switch" not in rec   # the merge that ran (2026-10-29)
    facts = [f for f in rec["record_facts"] if f["tool"] == "list_records"]
    assert facts and facts[0]["result"]["rows"] == {"rows_omitted": 3}
    assert facts[0]["result"]["total_count"] == 10
    q = rec["queries"][0]
    assert q["tool"] == "list_records" and q["ok"] and q["sql"][0].startswith("SELECT ") and "rows" not in q
    assert "I-00" not in lines[0]  # no record names in the sidecar
    # latency logging (2026-10-29): the row keeps the answer's timeline, and each query its seconds
    stages = {e["stage"] for e in rec["timeline"]["entries"] if e["kind"] == "stage"}
    assert stages >= {"spelling", "triage", "route", "records", "hints", "post", "merge", "assemble"}
    assert [(e["name"], e["ok"]) for e in rec["timeline"]["entries"]
            if e["kind"] == "tool" and e["stage"] == "records"] == [("list_records", True)]
    assert q["seconds"] >= q["sql_seconds"] >= 0
    summary = out.split("SUMMARY", 1)[1]
    assert "  1  merged     1 q, 3 rows" in summary
    assert "1 questions, 1 merged with records" in summary


def test_record_transcripts_stay_in_git_ignored_folders(monkeypatch, tmp_path):
    """With records on, --file output holds record data: inside the repository it goes only to .private/runs/ or
    .private/okeval_run/."""
    ask_cli = _load_ask()
    here = Path(ask_cli.HERE)
    monkeypatch.chdir(here)  # ask.sh runs from here
    path, note = ask_cli.transcript_path(None, "questions/records.txt", records=True)
    assert Path(path).parent == here / ".private" / "runs" and Path(path).name.startswith("ask_records_") and note is None
    assert ask_cli.transcript_path(".private/runs/my.txt", "q.txt", records=True) == (
        str(here / ".private" / "runs" / "my.txt"), None)
    assert ask_cli.transcript_path(str(here / ".private" / "okeval_run" / "a.txt"), "q.txt", records=True)[1] is None
    path, note = ask_cli.transcript_path("my.txt", "q.txt", records=True)
    assert path == str(here / ".private" / "runs" / "my.txt") and note.endswith("so it is written to .private/runs/my.txt")
    with pytest.raises(ValueError, match=r"inside the git repository but not in \.private/runs/"):
        ask_cli.transcript_path(str(here / "my.txt"), "q.txt", records=True)
    assert ask_cli.transcript_path("my.txt", "q.txt", records=False) == (str(here / "my.txt"), None)
    outside = tmp_path / "elsewhere.txt"  # outside the repository: the caller's choice
    assert ask_cli.transcript_path(str(outside), "q.txt", records=True) == (str(outside.resolve()), None)
    ignore = (here / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".private/", "*.jsonl", "ask_*.txt"} <= set(ignore)


def test_file_mode_refuses_an_unignored_absolute_out(monkeypatch, tmp_path):
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    monkeypatch.setenv("OKEVAL_BASE_URL", "http://localhost:8000/v1")
    ask_cli = _load_ask()
    monkeypatch.setattr(ask_cli, "KNOWLEDGE_DIR", str(make_knowledge(tmp_path)))
    qfile = tmp_path / "q.txt"
    qfile.write_text("How many?\n", encoding="utf-8")
    bad = Path(ask_cli.HERE) / "not_ignored_transcript.txt"
    llm = FakeLLM(["unused"])
    code, out = _main(ask_cli, ["--knowledge", str(FIXTURE_JSON), "--warehouse", str(make_warehouse(tmp_path)),
                                "--today", "2026-10-13", "--file", str(qfile), "--out", str(bad)], lambda cfg: llm)
    assert code == 2 and "inside the git repository but not in .private/runs/" in out
    assert not bad.exists() and not bad.with_suffix(".jsonl").exists() and llm.roles == []


def test_score_records(tmp_path):
    spec = importlib.util.spec_from_file_location("score_records_cli", sf_assistant.evaluation.score_records.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    golden = [{"n": 1, "question": "How many?", "tool": "count_records", "args": {}, "expect": [84], "tol": 0},
              {"n": 2, "question": "Share?", "tool": "percentage_of_records", "args": {}, "expect": [6.8], "tol": 0.3},
              {"n": 3, "question": "Per status?", "tool": "group_records", "args": {}, "expect": [25, 25, 5], "tol": 0},
              {"n": 4, "question": "Not asked?", "tool": "count_records", "args": {}, "expect": [1], "tol": 0}]
    (tmp_path / "g.json").write_text(json.dumps(golden), encoding="utf-8")
    runs = [{"n": 1, "question": "How many?", "answer": "There are 84.", "record_facts": [{"result": {"count": 84}}],
             "unsupported_numbers": []},
            {"n": 2, "question": "Share?", "answer": "About 6.9% (34 of 500).", "record_facts": [{"result": {"percent": 6.8}}],
             "unsupported_numbers": ["6.9"]},
            {"n": 3, "question": "Per status?", "answer": "Five sections have 25 each.", "record_facts": [],
             "unsupported_numbers": []}]
    (tmp_path / "r.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    rows, passed, unsupported = mod.score(str(tmp_path / "r.jsonl"), str(tmp_path / "g.json"))
    assert [r["ok"] for r in rows] == [True, True, False, False]
    assert rows[2]["missing"] == [5] and rows[2]["facts_ok"] is False and rows[3]["unsupported"] is None
    assert (passed, unsupported) == (2, 1)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main([str(tmp_path / "r.jsonl"), "--golden", str(tmp_path / "g.json")]) == 0
    assert "2/4 passed, 1 with unsupported numbers" in out.getvalue()


# ----------------------------------------------------------------------------- lookup disambiguation (§21.10)
II = "Internal_Interview__c"
PERSON_Q = "how many mock interviews has delta person taken"
LOOKUP_KNOWLEDGE = "Mock interviews an employee took are `Internal_Interview__c.Interviewer__c` (the interviewer)."
LOOKUP_SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."


def _stub_status():
    from sf_assistant.engine.types import RecordsStatus

    return RecordsStatus(
        snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
        copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
        origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
        time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
        today_pinned=True, objects=60, duckdb_version="1.5.6")


def _stub_person_evidence():
    """One count on Internal_Interview__c with Recruiter__c = 'Delta Person' -> 0, whose lookup check finds 3
    through Interviewer__c (role fits 'taken')."""
    from sf_assistant.engine.types import QueryEvidence, Statement

    check = {"filter": "Recruiter__c.Name = 'Delta Person'", "object": II, "field": "Recruiter__c",
             "target": "Recruiter__c", "target_label": "Employee", "person_kind": "employee", "role": "recruiter",
             "fits": None, "cues": ["taken"], "records": 0, "any_role": 3,
             "others": [{"field": "Interviewer__c", "target": "Recruiter__c", "group": "same_target",
                         "role": "interviewer", "fits": "taken", "records": 3}],
             "scope": "all other conditions of this query"}
    result = {"query_id": "q1", "object": II, "count": 0, "counted": f"{II} records",
              "applied": {"filters": ["Recruiter__c.Name = 'Delta Person'"]}, "notes": [], "lookup_check": [check],
              "source": f"preprod sandbox copy; {II} synced 2026-10-13 14:12 UTC"}
    st = Statement(sql=f'SELECT COUNT(*) AS n FROM main."{II}" AS t0', params=["Delta Person"], columns=["n"],
                   rows=[[0]], row_count=1, truncated=False, elapsed_ms=1.0)
    args = {"object": II, "filters": [{"field": "Recruiter__c", "op": "eq", "value": "Delta Person"}]}
    ev = QueryEvidence(query_id="q1", tool="count_records", kind="data", object=II, arguments=args, ok=True,
                       error=None, applied=result["applied"], notes=[], statements=[st], tables=[II],
                       synced_at={II: "2026-10-13 14:12 UTC"}, summary="0", model_result=result, elapsed_ms=2.0)
    ev.fields_used, ev.error_code = [(II, "Recruiter__c")], None
    return ev


class _StubSession:
    """The RecordSession surface ask.py uses, with a canned record answer."""

    def __init__(self, rec):
        self.rec, self.tools = rec, object()

    def ask(self, text):
        return self.rec

    def status(self):
        return _stub_status()


def _stub_rec(answer):
    from sf_assistant.agent.record_session import RecordAnswer

    return RecordAnswer(question=PERSON_Q, answer=answer, no_records=False, used_data=True,
                        evidence=[_stub_person_evidence()], agent=None, error=None, unsupported_numbers=[], seconds=1.0)


def test_field_check_line_footer_and_jsonl_keys():
    ask_cli = _load_ask()
    merged = "Delta Person has 0 mock interviews.\n" + LOOKUP_SOURCE
    llm = FakeLLM(["unused"], merge_answer=merged, knowledge_answer=LOOKUP_KNOWLEDGE)
    out, row = _run(ask_cli, PERSON_Q, llm, _StubSession(_stub_rec("Delta Person has taken 0 mock interviews.")))
    assert ("-- field check: the question's verb fits Internal_Interview__c.Interviewer__c; the record count used "
            "Recruiter__c (passed to the merge)") in out
    footer = (f"Lookup used: `{II}.Recruiter__c` (recruiter). Same person through other lookups of {II} "
              "(overlapping, not to be added): `Interviewer__c` (interviewer) 3.")
    assert f"Delta Person has 0 mock interviews.\n{footer}\n{LOOKUP_SOURCE}" in out  # before the Source line
    j = row["jsonl"]
    assert j["answer_kind"] == "merged" and j["answer_body"] == merged and j["lookup_footer"] is True
    assert j["answer"] == f"Delta Person has 0 mock interviews.\n{footer}\n{LOOKUP_SOURCE}"
    assert [c["field"] for c in j["lookup_checks"]] == ["Recruiter__c"] and j["lookup_checks"][0]["query_id"] == "q1"
    assert len(j["field_check"]) == 1 and j["field_check"][0].startswith(
        "The question's verb fits Internal_Interview__c.Interviewer__c (interviewer)")


def test_a_merge_that_adds_a_condition_falls_back_to_the_record_answer():
    ask_cli = _load_ask()
    merged = ("Delta Person has 0 mock interviews, counted on records where `Candidate_Training__c` is populated.\n"
              + LOOKUP_SOURCE)
    rec_answer = "Delta Person has taken 0 mock interviews."
    llm = FakeLLM(["unused"], merge_answer=merged, knowledge_answer=LOOKUP_KNOWLEDGE)
    out, row = _run(ask_cli, PERSON_Q, llm, _StubSession(_stub_rec(rec_answer)))
    assert ("-- could not merge (the merged answer states a condition on Candidate_Training__c that no query "
            "applied); the record agent's answer is shown") in out
    j = row["jsonl"]
    assert j["answer_kind"] == "records" and j["answer_body"] == rec_answer
    assert j["merge_error"] == "the merged answer states a condition on Candidate_Training__c that no query applied"
    assert j["record_nudges"] == []
    assert "Candidate_Training__c" not in j["answer"]


def test_records_only_answer_naming_the_lookup_gets_no_footer():
    ask_cli = _load_ask()
    answer = "0 mock interviews as recruiter (`Recruiter__c`); 3 as interviewer (`Interviewer__c`).\n" + LOOKUP_SOURCE
    llm = FakeLLM(["unused"])
    out, row = _run(ask_cli, PERSON_Q, llm, _StubSession(_stub_rec(answer)), records_only=True)
    assert "Lookup used" not in out and "-- field check" not in out
    j = row["jsonl"]
    assert j["answer"] == j["answer_body"] == answer and j["lookup_footer"] is False and j["field_check"] == []
    assert j["answer_kind"] == "records" and len(j["lookup_checks"]) == 1


def test_jsonl_lookup_keys_without_records():
    ask_cli = _load_ask()
    llm = FakeLLM(["unused"])
    out, row = _run(ask_cli, "What values can Status__c take?", llm, None)
    j = row["jsonl"]
    assert j["answer_body"] == j["answer"] and j["lookup_checks"] == [] and j["field_check"] == []
    assert j["lookup_footer"] is False
    # records off (--no-records): the knowledge agent as before, without the records note and without a route call
    assert llm.roles == ["knowledge", "knowledge"] and j["knowledge_mode"] == "full"
    assert llm.knowledge_calls[0]["system"] == prompts.AGENT_SYSTEM and llm.knowledge_calls[0]["max_tokens"] is None


class _InOrderLLM(FakeLLM):
    """FakeLLM whose record agent replies record_script[k] on its k-th call (so a nudge gets a new reply)."""

    def __init__(self, record_script, **kw):
        super().__init__(record_script, **kw)
        self.record_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, **kw):
        system = str(messages[0].get("content") or "")
        if not system.startswith("You answer questions about Salesforce RECORDS"):
            return super().chat(messages, tools=tools, **kw)
        self.roles.append("record")
        self.record_messages.append([dict(m) for m in messages])
        step = self.record_script[min(len(self.record_messages) - 1, len(self.record_script) - 1)]
        usage = {"prompt_tokens": 5, "completion_tokens": 1}
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"r{len(self.record_messages)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": usage}
        return {"content": step, "tool_calls": [], "usage": usage}


def test_integration_other_lookup_nudge_requeries_the_interviewer(fixture_tools):
    """Needs both builds (RECORDS_SPEC §21.12): the fixture's Internal_Interview__c lookups and lookup_check."""
    from sf_assistant.agent.record_session import RecordSession

    spec = fixture_tools.catalog.objects.get(II)
    if spec is None or "Interviewer__c" not in getattr(spec, "fields", {}):
        pytest.skip("the tools-side fixture (Internal_Interview__c.Interviewer__c) is not built yet")
    rec_args = {"object": II, "filters": [{"field": "Recruiter__c", "op": "eq", "value": "Delta Person"}]}
    int_args = {"object": II, "filters": [{"field": "Interviewer__c", "op": "eq", "value": "Delta Person"}]}
    final = ("Delta Person took 3 mock interviews as interviewer (`Interviewer__c`); 0 as recruiter "
             "(`Recruiter__c`).\nSource: preprod sandbox copy, synced 2026-10-13 14:30 UTC.")
    llm = _InOrderLLM([{"name": "count_records", "args": rec_args}, "Delta Person has taken 0 mock interviews.",
                       {"name": "count_records", "args": int_args}, final])
    session = RecordSession(fixture_tools, _cfg(), llm=llm)
    first = fixture_tools.call("count_records", rec_args)
    if "lookup_check" not in (first.get("result") or {}):
        pytest.skip("the tools-side lookup_check is not built yet")
    out, row = _run(_load_ask(), "how many mock interviews has delta person taken", llm, session, records_only=True)
    sent = [m["content"] for m in llm.record_messages[-1] if m.get("role") == "user"]
    assert any(s.startswith("Your count used Recruiter__c (recruiter) and found 0") for s in sent), sent
    assert "Interviewer__c (interviewer) 3" in next(s for s in sent if s.startswith("Your count used"))
    head = out.split("\n-- answer from the record agent only", 1)[0]
    assert "`Interviewer__c`" in head and "3 mock interviews" in head
    j = row["jsonl"]
    counts = [f["result"].get("count") for f in j["record_facts"] if f["tool"] == "count_records"]
    assert counts == [0, 3]
    assert [c["field"] for c in j["lookup_checks"]][:2] == ["Recruiter__c", "Interviewer__c"]
    assert j["lookup_footer"] is False  # the answer names the lookup it used


def _score_cli():
    spec = importlib.util.spec_from_file_location("score_records_cli", sf_assistant.evaluation.score_records.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_score_records_scores_answer_body(tmp_path):
    mod = _score_cli()
    golden = [{"n": 1, "question": "How many?", "tool": "count_records", "args": {}, "expect": [84], "tol": 0}]
    (tmp_path / "g.json").write_text(json.dumps(golden), encoding="utf-8")
    run = {"question": "How many?", "answer": "There are 0.\nLookup used: `X__c` 84.", "answer_body": "There are 0.",
           "record_facts": [{"result": {"count": 84}}], "unsupported_numbers": []}
    (tmp_path / "r.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    rows, passed, _ = mod.score(str(tmp_path / "r.jsonl"), str(tmp_path / "g.json"))
    assert passed == 0 and rows[0]["missing"] == [84]  # the footer's 84 does not make the answer pass
    run.pop("answer_body")
    (tmp_path / "r.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    assert mod.score(str(tmp_path / "r.jsonl"), str(tmp_path / "g.json"))[1] == 1  # older sidecars: answer


def test_score_records_lookup_mode(tmp_path):
    mod = _score_cli()
    expected = [
        {"n": 1, "q": "how many mock interviews has delta person taken", "object": II, "field": "Interviewer__c",
         "expected": {"main_by_type": {"Mock": 2, "Intake": 1}, "other": 9}, "score": [3, 2], "accept": "3; 2 done",
         "person": None},
        {"n": 2, "q": "mock interviews delta person supported", "object": II,
         "field": "Interview_Support_Person__c (legacy)", "expected": {"main_count": 4, "x": [1, 2]}, "accept": ""},
        {"n": 3, "q": "evaluations by nobody here", "object": "Interview_Evaluation__c",
         "field": "Evaluator__c (not synced)", "expected": {}, "expect_kind": "not_found", "accept": ""},
        {"n": 4, "q": "where does delta person appear", "object": "(all)", "field": "every lookup", "expected": {},
         "accept": ""},
        {"n": 5, "q": "not asked", "object": II, "field": "Recruiter__c", "expected": {"main_count": 1}, "accept": ""}]
    (tmp_path / "e.json").write_text(json.dumps(expected), encoding="utf-8")
    src = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
    runs = [
        {"question": expected[0]["q"], "answer_body": f"3 as interviewer (`Interviewer__c`), 2 Completed.\n{src}",
         "answer": "ignored 7", "unsupported_numbers": []},
        {"question": expected[0]["q"], "answer": f"Delta Person has taken 0 mock interviews.\n{src}",
         "unsupported_numbers": ["7"]},  # asked twice: each run line is scored
        {"question": expected[0]["q"], "answer": f"0 as recruiter (`Recruiter__c`); 3 and 2 elsewhere.\n{src}",
         "unsupported_numbers": []},
        {"question": expected[1]["q"], "answer": "4 mock interviews as support person.", "unsupported_numbers": []},
        {"question": expected[2]["q"], "answer": f"`Evaluator__c` is not synced to the warehouse, so it is not "
                                                 f"recorded.\n{src}", "unsupported_numbers": []},
        {"question": expected[2]["q"], "answer": "`Evaluator__c`: 0 evaluations.", "unsupported_numbers": []},
        {"question": expected[3]["q"], "answer": "Delta Person appears in 3 lookups.", "unsupported_numbers": []},
        {"question": "a question that is not in the expected file", "answer": "1", "unsupported_numbers": []}]
    (tmp_path / "r.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    rows, totals = mod.score_lookup(str(tmp_path / "r.jsonl"), str(tmp_path / "e.json"))
    got = [(r["n"], r["auto"], r["nums"], r["field"], r["none"], r["not_found"]) for r in rows]
    assert got == [(1, "correct", True, True, False, "-"),
                   (1, "wrong", False, False, True, "-"),        # a bare 0 without a lookup is a none-claim
                   (1, "partial", True, False, False, "-"),      # a scoped zero is not a none-claim
                   (2, "partial", True, False, False, "-"),      # main values only (lists skipped)
                   (3, "correct", "-", True, False, True),
                   (3, "partial", "-", True, False, False),      # not_found answers carry no count
                   (4, "correct", "-", "-", False, "-")]
    assert rows[1]["unsupported"] == 1 and rows[1]["missing_numbers"] == ["3", "2"]
    assert totals == {"correct": 3, "partial": 3, "wrong": 1, "scored": 7, "not_in_run": 1, "with_unsupported": 1}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(["--lookup", str(tmp_path / "e.json"), str(tmp_path / "r.jsonl"), "-v"]) == 0
    text = out.getvalue()
    assert "7 answers scored: 3 correct, 3 partial, 1 wrong" in text and "1 expected questions not in this run" in text
    assert "accept  : 3; 2 done" in text and "answer  : 3 as interviewer" in text


def test_score_records_lookup_helpers():
    mod = _score_cli()
    assert mod.first_api_name("Interview__c.Recruiter__c (client) / Internal_Interview__c.Interviewer__c") == \
        "Recruiter__c"
    assert mod.first_api_name("CreatedById (User)") == "CreatedById"
    assert mod.first_api_name("every lookup holding the Ids") is None
    assert mod.expected_numbers({"expected": {"main_a": {"x": 4, "y": 1.5}, "main_b": 2, "main_c": [1], "z": 9}}) == \
        [mod.Decimal("4"), mod.Decimal("1.5"), mod.Decimal("2")]
    assert mod.expected_numbers({"expected": {"z": 9}}) is None
    assert mod.expected_numbers({"score": [7, 4], "expected": {"main": 1}}) == [mod.Decimal("7"), mod.Decimal("4")]
    for text in ("X has 0 interviews.", "None are associated with X.", "X did not take any."):
        assert mod.none_claim(text), text
    for text in ("0 as recruiter (`Recruiter__c`).", "Synced 2026-10-13 14:00 UTC.", "There are 10.", "0.5 on average.",
                 "Source: none 0"):
        assert not mod.none_claim(text), text
    # an object name does not scope a claim; a breakdown value of 0 is no claim (review fixes 2026-10-14)
    objs = {"Internal_Interview__c", "Recruiter__c", "Interview__c"}
    for text in ("None of the `Internal_Interview__c` records are associated with P1.",
                 "P1 has taken 0 interviews in `Internal_Interview__c`; none are associated with P1.",
                 "Delta Person (Employee, `Recruiter__c`) has 0 interviews.", "Total: 0."):
        assert mod.none_claim(text, objects=objs), text
    for text in ("By status: Completed 4, Cancelled 0.", "*   **August 2026:** 0", "- **Cancelled**: 0 (month still running)",
                 "0 as recruiter (`Recruiter__c`).",
                 "0 internal interviews as interviewer (`Internal_Interview__c.Interviewer__c`).",
                 "`Evaluator__c`: 0 evaluations."):
        assert not mod.none_claim(text, objects=objs), text
    assert not mod.none_claim("Delta Person (Employee, `Recruiter__c`) has 0 interviews.", {"Recruiter__c"}, objs)
    run = {"lookup_checks": [{"field": "Recruiter__c", "others": [{"field": "Interviewer__c"}],
                              "elsewhere": [{"object": "Session__c", "field": "Host_User__c"}]}],
           "record_facts": [{"result": {"applied": {"filters": ["Program_Version__c.Program__c.Name = 'Alpha'"]}}}]}
    assert mod.run_fields(run) == {"Recruiter__c", "Interviewer__c", "Host_User__c", "Program_Version__c"}
    assert "Internal_Interview__c" in mod.object_names() or not any(KNOWLEDGE_DIR.glob("*.json"))
    assert mod.not_found_answer("No employee is named 'Nobody Here'.\nSource: copy, synced 2026-10-13 14:56 UTC.", "q")
    assert not mod.not_found_answer("No employee is named 'Nobody Here'; 0 interviews.", "q")
    assert not mod.not_found_answer("Nobody Here has interviews.", "q")


def test_score_records_lookup_round1_fixes(tmp_path):
    """Eval round 1 (2026-10-14): the Source line's date gives no numbers; list-item, per-record and value-scoped zeros
    are no none-claims; the code footer's "Lookup used" clause counts for the field check (never for numbers)."""
    mod = _score_cli()
    src = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
    assert mod.without_source(f"10 sessions.\n{src}") == "10 sessions."
    for text in ('**0** internal interviews have a "Completed" status for Delta Person.',
                 "There are 2 Employee records named Recruiter B: 0 interviews for #1 and 1 for #2.",
                 "- **August 2026**: 0 interviews", "*   **Sessions attended (Present):** 0",
                 "- **All other months (Jan-May, Jul, Oct)**: 0 sessions"):
        assert not mod.none_claim(text, objects=set()), text
    for text in ("Delta Person has taken 0 mock interviews.", "Total: 0.", "Delta Person did not host any sessions."):
        assert mod.none_claim(text, objects=set()), text
    expected = [{"n": 1, "q": "how many training sessions did delta person host", "object": "Session__c",
                 "field": "Host_User__c", "expected": {}, "score": [13], "accept": ""}]
    (tmp_path / "e.json").write_text(json.dumps(expected), encoding="utf-8")
    body = f"Delta Person hosted 15 training sessions.\n{src}"
    runs = [{"question": expected[0]["q"], "answer_body": body, "unsupported_numbers": [],
             "answer": f"Delta Person hosted 15 training sessions.\nLookup used: `Session__c.Host_User__c` (host).\n{src}"},
            {"question": expected[0]["q"], "answer_body": f"Delta Person hosted 13 training sessions.\n{src}",
             "answer": f"Delta Person hosted 13 training sessions.\n{src}", "unsupported_numbers": []}]
    (tmp_path / "r.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    rows, _totals = mod.score_lookup(str(tmp_path / "r.jsonl"), str(tmp_path / "e.json"))
    # 13 is only in the Source line's time: no match; the footer names the lookup used
    assert [(r["nums"], r["field"], r["auto"]) for r in rows] == [(False, True, "partial"), (True, False, "partial")]
    assert mod.footer_lookups({"answer": "x\nLookup used: `A__c.B__c` (b). Same person: `C__c` 1.", "answer_body": "x"}) \
        == "`A__c.B__c` (b)"
    assert mod.footer_lookups({"answer": "x", "answer_body": "x"}) == "" and mod.footer_lookups({"answer": "x"}) == ""


def test_integration_a_candidate_record_named_like_an_employee_gets_the_employee_nudge(fixture_tools):
    """Eval round 1, Q2 shape (both builds): the agent counts the same-name candidate record with 'Name (#k)'; the lookup
    check still finds the Employee with the same name, and the session sends the same-name-employee nudge."""
    from sf_assistant.agent.record_session import RecordSession

    cand_args = {"object": II, "filters": [{"field": "Candidate__c", "op": "eq", "value": "Delta Person (#1)"}]}
    int_args = {"object": II, "filters": [{"field": "Interviewer__c", "op": "eq", "value": "Delta Person"}]}
    final = ("Delta Person took 3 mock interviews as interviewer (`Interviewer__c`); a different Account record with the "
             "same name was the candidate (`Candidate__c`) on 2.\nSource: preprod sandbox copy, synced 2026-10-13 "
             "14:30 UTC.")
    llm = _InOrderLLM([{"name": "count_records", "args": cand_args}, "Delta Person took 2 mock interviews.",
                       {"name": "count_records", "args": int_args}, final])
    session = RecordSession(fixture_tools, _cfg(), llm=llm)
    out, row = _run(_load_ask(), "how many mock interviews has delta person taken", llm, session, records_only=True)
    sent = [m["content"] for m in llm.record_messages[-1] if m.get("role") == "user"]
    nudge = next(s for s in sent if s.startswith("Your count used Candidate__c (candidate) on a candidate record"))
    assert "Interviewer__c (interviewer) 3" in nudge and "'taken'" in nudge
    counts = [f["result"].get("count") for f in row["jsonl"]["record_facts"] if f["tool"] == "count_records"]
    assert counts == [2, 3]


def test_unsynced_field_check_status_line(fixture_tools):
    ask_cli = _load_ask()
    ev = _stub_person_evidence()
    ev.model_result = dict(ev.model_result, unsynced_fits=[{
        "field": "Final_Decision_By__c", "role": "final decision maker", "target": "Recruiter__c",
        "target_label": "Employee", "fits": "decisions", "proxy": [f"{II}.Interviewer__c"]}])
    from sf_assistant.agent.record_session import RecordAnswer
    rec = RecordAnswer(question=PERSON_Q, answer="Delta Person made 0 decisions.", no_records=False, used_data=True,
                       evidence=[ev], agent=None, error=None, unsupported_numbers=[], seconds=1.0)
    llm = FakeLLM(["unused"], merge_answer="Delta Person made 0 decisions.\n" + LOOKUP_SOURCE,
                  knowledge_answer=LOOKUP_KNOWLEDGE)
    out, row = _run(ask_cli, "how many decisions has delta person made", llm, _StubSession(rec))
    assert (f"-- field check: the question's word fits {II}.Final_Decision_By__c, which is not in the warehouse; the "
            f"record count used {II}.Interviewer__c (a proxy; passed to the merge)") in out
    assert "Not in the warehouse: `Internal_Interview__c.Final_Decision_By__c`" in row["jsonl"]["answer"]
    assert any("not synced to the warehouse" in line for line in row["jsonl"]["field_check"])


def test_a_dropped_record_reply_after_a_name_not_found_prints_the_not_found_answer(fixture_tools):
    """Follow-up round 1, merged Q36: the record agent listed employees after the E4h (that no longer silences the
    not-found check), its reply kept a 0 and was dropped; ask.py printed the raw knowledge answer, which never says
    that the person is not in the records. Now code's not-found answer is shown and the dropped reply is logged."""
    from sf_assistant.agent.record_session import RecordSession

    src = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
    second = "Result: 0 internal interviews.\n" + src
    llm = _InOrderLLM([{"name": "count_records", "args": {"object": II, "filters": [
                           {"field": "Interviewer__c", "op": "eq", "value": "Zulu Nobody"}]}},
                       {"name": "list_records", "args": {"object": "Recruiter__c", "fields": ["Name"]}},
                       "Zulu Nobody conducted 0 internal interviews.\n" + src, second],
                      knowledge_answer="The object knowledge does not contain record data; you would need to query "
                                       f"`{II}`.")
    out, row = _run(_load_ask(), "how many internal interviews did zulu nobody conduct", llm,
                    RecordSession(fixture_tools, _cfg(), llm=llm))
    head = out.split("=" * 72 + "\n", 2)[2].split("\n-- records:", 1)[0]
    assert head.startswith(f"No Employee (Recruiter__c) record is named 'Zulu Nobody' in the records (checked through "
                           f"`{II}.Interviewer__c`). There is no count for this name.\nSource: ")
    assert "you would need to query" not in head and "merge" not in llm.roles
    assert ("-- records: the name was not found (no successful record query); the record agent's reply gave a count and "
            "was dropped, so the answer is built from the not-found result") in out
    assert "   record reply dropped (no successful record query): Result: 0 internal interviews." in out
    sent = [m["content"] for m in llm.record_messages[-1] if m.get("role") == "user"]
    assert any(s.startswith("No record query succeeded") for s in sent), sent   # the list no longer hides it
    assert row["answer"] == "records" and row["jsonl"]["record_dropped_answer"] == second
    assert row["jsonl"]["answer_kind"] == "records" and "Zulu Nobody" in row["jsonl"]["answer"]


def test_a_corrected_name_after_a_not_found_error_keeps_the_record_reply(fixture_tools):
    """Review of the follow-up round 1 fixes: after an E4h for a misspelt name, a list_records call whose name filter
    matches the stored spelling (the base Name, or a lookup) is data about that record, even without a lookup_check:
    the reply is kept and no not-found message is sent. An unfiltered list still counts nothing (merged Q36)."""
    from sf_assistant.agent.nudges import _no_data_but_not_found
    from sf_assistant.agent.record_session import RecordSession

    src = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
    reply = ("No employee is named 'Recruiter Cee'; the closest stored name is Recruiter C, created 2025-01-01, an "
             "Active Employee.\n" + src)
    for corrected in ({"object": "Recruiter__c", "filters": [{"field": "Name", "op": "eq", "value": "Recruiter C"}]},
                      {"object": II, "filters": [{"field": "Interviewer__c", "op": "eq", "value": "Delta Person"}]}):
        llm = _InOrderLLM([{"name": "list_records", "args": {"object": "Recruiter__c", "filters": [
                               {"field": "Name", "op": "eq", "value": "Recruiter Cee"}]}},
                           {"name": "list_records", "args": corrected}, reply], knowledge_answer="")
        rec = RecordSession(fixture_tools, _cfg(), llm=llm).ask("when was recruiter cee created")
        ev = rec.evidence
        assert [e.error_code for e in ev] == ["E4h", None] and ev[1].names_matched, corrected
        assert not _no_data_but_not_found(ev)
        assert rec.answer.startswith(reply.split("\n")[0]) and not rec.no_records and not rec.dropped_answer
        assert not any(n.startswith("No record query succeeded") for n in rec.nudges)
    # an unfiltered list after the E4h is no data about the name
    fixture_tools.begin_question("when was recruiter cee created")
    fixture_tools.call("list_records", {"object": "Recruiter__c", "filters": [
        {"field": "Name", "op": "eq", "value": "Recruiter Cee"}]})
    fixture_tools.call("list_records", {"object": "Recruiter__c"})
    assert _no_data_but_not_found(fixture_tools.evidence)


def test_a_value_list_that_holds_the_corrected_name_counts_after_a_not_found_error(fixture_tools):
    """A misspelt department (E4h, "Close spellings: Dept One"), then get_field_values of the lookup: the value list
    holds the corrected name, so a reply giving its count is kept; a value list without it counts nothing."""
    from sf_assistant.agent.nudges import _no_data_but_not_found

    t = fixture_tools
    t.begin_question("how many employees are in dept onee")
    out = t.call("count_records", {"object": "Recruiter__c", "filters": [{"field": "Department__c", "op": "eq",
                                                                          "value": "Dept Onee"}]})
    assert not out["ok"] and "Close spellings: Dept One" in out["error"]
    assert "Dept One" in t.evidence[-1].not_found[0]["close"]
    t.call("get_field_values", {"object": "Recruiter__c", "field": "Department__c"})
    assert not _no_data_but_not_found(t.evidence)
    t.begin_question("how many employees are in dept zzz")
    out = t.call("count_records", {"object": "Recruiter__c", "filters": [{"field": "Department__c", "op": "eq",
                                                                          "value": "Qqq Zzz"}]})
    assert not out["ok"] and t.evidence[-1].not_found[0]["close"] == []
    t.call("get_field_values", {"object": "Recruiter__c", "field": "Department__c"})
    assert _no_data_but_not_found(t.evidence)


def test_a_no_records_reply_after_a_name_not_found_prints_the_not_found_answer(fixture_tools):
    """Review of the follow-up round 1 fixes: a NO_RECORDS reply after an E4h left no not-found answer, so merged mode
    printed the raw knowledge answer (the Q36 symptom). Code's not-found answer is printed instead."""
    from sf_assistant.agent.record_session import RecordSession

    llm = _InOrderLLM([{"name": "count_records", "args": {"object": II, "filters": [
                           {"field": "Interviewer__c", "op": "eq", "value": "Zulu Nobody"}]}}, "NO_RECORDS"],
                      knowledge_answer="The object knowledge does not contain record data; you would need to query "
                                       f"`{II}`.")
    out, row = _run(_load_ask(), "how many internal interviews did zulu nobody conduct", llm,
                    RecordSession(fixture_tools, _cfg(), llm=llm))
    head = out.split("=" * 72 + "\n", 2)[2].split("\n-- records:", 1)[0]
    assert head.startswith("No Employee (Recruiter__c) record is named 'Zulu Nobody' in the records")
    assert "you would need to query" not in head and "merge" not in llm.roles
    assert ("-- records: the name was not found (NO_RECORDS reply after a name was not found); the record agent replied "
            "NO_RECORDS, so the answer is built from the not-found result") in out
    assert row["jsonl"]["record_dropped_answer"] is None


# ------------------------------------------------------------- questions about named people (RECORDS_SPEC §22.11)

@pytest.fixture()
def people_tools(tmp_path):
    pytest.importorskip("duckdb")
    from warehouse_fixture import open_people_fixture

    tools = open_people_fixture(tmp_path)
    yield tools
    tools.close()


@pytest.mark.parametrize("merge_by", ["code", "model"])
def test_person_question_code_answer_merged(people_tools, monkeypatch, merge_by):
    """Code writes the record part from about_people. The merge call (SFA_MERGE=model) merges it with the knowledge
    answer. The merge by code (the default since 2026-10-29) adds no bullet that only names an object to a people
    answer (review of 2026-10-29: the person tool's objects make no bullet relevant), so code's answer is shown."""
    from sf_assistant.agent.record_session import RecordSession

    monkeypatch.setenv("SFA_MERGE", merge_by)
    ask_cli = _load_ask()
    llm = FakeLLM(["NO_RECORDS"], merge_answer="Lima Nobel is an employee (Active Employee) with a linked active user; "
                                               "no business record references them.\nSource: preprod sandbox copy, "
                                               "synced 2026-10-13 14:00 UTC.", route_answer="NO_RECORDS",
                  knowledge_answer="- `Recruiter__c` holds the employees.")   # the lean step's bullet
    llm.names_answer = "Lima Nobel"
    out, row = _run(ask_cli, "who is lima nobel", llm, RecordSession(people_tools, _cfg(), llm=llm))
    assert "names" in llm.roles and "lean" in llm.roles   # routed to the records: lean
    line = ("-- records: the record agent gave no usable answer (NO_RECORDS reply); code wrote the record part from "
            "about_people")
    if merge_by == "model":
        assert row["answer"] == "merged" and (line + "; merged with the knowledge answer") in out
    else:
        assert row["answer"] == "records" and (line + "\n") in out and llm.roles.count("merge") == 0
        assert row["jsonl"]["answer"].startswith(row["jsonl"]["record_answer"].split("\n")[0])
    assert row["jsonl"]["merge_by"] == merge_by
    assert row["jsonl"]["record_code_answer"] is True
    assert row["jsonl"]["triage"]["people"] == ["lima nobel"] and row["jsonl"]["triage"]["route"] is False
    assert "code wrote the record answer from about_people" in out


@pytest.mark.parametrize("merge_by", ["code", "model"])
def test_the_records_mode_people_answer_holds_no_reference_code(people_tools, monkeypatch, merge_by):
    """Step-2 fixes of 2026-10-30 (F1, HP03): the page's records mode (records_only: no knowledge step, no merge;
    choose_answer shows the record answer as it is) shows the code-written people answer without the internal P/R
    codes, in both merge modes."""
    from sf_assistant import pipeline
    from sf_assistant.agent.record_session import RecordSession

    monkeypatch.setenv("SFA_MERGE", merge_by)
    llm = FakeLLM(["NO_RECORDS"], route_answer="NO_RECORDS")
    llm.names_answer = "Lima Nobel"
    o = pipeline.answer("who is lima nobel", _cfg(), _registry(), None, llm, 1,
                        RecordSession(people_tools, _cfg(), llm=llm), records_only=True)
    assert o.rec.code_answer and o.m is None and o.kind == "records" and "Lima Nobel" in o.final
    assert "(P1)" not in o.final and "(R1)" not in o.final and "(P1)" not in o.rec.answer


def test_failed_people_read_prefix_line(people_tools, monkeypatch):
    from sf_assistant.agent.record_session import RecordSession
    real = people_tools.call

    def call(name, arguments=None):
        if name in ("about_people", "find_references"):
            return {"ok": False, "result": None, "error": "internal error in the record tools", "chunk_ids": []}
        return real(name, arguments)

    monkeypatch.setattr(people_tools, "call", call)
    ask_cli = _load_ask()
    llm = FakeLLM(["NO_RECORDS"], knowledge_answer="Employees are `Recruiter__c` records.", route_answer="RECORDS")
    llm.names_answer = "Lima Nobel"
    out, row = _run(ask_cli, "who is lima nobel", llm, RecordSession(people_tools, _cfg(), llm=llm))
    assert row["answer"] == "knowledge"
    assert "Record data about 'lima nobel' could not be read in this run.\n\nEmployees are `Recruiter__c` records." in out


def test_control_no_people_line(people_tools):
    from sf_assistant.agent.record_session import RecordSession
    ask_cli = _load_ask()
    llm = FakeLLM(["NO_RECORDS"], knowledge_answer="`Host_User__c` is the employee hosting the session.",
                  route_answer="NO_RECORDS")
    out, row = _run(ask_cli, "what does the host user field on a session mean?", llm,
                    RecordSession(people_tools, _cfg(), llm=llm))
    assert row["answer"] == "knowledge" and "record" not in llm.roles and "Record data about" not in out
    assert row["jsonl"]["triage"]["people"] == [] and row["jsonl"]["triage"]["missing"] == []
    # value spots (2026-10-28): the not-run line also names the stored record values that could overrule the route
    assert ("-- record agent: not run (the route call found no record data in the question, and no name or stored "
            "record value in it overrules that)") in out


def test_cli_knowledge_dir_is_also_the_record_catalog_default(monkeypatch, tmp_path):
    """One KNOWLEDGE_DIR, as in the notebook folder's ask.py: cli.open_record_session without knowledge_dir reads
    cli.KNOWLEDGE_DIR at call time (so pointing it elsewhere moves the record catalog too); an explicit folder wins."""
    assert sf_assistant.cli.KNOWLEDGE_DIR == sf_assistant.pipeline.KNOWLEDGE_DIR == str(KNOWLEDGE_DIR)
    seen = []

    def fake_open(cfg, rcfg, knowledge_dir=None):
        seen.append((cfg, rcfg, knowledge_dir))
        return None, ["records off: test"]

    monkeypatch.setattr(sf_assistant.pipeline, "open_record_session", fake_open)
    monkeypatch.setattr(sf_assistant.cli, "KNOWLEDGE_DIR", str(tmp_path))
    assert sf_assistant.cli.open_record_session("cfg", "rcfg") == (None, ["records off: test"])
    assert sf_assistant.cli.open_record_session("cfg", "rcfg", knowledge_dir="other") == (None, ["records off: test"])
    assert seen == [("cfg", "rcfg", str(tmp_path)), ("cfg", "rcfg", "other")]
