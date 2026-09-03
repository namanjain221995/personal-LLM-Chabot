"""How current does an answer have to be?

Three levels, decided BEFORE any expensive work:

    STATIC    the answer does not change ("what is photosynthesis?")
    RECENT    it changes on the scale of months ("who is the VP of India?")
    REALTIME  it changes hourly or faster ("NVIDIA stock price right now")

WHY THIS EXISTS. The 35B's weights are frozen at its training cut-off, so on
2026-08-31 it answered "who's vice president of india" with Jagdeep Dhankhar —
confidently, and wrong, while 19 pages already stored on this machine said
C. P. Radhakrishnan. Knowing a question is time-sensitive is what lets the
platform reach for that evidence instead of trusting the weights.

CHEAP FIRST. The deterministic pass below settles the large majority of real
questions with regex work measured in microseconds. The router model (8B) is
consulted ONLY when the lexical signals are genuinely ambiguous, and never the
main model — spending a 35B call to decide whether to spend a 35B call is how
a "fast" mode stops being fast.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import metrics

log = logging.getLogger(__name__)


class Freshness(str, Enum):
    STATIC = "static"
    RECENT = "recent"
    REALTIME = "realtime"


@dataclass(frozen=True)
class Verdict:
    requirement: Freshness
    #: How old stored evidence may be before it stops counting as an answer.
    max_age_seconds: int
    #: Which rule fired — surfaced in metrics and the debug view, never to the
    #: user. Makes a misclassification diagnosable instead of mysterious.
    reason: str

    #: The answer can change within days ("latest release", "current
    #: price"): a stored page inside the RECENT window can still be stale
    #: and an auto-decided live search must not be skipped for it
    #: (ADR-0001 D6, design critique 2026-09-03).
    volatile: bool = False

    @property
    def needs_evidence(self) -> bool:
        """True when pretrained weights alone are not a trustworthy source."""
        return self.requirement is not Freshness.STATIC


#: Seconds. A REALTIME answer from a two-hour-old page is a wrong answer; a
#: RECENT one from a two-week-old page is usually still right, which is why
#: the two levels exist at all rather than one "is it fresh" boolean.
_MAX_AGE = {
    Freshness.REALTIME: 3 * 3600,
    Freshness.RECENT: 14 * 24 * 3600,
    Freshness.STATIC: 365 * 24 * 3600,
}

# --------------------------------------------------------------------------
# Deterministic signals.
#
# Word-boundary anchored on purpose: a bare "now" substring matches "known",
# "nowhere" and "knowledge", which would drag half of all questions into
# REALTIME and defeat the point of having levels.
# --------------------------------------------------------------------------

_REALTIME = re.compile(
    r"\b("
    r"right now|at the moment|as of now|currently trading|"
    r"stock price|share price|exchange rate|conversion rate|"
    r"today'?s|tonight|this morning|this afternoon|live score|"
    r"score (?:of|in) the|weather|temperature outside|"
    r"trending|breaking news|happening now"
    r")\b",
    re.I,
)

#: Roles whose holder changes — the class of fact that produced the original
#: failure. Asking "who is the X" about any of these is inherently RECENT even
#: with no other time word in the sentence.
_OFFICE = re.compile(
    r"\b("
    r"president|vice[- ]president|vp|prime minister|pm|chancellor|premier|"
    r"chief minister|governor|mayor|senator|ambassador|"
    r"ceo|cto|cfo|coo|chairman|chairperson|chairwoman|"
    r"managing director|executive director|head of|leader of|"
    r"captain|coach|manager of"
    r")\b",
    re.I,
)

_RECENT = re.compile(
    r"\b("
    r"current|currently|latest|newest|most recent|up[- ]to[- ]date|"
    r"today|now|nowadays|these days|this (?:year|month|week|quarter)|"
    r"recent|recently|so far|to date|"
    r"who leads|who runs|who won|who currently|"
    r"new (?:version|release|model|ceo|president)|"
    r"released|launch(?:ed|es)?|announce(?:d|ment)?|"
    r"version|release notes|changelog|roadmap|"
    r"price|cost|salary|valuation|market cap|"
    r"election|appointed|resigned|replaced|succeeded"
    r")\b",
    re.I,
)

#: Explicitly timeless shapes. These OVERRIDE a weak RECENT hit — "what is the
#: current through a resistor" is physics, not news, and "how does X work"
#: never needs a web fetch.
_STATIC = re.compile(
    r"\b("
    # Bare "what is X" too, not only "what is a X" — the narrower form let
    # "What is photosynthesis?" fall through to the ambiguous branch and cost
    # a router call for the most obviously timeless question there is.
    # Volatile phrasings are unaffected: _REALTIME and _OFFICE are tested
    # first, and "what is the latest version" also trips _RECENT, which makes
    # it ambiguous rather than static.
    r"what is|what'?s a|what are|what does .* mean|definition of|define|"
    r"how does .* work|how do .* work|how to|why does|why do|why is|"
    r"explain|describe the|difference between|compare|"
    r"formula for|theorem|proof of|derive|"
    r"history of|who invented|who discovered|who wrote|who created|"
    r"author of|writer of|creator of|composer of|director of the film|"
    r"born in|died in|founded in"
    r")\b",
    re.I,
)

#: A four-digit year in the question is a strong recency signal when it is
#: near the present, and a strong STATIC signal when it is clearly historical.
#: Markers that settle the question on their own, whatever else matches.
_STRONG_RECENT = re.compile(
    r"\b(latest|newest|most recent|current|currently|up[- ]to[- ]date|"
    r"as of today|right now)\b",
    re.I,
)

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _deterministic(question: str, now_year: int) -> Optional[Verdict]:
    """A confident answer, or None when the model should decide."""
    q = (question or "").strip()
    if not q:
        return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], "empty")

    if _REALTIME.search(q):
        return Verdict(Freshness.REALTIME, _MAX_AGE[Freshness.REALTIME], "lexical:realtime")

    # An office-holder question is RECENT even phrased as "who is the ...",
    # which the STATIC pattern would otherwise claim.
    if _OFFICE.search(q):
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:office")

    years = [int(m.group(0)) for m in _YEAR.finditer(q)]
    if years:
        newest = max(years)
        if newest >= now_year - 1:
            return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], f"year:{newest}")
        if newest <= now_year - 3:
            # "the 2019 election" is history; it is not going to change.
            return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], f"year:{newest}")

    # "latest/newest/current" is decisive: it outranks the generic timeless
    # shapes ("what is ...") that would otherwise tie with it and push an
    # obviously time-sensitive question to the router.
    if _STRONG_RECENT.search(q):
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:recent")

    static_hit = _STATIC.search(q)
    recent_hit = _RECENT.search(q)
    if static_hit and not recent_hit:
        return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], "lexical:static")
    if recent_hit and not static_hit:
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:recent")

    # Both fired, or neither did — genuinely ambiguous, so ask the router.
    return None


_ROUTER_SYSTEM = (
    "Classify how time-sensitive a question is. Answer with EXACTLY one word:\n"
    "STATIC — the true answer does not change (science, definitions, history, "
    "how things work).\n"
    "RECENT — the answer changes over months (who currently holds an office or "
    "job, latest software version, current prices of slow-moving things).\n"
    "REALTIME — the answer changes hourly (live scores, market prices, weather, "
    "breaking news).\n"
    "Answer with the single word only."
)

_WORD = {
    "static": Freshness.STATIC,
    "recent": Freshness.RECENT,
    "realtime": Freshness.REALTIME,
}


async def _ask_router(question: str) -> Optional[Verdict]:
    """One tiny non-thinking call on the 8B. None on any failure."""
    try:
        from . import llm

        reply = await llm.router_chat_completion(
            [
                {"role": "system", "content": _ROUTER_SYSTEM},
                {"role": "user", "content": question[:400]},
            ],
            temperature=0.0,
            max_tokens=4,
        )
    except Exception:  # noqa: BLE001 — classification must never cost the answer
        log.debug("freshness router unavailable", exc_info=True)
        return None
    word = (reply or "").strip().lower().strip(".").split()
    if not word:
        return None
    level = _WORD.get(word[0])
    if level is None:
        return None
    return Verdict(level, _MAX_AGE[level], "router")


def classify_offline(question: str, *, now_year: int) -> Verdict:
    """The deterministic verdict, with the ambiguous case settled as RECENT.

    For callers that must never wait on a model to decide how to treat time
    (Deep Research, which already plans with the main model and only needs
    the level to weight recency): the same regex pass as `classify`, minus
    the router round trip, minus the possibility of blocking on it.
    """
    verdict = _deterministic(question, now_year)
    if verdict is not None:
        return _with_volatility(verdict, question)
    return _with_volatility(
        Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "default"), question
    )


#: Shapes whose answer moves within days. A page inside the RECENT window
#: (14 d) that answers "latest vLLM release" can be three releases old.
_VOLATILE = re.compile(
    r"\b(release[sd]?|version|changelog|price[sd]?|pricing|stock|score[sd]?|"
    r"rate[sd]?|schedule|status)\b",
    re.I,
)
#: How old a stored page may be for a volatile question before it is worth a
#: lookup: a day, not two weeks.
VOLATILE_MAX_AGE_S = 24 * 3600
#: How long the freshness router may take before the deterministic default
#: stands. It shares the 8B model with fact extraction, titling and query
#: rewriting; under load its queue must not become Fast's time to first token.
ROUTER_DEADLINE_S = 0.6


def _with_volatility(verdict: Verdict, question: str) -> Verdict:
    q = question or ""
    volatile = verdict.requirement is Freshness.REALTIME or bool(
        _STRONG_RECENT.search(q) or _VOLATILE.search(q)
    )
    if not volatile or verdict.requirement is Freshness.STATIC:
        return verdict
    return Verdict(
        verdict.requirement,
        min(verdict.max_age_seconds, VOLATILE_MAX_AGE_S),
        verdict.reason,
        volatile=True,
    )


async def classify(question: str, *, now_year: int, allow_router: bool = True) -> Verdict:
    """How fresh must the evidence behind this answer be?

    `now_year` is passed in rather than read here so callers share ONE notion
    of "now" for a request — the same value that goes into the prompt.
    """
    verdict = _deterministic(question, now_year)
    if verdict is not None:
        return _with_volatility(verdict, question)
    if allow_router:
        started = time.perf_counter()
        asked: Optional[Verdict] = None
        try:
            async with asyncio.timeout(ROUTER_DEADLINE_S):
                asked = await _ask_router(question)
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="ok")
        except TimeoutError:
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="timeout")
        except Exception:  # noqa: BLE001 — the default below is the fallback
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="error")
        if asked is not None:
            return _with_volatility(asked, question)
    # Unclassifiable and no router: treat as RECENT. The failure this module
    # exists to prevent is answering a live question from stale weights, so an
    # unnecessary cache lookup is the cheaper mistake than a wrong fact.
    return _with_volatility(
        Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "default"), question
    )
