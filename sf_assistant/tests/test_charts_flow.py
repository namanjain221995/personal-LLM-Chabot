"""The charts through the real answer path (the charts of 2026-11-03): ``pipeline.answer`` and ``result_row``,
``RecordSession.triage`` and ``.ask``, ``cli.print_outcome`` and the page's ``_shape``, with the ``SFA_CHARTS`` switch
off and on, against the three other switches and each path of a chart question.

Offline and model-free: the fake warehouse and its knowledge (``tests/warehouse_fixture.py``, synthetic names) and a
scripted fake of the model's client, so every request is kept as it would go on the wire; every name, value and count
is made up. The pure rules are in ``tests/test_charts.py``. Every test sets the switches its subject depends on, so this
file passes with ``SFA_CHARTS``, ``SFA_TABLES``, ``SFA_MERGE`` and ``SFA_RESOLVER`` unset or set in the shell.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import sf_assistant.pipeline as pipeline  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant import prompts as P  # noqa: E402
from sf_assistant.agent import charts  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent import tables as T  # noqa: E402
from sf_assistant.agent import timeline  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.agent.nudges import without_lines  # noqa: E402
from prompt_changes import notebook_tools  # noqa: E402   # the notebook view of the record tool list
from sf_assistant.agent.record_session import RecordSession  # noqa: E402
from sf_assistant.cli import ask as cli_ask, print_outcome, run_file  # noqa: E402
from sf_assistant.config import load_config, load_records_config  # noqa: E402
from sf_assistant.knowledge.store import knowledge_files  # noqa: E402
from sf_assistant.tools.open_records import open_records  # noqa: E402
from warehouse_fixture import FACTS, make_knowledge, make_warehouse  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
GOLDEN = json.loads((ROOT / "tests" / "golden" / "tool_lists.json").read_text(encoding="utf-8"))
W = charts._WORDS
CHART_KEYS = {"charts", "charts_switch", "charts_error", "charts_talk"}
TIME_KEYS = ("seconds", "timeline")

ASK = "show a bar chart of interviews by status"
TREND_ASK = "interviews per month graph dikhao"
PLAIN = "how many interviews are there per status?"
BARE = "now plot that"
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
DRAFT = "10 client interviews (`Interview__c`, `Interview_Status__c`).\n" + SOURCE
TABLE_DRAFT = "10 client interviews (`Interview__c`).\n\n{{table q1}}\n\n" + SOURCE
LEAN = "- `Interview__c` = client interviews.\n- `Interview_Status__c` is the status of the interview."
KNOWLEDGE = "`Interview_Status__c` holds the status."
MERGED = "10 client interviews (`Interview__c`).\n- `Interview_Status__c` is the status of the interview.\n" + SOURCE
TABLE_MERGED = ("10 client interviews (`Interview__c`).\n\n{{table q1}}\n\n"
                "- `Interview_Status__c` is the status of the interview.\n" + SOURCE)
REJECTED = "987 client interviews (`Interview__c`).\n" + SOURCE
GROUP_ARGS = {"object": "Interview__c", "group_by": "Interview_Status__c"}
TREND_ARGS = {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
              "period": "last 6 months"}
GROUP_CALL = {"name": "group_records", "args": GROUP_ARGS}
TREND_CALL = {"name": "trend_records", "args": TREND_ARGS}
BAD_TREND = {"name": "trend_records", "args": {k: v for k, v in TREND_ARGS.items() if k != "period"}}
COUNT_CALL = {"name": "count_records", "args": {"object": "Interview__c"}}
LIST_CALL = {"name": "list_records", "args": {"object": "Interview__c", "fields": ["Name"], "limit": 2}}
LIST_DRAFT = "2 of the 10 client interviews (`Interview__c`): I-0007 and I-0004.\n" + SOURCE


# ------------------------------------------------------------------------------------------------ the fake world
def _cfg():
    return load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")   # never reached: the client is a fake


def _h(x) -> str:
    return hashlib.sha256(json.dumps(x, ensure_ascii=False).encode("utf-8")).hexdigest()


def _open_world(tmp: Path, resolver: bool) -> NS:
    """The fake warehouse with its record tools and knowledge registry. The status is pinned: a request holds the data's
    age, and two runs of one test must not differ by the minute they ran in."""
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        mp.setenv("SFA_RESOLVER", "on" if resolver else "off")
        words = tmp / "words.txt"   # a made-up stand-in for the English word list (generic words only)
        words.write_text("\n".join("show bar chart of by status the and how many records list count open per month "
                                   "interview interviews".split()) + "\n", encoding="utf-8")
        mp.setenv("SFA_LEXICON", str(words))
        wh, kn = make_warehouse(tmp), make_knowledge(tmp)
        settings = C.load_settings(records=False, components_dir=str(tmp / "none"))
        _cfg_of_build, _names, registry, system, _llm = pipeline.build(knowledge_files(str(kn)), settings)
        tools = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn),
                             settings.resolver)
    status = tools.status()
    tools.status = lambda: status
    direct = tmp / "direct.duckdb"
    shutil.copy(wh, direct)
    return NS(tools=tools, registry=registry, system=system, direct=direct)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = _open_world(tmp_path_factory.mktemp("charts_flow"), resolver=False)
    yield w
    w.tools.close()


@pytest.fixture(scope="module")
def world_on(tmp_path_factory):
    """The same, with the spelling help on both sides."""
    w = _open_world(tmp_path_factory.mktemp("charts_flow_on"), resolver=True)
    yield w
    w.tools.close()


@pytest.fixture(autouse=True)
def _switches(monkeypatch):
    """The default path, with charts on; a test changes the switch it is about. No timeline is left open by another
    test."""
    monkeypatch.setenv("SFA_CHARTS", "on")
    monkeypatch.setenv("SFA_TABLES", "model")
    monkeypatch.setenv("SFA_MERGE", "code")
    monkeypatch.setenv("SFA_RESOLVER", "off")
    timeline.end()
    yield
    timeline.end()


# ------------------------------------------------------------------------------------------------ the scripted model
def _completion(content=None, calls=()):
    tcs = [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)]
    msg = NS(content=content, tool_calls=tcs or None)
    return NS(choices=[NS(message=msg, finish_reason="tool_calls" if tcs else "stop")],
              usage=NS(prompt_tokens=100, completion_tokens=10))


class Model:
    """``chat.completions.create`` of a fake OpenAI client, told apart by the system prompt: the route call (``route``),
    the names call (``names``), the merge call (``merged``), the record agent (``steps`` in order, a dict is one tool
    call and a str a reply, the last one repeating; a forced answer gets ``forced``) and the knowledge steps (one
    search, then ``lean`` or ``knowledge``). ``requests`` keeps every request by role, as sent then."""

    def __init__(self, *steps, route="RECORDS", names="NONE", merged=MERGED, lean=LEAN, knowledge=KNOWLEDGE, forced=""):
        self.steps, self.route, self.names, self.merged = steps or ("NO_RECORDS",), route, names, merged
        self.lean, self.knowledge, self.forced = lean, knowledge, forced
        roles = ("route", "names", "lean", "knowledge", "record", "merge")
        self.requests: dict[str, list[dict]] = {role: [] for role in roles}
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
        return "lean" if system.startswith(rprompts.KNOWLEDGE_LEAN_SYSTEM[:30]) else "knowledge"

    def _create(self, **kwargs):
        role = self._role(str(kwargs["messages"][0].get("content") or ""))
        with self._lock:
            n = len(self.requests[role])
            self.requests[role].append(json.loads(json.dumps(kwargs, default=str)))
        if role == "route":
            return _completion(self.route)
        if role == "names":
            return _completion(self.names)
        if role == "merge":
            return _completion(self.merged)
        if role == "record":
            if not kwargs.get("tools"):
                return _completion(self.forced)
            step = self.steps[min(n, len(self.steps) - 1)]
            return _completion(calls=[(step["name"], step["args"])]) if isinstance(step, dict) else _completion(step)
        if kwargs.get("tools") and not any(m.get("role") == "tool" for m in kwargs["messages"]):
            return _completion(calls=[("search_all_knowledge", {"query": "interview status"})])
        return _completion(self.lean if role == "lean" else self.knowledge)

    def rounds(self) -> dict[str, int]:
        return {role: len(reqs) for role, reqs in self.requests.items()}

    def total(self) -> int:
        return sum(self.rounds().values())


def run(world, model, question=ASK, **kw):
    """The real answer and row of ``question`` through a new session over the world's tools."""
    cfg = _cfg()
    llm = LLM(cfg, client=model)
    session = RecordSession(world.tools, cfg, llm=llm)
    o = pipeline.answer(question, cfg, world.registry, world.system, llm, 1, session, **kw)
    return o, pipeline.result_row(o, session)


def run_off(world, monkeypatch, make_model, question=ASK, **kw):
    """The same question with the switch off (for comparison); the switch goes back to what the test had."""
    with monkeypatch.context() as mp:
        mp.setenv("SFA_CHARTS", "off")
        model = make_model()
        return (model, *run(world, model, question, **kw))


def _canon(x):
    """The JSONL record without its times, at any depth."""
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in x.items() if k not in TIME_KEYS and not str(k).endswith("_seconds")}
    return [_canon(v) for v in x] if isinstance(x, list) else x


def _svg(item: dict) -> str:
    return base64.b64decode(item["svg_b64"]).decode("utf-8")


def _said(item: dict) -> list[str]:
    return ["".join(el.itertext()).strip() for el in ET.fromstring(_svg(item)).iter()
            if el.tag.rsplit("}", 1)[-1] == "text"]


def _drawn(item: dict) -> dict:
    """The chart's first series, as label -> value, read from the item's rows."""
    chart = item["chart"]
    return {row[chart["x_key"]]: row.get(chart["y_keys"][0]) for row in item["data"]}


def _sql(world, statement: str) -> list[tuple]:
    """Separate SQL on a copy of the fake warehouse: the open record tools hold the file with their own settings."""
    import duckdb

    con = duckdb.connect(str(world.direct), read_only=True)
    try:
        return con.execute(statement).fetchall()
    finally:
        con.close()


def _stages(line: dict) -> set[str]:
    """The stages of a timeline; the threads may order them differently from one run to the next."""
    return {e["stage"] for e in line["entries"]}


