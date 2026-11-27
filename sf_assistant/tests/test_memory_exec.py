"""Salesforce conversation memory, execution and contract v2 (design v2 of 2026-11-25 §4, §6.1, §7, §8, §9, §10, §14,
§16.1; lane S3): the ask service's job with ``SFA_MEMORY`` off, shadow and on, the seeded calls of a stored or patched
plan through the real record tools, the rebuilt terms, and the parity of shadow with off.

The harness (:class:`World`) is the real thing but the OpenAI client: the page's ``Pipeline`` (web/pipeline.py), the
real ``sf_assistant.cli.ask``, the record agent and the record tools on the synthetic terms warehouse
(``tests/terms_fixture.py``, today pinned to 2026-10-13), and the service's job (no HTTP). The client (:class:`Client`)
is scripted per role, told apart by the system prompt: no model call of any kind, invented names only.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))
sys.path.insert(0, str(ROOT / "tests"))

import ask_mcp  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import terms_fixture as TF  # noqa: E402
import warehouse_fixture as F  # noqa: E402

from sf_assistant import config as sfa_config  # noqa: E402
from sf_assistant import pipeline as core  # noqa: E402
from sf_assistant.agent import followup, knowledge_agent, query_state as Q, record_prompts, record_session  # noqa: E402
from sf_assistant.agent import resolver as R  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.cli import ask as cli_ask  # noqa: E402
from sf_assistant.engine import term_contracts as TC  # noqa: E402
from sf_assistant.knowledge.store import knowledge_files  # noqa: E402

TODAY = dt.date(2026, 10, 13)
SOURCE = "Source: the synthetic warehouse copy."
LEAN = "- `Account` = accounts.\n- `Interview__c` = client interviews."
ON = sfa_config.MemoryConfig(mode="on")
SHADOW = sfa_config.MemoryConfig(mode="shadow")
OFF = sfa_config.MemoryConfig(mode="off")
#: the candidates grouped by status, as the record agent calls it for "active or placed candidates by status"
BY_STATUS = {"object": "Account", "group_by": "Candidate_Status__c",
             "filters": [{"field": "Candidate_Status__c", "op": "in", "values": ["Active", "Placed"]}]}
FIRST = "how many active or placed candidates by status"


def completion(content=None, calls=(), finish=None):
    tool_calls = [types.SimpleNamespace(id=f"c{i}", function=types.SimpleNamespace(name=n, arguments=json.dumps(a)))
                  for i, (n, a) in enumerate(calls)]
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls or None)
    choice = types.SimpleNamespace(message=message, finish_reason=finish or ("tool_calls" if calls else "stop"))
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=1))


class Client:
    """A fake OpenAI client, scripted per role (told apart by the system prompt). ``record``: the record agent's steps,
    a dict (one tool call ``{"name", "args"}``) or a str (its reply), used in order; when none is left it replies
    ``"The figures are shown.\\n" + SOURCE``. ``resolve`` and ``rewrite``: the resolver's and the rewrite's replies, in
    order. ``requests[role]`` keeps every request."""

    ROLES = ("rewrite", "resolve", "route", "names", "lean", "knowledge", "record", "merge")

    def __init__(self) -> None:
        self.record: list = []
        self.resolve: list[str] = []
        self.rewrite: list[str] = []
        self.requests: dict[str, list] = {k: [] for k in self.ROLES}
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self._lock = threading.Lock()

    @staticmethod
    def _role(system: str) -> str:
        if system == followup.FOLLOWUP_REWRITE_SYSTEM:
            return "rewrite"
        if system == R.FOLLOWUP_RESOLVE_SYSTEM:
            return "resolve"
        if system == record_prompts.ROUTE_SYSTEM:
            return "route"
        if system == record_prompts.NAMES_SYSTEM:
            return "names"
        if system.startswith(record_prompts.COMBINE_SYSTEM):
            return "merge"
        if system.startswith("You answer questions about Salesforce RECORDS"):
            return "record"
        return "lean" if system.startswith(record_prompts.KNOWLEDGE_LEAN_SYSTEM[:30]) else "knowledge"

    def _create(self, **kw):
        role = self._role(str(kw["messages"][0].get("content") or ""))
        with self._lock:
            self.requests[role].append(copy.deepcopy(kw))
        if role == "rewrite":
            return completion(self.rewrite.pop(0) if self.rewrite else "UNCLEAR")
        if role == "resolve":
            return completion(self.resolve.pop(0) if self.resolve else "{}")
        if role == "route":
            return completion("RECORDS")
        if role == "names":
            return completion("NONE")
        if role == "merge":
            return completion("merged text\n" + SOURCE)
        if role == "record":
            if not kw.get("tools"):
                return completion("The figures are shown.\n" + SOURCE)
            step = self.record.pop(0) if self.record else "The figures are shown.\n" + SOURCE
            return completion(calls=[(step["name"], step["args"])]) if isinstance(step, dict) else completion(step)
        if kw.get("tools") and not any(m.get("role") == "tool" for m in kw["messages"]):
            return completion(calls=[("search_all_knowledge", {"query": "accounts"})])
        return completion(LEAN if role == "lean" else "`Account` holds the accounts.")

    def record_messages(self, n: int = -1) -> list[dict]:
        return self.requests["record"][n]["messages"]


class World:
    """The page's Pipeline, ``cli.ask``, the record agent and the record tools on the terms fixture, and the ask service's
    job, with one scripted client and the memory switches given."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, memory=ON, tables=None) -> None:
        monkeypatch.setenv("SFA_CHARTS", "on")
        monkeypatch.setenv("SFA_TABLES", "model")
        monkeypatch.setenv("SFA_MERGE", "code")
        monkeypatch.setenv("SFA_RESOLVER", "off")
        monkeypatch.setattr(LLM, "backoff_base_s", 0)
        self.client = Client()
        wh = F.make_warehouse(tmp, tables=tables or TF.TERMS_TABLES)
        kn = F.make_knowledge(tmp, knowledge=TF.TERMS_KNOWLEDGE)
        settings = sfa_config.load_settings(records=False, components_dir=str(tmp / "none"))
        _c, _n, registry, system, _l = core.build(knowledge_files(str(kn)), settings)
        cfg = sfa_config.load_config(base_url="http://127.0.0.1:9/v1", api_key="x")
        llm = LLM(cfg, client=self.client)

        def open_session(cfg, rcfg, knowledge_dir=None, **kw):
            session, lines = core.open_record_session(cfg, rcfg, knowledge_dir=knowledge_dir, **kw)
            if session is not None:
                session.llm = llm     # the scripted client; everything else is the real session
            return session, lines

        rcfg = sfa_config.load_records_config(warehouse=str(wh), today=TODAY)
        self.log = ask_mcp.RotatingAskLog(str(tmp / "ask_logs"))
        ask_mod = types.SimpleNamespace(ask=cli_ask, open_record_session=open_session)
        self.pipe = pipeline_mod.Pipeline(ask_mod, cfg, registry, system, llm, rcfg, log=self.log.write,
                                          knowledge_dir=str(kn))
        self.service = ask_mcp.AskService(self.pipe, types.SimpleNamespace(state=lambda: "ok"), self.log,
                                          memory=memory)

    @property
    def tools(self):
        return self.pipe.session.tools

    def job(self, question: str, turns: list | None = None, **v2) -> dict:
        """One job as ``AskService.ask`` runs it: a v2 call when ``v2`` holds contract-v2 keys (``contract`` 2 is
        added), else a v1 call. The job's outcome, with ``reply`` (what the caller gets) added."""
        arguments = {"question": question, **({"recent_turns": turns} if turns is not None else {})}
        if v2:
            arguments.update({"contract": 2, **v2})
        assert ask_mcp._bad_arguments(arguments) is None
        req = self.service._request(arguments)
        out = self.service._job(question, turns) if req is None else self.service._job(question, turns, req)
        return {**out, "reply": self.service._final(out)}

    def close(self) -> None:
        self.service.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    made: list[World] = []

    def build(memory=ON, tables=None) -> World:
        folder = tmp_path / str(len(made))
        folder.mkdir()
        w = World(folder, monkeypatch, memory, tables)
        made.append(w)
        return w

    yield build
    for w in made:
        w.close()


