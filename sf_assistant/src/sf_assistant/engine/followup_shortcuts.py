"""The code shortcuts of the follow-up resolver (design v2 of 2026-11-25, §6.2 step 2; lane S2): short follow-ups that
change one part of the last query and say nothing else, read by code with no model call.

The closed grammar, English and Hinglish:

- **display** ("visualize it", "plot this", "chart it", "as a table", "isko chart me dikhao", "graph banao", and the
  refusals "without the chart", "chart nahi"): ``set_show``. The display words have one home, ``agent/display.py``
  (prerequisite P3), which sits above this layer, so the caller reads them there and passes what they say
  (:func:`match` ``display``) and the question without them (``text``). A table ask that names no chart turns the
  chart off: "as a table" means a table instead of the chart;
- **ranking** ("top 10", "only top 5", "bottom 3", "top 5 dikhao"): ``set_top`` (top is ``desc``, bottom ``asc``;
  "first N" keeps the order). "the first one" and "the top one" are no ranking: they point at one row of the ranking
  shown, which the model reads as an ordinal drill (D3);
- **period** ("same for last month", "last month?", "2025 ka", "pichle mahine ka", "is saal"): ``set_period``. The
  words must make one period that :func:`~sf_assistant.engine.periods.parse_period` reads; Hinglish periods are given
  in its English words ("pichle mahine" is "last month", "pichle 3 mahine" "last 3 months", "is saal" "this year",
  "aaj" "today"; "kal" is yesterday and tomorrow both, so it is none). "All time" words ("overall", "so far", "ab
  tak") give the phrase "all time", which removes the period. The month "may" alone counts only after a word that
  leads a period ("same for may"), since "May I ..." is no period;
- **by period** ("by month", "monthwise", "per week", "har mahine", "mahine ke hisaab se", "weekly"): ``set_grain``.

A shortcut fires only when nothing but filler (:data:`FILLER`) is left after the matched words, so it can never
swallow a question that has a subject of its own ("Only for North America", "top 10 recruiters", "compare with last
month" are no shortcut). At most one operation of each kind, at most 3 in all (the patch cap), in a fixed order
(period, ranking, by period, display): "top 5 by month" ranks first and then splits the top 5 by month.

The operations are in the canonical form of ``agent/query_state.py`` (``{name: {params}}``) and go through the same
checks as a model patch there. Pure: standard library and :mod:`sf_assistant.engine.periods` only, no model, no I/O,
no clock (the caller passes ``today``).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Mapping

from sf_assistant.engine.periods import PeriodError, parse_period

__all__ = ["Shortcut", "match", "period_phrase", "words", "MAX_CHARS", "MAX_WORDS", "MAX_OPS", "MAX_TOP", "FILLER",
           "GRAINS", "HINGLISH_PERIODS", "ALL_TIME_WORDS", "ALL_TIME"]

#: A shortcut is short: a longer text is never one.
MAX_CHARS = 120
MAX_WORDS = 12
#: The patch cap (agent/query_state.py MAX_OPS).
MAX_OPS = 3
#: The largest N of a ranking (the schema's group_records.top).
MAX_TOP = 100
#: The phrase that removes a period (parse_period reads it as "no time limit").
ALL_TIME = "all time"

#: Words that add nothing to a shortcut: pointing words, courtesy, question frame, show verbs and their Hinglish
#: forms. Never a word that names data or changes it ("north", "recruiters", "compare", "not", "without", "by",
#: "trend": a trend asks for a split by time, which the model reads; stage-1 review fix M4).
FILLER = frozenset("""a an the it its this that these those them they same also and aur but so ok okay now then
please pls plz kindly just only sirf bas me mujhe us hume humein show give get see view display want need would like
can could you u i we do what whats about how for of in on at to as with is are was ka ki ke ko mein me mai main liye
bhi wala wale wali vala vale vali waala waale waali dikhao dikha dikhado dikhaiye batao bata batado bataiye karo kar
kardo kijiye chahiye hai ho data numbers results result records again too instead one ones isko iska iski usko
uska uski ise ye yeh wo woh""".split())

#: grain -> the words that name it as a unit (English and Hinglish)
_UNITS = {"day": ("day", "days", "din", "date"), "week": ("week", "weeks", "hafta", "hafte"),
          "month": ("month", "months", "mahina", "mahine"), "quarter": ("quarter", "quarters"),
          "year": ("year", "years", "saal")}
#: grain -> the words that name it alone ("monthly", "monthwise")
_GRAIN_ALONE = {"day": ("daily", "daywise", "datewise", "roz", "rozana"), "week": ("weekly", "weekwise"),
                "month": ("monthly", "monthwise", "mahinewise"), "quarter": ("quarterly", "quarterwise"),
                "year": ("yearly", "annually", "annual", "yearwise", "saalwise")}
GRAINS = tuple(_UNITS)
_BY_WORDS = ("by", "per", "each", "every", "har")


def _grain_phrases() -> list[tuple[tuple[str, ...], str]]:
    """The word runs that name a grain, longest first: "by month", "month wise", "mahine ke hisaab se", "monthly"."""
    out: list[tuple[tuple[str, ...], str]] = []
    for grain, units in _UNITS.items():
        for unit in units:
            out += [((by, unit), grain) for by in _BY_WORDS]
            out += [((unit, "wise"), grain), ((unit, "by", unit), grain)]
            out += [((unit, "ke", way, se), grain) for way in ("hisaab", "hisab", "hisaap") for se in ("se", "say")]
        out += [((word,), grain) for word in _GRAIN_ALONE[grain]]
    return sorted(out, key=lambda p: -len(p[0]))


_GRAIN_PHRASES = _grain_phrases()

#: Hinglish period words in the English words parse_period reads: (when, unit) -> English. "kal" is yesterday and
#: tomorrow both, so it is none.
_HI_WHEN = {"pichle": "last", "pichhle": "last", "pichla": "last", "pichhla": "last", "pichli": "last",
            "pichhli": "last", "is": "this", "iss": "this", "agle": "next", "agla": "next", "agli": "next"}
_HI_UNIT = {"din": "day", "dino": "day", "dinon": "day", "hafta": "week", "hafte": "week", "hafton": "week",
            "mahina": "month", "mahine": "month", "mahino": "month", "mahinon": "month", "saal": "year",
            "saalon": "year", "sal": "year", "quarter": "quarter", "quarters": "quarter"}
#: Hinglish period words of one word.
HINGLISH_PERIODS = {"aaj": "today"}
#: Words that mean all time (parse_period's "no time limit"): the phrase becomes :data:`ALL_TIME`.
ALL_TIME_WORDS = (("all", "time"), ("overall",), ("so", "far"), ("in", "total"), ("till", "now"), ("till", "date"),
                  ("to", "date"), ("ab", "tak"), ("abhi", "tak"))
#: Leading words parse_period reads as part of a period ("for last month"); the phrase is written without them.
_PERIOD_LEADS = ("in", "for", "during", "the", "of", "on")
_NUMBER_WORDS = {w: i for i, w in enumerate("""zero one two three four five six seven eight nine ten eleven twelve
thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty""".split())}
#: "one" after top, first, bottom ...: "the first one" points at one row of the ranking shown (an ordinal drill, D3),
#: it asks for no ranking of one (stage-1 review fix M3); the digit "top 1" still is one
_NOT_A_COUNT = frozenset({"one", "ones"})
#: a word, or a date written with hyphens ("2026-05", "2026-05-03")
_WORD_RE = re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?(?![\w-])|[^\W_]+")
_MAX_PERIOD_WORDS = 7


@dataclass(frozen=True)
class Shortcut:
    """A follow-up code read as a shortcut: the operations (canonical ``{name: {params}}``, in the fixed order) and
    which parts of the grammar matched (``period``, ``top``, ``grain``, ``display``; for the log)."""

    ops: tuple[dict, ...]
    parts: tuple[str, ...]


def _number(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def _words(text: str) -> list[str]:
    """The words of ``text``, cased down: ``’`` as ``'``, an apostrophe joined ("what's" -> "whats"), a hyphen after a
    letter splits ("top-5", "month-wise"), a date keeps its hyphens ("2026-05-03")."""
    raw = re.sub(r"(?<=[^\W\d_])-", " ", str(text or "").replace("’", "'").replace("'", ""))
    return [w.casefold() for w in _WORD_RE.findall(raw)]


def _parses(phrase: str, today: dt.date) -> bool:
    try:
        parse_period(phrase, today)
    except PeriodError:
        return False
    return True


def _hinglish(words: list[str], at: int) -> tuple[int, str] | None:
    """(length, English phrase) of a Hinglish period at ``words[at]``: "pichle mahine", "pichle 3 mahine", "is saal",
    "aaj"."""
    w = words[at]
    if w in HINGLISH_PERIODS:
        return 1, HINGLISH_PERIODS[w]
    when = _HI_WHEN.get(w)
    if when is None or at + 1 >= len(words):
        return None
    nxt = words[at + 1]
    if nxt in _HI_UNIT:
        return 2, f"{when} {_HI_UNIT[nxt]}"
    n = _number(nxt)
    if n is not None and when != "this" and at + 2 < len(words) and words[at + 2] in _HI_UNIT and n >= 1:
        unit = _HI_UNIT[words[at + 2]]
        return 3, f"{when} {n} {unit}{'s' if n > 1 else ''}"
    return None


def _period(words: list[str], used: list[bool], today: dt.date) -> str | None:
    """The one period of the free words (marks them used), or None. Hinglish first, then the all-time words, then
    the longest run of free words parse_period reads."""
    n = len(words)
    for at in range(n):
        if used[at]:
            continue
        got = _hinglish(words, at)
        if got and not any(used[at:at + got[0]]) and _parses(got[1], today):
            for i in range(at, at + got[0]):
                used[i] = True
            return got[1]
    for phrase in sorted(ALL_TIME_WORDS, key=len, reverse=True):
        k = len(phrase)
        for at in range(n - k + 1):
            if tuple(words[at:at + k]) == phrase and not any(used[at:at + k]):
                for i in range(at, at + k):
                    used[i] = True
                return ALL_TIME
    for size in range(min(_MAX_PERIOD_WORDS, n), 0, -1):
        for at in range(n - size + 1):
            if any(used[at:at + size]):
                continue
            span = words[at:at + size]
            while len(span) > 1 and span[0] in _PERIOD_LEADS and _parses(" ".join(span[1:]), today):
                span = span[1:]
            phrase = " ".join(span)
            if not _parses(phrase, today) or all(w in FILLER for w in span):
                continue
            # "may" is also a verb ("May I see it by week?"): the month alone only after a word that leads a period
            if span == ["may"] and not (at and words[at - 1] in _PERIOD_LEADS):
                continue
            for i in range(at, at + size):
                used[i] = True
            return phrase
    return None


def _top(words: list[str], used: list[bool]) -> dict | None:
    """``set_top`` params of "top N", "bottom N", "first N" (marks the words used), or None."""
    for at in range(len(words) - 1):
        lead, n = words[at], _number(words[at + 1])
        if used[at] or used[at + 1] or n is None or not 1 <= n <= MAX_TOP or words[at + 1] in _NOT_A_COUNT:
            continue
        if lead in ("top", "highest", "bottom", "lowest", "least", "first"):
            used[at] = used[at + 1] = True
            if lead in ("top", "highest"):
                return {"n": n, "order": "desc"}
            if lead == "first":
                return {"n": n}
            return {"n": n, "order": "asc"}
    return None


def _grain(words: list[str], used: list[bool]) -> str | None:
    """The grain of "by month", "monthwise", "har mahine", "weekly" (marks the words used), or None."""
    for phrase, grain in _GRAIN_PHRASES:
        k = len(phrase)
        for at in range(len(words) - k + 1):
            if tuple(words[at:at + k]) == phrase and not any(used[at:at + k]):
                for i in range(at, at + k):
                    used[i] = True
                return grain
    return None


def _show(display: Mapping[str, Any] | None) -> dict | None:
    """``set_show`` params of what the caller's display reading says, or None when it says nothing."""
    if not display:
        return None
    chart, table, kind = display.get("chart"), bool(display.get("table")), display.get("type")
    params: dict[str, Any] = {}
    if chart is True:
        params["chart"] = True
        if isinstance(kind, str) and kind:
            params["type"] = kind
    elif chart is False:
        params["chart"] = False
    if table:
        params["table"] = True
        params.setdefault("chart", False)   # "as a table": a table instead of the chart
    return params or None