def _web():
    """The page's pipeline module under a name of its own: ``tests/test_web.py`` keeps ``pipeline`` for itself."""
    spec = importlib.util.spec_from_file_location("sfa_web_pipeline_flow", ROOT / "web" / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shape(row, session, mode="merged", question=ASK) -> dict:
    web = _web()
    page = web.Pipeline.__new__(web.Pipeline)
    page._status = {}
    return page._shape(question, mode, row, "", session, False)


# ----------------------------------------------------------------------------------------------- R3: off, and elsewhere
@pytest.mark.parametrize("raw", ["off", "  OFF "])
def test_off_chart_question_same_requests(world, monkeypatch, raw):
    monkeypatch.setenv("SFA_CHARTS", raw)
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model)
    assert model.rounds()["record"] == 2 and o.kind == "merged"
    for req in model.requests["record"]:
        assert req["messages"][0]["content"] == rprompts.render_records_system(o.status)   # byte for byte
        assert _h(notebook_tools(req["tools"])) == GOLDEN["records"]["openai_tools_sha256"]   # the catalog as ever
    assert not CHART_KEYS & set(row["jsonl"]) and "charts" not in row
    assert (o.charts, o.charts_switch, o.charts_error, o.charts_talk) == ([], "", "", False)
    assert o.status_lines == [] and "No chart" not in o.final and o.final.endswith(SOURCE)


def test_off_imports_no_charts_module(tmp_path):
    """Section 12.1: with the switch off a chart question imports nothing of the charts, on any surface."""
    script = textwrap.dedent(f"""
        import contextlib, datetime as dt, io, json, os, sys
        from pathlib import Path
        from types import SimpleNamespace as NS
        sys.path.insert(0, {str(ROOT / "tests")!r})
        import sf_assistant.pipeline as pipeline
        from sf_assistant.agent.record_session import RecordSession
        from sf_assistant.cli import print_outcome
        from sf_assistant.config import load_config
        from warehouse_fixture import open_fixture
        class Fake:
            route = ""
            def chat(self, messages, tools=None, **_):
                if not tools:
                    return {{"content": self.route, "tool_calls": []}}
                if not any(m["role"] == "tool" for m in messages):
                    return {{"content": None, "tool_calls": [{{"id": "c1", "name": "group_records", "arguments":
                            {{"object": "Interview__c", "group_by": "Interview_Status__c"}}}}]}}
                return {{"content": "10 client interviews (`Interview__c`).\\nSource: fake copy.", "tool_calls": []}}
        cfg = load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")
        tools = open_fixture(Path({str(tmp_path)!r}))
        kinds = []
        for Fake.route in ("RECORDS", "NO_RECORDS"):
            session = RecordSession(tools, cfg, llm=Fake())
            o = pipeline.answer({ASK!r}, cfg, None, None, Fake(), 1, session, records_only=True)
            row = pipeline.result_row(o, session)
            with contextlib.redirect_stdout(io.StringIO()):
                print_outcome(o, session, False)
            kinds.append(o.kind)
            assert not [k for k in row["jsonl"] if k.startswith("charts")]
        tools.close()
        print(json.dumps({{"kinds": kinds, "imported": "sf_assistant.agent.charts" in sys.modules}}))
    """)
    env = dict(os.environ, SFA_CHARTS="off", SFA_RESOLVER="off", OKEVAL_BASE_URL="http://127.0.0.1:9/v1",
               OKEVAL_API_KEY="test-key")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout.strip().splitlines()[-1]) == {"kinds": ["records", "none"], "imported": False}


def test_no_records_run_switch_on_loads_no_record_module(tmp_path):
    """With the switch on, every answer loads the charts module, also in a records-off run (off, not even that:
    test_no_records_unset_loads_charts_off_does_not). Any other question still loads no engine module, no duckdb and no warehouse, and a
    chart question gets the ``records_off`` line."""
    fixture = str(ROOT / "tests" / "fixtures" / "Internal_Interview__c.json")
    script = textwrap.dedent(f"""
        import contextlib, io, json, sys
        import sf_assistant.cli as mod
        import sf_assistant.pipeline
        from sf_assistant.agent.llm import LLM
        from sf_assistant.agent.mock_llm import MockClient
        sf_assistant.pipeline.LLM = lambda cfg: LLM(cfg, client=MockClient())
        out = {{}}
        for name, question in (("plain", "What values can Status__c take?"), ("chart", {ASK!r})):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.main(["--no-records", "--knowledge", {fixture!r}, question])
            out[name] = [code, buf.getvalue(), sorted(m for m in sys.modules if m.startswith("sf_assistant.engine"))]
        print(json.dumps({{**out, "charts": "sf_assistant.agent.charts" in sys.modules,
                           "duckdb": "duckdb" in sys.modules}}))
    """)
    env = dict(os.environ, OKEVAL_BASE_URL="http://127.0.0.1:9/v1", OKEVAL_API_KEY="test-key", SFA_CHARTS="on")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["charts"] is True and data["duckdb"] is False
    assert data["plain"][0] == 0 and "No chart" not in data["plain"][1] and data["plain"][2] == []
    # the line goes through agent.nudges, whose only engine import holds no duckdb and no warehouse
    assert set(data["chart"][2]) <= {"sf_assistant.engine", "sf_assistant.engine.types"}
    assert data["chart"][0] == 0 and W["records_off"] in data["chart"][1]


@pytest.mark.parametrize("question, steps, options, env, route", [
    (PLAIN, (GROUP_CALL, DRAFT), {}, {}, "RECORDS"),
    (PLAIN, (GROUP_CALL, DRAFT), {"records_only": True}, {}, "RECORDS"),
    (PLAIN, (GROUP_CALL, DRAFT), {"lean_knowledge": False}, {}, "RECORDS"),
    (PLAIN, (GROUP_CALL, TABLE_DRAFT), {}, {"SFA_TABLES": "code"}, "RECORDS"),
    (PLAIN, (GROUP_CALL, DRAFT), {}, {"SFA_MERGE": "model"}, "RECORDS"),
    ("what does the status field of an interview mean?", ("NO_RECORDS",), {}, {}, "NO_RECORDS"),
    # a chart word in a record answer: drop_talk is the one step on the switch-on path that rewrites record text
    (PLAIN, (GROUP_CALL, "The status chart lists them. " + DRAFT), {}, {}, "RECORDS"),
    (PLAIN, (GROUP_CALL, "The status chart lists them. " + DRAFT), {"records_only": True}, {}, "RECORDS"),
], ids=["lean", "records only", "full knowledge", "rows by code", "merge call", "design", "chart word, lean",
        "chart word, records only"])
def test_on_non_chart_question_identical(world, monkeypatch, question, steps, options, env, route):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    off_model, off, off_row = run_off(world, monkeypatch, lambda: Model(*steps, route=route), question, **options)
    model = Model(*steps, route=route)
    o, row = run(world, model, question, **options)
    assert model.requests == off_model.requests   # every request, round by round
    assert _canon(row["jsonl"]) == _canon(off_row["jsonl"]) and not CHART_KEYS & set(row["jsonl"])
    assert (o.final, o.body, o.kind, o.mode) == (off.final, off.body, off.kind, off.mode) and o.charts == []


