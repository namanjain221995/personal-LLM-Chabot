"""The chain runner of Salesforce conversation memory (design v2 of 2026-11-25 §16.1; lane S4): multi-step chats run
model-free through the real ask service job, with the state carried from step to step as the platform carries it.

A chain is one item of a JSON file in ``tests/chains/``. Each step is one question of one chat. The runner sends it as
the platform's bridge does (contract 2: the question, the last 12 turns of the active path, the parent ``state``, the
``gap`` and the summary), lets the real service answer it (``web/ask_mcp.py`` ``AskService._job``: the resolver, the
pipeline, ``sf_assistant.cli.ask``, the record agent and the record tools on the memory fixture of
``tests/warehouse_fixture.py``), and carries the reply's ``state`` to the next step **through a JSON round trip that
reorders the keys**, as jsonb does. A reply with no state (failed, no answer, busy) stores no row: the next step gets
the last state with ``gap`` one higher, as the bridge's walk-back gives it.

Only the model is scripted (:class:`Client`, told apart by the system prompt): the resolver's reply, the rewrite's
reply and the record agent's steps, per step. No model of any kind is called.

**Chain file.** ``{"chains": [chain, ...]}``; a chain is::

    {"id": "C01", "title": "...", "design": "§16.1 C01 ...", "world": "<name>" (the steps' default world),
     "worlds": {"<name>": {"today": "YYYY-MM-DD", "drop_fields": ["Object.Field"],   # gone from the copy and knowledge
                           "set": [{"table": "Interview__c", "name": "I-0007", "field": "Region__c", "value": null}]}},
     "steps": [step, ...]}

and a step::

    {"q": "<typed question>",
     "world": "<name>",                      # optional: the world of this step (default: the memory fixture as it is)
     "bridge": "fallback" | "transparent",   # optional: the platform answered this turn (no call), "answer" is its
                                             #   text; fallback counts as a gap, transparent does not (C3)
     "gap": 0,                               # optional: the gap sent (default: what the bridge's walk-back gives)
     "summary": "...",                       # optional: the Salesforce summary sent
     "resolve": {...} | [{...}] | "<raw>",   # the resolver model's reply (or replies), JSON-encoded by the runner
     "rewrite": "<text>",                    # the rewrite's reply (the v1 path: off and shadow; on after a fault)
     "route": ["NO_RECORDS"],                # optional: the route call's replies (default RECORDS)
     "record": [{"name": ..., "args": ...} | "<text>", ...],   # the record agent's steps, in order
     "v1": {"rewrite": ..., "record": [...]},                   # optional: other scripts for the off / shadow runs
     "expect": {...}}                                           # with SFA_MEMORY=on (below)

**What a step can expect** (with ``SFA_MEMORY=on``, :func:`step_problems`; every key optional):

- the reply: ``status``, ``kind``, ``reason``; ``detail`` (the resolver's rule, from the ask log's entry);
- the model calls of the step: ``model`` (the resolver), ``rewrite``, ``record`` (the record agent), each true or false;
- ``ran``: the record tool calls of the step, in order, each with ``tool``, ``ok``, ``args`` (exact: the model form
  sent), ``args_has`` (a subset), ``term_filters`` (exact), ``enforced_has`` (a subset of what ran), ``result_has`` (a
  subset of the result), ``result_holds`` and ``result_not`` (texts the result holds or never holds); ``ran_none``: no
  record tool call;
- ``contracts``: ``candidates``, ``placement``, ``support``: every data call of the step holds it
  (:func:`_contract_problem`);
- the next state: ``plan`` (``null``, or ``tool`` (of the main call), ``calls`` (exact: the ``tool`` and ``args`` of
  each call, in the order stored), ``show_chart``, ``show``, ``age``, ``keys`` (the terms keys), ``terms_text``, and of
  the main call (the call with the role ``main``) ``period``, ``nonempty`` and ``keys_from`` (subsets)); ``state``
  (``null``: no state); ``pending`` (``null``, or the labels of its choices);
- the text: ``answer`` (exact), ``answer_has``, ``answer_not``, ``answer_starts``, ``read_as`` (``"plan_line"``: the
  answer starts with the Read-as line of the reply's plan line; ``null``: no Read-as line), ``plan_line_has``,
  ``charts`` (how many), ``clarify`` (answered, kind none, reason clarify, and the answer is the pending's question and
  its numbered choices exactly as stored, so the next turn finds it: strictness);
- what the bridge sent: ``sent_gap``, ``sent_turns`` (how many turns).

**Checked on every step of a run with ``SFA_MEMORY=on``:** the reply has the ten keys of contract v2 and a reason of
its closed set; a patch or a shortcut runs records only (no route call, no knowledge step:
``SFA_MEMORY_PATCH_KNOWLEDGE`` off, D4); the ask log's ``memory`` entry holds fixed codes and numbers only; the state
holds no record Id and no e-mail address, and is at most 32 KB.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHAINS_DIR = Path(__file__).resolve().parent / "chains"
for _p in (ROOT / "web", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ask_mcp  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import warehouse_fixture as F  # noqa: E402

from sf_assistant import config as sfa_config  # noqa: E402
from sf_assistant import pipeline as core  # noqa: E402
from sf_assistant.agent import followup, record_prompts  # noqa: E402
from sf_assistant.agent import query_state as Q  # noqa: E402
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
#: The turns the bridge sends: 6 pairs (contract v2).
MAX_TURNS = ask_mcp.MAX_TURNS
#: The six result keys of contract v1, and the four contract v2 adds.
V1_KEYS = frozenset(ask_mcp._result("answered"))
V2_KEYS = V1_KEYS | {"contract", "state", "plan_line", "reason"}
ROLES = ("rewrite", "resolve", "route", "names", "lean", "knowledge", "record", "merge")
#: The step keys and expectation keys a chain may use (a typo in a chain file fails the file check).
STEP_KEYS = frozenset({"q", "world", "bridge", "answer", "gap", "summary", "resolve", "rewrite", "record", "route",
                       "v1", "expect", "note"})
EXPECT_KEYS = frozenset({"status", "kind", "reason", "model", "rewrite", "record", "ran", "ran_none", "contracts",
                         "plan", "state", "pending", "answer", "answer_has", "answer_not", "answer_starts", "read_as",
                         "plan_line_has", "charts", "clarify", "detail", "sent_gap", "sent_turns"})
RAN_KEYS = frozenset({"tool", "ok", "args", "args_has", "term_filters", "enforced_has", "result_has", "result_not",
                      "result_holds"})
PLAN_KEYS = frozenset({"tool", "calls", "show_chart", "show", "age", "keys", "terms_text", "period", "nonempty",
                       "keys_from"})
CONTRACTS = ("candidates", "placement", "support")


# ------------------------------------------------------------------------------------------------ the scripted model
def completion(content: Any = None, calls: Any = (), finish: str | None = None) -> Any:
    tool_calls = [types.SimpleNamespace(id=f"c{i}", function=types.SimpleNamespace(name=n, arguments=json.dumps(a)))
                  for i, (n, a) in enumerate(calls)]
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls or None)
    choice = types.SimpleNamespace(message=message, finish_reason=finish or ("tool_calls" if calls else "stop"))
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=1))


class Client:
    """A fake OpenAI client, scripted per role (told apart by the system prompt), as ``tests/test_memory_exec.py``
    scripts it. ``record``: the record agent's steps, a dict (one tool call ``{"name", "args"}``) or a str (its reply),
    used in order; when none is left it replies ``"The figures are shown.\\n" + SOURCE``. ``resolve`` and
    ``rewrite``: the replies of the resolver and the rewrite, in order (none left: ``{}`` and ``UNCLEAR``); ``route``:
    the route call's replies (none left: ``RECORDS``). ``requests[role]`` keeps every request."""

    def __init__(self) -> None:
        self.record: list = []
        self.resolve: list[str] = []
        self.rewrite: list[str] = []
        self.route: list[str] = []
        self.requests: dict[str, list] = {k: [] for k in ROLES}
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self._lock = threading.Lock()

    @staticmethod
    def role(system: str) -> str:
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

    def _create(self, **kw: Any) -> Any:
        role = self.role(str(kw["messages"][0].get("content") or ""))
        with self._lock:
            self.requests[role].append(copy.deepcopy(kw))
        if role == "rewrite":
            return completion(self.rewrite.pop(0) if self.rewrite else "UNCLEAR")
        if role == "resolve":
            return completion(self.resolve.pop(0) if self.resolve else "{}")
        if role == "route":
            return completion(self.route.pop(0) if self.route else "RECORDS")
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

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.requests.items()}