def stored(state: dict) -> dict:
    """``state`` as the platform stores it: a JSON round trip with the keys in another order (jsonb reorders)."""
    def shuffle(v):
        if isinstance(v, dict):
            return {k: shuffle(v[k]) for k in sorted(v, reverse=True)}
        if isinstance(v, list):
            return [shuffle(x) for x in v]
        return v
    return json.loads(json.dumps(shuffle(state)))


def first_turn(w: World, question: str = FIRST, args: dict | None = None, tool: str = "group_records",
               reply: str = "The candidates by status are shown.\n" + SOURCE) -> dict:
    """A first question (no state, no turn) that the record agent answers with one call: the job's outcome."""
    w.client.record = [{"name": tool, "args": args or BY_STATUS}, reply]
    return w.job(question, state=None, gap=0)


def codes_only(value) -> bool:
    """``value`` holds fixed codes and numbers only: every text is a short lower-case code (the log's ``memory``)."""
    if isinstance(value, dict):
        return all(codes_only(k) and codes_only(v) for k, v in value.items())
    if isinstance(value, list):
        return all(codes_only(v) for v in value)
    if isinstance(value, str):
        return len(value) <= 40 and all(ch.islower() or ch.isdigit() or ch in "_+" for ch in value)
    return value is None or isinstance(value, (bool, int, float))


def turns_after(question: str, out: dict) -> list[dict]:
    return [{"role": "user", "content": question}, {"role": "assistant", "content": out["reply"]["answer"]}]