@pytest.mark.parametrize("tables", ["model", "code"])
def test_on_chart_note_once_every_round(world, monkeypatch, tables):
    monkeypatch.setenv("SFA_TABLES", tables)
    steps = (BAD_TREND, TREND_CALL, "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    off_model, _off, _off_row = run_off(world, monkeypatch, lambda: Model(*steps), TREND_ASK)
    model = Model(*steps)
    o, _row = run(world, model, TREND_ASK)
    record = model.requests["record"]
    assert len(record) == 3 == len(off_model.requests["record"])
    for req, off_req in zip(record, off_model.requests["record"]):
        system = req["messages"][0]["content"]
        assert system.count(charts.RECORDS_CHART_NOTE) == 1 and system == off_req["messages"][0]["content"] + \
            charts.RECORDS_CHART_NOTE
        assert system.startswith(rprompts.render_records_system(o.status))   # appended, never inside the template
        assert req["messages"][1:] == off_req["messages"][1:]                # the same user message and results
        assert req["tools"] == off_req["tools"] and             _h(notebook_tools(req["tools"])) == GOLDEN["records"]["openai_tools_sha256"]
    if tables == "code":
        assert T.RECORDS_TABLES_NOTE + charts.RECORDS_CHART_NOTE in record[0]["messages"][0]["content"]
    others = [r for role, reqs in model.requests.items() if role != "record" for r in reqs]
    assert others and not any(charts.RECORDS_CHART_NOTE in json.dumps(r, ensure_ascii=False) for r in others)


# ------------------------------------------------------------------------------------------------ R9: every mix
@pytest.mark.parametrize("path", ["merged accepted", "merge rejected", "records only"])
@pytest.mark.parametrize("resolver", ["off", "on"])
@pytest.mark.parametrize("merge_by", ["code", "model"])
@pytest.mark.parametrize("tables", ["model", "code"])
def test_matrix_8_switches(world, world_on, monkeypatch, tables, merge_by, resolver, path):
    monkeypatch.setenv("SFA_TABLES", tables)
    monkeypatch.setenv("SFA_MERGE", merge_by)
    monkeypatch.setenv("SFA_RESOLVER", resolver)
    w = world_on if resolver == "on" else world
    draft = TABLE_DRAFT if tables == "code" else DRAFT
    merged = REJECTED if path == "merge rejected" else TABLE_MERGED if tables == "code" else MERGED
    options = ({"records_only": True} if path == "records only" else
               {"lean_knowledge": False} if path == "merge rejected" and merge_by == "code" else {})   # a merge call

    def script():
        return Model(GROUP_CALL, draft, merged=merged)

    off_model, _off, off_row = run_off(w, monkeypatch, script, ASK, **options)
    model = script()
    o, row = run(w, model, ASK, **options)
    j = row["jsonl"]
    assert model.rounds() == off_model.rounds()
    assert o.kind == ("merged" if path == "merged accepted" else "records")
    assert o.merge_by == (None if path == "records only" else "code" if path == "merged accepted" and merge_by == "code"
                          else "model")
    assert (o.m.rejected_numbers == ["987"]) if path == "merge rejected" else True
    (item,) = o.charts
    assert item["chart"]["type"] == "bar" and item["query_id"] == "q1" and _svg(item).startswith("<svg")
    assert sum(_drawn(item).values()) == FACTS["interviews"] and item["rows_in_answer"] is (tables == "code")
    for text in (j["answer"], j["answer_body"], j["record_answer"], o.final, o.body):
        assert "{{chart" not in text and "{{table" not in text, text
    assert row["charts"] == [item] and "svg_b64" in row["charts"][0]
    assert [set(x) for x in j["charts"]] == [set(item) - {"svg_b64"}] and "svg_b64" not in json.dumps(j)
    assert o.body == off_row["jsonl"]["answer_body"] and j["answer"] == off_row["jsonl"]["answer"]   # no line added
    assert not {"charts_error", "charts_switch", "charts_talk"} & set(j)


# ------------------------------------------------------------------------------------------------ the paths of map 5.2
def test_map_cases_D_F_G_J_K(world, monkeypatch):
    """D: a NO_RECORDS route reads as a data question; only without that overrule is the evidence empty."""
    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model)
    assert o.mode == "full" and o.kind == "merged" and [e.tool for e in o.rec.evidence] == ["group_records"]
    assert row["jsonl"]["triage"]["overruled_by"] == "chart" and o.charts and o.status_lines == []
    with monkeypatch.context() as mp:
        mp.setattr(charts, "drawable_ask", lambda question: False)
        model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
        o, row = run(world, model)
        assert o.rec.routed_out and o.rec.evidence == [] and o.kind == "knowledge" and model.rounds()["record"] == 0
        assert o.charts == [] and o.status_lines == [W["no_data"]] and o.final == KNOWLEDGE + "\n" + W["no_data"]

    # F: the record agent replies NO_RECORDS on a RECORDS route: the second knowledge agent as today, then the line
    model = Model("NO_RECORDS")
    o, row = run(world, model)
    assert o.rec.evidence == [] and o.kind == "knowledge" and o.mode == "lean+full" and o.merge_by is None
    assert model.rounds() == {"route": 1, "names": 1, "lean": 2, "knowledge": 2, "record": 2, "merge": 0}
    assert o.charts == [] and o.status_lines == [W["no_data"]] and o.body == KNOWLEDGE

    # G: the names call returns the verb: one nudge and one more round, then the chart
    model = Model(TREND_CALL, "10 interviews over the months (`Interview__c`).\n" + SOURCE, names="dikhao")
    o, row = run(world, model, TREND_ASK)
    assert model.rounds()["record"] == 3 and len(o.rec.nudges) == 1 and o.kind == "merged" and o.merge_by == "code"
    assert [(e.query_id, e.tool, e.ok) for e in o.rec.evidence] == [("q1", "trend_records", True)]
    assert o.charts[0]["chart"]["type"] == "line" and o.status_lines == []

    # J: the agent refuses twice and calls no tool: the record step failed, and the answer says so plainly instead of
    # showing the lean bullets in place of the figures (2026-11-16); the record step read no records, so no note
    # follows (review of 2026-11-16: only the objects of good data calls keep one), then the line
    model = Model("I cannot draw charts here.")
    o, row = run(world, model)
    assert model.rounds() == {"route": 1, "names": 1, "lean": 2, "knowledge": 0, "record": 2, "merge": 0}
    failed = pipeline.RECORDS_FAILED_ANSWER
    assert o.rec.evidence == [] and o.kind == "none" and o.merge_by is None and o.body == failed
    assert o.charts == [] and o.status_lines == [W["no_data"]] and o.final == failed + "\n" + W["no_data"]

    # K: a failed trend, then fixed: the failed call is skipped
    model = Model(BAD_TREND, TREND_CALL, "10 interviews over the months (`Interview__c`).\n" + SOURCE)
    o, row = run(world, model, TREND_ASK)
    assert model.rounds()["record"] == 3 and o.kind == "merged" and o.merge_by == "code"
    assert [(e.query_id, e.ok) for e in o.rec.evidence] == [("q1", False), ("q2", True)]
    assert o.charts[0]["query_id"] == "q2" and o.charts[0]["chart"] and o.status_lines == []


def test_only_failed_calls_get_their_line(world):
    model = Model(BAD_TREND, "NO_RECORDS")
    o, _row = run(world, model, TREND_ASK)
    assert [e.ok for e in o.rec.evidence] == [False] and o.charts == [] and o.status_lines == [W["failed"]]
    assert W["failed"] in o.final and W["failed"] not in o.body


def test_records_off_gets_the_line(world):
    """No session: the knowledge answer and the ``records_off`` line, with no chart."""
    cfg = _cfg()
    o = pipeline.answer(ASK, cfg, world.registry, world.system, LLM(cfg, client=Model()), 1, None)
    row = pipeline.result_row(o, None)
    assert o.rec is None and o.kind == "knowledge" and o.charts == [] and o.status_lines == [W["records_off"]]
    assert o.body == KNOWLEDGE and o.final == KNOWLEDGE + "\n" + W["records_off"]
    assert "charts" not in row["jsonl"] and row["jsonl"]["lookup_footer"] is False


def test_several_results_draw_the_last_chartable_one(world):
    """Section 5: the last chartable result with the ``several`` note; a count after a group leaves the group drawn."""
    o, _row = run(world, Model(GROUP_CALL, TREND_CALL, DRAFT), ASK)
    (item,) = o.charts
    assert item["query_id"] == "q2" and item["tool"] == "trend_records"
    assert charts._say("several", COUNT=2) in item["notes"] and o.status_lines == []
    o, _row = run(world, Model(GROUP_CALL, COUNT_CALL, DRAFT), ASK)
    (item,) = o.charts
    assert item["query_id"] == "q1" and item["tool"] == "group_records" and not item["notes"]


# ------------------------------------------------------------------------------------------------ the reason line
def test_line_before_source(world):
    o, _row = run(world, Model(LIST_CALL, LIST_DRAFT), ASK, records_only=True)
    lines = o.final.split("\n")
    assert lines[-1] == SOURCE and lines[-2] == W["list"]
    body, source = _web().split_source(o.final)
    assert source == SOURCE and body.endswith(W["list"])
    assert W["list"] not in o.body and o.body == LIST_DRAFT


def test_body_footer_read_as_right(world, world_on, monkeypatch):
    # a line alone: the body as with the switch off, no footer and no Read-as line read into it
    steps = (LIST_CALL, LIST_DRAFT)
    _m, off, off_row = run_off(world, monkeypatch, lambda: Model(*steps), ASK, records_only=True)
    o, row = run(world, Model(*steps), ASK, records_only=True)
    assert o.body == off.body and row["jsonl"]["answer_body"] == off_row["jsonl"]["answer_body"]
    assert row["jsonl"]["lookup_footer"] is False is off_row["jsonl"]["lookup_footer"]
    assert o.status_lines == [W["list"]] == row["jsonl"]["status_lines"]
    assert without_lines(o.final, [W["list"]]) == off.final

    # a real lookup footer and a line: the flag stays true, as with the switch off, and the line goes after the footer
    person = {"name": "count_records", "args": {"object": "Internal_Interview__c", "filters": [
        {"field": "Interviewer__c", "op": "eq", "value": "Echo One"}]}}
    text = "1 internal interview.\n" + SOURCE
    question = "bar chart of the internal interviews of Echo One"
    _m, off, off_row = run_off(world, monkeypatch, lambda: Model(person, text), question, records_only=True)
    o, row = run(world, Model(person, text), question, records_only=True)
    assert off_row["jsonl"]["lookup_footer"] is True and row["jsonl"]["lookup_footer"] is True
    assert o.body == off.body == text and o.status_lines == [W["one_row"]]
    assert o.final.split("\n") == [off.final.split("\n")[0], off.final.split("\n")[1], W["one_row"], SOURCE]
    assert off.final.split("\n")[1].startswith("Lookup used") and "Lookup used" not in o.body

    # a real lookup footer and a chart: no line at all
    person["args"]["filters"][0]["value"] = "Delta Person"
    text = "3 internal interviews.\n" + SOURCE
    question = "bar chart of the internal interviews of Delta Person"
    _m, off, off_row = run_off(world, monkeypatch, lambda: Model(person, text), question, records_only=True)
    o, row = run(world, Model(person, text), question, records_only=True)
    assert row["jsonl"]["lookup_footer"] is True is off_row["jsonl"]["lookup_footer"]
    assert o.charts[0]["chart"] and o.final == off.final and o.body == off.body == text

    # a lean bullet about the lookup keeps the footer, as the same bullet without its chart talk does (section 9)
    person["args"]["filters"][0]["value"] = "Delta Person"
    text = "3 internal interviews (`Internal_Interview__c`).\n" + SOURCE
    question = "bar chart of the internal interviews of Delta Person"
    for lean in ("- `Interviewer__c` is the interviewer of the internal interview.",
                 "- `Interviewer__c` is the interviewer of the internal interview. It appears on the chart."):
        _m, off, off_row = run_off(world, monkeypatch, lambda: Model(person, text, lean=lean), question)
        assert off_row["jsonl"]["lookup_footer"] is True
        assert "Lookup used: `Internal_Interview__c.Interviewer__c`" in off.final
        o, row = run(world, Model(person, text, lean=lean), question)
        assert row["jsonl"]["lookup_footer"] is True and "Lookup used: `Internal_Interview__c.Interviewer__c`" in o.final
        assert "It appears on the chart" not in o.final

    # the spelling help: a correction gets its Read-as line and the flag; the chart's line never sets the flag
    monkeypatch.setenv("SFA_RESOLVER", "on")
    o, row = run(world_on, Model(LIST_CALL, LIST_DRAFT), "bar chart of intervews by status", records_only=True)
    assert row["jsonl"]["read_as_line"] is True
    assert o.final.split("\n")[1:] == [W["list"], "Read as: 'intervews' as Interview.", SOURCE]
    assert o.body == LIST_DRAFT and row["jsonl"]["lookup_footer"] is False
    o, row = run(world_on, Model(LIST_CALL, LIST_DRAFT), ASK, records_only=True)
    assert row["jsonl"]["read_as_line"] is False and "Read as:" not in o.final and W["list"] in o.final
    assert row["jsonl"]["lookup_footer"] is False
    # a span no call and no sentence used: the reason line's own words ("such as by status") never name it
    question = "bar chart of interviews by statsu"
    _m, off, off_row = run_off(world_on, monkeypatch, lambda: Model(LIST_CALL, LIST_DRAFT), question, records_only=True)
    assert off_row["jsonl"]["read_as_line"] is False and "Read as:" not in off.final
    o, row = run(world_on, Model(LIST_CALL, LIST_DRAFT), question, records_only=True)
    assert row["jsonl"]["read_as_line"] is False and "Read as:" not in o.final and W["list"] in o.final


