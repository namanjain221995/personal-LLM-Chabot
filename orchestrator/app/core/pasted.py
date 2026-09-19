"""Pasted text: the person's own words, and the material they pasted.

THE REPORT (hotfix 1.2). A ~10,000-character job description pasted as plain
lines, then one line of the person's own words ("... the sample format below
change it in the same way"), then a plain-lines sample. The composer folds a
paste inline with no marker (frontend/lib/pasted.ts), so every consumer read
the whole message as if the person had said all of it. The freshness rule read
"report to the head of care technology" as an office-holder question, and the
Fast pre-pass searched the web with the ENTIRE 9,961-character paste as the
query: the person's pasted text, sent to third-party engines through SearXNG.

This module is the one place that tells the person's words from the material.
Pure: regex over the message, no model, no I/O. English only (owner decision,
2026-09-16).

WHAT COUNTS AS A PASTE. At least PASTE_MIN_CHARS of material, on several lines
(or one very long line). A short message is the person talking, whatever it
contains, and nothing here changes for it.

WHAT COUNTS AS THE ASK. A line of at most ASK_MAX_CHARS that asks to transform
supplied text (rewrite, reformat, summarise, translate, "change it in the same
way", "like the sample") AND points at that text (this, it, below, the
sample...). A line addressed to "any AI assistant" is never the person's ask: it
is material. When several lines qualify, lines standing next to a blank line or
at either end of the message win: the person's ask sits between pasted blocks.

A WEB QUERY carries only the person's own words (`web_query`), and
`without_paste` is the last check before a search provider: any query holding a
PASTE_RUN_CHARS run of this turn's pasted material is dropped.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

#: Material shorter than this is not a paste: the person typed it.
PASTE_MIN_CHARS = 300
#: ...and it spans at least this many non-blank lines, unless it is one line
#: of at least PASTE_ONE_LINE_CHARS (a paragraph copied out of a page).
PASTE_MIN_LINES = 4
PASTE_ONE_LINE_CHARS = 1000
#: A line longer than this is part of the paste, never the person's ask. The
#: reported ask is 84 characters.
ASK_MAX_CHARS = 300
#: A web query built from a message carries at most this much of the person's
#: own words.
WEB_QUERY_MAX_CHARS = 200
#: A query carrying a run of pasted material this long is dropped before it
#: reaches a search provider (the privacy bar: no 40-character substring).
PASTE_RUN_CHARS = 40

_VERBS = (
    r"re-?writ(?:e|es|ing|ten)|rewrote|re-?format(?:s|ted|ting)?|format(?:s|ted|ting)?|"
    r"re-?structur(?:e|es|ed|ing)|re-?phras(?:e|es|ed|ing)|re-?word(?:s|ed|ing)?|"
    r"paraphras(?:e|es|ed|ing)|summari[sz](?:e|es|ed|ing)|summary|tl;?dr|condens(?:e|es|ed|ing)|"
    r"shorten(?:s|ed|ing)?|proof-?read(?:s|ing)?|convert(?:s|ed|ing)?|translat(?:e|es|ed|ing)|"
    r"tidy|clean(?:ed|ing)?\s+up|polish(?:es|ed|ing)?|edit(?:s|ed|ing)?"
)
#: A request to transform supplied text.
_TRANSFORM = re.compile(
    rf"\b(?:{_VERBS}|"
    r"(?:change|make|turn|put|modify|update|adapt|adjust|arrange|redo)\s+(?:it|this|them|these|that)\b|"
    r"same\s+(?:format|way|shape|style|structure|layout|pattern|template|manner)|"
    r"sample|template|"
    r"in\s+the\s+(?:following|given|below|above)\s+(?:format|style|structure|layout))\b",
    re.I,
)
#: ...that points at the supplied text.
_POINTS_AT_TEXT = re.compile(
    r"\b(?:this|these|it|them|that|below|above|following|same|sample|template|pasted|given|"
    r"attached|mentioned)\b"
    r"|\bthe\s+(?:jd|job\s+description|text|post|posting|document|doc|content|paragraph|passage|"
    r"resume|cv|email|mail|letter|article|notes?|draft|description|requirements?|details|data)\b",
    re.I,
)
#: A line addressed to a model rather than to a reader: pasted material,
#: whatever it asks for.
_ADDRESSED_TO_A_MODEL = re.compile(
    r"\b(?:(?:note|message|instructions?|reminder)\s+(?:to|for)\s+(?:any\s+|every\s+|the\s+|an?\s+)?"
    r"(?:ai|assistant|chatbot|bot|llm|language\s+model|model|gpt|chatgpt)"
    r"|(?:any|every)\s+(?:ai|assistant|llm|chatbot|language\s+model)"
    r"|if\s+you\s+are\s+an?\s+(?:ai|assistant|llm|language\s+model|chatbot)"
    r"|ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions)\b",
    re.I,
)
#: "Rewrite this: <the text, on the same line>".
_LEAD = re.compile(
    rf"^\W*(?:(?:please|pls|kindly)\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    rf"(?:{_VERBS})\b[^:\n]{{0,80}}:",
    re.I,
)
#: A question or request, for the web query of a paste whose ask is not a
#: transform ("<JD>\n\nwhat does this role pay in Pune today?").
_QUESTION = re.compile(
    r"\?\s*$|^\W*(?:what|how|why|who|when|where|which|can|could|would|should|is|are|do|does|"
    r"please|tell|give|find|search|look\s+up|compare|explain|list|show)\b",
    re.I,
)


@dataclass(frozen=True)
class Pasted:
    """A message read as the person's ask plus the material they pasted."""

    #: ("ask" | "paste", text) in message order.
    parts: Tuple[Tuple[str, str], ...]

    @property
    def asks(self) -> List[str]:
        return [t for kind, t in self.parts if kind == "ask"]

    @property
    def material(self) -> List[str]:
        return [t for kind, t in self.parts if kind == "paste"]


