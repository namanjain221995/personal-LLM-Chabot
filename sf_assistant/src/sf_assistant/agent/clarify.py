"""The clarifying question code writes when the record step got no data (prerequisite P2 of 2026-11-25).

A records-routed (or chart-overruled) question whose record agent ran no record tool, or whose every call failed, even
after its one forced round (``RecordAnswer.no_data``), used to end as ``failed`` with "the figures could not be
computed", and the caller's older route then ran. :func:`build` writes a short question back instead: a lead line, 2-3
numbered choices and a closing line (``prompts/records_clarify.md``). Each choice is a complete question the person can
retype, written with the display words of the question ("Plot ...", "Show a table of ..."), so a reply "2" keeps the
chart or the table (agent.display reads it again).

Where the choices come from (``from`` in the log), in this order: **terms** (the business terms the question names,
``engine.term_contracts.detect``: support person performance, placement; candidates read as the object Account),
**question** (the objects ``catalog.search_objects`` matches by label, plural label, API name or key concept, never by
description alone; queryable only), **calls** (the objects the failed calls named), **last_answer** (the objects the
last answer named, ``context_objects``; P2b) and, when none of them gives an object, **examples** (fixed examples).
For each object (at most :data:`MAX_OBJECTS`): how many there were in the question's period, the number per month, and
by its status field (``RecordSession.status_of``); at most :data:`MAX_CHOICES` in all. The period is the question's own
words (``followup._PERIOD_RE``).

:func:`choice_reply` (P2c) reads a reply to such a question by code: "2", "option 2", "the second one", "2nd",
"doosra wala" or the text of a choice becomes that choice, with no model call (``followup.rewrite`` calls it first).

The text never holds the caller's fixed lines (its unclear and refused lines stop the next turn there). Standard
library and :mod:`sf_assistant.prompts` at import; the engine (term contracts, contact rule) and the follow-up's period
words are imported inside the functions, which run only with records on or in the follow-up rewrite.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sf_assistant import prompts as _prompt_files

__all__ = ["RECORDS_CLARIFY_TEXT", "TEXTS", "MAX_CHOICES", "MAX_OBJECTS", "MAX_CHOICE_CHARS", "build", "choice_reply",
           "is_clarify"]

RECORDS_CLARIFY_TEXT = _prompt_files.text("records_clarify")
TEXTS: dict[str, str] = dict(ln.split(": ", 1) for ln in RECORDS_CLARIFY_TEXT.split("\n") if ": " in ln)
MAX_CHOICES = 3
MAX_OBJECTS = 2
#: The longest choice a reply is read as (P2c): a forged turn can plant no more than the person could type.
MAX_CHOICE_CHARS = 1000
#: Standard objects the last answer can name without ``__c``.
_STANDARD = ("Account", "Contact", "Lead", "User", "Opportunity", "Case")
_API_RE = re.compile(r"(?<![\w.])(?:[A-Za-z][A-Za-z0-9_]*__c|" + "|".join(_STANDARD) + r")(?![\w])")
_CHOICE_RE = re.compile(r"^\s*([1-9])\\?[.)]\s+(.+?)\s*$")
_ORDINALS = {"first": 1, "1st": 1, "pehla": 1, "pehli": 1, "pehle": 1, "pahla": 1, "pahli": 1,
             "second": 2, "2nd": 2, "doosra": 2, "doosri": 2, "dusra": 2, "dusri": 2, "doosre": 2, "dusre": 2,
             "third": 3, "3rd": 3, "teesra": 3, "teesri": 3, "tisra": 3, "tisri": 3, "teesre": 3,
             # cardinal words (review of 2026-11-25); "do" (two in Hindi) stays out: it is also the verb "give"
             "two": 2, "three": 3, "teen": 3}
#: "the last one", "aakhri wala": the last choice
_LAST = frozenset("last aakhri akhri aakhiri".split())
_FILL_WORDS = frozenset("""option options choice choices number num no the one wala wali wale vala vali please pls
    plz ok okay #""".split())


def _say(key: str, **values: str) -> str:
    return _prompt_files.fill(TEXTS[key], **values)