def test_empty_answer_gets_line(world):
    """Records mode, NO_RECORDS: no answer at all, so the line stands alone, and the page shows it."""
    model = Model("NO_RECORDS")
    o, row = run(world, model, ASK, records_only=True)
    j = row["jsonl"]
    assert o.kind == "none" and o.body == "" and o.final == W["no_data"] == o.shown == j["answer"]
    assert j["answer_body"] == "" and j["lookup_footer"] is False and j["status_lines"] == [W["no_data"]]
    out = _shape(row, object(), "records")
    assert out["answer"] == W["no_data"] and out["kind"] == "none"


# ------------------------------------------------------------------------------------------------ decision 4
def test_overrule_keeps_full_knowledge(world, monkeypatch):
    off_model, off, off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, DRAFT, route="NO_RECORDS"))
    assert off_model.rounds()["record"] == 0 and off.kind == "knowledge"
    assert "overruled_by" not in off_row["jsonl"]["triage"]

    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model)
    tri = row["jsonl"]["triage"]
    assert tri["route"] is False and tri["routed"] is True and tri["overruled_by"] == "chart"
    assert tri["overrule_reason"] == charts.OVERRULE_REASON
    assert o.mode == "full" and o.kind == "merged" and o.merge_by == "model" and o.m.merged and o.charts
    assert model.rounds() == {"route": 1, "names": 1, "lean": 0, "knowledge": 2, "record": 2, "merge": 1}

    # NO_RECORDS from the record agent after the overrule: the full answer that ran alongside, and no second agent
    model = Model("NO_RECORDS", route="NO_RECORDS")
    o, _row = run(world, model)
    assert o.mode == "full" and o.kind == "knowledge" and o.merge_by is None and o.body == KNOWLEDGE
    assert model.rounds() == {"route": 1, "names": 1, "lean": 0, "knowledge": 2, "record": 2, "merge": 0}
    assert o.status_lines == [W["no_data"]]


class FailingMerge(Model):
    """The model's client, with the merge call raising."""

    def _create(self, **kwargs):
        if self._role(str(kwargs["messages"][0].get("content") or "")) == "merge":
            raise RuntimeError("the merge model is down")
        return super()._create(**kwargs)


@pytest.mark.parametrize("make_model", [
    lambda **kw: Model(GROUP_CALL, DRAFT, merged=REJECTED, **kw), lambda **kw: FailingMerge(GROUP_CALL, DRAFT, **kw),
], ids=["merge rejected", "merge failed"])
def test_overrule_keeps_its_design_answer_without_a_merge(world, make_model):
    """Decision 4: a question the route calls design never loses its design answer. A merge that is rejected or fails
    falls back to the record answer, and the knowledge answer stays in the shown text, before it."""
    question = "which dashboards have a bar chart"
    design = "The recruiting dashboard has a bar chart."
    o, row = run(world, make_model(route="NO_RECORDS", knowledge=design), question)
    assert row["jsonl"]["triage"]["overruled_by"] == "chart"
    assert o.mode == "full" and o.kind == "records" and o.m is not None and not o.m.merged
    assert o.final.startswith(design + "\n\n") and o.body.startswith(design + "\n\n") and o.body.endswith(DRAFT)
    assert o.final.endswith(SOURCE) and row["jsonl"]["record_answer"] == DRAFT
    assert o.charts and o.charts[0]["chart"] and o.status_lines == []
    assert "charts_talk" not in row["jsonl"]   # a design answer keeps its chart sentence: nothing was removed
    assert _shape(row, object(), "merged", question)["answer"].startswith(design)


def test_overrule_with_no_design_answer_and_other_overrules_stay_as_they_are(world, monkeypatch):
    """Only a chart overrule keeps it, and only with an answer to keep: on a RECORDS route the merge's fallback is the
    record answer, as with the switch off."""
    question = "which dashboards have a bar chart"
    o, _row = run(world, Model(GROUP_CALL, DRAFT, merged=REJECTED), question, lean_knowledge=False)
    assert o.kind == "records" and KNOWLEDGE not in o.final and o.body == DRAFT
    o, _row = run(world, Model(GROUP_CALL, DRAFT, route="NO_RECORDS", knowledge="", merged=REJECTED), question)
    assert o.kind == "records" and o.body == DRAFT
    o, _row = run(world, Model(GROUP_CALL, DRAFT, route="NO_RECORDS", knowledge="**Summary:**", merged=REJECTED),
                  question)
    assert o.kind == "records" and o.body == DRAFT   # a label alone is no answer to keep


def test_records_route_stays_lean(world):
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model)
    assert "overruled_by" not in row["jsonl"]["triage"] and row["jsonl"]["triage"]["routed"] is True
    assert o.mode == "lean" and o.merge_by == "code" and o.charts
    assert model.rounds() == {"route": 1, "names": 1, "lean": 2, "knowledge": 0, "record": 2, "merge": 0}


def test_an_unclear_route_needs_no_overrule(world):
    """An unclear route stays None, as for a value: the record agent runs anyway, next to the full knowledge agent."""
    model = Model(GROUP_CALL, DRAFT, route="MAYBE")
    o, row = run(world, model)
    tri = row["jsonl"]["triage"]
    assert tri["route"] is None and tri["routed"] is None and "overruled_by" not in tri
    assert o.mode == "full" and o.merge_by == "model" and o.charts and model.rounds()["record"] == 2


def test_org_chart_no_overrule(world):
    question = "show the org chart of the sales team"
    assert charts.chart_ask(question) is None
    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model, question)
    assert row["jsonl"]["triage"]["routed"] is False and "overruled_by" not in row["jsonl"]["triage"]
    assert model.rounds()["record"] == 0 and o.kind == "knowledge" and o.charts == [] and o.status_lines == []
    assert not CHART_KEYS & set(row["jsonl"])
    # the same question on a RECORDS route gets no note either
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model, question)
    assert charts.RECORDS_CHART_NOTE not in model.requests["record"][0]["messages"][0]["content"] and o.charts == []


def test_bare_no_overrule(world):
    assert charts.chart_ask(BARE).bare and not charts.drawable_ask(BARE)
    cfg = _cfg()
    session = RecordSession(world.tools, cfg, llm=LLM(cfg, client=Model(route="NO_RECORDS")))
    tri = session.triage(BARE)
    assert tri.routed is False and tri.overruled_by is None


def test_design_answer_keeps_chart_sentences(world):
    """Section 12.4: kind ``knowledge`` and a full-mode merge keep their sentences; a record answer does not."""
    question = "which dashboards have a bar chart"
    design = "The recruiting dashboard has a bar chart."
    o, row = run(world, Model("NO_RECORDS", route="NO_RECORDS", knowledge=design), question)
    assert o.kind == "knowledge" and o.mode == "full" and o.body == design and o.final == design + "\n" + W["no_data"]
    assert "charts_talk" not in row["jsonl"]

    merged = "10 client interviews (`Interview__c`). " + design + "\n" + SOURCE
    o, row = run(world, Model(GROUP_CALL, DRAFT, route="NO_RECORDS", knowledge=design, merged=merged), question)
    assert o.kind == "merged" and o.mode == "full" and o.m.merged and design in o.body and design in o.final
    assert "charts_talk" not in row["jsonl"]

    o, row = run(world, Model(GROUP_CALL, design + "\n" + DRAFT), question, records_only=True)
    assert o.kind == "records" and design not in o.body and row["jsonl"]["charts_talk"] is True


@pytest.mark.parametrize("draft, options, talk", [
    (DRAFT, {}, False),
    ("Here is the chart. " + DRAFT, {}, True),
    ("Here is the chart.\n\n" + DRAFT, {"records_only": True}, True),
    (DRAFT.replace("10 client", "10  client"), {"records_only": True}, False),   # a double space is no talk
    ("The chart shows 10 client interviews (`Interview__c`).\n" + SOURCE, {"records_only": True}, False),   # a digit
], ids=["lean none", "lean dropped", "records dropped", "double space", "digit stays"])
def test_charts_talk_only_when_dropped(world, monkeypatch, draft, options, talk):
    o, row = run(world, Model(GROUP_CALL, draft), **options)
    assert o.charts_talk is talk and row["jsonl"].get("charts_talk", False) is talk
    assert ("charts_talk" in row["jsonl"]) is talk and "Here is the chart" not in o.final
    assert "Here is the chart" not in row["jsonl"]["record_answer"]
    # off: the key never comes, and the sentence stays
    _m, off, off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, draft), ASK, **options)
    assert "charts_talk" not in off_row["jsonl"] and ("Here is the chart" in off.rec.answer) is talk


def test_a_lean_bullet_that_talks_about_a_chart_goes_too(world):
    """Section 12.4: ``make`` cleans a lean merge by code as well, whose bullets the record answer's cleaning never
    saw."""
    lean = LEAN.replace("is the status of the interview", "is shown on the status chart")
    o, row = run(world, Model(GROUP_CALL, DRAFT, lean=lean))
    assert o.kind == "merged" and o.mode == "lean" and o.merge_by == "code" and o.rec.answer == DRAFT
    assert "status chart" not in o.final and "status chart" not in o.body and "`Interview__c` = client" in o.final
    assert o.charts_talk is True and row["jsonl"]["charts_talk"] is True


@pytest.mark.parametrize("tables, options", [("model", {}), ("code", {}), ("model", {"records_only": True}),
                                             ("code", {"records_only": True}), ("code", {"lean_knowledge": False})],
                         ids=["lean", "lean rows by code", "records", "records rows by code", "full rows by code"])
def test_a_typed_marker_never_reaches_the_user(world, monkeypatch, tables, options):
    """R9: no chart marker exists, so one the model types itself is only talk, on every path."""
    monkeypatch.setenv("SFA_TABLES", tables)
    draft = (TABLE_DRAFT if tables == "code" else DRAFT).replace("\nSource", "\n\n{{chart q1 bar}}\n\nSource")
    assert "{{chart" in draft
    merged = (TABLE_MERGED if tables == "code" else MERGED)
    o, row = run(world, Model(GROUP_CALL, draft, merged=merged), **options)
    j = row["jsonl"]
    for text in (j["answer"], j["answer_body"], j["record_answer"]):
        assert "{{" not in text, text
    assert o.charts and o.charts[0]["chart"] and j["charts_talk"] is True