def words(text: Any) -> list[str]:
    """The words of ``text`` as the grammar reads them (cased down; a date keeps its hyphens)."""
    return _words(str(text or ""))


def period_phrase(text: Any, *, today: dt.date) -> str | None:
    """The one period the words of ``text`` make, in the English words parse_period reads (Hinglish mapped: "pichle
    mahine" is "last month"; "overall" is :data:`ALL_TIME`), or None. The resolver reads it to see whether a period
    the model chose is one the person typed. Never raises."""
    try:
        ws = _words(str(text or ""))
        return _period(ws, [False] * len(ws), today)
    except Exception:  # noqa: BLE001 - no period read
        return None


def match(text: Any, *, today: dt.date, display: Mapping[str, Any] | None = None) -> Shortcut | None:
    """The shortcut ``text`` is, or None (see the module text).

    ``text``: the typed follow-up without its display words (``agent/display.py`` takes them out; the question as
    typed when it had none). ``display``: what those words said, or None: ``chart`` (True: show a chart, False: turn
    it off, None: not said), ``type`` (a chart type of the plan's grammar, or None), ``table`` (a table was asked) and
    ``other`` (another display word was read, a file, a summary or a breakdown: no shortcut covers it). ``today``:
    for the period words. Never raises."""
    try:
        return _match(str(text or ""), today, display)
    except Exception:  # noqa: BLE001 - a question never fails on this; the model resolver reads it instead
        return None


def _match(text: str, today: dt.date, display: Mapping[str, Any] | None) -> Shortcut | None:
    if display and display.get("other"):
        return None
    if len(text) > MAX_CHARS:
        return None
    words = _words(text)
    if len(words) > MAX_WORDS:
        return None
    used = [False] * len(words)
    ops: list[dict] = []
    parts: list[str] = []
    phrase = _period(words, used, today)
    if phrase is not None:
        ops.append({"set_period": {"phrase": phrase}})
        parts.append("period")
    top = _top(words, used)
    if top is not None:
        ops.append({"set_top": top})
        parts.append("top")
    grain = _grain(words, used)
    if grain is not None:
        ops.append({"set_grain": {"grain": grain}})
        parts.append("grain")
    show = _show(display)
    if show is not None:
        ops.append({"set_show": show})
        parts.append("display")
    if not ops or len(ops) > MAX_OPS:
        return None
    if any(not used[i] and w not in FILLER for i, w in enumerate(words)):
        return None
    return Shortcut(tuple(ops), tuple(parts))