def _lines(text: str) -> List[str]:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def is_paste(text: str) -> bool:
    """Is this much text material rather than something a person typed?"""
    body = (text or "").strip()
    if len(body) < PASTE_MIN_CHARS:
        return False
    non_blank = sum(1 for ln in body.split("\n") if ln.strip())
    return non_blank >= PASTE_MIN_LINES or len(body) >= PASTE_ONE_LINE_CHARS


def _asks_to_transform(line: str) -> bool:
    s = line.strip()
    return (
        0 < len(s) <= ASK_MAX_CHARS
        and bool(_TRANSFORM.search(s))
        and bool(_POINTS_AT_TEXT.search(s))
        and not _ADDRESSED_TO_A_MODEL.search(s)
    )


def _at_boundary(lines: Sequence[str], i: int) -> bool:
    first = next((k for k, ln in enumerate(lines) if ln.strip()), 0)
    last = max((k for k, ln in enumerate(lines) if ln.strip()), default=0)
    if i in (first, last):
        return True
    before = lines[i - 1].strip() if i > 0 else ""
    after = lines[i + 1].strip() if i + 1 < len(lines) else ""
    return not before or not after


def read(message: str) -> Optional[Pasted]:
    """The message as ask + pasted material, or None when it is not a request
    to transform pasted text."""
    lines = _lines(message)
    candidates = [i for i, ln in enumerate(lines) if _asks_to_transform(ln)]
    if candidates:
        edged = [i for i in candidates if _at_boundary(lines, i)]
        asks = set(edged or candidates)
        parts: List[Tuple[str, str]] = []
        block: List[str] = []
        for i, ln in enumerate(lines):
            if i in asks:
                if "\n".join(block).strip():
                    parts.append(("paste", "\n".join(block).strip("\n")))
                block = []
                parts.append(("ask", ln.strip()))
            else:
                block.append(ln)
        if "\n".join(block).strip():
            parts.append(("paste", "\n".join(block).strip("\n")))
        pasted = Pasted(tuple(parts))
        return pasted if is_paste("\n".join(pasted.material)) else None
    # "Rewrite this: <long text on the same line>".
    stripped = (message or "").strip()
    lead = _LEAD.match(stripped)
    if lead and is_paste(stripped[lead.end():]):
        return Pasted((("ask", stripped[: lead.end()].strip()), ("paste", stripped[lead.end():].strip())))
    return None


def is_transform_ask(message: str) -> bool:
    """A rewrite / reformat / summarise / translate ask over pasted text: one
    pass of the chat engine, and nothing to look up on the web."""
    return read(message) is not None