# ------------------------------------------------------------------------------------------------ the bare ask
@pytest.mark.parametrize("question", [BARE, "graph dikhao"])
@pytest.mark.parametrize("options", [{}, {"records_only": True}, {"lean_knowledge": False}],
                         ids=["merged", "records only", "full knowledge"])
def test_bare_no_model_call_every_surface(world, question, options):
    line = charts.bare_line()
    model = Model(GROUP_CALL, DRAFT)
    cfg = _cfg()
    llm = LLM(cfg, client=model)
    session = RecordSession(world.tools, cfg, llm=llm)
    for s in (session, None):
        o = pipeline.answer(question, cfg, world.registry, world.system, llm, 1, s, **options)
        assert model.total() == 0
        assert o.final == line == o.shown and o.body == "" and o.kind == "none" and o.status_lines == [line]
        assert o.charts == [] and o.mode is None and o.r is None and (o.rec is None) is (s is None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_outcome(o, s, False)
        assert line in buf.getvalue()
        row = pipeline.result_row(o, s)
        j = row["jsonl"]
        assert j["answer"] == line and j["answer_body"] == "" and j["answer_kind"] == "none"
        assert j["lookup_footer"] is False and j["status_lines"] == [line] and j["merge_seconds"] is None
        assert "charts" not in row and "charts" not in j
        assert [e["stage"] for e in j["timeline"]["entries"]] == ["assemble"]   # no reader says "no timeline" of it
        out = _shape(row, s, "records" if options.get("records_only") else "merged", question)
        assert out["answer"] == line and out["kind"] == "none" and "charts" not in out
        json.dumps(j)


def test_let_me_plot_no_merge_call(world, monkeypatch):
    draft = "Let me plot this as a bar chart.\n\n" + DRAFT
    off_model, off, _off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, draft))
    assert off_model.rounds()["merge"] == 1 and off.merge_by == "model"   # the sentence forces the merge call, off
    model = Model(GROUP_CALL, draft)
    o, row = run(world, model)
    assert model.rounds()["merge"] == 0 and o.merge_by == "code" and o.charts
    assert "Let me plot" not in o.rec.answer and "Let me plot" not in o.final and row["jsonl"]["charts_talk"] is True


@pytest.mark.parametrize("lead", ["", "**Summary:**\n", "### Totals\n"], ids=["alone", "bold label", "heading"])
def test_a_reply_of_only_chart_talk_keeps_its_answer(world, monkeypatch, lead):
    """Only the Source line, or a short label and that line, would be left, which reads as no answer, and the reason line
    would say that no record data was used (T23). The reply stays, as "Let me plot this." alone does, and the chart is
    drawn. A lean merge has bullets beside it, so there the sentence goes."""
    draft = lead + "Here is a bar chart of interviews by status.\n" + SOURCE
    _m, off, _off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, draft), ASK, records_only=True)
    assert off.kind == "records" and off.final == draft
    o, _row = run(world, Model(GROUP_CALL, draft), records_only=True)
    assert o.kind == "records" and o.charts[0]["chart"] and o.rec.answer == draft and o.final == draft
    assert o.status_lines == []
    o, _row = run(world, Model(GROUP_CALL, draft))
    assert o.kind == "merged" and o.mode == "lean" and o.charts[0]["chart"] and o.rec.answer == draft
    assert W["no_data"] not in o.final and "Here is a bar chart" not in o.final and "`Interview__c` = client" in o.final


def test_a_label_and_talk_in_a_merged_text_keep_their_answer(world):
    """The second cleaning, in ``make``, uses the same test of an answer."""
    text = "**Summary:**\nHere is a bar chart of interviews by status.\n" + SOURCE
    ask = charts.chart_ask(ASK)
    rec = NS(evidence=[], table_blocks={})
    assert charts.make(ask, ASK, text, "merged", "lean", rec, world.tools.catalog, None)[0] == text


@pytest.mark.parametrize("draft", [
    "{{chart q1 bar}}\n" + SOURCE, "**{{chart q1}}**\n" + SOURCE, "![Interviews by status](chart.png)\n" + SOURCE,
    "```python\nimport matplotlib.pyplot as plt\nplt.bar([1],[2])\n```\n" + SOURCE,
], ids=["mark", "bold mark", "image", "plotting code"])
@pytest.mark.parametrize("options", [{"records_only": True}, {}], ids=["records only", "lean"])
def test_a_reply_of_only_a_marker_says_so(world, draft, options):
    """R9: the marker never reaches the user. Nothing is left, so the record answer stays empty and the answer gets its
    usual reason line (section 9, kind ``none`` or ``knowledge``)."""
    o, row = run(world, Model(GROUP_CALL, draft), **options)
    j = row["jsonl"]
    for text in (o.final, o.body, j["answer"], j["record_answer"]):
        assert "{{" not in text and "![" not in text and "matplotlib" not in text, text
    assert o.rec.answer == SOURCE and o.kind == ("none" if options else "knowledge") and j["charts_talk"] is True
    assert o.charts == [] and o.status_lines == [W["no_data"]]
    if options:
        assert o.final == W["no_data"] == o.shown


def test_a_merged_text_that_copies_a_mark_loses_it(world):
    """Mode ``full`` keeps its sentences, never a mark."""
    merged = "10 client interviews (`Interview__c`).\n{{chart q1 bar}}\n" + SOURCE
    o, row = run(world, Model(GROUP_CALL, DRAFT, merged=merged), ASK, lean_knowledge=False)
    assert o.kind == "merged" and o.mode == "full" and o.charts
    assert "{{" not in o.final and "{{" not in row["jsonl"]["answer_body"]
    assert o.body == "10 client interviews (`Interview__c`).\n" + SOURCE and row["jsonl"]["charts_talk"] is True


def test_code_written_not_found_answer_keeps_its_name(world, monkeypatch):
    """``make`` cleans what the model says: a name holding a chart word in code's own text stays, with the line."""
    name = "Chart Persson"
    call = {"name": "count_records", "args": {"object": "Internal_Interview__c", "filters": [
        {"field": "Interviewer__c", "op": "eq", "value": name}]}}
    question = f"bar chart of the internal interviews of {name}"
    _m, off, _off_row = run_off(world, monkeypatch, lambda: Model(call, "NO_RECORDS"), question, records_only=True)
    assert off.kind == "records" and off.not_found and name in off.final
    o, row = run(world, Model(call, "NO_RECORDS"), question, records_only=True)
    assert name in o.final and without_lines(o.final, [W["failed"]]) == off.final and o.body == off.body
    assert o.status_lines == [W["failed"]] and "charts_talk" not in row["jsonl"]


@pytest.mark.parametrize("options", [{"records_only": True}, {}], ids=["records only", "lean"])
def test_code_written_people_answer_keeps_its_name(world, monkeypatch, options):
    """A people answer code wrote holds no talk, and a stored name in it can hold a chart word."""
    text = "Chart Persson has no interview on record. The person is stored.\n" + SOURCE
    real = RecordSession.ask

    def ask(self, question, **kw):
        return dataclasses.replace(real(self, question, **kw), answer=text, code_answer=True)

    monkeypatch.setattr(RecordSession, "ask", ask)
    o, row = run(world, Model(GROUP_CALL, DRAFT), **options)
    assert o.rec.answer == text and "Chart Persson has no interview" in o.final and "charts_talk" not in row["jsonl"]


def test_the_rows_code_wrote_stay(world, monkeypatch):
    """On the record answer too: a row code wrote is never read as a sentence, whatever it holds."""
    block = "- Bar chart: see the chart.\n- Pie chart: see the chart."
    text = "Two chart types (`Interview__c`).\n" + block + "\n" + SOURCE
    real = RecordSession.ask

    def ask(self, question, **kw):
        return dataclasses.replace(real(self, question, **kw), answer=text, table_blocks={"{{table q1}}": block})

    monkeypatch.setattr(RecordSession, "ask", ask)
    o, _row = run(world, Model(GROUP_CALL, DRAFT), records_only=True)
    assert block in o.rec.answer and block in o.final and o.charts[0]["rows_in_answer"] is True


def test_the_rows_of_a_list_stay(world, monkeypatch):
    """Decision 14 and section 9's list row: a row that holds a chart word and no digit is no talk."""
    draft = ("2 client interviews (`Interview__c`):\n- I-Chart Analyst, Scheduled\n- I-Java Developer, Scheduled\n"
             + SOURCE)
    question = "list interviews and a bar chart of them by status"
    o, row = run(world, Model(LIST_CALL, draft), question, records_only=True)
    assert "- I-Chart Analyst, Scheduled" in o.final and o.body == draft and "charts_talk" not in row["jsonl"]
    assert o.status_lines == [W["list"]]


def test_an_empty_label_goes_with_its_bullets(world, monkeypatch):
    """Every bullet of the merge by code talks about a chart: its label line goes too, so the reason line never lands
    under it, and what is left reads as the record answer."""
    lean = "- `Interview__c` is shown on the chart.\n- `Interview_Status__c` is shown on the status chart."
    draft = "2 of the 10 client interviews (`Interview__c`, `Interview_Status__c`):\n- I-0007\n- I-0004\n" + SOURCE
    question = ASK
    _m, off, _off_row = run_off(world, monkeypatch, lambda: Model(LIST_CALL, draft, lean=lean), question)
    label = off.final.split("\n")[off.final.split("\n").index(SOURCE) - 3]
    assert label.endswith(":") and "`Interview__c`" in off.final and off.merge_by == "code"
    o, _row = run(world, Model(LIST_CALL, draft, lean=lean), question)
    assert label not in o.final and label not in o.body and o.merge_by == "code"
    assert o.body == draft == o.rec.answer
    assert o.final.split("\n")[-2:] == [W["list"], SOURCE]


