"""Prerequisite P2 of 2026-11-25: the records retry and the clarifying question.

``LLM.chat`` sends a forced tool choice and falls back to "auto" (forcing off for 10 minutes) when the server does not
take it; ``answer_question`` has two hooks that are off by default; ``RecordSession.ask`` forces ONE round for a
records-routed (or chart-overruled) question that names nobody when its draft ran no record tool or every call failed;
when still nothing ran, the pipeline writes the clarifying question (agent/clarify.py), and a reply "2" to it is read by
code (P2c). Through the real pipeline on the synthetic warehouse (tests/warehouse_fixture.py) with a scripted model
client: every request is kept as it would go on the wire. Made-up names, values and counts only; no model call.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(WEB))

import ask_mcp  # noqa: E402

import sf_assistant.pipeline as pipeline  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant.agent import clarify as CL  # noqa: E402
from sf_assistant.agent import followup, timeline  # noqa: E402
from sf_assistant.agent import llm as llm_mod  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent import record_session as RS  # noqa: E402
from sf_assistant.agent.knowledge_agent import answer_question  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.agent.question import Question  # noqa: E402
from sf_assistant.agent.record_session import RecordSession  # noqa: E402
from sf_assistant.config import load_config, load_records_config  # noqa: E402
from sf_assistant.knowledge.store import knowledge_files  # noqa: E402
from sf_assistant.tools.open_records import open_records  # noqa: E402
from warehouse_fixture import make_knowledge, make_warehouse  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
DRAFT = "10 client interviews (`Interview__c`, `Interview_Status__c`).\n" + SOURCE
LEAN = "- `Interview__c` = client interviews.\n- `Interview_Status__c` is the status of the interview."
KNOWLEDGE = "`Interview_Status__c` holds the status."
GROUP_CALL = {"name": "group_records", "args": {"object": "Interview__c", "group_by": "Interview_Status__c"}}
TREND_ARGS = {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
              "period": "last 6 months"}
TREND_CALL = {"name": "trend_records", "args": TREND_ARGS}
BAD_TREND = {"name": "trend_records", "args": {k: v for k, v in TREND_ARGS.items() if k != "period"}}
REFUSED_CALL = {"name": "list_records", "args": {"object": "Interview__c", "order_by": "Zoom_Passcode__c"}}
PLAIN = "how many interviews are there per status?"
L8 = "can you plot interviews of last 6 months"
CHART = "show a bar chart of interviews by status"
FORCED = rprompts.RECORDS_TOOLS_FORCED_MESSAGE
ALL_FAILED = rprompts.RECORDS_ALL_FAILED_MESSAGE
BRIDGE_LINES = ("This field is blocked, so it cannot be shown here.",
                "I could not tell what this refers to. Please ask the full question, with the name or object.",
                "One blocked field was left out.")


# ------------------------------------------------------------------------------------------------ the fake world
def _cfg():
    return load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("records_retry")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        mp.setenv("SFA_RESOLVER", "off")
        wh, kn = make_warehouse(tmp), make_knowledge(tmp)
        settings = C.load_settings(records=False, components_dir=str(tmp / "none"))
        _c, _n, registry, system, _l = pipeline.build(knowledge_files(str(kn)), settings)
        tools = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn),
                             settings.resolver)
    status = tools.status()
    tools.status = lambda: status
    yield NS(tools=tools, registry=registry, system=system)
    tools.close()


@pytest.fixture(autouse=True)
def _switches(monkeypatch):
    monkeypatch.setenv("SFA_CHARTS", "on")
    monkeypatch.setenv("SFA_TABLES", "model")
    monkeypatch.setenv("SFA_MERGE", "code")
    monkeypatch.setenv("SFA_RESOLVER", "off")
    timeline.end()
    yield
    timeline.end()


def _completion(content=None, calls=()):
    tcs = [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)]
    return NS(choices=[NS(message=NS(content=content, tool_calls=tcs or None),
                          finish_reason="tool_calls" if tcs else "stop")],
              usage=NS(prompt_tokens=100, completion_tokens=10))


class _Refused(Exception):
    status_code = 400


class Model:
    """A fake client told apart by the system prompt; the record agent gets ``steps`` in order (a dict is a tool call,
    a str a reply, the last one repeating). ``refuse_forced``: a forced request raises a 400 (the server does not take
    tool_choice); ``record_error``: every record request raises."""

    def __init__(self, *steps, route="RECORDS", names="NONE", refuse_forced=False, record_error=None):
        self.steps, self.route, self.names = steps or ("NO_RECORDS",), route, names
        self.refuse_forced, self.record_error = refuse_forced, record_error
        self.requests: dict[str, list[dict]] = {r: [] for r in ("route", "names", "lean", "knowledge", "record",
                                                                "merge", "rewrite")}
        self._lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))

    @staticmethod
    def _role(system: str) -> str:
        if system == rprompts.ROUTE_SYSTEM:
            return "route"
        if system == rprompts.NAMES_SYSTEM:
            return "names"
        if system.startswith(rprompts.COMBINE_SYSTEM):
            return "merge"
        if system.startswith("You answer questions about Salesforce RECORDS"):
            return "record"
        if system == followup.FOLLOWUP_REWRITE_SYSTEM:
            return "rewrite"
        return "lean" if system.startswith(rprompts.KNOWLEDGE_LEAN_SYSTEM[:30]) else "knowledge"

    def _create(self, **kwargs):
        role = self._role(str(kwargs["messages"][0].get("content") or ""))
        if role == "record" and self.refuse_forced and kwargs.get("tool_choice") == "required":
            with self._lock:
                self.requests["refused"] = self.requests.get("refused", []) + [kwargs.get("tool_choice")]
            raise _Refused("tool_choice required is not supported")
        with self._lock:
            n = len(self.requests[role])
            self.requests[role].append(json.loads(json.dumps(kwargs, default=str)))
        if role == "route":
            return _completion(self.route)
        if role == "names":
            return _completion(self.names)
        if role == "merge":
            return _completion(DRAFT)
        if role == "rewrite":
            return _completion("UNCLEAR")
        if role == "record":
            if self.record_error is not None:
                raise self.record_error
            if not kwargs.get("tools"):
                return _completion("")
            step = self.steps[min(n, len(self.steps) - 1)]
            return _completion(calls=[(step["name"], step["args"])]) if isinstance(step, dict) else _completion(step)
        if kwargs.get("tools") and not any(m.get("role") == "tool" for m in kwargs["messages"]):
            return _completion(calls=[("search_all_knowledge", {"query": "interview status"})])
        return _completion(LEAN if role == "lean" else KNOWLEDGE)

    def choices(self) -> list:
        return [r.get("tool_choice") for r in self.requests["record"]]

    def texts(self) -> list[str]:
        """Every user message the record agent got, in order."""
        return [m["content"] for r in self.requests["record"] for m in r["messages"] if m.get("role") == "user"]


def run(world, model, question=PLAIN, llm=None, **kw):
    cfg = _cfg()
    llm = llm or LLM(cfg, client=model)
    llm.backoff_base_s = 0
    session = RecordSession(world.tools, cfg, llm=llm)
    o = pipeline.answer(question, cfg, world.registry, world.system, llm, 1, session, **kw)
    return o, pipeline.result_row(o, session)


def _web_shape(row, session=None, question=PLAIN):
    import importlib.util

    spec = importlib.util.spec_from_file_location("sfa_web_pipeline_retry", WEB / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    page = mod.Pipeline.__new__(mod.Pipeline)
    page._status = {}
    return page._shape(question, "merged", row, "", session, False)


# ------------------------------------------------------------------------------------------------ LLM.chat
class _Client:
    def __init__(self, fail=None):
        self.kwargs: list[dict] = []
        self.fail = list(fail or [])
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.kwargs.append(dict(kwargs))
        if self.fail:
            raise self.fail.pop(0)
        return _completion("ok")


TOOLS = [{"type": "function", "function": {"name": "count_records", "parameters": {"type": "object"}}}]


def test_tool_choice_is_sent_only_when_given():
    client = _Client()
    llm = LLM(_cfg(), client=client)
    llm.chat([{"role": "user", "content": "q"}], tools=TOOLS)
    llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    llm.chat([{"role": "user", "content": "q"}], tool_choice="required")           # no tools: ignored
    assert [k.get("tool_choice") for k in client.kwargs] == ["auto", "required", None]
    assert "tools" not in client.kwargs[2]


class _Unprocessable(Exception):
    status_code = 422


class _ServerDown(Exception):
    status_code = 503


@pytest.mark.parametrize("error", [_Refused("no"), _Unprocessable("tool_choice")])
def test_a_forced_call_the_server_refuses_goes_again_as_auto_and_forcing_stays_off(monkeypatch, error):
    clock = [1000.0]
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: clock[0])
    client = _Client(fail=[error])
    llm = LLM(_cfg(), client=client)
    llm.backoff_base_s = 0
    out = llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    assert [k["tool_choice"] for k in client.kwargs] == ["required", "auto"] and out["tool_choice"] == "auto_fallback"
    assert llm.forced_fallbacks == 1 and llm.forcing_off()
    out = llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    assert client.kwargs[-1]["tool_choice"] == "auto" and out["tool_choice"] == "auto_off"
    clock[0] += llm_mod.FORCED_OFF_S + 1
    llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    assert client.kwargs[-1]["tool_choice"] == "required" and not llm.forcing_off()


def test_a_busy_or_failing_server_keeps_the_forced_choice_and_forcing_on(monkeypatch):
    """Review fix R4 / E-20: a 503 (or a timeout) of a forced call takes the normal backoff retries with "required"
    kept, and forcing stays on; an error that is no HTTP refusal is raised as for any call."""
    clock = [1000.0]
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: clock[0])
    client = _Client(fail=[_ServerDown("busy")])
    llm = LLM(_cfg(), client=client)
    llm.backoff_base_s = 0
    out = llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    assert [k["tool_choice"] for k in client.kwargs] == ["required", "required"] and out["tool_choice"] == "required"
    assert llm.forced_fallbacks == 0 and not llm.forcing_off()
    client = _Client(fail=[RuntimeError("anything")])
    llm = LLM(_cfg(), client=client)
    with pytest.raises(RuntimeError):
        llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    assert len(client.kwargs) == 1 and not llm.forcing_off()
    # a 400 about the temperature is no refusal of the choice: its own fallback drops the temperature, the choice stays
    client = _Client(fail=[_Refused("temperature is not supported")])
    llm = LLM(_cfg(), client=client)
    llm.chat([{"role": "user", "content": "q"}], tools=TOOLS, tool_choice="required")
    if "temperature" in client.kwargs[0]:
        assert [k["tool_choice"] for k in client.kwargs] == ["required", "required"]
        assert "temperature" not in client.kwargs[1] and not llm.forcing_off()


def test_a_call_that_is_not_forced_keeps_its_retries():
    client = _Client(fail=[_Refused("bad request")])
    llm = LLM(_cfg(), client=client)
    with pytest.raises(_Refused):
        llm.chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert len(client.kwargs) == 1 and llm.forced_fallbacks == 0 and not llm.forcing_off()


# ------------------------------------------------------------------------------------------------ the two hooks
class _Scripted:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, dict):
            return {"content": None, "tool_calls": [{"id": "c1", "name": reply["name"], "arguments": {}}], "usage": {}}
        return {"content": reply, "tool_calls": [], "usage": {}}


class _Registry:
    tools = {"count_records": None}

    def openai_tools(self):
        return TOOLS


def _q():
    return Question(qid="q1", category="cross_section", question="q", expected_facts=[], expected_tools=[],
                    answerable=True, source="t", gold_chunk_ids=[])


def test_hooks_unset_send_the_same_requests():
    a, b = _Scripted("draft one", "draft two"), _Scripted("draft one", "draft two")
    answer_question(_q(), a, _Registry(), NS(max_tool_steps=4), require_tools=True, require_tools_message="M")
    answer_question(_q(), b, _Registry(), NS(max_tool_steps=4), require_tools=True, require_tools_message="M",
                    tools_first=lambda draft: ("M", None))
    assert a.calls == b.calls and not any("tool_choice" in c for c in a.calls)


def test_the_hooks_force_the_next_round_only():
    llm = _Scripted("no tool", "still none", "final")
    seen = []
    answer_question(_q(), llm, _Registry(), NS(max_tool_steps=6), require_tools=True,
                    tools_first=lambda draft: (seen.append(draft) or "FORCE", "required"),
                    check_answer=lambda draft: ("AGAIN", "required"), max_checks=1)
    assert seen == ["no tool"]
    assert [c.get("tool_choice") for c in llm.calls] == [None, "required", "required"]
    assert llm.calls[1]["messages"][-1]["content"] == "FORCE" and llm.calls[2]["messages"][-1]["content"] == "AGAIN"


def test_retry_kind():
    ok, bad = NS(ok=True, error_code=None), NS(ok=False, error_code="E5")   # E5: a period the tool could not read
    assert RS.retry_kind("", []) == "no_call"
    assert RS.retry_kind("", [bad, ok]) is None and RS.retry_kind("", [bad]) == "all_failed"
    # not found, refused, and (review fix R3 / E-23) a fault of the engine: the time budget, the query guard, DuckDB,
    # an internal error
    for code in ("E2c", "E1d", "E4h", "E15a", "E1b", "E2b", "E10", "E12", "E13", "internal"):
        assert RS.retry_kind("", [bad, NS(ok=False, error_code=code)]) is None, code


def test_refusal_codes_have_one_home():
    from sf_assistant.agent import checks

    assert RS.REFUSAL_CODES is checks.REFUSAL_CODES and ask_mcp.REFUSAL_CODES == checks.REFUSAL_CODES


# ------------------------------------------------------------------------------------------------ the record session
def test_L8_shape_the_second_round_is_forced_and_answers(world):
    model = Model("I can plot that for you.", TREND_CALL, "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    o, row = run(world, model, L8)
    assert model.choices() == ["auto", "required", "auto"] and len(model.requests["record"]) == 3
    assert model.requests["record"][1]["messages"][-1]["content"] == FORCED
    assert rprompts.RECORDS_FINAL_FORM_MESSAGE not in model.texts()
    assert o.kind in ("merged", "records") and o.rec.tool_retry == {"at": "tools_first", "why": "no_call",
                                                                    "choice": "required"}
    assert row["jsonl"]["record_tool_retry"] == o.rec.tool_retry and "record_no_data" not in row["jsonl"]
    assert "records_tools_forced_message" in row["jsonl"]["record_nudge_kinds"]


def test_no_records_twice_after_a_records_route_is_forced(world):
    model = Model("NO_RECORDS", GROUP_CALL, DRAFT)
    o, _row = run(world, model)
    assert model.choices()[1] == "required" and o.rec.tool_retry["why"] == "no_records"
    assert o.kind in ("merged", "records") and o.mode == "lean"


def test_a_chart_question_that_says_no_records_is_not_forced(world):
    model = Model("NO_RECORDS")
    o, _row = run(world, model, CHART)
    assert "required" not in model.choices() and o.rec.tool_retry is None
    assert rprompts.RECORDS_REQUIRE_TOOLS_MESSAGE in model.texts()


@pytest.mark.parametrize("route", ["MAYBE", "NO_RECORDS"])
def test_an_unclear_or_design_route_is_not_forced(world, route):
    model = Model("There are many.", route=route)
    o, row = run(world, model)
    assert "required" not in model.choices() and (o.rec is None or o.rec.tool_retry is None)
    assert "record_tool_retry" not in row["jsonl"] and "clarify" not in row["jsonl"]


def test_a_person_question_is_not_forced(world):
    model = Model("Delta Person took some interviews.", names="Delta Person")
    o, _row = run(world, model, "how many internal interviews did Delta Person take?")
    assert "required" not in model.choices() and o.rec.tool_retry is None and not o.clarify


def test_every_call_failed_gets_one_forced_retry(world):
    model = Model(BAD_TREND, "The trend could not be read.", TREND_CALL,
                  "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    o, _row = run(world, model, "how many interviews per month in the last 6 months?")
    assert model.choices() == ["auto", "auto", "required", "auto"]
    assert ALL_FAILED in model.texts() and o.rec.tool_retry == {"at": "check", "why": "all_failed",
                                                                "choice": "required"}
    assert [e.ok for e in o.rec.evidence] == [False, True] and o.kind in ("merged", "records")


def test_a_refused_field_is_never_retried_around(world):
    model = Model(REFUSED_CALL, "That field is blocked.")
    o, row = run(world, model, "list the interviews by their zoom passcode")
    assert "required" not in model.choices() and ALL_FAILED not in model.texts()
    assert o.rec.tool_retry is None and o.rec.no_data is None and "clarify" not in row["jsonl"]
    assert ask_mcp.decide(_web_shape(row))[0] == "refused"


def test_the_budget_is_one_forced_round(world):
    model = Model("Sure.")
    o, _row = run(world, model)
    assert model.choices().count("required") == 1 and model.texts().count(FORCED) == 1
    assert rprompts.RECORDS_FINAL_FORM_MESSAGE not in model.texts() and o.rec.tool_retry["at"] == "tools_first"


# ------------------------------------------------------------------------------------------------ review fixes (P2)
UNSUPPORTED_HEAD = rprompts.RECORDS_UNSUPPORTED_MESSAGE.split("<<NUMBERS>>")[0][:40]
DESCRIBE_CALL = {"name": "describe_fields", "args": {"object": "Interview__c"}}
SYNC_CALL = {"name": "get_sync_status", "args": {}}
MADE_UP = "There are 4242 interviews over the months (`Interview__c`).\n" + SOURCE


def test_the_draft_after_a_forced_check_gets_the_full_check(world):
    """E-16: the forced round at the first draft check counts no draft, so the data-backed draft after it gets
    check_draft (the number audit): a made-up 4242 is sent back, never shown."""
    model = Model(BAD_TREND, "The trend could not be read.", TREND_CALL, MADE_UP,
                  "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    o, _row = run(world, model, "how many interviews per month in the last 6 months?")
    assert o.rec.tool_retry["at"] == "check" and any(t.startswith(UNSUPPORTED_HEAD) for t in model.texts())
    assert "4242" not in o.final
    # without a forced round the same draft gets the same message (the control)
    model = Model(TREND_CALL, MADE_UP, "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    run(world, model, "how many interviews per month in the last 6 months?")
    assert any(t.startswith(UNSUPPORTED_HEAD) for t in model.texts())


def test_a_third_draft_is_still_not_checked(world):
    """The forced round adds one check, never a third draft check: after tools_first was forced, draft 3 is taken."""
    model = Model("Sure.", TREND_CALL, MADE_UP, MADE_UP, "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    o, _row = run(world, model, "how many interviews per month in the last 6 months?")
    assert o.rec.tool_retry["at"] == "tools_first"
    assert sum(t.startswith(UNSUPPORTED_HEAD) for t in model.texts()) <= 1 and len(model.requests["record"]) <= 5


def test_a_warehouse_fault_stays_a_failure(world, monkeypatch):
    """E-23 / R3: an engine fault (E13 here) gets no forced round and no clarifying question: the record step fails,
    so the caller's older route runs."""
    from sf_assistant.engine.warehouse import WarehouseError

    def broken(*a, **k):
        raise WarehouseError("IO Error: could not read the file", kind="error")

    monkeypatch.setattr(world.tools.warehouse, "run", broken)
    model = Model(GROUP_CALL, "It could not be read.", GROUP_CALL, "Still nothing.")
    o, row = run(world, model)
    assert {e.error_code for e in o.rec.evidence if not e.ok} == {"E13"}
    assert "required" not in model.choices() and ALL_FAILED not in model.texts()
    assert not o.clarify and o.rec.no_data is None and not CL.is_clarify(o.final)
    assert o.final.startswith(pipeline.RECORDS_FAILED_ANSWER) and ask_mcp.decide(_web_shape(row))[0] == "failed"


