"""The freshness benchmark — a suite, not one political question.

The classifier decides whether a question may be answered from frozen weights.
Getting it wrong is expensive in both directions: too eager and every "what is
a for-loop" pays for retrieval; too lazy and the platform confidently names
last year's Vice President. So this is a small labelled corpus across the
categories that actually occur, run without any model call.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.freshness import Freshness, classify

NOW_YEAR = 2026


def verdict(question: str):
    return asyncio.run(classify(question, now_year=NOW_YEAR, allow_router=False))


#: (question, required level). Anything the deterministic pass cannot settle
#: belongs in AMBIGUOUS below rather than here — this list is the contract.
CASES = [
    # ── current office holders (the class that failed in production) ──
    ("who's vice president of india", Freshness.RECENT),
    ("Who is the Vice President of India?", Freshness.RECENT),
    ("who is the president of the united states", Freshness.RECENT),
    ("current prime minister of the uk", Freshness.RECENT),
    ("who is the governor of maharashtra", Freshness.RECENT),
    # ── company executives ──
    ("who is the CEO of NVIDIA", Freshness.RECENT),
    ("current chairman of Tata Sons", Freshness.RECENT),
    # ── software versions / releases ──
    ("latest Ubuntu version", Freshness.RECENT),
    ("what is the newest Python release", Freshness.RECENT),
    ("current version of PostgreSQL", Freshness.RECENT),
    # ── realtime ──
    ("Current NVIDIA stock price", Freshness.REALTIME),
    ("what happened in today's match", Freshness.REALTIME),
    ("weather in Bangalore", Freshness.REALTIME),
    ("USD to INR exchange rate right now", Freshness.REALTIME),
    # ── timeless science / definitions ──
    ("What is photosynthesis?", Freshness.STATIC),
    ("how does a transformer work", Freshness.STATIC),
    ("explain quantum entanglement", Freshness.STATIC),
    ("difference between TCP and UDP", Freshness.STATIC),
    ("what is the capital of France", Freshness.STATIC),
    ("formula for kinetic energy", Freshness.STATIC),
    # ── history: settled, and must not trigger a lookup ──
    ("Who invented Python?", Freshness.STATIC),
    ("who discovered penicillin", Freshness.STATIC),
    ("who won the 2019 election", Freshness.STATIC),
    ("history of the Roman empire", Freshness.STATIC),
]


@pytest.mark.parametrize("question,expected", CASES)
def test_freshness_classification(question, expected):
    got = verdict(question)
    assert got.requirement is expected, (
        f"{question!r} -> {got.requirement.value} (rule {got.reason}), "
        f"expected {expected.value}"
    )


def test_every_case_is_settled_without_a_model_call():
    """The whole point of the lexical pass: no router round trip for these."""
    for question, _ in CASES:
        assert verdict(question).reason != "default", (
            f"{question!r} fell through to the ambiguous default"
        )


def test_classification_is_effectively_free():
    """It runs on every assistant turn, so it has to cost nothing."""
    started = time.perf_counter()
    for _ in range(200):
        for question, _ in CASES:
            verdict(question)
    per_call_us = (time.perf_counter() - started) / (200 * len(CASES)) * 1e6
    # Generous ceiling: the measured figure is ~6 us, and asyncio.run dominates.
    assert per_call_us < 500, f"{per_call_us:.0f} us per classification"


def test_an_office_question_beats_the_timeless_phrasing():
    """"Who is the X" looks STATIC lexically; for an office it is not."""
    assert verdict("who is the vice president of india").requirement is Freshness.RECENT
    assert verdict("who is the author of Dune").requirement is Freshness.STATIC


def test_a_recent_year_is_recent_and_an_old_one_is_history():
    assert verdict("India GDP 2026").requirement is Freshness.RECENT
    assert verdict("India GDP 2015").requirement is Freshness.STATIC


def test_static_questions_do_not_need_evidence():
    assert not verdict("what is photosynthesis").needs_evidence
    assert verdict("who is the CEO of NVIDIA").needs_evidence


def test_max_age_tightens_with_volatility():
    static = verdict("what is photosynthesis").max_age_seconds
    recent = verdict("who is the CEO of NVIDIA").max_age_seconds
    realtime = verdict("NVIDIA stock price right now").max_age_seconds
    assert realtime < recent < static


def test_an_empty_question_is_harmless():
    assert verdict("").requirement is Freshness.STATIC
