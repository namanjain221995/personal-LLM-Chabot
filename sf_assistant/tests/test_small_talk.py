"""Prerequisite P6 of 2026-11-25: small talk answered by code (agent/small_talk.py, prompts/small_talk.md).

A full match: greetings, thanks, acknowledgements, goodbyes, presence checks, empty asks and help asks, in English,
Hinglish and with typos, get one fixed line with no tool and no model call, on every surface (``pipeline.answer``,
``followup.rewrite``, the ask service's ``_job``); any other word keeps the whole pipeline. Made-up names only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import ask_mcp  # noqa: E402
import pipeline as web_pipeline  # noqa: E402

from sf_assistant import pipeline as core  # noqa: E402
from sf_assistant import prompts as P  # noqa: E402
from sf_assistant.agent import followup, small_talk as ST  # noqa: E402
from sf_assistant.config import load_config  # noqa: E402

#: the fixed lines of the caller's bridge (orchestrator/app/engines/sfk_bridge.py, read only): a reply that held one
#: would stop the next turn there
BRIDGE_LINES = ("This field is blocked, so it cannot be shown here.",
                "I could not tell what this refers to. Please ask the full question, with the name or object.",
                "One blocked field was left out.")


@pytest.mark.parametrize("text, kind", [
    ("hi", "greeting"), ("Hello!", "greeting"), ("hey there", "greeting"), ("Hiiii!!", "greeting"),
    ("hii", "greeting"), ("helo", "greeting"), ("hlo", "greeting"), ("good evning", "greeting"),
    ("Good morning team", "greeting"), ("namaste ji", "greeting"), ("kaise ho", "greeting"), ("hi 👋", "greeting"),
    ("how are you?", "greeting"), ("whats up", "greeting"),
    ("thanks", "thanks"), ("Thank you so much sir", "thanks"), ("thnx", "thanks"), ("thanx", "thanks"),
    ("thnaks", "thanks"), ("shukriya", "thanks"), ("dhanyavaad", "thanks"), ("ok thanks", "thanks"),
    ("thanks again!", "thanks"),
    ("ok", "ack"), ("okkk", "ack"), ("got it", "ack"), ("theek hai", "ack"), ("accha", "ack"), ("all right", "ack"),
    ("bye", "bye"), ("bye bye", "bye"), ("good night", "bye"), ("see you later", "bye"), ("take care", "bye"),
    ("are you there?", "presence"), ("are u there", "presence"), ("suno", "presence"), ("anyone there?", "presence"),
    ("testing", "presence"),
    ("can you tell me one thing", "empty_ask"), ("can I ask you something?", "empty_ask"),
    ("i have a question", "empty_ask"), ("ek baat batao", "empty_ask"), ("mujhe kuch puchna hai", "empty_ask"),
    ("ek sawal hai", "empty_ask"), ("can you help me", "empty_ask"), ("help me", "empty_ask"),
    ("hello!! can you tell me one thing?", "empty_ask"),
    ("help", "help"), ("what can you do?", "help"), ("who are you", "help"),
])
def test_kinds(text, kind):
    assert ST.small_talk(text) == kind


@pytest.mark.parametrize("text", ["hi, I am Zorvan", "Hi I'm Zorvan Quell here", "hello, this is Zorvan here",
                                  "hi, mera naam Zorvan hai"])
def test_a_self_introduction_after_a_greeting_is_a_greeting_and_is_never_echoed(text):
    assert ST.small_talk(text) == "greeting"
    assert "zorvan" not in ST.line("greeting").lower() and "quell" not in ST.line("greeting").lower()


@pytest.mark.parametrize("text", [
    "hi, how many candidates joined this month", "thanks, and last week?", "ok so what about May", "hello INT-0042",
    "yes", "no", "haan", "nahi", "yeah", "test interviews", "help me with interview counts", "I am Zorvan",
    "mera naam Zorvan hai", "hi hi hi hi hi hi hi hi hi hi hi", "what is Report_URL__c", "thanks zorvan@example.com",
    "hi " + "a" * 80, "how are you doing today", "hello, show interviews by status", "ok plot it", "", "   ", "!!!",
    "what is this",
])
def test_never_small_talk(text):
    assert ST.small_talk(text) is None


def test_limits():
    assert len("thanks " + "sir " * 19) > ST.MAX_CHARS and ST.small_talk("thanks " + "sir " * 19) is None
    assert ST.small_talk("thanks " + "sir " * 3) == "thanks"


def test_priority_and_every_kind_has_a_line():
    assert set(ST.LINES) == set(ST.PRIORITY) == ST.KINDS
    assert ST.small_talk("hi, thanks, bye") == "greeting"
    assert ST.small_talk("thanks, bye") == "thanks"
    assert ST.small_talk("hi are you there") == "presence"


def test_lines_leak_no_prompt_text_and_no_bridge_line():
    """L63: an empty ask once echoed a nudge of the knowledge agent. No line holds a sentence of a nudge, a system prompt
    or a fixed line of the caller's bridge."""
    sentences = set()
    for pid, p in P.all_prompts().items():
        if pid.endswith(("_message", "_system")) or pid in ("followup_rewrite",):
            sentences |= {s.strip() for s in p.text.replace("\n", " ").split(". ") if len(s.strip()) > 25}
    for kind, text in ST.LINES.items():
        assert not any(s in text for s in sentences), kind
        assert not any(b in text for b in BRIDGE_LINES), kind
        assert "<<" not in text and "{" not in text