def test_retry_once_is_one_retry_when_the_forced_call_fails(world):
    """E-17: once the forced round is spent (tools_first here) and its call fails, the draft gets the checks of before,
    not a second retry message: one retry, the clarifying question."""
    model = Model("Sure.", BAD_TREND, "It could not be read.", BAD_TREND, "Still nothing.")
    o, _row = run(world, model)
    last = model.requests["record"][-1]["messages"]
    retries = [m["content"] for m in last if m.get("role") == "user" and m["content"] in (FORCED, ALL_FAILED)]
    assert retries == [FORCED] and model.choices().count("required") == 1
    assert len(model.requests["record"]) <= 4 and CL.is_clarify(o.final)


def test_a_forced_no_records_draft_whose_round_read_nothing_keeps_the_knowledge_answer(world):
    """E-19: a design question the route call read as RECORDS: the model says NO_RECORDS, the forced call fails, the
    model writes prose. The NO_RECORDS stands: the full knowledge answer, no clarifying question."""
    model = Model("NO_RECORDS", BAD_TREND, "The status field holds the interview stage.")
    o, _row = run(world, model, "what does the interview status field mean?")
    assert o.rec.tool_retry["why"] == "no_records" and o.rec.no_records and o.rec.no_data is None
    assert not CL.is_clarify(o.final) and not o.clarify and o.kind == "knowledge" and o.mode == "lean+full"
    assert o.rec.dropped_answer.startswith("The status field")


