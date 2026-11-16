"""Value spots (2026-10-28): the route, the record agent's pushback, the knowledge step and the trace for the stored
record values a question names (agent/record_session.py, agent/triage.py, pipeline.py, agent/render.py).

Offline: stub tools whose ``spot_values`` returns fixed spot dicts (the interface of ``RecordTools.spot_values``) and
a scripted model; no warehouse, no model. Fake data only: the department, company and people are made up.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import sf_assistant.cli
import sf_assistant.pipeline as pipeline
from sf_assistant.agent import knowledge_prompts as kprompts
from sf_assistant.agent import record_prompts as rprompts
from sf_assistant.agent import triage as T
from sf_assistant.agent.knowledge_agent import AgentResult
from sf_assistant.agent.record_session import RecordAnswer, RecordSession
from sf_assistant.agent.render import record_trace_lines
from sf_assistant.agent.triage import Triage
from sf_assistant.config import load_config
from sf_assistant.engine.types import QueryEvidence, RecordsStatus, Statement
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.prompts import fill
from sf_assistant.tools.knowledge_tools import Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"

STATUS = RecordsStatus(
    snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
    copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
    origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
    time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
    today_pinned=True, objects=60, duckdb_version="1.5.6")
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC."

Q_TEAM = "what does team zorvex do?"                       # a fake department named with its cue word
Q_COMPANY = "how many interviews did zorvex quill hold?"   # a fake two-word company name
LOG_KEYS = {"routed", "route", "names", "people", "missing", "people_data", "seconds"}   # the triage log before


def _spot(span: str, obj: str = "Department__c", field: str = "Name", kind: str = "name", strength: str = "strong",
          why: str = "cue word", records: int = 1, listed: bool = True, start: int = 0, **extra) -> dict:
    """One spot dict as ``RecordTools.spot_values`` gives it (design.md 4.5), with fake values."""
    d = {"span": span, "start": start, "end": start + len(span), "key": span.lower(), "object": obj, "field": field,
         "kind": kind, "value": span.title(), "values": [span.title()], "records": records, "truncated": False,
         "strength": strength, "why": why, "cue": "team" if why == "cue word" else "", "listed": listed}
    d.update(extra)
    return d


def _strong_person(words: str) -> dict:
    return {"words": words, "recs": [("Recruiter__c", "a06000000000020AAA", words.title(), "Active Employee")],
            "strength": "strong", "first_only": False, "asked": None, "close": []}


def _count_evidence(qid: str, args: dict) -> QueryEvidence:
    result = {"query_id": qid, "object": "Recruiter__c", "count": 6, "counted": "Recruiter__c records",
              "applied": {"filters": ["Department__c = 'Zorvex'"]}}
    st = Statement(sql='SELECT COUNT(*) AS n FROM main."Recruiter__c" AS t0 WHERE t0."Department__c" = ?',
                   params=["Zorvex"], columns=["n"], rows=[[6]], row_count=1, truncated=False, elapsed_ms=1.0)
    return QueryEvidence(query_id=qid, tool="count_records", kind="data", object="Recruiter__c", arguments=dict(args),
                         ok=True, error=None, applied=result["applied"], notes=[], statements=[st],
                         tables=["Recruiter__c"], synced_at={"Recruiter__c": "2026-10-13 14:12 UTC"}, summary="6",
                         model_result=result, elapsed_ms=1.5)


COUNT_CALL = {"name": "count_records", "args": {"object": "Recruiter__c",
                                                "filters": [{"field": "Department__c", "op": "eq", "value": "Zorvex"}]}}


class ValueTools:
    """The record tools surface the triage and the record agent use: ``spot_values`` gives the fixed spots of
    ``spots`` (question -> spot dicts) and logs the people it was given; ``spot_people`` gives ``people`` (question ->
    spot_people entries); ``resolve_person_name`` knows ``stored`` (name -> entry); ``design`` are the design words.
    count_records -> 6."""

    def __init__(self, spots=None, people=None, stored=None, design=()):
        self.spots, self.people, self.stored, self.design = dict(spots or {}), dict(people or {}), dict(stored or {}), \
            set(design)
        self.spot_calls: list[tuple[str, list | None]] = []
        self.began: list[dict] = []
        self._evidence: list[QueryEvidence] = []
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.tools = {n: Tool(name=n, description=n, input_schema=schema, handler=lambda **_: {})
                      for n in ("count_records", "list_records")}

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name, arguments=None):
        ev = _count_evidence(f"q{len(self._evidence) + 1}", arguments or {})
        self._evidence.append(ev)
        return {"ok": True, "result": ev.model_result, "error": None, "chunk_ids": []}

    def spot_values(self, question, people=None):
        self.spot_calls.append((question, None if people is None else [p.get("words") for p in people]))
        return [dict(d) for d in self.spots.get(question, [])]

    def spot_people(self, question):
        return [dict(d) for d in self.people.get(question, [])]

    def resolve_person_name(self, name):
        hit = self.stored.get(" ".join(str(name).lower().split()))
        return dict(hit) if hit else None

    def is_design_word(self, word):
        return word in self.design

    def begin_question(self, question="", people=None, values=None):
        self.began.append({"question": question, "people": people, "values": values})
        self._evidence = []

    @property
    def evidence(self):
        return list(self._evidence)

    def object_hints(self, question, missing=()):
        return "[Hints from code, not from the user]\nRecords named in the question: (fake)"

    def status(self):
        return STATUS

    def close(self):
        pass


class ScriptedLLM:
    """The route call answers ``route``, the names call ``names``; the record agent's k-th call replies script[k] (the
    last one repeats): a dict is a tool call, a string the reply."""

    def __init__(self, script, route="NO_RECORDS", names="NONE"):
        self.script, self.route, self.names = list(script), route, names
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, max_tokens=None, **_):
        system = messages[0].get("content")
        if system == rprompts.ROUTE_SYSTEM:
            return {"content": self.route, "tool_calls": [], "usage": {}}
        if system == rprompts.NAMES_SYSTEM:
            return {"content": self.names, "tool_calls": [], "usage": {}}
        self.calls.append([dict(m) for m in messages])
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, dict):
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": step["name"],
                                                     "arguments": step["args"]}], "usage": {}}
        return {"content": step, "tool_calls": [], "usage": {}}


def _cfg():
    return load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=6)


def _triage(tools, q, route="NO_RECORDS", names="NONE") -> Triage:
    return RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"], route=route, names=names)).triage(q)


# ------------------------------------------------------------------------------------ the triage (design.md 6.1)

def test_a_strong_value_overrules_a_no_records_route():
    tools = ValueTools({Q_TEAM: [_spot("zorvex", start=15)]})
    tri = _triage(tools, Q_TEAM)
    assert tri.route is False and tri.routed is True and tri.overruled_by == "value"
    assert tri.overrule_reason == "the stored value 'zorvex' (Department__c.Name, cue word)"
    assert tri.people == [] and tri.missing == [] and [d["span"] for d in tri.values] == ["zorvex"]
    d = tri.as_dict()
    assert d["overruled_by"] == "value" and d["overrule_reason"] == tri.overrule_reason
    assert d["values"] == [{"span": "zorvex", "object": "Department__c", "field": "Name", "kind": "name",
                            "strength": "strong", "why": "cue word", "records": 1}]   # never value, values or Ids
    assert tools.spot_calls == [(Q_TEAM, [])]   # nobody confirmed: [] (no person owns a word), never None


def test_the_first_strong_spot_is_the_reason_and_read_as_is_kept():
    q = "what do zorvx and quillmark do?"
    tools = ValueTools({q: [_spot("zorvx", why="unknown word", read_as="Zorvex", start=8),
                            _spot("quillmark", obj="Company__c", why="capitalised", start=18)]})
    tri = _triage(tools, q)
    assert tri.overrule_reason == "the stored value 'zorvx' read as 'Zorvex' (Department__c.Name, unknown word)"
    assert tri.as_dict()["values"][0]["read_as"] == "Zorvex" and "read_as" not in tri.as_dict()["values"][1]


def test_design_vocabulary_and_plain_words_never_overrule():
    """Picklist values, record type names and plain-word hints never overrule the route (design vocabulary); only
    listed spots are kept, and a listed hint is logged."""
    q = "what does the zorvex status mean?"
    tools = ValueTools({q: [_spot("zorvex", kind="picklist", strength="hint", why="picklist", listed=False),
                            _spot("status", kind="record_type", why="record_type"),   # even when marked strong
                            _spot("status", kind="picklist", why="picklist"),
                            _spot("zorvex", strength="hint", why="plain words")]})
    tri = _triage(tools, q)
    assert tri.routed is False and tri.overruled_by is None and tri.overrule_reason == ""
    assert [(d["kind"], d["strength"]) for d in tri.values] == [("record_type", "strong"), ("picklist", "strong"),
                                                                 ("name", "hint")]
    assert tri.strong_values() == []
    d = tri.as_dict()
    assert "overruled_by" not in d and "overrule_reason" not in d and len(d["values"]) == 3


def test_an_unclear_or_records_route_needs_no_value_overrule():
    for route, routed in (("maybe", None), ("RECORDS", True)):
        tools = ValueTools({Q_TEAM: [_spot("zorvex")]})
        tri = _triage(tools, Q_TEAM, route=route)
        assert tri.routed is routed and tri.overruled_by is None, route
        assert [d["span"] for d in tri.values] == ["zorvex"] and "overruled_by" not in tri.as_dict()


def test_a_route_given_by_the_caller_is_overruled_the_same_way():
    tools = ValueTools({Q_TEAM: [_spot("zorvex")]})
    tri = RecordSession(tools, _cfg(), llm=ScriptedLLM(["x"])).triage(Q_TEAM, route=False)
    assert tri.routed is True and tri.overruled_by == "value"


def test_a_question_without_value_spots_logs_as_before():
    class NoSpots(ValueTools):
        spot_values = None   # tools without spot_values: the triage as before

    class Raises(ValueTools):
        def spot_values(self, question, people=None):
            raise RuntimeError("index broken")

    for tools in (NoSpots(), Raises(), ValueTools()):
        tri = _triage(tools, Q_TEAM)
        assert tri.routed is False and tri.values == [] and tri.overruled_by is None
        assert set(tri.as_dict()) == LOG_KEYS


def test_the_people_overrules_decide_first_and_are_logged():
    kilo = _strong_person("kilo zephyr")
    q = "what does kilo zephyr do in team zorvex?"
    stored = {"kilo zephyr": {**kilo, "from": "names"}}
    tools = ValueTools({q: [_spot("zorvex", start=30)]}, people={q: [kilo]}, stored=stored)
    tri = _triage(tools, q, names="Kilo Zephyr")
    assert tri.routed is True and tri.overruled_by == "person" and tri.overrule_reason == T.PERSON_OVERRULE_REASON
    assert tri.as_dict()["overrule_reason"] == "a named person" and [d["span"] for d in tri.values] == ["zorvex"]
    assert tools.spot_calls == [(q, ["kilo zephyr"])]   # the confirmed person owns its words
    tri = _triage(ValueTools(people={q: [kilo]}, stored=stored), q, route="maybe", names="Kilo Zephyr")
    assert tri.overruled_by == "person"                  # an unclear route overruled by a person: logged too
    tri = _triage(ValueTools(people={q: [kilo]}, stored=stored), q, route="RECORDS", names="Kilo Zephyr")
    assert tri.routed is True and tri.overruled_by is None and "overruled_by" not in tri.as_dict()
    q2 = "who is zorblax quentin"
    tri = _triage(ValueTools(), q2, names="Zorblax Quentin")
    assert tri.missing == ["Zorblax Quentin"] and tri.routed is True and tri.overruled_by == "missing name"
    assert tri.overrule_reason == T.MISSING_OVERRULE_REASON == "a name no stored person has"


def test_spot_values_gets_the_confirmed_and_the_hinted_people():
    kilo = _strong_person("kilo zephyr")
    q = "what does the kilo zephyr integration user do?"
    tools = ValueTools(people={q: [kilo]}, stored={"kilo zephyr": kilo})
    tri = _triage(tools, q)   # a design question: the strong person is only hinted, and still owns its words
    assert tri.people == [] and [p["words"] for p in tri.hinted] == ["kilo zephyr"]
    assert tools.spot_calls == [(q, ["kilo zephyr"])]
    weak = {"words": "kilo", "recs": kilo["recs"], "strength": "weak", "first_only": True, "asked": None, "close": []}
    q = "what does the kilo company do?"
    tools = ValueTools({q: [_spot("kilo", obj="Company__c", start=14, cue="company")]}, people={q: [weak]})
    tri = _triage(tools, q)   # an unconfirmed first name owns nothing: the company spot reaches the triage
    assert tools.spot_calls == [(q, [])] and tri.people == [] and tri.overruled_by == "value"


def test_a_name_inside_a_stored_value_is_no_missing_person():
    """Amendment 18: a names-call name whose every word that is no design word lies inside a listed value spot of
    kind name, code or text names a record, not a missing person ("Team Zorvex" for a stored department)."""
    tri = _triage(ValueTools({Q_TEAM: [_spot("zorvex", start=15)]}, design={"team"}), Q_TEAM, names="Team Zorvex")
    assert tri.names == ["Team Zorvex"] and tri.missing == [] and tri.overruled_by == "value"
    tri = _triage(ValueTools(design={"team"}), Q_TEAM, names="Team Zorvex")
    assert tri.missing == ["Team Zorvex"] and tri.routed is False   # no spot: as before (a design word, no overrule)
    # a two-word company name: a missing person without the spot (the full-name shape overrules), none with it
    tri = _triage(ValueTools(), Q_COMPANY, names="Zorvex Quill")
    assert tri.missing == ["Zorvex Quill"] and tri.overruled_by == "missing name"
    for kind, strength, by in (("name", "strong", "value"), ("name", "hint", None), ("text", "hint", None),
                               ("code", "strong", "value")):
        spot = _spot("zorvex quill", obj="Company__c", kind=kind, strength=strength, why="multi-word", start=25)
        tri = _triage(ValueTools({Q_COMPANY: [spot]}), Q_COMPANY, names="Zorvex Quill")
        assert tri.missing == [] and tri.overruled_by == by and tri.routed is (by is not None), (kind, strength)
    # only a part of the name lies inside a value: still a missing person
    tri = _triage(ValueTools({Q_COMPANY: [_spot("zorvex", obj="Company__c", start=25)]}), Q_COMPANY,
                  names="Zorvex Quill")
    assert tri.missing == ["Zorvex Quill"] and tri.overruled_by == "missing name"
    # review F1: the words of two separate spots (two stored company Names) never pool into one stored name
    q = "how many interviews did zorvex plumtrax hold?"
    two = [_spot("zorvex", obj="Company__c", start=24), _spot("plumtrax", obj="Company__c", start=31)]
    tri = _triage(ValueTools({q: two}), q, names="Zorvex Plumtrax")
    assert tri.missing == ["Zorvex Plumtrax"] and tri.overruled_by == "missing name"
    # a picklist value is design vocabulary: no record name (and never listed by the tools)
    tri = _triage(ValueTools({Q_COMPANY: [_spot("zorvex quill", kind="picklist", why="picklist", start=25)]}),
                  Q_COMPANY, names="Zorvex Quill")
    assert tri.missing == ["Zorvex Quill"]


def test_contact_values_are_never_repeated_in_the_log_or_the_trace():
    """Amendment 20: an e-mail address or phone number typed in the question is logged as "<e-mail>" / "<phone>", and
    the reason names it without its value."""
    mail, phone = "kilo.fake@example.invalid", "+1 555 010 0199"
    for typed, obj_field, logged, said in ((mail, ("Lead", "Email"), "<e-mail>", "an e-mail address"),
                                           (phone, ("Lead", "Phone"), "<phone>", "a phone number")):
        q = f"whose record holds {typed}?"
        spot = _spot(typed, obj=obj_field[0], field=obj_field[1], kind="contact", why="contact", start=19)
        spot.pop("value")
        tri = _triage(ValueTools({q: [spot]}), q)
        assert tri.overruled_by == "value"
        assert tri.overrule_reason == f"{said} in the question ({obj_field[0]}.{obj_field[1]})"
        d = tri.as_dict()
        assert d["values"][0]["span"] == logged and typed not in json.dumps(d)
        rec = RecordAnswer(question=q, answer="", no_records=True, used_data=False, triage=d, error="x")
        assert all(typed not in ln for ln in record_trace_lines(rec))


def test_a_bare_number_stored_as_a_phone_never_overrules_a_design_question():
    """Review PPI-1: a bare number that a contact field stores is a hint spot (the tools' rule), which never overrules
    the route call and never pushes back on a NO_RECORDS reply; numbers stay filters."""
    q = "what does error 0000000001 mean in the sync log?"
    spot = _spot("0000000001", obj="Lead", field="Phone", kind="contact", strength="hint", why="contact", start=16)
    spot.pop("value")
    tri = _triage(ValueTools({q: [spot]}), q)
    assert tri.routed is False and tri.overruled_by is None and tri.strong_values() == []
    assert tri.as_dict()["values"][0]["span"] == "<phone>" and "0000000001" not in json.dumps(tri.as_dict())


def test_the_log_keeps_at_most_eight_values(monkeypatch):
    q = "a b c d e f g h i j"
    spots = [_spot(w, why="multi-word", start=2 * i) for i, w in enumerate(q.split())]
    tri = _triage(ValueTools({q: spots}), q)
    assert len(tri.values) == 10 and len(tri.as_dict()["values"]) == T.VALUES_LOG_MAX == 8
    monkeypatch.setattr(T, "VALUES_LOG_MAX", 2)   # read at call time
    assert len(tri.as_dict()["values"]) == 2


# ------------------------------------------------------------------------------------ the record agent (6.2)

def test_check_draft_pushes_back_on_no_records_about_a_named_record(monkeypatch):
    session = RecordSession(ValueTools(), _cfg(), llm=ScriptedLLM(["x"]))
    tri = Triage(routed=True, route=False, values=[_spot("zorvex"), _spot("zorvex", obj="Zoom_Department__c"),
                                                   _spot("quillmark", obj="Company__c", why="capitalised"),
                                                   _spot("ops", strength="hint", why="plain words")],
                 overruled_by="value")
    want = fill(rprompts.RECORDS_VALUE_NO_RECORDS_MESSAGE,
                VALUES="'zorvex' (Department__c.Name), 'zorvex' (Zoom_Department__c.Name), "
                       "'quillmark' (Company__c.Name)")
    assert session.check_draft("NO_RECORDS", Q_TEAM, STATUS, True, tri) == want
    assert "do not reply NO_RECORDS" in want and "<<" not in want
    monkeypatch.setattr(T, "VALUES_MESSAGE_MAX", 1)   # read at call time
    assert session.check_draft("NO_RECORDS", Q_TEAM, STATUS, True, tri) == fill(
        rprompts.RECORDS_VALUE_NO_RECORDS_MESSAGE, VALUES="'zorvex' (Department__c.Name)")
    # hints only: NO_RECORDS passes; no triage: as before
    hints = Triage(routed=None, route=None, values=[_spot("ops", strength="hint", why="plain words")])
    assert session.check_draft("NO_RECORDS", Q_TEAM, STATUS, None, hints) is None
    assert session.check_draft("NO_RECORDS", Q_TEAM, STATUS, True, None) is None
    # the people message comes first
    people = Triage(routed=True, route=False, missing=["Zorblax Quentin"], values=[_spot("zorvex")])
    assert session.check_draft("NO_RECORDS", Q_TEAM, STATUS, True, people).startswith(
        "The question names 'Zorblax Quentin'")
    # an e-mail address in the question is named, not repeated
    mail = Triage(routed=True, route=False, values=[_spot("kilo.fake@example.invalid", obj="Lead", field="Email",
                                                          kind="contact", why="contact")])
    msg = session.check_draft("NO_RECORDS", Q_TEAM, STATUS, True, mail)
    assert "an e-mail address in the question (Lead.Email)" in msg and "example.invalid" not in msg


def test_ask_gives_the_values_to_begin_question():
    tools = ValueTools({Q_TEAM: [_spot("zorvex")]})
    rec = RecordSession(tools, _cfg(), llm=ScriptedLLM([COUNT_CALL, "6 employees belong to Zorvex."])).ask(Q_TEAM)
    assert tools.began[-1]["values"] == [_spot("zorvex")] and tools.began[-1]["people"] == []
    assert rec.answer.startswith("6 employees") and rec.triage["overruled_by"] == "value"

    class PeopleOnly(ValueTools):   # tools without the values argument: the old calls
        def begin_question(self, question="", people=None):
            self.began.append({"question": question, "people": people})
            self._evidence = []

    class QuestionOnly(ValueTools):
        def begin_question(self, question=""):
            self.began.append({"question": question})
            self._evidence = []

    for cls, keys in ((PeopleOnly, {"question", "people"}), (QuestionOnly, {"question"})):
        tools = cls({Q_TEAM: [_spot("zorvex")]})
        rec = RecordSession(tools, _cfg(), llm=ScriptedLLM([COUNT_CALL, "6 employees belong to Zorvex."])).ask(Q_TEAM)
        assert set(tools.began[-1]) == keys and rec.answer.startswith("6 employees"), cls.__name__


def test_a_value_question_runs_the_record_agent_and_gets_one_pushback():
    tools = ValueTools({Q_TEAM: [_spot("zorvex")]})
    llm = ScriptedLLM(["NO_RECORDS", "NO_RECORDS", COUNT_CALL, "6 employees belong to Zorvex."])
    rec = RecordSession(tools, _cfg(), llm=llm).ask(Q_TEAM)
    want = fill(rprompts.RECORDS_VALUE_NO_RECORDS_MESSAGE, VALUES="'zorvex' (Department__c.Name)")
    assert not rec.routed_out and not rec.no_records and rec.answer == "6 employees belong to Zorvex.\n" + SOURCE
    assert rec.nudges == [want] and want in [m["content"] for m in llm.calls[-1] if m["role"] == "user"]
    lines = record_trace_lines(rec)
    assert lines[0] == ("-- route: the route call said NO_RECORDS; overruled by the stored value 'zorvex' "
                        "(Department__c.Name, cue word)")
    assert lines[1].startswith("-- record agent: 1 tool call(s), 4 model call(s), ")
    # a second NO_RECORDS is accepted (the knowledge answer is shown, as for people)
    llm = ScriptedLLM(["NO_RECORDS"])
    rec = RecordSession(ValueTools({Q_TEAM: [_spot("zorvex")]}), _cfg(), llm=llm).ask(Q_TEAM)
    assert rec.no_records and not rec.routed_out and rec.error is None and rec.nudges == [want]
    # without a strong spot the route call decides, as before
    rec = RecordSession(ValueTools({Q_TEAM: [_spot("zorvex", strength="hint", why="plain words")]}), _cfg(),
                        llm=ScriptedLLM(["NO_RECORDS"])).ask(Q_TEAM)
    assert rec.routed_out and rec.agent is None and rec.triage["values"][0]["strength"] == "hint"


def test_last_triage_is_the_triage_the_route_call_kept():
    session = RecordSession(ValueTools({Q_TEAM: [_spot("zorvex")]}), _cfg(), llm=ScriptedLLM(["x"]))
    assert session.last_triage(Q_TEAM) is None
    assert session.route(Q_TEAM) is True
    tri = session.last_triage(Q_TEAM)
    assert isinstance(tri, Triage) and tri.overruled_by == "value" and session.last_triage("another question") is None


# ------------------------------------------------------------------------------------ the trace (6.4)

def test_trace_route_line_and_not_run_line():
    def rec(triage, agent=None, routed_out=False, error=None):
        return RecordAnswer(question="q", answer="", no_records=True, used_data=False, agent=agent, error=error,
                            routed_out=routed_out, triage=triage)

    value = {"route": False, "routed": True, "overruled_by": "value",
             "overrule_reason": "the stored value 'zorvex' (Department__c.Name, cue word)"}
    assert record_trace_lines(rec(value, error="model down")) == [
        "-- route: the route call said NO_RECORDS; overruled by the stored value 'zorvex' (Department__c.Name, cue "
        "word)", "-- record agent: no run (model down)"]
    person = {"route": None, "routed": True, "overruled_by": "person", "overrule_reason": "a named person"}
    agent = AgentResult(qid="rec-001", question="q", answer="NO_RECORDS", steps=1, latency_s=0.5)
    assert record_trace_lines(rec(person, agent=agent)) == [
        "-- route: the route call said unclear; overruled by a named person",
        "-- record agent: 0 tool call(s), 1 model call(s), 0.5 s, 0+0 tokens"]
    assert record_trace_lines(rec({"route": False, "routed": False}, routed_out=True)) == [
        "-- record agent: not run (the route call found no record data in the question, and no name or stored record "
        "value in it overrules that)"]
    assert record_trace_lines(rec({}, agent=agent))[0].startswith("-- record agent: 0 tool call(s)")


# ------------------------------------------------------------------------------------ the knowledge step (6.3)

class _FakeLLM:
    """The knowledge agent (full or lean), the route and names calls, the record agent and the merge, told apart by
    the system prompt; ``roles`` lists the calls."""

    def __init__(self, record_script=("NO_RECORDS",), route="NO_RECORDS"):
        self.record_script, self.route = list(record_script), route
        self.roles: list[str] = []
        self.knowledge_systems: list[str] = []
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def chat(self, messages, tools=None, **_):
        system = str(messages[0].get("content") or "")
        n = sum(1 for m in messages if m.get("role") == "tool")
        if system == rprompts.ROUTE_SYSTEM:
            self.roles.append("route")
            return {"content": self.route, "tool_calls": [], "usage": {}}
        if system == rprompts.NAMES_SYSTEM:
            self.roles.append("names")
            return {"content": "NONE", "tool_calls": [], "usage": {}}
        if system.startswith("You write the ONE final answer"):
            self.roles.append("merge")
            return {"content": "6 employees belong to Zorvex (`Recruiter__c.Department__c`).\n" + SOURCE,
                    "tool_calls": [], "usage": {}}
        if system.startswith("You answer questions about Salesforce RECORDS"):
            self.roles.append("record")
            step = self.record_script[min(n, len(self.record_script) - 1)]
            if isinstance(step, dict):
                return {"content": None, "tool_calls": [{"id": f"r{n}", "name": step["name"],
                                                         "arguments": step["args"]}], "usage": {}}
            return {"content": step, "tool_calls": [], "usage": {}}
        self.roles.append("lean" if system.startswith("You add FIELD MEANINGS") else "knowledge")
        self.knowledge_systems.append(system)
        return {"content": "Department__c records group employees.", "tool_calls": [], "usage": {}}


def _registry():
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


class _FakeSession:
    """The RecordSession surface pipeline.answer uses, with a fixed triage and a NO_RECORDS record answer."""

    def __init__(self, tri: Triage | None, with_last: bool = True):
        self.tri, self.asked = tri, []
        if not with_last:
            self.last_triage = None

    def route(self, text):
        return self.tri.routed

    def last_triage(self, text):
        return self.tri

    def ask(self, text, routed=None):
        self.asked.append(routed)
        return RecordAnswer(question=text, answer="", no_records=True, used_data=False, triage=self.tri.as_dict())

    def status(self):
        return STATUS


def test_a_value_overrule_keeps_the_full_knowledge_agent():
    value = Triage(routed=True, route=False, values=[_spot("zorvex")], overruled_by="value",
                   overrule_reason="the stored value 'zorvex' (Department__c.Name, cue word)")
    llm = _FakeLLM()
    o = pipeline.answer(Q_TEAM, _cfg(), _registry(), None, llm, 1, _FakeSession(value))
    assert o.mode == "full" and "lean" not in llm.roles and "knowledge" in llm.roles
    assert llm.knowledge_systems[0] == kprompts.AGENT_SYSTEM + rprompts.KNOWLEDGE_RECORDS_NOTE
    # people overrules and RECORDS routes keep the lean step, as before; so does a session without last_triage
    person = Triage(routed=True, route=False, overruled_by="person", overrule_reason="a named person")
    for session in (_FakeSession(person), _FakeSession(Triage(routed=True, route=True)),
                    _FakeSession(value, with_last=False)):
        llm = _FakeLLM()
        o = pipeline.answer(Q_TEAM, _cfg(), _registry(), None, llm, 1, session)
        assert o.mode == "lean+full" and llm.roles[0] == "lean"   # lean first; NO_RECORDS then brings the full agent


def test_a_value_overrule_end_to_end_prints_the_route_line_and_logs_the_values():
    """cli.ask over a real RecordSession on stub tools: the route call says NO_RECORDS, the stored department value
    sends the question to the record agent, the full knowledge agent answers next to it, and the trace and the JSONL
    say why."""
    tools = ValueTools({Q_TEAM: [_spot("zorvex", start=15)]})
    llm = _FakeLLM(record_script=[COUNT_CALL, "6 employees belong to Zorvex.\n" + SOURCE])
    session = RecordSession(tools, _cfg(), llm=llm)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        row = sf_assistant.cli.ask(Q_TEAM, _cfg(), _registry(), None, llm, 1, False, session, 20)
    text = out.getvalue()
    assert ("-- route: the route call said NO_RECORDS; overruled by the stored value 'zorvex' (Department__c.Name, "
            "cue word)\n-- record agent: 1 tool call(s)") in text
    j = row["jsonl"]
    assert j["knowledge_mode"] == "full" and "lean" not in llm.roles and llm.roles.count("route") == 1
    assert j["triage"]["overruled_by"] == "value" and j["triage"]["values"][0]["object"] == "Department__c"
    assert row["answer"] in ("merged", "records") and row["rq"] == 1