# ------------------------------------------------------------------------------------------------ numbers and rows
def test_expected_bars_by_direct_sql(world):
    """The expected numbers come from separate SQL on the fake warehouse, not from the build."""
    rows = _sql(world, 'SELECT "Interview_Status__c", COUNT(*) FROM main."Interview__c" GROUP BY 1')
    expected = {str(k): v for k, v in rows}
    assert sum(expected.values()) == FACTS["interviews"] and len(expected) > 2
    o, _row = run(world, Model(GROUP_CALL, DRAFT))
    (item,) = o.charts
    assert _drawn(item) == expected
    said = _said(item)
    assert all(label in said and str(value) in said for label, value in expected.items())

    months = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    rows = _sql(world, """SELECT strftime("Date_of_Interview__c", '%Y-%m'), COUNT(*) FROM main."Interview__c"
                          WHERE "Date_of_Interview__c" >= DATE '2026-05-01'
                            AND "Date_of_Interview__c" < DATE '2026-11-01'
                          GROUP BY 1""")
    found = dict(rows)
    o, _row = run(world, Model(TREND_CALL, "9 interviews over the months (`Interview__c`).\n" + SOURCE), TREND_ASK)
    (item,) = o.charts
    assert _drawn(item) == {m: found.get(m, 0) for m in months} and found.get("2026-06") is None
    assert item["partial"] == [5]   # today is 2026-10-13: October is still running

    rows = _sql(world, """SELECT COALESCE("Employment_Type__c", '(blank)'), SUM("Client_Bill_Rate__c"::DOUBLE)
                          FROM main."Interview__c" GROUP BY 1""")
    call = {"name": "group_records", "args": {"object": "Interview__c", "group_by": "Employment_Type__c",
                                              "measure": "sum", "measure_field": "Client_Bill_Rate__c"}}
    o, _row = run(world, Model(call, "Bill rates by employment type (`Interview__c`).\n" + SOURCE),
                  "bar chart of the bill rate by employment type")
    (item,) = o.charts
    assert _drawn(item) == {k: v for k, v in rows}


def _real_chart(world, tool, args):
    """``charts.for_tool`` on the real tool's result over the fake warehouse."""
    out = world.tools.call(tool, dict(args))
    assert out["ok"], out.get("error")
    return out["result"], charts.for_tool(tool, out["result"], args, world.tools.catalog, world.tools.status().today)


@pytest.mark.parametrize("tool, args", [
    ("compare_records", {"object": "Deliverable__c", "segments": [
        {"label": "Approved by the person", "filters": [{"field": "Approved_By__c.Name", "op": "eq",
                                                         "value": "Recruiter A"}]},
        {"label": "Open", "filters": [{"field": "Status__c", "op": "eq", "value": "Open"}]}]}),
    ("trend_records", {"object": "Deliverable__c", "grain": "month", "date_field": "CreatedDate", "period": "this year",
                       "filters": [{"field": "Approved_By__c.Name", "op": "eq", "value": "Recruiter A"}]}),
    ("group_records", {"object": "Deliverable__c", "group_by": "Status__c",
                       "filters": [{"field": "Approved_By__c.Name", "op": "eq", "value": "Recruiter A"}]}),
], ids=["compare", "trend", "group"])
def test_a_lookup_blank_on_every_record_is_no_zero(world, tool, args):
    """T23: the tool says the data does not record this; a chart never draws or states the 0 the draft check forbids."""
    res, item = _real_chart(world, tool, args)
    assert res["no_data_fields"] == ["Deliverable__c.Approved_By__c"]
    assert item["chart"] is None and item["reason"] == W["no_field_data"] and item["data"] == []
    assert item["subtitle"] == [] and "svg" not in item


def test_subtitle_names_each_lookup_on_the_real_tool(world):
    """T21: two person filters on two lookups are two words, each saying its lookup."""
    args = {"object": "Internal_Interview__c", "group_by": "Status__c", "filters": [
        {"field": "Interviewer__c.Name", "op": "eq", "value": "Delta Person"},
        {"field": "Recruiter__c.Name", "op": "eq", "value": "Recruiter A"}]}
    res, item = _real_chart(world, "group_records", args)
    words = next(s for s in item["subtitle"] if s.startswith("Filtered on:"))
    assert words == "Filtered on: Interviewer, Recruiter" and "Delta" not in json.dumps(item["subtitle"])
    args["filters"] = args["filters"][:1]
    _res, item = _real_chart(world, "group_records", args)
    assert "Filtered on: Interviewer" in item["subtitle"]


def test_real_blank_arguments_and_a_minimum_none_reaches(world):
    """T23 and section 7.1: arguments read as the tool reads them, on the real tool's result."""
    args = {"object": "Interview__c", "group_by": "Interview_Status__c"}
    _res, plain = _real_chart(world, "group_records", args)
    for blank in ("", "  "):
        _res, item = _real_chart(world, "group_records", {**args, "min_value": blank})
        assert item["truncated"] is False and item["subtitle"] == plain["subtitle"] and item["data"] == plain["data"]
    compared = {"object": "Interview__c", "segments": [
        {"label": "Contract", "filters": [{"field": "Employment_Type__c", "op": "eq", "value": "Contract"}]},
        {"label": "Full", "filters": [{"field": "Employment_Type__c", "op": "eq", "value": "Full-Time"}]}]}
    _res, counted = _real_chart(world, "compare_records", compared)
    _res, item = _real_chart(world, "compare_records", {**compared, "measure": " "})
    assert item["chart"] == counted["chart"] and item["data"] == counted["data"]
    # records matched, and a minimum left no group: the reason says that, not "no records match"
    res, item = _real_chart(world, "group_records", {**args, "min_value": 500})
    assert res["groups"] == [] and item["reason"] == W["no_rows_min"]


BLANK_TOP = {"object": "Interview__c", "group_by": "Recruiter__c", "top": 3,
             "filters": [{"field": "Recruiter__c", "op": "is_blank"}]}
RATE_SUM = {"measure": "sum", "measure_field": "Client_Bill_Rate__c"}
RATE_AVG = {"measure": "avg", "measure_field": "Client_Bill_Rate__c"}
UNRESOLVED_TOP = {"object": "Interview__c", "group_by": "Recruiter__c", "top": 3,
                  "filters": [{"field": "Recruiter__c", "op": "eq", "value": "a06000000000099AAA"}]}


@pytest.mark.parametrize("extra", [{}, RATE_SUM, RATE_AVG], ids=["count", "sum", "avg"])
def test_a_ranking_of_only_blank_records_is_not_no_records(world, extra):
    """T23, through the real tool and the answer path: ``top`` leaves the blank group out of ``groups``, so the result
    holds no rows although records match."""
    args = {**BLANK_TOP, **extra}
    o, _row = run(world, Model({"name": "group_records", "args": args}, DRAFT))
    res = o.rec.evidence[0].model_result
    assert res["groups"] == [] and res["group_count"] == 0
    assert o.charts == [] and o.status_lines == [W["only_blank"]] and W["only_blank"] in o.final
    assert W["no_rows"] not in o.final
    if not extra:
        assert res["blank_group_value"] == 1 and res["total_across_groups"] == 1


def test_a_ranking_of_only_unresolved_records(world):
    """The same for the ``(unresolved id)`` group, which only a count gives a number. Under another measure the result
    holds nothing that tells it from no match, so the words are true of both."""
    res, item = _real_chart(world, "group_records", UNRESOLVED_TOP)
    assert res["groups"] == [] and res["unresolved_ids"] == 1 and "blank_group_value" not in res
    assert item["reason"] == W["only_blank"]
    for extra in (RATE_SUM, RATE_AVG, {**RATE_SUM, "top": 0}):   # a top of 0 is clamped to 1, so it ranks all the same
        res, item = _real_chart(world, "group_records", {**UNRESOLVED_TOP, **extra})
        assert res["groups"] == [] and res["unresolved_ids"] == 0 and item["reason"] == W["no_rows_top"]
    o, _row = run(world, Model({"name": "group_records", "args": {**UNRESOLVED_TOP, **RATE_SUM}}, DRAFT))
    assert o.charts == [] and o.status_lines == [W["no_rows_top"]] and W["no_rows"] not in o.final


@pytest.mark.parametrize("args, left_out", [(BLANK_TOP, "blank_group_value"), (UNRESOLVED_TOP, "unresolved_ids")],
                         ids=["blank", "unresolved"])
def test_a_minimum_only_a_left_out_group_reaches(world, args, left_out):
    """T23, through the real tool: the group passes the minimum and ``top`` leaves it out of ``groups``, so the words
    speak only of named groups."""
    res, item = _real_chart(world, "group_records", {**args, "min_value": 1})
    assert res["groups"] == [] and res[left_out] == 1 and item["reason"] == W["no_rows_min"]
    assert "no named group" in item["reason"]


def test_a_ranking_with_no_match_says_no_records(world):
    """Only a count can say so: under another measure, no match reads like a ranking of only unresolved records. With no
    ``top``, nothing is left out of ``groups``, so any measure can."""
    plain = {"object": "Interview__c", "group_by": "Recruiter__c", "date_field": "Date_of_Interview__c",
             "period": "2001"}
    args = {**plain, "top": 3}
    for call, word in ((args, "no_rows"), ({**args, **RATE_SUM}, "no_rows_top"), ({**plain, **RATE_SUM}, "no_rows")):
        res, item = _real_chart(world, "group_records", call)
        assert res["groups"] == [] and item["reason"] == W[word]


def test_real_fit_that_leaves_no_rows_says_too_large(world):
    """``RecordTools._fit`` can leave ``groups`` empty with no ``trimmed`` key, when other keys fill its limit."""
    big = {"query_id": "q1", "object": "Interview__c", "grouped_by": "Interview_Status__c", "measure": "count",
           "groups": [{"group": f"G{i}", "value": i + 1} for i in range(5)], "group_count": 5, "shown": 5,
           "notes": ["n" * 11700]}
    fit = world.tools._fit(big)
    assert fit["groups"] == [] and "trimmed" not in fit and fit["group_count"] == 5
    item = charts.for_tool("group_records", fit, {}, world.tools.catalog, None)
    assert item["reason"] == W["too_large"]