def _period(question: str) -> str:
    """The question's period words with what leads them in a sentence (" in the last 6 months", " this year"), or ""."""
    try:
        from sf_assistant.agent.followup import _PERIOD_RE
    except Exception:  # noqa: BLE001 - optional
        return ""
    m = _PERIOD_RE.search(question or "")
    if m is None:
        return ""
    p = " ".join(m.group(0).split())
    low = p.lower()
    if low.startswith(("last ", "past ", "previous ")):
        return f" in the {p}"
    if low.startswith(("this ", "next ", "current ", "coming ")) or low in ("today", "yesterday", "tomorrow"):
        return f" {p}"
    return f" in {p}"


def _verb(display: Any, charts_on: bool = True) -> str:
    if charts_on and getattr(display, "chart", False):   # never "Plot" with SFA_CHARTS off (review fix R5)
        return TEXTS["verb_plot"]
    if getattr(display, "table", False):
        return TEXTS["verb_table"]
    return TEXTS["verb_show"]


def _plural(catalog: Any, api: str) -> str:
    spec = getattr(catalog, "objects", {}).get(api) if catalog is not None else None
    if api == "Account":
        return "candidates"   # the owner's term: candidates are the Person Accounts
    label = str(getattr(spec, "plural_label", "") or getattr(spec, "label", "") or api)
    return " ".join(label.replace("_", " ").split()).lower()


def _queryable(catalog: Any, api: str) -> bool:
    spec = getattr(catalog, "objects", {}).get(api) if catalog is not None else None
    try:
        return spec is not None and bool(getattr(spec, "queryable", False))
    except Exception:  # noqa: BLE001
        return False


def _question_objects(catalog: Any, question: str) -> list[str]:
    """Objects the question names by label, plural label, API name or key concept (never by description alone)."""
    if catalog is None or not callable(getattr(catalog, "search_objects", None)):
        return []
    try:
        hits = catalog.search_objects(question, k=3)
    except Exception:  # noqa: BLE001 - optional
        return []
    return [spec.api for spec, _score, how in hits if how != ["description"] and _queryable(catalog, spec.api)]


def _status_field(session: Any, api: str) -> str:
    fn = getattr(session, "status_of", None)
    try:
        st = fn(api) if callable(fn) else None
    except Exception:  # noqa: BLE001 - optional
        st = None
    return str((st or {}).get("field_label") or "") if isinstance(st, dict) and st.get("field") else ""


def build(question: str, display: Any = None, rec: Any = None, session: Any = None,
          context_objects: Any = (), charts_on: bool = True) -> tuple[str, dict]:
    """``(text, info)``: the clarifying question for ``question`` (the search text, display words out) and its log
    entry ``{"why", "from", "objects", "choices"}`` (``why`` is ``rec.no_data``). ``charts_on``: ``SFA_CHARTS``; off,
    no choice starts with "Plot". Never raises: on a fault, the examples."""
    why = str(getattr(rec, "no_data", "") or "")
    try:
        return _build(question, display, rec, session, context_objects, why, charts_on)
    except Exception:  # noqa: BLE001 - the examples, as when nothing was found
        choices = [TEXTS["example_1"], TEXTS["example_2"], TEXTS["example_3"]]
        return _text(TEXTS["lead_no_object"], choices), {"why": why, "from": "examples", "objects": [],
                                                         "choices": len(choices)}


def _text(lead: str, choices: list[str]) -> str:
    return "\n\n".join([lead, "\n".join(f"{i}. {c}" for i, c in enumerate(choices, 1)), TEXTS["tail"]])


