"""Reviewer reproductions (hotfix 1.2, P6): pasted PII reaching the search spy.
Each test asserts the SAFE behaviour, so a FAIL is a reproduction."""
from __future__ import annotations

import asyncio
import re

import pytest

from app import llm
from app.config import settings
from app.engines import search as search_engine
from tests.test_pasted_web_privacy import (  # noqa: F401  (fixture re-export)
    _SpyProvider, _chat, _copying_router, _no_stream, _stale_store, spy,
)

EMAIL = "priya.venkat.test@example.com"
PHONE = "98765 43210"
NAME = "venkataraman"
RECRUITER = "anita.rao.hr@orbiton-mutual.example"

LETTER = "\n".join([
    "Dear Hiring Manager,",
    "I am writing to apply for the Lead Data Engineer role on the claims platform team.",
    "For the last four years I have worked at Orbiton Mutual Insurance, reporting to the head of claims data engineering.",
    "I rebuilt the fraud scoring pipeline on Kafka and Flink and cut claim review time by 40 percent.",
    "Before that I spent four years at Kestrel Retail Labs building batch pipelines in Python and SQL.",
    "Yours sincerely, Priya Venkataraman",
    f"Please feel free to contact me at +91 {PHONE} or {EMAIL}",
])
#: An everyday ask: "improve" is not one of pasted._VERBS, so this is not a
#: transform ask; the letter's last line reads as a request (^please).
COVER = "can you improve this cover letter?\n\n" + LETTER

CV = "\n".join([
    "Priya Venkataraman",
    f"Senior Data Engineer, Pune. {EMAIL}, +91 {PHONE}",
    "Summary",
    "Nine years building batch and streaming pipelines for insurers and retailers across India.",
    "Experience",
    "Lead Data Engineer, Orbiton Mutual Insurance, 2021 to present",
    "Built the fraud scoring pipeline on Kafka and Flink, cutting claim review time by 40 percent.",
    "Data Engineer, Kestrel Retail Labs, 2017 to 2021",
])
CV_REWRITE = CV + "\n\nrewrite this in the same format as the sample below\n\n" + "\n".join(
    ["Name: A Person", "Role: Engineer", "Skills", "Python", "SQL"])

JD_ASK_TOP = "what do you think about this role?\n\n" + "\n".join([
    "Senior Data Engineer - Claims Platform", "Orbiton Mutual Insurance Private Limited",
    "Location Pune, hybrid, in office Monday and Wednesday at the Baner campus",
    "Requirements", "7+ years building batch and streaming data pipelines in Python and SQL",
    "hands on experience with Kafka, Flink or Spark Structured Streaming",
    "has run a dbt project with more than 400 models in production",
    f"Please email your resume to {RECRUITER}",
])


def _pii(queries):
    low = [q.lower() for q in queries]
    return [q[:120] for q in low if EMAIL in q or PHONE in q or NAME in q or RECRUITER in q]


async def _emit(kind, data):
    return None


def test_fast_lookup_cover_letter_contact_line(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    asyncio.run(lk.prepare(COVER, effort="fast", mode="assistant", web_search_pref="auto", allow_network=True))
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_fast_lookup_terse_followup_after_pasted_cv(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    history = [{"role": "user", "content": CV_REWRITE},
               {"role": "assistant", "content": "**Name:** Priya Venkataraman\n**Role:** Lead Data Engineer"}]

    async def turn():
        from app.core import pasted
        pasted.mark_turn("who heads it now?", CV_REWRITE)  # what main.py marks
        return await lk.prepare("who heads it now?", effort="fast", mode="assistant",
                                web_search_pref="auto", allow_network=True, history=history)

    asyncio.run(turn())
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_search_engine_pill_on_jd_recruiter_email(monkeypatch, spy):
    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)
    asyncio.run(search_engine.run_search_engine(JD_ASK_TOP, [], _emit, "think"))
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_search_engine_router_down_fallback_cover_letter(monkeypatch, spy):
    async def down(messages, **kw):
        raise RuntimeError("router down")
    monkeypatch.setattr(llm, "router_chat_completion", down)
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)
    asyncio.run(search_engine.run_search_engine(COVER, [], _emit, "think"))
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_search_engine_followup_after_cv_rewrite_sees_assistant_copy(monkeypatch, spy):
    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)
    history = [{"role": "user", "content": CV_REWRITE},
               {"role": "assistant", "content": f"**Name:** Priya Venkataraman\n**Contact:** {EMAIL}"}]

    async def turn():
        from app.core import pasted
        pasted.mark_turn("find similar open roles in Pune", CV_REWRITE)
        await search_engine.run_search_engine("find similar open roles in Pune", history, _emit, "think")

    asyncio.run(turn())
    print("ROUTER SAW PII", bool(_pii(seen)), "QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_chat_fast_cover_letter_end_to_end(monkeypatch, spy):
    _stale_store(monkeypatch)
    _chat(monkeypatch, spy, {"message": COVER, "effort": "fast", "web_search": "auto"})
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_chat_think_pill_on_jd_end_to_end(monkeypatch, spy):
    _chat(monkeypatch, spy, {"message": JD_ASK_TOP, "effort": "think", "web_search": "on"})
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []


def test_chat_fast_terse_followup_after_pasted_cv_end_to_end(monkeypatch, spy):
    _stale_store(monkeypatch)
    follow = "what's the latest salary for this?"
    msgs = [{"role": "user", "content": CV_REWRITE},
            {"role": "assistant", "content": "**Name:** Priya Venkataraman\n**Role:** Lead Data Engineer"},
            {"role": "user", "content": follow}]
    _chat(monkeypatch, spy, {"message": follow, "messages": msgs, "effort": "fast", "web_search": "auto",
                             "conversation_id": "rv-terse-1"})
    print("QUERIES", spy.queries)
    assert _pii(spy.queries) == []
