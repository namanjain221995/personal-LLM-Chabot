"""The lean knowledge step for record questions (RECORDS_SPEC §21.19): budget, tools, prompt (offline, scripted LLM)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.knowledge.search import KnowledgeIndex  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge  # noqa: E402
from sf_assistant.agent.question import Question  # noqa: E402
from sf_assistant.tools.knowledge_tools import ToolRegistry  # noqa: E402
from sf_assistant.config import load_config  # noqa: E402
from sf_assistant.agent import knowledge_prompts as kprompts  # noqa: E402
from sf_assistant.agent.knowledge_prompts import MULTI_AGENT_SYSTEM  # noqa: E402
from sf_assistant.tools.knowledge_tools import MultiToolRegistry  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent.lean import (LEAN_MAX_MODEL_CALLS, LEAN_MAX_TOKENS, LEAN_MAX_TOOL_CALLS, LEAN_SEARCH_K,  # noqa: E402
                        LeanLLM, LeanRegistry, is_budget_refusal, lean_facts, lean_knowledge_answer)
from sf_assistant.agent.record_session import clean_knowledge_answer  # noqa: E402

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
FACTS = ("- `Internal_Interview__c` holds internal mock interviews (not the client interviews of `Interview__c`).\n"
         "- `Interviewer__c` is the employee who conducts the interview.")


def _kn():
    return ObjectKnowledge.load(str(FIXTURE_JSON))


def _single():
    kn = _kn()
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


def _multi():
    return MultiToolRegistry([_kn()])


def _cfg():
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=8)


def _q(text="How many mock interviews has Delta Person taken?"):
    return Question(qid="ask-001", category="cross_section", question=text, expected_facts=[],
                    expected_tools=["search_knowledge"], answerable=True, source="ask", gold_chunk_ids=[])


class ToolHungryLLM:
    """Asks for ``per_reply`` tool calls on every call that offers tools; answers ``answer`` when offered none."""

    def __init__(self, calls_per_reply=1, answer=FACTS, name="search_all_knowledge", args=None):
        self.per_reply, self.answer, self.name = calls_per_reply, answer, name
        self.args = args or {"query": "interviewer mock", "k": 20}
        self.requests: list[dict] = []

    def chat(self, messages, tools=None, max_tokens=None, **_):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        usage = {"prompt_tokens": 10, "completion_tokens": 2}
        if tools:
            n = len(self.requests)
            return {"content": None, "usage": usage,
                    "tool_calls": [{"id": f"c{n}_{i}", "name": self.name, "arguments": dict(self.args)}
                                   for i in range(self.per_reply)]}
        return {"content": self.answer, "tool_calls": [], "usage": usage}


# ----------------------------------------------------------------------------- budget
def test_budget_caps_tool_calls_and_model_calls():
    llm = ToolHungryLLM(calls_per_reply=2)
    r = lean_knowledge_answer(_q(), llm, _multi(), _cfg())
    assert (LEAN_MAX_TOOL_CALLS, LEAN_MAX_MODEL_CALLS) == (3, 3)
    assert len(llm.requests) == 3 and r.steps == 3                      # 3 model calls, never more
    ran = [c for c in r.tool_calls if c["ok"]]
    refused = [c for c in r.tool_calls if not c["ok"]]
    assert len(ran) == 3 and len(refused) == 1                          # 2 + 2 asked; the 4th did not run
    assert all(is_budget_refusal(c) for c in refused) and not any(is_budget_refusal(c) for c in ran)
    assert refused[0]["error"] == rprompts.lean_budget_error(3)
    last = llm.requests[-1]
    assert last["tools"] is None                                       # the last call offers no tools
    assert last["messages"][-1] == {"role": "user", "content": rprompts.KNOWLEDGE_LEAN_ANSWER_MESSAGE}
    assert all(q["max_tokens"] == LEAN_MAX_TOKENS for q in llm.requests)
    assert r.answer == FACTS and r.error is None


def test_a_first_reply_without_tools_is_sent_back_once_within_the_budget():
    class AnswersFirst(ToolHungryLLM):
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            if len(self.requests) == 0:
                self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
                return {"content": "From memory: it is the interviewer.", "tool_calls": [], "usage": {}}
            return super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)

    llm = AnswersFirst()
    r = lean_knowledge_answer(_q(), llm, _multi(), _cfg())
    assert len(llm.requests) == 3 and r.answer == FACTS
    assert len(r.tool_calls) == 1 and r.tool_calls[0]["ok"]
    nudge = [m["content"] for m in llm.requests[1]["messages"] if m.get("role") == "user"][-1]
    assert nudge == rprompts.lean_require_tools_message(3) and "Call 1 to 3 tools now" in nudge


def test_a_tool_call_in_the_last_reply_is_dropped_and_tool_call_text_removed():
    class StillWantsTools(ToolHungryLLM):
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            resp = super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)
            if tools is None:   # the server may still parse a tool call out of the text
                resp = {"content": FACTS + '\n<tool_call>\n{"name": "get_field"}\n</tool_call>',
                        "tool_calls": [{"id": "x", "name": "get_field", "arguments": {}}], "usage": {}}
            return resp

    llm = StillWantsTools()
    r = lean_knowledge_answer(_q(), llm, _multi(), _cfg())
    assert len(llm.requests) == 3 and r.answer == FACTS and r.error is None


def test_an_empty_lean_reply_is_an_error():
    r = lean_knowledge_answer(_q(), ToolHungryLLM(answer=""), _multi(), _cfg())
    assert r.answer == "" and r.error == "empty final answer"
    r = lean_knowledge_answer(_q(), ToolHungryLLM(answer="<tool_call>{}</tool_call>"), _multi(), _cfg())
    assert r.answer == "" and r.error == "empty final answer"


def test_lean_llm_never_sends_a_call_past_its_budget():
    inner = ToolHungryLLM()
    llm = LeanLLM(inner, max_calls=2, max_tokens=100)
    tools = [{"type": "function", "function": {"name": "search_all_knowledge"}}]
    assert llm.chat([{"role": "user", "content": "q"}], tools=tools)["tool_calls"]
    last = llm.chat([{"role": "user", "content": "q"}], tools=tools, max_tokens=900)
    assert last["tool_calls"] == [] and last["content"] == FACTS
    assert inner.requests[-1]["tools"] is None and inner.requests[-1]["max_tokens"] == 100
    assert llm.chat([{"role": "user", "content": "q"}], tools=tools) == {"content": "", "tool_calls": [], "usage": {}}
    assert len(inner.requests) == 2 and llm.refused == 1
    assert llm.calls == 2 and LeanLLM(inner).max_tokens == LEAN_MAX_TOKENS


def test_lean_registry_offers_few_tools_and_clamps_search_size():
    multi = LeanRegistry(_multi())
    assert list(multi.tools) == ["search_all_knowledge", "search_knowledge", "search_fields", "get_field",
                                 "get_picklist_values"]
    assert sorted(t["function"]["name"] for t in multi.openai_tools()) == sorted(multi.tools)
    assert sorted(t["name"] for t in multi.list_tools()) == sorted(multi.tools)
    single = LeanRegistry(_single())
    assert list(single.tools) == ["search_knowledge", "search_fields", "get_field", "get_picklist_values"]
    # k above the lean size is cut to LEAN_SEARCH_K; a smaller k stays
    seen = []

    class Spy:
        tools = {"search_all_knowledge": object(), "get_field": object()}

        def openai_tools(self):
            return []

        def call(self, name, arguments=None):
            seen.append((name, dict(arguments or {})))
            return {"ok": True, "result": {}, "error": None, "chunk_ids": []}

    reg = LeanRegistry(Spy(), max_calls=3)
    reg.call("search_all_knowledge", {"query": "x", "k": 20})
    reg.call("search_all_knowledge", {"query": "x", "k": 2})
    reg.call("get_field", {"name": "Interviewer__c", "k": 20})
    assert seen == [("search_all_knowledge", {"query": "x", "k": LEAN_SEARCH_K}),
                    ("search_all_knowledge", {"query": "x", "k": 2}),
                    ("get_field", {"name": "Interviewer__c", "k": 20})]
    assert reg.call("get_field", {"name": "x"})["error"] == rprompts.lean_budget_error(3) and len(seen) == 3
    assert reg.call("list_objects", {})["error"].startswith("unknown tool 'list_objects'")

    class Other:   # a registry with none of the lean tools keeps all of its tools
        tools = {"a": object(), "b": object()}

    assert list(LeanRegistry(Other()).tools) == ["a", "b"]


def test_lean_tool_calls_run_against_the_real_registry():
    llm = ToolHungryLLM(name="search_all_knowledge", args={"query": "interviewer"})
    r = lean_knowledge_answer(_q(), llm, _multi(), _cfg())
    tool_msgs = [m for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2 and all('"ok": true' in m["content"] for m in tool_msgs)
    assert r.tool_calls[0]["arguments"] == {"query": "interviewer"} and r.tool_calls[0]["chunk_ids"]
    offered = [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert sorted(offered) == ["get_field", "get_picklist_values", "search_all_knowledge", "search_fields",
                               "search_knowledge"]


# ----------------------------------------------------------------------------- prompt
def test_lean_system_prompt_contents():
    text = rprompts.render_lean_system(LEAN_MAX_TOOL_CALLS)
    assert "<<" not in text and "At most 3 tool calls in all" in text
    assert text.startswith("You add FIELD MEANINGS")
    assert "never answer\nbefore you have a tool result" in text and "your first reply calls 1 to 3 tools at once" in text
    assert "(not a person's name)" in text and 'never "no caveat is documented"' in text
    for part in ("FACTS block", "2 to 5 short bullets", "`Interview__c` = client", "`Internal_Interview__c` = internal mock",
                 "documented role", "fits the question's verb", "if no lookup's role clearly fits, name none",
                 "legacy or superseded", "Never write record counts or record values", "SOQL or SQL",
                 "that the knowledge has no record data or that someone\nwould need to query",
                 "No heading, intro or\nsummary"):
        assert part in text, part
    # not the full agent's rules: no "needs record data" reply, no unlimited answer
    assert "needs record data" not in text and "not available from object knowledge" not in text
    assert text != MULTI_AGENT_SYSTEM and text != kprompts.AGENT_SYSTEM
    assert "did not run" in rprompts.lean_budget_error(3) and "(3 calls)" in rprompts.lean_budget_error(3)
    assert "Do not call any tool" in rprompts.KNOWLEDGE_LEAN_ANSWER_MESSAGE
    assert "often as a short block of facts" in rprompts.COMBINE_SYSTEM


def test_a_facts_block_passes_the_merge_cleaner_unchanged():
    assert clean_knowledge_answer(FACTS) == FACTS
    assert lean_facts("\n" + FACTS + "\n\n\n\n") == FACTS


# ----------------------------------------------------------------------------- backstops (after the timed runs)
LIST_TEXT = ('```json\n[{"tool": "search_all_knowledge", "args": {"query": "Interview__c offer ended"}},\n'
             ' {"tool": "search_fields", "args": {"object": "Internal_Interview__c", "query": "outcome"}}]\n```')


def _offered():
    return LeanRegistry(_multi()).openai_tools()


def test_text_tool_calls_reads_calls_written_as_text():
    from sf_assistant.agent.lean import text_tool_calls

    assert text_tool_calls(LIST_TEXT, _offered()) == [
        ("search_all_knowledge", {"query": "Interview__c offer ended"}),
        ("search_fields", {"object": "Internal_Interview__c", "query": "outcome"})]
    tags = ('<tool_call>\n{"name": "get_field", "arguments": {"object": "Internal_Interview__c", "name": "X"}}\n'
            '</tool_call>\n<tool_call>{"name": "search_all_knowledge", "arguments": "{\\"query\\": \\"q\\"}"}</tool_call>')
    assert text_tool_calls(tags, _offered()) == [("get_field", {"object": "Internal_Interview__c", "name": "X"}),
                                                 ("search_all_knowledge", {"query": "q"})]
    assert text_tool_calls('{"name": "get_field", "arguments": {"name": "X"}}', _offered()) == [
        ("get_field", {"name": "X"})]
    for text in (FACTS, '[{"tool": "list_objects", "args": {}}]', '[{"tool": "get_field"}, 3]', "", "[]x",
                 'Here: <tool_call>{"name": "get_field", "arguments": {}}</tool_call>'):
        assert text_tool_calls(text, _offered()) == [], text
    assert text_tool_calls(LIST_TEXT, None) == []


def test_a_reply_that_writes_its_tool_calls_as_text_runs_them():
    class WritesCalls(ToolHungryLLM):
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
            if tools and not any(m.get("role") == "tool" for m in messages):
                return {"content": LIST_TEXT, "tool_calls": [], "usage": {}}
            return {"content": FACTS, "tool_calls": [], "usage": {}}

    llm = WritesCalls()
    r = lean_knowledge_answer(_q("How many interviews ended with an offer?"), llm, _multi(), _cfg())
    assert [c["name"] for c in r.tool_calls] == ["search_all_knowledge", "search_fields"]
    assert all(c["ok"] for c in r.tool_calls) and len(llm.requests) == 2 and r.answer == FACTS


def test_a_reply_without_any_tool_result_is_not_used():
    class Memory(ToolHungryLLM):
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
            return {"content": "- `Assigned_Recruiter__c` is the support person.", "tool_calls": [], "usage": {}}

    from sf_assistant.agent.lean import NO_TOOL_RESULT_ERROR

    llm = Memory()
    r = lean_knowledge_answer(_q(), llm, _multi(), _cfg())
    assert len(llm.requests) == 2 and r.tool_calls == []          # answered, sent back once, answered again
    assert r.answer == "" and r.error == NO_TOOL_RESULT_ERROR
    # only refused or failed calls: no tool result either
    failing = ToolHungryLLM(name="get_field", args={"object": "Internal_Interview__c", "name": "No_Such__c"})
    r = lean_knowledge_answer(_q(), failing, _multi(), _cfg())
    assert r.tool_calls and not any(c["ok"] for c in r.tool_calls)
    assert r.answer == "" and r.error == NO_TOOL_RESULT_ERROR


def test_a_last_reply_that_only_asks_for_tools_is_dropped():
    r = lean_knowledge_answer(_q(), ToolHungryLLM(answer=LIST_TEXT), _multi(), _cfg())
    assert r.answer == "" and r.error == "empty final answer"


def test_lean_facts_drop_lines_about_what_the_knowledge_does_not_hold():
    text = (FACTS + "\n- The knowledge base does not contain specific record data or counts for individual trainers."
            "\n- No caveat is documented regarding the count of evaluations per person."
            "\n- The knowledge base references a snapshot of 400 sessions, but does not provide a breakdown of how many "
            "sessions this person hosted.")
    assert lean_facts(text) == FACTS
    for kept in ("- `Evaluator__c` is not written by any documented automation and is normally blank.",
                 "- `Interview_Type__c` does not have data in practice; the knowledge says to use `Round_Info__c`.",
                 "- The knowledge says `Evaluator__c` does not hold records in practice (normally blank)."):
        assert lean_facts(kept) == kept


# ----------------------------------------------------------------------------- review fixes (§21.19)
def test_lean_facts_drop_record_counts_the_knowledge_quotes():
    """The record agent gives every number: a snapshot or fill count in the facts block is dropped, clause by clause."""
    for line in ("- In the preprod snapshot of 2026-10-13, there were 9 sessions with `Purpose__c` = `Background Check`.",
                 "- `Interview_Support_Person__c` is filled on 1,200 of 3,000 records in the preprod snapshot, with 1,100 "
                 "of those having an Interview Success employee assigned",
                 "- `Evaluator__c` is filled on 98 % of evaluations.",
                 "- The knowledge base references a pre-production snapshot from 2026-10-13 containing 400 sessions total, "
                 "but does not provide a breakdown of how many sessions Delta Person specifically hosted."):
        assert lean_facts(FACTS + "\n" + line) == FACTS, line
    # a parenthesis with a count goes, the caveat around it stays
    assert lean_facts("- `Assigned_Trainer__c` is mostly blank (150 of 400 filled in preprod), whereas "
                      "`Assigned_Trainer_User__c` is almost empty (2 of 400 filled) and should not be used.") == (
        "- `Assigned_Trainer__c` is mostly blank, whereas `Assigned_Trainer_User__c` is almost empty and should not be used.")
    # digits in values, API names and dates, and small numbers without a count word, are no counts
    for kept in ("- `Purpose__c` has 6 values, among them `Round 15 Review` and 'Week 20'.",
                 "- `Interview_Month__c` is the first day of the month of `Date_of_Interview__c` (since 2024-01-01).",
                 "- `Drop_Date__c` is set by 2 flows when blank."):
        assert lean_facts(kept) == kept, kept


def test_lean_facts_drop_noise_clauses_and_keep_field_caveats():
    # a line with an API name loses only its noise and count clauses
    line = ("- The knowledge base notes that in the preprod snapshot of 2026-10-13, 390 of 400 sessions have a host "
            "recorded in `Host_User__c`, but it does not provide specific counts for Delta Person or a breakdown by purpose.")
    assert lean_facts(FACTS + "\n" + line) == FACTS
    assert lean_facts("- `Host_User__c` is the employee who hosted the session, but the knowledge base does not "
                      "contain record counts.") == "- `Host_User__c` is the employee who hosted the session."
    assert lean_facts("- The knowledge base does not contain counts per host; `Host_User__c` is the host.") == (
        "- `Host_User__c` is the host.")
    # a field caveat stays, with or without backticks; so does a statement about which fields exist
    for kept in ("- Per the knowledge, the Evaluator lookup does not hold data in practice (normally blank).",
                 "- The Evaluator lookup does not have data in practice, so it should not be used.",
                 "- No field or formula in the knowledge base calculates or stores the percentage of interviews that "
                 "are `Completed`",
                 "- `Status__c` is the source of truth for the state, but no aggregate metric for completion rates is "
                 "documented"):
        assert lean_facts(kept) == kept, kept


def test_a_tool_call_with_a_malformed_name_does_not_raise():
    from sf_assistant.agent.record_session import text_tool_call

    offered = _offered()
    for reply in ('{"name": ["get_field"], "arguments": {}}', '{"name": {"x": 1}, "arguments": {}}'):
        assert text_tool_call(reply, offered) is None

    class Malformed(ToolHungryLLM):   # tool calls on calls 1 and 2, then a malformed call as the last reply
        def chat(self, messages, tools=None, max_tokens=None, **kw):
            if tools:
                return super().chat(messages, tools=tools, max_tokens=max_tokens, **kw)
            self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
            return {"content": '{"name": ["get_field"], "arguments": {}}', "tool_calls": [], "usage": {}}

    r = lean_knowledge_answer(_q(), Malformed(), _multi(), _cfg())
    assert r.answer == "" and r.error == "empty final answer"     # only JSON: no facts block


def test_a_failure_while_cleaning_the_reply_is_an_error(monkeypatch):
    import sf_assistant.agent.lean as lean

    def boom(text):
        raise RuntimeError("cleaner broke")

    monkeypatch.setattr(lean, "lean_facts", boom)
    r = lean.lean_knowledge_answer(_q(), ToolHungryLLM(), _multi(), _cfg())
    assert r.answer == "" and r.error == "RuntimeError: cleaner broke"


def test_the_lean_prompt_names_a_search_tool_the_step_offers():
    assert "for example search_all_knowledge with" in rprompts.render_lean_system(3)
    assert "for example search_all_knowledge with" in rprompts.render_lean_system(3, LeanRegistry(_multi()).tools)
    single = rprompts.render_lean_system(3, LeanRegistry(_single()).tools)
    assert "for example search_knowledge with" in single and "search_all_knowledge" not in single
    llm = ToolHungryLLM(name="search_knowledge", args={"query": "interviewer"})
    r = lean_knowledge_answer(_q(), llm, _single(), _cfg())
    system = llm.requests[0]["messages"][0]["content"]
    assert system == single and r.tool_calls[0]["ok"] and r.answer == FACTS