# ------------------------------------------------------------------------------------------------ the world
def _variant(spec: dict) -> tuple[list, list, dt.date]:
    """The tables, knowledge and business date of a world spec (see the module text)."""
    tables, knowledge = F.memory_tables(), F.memory_knowledge()
    for item in spec.get("set") or ():
        for i, (name, cols, rows) in enumerate(tables):
            if name != item["table"]:
                continue
            keys = list(cols)
            at, col = keys.index("Name"), keys.index(item["field"])
            hit = [r for r in rows if r[at] == item["name"]]
            assert hit, f"no {item['table']} row named {item['name']}"
            tables[i] = (name, cols, [tuple(item["value"] if k == col and r[at] == item["name"] else v
                                            for k, v in enumerate(r)) for r in rows])
    for path in spec.get("drop_fields") or ():   # a field a release removed: gone from the copy and the knowledge
        obj, fld = path.split(".", 1)
        doc = next(x for x in knowledge if x["object"]["api_name"] == obj)
        before = len(doc["fields"])
        doc["fields"] = [f for f in doc["fields"] if f["api_name"] != fld]
        assert len(doc["fields"]) == before - 1, f"no knowledge field {path}"
        for i, (name, cols, rows) in enumerate(tables):
            if name == obj:
                at = list(cols).index(fld)
                tables[i] = (name, {k: v for k, v in cols.items() if k != fld},
                             [tuple(v for k, v in enumerate(r) if k != at) for r in rows])
    today = dt.date.fromisoformat(spec["today"]) if spec.get("today") else TODAY
    return tables, knowledge, today