@pytest.mark.parametrize("call", [DESCRIBE_CALL, SYNC_CALL], ids=["describe_fields", "get_sync_status"])
def test_numbers_after_only_describe_fields_get_the_forced_query_round(world, call):
    """E-15: describe_fields (or the sync status) alone does not count as data: a draft with numbers gets the forced
    run-query round; a data call then answers."""
    model = Model(call, "There are 7 interviews per status (`Interview__c`).\n" + SOURCE, GROUP_CALL, DRAFT)
    o, _row = run(world, model)
    assert o.rec.tool_retry == {"at": "check", "why": "no_data_call", "choice": "required"}
    assert rprompts.RECORDS_RUN_QUERY_MESSAGE in model.texts() and model.choices().count("required") == 1
    assert o.kind in ("merged", "records") and any(e.ok and e.tool == "group_records" for e in o.rec.evidence)


@pytest.mark.parametrize("call", [DESCRIBE_CALL, SYNC_CALL], ids=["describe_fields", "get_sync_status"])
def test_numbers_that_still_rest_on_no_data_call_are_no_answer(world, call):
    model = Model(call, "There are 7 interviews per status (`Interview__c`).\n" + SOURCE)
    o, _row = run(world, model)
    data = [e for e in o.rec.evidence if e.ok and e.kind == "data" and e.tool != "get_sync_status"]
    assert not data and o.rec.no_records and o.rec.no_data == "no_call" and CL.is_clarify(o.final)
    assert "7 interviews" not in o.final