def _question_lines(text: str) -> List[str]:
    """The first and last non-blank lines of a paste, when they read as the
    person's question or request. At most two short lines: anything longer,
    or anything in between, is material."""
    lines = [ln.strip() for ln in _lines(text) if ln.strip()]
    picked: List[str] = []
    for ln in (lines[:1] + lines[-1:]) if lines else []:
        if (
            len(ln) <= ASK_MAX_CHARS
            and _QUESTION.search(ln)
            and not _ADDRESSED_TO_A_MODEL.search(ln)
            and ln not in picked
        ):
            picked.append(ln)
    return picked


def own_words(message: str) -> str:
    """What the PERSON said in a message: the whole message when it is not a
    paste, the ask (or a question at either end) when it is, "" when a paste
    carries no words of theirs that can be told apart."""
    text = (message or "").strip()
    if not is_paste(text):
        return text
    pasted = read(text)
    if pasted is not None:
        return " ".join(pasted.asks)
    return " ".join(_question_lines(text))


def web_query(message: str) -> str:
    """A web-search query for this message that carries none of what was
    pasted into it: the person's own words, at most WEB_QUERY_MAX_CHARS, cut
    at a word. "" means there is nothing of theirs to search for."""
    words = " ".join(own_words(message).split())
    if len(words) <= WEB_QUERY_MAX_CHARS:
        return words
    cut = words[:WEB_QUERY_MAX_CHARS]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def own_turns(turns: Sequence[dict]) -> List[dict]:
    """Conversation turns for a prompt whose output becomes a web query: every
    user turn reduced to the person's own words (an earlier turn's paste would
    otherwise be rewritten into this turn's search phrases)."""
    out: List[dict] = []
    for turn in turns:
        content = turn.get("content")
        if turn.get("role") == "user" and isinstance(content, str) and is_paste(content):
            turn = {**turn, "content": own_words(content)}
        out.append(turn)
    return out


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def pasted_material(message: str) -> str:
    """The pasted part of a message, normalised for matching ("" if none)."""
    text = message or ""
    if not is_paste(text):
        return ""
    pasted = read(text)
    if pasted is not None:
        return _norm("\n".join(pasted.material))
    own = set(_question_lines(text))
    return _norm("\n".join(ln for ln in _lines(text) if ln.strip() not in own))


def carries_paste(query: str, material: str) -> bool:
    """Does `query` contain a PASTE_RUN_CHARS run of normalised `material`?"""
    q = _norm(query)
    if not material or len(q) < PASTE_RUN_CHARS:
        return False
    return any(q[i : i + PASTE_RUN_CHARS] in material for i in range(len(q) - PASTE_RUN_CHARS + 1))


# ---------------------------------------------------------------------------
# The turn's pasted material, for the last check before a search provider.
#
# A ContextVar with the same per-task scope as llm._usage and
# resilience.set_wait_notifier: main.py marks it once per chat turn (the
# message and the earlier user turns), and each engine that builds web queries
# marks the message it was handed. engines/search.py `_collect_results` - the
# one door every web query goes through - reads it. Copies of the context
# (gathered tasks, LangGraph nodes) carry the value, so no parameter is
# threaded through the engines.
# ---------------------------------------------------------------------------

_turn_material: ContextVar[Tuple[str, ...]] = ContextVar("_turn_material", default=())


def mark_turn(*messages: str) -> None:
    """Add these messages' pasted material to this turn's (a message with no
    paste adds nothing). Additive, so an engine marking the message it was
    handed never drops what main.py marked from the history."""
    known = _turn_material.get()
    fresh = tuple(
        m for m in dict.fromkeys(pasted_material(x) for x in messages) if m and m not in known
    )
    if fresh:
        _turn_material.set(known + fresh)


def without_paste(queries: Iterable[str], *messages: str) -> List[str]:
    """`queries` minus every one that carries a run of pasted material from
    this turn or from `messages`."""
    materials = [m for m in _turn_material.get() + tuple(pasted_material(x) for x in messages) if m]
    if not materials:
        return list(queries)
    return [q for q in queries if not any(carries_paste(q, m) for m in materials)]