class World:
    """The page's Pipeline, ``cli.ask``, the record agent and the record tools on the memory fixture (or a variant),
    and the ask service's job, with one scripted client and the memory switches given (``tests/test_memory_exec.py``
    builds the same)."""

    def __init__(self, tmp: Path, monkeypatch: Any, memory: Any, spec: dict | None = None) -> None:
        monkeypatch.setenv("SFA_CHARTS", "on")
        monkeypatch.setenv("SFA_TABLES", "model")
        monkeypatch.setenv("SFA_MERGE", "code")
        monkeypatch.setenv("SFA_RESOLVER", "off")
        monkeypatch.setattr(LLM, "backoff_base_s", 0)
        tables, knowledge, today = _variant(spec or {})
        self.today = today
        self.client = Client()
        wh = F.make_warehouse(tmp, tables=tables)
        kn = F.make_knowledge(tmp, knowledge=knowledge)
        settings = sfa_config.load_settings(records=False, components_dir=str(tmp / "none"))
        _c, _n, registry, system, _l = core.build(knowledge_files(str(kn)), settings)
        cfg = sfa_config.load_config(base_url="http://127.0.0.1:9/v1", api_key="x")
        llm = LLM(cfg, client=self.client)

        def open_session(cfg: Any, rcfg: Any, knowledge_dir: Any = None, **kw: Any) -> Any:
            session, lines = core.open_record_session(cfg, rcfg, knowledge_dir=knowledge_dir, **kw)
            if session is not None:
                session.llm = llm     # the scripted client; everything else is the real session
            return session, lines

        rcfg = sfa_config.load_records_config(warehouse=str(wh), today=today)
        self.log = ask_mcp.RotatingAskLog(str(tmp / "ask_logs"))
        ask_mod = types.SimpleNamespace(ask=cli_ask, open_record_session=open_session)
        self.pipe = pipeline_mod.Pipeline(ask_mod, cfg, registry, system, llm, rcfg, log=self.log.write,
                                          knowledge_dir=str(kn))
        self.service = ask_mcp.AskService(self.pipe, types.SimpleNamespace(state=lambda: "ok"), self.log,
                                          memory=memory)

    @property
    def tools(self) -> Any:
        session = self.pipe.session
        return session.tools if session is not None else None

    def job(self, question: str, turns: list, *, state: Any, gap: int, summary: str = "") -> dict:
        """One contract-v2 job as ``AskService.ask`` runs it; its outcome with ``reply`` (what the caller gets)."""
        arguments = {"question": question, "recent_turns": turns, "contract": 2, "state": state, "gap": gap,
                     "summary": summary}
        assert ask_mcp._bad_arguments(arguments) is None, arguments
        req = self.service._request(arguments)
        out = self.service._job(question, turns) if req is None else self.service._job(question, turns, req)
        return {**out, "reply": self.service._final(out)}

    def close(self) -> None:
        self.service.close()