def test_a_coded_failure_is_never_forced_around():
    """The E-15 rule never runs around a not-found, refusal or engine code: the checks of before answer those."""
    ok = NS(ok=True, error_code=None, kind="orientation", tool="describe_fields")
    for code in ("E4h", "E15a", "E1b", "E2b", "E2c", "E1d", "E10", "E12", "E13", "internal"):
        assert RS._coded_failure([ok, NS(ok=False, error_code=code, kind="data", tool="count_records")]), code
    assert not RS._coded_failure([ok, NS(ok=False, error_code="E5", kind="data", tool="count_records")])
    assert not RS._coded_failure([ok])


def test_a_stored_value_answer_after_describe_fields_is_kept(world):
    """The E-15 rule reads numbers only: "which values" answered from describe_fields with no count stays."""
    model = Model(DESCRIBE_CALL, "The status values are Completed, Scheduled and Canceled (`Interview__c`).\n" + SOURCE)
    o, _row = run(world, model, "which status values does an interview have?")
    assert o.rec.tool_retry is None and not o.clarify and "Completed" in o.final


class _EchoKnowledge:
    """The knowledge agent answers with no tool, and after the require-tools nudge echoes it (made-up wording)."""

    def __init__(self):
        from sf_assistant.agent.knowledge_agent import REQUIRE_TOOLS_MESSAGE

        self.nudge = REQUIRE_TOOLS_MESSAGE
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        if (kwargs["messages"][-1].get("content") or "") == self.nudge:
            return _completion("You're right, I should not answer from memory. Which object do you mean?")
        return _completion("Sure, what would you like to know?")