def test_a_lone_surrogate_in_a_compare_label_costs_nothing(world, monkeypatch):
    """The model must type half a surrogate pair for this. The chart item encodes, in the page's reply and the command
    line's print, as the answer does with the switch off."""
    call = {"name": "compare_records", "args": {"object": "Interview__c", "segments": [
        {"label": "Team \ud83d full", "filters": [{"field": "Employment_Type__c", "op": "eq", "value": "Contract"}]},
        {"label": "Contract", "filters": [{"field": "Employment_Type__c", "op": "eq", "value": "Full-Time"}]}]}}
    question = "bar chart comparing full time and contract interviews"
    draft = "10 client interviews (`Interview__c`).\n" + SOURCE
    o, row = run(world, Model(call, draft), question)
    (item,) = o.charts
    assert item["chart"] and o.charts_error == "" and item["data"][0]["Compared"] == "Team full"
    shaped = _shape(row, object(), question=question)
    json.dumps({k: shaped.get(k) for k in ("answer", "kind", "charts")}, ensure_ascii=False).encode("utf-8")
    "\n".join(charts.text_lines(o.charts)).encode("utf-8")


def test_the_session_gives_the_catalog_and_today(world):
    """The heading reads the catalog's labels, and a period past the snapshot's today gets its note."""
    o, _row = run(world, Model(GROUP_CALL, DRAFT))
    assert o.charts[0]["chart"]["title"] == "Interviews by Interview Status"
    call = {"name": "group_records", "args": {**GROUP_ARGS, "date_field": "Date_of_Interview__c",
                                              "period": "this month"}}
    text = "3 client interviews this month (`Interview__c`, `Interview_Status__c`).\n" + SOURCE
    o, _row = run(world, Model(call, text), "bar chart of this month's interviews by status")
    (item,) = o.charts
    assert W["running"] in item["notes"]
    assert "Period: 2026-10-01 to 2026-10-31 (by Date of Interview)" in item["subtitle"]


def test_rows_hidden_when_in_answer(world, monkeypatch):
    monkeypatch.setenv("SFA_TABLES", "code")
    o, _row = run(world, Model(GROUP_CALL, TABLE_DRAFT))
    (item,) = o.charts
    assert item["rows_in_answer"] is True and item["data"]
    assert o.final.count("- Completed: 5") == 1
    assert W["cli_rows"] not in charts.text_lines(o.charts)
    monkeypatch.setenv("SFA_TABLES", "model")
    o, _row = run(world, Model(GROUP_CALL, DRAFT))
    (item,) = o.charts
    assert item["rows_in_answer"] is False
    assert W["cli_rows"] in charts.text_lines(o.charts)


def test_print_outcome_prints_the_chart_as_text(world):
    cfg = _cfg()
    llm = LLM(cfg, client=Model(GROUP_CALL, DRAFT))
    session = RecordSession(world.tools, cfg, llm=llm)
    o = pipeline.answer(ASK, cfg, world.registry, world.system, llm, 1, session)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_outcome(o, session, False)
    out = buf.getvalue()
    lines = charts.text_lines(o.charts)
    assert "\n".join(lines) in out and out.index(o.shown) < out.index(lines[0]) and "<svg" not in out
    assert o.charts[0]["svg_b64"] not in out


# ------------------------------------------------------------------------------------------------ the later model test
#: The reader of that test (docs/runbook.md); it lives beside the build's notes, so a clean checkout has none.
READER = ROOT / ".private" / "charts" / "work" / "build" / "read_model_test.py"
DESIGN = "which dashboards have a bar chart"
FILE_RUN = {ASK: ("C01", lambda: Model(GROUP_CALL, DRAFT)), BARE: ("B01", Model),
            DESIGN: ("D01", lambda: Model(GROUP_CALL, DRAFT, route="NO_RECORDS"))}


def _file_run(world, path):
    """What ``ask --file`` runs, with a scripted model per question: ``run_file`` over ``cli.ask``. The sidecar's rows."""
    def one(text, n):
        cfg = _cfg()
        llm = LLM(cfg, client=FILE_RUN[text][1]())
        session = RecordSession(world.tools, cfg, llm=llm)
        return cli_ask(text, cfg, world.registry, world.system, llm, n, False, session, records_wanted=True)

    run_file(list(FILE_RUN), one, str(path))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_file_run_sidecar_holds_what_the_reader_reads(world, tmp_path, capsys):
    """Section 19's chain up to the sidecar: a renamed key shows here and not first in the owner's model run."""
    side = tmp_path / "charts_asks.jsonl"
    asked, bare, design = _file_run(world, side)
    out = capsys.readouterr().out
    assert "svg_b64" not in side.read_text(encoding="utf-8") and "<svg" not in out and "SUMMARY" in out
    assert [r["question"] for r in (asked, bare, design)] == list(FILE_RUN)
    assert [r["n"] for r in (asked, bare, design)] == [1, 2, 3]
    for row in (asked, bare, design):
        assert isinstance(row["seconds"], float) and row["answer_kind"] in ("merged", "records", "none")
        for entry in row["timeline"]["entries"]:
            assert entry["kind"] in ("stage", "model", "tool", "check") and entry["stage"]

    def models(row, stage):
        return [e for e in row["timeline"]["entries"] if e["kind"] == "model" and e["stage"] == stage]

    (item,) = asked["charts"]
    assert item["tool"] == "group_records" and item["chart"]["type"] == "bar" and item["data"]
    assert asked["queries"][0]["tool"] == "group_records" and asked["knowledge_mode"] == "lean"
    assert asked["triage"]["route"] is True and "overruled_by" not in asked["triage"]
    assert len(models(asked, "records")) == 2 and models(asked, "records")[0]["tok_in"] == 100
    assert "charts_talk" not in asked and "charts_error" not in asked and "status_lines" not in asked

    assert bare["answer_kind"] == "none" and bare["status_lines"] == [W["bare"]] and "charts" not in bare
    assert not [e for e in bare["timeline"]["entries"] if e["kind"] == "model"]

    assert design["triage"]["overruled_by"] == "chart" and design["knowledge_mode"] == "full"
    assert design["charts"][0]["chart"]["type"] == "bar" and len(models(design, "merge")) == 1


@pytest.mark.skipif(not READER.is_file(), reason="the reader of the model test is not in a clean checkout")
def test_the_reader_reads_a_file_run(world, tmp_path):
    """Section 19's chain to its end, with made-up question files: counts and fixed words, never a question."""
    side = tmp_path / "charts_asks.jsonl"
    _file_run(world, side)
    home = tmp_path / "home"
    questions = home / ".private" / "questions" / "charts"
    questions.mkdir(parents=True)
    (home / "prompts").mkdir()
    shutil.copy(ROOT / "prompts" / "records_chart_words.md", home / "prompts")
    lines = "".join(f"# {qid}: made-up\n{question}\n" for question, (qid, _model) in FILE_RUN.items())
    (questions / "asks.txt").write_text(lines, encoding="utf-8")
    (questions / "plain.txt").write_text("", encoding="utf-8")
    res = subprocess.run([sys.executable, "-B", str(READER), "--asks", str(side), "--plain", str(tmp_path / "none")],
                         cwd=str(home), capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "read: 3 chart rows (3 questions, 0 fit no id)" in out
    row = {line.split()[0]: line for line in out.splitlines() if " route=" in line}
    assert set(row) == {"B01", "C01", "D01"}
    for held in ("route=True", "kind=merged", "mode=lean", "rounds= 2", "merge_calls=0", "word=-",
                 "charts=group_records/barx1", "tools=group_recordsx1"):
        assert held in row["C01"], held
    for held in ("kind=none", "rounds= 0", "tok_in=      0", "word=bare", "charts=-", "tools=-"):
        assert held in row["B01"], held
    for held in ("route=False", "overruled=chart", "mode=full", "rounds= 2", "merge_calls=1",
                 "charts=group_records/barx1"):
        assert held in row["D01"], held
    assert "bare with the bare line and no model call: 1 of 4" in out and "D01 overruled: chart" in out
    assert "{'bare': 1}" in out and "NO_TIMELINE" not in out and "charts_error=" not in out
    assert not any(question in out for question in FILE_RUN)


def test_list_gets_reason_only(world, monkeypatch):
    steps = (LIST_CALL, LIST_DRAFT)
    _m, off, off_row = run_off(world, monkeypatch, lambda: Model(*steps))
    o, row = run(world, Model(*steps))
    assert o.charts == [] and "charts" not in row and "charts" not in row["jsonl"]
    assert o.status_lines == [W["list"]] and o.body == off.body and without_lines(o.final, [W["list"]]) == off.final
    assert o.kind == off.kind


def test_fault_in_a_two_field_breakdown_leaves_no_private_key(world, monkeypatch):
    """The second item of a breakdown is not spared: no key of the module's reaches the page reply or the sidecar."""
    real = RecordSession.ask

    def ask(self, question, **kw):
        rec = real(self, question, **kw)
        rec.evidence[0].model_result = {**rec.evidence[0].model_result, "breakdown": {
            "Interview_Status__c": {"A": 6, "B": 4}, "Employment_Type__c": {"C": 7, "D": 3}}}
        return rec

    def fail(*a, **k):
        raise KeyError("a made-up fault text")

    monkeypatch.setattr(RecordSession, "ask", ask)
    monkeypatch.setattr(charts, "_draw", fail)
    o, row = run(world, Model(COUNT_CALL, DRAFT))
    assert o.charts_error == "KeyError" == row["jsonl"]["charts_error"] and len(o.charts) == 2
    for item in [*o.charts, *row["jsonl"]["charts"]]:
        assert not [k for k in item if k.startswith("_")] and item["chart"] is None and item["data"]


@pytest.mark.parametrize("patched, exc, items", [("_pick", ValueError, 0), ("_draw", KeyError, 1),
                                                 ("_Cleaner", TypeError, 0)])
def test_fault_keeps_answer(world, monkeypatch, patched, exc, items):
    """``make`` fails inside: the answer whole, the ``fault`` line and a class name, never the fault's text."""
    steps = (GROUP_CALL, DRAFT)
    _m, off, _off_row = run_off(world, monkeypatch, lambda: Model(*steps))

    def fail(*a, **k):
        raise exc("a made-up fault text")

    monkeypatch.setattr(charts, patched, fail)
    o, row = run(world, Model(*steps))
    assert o.body == off.body and o.status_lines == [W["fault"]] and without_lines(o.final, [W["fault"]]) == off.final
    assert o.charts_error == exc.__name__ == row["jsonl"]["charts_error"]
    assert "made-up fault" not in json.dumps(row["jsonl"], default=str)
    assert len(o.charts) == items
    if items:   # the rows are built: they stay, with no picture and no chart
        assert o.charts[0]["chart"] is None and o.charts[0]["data"] and "svg_b64" not in o.charts[0]


# ------------------------------------------------------------------- the default since the go-live of 2026-11-04
def test_unset_chart_question_gets_a_chart(world, monkeypatch):
    monkeypatch.delenv("SFA_CHARTS", raising=False)
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model)
    assert o.charts and o.charts[0]["chart"]["type"] == "bar" and "charts" in row
    assert charts.RECORDS_CHART_NOTE in model.requests["record"][0]["messages"][0]["content"]


