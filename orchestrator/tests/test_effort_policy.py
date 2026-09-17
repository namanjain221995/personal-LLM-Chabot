"""The multi-step-reasoning classifier (core/effort_policy.py).

It has no runtime caller since 2026-09-17 — Fast never thinks, and the rule is
enforced for the whole turn in `llm`, not judged per prompt (the grant API this
file also covered went with it). The classifier and its gates stay, so a future
round that wants it for something honest starts from a measured baseline.

The gates the owner set (2026-09-15): on a labelled set of >= 150 prompts
(>= 60 must-think, >= 90 must-not, in English, Hindi, Gujarati and Hinglish,
including numbers that need no reasoning) precision on must-think >= 0.95 and
the false-positive rate on must-not <= 3%, deterministic and < 5 ms a prompt.
"""
from __future__ import annotations

import time

import pytest

from app.core import effort_policy
from tests.effort_policy_labels import HELD_OUT_MUST_NOT, HELD_OUT_MUST_THINK, MUST_NOT, MUST_THINK

THINK = MUST_THINK + HELD_OUT_MUST_THINK
DIRECT = MUST_NOT + HELD_OUT_MUST_NOT


def test_the_labelled_set_is_big_enough_and_covers_the_named_traps():
    assert len(THINK) >= 60
    assert len(DIRECT) >= 90
    assert len(THINK) + len(DIRECT) >= 150
    assert not set(THINK) & set(DIRECT)
    # The numbers-that-need-no-reasoning the owner named.
    for trap in ("what is 5G", "top 10 movies 2024", "iPhone 15 price in India"):
        assert trap in DIRECT
    assert any("GST rate" in p for p in DIRECT)
    # Every language the policy claims.
    joined = " ".join(THINK)
    assert any("ऀ" <= ch <= "ॿ" for ch in joined)  # Devanagari
    assert any("઀" <= ch <= "૿" for ch in joined)  # Gujarati
    assert "kitne" in joined and "ketli" in joined  # Hinglish / Gujarati Latin


def test_precision_and_false_positive_rate_meet_the_gates():
    true_pos = [p for p in THINK if effort_policy.classify(p).think]
    false_pos = [p for p in DIRECT if effort_policy.classify(p).think]
    precision = len(true_pos) / max(1, len(true_pos) + len(false_pos))
    fp_rate = len(false_pos) / len(DIRECT)
    recall = len(true_pos) / len(THINK)
    assert precision >= 0.95, (precision, false_pos)
    assert fp_rate <= 0.03, (fp_rate, false_pos)
    # Not a gate the owner set, but a classifier that never fires would pass
    # the two above: pin that it does the job it exists for.
    assert recall >= 0.90, (recall, [p for p in THINK if p not in true_pos])


def test_the_owners_puzzle_thinks():
    decision = effort_policy.classify(MUST_THINK[0])
    assert decision.think
    assert decision.reason in {"ratio", "measurement"}


@pytest.mark.parametrize(
    "prompt",
    [
        "what is 5G",
        "top 10 movies 2024",
        "GST rate on mobile phones in India",
        "iPhone 15 price in India",
        "hi",
        "Translate to Hindi: If a train travels 60 km in 1 hour, how far will it go in 3 hours?",
        "Summarize this: revenue was 4.5 billion, up 12% from 4.0 billion.",
        "Write a python function to reverse a string",
    ],
)
def test_named_non_reasoning_prompts_stay_direct(prompt):
    assert effort_policy.classify(prompt).think is False


def test_decisions_are_deterministic_and_reasons_are_a_closed_set():
    for prompt in THINK + DIRECT:
        first = effort_policy.classify(prompt)
        again = effort_policy.classify(prompt)
        assert (first.think, first.reason, first.score, first.signals) == (
            again.think, again.reason, again.score, again.signals,
        )
        assert first.reason in effort_policy.REASONS


def test_empty_and_none_are_direct():
    assert effort_policy.classify("").think is False
    assert effort_policy.classify(None).reason == "empty"
    assert effort_policy.classify("   \n ").think is False


def test_the_trace_carries_names_never_the_prompt():
    prompt = "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much is the ball?"
    trace = effort_policy.classify(prompt).as_trace()
    assert set(trace) == {"think", "reason", "score", "signals", "classify_ms"}
    assert "bat" not in repr(trace) and "1.10" not in repr(trace)


def _best_ms(prompt: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        effort_policy.classify(prompt)
        best = min(best, (time.perf_counter() - started) * 1000)
    return best


def test_every_labelled_prompt_classifies_in_under_5ms():
    slowest = max((_best_ms(p), p[:40]) for p in THINK + DIRECT)
    assert slowest[0] < 5.0, slowest


@pytest.mark.parametrize(
    "text",
    [
        "1" * 20_000,          # was 6.4 s before quantifiers were bounded
        "x" * 20_000,
        "1+" * 8_000,
        "f(" * 9_000,
        "a[" * 9_000,
        "1:" * 9_000,
        "कितने लीटर " * 2_000,
        "def f(x):\n    return x[0] == 1\n" * 600,
        # The verifier's additions (inline SQL, chained comparisons, Indic
        # word numbers, lookups): bounded the same way.
        "SELECT " + "a " * 20_000 + "FROM",
        "taller than " * 3_000,
        "तीन " * 5_000,
        "who " + "x" * 20_000 + " tallest",
    ],
)
def test_pathological_long_input_stays_bounded(text):
    # Only the head and tail are scanned. The bound here is looser than the
    # 5 ms target (measured 2.5-4.9 ms on the DGX host) so a slower CI runner
    # does not flake, and still fails by three orders of magnitude on the
    # catastrophic backtracking it guards against.
    assert _best_ms(text) < 25.0