@pytest.mark.parametrize("q", ["can you tell me about one thing please, just one", "can you help me with my thing"])
def test_a_near_miss_of_small_talk_never_shows_the_nudge(world, q):
    """E-22: a near miss of the small-talk phrases reaches the knowledge agent; its echo of the nudge is not shown."""
    o = pipeline.answer(q, _cfg(), world.registry, world.system, LLM(_cfg(), client=_EchoKnowledge()), 1, None)
    assert "from memory" not in o.final and "Which object do you mean?" in o.final


# ------------------------------------------------------------------------------------------------ the clarifying question
def test_still_nothing_gives_the_clarifying_question(world):
    model = Model("I can plot that for you.")
    o, row = run(world, model, L8)
    assert o.kind == "none" and CL.is_clarify(o.final) and o.final == o.body
    assert o.rec.no_data == "no_call" and o.clarify["why"] == "no_call" and o.clarify["from"] == "question"
    assert o.clarify["objects"] == ["Interview__c"] and 2 <= o.clarify["choices"] <= 3
    assert "1. How many interviews were there in the last 6 months?" in o.final
    assert "2. Plot the number of interviews per month in the last 6 months" in o.final
    assert "3. Plot interviews by Interview Status in the last 6 months" in o.final
    assert o.charts == [] and o.status_lines == [] and "No chart" not in o.final
    assert not any(b in o.final for b in BRIDGE_LINES) and pipeline.RECORDS_FAILED_ANSWER not in o.final
    j = row["jsonl"]
    assert j["clarify"] == o.clarify and j["record_no_data"] == "no_call" and j["record_tool_retry"]["at"] == \
        "tools_first"
    shaped = _web_shape(row, question=L8)
    assert shaped["trace"]["clarify"] == o.clarify and ask_mcp.decide(shaped) == ("answered", "clarify")


