"""The Fast small-talk lane (2026-09-14).

A plain "hi ??" at Fast took 6-8 s and came back citing a Wikipedia page
titled "HI" and a movie trailer: the turn ran the freshness router, the
speculative retrieval, the reranker, cross-chat recall and compaction, and
then sent the model a ~3,000-token prompt — for a greeting. The main model's
own time to first token was 0.05-0.09 s; everything else was the pre-pass.

The lane answers a CLOSED set of pleasantries (a greeting, thanks, a
farewell, laughter, one friendly emoji) with the model alone: no router, no
retrieval, no rerank, no cross-chat recall, a short prompt. Everything else
takes the full path, and the lane is built so that a miss costs only latency
and a false entry is as hard as possible:

- the lexicon is this module's OWN phrase tables, fullmatched against the
  whole normalised message. It is deliberately NOT freshness._SMALL_TALK,
  which fullmatches "ok", "cool", "again" and "a lot" — a bare "again" is a
  regenerate command, "a lot" answers a question and "ok" accepts an offer;
- acknowledgements ("ok", "got it", "👍", "🙏" alone, "theek hai") are not in
  the lane at all this round: they answer the assistant's last offer, and
  "Shall I check today's gold rate?" -> "👍" needs the live-value path;
- a turn typed after an UNANSWERED one ("hi ??" after a generation died
  mid-answer means "are you there?") takes the full path, because the full
  path resolves the unanswered question's subject from history;
- every live-value, URL, attachment, artifact and video cue the rest of the
  orchestrator knows is checked again as defence in depth.

The code never uses a "no content words" test: a greeting is recognised by
what it IS, never by what it lacks.

No import side effects, no database and no network here. main.py adds the
one check that needs the database (the previous send's durable status).
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional, Sequence

#: How long the lane waits for the saved-facts read before answering without
#: it. The read is a single indexed query; a slow database costs the facts
#: block on this one greeting, never the greeting's speed.
FAST_LANE_FACTS_WAIT_S = 0.15

#: The lane's generation ceiling. It does not affect time to first token, and
#: a smaller cap visibly truncates a model that decides to say more.
FAST_LANE_MAX_TOKENS = 1024

#: The lane's prompt carries this many exchanges of the conversation, each
#: clipped to FAST_LANE_TURN_CHARS. Stored history is untouched: the next real
#: question still sees the whole conversation.
FAST_LANE_HISTORY_TURNS = 2
FAST_LANE_TURN_CHARS = 1000

#: Longest normalised message the lane considers.
MAX_CHARS = 40
#: Raw length beyond which nothing is normalised at all (a paste is never a
#: pleasantry, and NFKC over a 250 KB paste is not free).
_RAW_PRECHECK_CHARS = 200

CATEGORIES = ("greeting", "thanks", "farewell", "laughter", "emoji")

#: The closed veto vocabulary, also the `veto` label of fast_lane_total.
VETOES = (
    "none",
    "disabled",
    "not_fast",
    "not_assistant",
    "too_long",
    "digit",
    "url",
    "attachment",
    "flags",
    "sf",
    "artifact_intent",
    "live_signal",
    "cue",
    "unanswered_previous",
    "pending_offer",
    "not_lexicon",
)


@dataclass(frozen=True)
class LaneDecision:
    entered: bool
    category: str = "none"
    veto: str = "none"

    def as_details(self) -> dict:
        return {"entered": self.entered, "category": self.category, "veto": self.veto}


def enabled() -> bool:
    """FAST_LANE_ENABLED, default true. Read per call so the kill switch
    needs no restart of anything that imported this module."""
    raw = os.environ.get("FAST_LANE_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------
# The lexicon
# --------------------------------------------------------------------------

_GREETING = (
    "hi", "hello", "hey", "hiya",
    "hi there", "hello there", "hey there",
    "good morning", "good afternoon", "good evening", "suprabhat", "सुप्रभात",
    "how are you", "how are you doing", "how's it going", "hows it going",
    "what's up", "whats up",
    "nice to meet you",
    "namaste", "namaskar", "नमस्ते", "नमस्कार",
    "kem cho", "કેમ છો",
    "jai shree krishna",
    "kya haal hai", "kaise ho", "aap kaise hain", "आप कैसे हैं",
    "जय श्री कृष्ण", "જય શ્રી કૃષ્ણ",
)
_THANKS = (
    "thanks", "thank you", "thanks a lot", "thank you so much", "thanks again",
    "much appreciated", "thx",
    "dhanyavaad", "shukriya", "धन्यवाद", "शुक्रिया",
    "aabhar", "આભાર",
)
_FAREWELL = (
    "bye", "goodbye", "good night", "see you", "see you later", "take care", "ttyl",
    "alvida", "अलविदा", "shubh ratri", "शुभ रात्रि", "aavjo", "આવજો",
)

#: Optional words after any phrase. "again", "so much" and "a lot" are tails
#: of THANKS only: on their own they are a regenerate command and answers.
_TAILS = ("ji", "bhai", "sir", "dost", "there")
_THANKS_TAILS = _TAILS + ("again", "so much", "a lot")

#: Emoji that are a pleasantry on their own (optionally repeated).
_SOLO_EMOJI = ("👋", "😊", "😂", "🤣", "❤")
#: Emoji that may close a Latin phrase. 🙏, 👍 and 👌 ALONE mean "yes
#: please" and are not in any table; 🙏 after "thank you" is thanks.
_CLOSING_EMOJI = ("👋", "🙏", "😊")


def _alternation(words: Sequence[str]) -> str:
    # Longest first, so "thank you so much" is not cut at "thank you".
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _phrase_re(phrases: Sequence[str], tails: Sequence[str]) -> "re.Pattern[str]":
    return re.compile(rf"(?:{_alternation(phrases)})(?: (?:{_alternation(tails)}))?")


_TABLES = (
    ("greeting", _phrase_re(_GREETING, _TAILS)),
    ("thanks", _phrase_re(_THANKS, _THANKS_TAILS)),
    ("farewell", _phrase_re(_FAREWELL, _TAILS)),
    ("laughter", re.compile(r"(?:lol|lmao|ha(?:ha)+|he(?:he)+|हा(?:हा)+)")),
)
_SOLO_EMOJI_RE = re.compile(rf"({_alternation(_SOLO_EMOJI)})(?: ?\1)*")
_CLOSING_EMOJI_RE = re.compile(rf" ?(?:{_alternation(_CLOSING_EMOJI)})(?: ?(?:{_alternation(_CLOSING_EMOJI)}))*$")
_LATIN_ONLY = re.compile(r"[a-z' ]+")

#: Skin-tone modifiers, zero-width joiner, variation selector 16.
_INVISIBLE = re.compile("[\U0001F3FB-\U0001F3FF\u200d\ufe0f]")
_WS = re.compile(r"\s+")
_TRAILING_SOFT = re.compile(r"[!.~,\s]+$")
_TRAILING_QUESTIONS = re.compile(r"\?+$")


def _normalise(raw: str) -> tuple[str, bool]:
    """(normalised text, whether ONE trailing run of '?' was removed)."""
    s = unicodedata.normalize("NFKC", raw)
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = _INVISIBLE.sub("", s).casefold()
    s = _WS.sub(" ", s).strip()
    s = _TRAILING_SOFT.sub("", s)
    asked = False
    m = _TRAILING_QUESTIONS.search(s)
    if m:
        asked = True
        s = _TRAILING_SOFT.sub("", s[: m.start()])
    return s.strip(), asked


def _denied_raw(raw: str) -> bool:
    """Deny before normalising: a message with no letter and no emoji the
    lexicon knows ("??", "?", "🤔", "…") is a question about the last turn,
    and normalising it would leave nothing to deny."""
    stripped = raw.strip()
    if not stripped:
        return True
    if any(ch.isalpha() for ch in stripped):
        return False
    return not any(e in stripped for e in _SOLO_EMOJI)


def classify_pleasantry(raw_text: str) -> Optional[str]:
    """The pleasantry category of the WHOLE message, or None."""
    raw = raw_text or ""
    if len(raw) > _RAW_PRECHECK_CHARS or _denied_raw(raw):
        return None
    text, asked = _normalise(raw)
    if not text or len(text) > MAX_CHARS:
        return None
    # A second run of '?' ("hi?!?") is not a greeting shape.
    if "?" in text:
        return None
    if _SOLO_EMOJI_RE.fullmatch(text):
        return None if asked else "emoji"
    phrase = text
    closing = _CLOSING_EMOJI_RE.search(text)
    if closing:
        phrase = text[: closing.start()].strip()
        # Only a Latin phrase may carry a closing emoji.
        if not phrase or not _LATIN_ONLY.fullmatch(phrase):
            return None
    for category, pattern in _TABLES:
        if pattern.fullmatch(phrase):
            # "lol?", "thanks?", "bye?" are confusion or sarcasm, not the
            # pleasantry; only a greeting may end in '?' ("hello?").
            if asked and category != "greeting":
                return None
            return category
    return None


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------

_ASCII_DIGIT = re.compile(r"[0-9]")
_FAILED_META_KEYS = ("error", "interrupted", "cancelled", "canceled", "failed")
_FAILED_STATUSES = {"error", "failed", "interrupted", "cancelled", "canceled", "queued", "running"}


def previous_turn_unanswered(history: Sequence[Any]) -> bool:
    """The conversation's last turn has no usable answer: the last message is
    the user's own, or the last assistant message is empty or marked failed,
    interrupted or cancelled. System blocks are not turns."""
    for message in reversed(list(history or ())):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            continue
        if role != "assistant":
            return True
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return True
        meta = message.get("meta")
        if isinstance(meta, dict):
            if any(meta.get(k) for k in _FAILED_META_KEYS):
                return True
            if str(meta.get("status") or "").lower() in _FAILED_STATUSES:
                return True
        return False
    return False


#: How much of the last answer's END is read for an open offer.
_OFFER_TAIL_CHARS = 400
#: An offer or a question the assistant left open. Verified live (2026-09-15):
#: "thanks" after "Want me to write the full script with gzip support?" made the
#: lane write a 755-token script with no code rules under the 1024 cap — a
#: pleasantry that answers an offer is an ACCEPTANCE, and "thank you 🙏" after
#: "Shall I check today's gold rate?" is the live-value shape. A miss here
#: costs only the full path's latency, so the pattern over-reads on purpose.
_OFFER_RE = re.compile(
    r"\b(?:shall i|should i|want me to|would you like|do you want|would it help|"
    r"if you(?:'d| would) like|i can also|i could also|i can (?:write|draft|build|make|create|"
    r"check|look|find|send|prepare|generate|expand|add|run|share|convert|translate|summari[sz]e)|"
    r"let me know (?:if|whether) you(?:'d| would) like|i(?:'d| would) be (?:happy|glad) to|"
    r"need me to|shall we|should we)"
)
#: Question endings that hand the turn back without offering anything.
_GENERIC_CLOSER = re.compile(
    r"(?:(?:so,? )?(?:how|what) (?:else )?can i (?:help(?: you)?|assist(?: you)?|do for you)(?: with)?"
    r"(?: today| now| anything else| next)?|(?:is there )?anything else(?: i can (?:help|assist)(?: you)?(?: with)?)?|"
    r"how are you(?: doing)?(?: today)?|how(?:'s| is) your day(?: going)?|what's on your mind(?: today)?)\?"
)
_SENTENCE_BREAK = re.compile(r"[.!\n\u0964]")


def assistant_left_an_offer(history: Sequence[Any]) -> bool:
    """The last assistant answer ENDS with an offer or an open question (other
    than a generic "how can I help?"): a pleasantry now may be its answer."""
    for message in reversed(list(history or ())):
        if not isinstance(message, dict) or str(message.get("role") or "") == "system":
            continue
        if message.get("role") != "assistant":
            return False
        content = message.get("content")
        if not isinstance(content, str):
            return False
        tail = unicodedata.normalize("NFKC", content[-_OFFER_TAIL_CHARS:])
        tail = tail.replace("\u2019", "'").replace("\u2018", "'").casefold()
        tail = _WS.sub(" ", _INVISIBLE.sub("", tail)).strip()
        if _OFFER_RE.search(tail):
            return True
        # Trailing emoji, markdown and punctuation other than '?' do not
        # change whether the answer ended on a question.
        core = re.sub(r"[^\w?)\]'\"]+$", "", tail)
        if "?" not in core[-2:]:
            return False
        last = _SENTENCE_BREAK.split(core)[-1].strip(" *_\"'")
        return not _GENERIC_CLOSER.fullmatch(last)
    return False


def _live_or_named(text: str, now_year: int) -> bool:
    from . import freshness

    return bool(
        freshness._live_signal(text)
        or freshness._names_something(text)
        or freshness._VERSIONISH.search(text)
        or freshness._YEAR.search(text)
    )


def _cue(text: str) -> bool:
    from .engines import crawl, video

    return bool(
        video._ABOUT_VIDEO_RE.search(text)
        or crawl.detect_crawl(text)
        or crawl.detect_resume(text)
    )


def _artifact_intent(text: str) -> bool:
    from .artifacts import intent as artifact_intent

    # has_artifacts=True is the strict reading ("make it shorter" could mean
    # a file), so no artifact list has to be read to decide.
    return artifact_intent.decide(text, has_artifacts=True, has_assistant_answer=True).action != "none"


def decide(request: Any, *, text: str, history: Sequence[Any], now_year: int) -> LaneDecision:
    """May this turn take the small-talk lane? `request` is the ChatRequest,
    `text` the text the worker is about to answer, `history` the prior turns."""
    if not enabled():
        return LaneDecision(False, veto="disabled")
    if str(getattr(request, "effort", "") or "") != "fast":
        return LaneDecision(False, veto="not_fast")
    if str(getattr(request, "mode", "") or "") != "assistant":
        return LaneDecision(False, veto="not_assistant")
    if (
        getattr(request, "pdf_data", None)
        or getattr(request, "pdf_uploads", None)
        or getattr(request, "video_uploads", None)
        or getattr(request, "image_data", None)
    ):
        return LaneDecision(False, veto="attachment")
    raw_request_text = str(getattr(request, "text", "") or "")
    if (
        getattr(request, "agent", False)
        or getattr(request, "deep_research", False)
        or str(getattr(request, "web_search", "") or "") == "on"
        # Nothing upstream may have expanded or rewritten the message.
        or not text
        or text != raw_request_text.strip()
    ):
        return LaneDecision(False, veto="flags")
    if getattr(request, "sf_live", False) or getattr(request, "clarification", None):
        return LaneDecision(False, veto="sf")
    if len(text) > _RAW_PRECHECK_CHARS or len(_normalise(text)[0]) > MAX_CHARS:
        return LaneDecision(False, veto="too_long")
    if _ASCII_DIGIT.search(text):
        return LaneDecision(False, veto="digit")
    from .engines.crawl import _URL_RE

    if _URL_RE.search(text):
        return LaneDecision(False, veto="url")
    category = classify_pleasantry(text)
    if category is None:
        return LaneDecision(False, veto="not_lexicon")
    if _artifact_intent(text):
        return LaneDecision(False, category, "artifact_intent")
    if _live_or_named(text, now_year):
        return LaneDecision(False, category, "live_signal")
    if _cue(text):
        return LaneDecision(False, category, "cue")
    if previous_turn_unanswered(history):
        return LaneDecision(False, category, "unanswered_previous")
    if assistant_left_an_offer(history):
        return LaneDecision(False, category, "pending_offer")
    return LaneDecision(True, category, "none")


def record(decision: LaneDecision) -> None:
    """Count the decision (fast_lane_total, closed labels)."""
    from . import metrics

    metrics.inc(
        "fast_lane_total",
        "Fast small-talk lane decisions by result, category and veto reason.",
        result="entered" if decision.entered else "vetoed",
        category=decision.category,
        veto=decision.veto,
    )