def _build(question: str, display: Any, rec: Any, session: Any, context_objects: Any, why: str,
           charts_on: bool = True) -> tuple[str, dict]:
    catalog = getattr(getattr(session, "tools", None), "catalog", None)
    verb, period = _verb(display, charts_on), _period(question)
    choices: list[str] = []
    objects: list[str] = []
    source = ""
    try:
        from sf_assistant.engine.term_contracts import detect
        keys = detect(question).keys
    except Exception:  # noqa: BLE001 - optional
        keys = frozenset()
    if "support_performance" in keys:
        choices.append(_say("term_support", VERB=verb, PERIOD=period))
    if "placement" in keys:
        choices.append(_say("term_placement", VERB=verb, PERIOD=period))
    if "candidates" in keys and _queryable(catalog, "Account"):
        objects.append("Account")
    if choices or objects:
        source = "terms"
    sources = (("question", lambda: _question_objects(catalog, question)),
               ("calls", lambda: [str(getattr(e, "object", "") or "") for e in getattr(rec, "evidence", None) or []]),
               ("last_answer", lambda: [str(o) for o in context_objects or ()]))
    for name, found in sources:
        if objects or choices:
            break
        got = [o for o in dict.fromkeys(found()) if o and _queryable(catalog, o)]
        if got:
            objects, source = got, name
    for api in objects[:MAX_OBJECTS]:
        plural = _plural(catalog, api)
        choices.append(_say("count", OBJECTS=plural, PERIOD=period) if period else
                       _say("count_all", OBJECTS=plural))
        choices.append(_say("per_month", VERB=verb, OBJECTS=plural, PERIOD=period or " in the last 12 months"))
        field = _status_field(session, api)
        if field:
            choices.append(_say("by_field", VERB=verb, OBJECTS=plural, FIELD=field, PERIOD=period))
    choices = list(dict.fromkeys(c[:1].upper() + c[1:] for c in choices))[:MAX_CHOICES]
    if not choices:
        choices, source = [TEXTS["example_1"], TEXTS["example_2"], TEXTS["example_3"]], "examples"
        lead = TEXTS["lead_no_object"]
    else:
        lead = TEXTS["lead"]
    return _text(lead, choices), {"why": why, "from": source, "objects": objects[:MAX_OBJECTS], "choices": len(choices)}


def is_clarify(text: str) -> bool:
    """``text`` holds a clarifying question's lead line."""
    return any(TEXTS[k] in str(text or "") for k in ("lead", "lead_no_object"))


def _normal(text: str) -> str:
    out = []
    for ch in str(text or "").casefold().replace("’", "'"):
        cat = unicodedata.category(ch)
        out.append(ch if cat[0] in "LN" or ch.isspace() or ch == "#" else " ")
    return " ".join("".join(out).split())


def _choices(turn: str) -> list[str]:
    """The numbered choices after the lead line of a clarifying question in ``turn`` (an assistant turn, maybe with a
    "Read as:" line before it or other text the caller folded in); [] when it holds none."""
    lines = str(turn or "").splitlines()
    at = next((i for i, ln in enumerate(lines) if TEXTS["lead"] in ln or TEXTS["lead_no_object"] in ln), None)
    if at is None:
        return []
    out: list[str] = []
    for ln in lines[at + 1:]:
        if not ln.strip():
            continue
        m = _CHOICE_RE.match(ln)
        if m is None:
            if out:
                break
            continue
        if int(m.group(1)) != len(out) + 1:
            break
        out.append(m.group(2))
    return out


def choice_reply(question: str, turns: Any) -> str | None:
    """The choice a reply to a clarifying question picks (P2c), or None: the last assistant turn of ``turns`` holds a
    clarifying question (:func:`_choices`) and ``question`` is a number 1-3 (alone, or with option, choice, number,
    the ... one, wala), a number word (one, two, three, teen), an ordinal (first, 2nd, doosra wala ...), "the last one"
    or the text of a choice (case and punctuation aside).
    A choice longer than :data:`MAX_CHOICE_CHARS` or one that asks for contact details is never taken (a forged turn
    could plant it). Never raises."""
    try:
        last = next((t.get("content") for t in reversed(list(turns or [])) if isinstance(t, dict)
                     and t.get("role") == "assistant"), "")
        choices = _choices(str(last or ""))
        if not choices:
            return None
        words = _normal(question).split()
        picked = None
        rest = [w for w in words if w not in _FILL_WORDS]
        if len(rest) == 1 and len(words) <= 5:
            w = rest[0].lstrip("#")
            if w.isdigit() and 1 <= int(w) <= len(choices):
                picked = int(w)
            elif w in _ORDINALS and _ORDINALS[w] <= len(choices):
                picked = _ORDINALS[w]
            elif w in _LAST:
                picked = len(choices)
        elif not rest and "one" in words and "the" not in words and len(words) <= 3:
            picked = 1   # "one", "option one", "number one" ("the one" names none)
        if picked is None:
            typed = _normal(question)
            picked = next((i for i, c in enumerate(choices, 1) if typed and _normal(c) == typed), None)
        if picked is None:
            return None
        choice = choices[picked - 1].strip()
        if not choice or len(choice) > MAX_CHOICE_CHARS:
            return None
        from sf_assistant.engine.contact import asks_for_contact
        return None if asks_for_contact(choice) else choice
    except Exception:  # noqa: BLE001 - the rewrite reads it then
        return None