def test_the_server_refusing_the_forced_call_still_ends_with_the_question(world):
    model = Model("Sure, here it is.", refuse_forced=True)
    llm = LLM(_cfg(), client=model)
    o, _row = run(world, model, PLAIN, llm=llm)
    assert model.requests.get("refused") == ["required"] and llm.forced_fallbacks == 1
    assert o.rec.tool_retry["choice"] == "auto_fallback" and CL.is_clarify(o.final)
    o, _row = run(world, Model("Sure, here it is."), PLAIN, llm=llm)   # forcing is off now: one failed try in all
    assert o.rec.tool_retry["choice"] == "auto_off"


def test_a_model_fault_stays_a_failure(world):
    model = Model(record_error=RuntimeError("model down"))
    o, row = run(world, model)
    assert o.final.startswith(pipeline.RECORDS_FAILED_ANSWER) and not o.clarify and o.rec.no_data is None
    assert ask_mcp.decide(_web_shape(row))[0] == "failed"


def test_the_last_answer_objects_reach_the_choices(world):
    model = Model("Sure.")
    o, _row = run(world, model, "can you tell me by those records?", context_objects=("Interview__c", "Nope__c"))
    assert o.clarify["from"] in ("question", "last_answer") and "Interview__c" in o.clarify["objects"]