class _NoModel:
    """A model client any call to fails the test."""

    cfg = types.SimpleNamespace(base_url="http://localhost:8000/v1")

    def chat(self, *a, **k):
        raise AssertionError("no model call for small talk")


def test_pipeline_answers_with_the_fixed_line_and_no_model_call():
    cfg = load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")
    o = core.answer("thanks!", cfg, None, None, _NoModel(), 1, None)
    assert o.final == ST.line("thanks") and o.kind == "none" and o.line_reason == "small_talk"
    assert o.small_talk == "thanks" and o.r is None and o.rec is None
    row = core.result_row(o, None)
    assert row["jsonl"]["line_reason"] == "small_talk" and row["jsonl"]["small_talk"] == "thanks"
    assert row["jsonl"]["answer"] == ST.line("thanks") and "display" not in row["jsonl"]


def test_a_real_question_after_a_greeting_reaches_the_model():
    calls = []

    class Model:
        cfg = types.SimpleNamespace(base_url="http://localhost:8000/v1")

        def chat(self, *a, **k):
            calls.append(1)
            return {"content": "From the knowledge files.", "tool_calls": [], "usage": {}}

    cfg = load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")
    o = core.answer("hi, how many candidates joined this month", cfg, _Registry(), None, Model(), 1, None)
    assert calls and o.line_reason == "" and o.small_talk == ""


class _Registry:
    tools: dict = {}
    resolver = None

    def openai_tools(self):
        return []


def test_rewrite_makes_no_call_for_small_talk_with_turns():
    turns = [{"role": "user", "content": "How many interviews last week?"},
             {"role": "assistant", "content": "There were 4 interviews last week."}]
    step = followup.rewrite("thanks!", turns, _NoModel())
    assert step == followup.Rewrite(followup.OK, "thanks!", reason="small_talk")
    assert followup.rewrite("thanks!", [], _NoModel()) == followup.Rewrite(followup.OK, "thanks!")


class _NoFresh:
    """A pipeline whose read copy may not be touched: small talk answers before ensure_fresh."""

    session = None
    llm = _NoModel()

    def ensure_fresh(self):
        raise AssertionError("ensure_fresh ran")

    def answer(self, *a, **k):
        raise AssertionError("the pipeline ran")


@pytest.mark.parametrize("text, kind", [("hi there", "greeting"), ("can you tell me one thing?", "empty_ask"),
                                        ("are you there", "presence")])
def test_the_ask_service_answers_first_also_with_records_off(text, kind):
    out = ask_mcp.AskService._job(types.SimpleNamespace(pipeline=_NoFresh()), text, None)
    assert out["result"] == ask_mcp._result("answered", ST.line(kind))
    assert out["reason"] == "small_talk" and out["detail"] == {"small_talk": kind} and not out["error"]


def test_decide_reads_small_talk_from_the_trace():
    shaped = {"kind": "none", "answer": ST.line("thanks"), "trace": {"line_reason": "small_talk", "queries": []}}
    assert ask_mcp.decide(shaped) == ("answered", "small_talk")
    shaped["trace"]["line_reason"] = "display_only"
    assert ask_mcp.decide(shaped) == ("answered", "code_line")