def test_unset_no_records_route_is_overruled(world, monkeypatch):
    monkeypatch.delenv("SFA_CHARTS", raising=False)
    o, row = run(world, Model(GROUP_CALL, DRAFT, route="NO_RECORDS"))
    assert row["jsonl"]["triage"]["overruled_by"] == "chart" and o.charts


def test_unset_bare_gets_one_line_no_model(world, monkeypatch):
    monkeypatch.delenv("SFA_CHARTS", raising=False)
    model = Model()
    o, _row = run(world, model, BARE)
    assert model.total() == 0 and o.status_lines == [W["bare"]]


def test_unset_plain_question_equals_off(world, monkeypatch):
    monkeypatch.delenv("SFA_CHARTS", raising=False)
    unset_model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, unset_model, PLAIN)
    off_model, off, off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, DRAFT), PLAIN)
    assert unset_model.requests == off_model.requests and o.final == off.final
    assert _canon(row["jsonl"]) == _canon(off_row["jsonl"])


def _no_records_loads_charts(tmp_path, switch):
    fixture = str(ROOT / "tests" / "fixtures" / "Internal_Interview__c.json")
    script = textwrap.dedent(f"""
        import contextlib, io, json, sys
        import sf_assistant.cli as mod
        import sf_assistant.pipeline
        from sf_assistant.agent.llm import LLM
        from sf_assistant.agent.mock_llm import MockClient
        sf_assistant.pipeline.LLM = lambda cfg: LLM(cfg, client=MockClient())
        with contextlib.redirect_stdout(io.StringIO()):
            code = mod.main(["--no-records", "--knowledge", {fixture!r}, "What values can Status__c take?"])
        print(json.dumps({{"code": code, "charts": "sf_assistant.agent.charts" in sys.modules}}))
    """)
    env = {k: v for k, v in os.environ.items() if k != "SFA_CHARTS"}
    env.update(OKEVAL_BASE_URL="http://127.0.0.1:9/v1", OKEVAL_API_KEY="test-key")
    if switch is not None:
        env["SFA_CHARTS"] = switch
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_no_records_unset_loads_charts_off_does_not(tmp_path):
    assert _no_records_loads_charts(tmp_path, None) == {"code": 0, "charts": True}
    assert _no_records_loads_charts(tmp_path, "off") == {"code": 0, "charts": False}


def test_invalid_switch_is_off_and_logged(world, monkeypatch):
    off_model, _off, off_row = run_off(world, monkeypatch, lambda: Model(GROUP_CALL, DRAFT))
    monkeypatch.setenv("SFA_CHARTS", "zorvex")
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model)
    assert model.requests == off_model.requests and o.charts == [] and o.status_lines == []
    assert row["jsonl"]["charts_switch"] == "SFA_CHARTS must be off or on"   # the problem, not the value
    rest = _canon(row["jsonl"])
    rest.pop("charts_switch")
    assert rest == _canon(off_row["jsonl"])


def test_no_chart_value_in_timeline(world, monkeypatch):
    sentinel = "Recruiter A"
    call = {"name": "group_records", "args": {"object": "Interview__c", "group_by": "Recruiter__c"}}
    question = "bar chart of interviews by recruiter"
    draft = "10 client interviews (`Interview__c`, `Recruiter__c`).\n" + SOURCE
    _m, off, _off_row = run_off(world, monkeypatch, lambda: Model(call, draft), question)
    o, row = run(world, Model(call, draft), question)
    (item,) = o.charts
    assert sentinel in _drawn(item) and sentinel in json.dumps(row["jsonl"]["charts"])
    assert sentinel not in json.dumps(o.timeline) and sentinel not in json.dumps(row["jsonl"]["timeline"])
    assert _stages(o.timeline) == _stages(off.timeline) and "records" in _stages(o.timeline)   # no new stage


def test_preload_with_switch_off():
    """The page imports the module at start-up with the switch off, and reads both prompts: a failure there means the
    page cannot start. In a new process, so the import is the preload's own."""
    script = textwrap.dedent(f"""
        import importlib.util, json, sys
        spec = importlib.util.spec_from_file_location("web_pipeline", {str(ROOT / "web" / "pipeline.py")!r})
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        assert "sf_assistant.agent.charts" not in sys.modules
        names = web.preload_package()
        from sf_assistant import prompts
        loaded = prompts.all_prompts()
        print(json.dumps({{"listed": "sf_assistant.agent.charts" in names, "imported": "sf_assistant.agent.charts" in
                           sys.modules, "note": loaded["records_chart_note"].text, "words":
                           loaded["records_chart_words"].text}}))
    """)
    env = dict(os.environ, SFA_CHARTS="off")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(ROOT),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["listed"] is True and out["imported"] is True
    assert out["note"] == charts.RECORDS_CHART_NOTE and out["words"] == charts.RECORDS_CHART_WORDS
    assert P.load("records_chart_note").text == charts.RECORDS_CHART_NOTE


def test_a_result_stays_as_the_model_saw_it(world):
    """T2 on the real evidence: the chart reads the model's result and leaves it equal; the request's tool message
    holds what the evidence holds."""
    model = Model(GROUP_CALL, DRAFT)
    o, _row = run(world, model)
    (ev,) = o.rec.evidence
    before = copy.deepcopy(ev.model_result)
    again = charts.make(charts.chart_ask(ASK), ASK, o.body, o.kind, o.mode, o.rec, world.tools.catalog, o.status.today)
    assert ev.model_result == before and again[1][0]["data"] == o.charts[0]["data"]
    tool_message = next(m for m in model.requests["record"][1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["result"] == json.loads(json.dumps(before, default=str))


# ------------------------------------------------------------------------------------------------ placement fixes
# The four page questions of 2026-11-16 (placement and its rate), with the scripted model: the owner's definition in the
# hints, a rate per month in one call and its chart, counts of placements per month, and an empty record step.
RATE_ARGS = {**TREND_ARGS, "period": "this year",
             "part_filters": [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]}
PLACED_ARGS = {**TREND_ARGS, "period": "this year",
               "filters": [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}],
               "measure": "count_distinct", "measure_field": "Candidate__c"}   # candidates counted: different ones
RATE_DRAFT = ("The placement rate per month is the share of client interviews (`Interview__c`) with "
              "`Interview_Outcome__c` = 'Offer Received' among all client interviews of that month.\n" + SOURCE)


def test_placement_rate_monthwise_is_one_call_and_a_rate_chart(world):
    from sf_assistant.engine.business_terms import DEFINITIONS, TERMS_HEADER

    model = Model({"name": "trend_records", "args": RATE_ARGS}, RATE_DRAFT)
    o, row = run(world, model, "can you plot the placement rate monthwise from january to october?")
    assert model.rounds()["record"] == 2 and [e.tool for e in o.rec.evidence] == ["trend_records"]
    user = model.requests["record"][0]["messages"][1]["content"]
    assert TERMS_HEADER in user and DEFINITIONS["placement"] in user   # the owner's definition reaches the agent
    (item,) = o.charts
    res = o.rec.evidence[0].model_result
    assert item["chart"]["type"] == "line" and item["chart"]["y_keys"] == [W["percent"]]
    assert _drawn(item) == {b["bucket"]: b["percent"] for b in res["buckets"]}   # the chart's numbers are the result's
    assert o.status_lines == [] and "No chart" not in o.final and o.kind in ("merged", "records")


def test_candidates_placed_per_month_are_offer_interviews_drawn(world):
    draft = ("Placements per month are client interviews (`Interview__c`) with `Interview_Outcome__c` = "
             "'Offer Received'.\n" + SOURCE)
    model = Model({"name": "trend_records", "args": PLACED_ARGS}, draft)
    o, row = run(world, model, "can you plot the number of candidates placed from january to december 2026")
    system = model.requests["record"][0]["messages"][0]["content"]
    assert "- Placements per month this year (business term) -> trend_records" in system
    assert ('- Candidates placed per month this year (business term) -> trend_records {"object":"Interview__c",'
            '"grain":"month"') in system and '"measure":"count_distinct","measure_field":"Candidate__c"}' in system
    (item,) = o.charts
    res = o.rec.evidence[0].model_result
    assert item["chart"]["type"] == "line" and len(item["chart"]["y_keys"]) == 1
    assert _drawn(item) == {b["bucket"]: b["value"] for b in res["buckets"]}
    assert o.status_lines == [] and W["list"] not in o.final


def test_a_source_line_alone_gets_one_more_round(world):
    model = Model({"name": "trend_records", "args": RATE_ARGS}, SOURCE, RATE_DRAFT)
    o, row = run(world, model, "can you plot the placement rate monthwise from january to october?")
    assert model.rounds()["record"] == 3 and len(o.rec.nudges) == 1
    assert o.rec.nudges[0] == rprompts.RECORDS_FINAL_FORM_MESSAGE and not o.rec.error
    assert o.charts and o.charts[0]["chart"]["y_keys"] == [W["percent"]] and o.status_lines == []


def test_an_empty_record_step_says_the_figures_could_not_be_computed(world):
    model = Model({"name": "trend_records", "args": RATE_ARGS}, SOURCE)   # every draft is the Source line alone
    o, row = run(world, model, "can you plot the placement rate monthwise from january to october?")
    assert o.rec.error == "empty record answer" and o.kind == "none" and o.charts == []
    # the lean answer names Interview__c, which the record step read: it may follow, labelled
    assert o.body == f"{pipeline.RECORDS_FAILED_ANSWER}\n\n{pipeline.RECORDS_FAILED_NOTE_LABEL}\n{LEAN}"
    assert o.status_lines == [W["answer_failed"]] and o.final == o.body + "\n" + W["answer_failed"]
    assert row["jsonl"]["record_dropped_raw"] == SOURCE and row["jsonl"]["answer_kind"] == "none"