# ------------------------------------------------------------------------------------------------ clarify, by itself
class _Spec(NS):
    pass


def _session(objects=("Interview__c",), hits=None):
    specs = {o: _Spec(api=o, label=o.replace("__c", "").replace("_", " "), plural_label=o.replace("__c", "s")
                      .replace("_", " "), queryable=True) for o in objects}

    class Catalog:
        def __init__(self):
            self.objects = specs

        def search_objects(self, q, k=3):
            return [(specs[a], 2.0, how) for a, how in (hits or [])]

    return NS(tools=NS(catalog=Catalog()), status_of=lambda api: {"field": "Status__c", "field_label": "Status"})


def test_choices_from_each_source():
    rec = NS(no_data="all_failed", evidence=[NS(object="Interview__c", ok=False)])
    text, info = CL.build("support person performance last month", None, rec, _session())
    assert info["from"] == "terms" and "Show support person performance in the last month" in text
    text, info = CL.build("placement rate this year", NS(chart=True, table=False), rec, _session())
    assert info["from"] == "terms" and "Plot the placement rate this year" in text
    text, info = CL.build("how many interviews?", None, rec, _session(hits=[("Interview__c", ["plural_label"])]))
    assert info["from"] == "question" and "How many interviews are there?" in text
    assert "Show interviews by Status in the last 12 months" not in text and "Show interviews by Status" in text
    text, info = CL.build("can you tell me by those records?", None, rec,
                          _session(hits=[("Interview__c", ["description"])]))
    assert info["from"] == "calls" and info["objects"] == ["Interview__c"]
    rec = NS(no_data="no_call", evidence=[])
    text, info = CL.build("can you tell me by those records?", NS(chart=False, table=True), rec,
                          _session(objects=("Offer__c",)), ("Offer__c", "Gone__c"))
    assert info["from"] == "last_answer" and "Show a table of the number of offers per month" in text
    text, info = CL.build("can you tell me by those records?", None, rec, _session(), ())
    assert info["from"] == "examples" and text.startswith(CL.TEXTS["lead_no_object"]) and info["choices"] == 3