def test_the_page_shape_carries_the_reason():
    o = core.answer("ok", load_config(base_url="http://127.0.0.1:9/v1", api_key="k"), None, None, _NoModel(), 1, None)
    row = core.result_row(o, None)
    page = web_pipeline.Pipeline.__new__(web_pipeline.Pipeline)
    page._status = {}
    shaped = page._shape("ok", "merged", row, "", None, False)
    assert shaped["trace"]["line_reason"] == "small_talk" and shaped["answer"] == ST.line("ack")
    assert ask_mcp.decide(shaped) == ("answered", "small_talk")


def test_records_off_run_loads_no_engine_module(tmp_path):
    """The command line with --no-records: small talk prints its line and imports no engine module and no duckdb."""
    fixture = str(ROOT / "tests" / "fixtures" / "Internal_Interview__c.json")
    script = textwrap.dedent(f"""
        import contextlib, io, json, sys
        import sf_assistant.cli as mod
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(["--no-records", "--knowledge", {fixture!r}, "hello! are you there?"])
        print(json.dumps({{"code": code, "out": buf.getvalue(),
                           "engine": sorted(m for m in sys.modules if m.startswith("sf_assistant.engine")),
                           "duckdb": "duckdb" in sys.modules}}))
    """)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), OKEVAL_BASE_URL="http://127.0.0.1:9/v1", OKEVAL_API_KEY="test-key", SFA_CHARTS="on")
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                         timeout=120)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["code"] == 0 and ST.line("presence") in data["out"]
    assert data["engine"] == [] and data["duckdb"] is False


# ------------------------------------------------------------------------------------------------ review fixes (P6)
@pytest.mark.parametrize("text", [
    # E-01 / R5: "this" points at the last answer
    "how does this work", "how does this work?", "how do i use this",
    # E-02 / R1 / R2 of the replay: the words after "this is" / "I am" are no name
    "hey this is wrong", "hello, this is incorrect", "hi I am confused", "hi this is urgent", "hi this is not right",
    "hi, this is about interviews", "hey this is for placements", "hello, this is interviews data", "hi this is offers",
    "hey, this is candidate", "hi i am recruiter", "hi myself trainer", "hi, this is about Zorvan Quell",
    # yes / no outside a phrase
    "ok yes", "hi no", "no", "nope thanks bye",
])
def test_review_fixes_never_small_talk(text):
    assert ST.small_talk(text) is None


@pytest.mark.parametrize("text, kind", [
    # E-03: please inside a phrase, more empty asks, thanks, acks, presence, emoji
    ("can you please tell me one thing", "empty_ask"), ("can you please help me", "empty_ask"),
    ("can you help me with something", "empty_ask"), ("i have a small question", "empty_ask"),
    ("i want to know something", "empty_ask"), ("need help", "empty_ask"), ("i need your help", "empty_ask"),
    ("thank you so much for your help", "thanks"), ("thanks a lot for the help", "thanks"),
    ("thanks for the info", "thanks"), ("good job", "thanks"), ("well done", "thanks"),
    ("no thanks", "ack"), ("okie", "ack"), ("hmm", "ack"), ("that's all", "ack"), ("nothing else", "ack"),
    ("anyone?", "presence"), ("is anyone there", "presence"), ("what can i ask", "help"),
    ("how does this assistant work", "help"),
    ("\U0001F44D", "ack"), ("\U0001F64F", "thanks"), ("\U0001F44B", "greeting"),
    # a capitalised name, or a tail after it, is still part of a greeting
    ("hi this is Zorvan", "greeting"), ("hello, I am Zorvan Quell", "greeting"), ("hi this is zorvan here", "greeting"),
])
def test_review_fixes_kinds(text, kind):
    assert ST.small_talk(text) == kind


def test_review_fix_the_nudge_echo_is_never_shown():
    """E-22: a knowledge answer that echoes the require-tools nudge loses that sentence (the L63 reply shape, made-up
    wording); an answer without one is unchanged."""
    echo = "You\u2019re right\u2014I should not answer from memory. Which object do you mean?\n\n- Interviews\n- Offers"
    assert core.drop_nudge_echo(echo) == "Which object do you mean?\n\n- Interviews\n- Offers"
    assert core.drop_nudge_echo("You're right, I should not answer without calling a tool.") == ""
    plain = "`Interview_Status__c` holds the status. You are right that it is a picklist."
    assert core.drop_nudge_echo(plain) == plain


def test_review_fix_the_help_line_promises_no_chart():
    """R5: SFA_CHARTS=off turns charts off; no small-talk line promises one."""
    assert not any("chart" in text.lower() for text in ST.LINES.values())