# ------------------------------------------------------------------------------------------------ seeded calls (§7.1)
def test_a_seeded_call_passes_the_record_tools_and_its_terms_are_enforced_again(world):
    """"visualize it" after "active or placed candidates by status": the stored call runs through RecordTools.call in
    its model form, the candidates contract adds the Person Account filter again (the words "active or placed" carried
    as typed words, so no T2), and the record agent starts from the seeded call and its result."""
    w = world()
    one = first_turn(w)
    state = one["reply"]["state"]
    assert state["plan"]["calls"][0]["args"] == BY_STATUS and "RecordTypeId" not in json.dumps(state)
    w.client.requests = {k: [] for k in Client.ROLES}
    two = w.job("visualize it", turns_after(FIRST, one), state=stored(state), gap=0)
    reply = two["reply"]
    assert reply["status"] == "answered" and reply["kind"] == "records" and reply["reason"] == "shortcut", two
    seeded = [e for e in w.tools.evidence if e.tool == "group_records"]
    assert len(seeded) == 1 and seeded[0].ok
    assert seeded[0].arguments == BY_STATUS                                     # the model form went in
    assert seeded[0].applied.get("term_filters") == ["RecordTypeId = 'Person Account'"]
    assert seeded[0].enforced_arguments["filters"][-1] == {"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}
    # the record agent's first request starts with the seeded call and its result, and is its only request: the
    # seed counts as a tool call (no require-tools nudge, no forced round, C35), and no other role was called
    msgs = w.client.record_messages(0)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["tool_calls"][0]["id"] == "seed_1"
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == BY_STATUS
    assert json.loads(msgs[3]["content"])["ok"] is True
    assert len(w.client.requests["record"]) == 1
    assert all(r.get("tool_choice") != "required" for r in w.client.requests["record"])
    assert {k for k, v in w.client.requests.items() if v} == {"record"}       # no route, names, knowledge or merge
    # the next state keeps the model form, the display flag and the typed words of the chain
    nxt = reply["state"]["plan"]
    assert nxt["calls"][0]["args"] == BY_STATUS and nxt["show"]["chart"] is True
    assert nxt["terms"] == {"keys": ["candidates", "placement"], "text": FIRST + "\nvisualize it"}


def test_the_chart_comes_from_the_show_flag_and_the_read_as_line_is_the_plan_line(world):
    w = world()
    one = first_turn(w)
    two = w.job("visualize it", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    reply = two["reply"]
    assert reply["charts"] and reply["charts"][0]["chart"]
    line = reply["plan_line"]
    assert line.endswith("as a chart") and reply["answer"].startswith(f"Read as: `{line}`")
    # the record agent reads the question with its display words (pre-release fix of 2026-11-25: the typed text
    # keeps "as a chart"); the search inputs still read it without them
    user = w.client.record_messages(-1)[1]["content"]
    assert user.split("\n")[0] == line


def test_the_support_offer_rate_comes_back_on_a_follow_up_that_names_no_term(world):
    """C18: "visualize it" names no term; the stored words "support person performance" complete the support call
    again (the outcome split and the offer rate), which today's terms of "visualize it" never would."""
    w = world()
    group = {"object": "Interview__c", "group_by": "Interview_Support_Person__c"}
    one = first_turn(w, "support person performance", group)
    assert one["reply"]["state"]["plan"]["calls"][0]["args"] == group
    two = w.job("visualize it", turns_after("support person performance", one), state=stored(one["reply"]["state"]),
                gap=0)
    assert two["reply"]["status"] == "answered", two
    ev = [e for e in w.tools.evidence if e.tool == "group_records"][0]
    assert ev.arguments == group and ev.enforced_arguments == {**group, "split_by": "Interview_Outcome__c"}
    assert ev.model_result["offer_rate_overall_pct"] == TF.TERMS_FACTS["offer_rate_overall"]
    w.tools.begin_question("visualize it")                                     # today's terms of the same words
    out = w.tools.call("group_records", group)
    assert out["ok"] and "offer_rate_overall_pct" not in out["result"]


def test_a_job_with_state_then_a_job_without_carries_nothing(world):
    """C15: the record tools live across jobs; the job after a patched one gets today's terms of its own question."""
    w = world()
    one = first_turn(w)
    w.job("visualize it", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert "candidates" in w.tools._terms.keys
    w.client.record = [{"name": "count_records", "args": {"object": "Account"}}, "The accounts are counted.\n" + SOURCE]
    three = w.job("how many accounts are there")                                 # a v1 call: no state, no terms
    assert three["reply"]["status"] == "answered" and set(three["reply"]) == set(ask_mcp._result("answered"))
    ev = [e for e in w.tools.evidence if e.tool == "count_records"][-1]
    assert ev.ok and "term_filters" not in ev.applied
    assert w.tools._terms == w.tools._terms_for("how many accounts are there")


def test_begin_question_takes_the_terms_given_and_defaults_to_todays(world):
    w = world()
    w.pipe.ensure_fresh()
    tools = w.tools
    carried = Q.terms_for({"keys": ["candidates"], "text": "how many candidates"}, "same for last month",
                          other_record_types=tools.other_record_types())
    tools.begin_question("Accounts this year", terms=carried)
    assert tools._terms is carried
    tools.begin_question("Accounts this year")
    assert tools._terms == tools._terms_for("Accounts this year") and "candidates" not in tools._terms.keys
    assert TC.detect("x", list(tools.other_record_types())) == tools._terms_for("x")
    assert set(tools.other_record_types()) >= {"Internal Employee Account", "Internal_Employee_Account"}
    assert not ({"PersonAccount", "Person Account"} & set(tools.other_record_types()))


def test_the_contact_flag_is_never_carried(world):
    """CT01: a list with e-mail addresses the person asked for, then "top 3": the seeded list runs under the question
    of this turn, which asks for no contact detail, so the e-mail column is left out."""
    w = world()
    ask = "list the employees with their email"
    args = {"object": "Recruiter__c", "fields": ["Name", "Email__c"]}
    one = first_turn(w, ask, args, "list_records", "The employees are listed.\n" + SOURCE)
    first_ev = [e for e in w.tools.evidence if e.tool == "list_records"][0]
    assert first_ev.ok and "Email__c" in json.dumps(first_ev.model_result)
    state = one["reply"]["state"]
    assert "contact" not in json.dumps(state).lower()
    two = w.job("top 3", turns_after(ask, one), state=stored(state), gap=0)
    assert two["reply"]["reason"] == "shortcut", two
    ev = [e for e in w.tools.evidence if e.tool == "list_records"][0]
    assert ev.ok and ev.arguments["fields"] == ["Name", "Email__c"] and ev.arguments["limit"] == 3
    assert w.tools._contact_ok is False
    assert "Email__c" not in json.dumps(ev.model_result.get("rows"))


def test_a_new_question_reads_as_the_model_wrote_it_and_runs_with_its_rebuilt_terms(world):
    w = world()
    one = first_turn(w)
    standalone = "How many interviews by status?"
    w.client.resolve = [json.dumps({"kind": "new", "standalone": standalone, "ops": []})]
    w.client.record = [{"name": "group_records", "args": {"object": "Interview__c", "group_by": "Interview_Status__c"}},
                       "The interviews by status are shown.\n" + SOURCE]
    two = w.job("and the interviews?", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    reply = two["reply"]
    assert reply["reason"] == "new" and reply["answer"].startswith(f"Read as: `{standalone}`"), reply
    assert len(w.client.requests["resolve"]) == 1 and w.client.requests["rewrite"] == []
    assert reply["state"]["plan"]["terms"]["text"] == "and the interviews?"   # a new chain: the typed words again
    assert reply["state"]["plan"]["calls"][0]["args"]["object"] == "Interview__c"
    assert w.tools._terms.keys == frozenset()                                  # "interviews" names no contract


def test_a_question_that_stands_alone_runs_as_typed_with_no_read_as_line(world):
    w = world()
    one = first_turn(w)
    two = first_turn(w, "how many interviews are there", {"object": "Interview__c"}, "count_records",
                     "The interviews are counted.\n" + SOURCE)
    assert two["reply"]["reason"] == "new" and "Read as:" not in two["reply"]["answer"]
    assert one["reply"]["reason"] == "new"
    assert w.client.requests["resolve"] == [] and w.client.requests["rewrite"] == []


def test_seeds_count_for_the_draft_check(world):
    """C35: a record agent that answers at once with the figures of the seeded result gets no nudge (with no call in
    the evidence it would get the run-a-query message)."""
    w = world()
    one = first_turn(w)
    w.client.requests = {k: [] for k in Client.ROLES}
    w.client.record = ["2 candidates are Placed and 1 is Active.\n" + SOURCE]
    two = w.job("visualize it", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert two["reply"]["status"] == "answered" and "2 candidates are Placed" in two["reply"]["answer"], two
    assert len(w.client.requests["record"]) == 1                                 # no nudge, no second round


def test_answer_question_starts_from_the_seed_and_counts_it_as_a_tool_call():
    class Tools:
        tools = {"count_records": object()}

        def openai_tools(self):
            return [{"type": "function", "function": {"name": "count_records", "parameters": {}}}]

        def call(self, name, args):
            raise AssertionError("the seeded call must not run again")

    seen: list = []

    class Model:
        cfg = types.SimpleNamespace(base_url="http://127.0.0.1:9/v1")

        def chat(self, messages, **kw):
            seen.append((copy.deepcopy(messages), kw))
            return {"content": "There are 3 accounts.", "tool_calls": []}

    q = types.SimpleNamespace(qid="q1", question="how many accounts")
    payload = {"ok": True, "result": {"count": 3}, "error": None, "chunk_ids": []}
    r = knowledge_agent.answer_question(q, Model(), Tools(), types.SimpleNamespace(max_tool_steps=4), system="S",
                                        require_tools=True, seed=[("count_records", {"object": "Account"}, payload)])
    assert r.error is None and r.answer == "There are 3 accounts." and r.steps == 1
    assert r.tool_calls == [{"name": "count_records", "arguments": {"object": "Account"}, "ok": True, "chunk_ids": [],
                             "latency_s": 0.0, "seeded": True}]
    msgs = seen[0][0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[3] == {"role": "tool", "tool_call_id": "seed_1", "content": json.dumps(payload)}
    # with no seed the messages are as before: system and user only, and the require-tools nudge goes out
    seen.clear()
    r = knowledge_agent.answer_question(q, Model(), Tools(), types.SimpleNamespace(max_tool_steps=4), system="S",
                                        require_tools=True)
    assert [m["role"] for m in seen[0][0]] == ["system", "user"] and len(seen) == 2


# ------------------------------------------------------------------------------------------------ re-runs (§7.4, §10)
def _crafted(plan_calls: list[dict], text: str = FIRST, keys=("candidates", "placement"), show=None) -> dict:
    """A state as the service writes it, with the given calls (made by code here, no answer ran)."""
    return {"v": 1, "made_at": "2026-10-13T10:00:00+05:30", "kb": "", "profile": None, "pending": None,
            "last": {"kind": "records", "reason": "records", "op": None},
            "plan": {"question": text, "terms": {"keys": list(keys), "text": text}, "calls": plan_calls,
                     "show": show or {"chart": False, "type": None, "table": False}, "age": 0}}


TURNS = [{"role": "user", "content": FIRST}, {"role": "assistant", "content": "The candidates are shown."}]


def test_an_unchanged_re_run_that_finds_nothing_now_says_so(world):
    """C7: the stored call found rows before (``nonempty``) and finds none now: a code line, never a plain zero."""
    w = world()
    call = {"role": "main", "tool": "group_records", "nonempty": True,
            "args": {"object": "Interview__c", "group_by": "Interview_Status__c",
                     "filters": [{"field": "Date_of_Interview__c", "op": "gte", "value": "2030-01-01"}]}}
    state = _crafted([call], "interviews by status", keys=())
    out = w.job("visualize it", TURNS, state=state, gap=0)
    reply = out["reply"]
    assert reply["status"] == "answered" and reply["reason"] == "shortcut", out
    assert "now; the data may have changed since the earlier answer." in reply["answer"]
    assert "No records match the earlier query (" in reply["answer"]
    assert reply["state"]["plan"]["calls"][0]["nonempty"] is False


def test_an_unchanged_relative_period_keeps_its_stored_dates_with_a_note(world):
    """D1: "this month" stored in September runs in October with September's dates, and the answer says so."""
    w = world()
    call = {"role": "main", "tool": "group_records", "nonempty": True,
            "args": {"object": "Interview__c", "group_by": "Interview_Status__c", "date_field": "Date_of_Interview__c",
                     "period": "this month"},
            "period": {"phrase": "this month", "from": "2026-09-01", "to": "2026-09-30",
                       "date_field": "Date_of_Interview__c"}}
    out = w.job("visualize it", TURNS, state=_crafted([call], "interviews by status this month", keys=()), gap=0)
    reply = out["reply"]
    assert reply["status"] == "answered", out
    ev = [e for e in w.tools.evidence if e.tool == "group_records"][0]
    assert ev.arguments["period"] == "2026-09-01 to 2026-09-30"
    assert "The dates of the earlier answer are kept: 2026-09-01 to 2026-09-30." in reply["answer"]


def test_a_ranking_that_changed_since_it_was_shown_asks_and_runs_nothing_more(world):
    """D3 and §10: an ordinal drill into a ranking whose fresh keys no longer match the digest shown asks, with no
    model call; the plan is kept for the next turn."""
    w = world()
    set_call = {"object": "Interview__c", "group_by": "Interview_Status__c", "top": 2}
    call = {"role": "main", "tool": "list_records", "nonempty": True,
            "args": {"object": "Interview__c", "limit": 20},
            "keys_from": {"field": "Interview_Status__c", "tool": "group_records", "args": set_call,
                          "digest": Q.keys_digest(["Not", "These"]), "rank": 1}}
    state = _crafted([call], "the first one", keys=())
    assert Q.check(state).reason == "", Q.check(state)
    w.client.requests = {k: [] for k in Client.ROLES}
    out = w.job("visualize it", TURNS, state=state, gap=0)
    reply = out["reply"]
    assert reply["status"] == "answered" and reply["kind"] == "none" and reply["reason"] == "clarify", out
    assert record_session.MEMORY_LINES["stop_rank_changed"].replace("<<N>>", "1") in reply["answer"]
    assert w.client.requests["record"] == []                                    # no model call
    assert [e.tool for e in w.tools.evidence] == ["group_records"]              # only the set ran, fresh
    assert reply["state"]["plan"]["calls"][0]["keys_from"]["rank"] == 1 and reply["state"]["pending"] is None


class _Tools:
    """Record tools stand-in for SeedRun: each call returns the next scripted result and logs one evidence entry."""

    def __init__(self, results: list[dict]) -> None:
        self.results, self.evidence, self.calls = list(results), [], []

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        res = self.results.pop(0)
        self.evidence.append(types.SimpleNamespace(tool=name, ok=True, kind="data", arguments=copy.deepcopy(args),
                                                   model_result=res, statements=[], applied={}))
        return {"ok": True, "result": res, "error": None, "chunk_ids": []}


def _set_run(digest_keys, rank=None):
    plan = {"calls": [{"role": "main", "tool": "list_records", "nonempty": True,
                       "args": {"object": "Interview__c"},
                       "keys_from": {"field": "Recruiter__c", "tool": "group_records",
                                     "args": {"object": "Interview__c", "group_by": "Recruiter__c", "top": 2},
                                     "digest": Q.keys_digest(digest_keys), "rank": rank}}]}
    return record_session.SeedRun(calls=Q.run_calls(plan, TODAY), today=TODAY)


def test_a_record_set_that_changed_filters_on_the_fresh_keys_with_a_line():
    groups = {"groups": [{"group": "Person Kilo", "value": 3}, {"group": "Person Lima", "value": 2}]}
    tools = _Tools([groups, {"total_count": 5, "rows": [{"Name": "I-1"}]}])
    seed = _set_run(["Person Kilo", "Person Mike"])
    pairs = seed.run(tools)
    assert seed.stop == "" and len(pairs) == 2 and len(seed.ran) == 1
    assert tools.calls[1][1]["filters"] == [{"field": "Recruiter__c", "op": "in", "values": ["Person Kilo",
                                                                                              "Person Lima"]}]
    assert seed.lines == [record_session.MEMORY_LINES["set_changed"]]
    same = _set_run(["Person Kilo", "Person Lima"])
    same.run(_Tools([groups, {"total_count": 5}]))
    assert same.lines == [] and same.stop == ""


@pytest.mark.parametrize("groups, rank, stop", [
    ({"groups": [{"group": "Person Kilo", "value": 3}]}, 1, "rank_changed"),
    ({"groups": []}, None, "empty"),
    ({"groups": [{"group": "Person Kilo (#2)", "value": 3}]}, None, "namesakes"),
])
def test_a_record_set_that_cannot_become_a_filter_stops_before_the_main_call(groups, rank, stop):
    tools = _Tools([groups])
    seed = _set_run(["Person Other"], rank)
    seed.run(tools)
    assert seed.stop == stop and seed.ran == [] and len(tools.calls) == 1
    text = seed.stop_text()
    assert text == record_session.MEMORY_LINES[f"stop_{stop}"].replace("<<N>>", str(rank or ""))


# ------------------------------------------------------------------------------------------------ about the answer (§6.7)
def test_a_question_about_the_last_answer_re_runs_the_plan_and_gets_the_answer_as_data(world):
    w = world()
    one = first_turn(w)
    standalone = "Why are more candidates Placed than Active?"
    w.client.resolve = [json.dumps({"kind": "about_answer", "standalone": standalone, "ops": []})]
    w.client.requests = {k: [] for k in Client.ROLES}
    two = w.job(standalone, turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    reply = two["reply"]
    assert reply["reason"] == "about_answer" and reply["status"] == "answered", two
    user = w.client.record_messages(0)[1]["content"]
    note, head = record_session.MEMORY_LINES["earlier_note"], record_session.MEMORY_LINES["earlier_head"]
    assert note in user and head in user
    assert user.split(head + "\n", 1)[1] == json.dumps(one["reply"]["answer"][:2000], ensure_ascii=False)
    assert [m["role"] for m in w.client.record_messages(0)][:4] == ["system", "user", "assistant", "tool"]
    plan, parent = reply["state"]["plan"], one["reply"]["state"]["plan"]
    assert plan["calls"] == parent["calls"] and plan["age"] == 0 and plan["terms"] == parent["terms"]   # C6


# ------------------------------------------------------------------------------------------------ clarify and faults
def test_a_confirm_is_sent_as_the_resolver_wrote_it_and_its_yes_runs_the_patch(world):
    """§6.5 (D10): a model patch at gap 1 asks first. The text goes out unchanged (the next turn finds it, strict),
    the reply is answered, kind none, reason clarify, and the state holds the pending; "1" then runs the patch."""
    w = world()
    one = first_turn(w)
    w.client.resolve = [json.dumps({"kind": "patch", "standalone": "", "ops": [{"set_top": {"n": 1}}]})]
    two = w.job("only the top one", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=1)
    reply = two["reply"]
    assert (reply["status"], reply["kind"], reply["reason"]) == ("answered", "none", "clarify"), two
    pending = reply["state"]["pending"]
    assert pending and [c["label"] for c in pending["choices"]] == [R.LINES["yes_label"], R.LINES["no_label"]]
    assert reply["answer"].startswith(pending["asked"]) and "Read as:" not in reply["answer"]
    assert reply["answer"] == pending["asked"] + "\n\n" + "\n".join(f"{c['n']}. {c['label']}"
                                                                     for c in pending["choices"])
    turns = turns_after(FIRST, one) + turns_after("only the top one", two)
    three = w.job("1", turns, state=stored(reply["state"]), gap=0)
    assert three["reply"]["reason"] == "patch" and three["reply"]["status"] == "answered", three
    ev = [e for e in w.tools.evidence if e.tool == "group_records"][0]
    assert ev.arguments == {**BY_STATUS, "top": 1}


def test_a_failed_check_is_a_clarify_with_the_plan_kept(world):
    w = world()
    one = first_turn(w, "how many candidates", {"object": "Account"}, "count_records",
                     "The candidates are counted.\n" + SOURCE)
    two = w.job("show the top 10", turns_after("how many candidates", one), state=stored(one["reply"]["state"]),
                gap=0)
    reply = two["reply"]
    assert (reply["status"], reply["kind"], reply["reason"]) == ("answered", "none", "clarify"), two
    assert reply["state"]["plan"]["calls"] == one["reply"]["state"]["plan"]["calls"]
    assert "Top N of what?" in reply["answer"]


def test_a_fault_of_the_resolver_falls_back_to_todays_rewrite(world, monkeypatch):
    w = world()
    one = first_turn(w)

    def broken(self, llm, call):
        raise RuntimeError("resolver bug")

    monkeypatch.setattr(R._Job, "run", broken)
    w.client.rewrite = ["How many active or placed candidates are there by status?"]
    w.client.record = [{"name": "group_records", "args": BY_STATUS}, "The candidates by status.\n" + SOURCE]
    two = w.job("and by status?", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert two["reply"]["status"] == "answered" and two["reply"]["reason"] == "records", two
    assert len(w.client.requests["rewrite"]) == 1
    assert two["memory"]["resolver"]["reason"] == "resolver_error"


# ------------------------------------------------------------------------------------------------ shadow and off (C31)
PARITY = [
    # (typed question, earlier turns, the rewrite's reply, the record agent's steps)
    ("how many interviews are there", None, None,
     [{"name": "count_records", "args": {"object": "Interview__c"}}, "The interviews are counted.\n" + SOURCE]),
    ("and by status?", [{"role": "user", "content": "how many interviews are there"},
                        {"role": "assistant", "content": "There are some interviews."}],
     "How many interviews are there by status?",
     [{"name": "group_records", "args": {"object": "Interview__c", "group_by": "Interview_Status__c"}},
      "The interviews by status are shown.\n" + SOURCE]),
    ("visualize it", [{"role": "user", "content": FIRST}, {"role": "assistant", "content": "Shown."}] * 4,
     "Visualize the active or placed candidates by status",
     [{"name": "group_records", "args": BY_STATUS}, "The candidates by status.\n" + SOURCE]),
    ("what is his status?", [{"role": "user", "content": "who owns I-0001?"},
                             {"role": "assistant", "content": "Recruiter A owns it."}], "UNCLEAR", []),
    ("thanks", [{"role": "user", "content": "how many interviews"}, {"role": "assistant", "content": "Some."}],
     None, []),
    ("show the top 10", [{"role": "user", "content": "how many candidates"},
                         {"role": "assistant", "content": "Some candidates."}],
     "Show the top 10 candidates", [{"name": "list_records", "args": {"object": "Account", "limit": 10}},
                                    "The top accounts are listed.\n" + SOURCE]),
]


def _run_parity(world, memory, question, turns, rewrite, steps, state):
    w = world(memory)
    w.client.rewrite = [rewrite] if rewrite else []
    w.client.record = list(steps)
    out = w.job(question, turns, state=state, gap=0, summary="the person counts candidates") \
        if memory is not OFF or state is not None else w.job(question, turns)
    return w, out


@pytest.mark.parametrize("case", range(len(PARITY)))
def test_shadow_answers_are_byte_identical_to_off_and_the_rewrite_gets_the_same_input(world, case):
    question, turns, rewrite, steps = PARITY[case]
    state = _crafted([{"role": "main", "tool": "group_records", "args": BY_STATUS, "nonempty": True}])
    w_off, off = _run_parity(world, OFF, question, turns, rewrite, steps, state)
    w_sh, sh = _run_parity(world, SHADOW, question, turns, rewrite, steps, state)
    six = set(ask_mcp._result("answered"))
    assert set(off["reply"]) == six                                             # off: contract v1, always
    assert {k: sh["reply"][k] for k in six} == off["reply"]                     # the answer, byte for byte
    assert set(sh["reply"]) == six | {"contract", "state", "plan_line", "reason"}
    for role in ("rewrite", "record", "route", "names", "lean", "knowledge", "merge"):
        assert w_sh.client.requests[role] == w_off.client.requests[role], role
    assert w_sh.client.requests["resolve"] == []                                # shadow makes no model call
    if turns:   # the rewrite read the last 4 turns only (C31)
        for req in w_off.client.requests["rewrite"]:
            assert len(json.dumps(req["messages"])) < len(json.dumps(turns)) + 20_000
    assert sh["memory"]["mode"] == "shadow" and codes_only(sh["memory"])   # no part of a state, a summary or a turn


def test_the_rewrite_reads_the_last_four_turns_in_shadow_and_off(world):
    turns = [{"role": "user", "content": f"how many interviews in month {n}"} if i % 2 == 0 else
             {"role": "assistant", "content": f"Answer {n}."} for n in range(6) for i in range(2)]
    for memory in (OFF, SHADOW):
        w = world(memory)
        w.client.rewrite = ["UNCLEAR"]
        w.job("and by status?", turns, state=None, gap=0)
        w2 = world(memory)
        w2.client.rewrite = ["UNCLEAR"]
        w2.job("and by status?", turns[-4:], state=None, gap=0)
        assert w.client.requests["rewrite"] == w2.client.requests["rewrite"]
        text = json.dumps(w.client.requests["rewrite"][0]["messages"])
        assert "month 3" not in text and "month 4" in text and "month 5" in text


def test_shadow_returns_the_next_state_built_from_what_ran(world):
    w = world(SHADOW)
    w.client.record = [{"name": "group_records", "args": BY_STATUS}, "The candidates by status.\n" + SOURCE]
    out = w.job(FIRST, None, state=None, gap=0)
    reply = out["reply"]
    assert reply["contract"] == 2 and reply["reason"] == "records"
    assert reply["state"]["plan"]["calls"][0]["args"] == BY_STATUS and reply["plan_line"]
    assert out["memory"]["resolver"]["action"] == "new" and out["memory"]["resolver"]["model"] is False


def test_with_off_a_v2_call_is_answered_as_v1(world):
    w = world(OFF)
    w.client.record = [{"name": "group_records", "args": BY_STATUS}, "The candidates by status.\n" + SOURCE]
    out = w.job(FIRST, None, state=None, gap=0)
    assert set(out["reply"]) == set(ask_mcp._result("answered")) and "memory" not in out and "v2" not in out


def test_the_ask_log_holds_no_state_summary_or_turns(world):
    w = world()
    one = first_turn(w)
    turns = turns_after(FIRST, one) + [{"role": "user", "content": "TURN-MARK-ZZ about nothing"},
                                       {"role": "assistant", "content": "TURN-MARK-ZZ again"}]
    state = stored(one["reply"]["state"])
    state["plan"]["question"] = "STATE-MARK-ZZ"
    w.service._note({"event": "probe"})
    out = w.job("visualize it", turns, state=state, gap=0, summary="SUMMARY-MARK-ZZ")
    w.service._note(ask_mcp._ask_line({"event": "ask", "question": "visualize it"}, out, 0.1))
    text = "".join(p.read_text() for p in (Path(w.log.path)).glob("ask_log-*.jsonl"))
    assert '"memory"' in text and "STATE-MARK-ZZ" not in text and "SUMMARY-MARK-ZZ" not in text
    assert "TURN-MARK-ZZ" not in text
    entry = json.loads([ln for ln in text.splitlines() if '"memory"' in ln][-1])["memory"]
    assert entry["state_bytes"] > 0 and entry["plan_calls"] == 1 and entry["gap"] == 0 and entry["age"] == 0
    assert entry["resolver"]["ops"] == ["set_show"] and entry["shortcut"] is True


# ------------------------------------------------------------------------------------------------ the page (§6.1)
def test_the_page_with_memory_on_runs_a_complete_follow_up_as_typed_with_no_model_call_to_read_it(world):
    w = world()
    w.pipe.memory = ON
    w.client.record = [{"name": "count_records", "args": {"object": "Interview__c"}},
                       "The interviews are counted.\n" + SOURCE]
    out = w.pipe.answer_followup("how many interviews are there in total", TURNS)
    assert out["kind"] in ("merged", "records") and "Read as:" not in out["answer"]
    assert w.client.requests["rewrite"] == [] and w.client.requests["resolve"] == []
    assert out["log"]["rewrite"]["memory"]["action"] == "new"
    w.pipe.memory = None                                                          # the page as before: the rewrite
    w.client.rewrite = ["UNCLEAR"]
    assert w.pipe.answer_followup("and his?", TURNS)["answer"] == pipeline_mod.UNCLEAR_LINE
    assert len(w.client.requests["rewrite"]) == 1


# ------------------------------------------------------------------------------------------------ switches (§15.1)
def test_the_memory_switches_have_the_design_defaults():
    m = sfa_config.memory_settings({})
    assert (m.mode, m.shortcuts, m.patch_knowledge, m.max_calls, m.max_age, m.tokens, m.problem) == \
        ("shadow", True, False, 3, 2, 12000, "")
    assert m == sfa_config.MemoryConfig() and m.shadow and not m.on and not m.off
    assert sfa_config.ASK_LOG_DAYS_DEFAULT == 90 and sfa_config.ASK_LOG_DAYS_ENV_VAR == "SFA_ASK_LOG_DAYS"
    import ask_log
    assert ask_log.DAYS_DEFAULT == sfa_config.ASK_LOG_DAYS_DEFAULT and ask_log.DAYS_ENV_VAR == "SFA_ASK_LOG_DAYS"


@pytest.mark.parametrize("env, field, value, problem", [
    ({"SFA_MEMORY": "ON"}, "mode", "on", ""),
    ({"SFA_MEMORY": "maybe"}, "mode", "off", "SFA_MEMORY must be off, shadow or on"),
    ({"SFA_MEMORY_SHORTCUTS": "off"}, "shortcuts", False, ""),
    ({"SFA_MEMORY_SHORTCUTS": "no"}, "shortcuts", True, "SFA_MEMORY_SHORTCUTS must be off or on"),
    ({"SFA_MEMORY_PATCH_KNOWLEDGE": "on"}, "patch_knowledge", True, ""),
    ({"SFA_MEMORY_MAX_CALLS": "2"}, "max_calls", 2, ""),
    ({"SFA_MEMORY_MAX_CALLS": "4"}, "max_calls", 3, "SFA_MEMORY_MAX_CALLS must be a whole number from 1 to 3"),
    ({"SFA_MEMORY_MAX_AGE": "0"}, "max_age", 0, ""),
    ({"SFA_MEMORY_MAX_AGE": "-1"}, "max_age", 2, "SFA_MEMORY_MAX_AGE must be a whole number from 0 to 6"),
    ({"SFA_MEMORY_TOKENS": "8000"}, "tokens", 8000, ""),
    ({"SFA_MEMORY_TOKENS": "lots"}, "tokens", 12000, "SFA_MEMORY_TOKENS must be a whole number"),
])
def test_an_invalid_memory_switch_never_stops_the_service(env, field, value, problem):
    m = sfa_config.memory_settings(env)
    assert getattr(m, field) == value
    assert m.problem.startswith(problem) if problem else m.problem == ""
    for raw in env.values():
        assert raw not in m.problem or raw.lower() in ("on", "off")                # the value is never repeated


def test_a_prompt_cap_below_the_resolvers_smallest_prompt_is_refused():
    """N2 (stage-2 review): the floor of SFA_MEMORY_TOKENS is the system prompt and the smallest user message with a
    full plan part and a question at the service's limit, so no accepted cap leaves every follow-up over the cap."""
    system = sfa_config.memory_prompt_tokens()
    assert system == R._SYSTEM_TOKENS == R.estimate_tokens(R.FOLLOWUP_RESOLVE_SYSTEM) and 1000 < system < 12000
    assert sfa_config._estimate_tokens("abc déf") == R.estimate_tokens("abc déf")
    floor = sfa_config.memory_tokens_floor()
    assert sfa_config._RESOLVE_PLAN_TOKENS == R.PLAN_TOKENS
    assert sfa_config._RESOLVE_QUESTION_CHARS == ask_mcp.MAX_QUESTION
    assert system + R.PLAN_TOKENS < floor < 12000
    for low in (system, floor - 1):
        got = sfa_config.memory_settings({"SFA_MEMORY_TOKENS": str(low)})
        assert got.tokens == 12000 and "SFA_MEMORY_TOKENS must be a whole number from" in got.problem
    assert sfa_config.memory_settings({"SFA_MEMORY_TOKENS": str(floor)}).tokens == floor


def test_at_the_lowest_prompt_cap_a_full_plan_and_question_still_reach_the_model():
    """N2: at the floor, the resolver's prompt with a plan part at its cap, the line that it answered an earlier
    question, a pending choice, a summary, one pair of turns and a question of 1,000 characters is not over the cap
    (the turns, the summary and the choices are cut first)."""
    floor = sfa_config.memory_tokens_floor()
    plan = {"calls": [{"role": "main", "tool": "group_records", "args": {"object": "Interview__c",
                                                                         "group_by": "Recruiter__c"}}]}
    for question in ("x" * ask_mcp.MAX_QUESTION, "and by month?"):
        _m, info = R.build_messages(question, [{"role": "user", "content": "interviews " * 300},
                                               {"role": "assistant", "content": "There are 3. " * 300}],
                                    plan=plan, plan_line="number of Interviews per Recruiter " * 200, earlier=True,
                                    summary="the person asked about interviews " * 100,
                                    pending={"choices": [{"n": 1, "label": "Yes", "question": "q " * 300}]},
                                    max_tokens=floor)
        assert info["over"] is False and info["tokens"] <= floor, info


def test_settings_carry_the_memory_switches(monkeypatch):
    monkeypatch.setenv("SFA_MEMORY", "on")
    monkeypatch.setenv("SFA_MEMORY_MAX_AGE", "1")
    s = sfa_config.load_settings(records=False)
    assert s.memory.on and s.memory.max_age == 1 and s.safe_dict()["memory"]["mode"] == "on"


def test_the_service_reads_the_switches_once_when_none_are_given(monkeypatch, tmp_path):
    monkeypatch.setenv("SFA_MEMORY", "off")

    class Pipe:
        knowledge_dir = None

        def close(self):
            pass

    svc = ask_mcp.AskService(Pipe(), types.SimpleNamespace(state=lambda: "ok"),
                             ask_mcp.RotatingAskLog(str(tmp_path / "l")))
    try:
        assert svc.memory.off and svc._request({"question": "x", "contract": 2}) is None
        monkeypatch.setenv("SFA_MEMORY", "on")
        assert svc.memory.off                                                     # read once, at start
    finally:
        svc.close()


def test_the_plan_line_words_are_the_prompt_file():
    from sf_assistant import prompts

    assert Q.PLAN_LINE_WORDS == prompts.text("plan_line_words") and Q._WORDS["count"] == "number of <<OBJ>>"
    assert record_session.MEMORY_EXEC_TEXT == prompts.text("memory_exec")
    assert set(record_session.MEMORY_LINES) == {"kept_dates", "no_rows_now", "set_changed", "earlier_note",
                                                "earlier_head", *(f"stop_{s}" for s in record_session.SEED_STOPS)}


# ------------------------------------------------------------------------------------------------ the reason codes
@pytest.mark.parametrize("action, reason, want", [
    ("patch", "clarify_choice", "patch"), ("new", "clarify_choice", "new"), ("reply", "ask_full", "unclear_rewrite"),
    ("reply", "unclear_rewrite", "unclear_rewrite"), ("reply", "clarify", "clarify"), ("patch", "shortcut", "shortcut"),
    ("reply", "small_talk", "small_talk"), ("about_answer", "about_answer", "about_answer"),
    ("reply", "resolver_error", "failed"), ("reply", "call_failed", "failed"), ("reply", "not_local", "failed"),
])
def test_every_resolver_reason_maps_onto_the_closed_set(action, reason, want):
    got = ask_mcp._resolver_reason(types.SimpleNamespace(action=action, reason=reason))
    assert got == want and got in ask_mcp.REASONS


def test_the_resolvers_reasons_are_all_mapped():
    for reason in R.REASONS:
        for action in R.ACTIONS:
            assert ask_mcp._resolver_reason(types.SimpleNamespace(action=action, reason=reason)) in ask_mcp.REASONS


def test_a_prompt_over_the_cap_is_never_sent_and_the_plan_asks(world):
    """C32 / F4: with SFA_MEMORY_TOKENS at the floor, a follow-up that needs the model makes no call; with a plan it is
    a clarify (reason clarify), and the log says over_cap."""
    w = world(dataclasses.replace(ON, tokens=sfa_config.memory_prompt_tokens()))
    one = first_turn(w)
    two = w.job("only for the active ones in Pune", turns_after(FIRST, one), state=stored(one["reply"]["state"]),
                gap=0)
    assert w.client.requests["resolve"] == []
    assert (two["reply"]["status"], two["reply"]["reason"]) == ("answered", "clarify"), two
    assert two["memory"]["resolver"]["detail"] == "over_cap" and two["memory"]["resolver"]["over"] is True


def test_with_patch_knowledge_on_the_seeded_run_also_gets_the_knowledge_step(world):
    """D4's switch (off by default): on, a patched follow-up runs the knowledge step next to the seeded record agent,
    and the seeds still run whatever the route call says."""
    w = world(dataclasses.replace(ON, patch_knowledge=True))
    one = first_turn(w)
    w.client.requests = {k: [] for k in Client.ROLES}
    two = w.job("visualize it", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert two["reply"]["status"] == "answered" and two["reply"]["reason"] == "shortcut", two
    assert w.client.requests["route"] and (w.client.requests["lean"] or w.client.requests["knowledge"])
    assert [m["role"] for m in w.client.record_messages(0)][:4] == ["system", "user", "assistant", "tool"]
    off = world()
    one = first_turn(off)
    off.client.requests = {k: [] for k in Client.ROLES}
    off.job("visualize it", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert off.client.requests["route"] == off.client.requests["lean"] == off.client.requests["knowledge"] == []


def test_a_clarifying_text_loses_only_the_force_of_a_link():
    """The resolver's text goes out as written (strictness), but a Markdown link in it is shown, never followed."""
    plain = "Do you mean: number of Accounts per Candidate Status?\n\n1. Yes\n2. No, I'll ask in full"
    assert ask_mcp._no_links(plain) == plain
    planted = "Which do you mean?\n\n1. [see here](https://example.invalid/x)\n2. No, I'll ask in full"
    assert "](" not in ask_mcp._no_links(planted)


def test_the_typed_display_words_merge_into_a_model_patch(world):
    """§7.2: a model patch that changes only the period still draws the chart the person typed, and a typed chart
    refusal turns a stored chart off (shortcuts off, so the model reads these turns)."""
    w = world(dataclasses.replace(ON, shortcuts=False))
    call = {"role": "main", "tool": "group_records", "nonempty": True,
            "args": {"object": "Interview__c", "group_by": "Interview_Status__c", "date_field": "Date_of_Interview__c",
                     "period": "this month"},
            "period": {"phrase": "this month", "from": "2026-10-01", "to": "2026-10-31",
                       "date_field": "Date_of_Interview__c"}}
    state = _crafted([call], "interviews by status this month", keys=())
    w.client.resolve = [json.dumps({"kind": "patch", "standalone": "",
                                    "ops": [{"set_period": {"phrase": "last month"}}]})]
    out = w.job("how about the previous month, as a chart", TURNS, state=state, gap=0)
    assert out["reply"]["reason"] == "patch" and out["reply"]["charts"], out
    assert out["reply"]["state"]["plan"]["show"]["chart"] is True
    shown = dict(state, plan=dict(state["plan"], show={"chart": True, "type": None, "table": False}))
    w.client.resolve = [json.dumps({"kind": "patch", "standalone": "",
                                    "ops": [{"set_period": {"phrase": "last month"}}]})]
    out = w.job("how about the previous month, without the chart", TURNS, state=shown, gap=0)
    assert out["reply"]["reason"] == "patch" and out["reply"]["charts"] == [], out
    assert out["reply"]["state"]["plan"]["show"]["chart"] is False


# ------------------------------------------------------------------------------------------------ stage-2 review fixes
@pytest.mark.parametrize("memory", [OFF, SHADOW, ON], ids=["off", "shadow", "on"])
def test_a_fault_after_the_question_was_read_keeps_the_question_used_in_the_log(world, monkeypatch, memory):
    """L1: a job whose answer step raises after the rewrite (or the resolver) read the question logs the question it
    used, as the job did before it moved into module functions: a v1 call in every mode, and a v2 call with on."""
    w = world(memory)
    w.pipe.ensure_fresh()
    monkeypatch.setattr(followup, "rewrite",
                        lambda q, turns, llm: followup.Rewrite(followup.OK, question="How many interviews in 2026?"))

    def broken(*_a, **_k):
        raise RuntimeError("answer bug")

    monkeypatch.setattr(w.pipe, "answer", broken)
    turns = [{"role": "user", "content": "how many interviews"}, {"role": "assistant", "content": "There are 3."}]
    out = w.job("and in 2026?", turns)                                                 # a v1 call
    line = ask_mcp._ask_line({"event": "ask"}, out, 0.1)
    assert out["reason"] == "exception" and line["question_used"] == "How many interviews in 2026?"
    if memory.on:   # a v2 call: the resolver's question
        w.client.resolve = [json.dumps({"kind": "new", "standalone": "How many interviews are there in 2026?",
                                        "ops": []})]
        monkeypatch.setattr(R._Job, "_read", lambda self, standalone: followup.Rewrite(followup.OK,
                                                                                         question=standalone))
        out = w.job("and in 2026?", turns, state=None, gap=0)
        assert out["reason"] == "exception" and out["question_used"] == "How many interviews are there in 2026?"
        assert out["reply"]["reason"] == "failed" and out["reply"]["state"] is None


@pytest.mark.parametrize("memory", [ON, SHADOW], ids=["on", "shadow"])
def test_a_small_talk_reply_keeps_the_plan_line_of_the_answer_before(world, memory):
    """L2: "thanks" is answered before the resolver reads the record session, so the plan line was written with no
    catalog words; it is read first now, and the same plan reads the same."""
    w = world(memory)
    one = first_turn(w)
    two = w.job("thanks", turns_after(FIRST, one), state=stored(one["reply"]["state"]), gap=0)
    assert two["reply"]["reason"] == "small_talk" and w.client.requests["resolve"] == []
    assert two["reply"]["state"]["plan"]["calls"] == one["reply"]["state"]["plan"]["calls"]
    assert two["reply"]["plan_line"] == one["reply"]["plan_line"] != ""


def test_a_clarify_with_the_shape_of_a_link_stays_strict(world):
    """N1: a confirm whose plan line holds a typed value shaped like a Markdown link ("[A](b)") is sent with the
    link's force taken out; its pending question is stored as sent, so the next turn finds it (strict) and "1" runs
    the stored choice."""
    name = "[A](b) Co"   # the fixture's client account, renamed
    tables = [(t, cols, [tuple(name if v == "Client Co" else v for v in r) for r in rows]) if t == "Account"
              else (t, cols, rows) for t, cols, rows in TF.TERMS_TABLES]
    w = world(tables=tables)
    question = f"how many accounts named {name} by status"
    args = {"object": "Account", "group_by": "Candidate_Status__c",
            "filters": [{"field": "Name", "op": "eq", "value": name}]}
    one = first_turn(w, question, args, reply="The accounts are shown.\n" + SOURCE)
    assert one["reply"]["state"]["plan"]["calls"][0]["args"] == args
    w.client.resolve = [json.dumps({"kind": "patch", "standalone": "", "ops": [{"set_top": {"n": 1}}]})]
    two = w.job("only the top one", turns_after(question, one), state=stored(one["reply"]["state"]), gap=1)
    reply = two["reply"]
    assert reply["reason"] == "clarify" and "]\\(" in reply["answer"], two
    pending = reply["state"]["pending"]
    assert reply["answer"].startswith(pending["asked"]) and "]\\(" in pending["asked"]
    assert [c["label"] for c in pending["choices"]] == [R.LINES["yes_label"], R.LINES["no_label"]]
    turns = turns_after(question, one) + turns_after("only the top one", two)
    three = w.job("1", turns, state=stored(reply["state"]), gap=0)
    assert three["reply"]["reason"] == "patch" and three["reply"]["status"] == "answered", three
    ev = [e for e in w.tools.evidence if e.tool == "group_records"][0]
    assert ev.arguments == {**args, "top": 1}


# ------------------------------------------------------------------------ development B: say what is missing
B_TURNS = [{"role": "user", "content": "list out all the analysts of the audit team"},
           {"role": "assistant", "content": "The audit team has 4 analysts: Echo One and Delta Person among them."}]
B_QUESTION = "how many of them have less tickets in last years"
B_REWRITE = "UNCLEAR missing=number,period about=the analysts of the audit team"
B_TEXT = ('Do you mean the analysts of the audit team? Fewer than how many tickets, and which years do you mean by '
          '"last years"?\n\n' + pipeline_mod.UNCLEAR_LINE)


def test_b_an_unclear_follow_up_says_what_is_missing_alike_in_off_and_shadow(world):
    replies = {}
    for name, memory, v2 in (("off", OFF, False), ("shadow", SHADOW, True), ("shadow_v1", SHADOW, False),
                             ("on_v1", ON, False)):
        w = world(memory)
        w.client.rewrite = [B_REWRITE]
        out = w.job(B_QUESTION, B_TURNS, state=None, gap=0) if v2 else w.job(B_QUESTION, B_TURNS)
        replies[name] = out["reply"]
        assert out["reason"] == "rewrite_unclear" and w.client.requests["record"] == []
        assert w.client.requests["resolve"] == []
    six = set(ask_mcp._result("unclear"))
    assert replies["off"] == {"status": "unclear", "answer": B_TEXT, "kind": "none", "data_ts": None, "objects": [],
                              "charts": []}
    assert {k: replies["shadow"][k] for k in six} == replies["off"] == replies["shadow_v1"] == replies["on_v1"]
    assert replies["shadow"]["reason"] == "unclear_rewrite"
    # the bridge's stop rule finds the fixed line in the answer (sfk_bridge.follows_a_stop_line reads it with `in`)
    assert ask_mcp.UNCLEAR_LINE in replies["off"]["answer"]


def test_b_with_no_code_off_and_shadow_say_todays_line(world):
    for memory, v2 in ((OFF, False), (SHADOW, True)):
        w = world(memory)
        w.client.rewrite = ["UNCLEAR"]
        out = w.job(B_QUESTION, B_TURNS, state=None, gap=0) if v2 else w.job(B_QUESTION, B_TURNS)
        assert out["reply"]["status"] == "unclear" and out["reply"]["answer"] == ask_mcp.UNCLEAR_LINE


def test_b_with_memory_on_the_resolvers_unclear_line_uses_the_same_parts(world):
    outs = {}
    for name, reply in (("plain", '{"kind": "unclear", "standalone": "", "ops": []}'),
                        ("coded", '{"kind": "unclear", "standalone": "", "ops": [], "missing": ["number", "period"]}')):
        w = world(ON)
        w.client.resolve = [reply]
        outs[name] = w.job(B_QUESTION, B_TURNS, state=None, gap=0)
        assert len(w.client.requests["resolve"]) == 1 and w.client.requests["record"] == []
    plain, coded = outs["plain"], outs["coded"]
    assert plain["reply"]["answer"] == ask_mcp.UNCLEAR_LINE
    assert coded["reply"]["answer"] == ('Fewer than how many tickets, and which years do you mean by "last years"?'
                                        '\n\n' + ask_mcp.UNCLEAR_LINE)
    # the same decision: status, reason codes and the next state's kind
    assert {k: v for k, v in coded["reply"].items() if k not in ("answer", "state")} == {
        k: v for k, v in plain["reply"].items() if k not in ("answer", "state")}
    assert coded["reason"] == plain["reason"] and coded["reply"]["state"]["last"] == plain["reply"]["state"]["last"]


def test_b_the_page_says_what_is_missing_too(world):
    w = world()
    w.pipe.memory = None                                                          # the page: the rewrite
    w.client.rewrite = [B_REWRITE]
    out = w.pipe.answer_followup(B_QUESTION, B_TURNS)
    assert out["answer"] == B_TEXT and out["kind"] == "none"
    assert out["log"]["rewrite"]["missing"] == "number,period" and out["log"]["rewrite"]["offer"] is True
    w.client.rewrite = ["UNCLEAR"]
    out = w.pipe.answer_followup(B_QUESTION, B_TURNS)
    assert out["answer"] == pipeline_mod.UNCLEAR_LINE and "missing" not in out["log"]["rewrite"]


# ------------------------------------------------- pre-release fixes of 2026-11-28: the answer to a coded clarify
C_TURNS = B_TURNS + [{"role": "user", "content": B_QUESTION}, {"role": "assistant", "content": B_TEXT}]
C_WHOLE = "how many of the analysts of the audit team have less than 3 tickets in 2024 and 2025"
C_REST = 'Do you mean the analysts of the audit team? Which years do you mean by "last years"?\n\n'


def test_c_the_answer_to_a_coded_clarify_runs_the_whole_question_in_every_mode(world):
    """RO-C step 3: "fewer than 3, in 2024 and 2025" after the coded clarify runs the whole question (code rebuilds
    it, no rewrite call) in off and shadow, and with memory on when the resolver reads it so too."""
    for name, memory, v2 in (("off", OFF, False), ("shadow", SHADOW, True), ("on", ON, True)):
        w = world(memory)
        w.client.resolve = [json.dumps({"kind": "new", "standalone": C_WHOLE, "ops": []})]
        answer = "fewer than 3, in 2024 and 2025"
        out = w.job(answer, C_TURNS, state=None, gap=0) if v2 else w.job(answer, C_TURNS)
        reply = out["reply"]
        assert reply["status"] == "answered" and C_WHOLE in reply["answer"], (name, out)
        assert w.client.requests["rewrite"] == [] and w.client.requests["record"], name
        assert C_WHOLE in json.dumps(w.client.record_messages(0)), name


def test_c_a_partial_answer_asks_for_the_rest_on_the_service_and_the_page(world):
    for memory, v2 in ((OFF, False), (SHADOW, True)):
        w = world(memory)
        out = w.job("fewer than 3", C_TURNS, state=None, gap=0) if v2 else w.job("fewer than 3", C_TURNS)
        assert out["reply"]["status"] == "unclear" and out["reply"]["answer"] == C_REST + ask_mcp.UNCLEAR_LINE
        assert w.client.requests["rewrite"] == [] and w.client.requests["record"] == []
    w = world()
    w.pipe.memory = None                                                          # the page: the rewrite
    out = w.pipe.answer_followup("fewer than 3", C_TURNS)
    assert out["answer"] == C_REST + pipeline_mod.UNCLEAR_LINE and out["log"]["rewrite"]["missing"] == "period"
    then = C_TURNS + [{"role": "user", "content": "fewer than 3"}, {"role": "assistant", "content": out["answer"]}]
    out = w.pipe.answer_followup("in 2024 and 2025", then)
    assert C_WHOLE in out["answer"] and out["log"]["rewrite"]["reason"] == "clarify_answer"