def test_the_period_is_the_questions_own_words():
    rec = NS(no_data="no_call", evidence=[])
    hits = [("Interview__c", ["label"])]
    for q, words in (("interviews in the last six months?", " in the last six months"),
                     ("interviews this quarter", " this quarter"), ("interviews in March 2026", " in March 2026")):
        text, _info = CL.build(q, None, rec, _session(hits=hits))
        assert f"How many interviews were there{words}?" in text, q


def test_no_bridge_line_and_no_placeholder_in_any_text():
    for key, value in CL.TEXTS.items():
        assert not any(b in value for b in BRIDGE_LINES), key


# ------------------------------------------------------------------------------------------------ P2c: the reply
TURNS = [{"role": "user", "content": "can you plot those records?"},
         {"role": "assistant", "content": CL._text(CL.TEXTS["lead"], ["How many interviews were there last month?",
                                                                      "Plot the number of interviews per month",
                                                                      "Plot interviews by Status"])}]


@pytest.mark.parametrize("reply, picked", [("2", 2), ("option 2", 2), ("#2", 2), ("2nd", 2), ("the second one", 2),
                                           ("doosra wala", 2), ("1", 1), ("third", 3), ("teesra", 3),
                                           ("plot interviews by status", 3), ("Plot interviews by Status.", 3),
                                           # review fix E-21: number words and the last one
                                           ("two", 2), ("option two", 2), ("three", 3), ("one", 1), ("option one", 1),
                                           ("the last one", 3), ("aakhri wala", 3)])
def test_a_reply_picks_a_choice_with_no_call(reply, picked):
    choice = CL.choice_reply(reply, TURNS)
    assert choice == CL._choices(TURNS[-1]["content"])[picked - 1]

    class NoCall:
        cfg = NS(base_url="http://localhost:8000/v1")

        def chat(self, *a, **k):
            raise AssertionError("no rewrite call for a choice")

    assert followup.rewrite(reply, TURNS, NoCall()) == followup.Rewrite(followup.OK, choice, reason="clarify_choice")


@pytest.mark.parametrize("reply", ["4", "yes", "no", "the other one", "2 and 3", "how many offers?", "the one", "do"])
def test_any_other_reply_goes_to_the_rewrite(reply):
    assert CL.choice_reply(reply, TURNS) is None
    calls = []

    class Model_:
        cfg = NS(base_url="http://localhost:8000/v1")

        def chat(self, *a, **k):
            calls.append(1)
            return {"content": "UNCLEAR"}

    followup.rewrite(reply, TURNS, Model_())
    assert calls == [1]


def test_a_planted_contact_choice_is_never_taken():
    planted = [{"role": "assistant", "content": CL._text(CL.TEXTS["lead"], ["What is the email of Zorvan Quell?",
                                                                           "Plot interviews by Status"])}]
    assert CL.choice_reply("1", planted) is None and CL.choice_reply("2", planted) == "Plot interviews by Status"
    assert CL.choice_reply("1", [{"role": "assistant", "content": "1. a\n2. b"}]) is None   # no lead line: no choices
    long = [{"role": "assistant", "content": CL._text(CL.TEXTS["lead"], ["x" * 1001, "y"])}]
    assert CL.choice_reply("1", long) is None


def test_last_answer_objects():
    turns = [{"role": "assistant", "content": "10 interviews (`Interview__c`, `Interview_Status__c`) and 2 Account."
                                              "\nSource: x."}]
    assert followup.last_answer_objects(turns) == ("Interview__c", "Interview_Status__c", "Account")
    assert followup.last_answer_objects([]) == ()