def stored(state: Any) -> Any:
    """``state`` as the platform stores it: a JSON round trip with every object's keys in another order (jsonb
    reorders them)."""
    def shuffle(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: shuffle(v[k]) for k in sorted(v, reverse=True)}
        if isinstance(v, list):
            return [shuffle(x) for x in v]
        return v
    return json.loads(json.dumps(shuffle(state)))


# ------------------------------------------------------------------------------------------------ running a chain
@dataclass
class StepOut:
    """What one step gave: the job's outcome (``out``, with ``reply``), what the bridge sent (``sent``: turns, state,
    gap), the record tool calls of the step (``evidence``) and the model requests per role made in it (``calls``)."""

    index: int
    step: dict
    out: dict | None
    sent: dict
    evidence: list = field(default_factory=list)
    calls: dict = field(default_factory=dict)
    world: str = ""
    requests: dict = field(default_factory=dict)

    @property
    def reply(self) -> dict:
        return (self.out or {}).get("reply") or {}


def load_chains(folder: Path = CHAINS_DIR) -> list[dict]:
    """Every chain of every ``*.json`` file in ``folder``, in file order, each with ``file`` (its file name)."""
    out = []
    for path in sorted(folder.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for chain in doc["chains"]:
            out.append({**chain, "file": path.name})
    return out


def _script(step: dict, memory: Any) -> tuple[list, list, list]:
    """The resolver, rewrite and record scripts of a step for this run (``v1`` replaces them off and in shadow)."""
    resolve = step.get("resolve")
    if resolve is None:
        resolve = []
    elif not isinstance(resolve, list):
        resolve = [resolve]
    resolve = [r if isinstance(r, str) else json.dumps(r) for r in resolve]
    rewrite = step.get("rewrite")
    record = step.get("record") or []
    if not memory.on:
        rewrite, record = _v1_script(step, resolve)
    rewrite = [] if rewrite is None else ([rewrite] if isinstance(rewrite, str) else list(rewrite))
    return resolve, rewrite, copy.deepcopy(list(record))


def _v1_script(step: dict, resolve: list[str]) -> tuple[Any, list]:
    """The rewrite reply and the record agent's steps of a step answered by today's path (off and shadow). Given in
    ``v1``, or else: the step's ``rewrite``, else the resolver's ``standalone`` when the step scripts one, else the
    typed question; the step's record steps when they hold a tool call, else the calls the step expects to run with
    ``SFA_MEMORY=on`` (``expect.ran`` entries with exact ``args``: what the record agent would call for the rewritten
    question), then the step's texts."""
    v1 = step.get("v1") or {}
    rewrite = v1.get("rewrite", step.get("rewrite"))
    if rewrite is None:
        standalone = ""
        for raw in resolve:
            try:
                got = json.loads(raw)
            except ValueError:
                continue
            if isinstance(got, dict) and isinstance(got.get("standalone"), str) and got["standalone"].strip():
                standalone = got["standalone"]
                break
        rewrite = standalone or step["q"]
    if "record" in v1:
        return rewrite, list(v1["record"])
    record = list(step.get("record") or [])
    if not any(isinstance(x, dict) for x in record):
        ran = [{"name": r["tool"], "args": r["args"]} for r in (step.get("expect") or {}).get("ran") or ()
               if "args" in r and r.get("ok", True)]
        record = ran + record
    return rewrite, record


def run_chain(chain: dict, memory: Any, tmp: Path, monkeypatch: Any) -> list[StepOut]:
    """Run every step of ``chain`` with the memory switches ``memory``; one :class:`World` per world name, each in its
    own folder under ``tmp``. The turns, the state and the gap are carried as the platform carries them."""
    worlds: dict[str, World] = {}
    specs = chain.get("worlds") or {}
    turns: list[dict] = []
    state: Any = None
    gap = 0
    outs: list[StepOut] = []
    try:
        for i, step in enumerate(chain["steps"]):
            q = step["q"]
            bridge = step.get("bridge")
            if bridge:   # the platform answered this turn: no service call
                answer = step.get("answer") or "The older route answered this."
                turns += [{"role": "user", "content": q}, {"role": "assistant", "content": answer}]
                if bridge == "fallback":
                    gap += 1
                outs.append(StepOut(i, step, None, {"bridge": bridge, "gap": gap}))
                continue
            name = step.get("world", chain.get("world", ""))
            if name not in worlds:
                folder = Path(tmp) / f"w{len(worlds)}"
                folder.mkdir(parents=True)
                assert name == "" or name in specs, f"{chain['id']} step {i}: no world {name!r}"
                worlds[name] = World(folder, monkeypatch, memory, specs.get(name) or {})
            w = worlds[name]
            resolve, rewrite, record = _script(step, memory)
            w.client.resolve, w.client.rewrite, w.client.record = resolve, rewrite, record
            w.client.route = list(step.get("route") or [])
            before = w.client.counts()
            seen = list(w.tools.evidence) if w.tools is not None else []   # kept alive: their ids stay unique
            sent_gap = step.get("gap", gap)
            sent = {"turns": copy.deepcopy(turns[-MAX_TURNS:]), "state": copy.deepcopy(state), "gap": sent_gap}
            out = w.job(q, turns[-MAX_TURNS:], state=state, gap=sent_gap, summary=step.get("summary", ""))
            after = w.client.counts()
            tools = w.tools
            # the record tool calls of this step only (a step that runs none leaves the last question's evidence)
            evidence = [e for e in (tools.evidence if tools is not None else []) if all(e is not x for x in seen)]
            made = {k: w.client.requests[k][before[k]:] for k in ROLES}
            outs.append(StepOut(i, step, out, sent, evidence, {k: after[k] - before[k] for k in ROLES}, name, made))
            reply = out["reply"]
            turns += [{"role": "user", "content": q}, {"role": "assistant", "content": reply.get("answer") or ""}]
            new_state = reply.get("state") if isinstance(reply.get("state"), dict) else None
            if new_state is not None:   # the bridge stores a row: the next step's parent, at gap 0
                state, gap = stored(new_state), 0
            elif memory.on or memory.shadow:   # no row: the walk-back finds the last one, one answer further back
                gap += 1
    finally:
        for w in worlds.values():
            w.close()
    return outs


# ------------------------------------------------------------------------------------------------ the checks
#: A Salesforce record Id (15 or 18 characters with at least 5 digits): never stored in a state (§10, the owner's note).
_ID_RE = re.compile(r"(?<![A-Za-z0-9])(?=(?:[A-Za-z]*\d){5})[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?(?![A-Za-z0-9])")


def codes_only(value: Any) -> bool:
    """``value`` holds fixed codes and numbers only (the ask log's ``memory`` entry): every text is a short lower-case
    code, as ``tests/test_memory_exec.py`` reads it."""
    if isinstance(value, dict):
        return all(codes_only(k) and codes_only(v) for k, v in value.items())
    if isinstance(value, list):
        return all(codes_only(v) for v in value)
    if isinstance(value, str):
        return len(value) <= 40 and all(ch.islower() or ch.isdigit() or ch in "_+" for ch in value)
    return value is None or isinstance(value, (bool, int, float))


def _subset(want: Any, got: Any) -> bool:
    """``want`` is contained in ``got``: dict keys recursively, lists item for item (same length), else equal."""
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and _subset(v, got[k]) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and len(want) == len(got) and all(_subset(a, b) for a, b in zip(want, got))
    return want == got


def _data_calls(evidence: list) -> list:
    return [e for e in evidence if e.tool in TC.ENFORCED_TOOLS and e.ok]


def _ran(e: Any) -> dict:
    return e.enforced_arguments or e.arguments


def _filter_items(args: dict) -> list[dict]:
    items = []
    for key in ("filters", "part_filters", "base_filters", "any_of"):
        items += [x for x in args.get(key) or [] if isinstance(x, dict)]
    for seg in args.get("segments") or []:
        items += [x for x in (seg.get("filters") or []) if isinstance(x, dict)]
    return items


def _contract_problem(name: str, evidence: list) -> str:
    """"" when every data call of the step holds contract ``name``, else what is wrong:

    - ``candidates``: candidates are Person Accounts. Each data call ran with the record-type filter
      (``RecordTypeId`` eq ``PersonAccount``, on the call's object or on its candidate lookup): added by code, and then
      its applied block says so (``term_filters``), or already in the call's own arguments (a record set followed to
      another object carries the set's enforced filters as path filters, C22).
    - ``placement``: a placement is a client interview with the outcome Offer Received. Each data call counts
      ``Interview__c`` with ``Interview_Outcome__c`` "Offer Received" in a filter, a part filter or a segment, and none
      reads ``Candidate_Status__c`` or ``Onboarding__c``.
    - ``support``: support person performance is grouped by ``Interview_Support_Person__c`` on ``Interview__c``, split
      by ``Interview_Outcome__c`` (completed by code when the model left it out), with the offer rate added."""
    calls = _data_calls(evidence)
    if not calls:
        return "no data call ran"
    for e in calls:
        ran = _ran(e)
        if name == "candidates":
            def scoped(args: dict) -> bool:
                return any(str(x.get("field", "")).endswith("RecordTypeId") and x.get("op") == "eq"
                           and x.get("value") == TC.PERSON_TYPE for x in _filter_items(args))
            tf = e.applied.get("term_filters") or []
            said = any("RecordTypeId = 'Person Account'" in t for t in tf)
            if not scoped(ran) or not (said or scoped(e.arguments)):
                return f"{e.tool} ran without the Person Account scope: {ran} / term_filters {tf}"
        elif name == "placement":
            if ran.get("object") != "Interview__c":
                return f"{e.tool} measures placements on {ran.get('object')}, not Interview__c"
            text = json.dumps(ran)
            if "Candidate_Status__c" in text or "Onboarding__c" in text:
                return f"{e.tool} reads the candidate status or onboarding: {ran}"
            if not any(x.get("field") == TC.OUTCOME and (x.get("value") == TC.OFFER or TC.OFFER in (x.get("values")
                       or [])) for x in _filter_items(ran)):
                return f"{e.tool} has no {TC.OUTCOME} = {TC.OFFER!r} item: {ran}"
        elif name == "support":
            if (ran.get("object"), ran.get("group_by")) != ("Interview__c", "Interview_Support_Person__c"):
                return f"{e.tool} is not grouped by Interview__c.Interview_Support_Person__c: {ran}"
            if e.tool == "group_records" and (ran.get("split_by") != TC.OUTCOME
                                              or "offer_rate_overall_pct" not in (e.model_result or {})):
                return f"{e.tool} has no outcome split or offer rate: {ran}"
        else:
            return f"unknown contract {name!r}"
    return ""


def _plan_problems(want: Any, state: Any) -> list[str]:
    plan = state.get("plan") if isinstance(state, dict) else None
    if want is None:
        return [] if plan is None else [f"plan is not null: {plan}"]
    if plan is None:
        return ["plan is null"]
    bad = []
    calls = plan.get("calls") or []
    # the main call by its role (the seeded calls run with the main call last, stage-2 review fix M2), else the first
    main = next((c for c in calls if c.get("role") == "main"), calls[0] if calls else {})
    if "tool" in want and (not calls or main.get("tool") != want["tool"]):
        bad.append(f"plan main tool {main.get('tool') if calls else None} != {want['tool']}")
    if "calls" in want:
        got = [{"tool": c.get("tool"), "args": c.get("args")} for c in calls]
        if got != want["calls"]:
            bad.append(f"plan calls {got} != {want['calls']}")
    if "period" in want and not (calls and _subset(want["period"], main.get("period"))):
        bad.append(f"plan main period {main.get('period') if calls else None} lacks {want['period']}")
    if "nonempty" in want and not (calls and main.get("nonempty") is want["nonempty"]):
        bad.append(f"plan main nonempty {main.get('nonempty') if calls else None} != {want['nonempty']}")
    if "keys_from" in want and not (calls and _subset(want["keys_from"], main.get("keys_from"))):
        bad.append(f"plan main keys_from {main.get('keys_from') if calls else None} lacks {want['keys_from']}")
    if "show_chart" in want and (plan.get("show") or {}).get("chart") is not want["show_chart"]:
        bad.append(f"plan show {plan.get('show')}: chart is not {want['show_chart']}")
    if "show" in want and not _subset(want["show"], plan.get("show")):
        bad.append(f"plan show {plan.get('show')} != {want['show']}")
    if "age" in want and plan.get("age") != want["age"]:
        bad.append(f"plan age {plan.get('age')} != {want['age']}")
    if "keys" in want and sorted((plan.get("terms") or {}).get("keys") or []) != sorted(want["keys"]):
        bad.append(f"terms keys {(plan.get('terms') or {}).get('keys')} != {want['keys']}")
    if "terms_text" in want and (plan.get("terms") or {}).get("text") != want["terms_text"]:
        bad.append(f"terms text {(plan.get('terms') or {}).get('text')!r} != {want['terms_text']!r}")
    return bad


def step_problems(s: StepOut) -> list[str]:
    """What is wrong with one step of a run with ``SFA_MEMORY=on`` against its ``expect`` (see the module text)."""
    want = s.step.get("expect") or {}
    if s.out is None:
        return []
    reply, bad = s.reply, []
    if set(reply) != V2_KEYS:
        bad.append(f"reply keys {sorted(reply)}")
    if reply.get("reason") not in ask_mcp.REASONS:
        bad.append(f"reason {reply.get('reason')!r} is no contract-v2 code")
    if reply.get("reason") in ("patch", "shortcut") and any(s.calls.get(r) for r in ("route", "lean", "knowledge")):
        bad.append(f"a patched run called the route or the knowledge step (D4: the switch is off): {s.calls}")
    if not codes_only(s.out.get("memory")):
        bad.append(f"the ask log's memory entry holds more than codes and numbers: {s.out.get('memory')}")
    if isinstance(reply.get("state"), dict):
        text = json.dumps(reply["state"], ensure_ascii=False)
        if _ID_RE.search(text) or "@" in text:
            bad.append("the state holds a record Id or an e-mail address")
        if Q.state_bytes(reply["state"]) > Q.MAX_STATE_BYTES:
            bad.append("the state is larger than 32 KB")
    for key in ("status", "kind", "reason"):
        if key in want and reply.get(key) != want[key]:
            bad.append(f"{key} {reply.get(key)!r} != {want[key]!r}")
    if "sent_gap" in want and s.sent.get("gap") != want["sent_gap"]:
        bad.append(f"the bridge sent gap {s.sent.get('gap')}, expected {want['sent_gap']}")
    if "sent_turns" in want and len(s.sent.get("turns") or []) != want["sent_turns"]:
        bad.append(f"the bridge sent {len(s.sent.get('turns') or [])} turns, expected {want['sent_turns']}")
    if "detail" in want:
        got = ((s.out.get("memory") or {}).get("resolver") or {}).get("detail")
        if got != want["detail"]:
            bad.append(f"resolver detail {got!r} != {want['detail']!r}")
    for key, role in (("model", "resolve"), ("rewrite", "rewrite"), ("record", "record")):
        if key in want and bool(s.calls.get(role)) is not want[key]:
            bad.append(f"{role} requests {s.calls.get(role)}, expected {'some' if want[key] else 'none'}")
    if want.get("ran_none") and s.evidence:
        bad.append(f"record tools ran: {[e.tool for e in s.evidence]}")
    if "ran" in want:
        ev = s.evidence
        if len(ev) != len(want["ran"]):
            bad.append(f"ran {[(e.tool, e.arguments) for e in ev]}, expected {len(want['ran'])} calls")
        for exp, e in zip(want["ran"], ev):
            if "tool" in exp and e.tool != exp["tool"]:
                bad.append(f"ran {e.tool} != {exp['tool']}")
            if exp.get("ok", True) is not e.ok:
                bad.append(f"{e.tool} ok {e.ok} ({e.error})")
            if "args" in exp and e.arguments != exp["args"]:
                bad.append(f"{e.tool} args {e.arguments} != {exp['args']}")
            if "args_has" in exp and not _subset(exp["args_has"], e.arguments):
                bad.append(f"{e.tool} args {e.arguments} lack {exp['args_has']}")
            if "term_filters" in exp and (e.applied.get("term_filters") or []) != exp["term_filters"]:
                bad.append(f"{e.tool} term_filters {e.applied.get('term_filters')} != {exp['term_filters']}")
            if "enforced_has" in exp and not _subset(exp["enforced_has"], _ran(e)):
                bad.append(f"{e.tool} ran {_ran(e)}, lacks {exp['enforced_has']}")
            if "result_has" in exp and not _subset(exp["result_has"], e.model_result):
                bad.append(f"{e.tool} result {e.model_result} lacks {exp['result_has']}")
            for text in exp.get("result_not") or ():
                if text in json.dumps(e.model_result):
                    bad.append(f"{e.tool} result holds {text!r}")
            for text in exp.get("result_holds") or ():
                if text not in json.dumps(e.model_result):
                    bad.append(f"{e.tool} result lacks {text!r}")
    for name in want.get("contracts") or ():
        problem = _contract_problem(name, s.evidence)
        if problem:
            bad.append(f"contract {name}: {problem}")
    state = reply.get("state")
    if "state" in want and want["state"] is None and state is not None:
        bad.append(f"state is not null: {state}")
    if "plan" in want:
        bad += _plan_problems(want["plan"], state)
    if "pending" in want:
        pending = (state or {}).get("pending")
        labels = [c["label"] for c in pending["choices"]] if pending else None
        if labels != want["pending"]:
            bad.append(f"pending {labels} != {want['pending']}")
    answer = reply.get("answer") or ""
    if "answer" in want and answer != want["answer"]:
        bad.append(f"answer {answer!r} != {want['answer']!r}")
    for text in want.get("answer_has") or ():
        if text not in answer:
            bad.append(f"answer lacks {text!r}: {answer!r}")
    for text in want.get("answer_not") or ():
        if text in answer:
            bad.append(f"answer holds {text!r}: {answer!r}")
    if "answer_starts" in want and not answer.startswith(want["answer_starts"]):
        bad.append(f"answer does not start with {want['answer_starts']!r}: {answer!r}")
    if "read_as" in want:
        if want["read_as"] is None and answer.startswith("Read as:"):
            bad.append(f"answer has a Read-as line: {answer.splitlines()[0]!r}")
        if want["read_as"] == "plan_line" and not answer.startswith(f"Read as: `{reply.get('plan_line')}`"):
            bad.append(f"answer does not read as the plan line {reply.get('plan_line')!r}: {answer[:200]!r}")
    for text in want.get("plan_line_has") or ():
        if text not in (reply.get("plan_line") or ""):
            bad.append(f"plan line lacks {text!r}: {reply.get('plan_line')!r}")
    if "charts" in want and len(reply.get("charts") or []) != want["charts"]:
        bad.append(f"charts {len(reply.get('charts') or [])} != {want['charts']}")
    if want.get("clarify"):
        pending = (state or {}).get("pending")
        if (reply.get("status"), reply.get("kind"), reply.get("reason")) != ("answered", "none", "clarify"):
            bad.append(f"a clarify is answered / none / clarify, not {reply.get('status')} / {reply.get('kind')} / "
                       f"{reply.get('reason')}")
        elif pending:
            numbered = pending["asked"] + "\n\n" + "\n".join(f"{c['n']}. {c['label']}" for c in pending["choices"])
            if answer != numbered:
                bad.append(f"the clarify text is not the stored question and choices: {answer!r} != {numbered!r}")
    return bad


def check_chain(chain: dict, outs: list[StepOut]) -> None:
    """Every step of a run with ``SFA_MEMORY=on`` meets its ``expect``; one AssertionError lists every miss."""
    bad = []
    for s in outs:
        for problem in step_problems(s):
            bad.append(f"{chain['id']} step {s.index + 1} {s.step['q']!r}: {problem}")
    assert not bad, "\n".join(bad)


def check_parity(chain: dict, off: list[StepOut], shadow: list[StepOut]) -> None:
    """Shadow answers byte for byte as off on every step (C31): the six result keys are equal, every model request of
    the answer path (rewrite, record, route, names, knowledge, merge) is equal, shadow makes no resolver call, off
    replies as contract v1 and shadow as contract v2, and shadow's log entry holds fixed codes and numbers only."""
    bad = []
    for a, b in zip(off, shadow):
        where = f"{chain['id']} step {a.index + 1} {a.step['q']!r}"
        if a.out is None:
            continue
        if set(a.reply) != V1_KEYS:
            bad.append(f"{where}: off replied {sorted(a.reply)}")
        if set(b.reply) != V2_KEYS:
            bad.append(f"{where}: shadow replied {sorted(b.reply)}")
        same = {k: b.reply.get(k) for k in V1_KEYS}
        if same != a.reply:
            diff = {k: (a.reply.get(k), same.get(k)) for k in V1_KEYS if a.reply.get(k) != same.get(k)}
            bad.append(f"{where}: shadow answered otherwise: {diff}")
        if b.calls.get("resolve"):
            bad.append(f"{where}: shadow called the resolver model")
        for role in ROLES:
            if role != "resolve" and a.requests.get(role) != b.requests.get(role):
                bad.append(f"{where}: the {role} requests differ ({a.calls.get(role)} off, {b.calls.get(role)} "
                           "shadow)")
        if [(e.tool, e.arguments) for e in a.evidence] != [(e.tool, e.arguments) for e in b.evidence]:
            bad.append(f"{where}: other record tool calls in shadow")
        if (b.out.get("memory") or {}).get("mode") != "shadow":
            bad.append(f"{where}: no shadow log entry")
    assert len(off) == len(shadow)
    assert not bad, "\n".join(bad)
