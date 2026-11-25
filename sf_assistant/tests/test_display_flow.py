"""Prerequisite P3 of 2026-11-25 through the real answer path: the display words leave the question before the
knowledge and record search, and their flags reach the chart note, the chart overrule and the record agent's display
note, while the typed text stays in the outcome, the merge, the contact rule and the JSONL.

Offline and model-free: the fake warehouse and its knowledge (tests/warehouse_fixture.py, synthetic names) and a
scripted fake of the model's client (tests/test_records_retry.py's), so every request is kept as it would go out.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.agent import charts, display as D  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
import sf_assistant.pipeline as pipeline  # noqa: E402
from test_records_retry import DRAFT, GROUP_CALL, Model, _switches, run, world  # noqa: E402,F401   # fixtures


def _user_texts(model) -> list[str]:
    """Every user message of the route, names, lean, knowledge and record requests."""
    return [m["content"] for role in ("route", "names", "lean", "knowledge", "record")
            for r in model.requests[role] for m in r["messages"] if m.get("role") == "user"]


@pytest.mark.parametrize("question, removed, flag", [
    ("show me a table of interviews by status", "table", "table"),
    ("can you show interviews by status as an artifact", "artifact", "visual"),
    ("give me a summary of interviews by status", "summary", "detail"),
])
def test_no_search_request_holds_a_removed_word(world, question, removed, flag):
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model, question)
    assert model.requests["route"] and model.requests["record"] and model.requests["lean"]
    assert not any(removed in t.lower() for t in _user_texts(model)), removed
    assert o.text == question and row["jsonl"]["question"] == question and row["question"] == question
    assert o.q.question == question and o.rec.question == o.display.search
    assert flag in row["jsonl"]["display"]["flags"] and removed in row["jsonl"]["display"]["words"]
    if removed == "artifact":   # no request at all names it (L65: it was searched as a field)
        assert "artifact" not in json.dumps(model.requests).lower()


def test_the_display_note_follows_the_chart_note(world):
    model = Model(GROUP_CALL, DRAFT)
    run(world, model, "show me a table of interviews by status in detail")
    system = model.requests["record"][0]["messages"][0]["content"]
    assert system.endswith(D.note(D.split("show me a table of interviews by status in detail")))
    assert D.NOTE_LINES["table"] in system and D.NOTE_LINES["detail"] in system
    assert system.startswith(rprompts.render_records_system(world.tools.status()))
    others = [r for role, reqs in model.requests.items() if role != "record" for r in reqs]
    assert not any(D.NOTE_LINES["lead"] in json.dumps(r) for r in others)
    model = Model(GROUP_CALL, DRAFT)
    run(world, model, "show a bar chart of interviews by status")
    system = model.requests["record"][0]["messages"][0]["content"]
    assert system.endswith(charts.RECORDS_CHART_NOTE) and D.NOTE_LINES["lead"] not in system
    assert "chart" not in model.requests["record"][0]["messages"][1]["content"].split("\n")[0]


def test_the_chart_overrule_reads_the_flags(world):
    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model, "can you visualize interviews by status")
    assert row["jsonl"]["triage"]["overruled_by"] == "chart" and o.rec.evidence and o.charts
    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model, "what is a chart of accounts")   # not a chart: the route stands
    assert row["jsonl"]["triage"].get("overruled_by") != "chart"


@pytest.mark.parametrize("question, key", [("can you do visualizations ?", "capability"), ("excel me bhejo", "bare"),
                                           ("isko table me dikhao", "bare")])
def test_a_display_only_question_gets_one_line_and_no_model_call(world, question, key):
    model = Model()
    o, row = run(world, model, question)
    assert o.final == D.line(key) and o.kind == "none" and o.line_reason == "display_only"
    assert not any(model.requests.values())
    assert row["jsonl"]["line_reason"] == "display_only" and row["jsonl"]["display"]["flags"]["empty"] is True


def test_a_bare_chart_ask_keeps_its_line(world):
    model = Model()
    o, row = run(world, model, "now plot that")
    assert o.final == charts.bare_line() and o.line_reason == "bare_chart" and not any(model.requests.values())


def test_the_file_line_goes_after_the_answer(world):
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model, "export interviews by status to excel")
    assert o.kind in ("merged", "records") and D.line("file") in o.final and D.line("file") not in o.body
    assert row["jsonl"]["display"]["flags"]["file"] == "excel"
    assert D.line("file") in o.status_lines


def test_the_contact_rule_and_the_merge_keep_the_typed_text(world, monkeypatch):
    seen = []
    import sf_assistant.engine.contact as contact

    real = contact.asks_for_contact
    monkeypatch.setattr(contact, "asks_for_contact", lambda q: seen.append(q) or real(q))
    run(world, Model(GROUP_CALL, DRAFT), "show me a table of interviews by status")
    assert "show me a table of interviews by status" in seen   # the pipeline's own contact rule reads the typed text


def test_the_invariant_guard_keeps_the_typed_text(world, monkeypatch):
    monkeypatch.setattr(D.SessionGuards, "same", lambda self, typed, search: False)
    model = Model(GROUP_CALL, DRAFT)
    o, row = run(world, model, "show me a table of interviews by status")
    assert o.display.search == "show me a table of interviews by status" and o.display.table
    assert ["*", "invariant"] in row["jsonl"]["display"]["kept"]
    assert "table" in model.requests["route"][0]["messages"][1]["content"]
    assert D.NOTE_LINES["table"] in model.requests["record"][0]["messages"][0]["content"]


def test_charts_off_imports_no_charts_module_and_still_splits(tmp_path):
    script = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT / "tests")!r})
        import sf_assistant.pipeline as pipeline
        from sf_assistant.agent.record_session import RecordSession
        from sf_assistant.config import load_config
        from warehouse_fixture import open_fixture
        seen = []
        class Fake:
            def chat(self, messages, tools=None, **_):
                seen.append(messages[-1]["content"] if messages[-1]["role"] == "user" else "")
                if not tools:
                    return {{"content": "RECORDS", "tool_calls": []}}
                if not any(m["role"] == "tool" for m in messages):
                    return {{"content": None, "tool_calls": [{{"id": "c1", "name": "group_records", "arguments":
                            {{"object": "Interview__c", "group_by": "Interview_Status__c"}}}}]}}
                return {{"content": "10 client interviews (`Interview__c`).\\nSource: fake copy.", "tool_calls": []}}
        cfg = load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")
        tools = open_fixture(Path({str(tmp_path)!r}))
        session = RecordSession(tools, cfg, llm=Fake())
        o = pipeline.answer("show a bar chart of interviews by status", cfg, None, None, Fake(), 1, session,
                            records_only=True)
        tools.close()
        print(json.dumps({{"search": o.display.search, "chart": o.display.chart,
                           "imported": "sf_assistant.agent.charts" in sys.modules,
                           "said": any("chart" in s for s in seen if s)}}))
    """)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), SFA_CHARTS="off", SFA_RESOLVER="off", OKEVAL_BASE_URL="http://127.0.0.1:9/v1",
               OKEVAL_API_KEY="test-key")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data == {"search": "show interviews by status", "chart": True, "imported": False, "said": False}


def test_records_off_display_question_loads_no_engine_module(tmp_path):
    fixture = str(ROOT / "tests" / "fixtures" / "Internal_Interview__c.json")
    script = textwrap.dedent(f"""
        import contextlib, io, json, sys
        import sf_assistant.cli as mod
        import sf_assistant.pipeline
        from sf_assistant.agent.llm import LLM
        from sf_assistant.agent.mock_llm import MockClient
        sf_assistant.pipeline.LLM = lambda cfg: LLM(cfg, client=MockClient())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(["--no-records", "--knowledge", {fixture!r}, "show the internal interviews as a table"])
        print(json.dumps({{"code": code, "engine": sorted(m for m in sys.modules if m.startswith("sf_assistant.engine")),
                           "duckdb": "duckdb" in sys.modules}}))
    """)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), OKEVAL_BASE_URL="http://127.0.0.1:9/v1", OKEVAL_API_KEY="test-key", SFA_CHARTS="on")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data == {"code": 0, "engine": [], "duckdb": False}


# ------------------------------------------------------------------------------------------------ review fixes (P3)
def _not_requests():
    import test_charts as T

    return [q for v in T.NOT_REQUESTS.values() for q in v] + list(T.NOT_REQUESTS_AFTER_REVIEW)


def test_no_pinned_non_chart_string_becomes_a_chart_ask():
    """R1: every string tests/test_charts.py pins as no chart ask stays none after the display split (no promotion to a
    chart ask, so no chart overrule and no chart note), and its skill and refusal words stay in the search text."""
    for q in _not_requests():
        d = D.split(q)
        ask, d2 = pipeline.chart_reading(charts.chart_ask(q), d)
        assert ask is None and not d2.chart, (q, d.words, d.search)
    for q in ("how many visually impaired candidates are there", "open positions for visualization engineer",
              "data visualization candidates", "candidates with tableau visualization experience",
              "stop showing charts", "hide the pie chart", "remove the chart", "graph hata do", "chart band karo"):
        assert D.split(q).search == q, q


def test_pinned_chart_requests_stay_chart_asks():
    import test_charts as T

    for q in [q for v in T.REQUESTS.values() for q in v] + list(T.REQUESTS_AFTER_REVIEW):
        ask = charts.chart_ask(q)
        if ask is not None:
            got, d2 = pipeline.chart_reading(ask, D.split(q))
            assert got is ask and d2.chart, q


@pytest.mark.parametrize("question", ["remove the chart", "how many visually impaired candidates are there"])
def test_a_refusal_or_a_skill_word_runs_as_before(world, question):
    """R1 through the pipeline: no chart overrule, no chart, the route and the record agent read the typed words."""
    model = Model(GROUP_CALL, DRAFT, route="NO_RECORDS")
    o, row = run(world, model, question)
    assert o.charts == [] and (row["jsonl"].get("triage") or {}).get("overruled_by") != "chart"
    assert question in model.requests["route"][0]["messages"][1]["content"]


def test_a_guard_that_only_kept_a_word_adds_no_display_key(world):
    """R8: "details of X" keeps its word (gate): the row gains no display key."""
    o, row = run(world, Model(GROUP_CALL, DRAFT), "show details of interview INT-0001")
    assert o.display.kept and not o.display.words and "display" not in row["jsonl"]


@pytest.mark.parametrize("question, key", [("can you export to excel?", "capability_file"),
                                           ("can you make a dashboard?", "capability_visual")])
def test_the_capability_line_fits_the_ask(world, question, key):
    model = Model()
    o, _row = run(world, model, question)
    assert o.final == D.line(key) and o.line_reason == "display_only" and not any(model.requests.values())
