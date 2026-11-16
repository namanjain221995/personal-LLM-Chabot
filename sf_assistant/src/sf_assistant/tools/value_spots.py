"""Value spots: the stored record values a question names, and the record agent's hint section for them.

Owner, 2026-10-28: fetching records from any stored value must work in every object and every field, so that a question
naming a stored record never misses the records step; rule R4 (2026-10-26): a question can name many entities, and
every one of them is recognised. The catalog's entity index (``Catalog.entities``, built at open by
``Catalog._entity_index``) holds every stored value a person could name: the Names of the objects that are no people,
codes (auto-numbers, identifier-shaped text, one-per-record numbers), short text, picklist values and record type
names, each with the records that hold it. Long text stays with ``search_records``; contact fields are never indexed (an
e-mail address or phone number typed in the question is matched as a whole value at spot time).

:meth:`ValueSpotsMixin.spot_values` finds the question's word sequences in that index (several per question) and says
how sure code is that each one names a record (``strength``): ``strong`` for a record identity the question marks (a
code, a cue word of the object next to it, a quote, capitals inside a sentence, a word that no English, design or spoken
word list knows, a Name of two or more distinctive words, an e-mail address or a phone number the question marks as one,
stored whole), ``hint`` for a value that may be a plain word or the org's design vocabulary (a picklist value, a record
type name, a value many records share, a word of a design phrase, an acronym typed in capitals, plain words after a cue
word that do not end their noun phrase, a bare number that a contact field stores). A strong spot sends the question to the record agent even when the route
call said NO_RECORDS (``agent/record_session.py``), and the record agent's hints say for each named record where it is
stored, its record type, whether a description is stored and which records link to it
(:meth:`ValueSpotsMixin.value_hint_lines`, a section of :meth:`NamesMixin.object_hints
<sf_assistant.tools.names.NamesMixin.object_hints>`).

Precision guards: people keep their own path (a stored full name and a confirmed one-word person own their words);
numbers and dates stay filters (a digits-only code or a date-shaped Name needs its object's cue word right before it,
or a quote); a word of a longer design phrase, a quoted design value and an ordinary word are hints, which never
overrule a route. With the spelling help on (``catalog.resolver``) a misspelt value is read only through a ``corrected``
span of the question; off (``SFA_RESOLVER=off``) spotting reads the typed words alone. No record Id, no blocked field
and no value with an embedded secret enters a spot, a hint or a log: the index never holds them, and the Ids read at
hint time never leave code.
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from typing import Any

from sf_assistant import prompts as _prompt_files   # the hint section's text (prompts/records_value_hints.md)
from sf_assistant.engine.catalog import (BLANK_VALUES, FIELD_SENSITIVITY, TEXT_INDEX_MAX_DISTINCT, RecordError,
                                         entity_key)
from sf_assistant.engine.contact import asks_for_contact
from sf_assistant.engine.business_terms import is_not_the_term, not_the_term   # the owner's terms (2026-11-16)
from sf_assistant.engine.query_builder import Frag
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.tools.knowledge_tools import corrected_text
from sf_assistant.tools.names import _common_words
from sf_assistant.tools.record_base import _HINT_STOPWORDS, _cut_text, _disp


__all__ = ["ValueSpotsMixin", "RECORDS_VALUE_HINTS"]

#: The hint section of the records a question names (``<<LINES>>``: one line per named span).
RECORDS_VALUE_HINTS = _prompt_files.text("records_value_hints")

#: Limits of the spotting and of the hint section (read at call time: patch them here).
VALUE_NGRAM_MAX = 12            # longest word sequence matched (the index's longest Name; entity_max_words bounds it)
VALUE_SPOTS_MAX = 40            # spots per question (the listed ones kept first when there are more)
VALUE_FEW_RECORDS = 3           # a text value held by more records of its field is a value list (a hint)
VALUE_CONTACT_BUDGET_S = 0.3    # the statements of an e-mail address or phone number typed in the question
VALUE_HINT_MAX_SPANS = 8        # named spans in the hint section (strong ones first)
VALUE_HINT_MAX_PLACES = 3       # places (object and field) per span
VALUE_HINT_LINKED_SPANS = 4     # spans whose named record gets its linked records counted
VALUE_HINT_LINK_RECORDS = 5     # ... when at most this many records hold the name
VALUE_HINT_LOOKUPS = 4          # linked lookups listed per span ("and n more lookups" after them)
VALUE_HINT_MAX_CHARS = 1_600    # the whole section
VALUE_HINT_BUDGET_S = 1.0       # the Id and link statements of the whole section

#: Words between a cue word and a digits-only code: "case no 98765", "invoice number 1001" ("#" is no word).
_NUMBER_WORDS = frozenset({"number", "no", "num"})
#: "<cue> named <span>", "<cue> called <span>".
_NAMED_WORDS = frozenset({"named", "called"})
#: Month and weekday names and abbreviations (the calendar module): with digits they make a date-shaped value.
_CALENDAR_WORDS = frozenset(w.lower() for seq in (calendar.month_name, calendar.month_abbr, calendar.day_name,
                                                  calendar.day_abbr) for w in seq if w)
#: Rules that make a spot on the words of a confirmed one-word person strong (its strength never comes from them).
_MARKED = frozenset({"cue word", "quoted", "code"})

#: A quote that opens at a word start and closes at a word end (a possessive or an apostrophe inside a word is none).
_QUOTE_RE = re.compile("|".join(
    rf"(?<!\w){re.escape(a)}(?=\S)(?P<q{k}>.+?)(?<=\S){re.escape(b)}(?!\w)"
    for k, (a, b) in enumerate((("'", "'"), ('"', '"'), ("`", "`"), ("‘", "’"), ("“", "”")))))
_CHUNK_RE = re.compile(r"\S+")
#: One key word of a typed word: letters and digits, with the characters ``entity_key`` deletes between them
#: (``.,'"()``: "37.08", "salesforce.com", "don't" are one word each); any other character ("-", "/", "&", ":")
#: separates two.
_PIECE_RE = re.compile(r"[^\W_]+(?:[.,'\"()]+[^\W_]+)*")
_POSSESSIVE_RE = re.compile(r"['’]s\b", re.IGNORECASE)   # dropped first, as entity_key does ("team's" -> "team")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w+])\+?\(?\d[\d ().-]{5,}\d(?!\w)")
_DATE_IN_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}")
#: A stored value shaped like a phone number as a whole (the contact match compares whole values only).
_PHONE_VALUE_SQL = r"^\+?[0-9 ().\-]{7,}$"
#: Digits typed with a phone number's formatting: a bracket, or a space or dash between digits ("+" is read apart).
_PHONE_FORMAT_RE = re.compile(r"[()]|\d[ -]+\d")
#: Placeholder digit strings (one repeated digit, a run of 0-9 up or down): never matched as a phone number.
_DIGIT_RUNS = ("0123456789" * 2, "9876543210" * 2)
_SPOKEN_WORDS: frozenset[str] | None = None   # knowledge/spoken_words.txt, read once


def _spoken_words() -> frozenset[str]:
    """The romanized Hindi and Gujarati question words of ``knowledge/spoken_words.txt`` (known words, never a
    value's unknown word), read once; empty when the file cannot be read."""
    global _SPOKEN_WORDS
    if _SPOKEN_WORDS is None:
        from sf_assistant.knowledge.terms import SPOKEN_WORDS_PATH

        try:
            lines = SPOKEN_WORDS_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        _SPOKEN_WORDS = frozenset(w.strip().lower() for w in lines
                                  if w.strip() and not w.startswith("#") and " " not in w.strip())
    return _SPOKEN_WORDS


@dataclass(frozen=True)
class _Tok:
    """One key word of the question: an ``entity_key`` word of a typed word, with its offsets and spelling as typed."""

    key: str        # one word of entity_key; "" for an API name
    start: int      # offsets in the question as typed (a possessive "'s", quotes and brackets around it left out)
    end: int
    chunk: int      # index of the whitespace-delimited word that holds it ("Z-1000" is one word, "z 1000" two)
    text: str       # the letters and digits as typed (their capitals)
    api: bool       # an API name (a typed word holding "_"): no value, and no word sequence crosses it


def _tokens(question: str) -> list[_Tok]:
    """The key words of ``question``, word by word as :func:`entity_key` writes a stored value (so both meet in the
    index): each whitespace-delimited word, its possessive "'s" dropped, gives the ``entity_key`` words of its pieces
    (:data:`_PIECE_RE`: "Z-1000" gives "z", "1000"; "Q&A" gives "q", "a"; "$37.08" gives "3708"); a word holding "_"
    is one API token."""
    out: list[_Tok] = []
    for c, chunk in enumerate(_CHUNK_RE.finditer(question)):
        text = chunk.group(0)
        if "_" in text:
            out.append(_Tok("", chunk.start(), chunk.end(), c, text, True))
            continue
        clean = _POSSESSIVE_RE.sub(lambda m: " " * len(m.group(0)), text)   # the offsets stay
        for m in _PIECE_RE.finditer(clean):
            a, b = chunk.start() + m.start(), chunk.start() + m.end()
            for w in entity_key(m.group(0)).split():
                out.append(_Tok(w, a, b, c, question[a:b], False))
    return out


def _date_shaped(key: str) -> bool:
    """A key that holds a digit and whose every word holds a digit, is a month or weekday name or abbreviation or has
    at most 3 letters ("jan 2025", "q1 2020"): a date or amount shape (the index keeps it only for Names)."""
    ws = key.split()
    return bool(ws) and any(ch.isdigit() for ch in key) and all(
        any(ch.isdigit() for ch in w) or w in _CALENDAR_WORDS or len(w) <= 3 for w in ws)


def _records(n: int) -> str:
    return f"{n} record" if n == 1 else f"{n} records"


def _sentence_start(question: str, start: int) -> bool:
    """The text before ``start`` is empty or ends a sentence (".", "!", "?" or a line break, quotes and brackets
    aside), as :func:`sf_assistant.tools.names._written_as_name` reads a sentence start."""
    before = question[:start]
    bare = before.rstrip()
    gap = before[len(bare):]
    bare = bare.rstrip("\"'“‘`([{").rstrip()
    return not bare or bare[-1] in ".!?" or "\n" in gap


def _cue_in(word: str, cues: Any) -> bool:
    """``word`` (or the same word without a plural "s") is one of ``cues``."""
    return bool(word) and (word in cues or (len(word) > 3 and word.endswith("s") and word[:-1] in cues))


class _Question:
    """What :meth:`ValueSpotsMixin.spot_values` reads of one question once: its key words, which of them are used or
    owned by people, the quotes, the design-phrase ranges and whether it mixes capitals and small letters."""

    def __init__(self, question: str, toks: list[_Tok], phrases: Any) -> None:
        self.q = question
        self.toks = toks
        self.used = [t.api for t in toks]
        self.own = [False] * len(toks)
        self.mixed = any(ch.islower() for ch in question) and any(ch.isupper() for ch in question)
        self.quotes: list[tuple[int, int]] = []   # (start, end) of each quote's inner text
        for m in _QUOTE_RE.finditer(question):
            g = next(k for k, v in m.groupdict().items() if v is not None)
            self.quotes.append((m.start(g), m.end(g)))
        self.ranges: list[tuple[int, int]] = []   # word ranges (2 to 6 words) of the question that are design phrases
        for n in range(min(6, len(toks)), 1, -1):
            for a in range(len(toks) - n + 1):
                win = toks[a:a + n]
                if not any(t.api for t in win) and " ".join(t.key for t in win) in phrases:
                    self.ranges.append((a, a + n))

    def key(self, i: int, n: int) -> str:
        return " ".join(t.key for t in self.toks[i:i + n])

    def word(self, j: int) -> str:
        """The key word at ``j`` ("" outside the question or for an API name)."""
        return self.toks[j].key if 0 <= j < len(self.toks) and not self.toks[j].api else ""

    def offsets(self, i: int, n: int) -> tuple[int, int]:
        return self.toks[i].start, self.toks[i + n - 1].end

    def whole_quote(self, i: int, n: int) -> bool:
        """A quote's inner text is exactly these words."""
        a, b = self.offsets(i, n)
        k = self.key(i, n)
        return any(qa <= a and b <= qb and entity_key(self.q[qa:qb]) == k for qa, qb in self.quotes)

    def in_quote(self, i: int, n: int) -> bool:
        a, b = self.offsets(i, n)
        return any(qa <= a and b <= qb for qa, qb in self.quotes)

    def in_phrase(self, i: int, n: int) -> bool:
        """The words lie inside a longer design phrase of the question ("zoom recording departments")."""
        return any(a <= i and i + n <= b and b - a > n for a, b in self.ranges)

    def capitals(self, i: int, n: int) -> bool:
        """The question mixes capitals and small letters, every word here starts with a capital and the words do not
        start a sentence."""
        return self.mixed and all(t.text[:1].isupper() for t in self.toks[i:i + n]) and \
            not _sentence_start(self.q, self.toks[i].start)

    def ends_phrase(self, i: int, n: int) -> bool:
        """No noun follows the words: the question ends after them, punctuation follows them, or the next word is a
        function word of the hints (:data:`_HINT_STOPWORDS`)."""
        j = i + n
        if j >= len(self.toks):
            return True
        gap = self.q[self.toks[j - 1].end:self.toks[j].start]
        return any(ch in ".,;:!?()[]{}\"'`‘’“”" for ch in gap) or self.toks[j].api or \
            self.toks[j].key in _HINT_STOPWORDS

    def acronym(self, i: int, n: int) -> bool:
        """One typed word of at most 4 letters, written all in capitals in a question that mixes capitals and small
        letters ("CTC", "GCP"): an acronym, which is always typed so."""
        t = self.toks[i]
        return self.mixed and n == 1 and t.text.isupper() and sum(ch.isalpha() for ch in t.text) <= 4

    def one_word(self, i: int, n: int) -> bool:
        """The words come from one typed word ("Z-1000"), or from two typed words inside one quote."""
        chunks = {t.chunk for t in self.toks[i:i + n]}
        return len(chunks) == 1 or (len(chunks) == 2 and self.in_quote(i, n))


class ValueSpotsMixin:
    """Value spots and the named records' hint section of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    _value_cache: tuple[tuple, list[dict]] | None = None   # (question, person words) and its spots (spot_values)

    # ================================================================== spotting (one question)
    def spot_values(self, question: str, people: list[dict] | None = None) -> list[dict]:
        """The stored values the question names, as JSON-safe spot dicts, sorted by question position, then strong
        first, then fewest records; at most :data:`VALUE_SPOTS_MAX` (the listed ones kept first). Never raises ([] on
        any failure). Each spot is one (object, field) a word sequence of the question matches::

            {"span", "start", "end",   # the question's words as typed and their offsets
             "key",                    # entity_key of the stored value ("<e-mail>" / "<phone>" for kind contact)
             "read_as",                # only when read through the spelling help: its spelling
             "object", "field", "kind",          # kind: name | code | text | picklist | record_type | contact
             "value", "values",        # the stored spelling with most records, every stored spelling (no contact)
             "records", "truncated",   # records holding it in this field; the field's index was cut
             "strength", "why", "cue", # strong | hint, the rule that decided, the cue word of rule "cue word"
             "listed"}                 # it reaches the hints, the triage and the log

        ``people``: :meth:`spot_people` entries of the question the session confirmed or hints (None: every spot of
        :meth:`spot_people` here). The words of every stored full name (2 or more words) of the question and of the
        one-word people of ``people`` belong to the people path: a value on them is spotted only when a cue word or a
        whole quote makes it strong (it is then listed next to the person). Cached for the last question and person
        words. No record Id is ever in a spot."""
        try:
            q = str(question or "")
            words = None if people is None else tuple(str(d.get("words") or "") for d in people if isinstance(d, dict))
            ck = (q, words)
            cached = self._value_cache
            if cached is None or cached[0] != ck:
                cached = (ck, self._spot_values(q, people))
                self._value_cache = cached
            return [dict(s, values=list(s.get("values") or [])) for s in cached[1]]
        except Exception:  # noqa: BLE001 - optional: no spots keep today's triage and hints
            return []

    def _spot_values(self, q: str, people: list[dict] | None) -> list[dict]:
        cat = self.catalog
        if not q.strip():
            return []
        toks = _tokens(q)
        ctx = _Question(q, toks, getattr(cat, "design_phrases", None) or frozenset())
        spots = self._contact_spots(q, ctx)
        if getattr(cat, "entities", None):
            self._own_person_words(q, ctx, people)
            typed = self._index_spots(ctx)
            if getattr(cat, "resolver", None) is not None:   # the spelling help (SFA_RESOLVER=on)
                read = self._spelling_value_spots(ctx, typed)
                if read:   # a reading replaces the typed spots inside its words (a picklist word inside a misspelt Name)
                    typed = [t for t in typed if not any(r["start"] < t["end"] and t["start"] < r["end"] for r in read)]
                    typed += read
            spots += typed
        order = {"strong": 0, "hint": 1}

        def sort(s: dict) -> tuple:
            return s["start"], order.get(s["strength"], 2), s["records"], s["object"], s["field"]

        spots.sort(key=sort)
        if len(spots) > VALUE_SPOTS_MAX:
            listed = [s for s in spots if s["listed"]][:VALUE_SPOTS_MAX]
            rest = [s for s in spots if not s["listed"]][:VALUE_SPOTS_MAX - len(listed)]
            spots = sorted(listed + rest, key=sort)
        return spots

    def _own_person_words(self, q: str, ctx: _Question, people: list[dict] | None) -> None:
        """Mark the words the people path owns: every stored full name of the question (:meth:`spot_people` spots of 2
        or more words) and the one-word people of ``people`` (None: every one-word spot of :meth:`spot_people`)."""
        try:
            found = [d for d in self.spot_people(q) if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            found = []
        given = found if people is None else [d for d in people if isinstance(d, dict)]
        owners = [d for d in found if len(str(d.get("words") or "").split()) >= 2] + \
            [d for d in given if len(str(d.get("words") or "").split()) == 1]
        keys = [t.key for t in ctx.toks]
        for d in owners:
            ws = entity_key(str(d.get("words") or "")).split()
            for i in range(len(keys) - len(ws) + 1) if ws else ():
                if keys[i:i + len(ws)] == ws:
                    for j in range(i, i + len(ws)):
                        ctx.own[j] = True

    def _index_spots(self, ctx: _Question, fixed: list[Any] | None = None) -> list[dict]:
        """Word sequences of the question (longest first, left to right) that are keys of the entity index, each word
        used once. ``fixed`` (the spelling help's pass, :meth:`_spelling_value_spots`): per word, the ``corrected``
        span it was read from, or None; only sequences holding such a word are tried, read as the corrected text."""
        cat = self.catalog
        toks = ctx.toks
        nmax = min(int(getattr(cat, "entity_max_words", 0) or 0) or VALUE_NGRAM_MAX, VALUE_NGRAM_MAX)
        out: list[dict] = []
        for n in range(min(nmax, len(toks)), 0, -1):
            for i in range(len(toks) - n + 1):
                if any(ctx.used[i:i + n]) or (fixed is not None and not any(fixed[i:i + n])):
                    continue
                keys = [t.key for t in toks[i:i + n]]
                key = " ".join(keys)
                if key in BLANK_VALUES or (all(k in _HINT_STOPWORDS for k in keys) and not (
                        n == 1 and len(key) == 2 and ctx.mixed and toks[i].text.isupper())):
                    continue   # function words; a 2-letter one written in capitals next to a cue word may be a value
                if n > 1 and all(ctx.own[i:i + n]):
                    continue   # a stored full name: the people path owns it
                read_as = None
                if fixed is not None:
                    a, b = toks[i].start, toks[i + n - 1].end
                    if entity_key(ctx.q[a:b]) == key:
                        continue   # these words are read as typed: the typed pass had them
                    read_as = ctx.q[a:b]
                    spans = {id(x): x for x in fixed[i:i + n] if x is not None}.values()
                    for sp in sorted(spans, key=lambda x: -x.start):
                        if a <= sp.start and sp.end <= b:
                            read_as = read_as[:sp.start - a] + str(sp.used) + read_as[sp.end - a:]
                    read_as = _disp(read_as)
                got = self._match(ctx, i, n, self._candidates(key, n), read_as=read_as)
                if got:
                    out += got
                    for j in range(i, i + n):
                        ctx.used[j] = True
        return out

    def _candidates(self, key: str, n: int) -> list[tuple[str, Any]]:
        """``(stored key, EntityEntry)`` for a question key: its own entries; a digits-only word, the digits-only codes
        of its digits without leading zeros; no entry, the compact form (every space removed) of a key of 1 to 3
        words against the index's Names and codes (:attr:`Catalog.entity_compact`; 2 or 3 words: also as a key):
        "Z12345" finds "Z-12345", "zen-trova" finds "Zentrova"."""
        cat = self.catalog
        if all(w.isdigit() for w in key.split()):
            if n != 1:
                return []   # numbers and dates are filters
            k = key.lstrip("0") or "0"
            return [(k, e) for e in self._entries(k) if e.kind == "code"]
        own = [(key, e) for e in self._entries(key)]
        if own or n > 3:
            return own
        compact = key.replace(" ", "")
        if len(compact) < 4:
            return []
        out: list[tuple[str, Any]] = []
        if n >= 2:
            out += [(compact, e) for e in self._entries(compact) if e.kind in ("name", "code")]
        for k in (getattr(cat, "entity_compact", None) or {}).get(compact, ()):
            if k != key:
                out += [(k, e) for e in self._entries(k) if e.kind in ("name", "code")]
        return out

    def _entries(self, key: str) -> tuple:
        cat = self.catalog
        fn = getattr(cat, "entity_entries", None)
        if fn is not None:
            return tuple(fn(key) or ())
        return tuple((getattr(cat, "entities", None) or {}).get(key) or ())

    def _match(self, ctx: _Question, i: int, n: int, cands: list[tuple[str, Any]],
               read_as: str | None = None) -> list[dict]:
        """The spots of the question words ``i .. i+n-1`` for candidate entries: one per (stored key, object, field),
        with its strength (:meth:`_strength`); entries the rules leave out give none."""
        if not cands:
            return []
        cat = self.catalog
        typed = ctx.key(i, n)
        whole = ctx.whole_quote(i, n)
        prev, prev2, nxt = ctx.word(i - 1), ctx.word(i - 2), ctx.word(i + n)
        num_before = prev2 if prev in _NUMBER_WORDS else prev   # "case no 98765"
        owned = ctx.own[i:i + n]
        groups: dict[tuple[str, str, str, str], list[Any]] = {}
        for skey, e in cands:
            if getattr(e, "truncated", False) and read_as is not None:
                continue   # never read a misspelling into a cut field (the typed value may be one it left out)
            if e.kind == "code" and not ctx.one_word(i, n):
                continue   # "z 1000" is no code, "Z-1000" is
            cues = self._cues(e.obj, e.field, e.kind)
            if (skey.isdigit() and e.kind == "code") or (e.kind == "name" and _date_shaped(skey)):
                if not (whole or _cue_in(num_before, cues)):
                    continue   # a digits-only code or a date-shaped Name: only after its cue word, or quoted
            if n == 1 and len(typed) <= 2 and not (_cue_in(prev, cues) or _cue_in(nxt, cues)):
                continue   # a 1- or 2-letter value only next to a cue word of its object ("XY team")
            if any(owned) and read_as is None and skey != typed:
                continue   # a compact match on a person's words: the people path owns them
            groups.setdefault((skey, e.obj, e.field, e.kind), []).append(e)
        out: list[dict] = []
        for (skey, obj, fld, kind), ents in groups.items():
            ents.sort(key=lambda e: (-int(e.records or 0), str(e.value)))
            strength, why, cue = self._strength(ctx, i, n, skey, obj, fld, kind, sum(int(e.records or 0) for e in ents))
            if n == 1 and owned and owned[0] and (strength != "strong" or why not in _MARKED):
                continue   # a confirmed one-word person: a value on it needs a cue word or a whole quote
            spot = {"span": ctx.q[ctx.toks[i].start:ctx.toks[i + n - 1].end], "start": ctx.toks[i].start,
                    "end": ctx.toks[i + n - 1].end, "key": skey, "object": obj, "field": fld, "kind": kind,
                    "value": str(ents[0].value), "values": list(dict.fromkeys(str(e.value) for e in ents)),
                    "records": sum(int(e.records or 0) for e in ents),
                    "truncated": any(bool(getattr(e, "truncated", False)) for e in ents),
                    "strength": strength, "why": why, "cue": cue}
            if read_as is not None:
                spot["read_as"] = read_as
            phrases = getattr(cat, "design_phrases", None) or frozenset()
            spot["listed"] = strength == "strong" or (
                kind in ("name", "code", "text") and skey not in phrases and not self._design_key(skey)
                and skey not in _HINT_STOPWORDS and not ctx.in_phrase(i, n) and not
                (ctx.in_quote(i, n) and not ctx.whole_quote(i, n)))
            out.append(spot)
        return out

    def _cues(self, obj: str, fld: str, kind: str) -> frozenset[str]:
        """The cue words of a value: its object's (:meth:`Catalog.entity_cues`); a text value's are its field's
        (:meth:`Catalog.field_cues`: the head of the field's label). Never a function word of the hints
        (:data:`_HINT_STOPWORDS`: "type", "number", "list" ...) or a blank value."""
        cat = self.catalog
        fn = getattr(cat, "field_cues" if kind == "text" else "entity_cues", None)
        try:
            got = set((fn(obj, fld) if kind == "text" else fn(obj)) or ()) if fn is not None else set()
        except Exception:  # noqa: BLE001 - optional: no cue words
            got = set()
        return frozenset(w for w in got if w not in BLANK_VALUES and w not in _HINT_STOPWORDS)

    def _design_key(self, skey: str) -> bool:
        """``skey`` is also a picklist value or a record type name somewhere: design vocabulary."""
        return any(e.kind in ("picklist", "record_type") for e in self._entries(skey))

    def _strength(self, ctx: _Question, i: int, n: int, skey: str, obj: str, fld: str, kind: str,
                  records: int) -> tuple[str, str, str]:
        """``(strength, why, cue)`` of one spot by the rules of the value spots design (section 4.3), in order. Cue
        words, quotes and capitals read the typed words; the word rules (unknown word, multi-word, 3 letters) read the
        stored key, without the words a person owns (a spot's strength never comes from a person's words). A compact
        match over two or three typed words ("do Zinc" for a stored "Dozinc") reads the typed words in the word rules
        and is no multi-word name: known words glued together are no record identity."""
        cat = self.catalog
        phrases = getattr(cat, "design_phrases", None) or frozenset()
        labels = getattr(cat, "label_phrases", None) or frozenset()
        typed = ctx.key(i, n)
        if kind == "code":
            return "strong", "code", ""
        if kind == "contact":
            return "strong", "contact", ""
        if kind in ("picklist", "record_type"):
            return "hint", kind, ""
        design = skey in phrases or self._design_key(skey)
        if kind == "text":
            info = (getattr(cat, "entity_fields", None) or {}).get((obj, fld)) or {}
            distinct = info.get("distinct")
            if distinct is None or int(distinct) <= TEXT_INDEX_MAX_DISTINCT or records > VALUE_FEW_RECORDS or \
                    self._design_key(skey):
                return "hint", "value list", ""
        if ctx.in_phrase(i, n):
            return "hint", "inside a design phrase", ""
        whole = ctx.whole_quote(i, n)
        if (ctx.in_quote(i, n) and not whole) or (whole and design):
            return "hint", "quoted design value", ""
        cues = self._cues(obj, fld, kind)
        nxt, prev, prev2 = ctx.word(i + n), ctx.word(i - 1), ctx.word(i - 2)
        before = prev2 if prev in _NAMED_WORDS else prev
        if _cue_in(nxt, cues) and f"{typed} {nxt}" not in phrases:
            return "strong", "cue word", nxt
        if _cue_in(before, cues) and f"{before} {typed}" not in phrases and not (n == 1 and typed in labels) and (
                ctx.ends_phrase(i, n) or ctx.capitals(i, n) or not self._plain_words(typed)):
            # review F3 (b): plain words after a cue word must end their noun phrase ("team training?" names a
            # department, "team training session" a kind of session)
            return "strong", "cue word", before
        if whole:
            return "strong", "quoted", ""
        if ctx.acronym(i, n):
            return "hint", "acronym", ""   # review F3 (a): an acronym is always typed in capitals; capitals mark nothing
        common = _common_words()
        words = self.design_words()   # catalog vocabulary, knowledge words and function words
        glued = skey != typed and len({t.chunk for t in ctx.toks[i:i + n]}) > 1   # compact over typed words
        ws = (typed if glued else skey).split()
        free = [w for k, w in enumerate(ws) if not (len(ws) == n and ctx.own[i + k])]   # the words no person owns
        design_only = all(w in words or w in _HINT_STOPWORDS for w in free)
        if common and free and ctx.capitals(i, n) and not design and not design_only:
            return "strong", "capitalised", ""
        why = ""
        if common and any(self._unknown_word(w, common, words) for w in free):
            why = "unknown word"
        elif kind == "name" and len(ws) >= 2 and free and not design and not design_only and not glued:
            why = "multi-word"
        if why and len(ws) == 1 and len(ws[0]) == 3:
            return "hint", "3 letters", ""
        if why:
            return "strong", why, ""
        return "hint", "plain words", ""

    def _plain_words(self, key: str) -> bool:
        """Every word of ``key`` is a common English word, a design word (:meth:`design_words`) or a function word
        (False without an English word list: no word is common then)."""
        common = _common_words()
        words = self.design_words()
        return bool(common) and all(w in common or w in words or w in _HINT_STOPWORDS for w in key.split())

    def _unknown_word(self, w: str, common: Any, words: Any) -> bool:
        """A word of 4 or more letters that no English word list, no design word (``words``: :meth:`design_words`), no
        spoken word, no function word and no stored person's first or last name holds (a plural "s" aside for the word
        lists and the design words): a record's own name ("zentrova")."""
        if sum(ch.isalpha() for ch in w) < 4 or w.isdigit():
            return False
        if w in common or w in words or w in _HINT_STOPWORDS or w in _spoken_words() or \
                (w.endswith("s") and (w[:-1] in common or w[:-1] in words)):
            return False
        try:
            self._name_index()
        except Exception:  # noqa: BLE001 - no name index: no first or last name to rule out
            pass
        return w not in (self._names_first or {}) and w not in (self._names_last or {})

    def _spelling_value_spots(self, ctx: _Question, typed: list[dict]) -> list[dict]:
        """The spelling help's reading of misspelt values (design section 4.4, as the prototype reads it): the
        question's words with every ``corrected`` span (:meth:`_spelling_spans`: stored values included, person names
        left out) replaced by its spelling are matched again (:meth:`_index_spots`); only word sequences holding a
        changed word give spots, with ``read_as`` (the corrected text of the span) and the strength of the stored key
        (the typed words never count as unknown words). A span is read only over words that no person owns and that
        no typed spot crossing its edge uses; the typed spots inside it are tried again in the reading (the caller
        drops the ones a reading covers), unless one of them is the whole span as a stored Name or code (the typed
        record wins). Never for an entry of a cut field; a did-you-mean (``ambiguous``) is never read."""
        try:
            spans = [s for s in self._spelling_spans(ctx.q)
                     if getattr(s, "decision", "") == "corrected" and getattr(s, "used", None)]
        except Exception:  # noqa: BLE001 - optional
            return []
        used = list(ctx.used)
        starts: dict[int, tuple[Any, list[int]]] = {}
        taken: set[int] = set()
        for s in spans:
            idx = [j for j, t in enumerate(ctx.toks) if t.start >= s.start and t.end <= s.end]
            words = entity_key(str(s.used)).split()
            if not idx or idx != list(range(idx[0], idx[-1] + 1)) or taken & set(idx) or not words \
                    or any(ctx.toks[j].api for j in idx):
                continue
            if any(ctx.own[j] and (len(words) != len(idx) or words[k] != ctx.toks[j].key) for k, j in enumerate(idx)):
                continue   # never read a person's word as another word
            inside = [t for t in typed if s.start <= t["start"] and t["end"] <= s.end]
            crossing = [t for t in typed if t["start"] < s.end and s.start < t["end"] and t not in inside]
            if crossing or any(t["start"] == ctx.toks[idx[0]].start and t["end"] == ctx.toks[idx[-1]].end
                               and t["kind"] in ("name", "code") for t in inside):
                continue
            if any(used[j] and not any(t["start"] <= ctx.toks[j].start and ctx.toks[j].end <= t["end"] for t in inside)
                   for j in idx):
                continue   # a word the contact step or an API name holds
            for j in idx:
                used[j] = False   # the typed spots inside the span are tried again in the reading
            starts[idx[0]] = (s, idx)
            taken |= set(idx)
        if not starts:
            return []
        toks: list[_Tok] = []
        r_used: list[bool] = []
        r_own: list[bool] = []
        fixed: list[Any] = []
        i = 0
        while i < len(ctx.toks):
            if i not in starts:
                toks.append(ctx.toks[i])
                r_used.append(used[i])
                r_own.append(ctx.own[i])
                fixed.append(None)
                i += 1
                continue
            s, idx = starts[i]
            words = entity_key(str(s.used)).split()
            same = len(words) == len(idx)   # word for word: each read word keeps its typed word's place
            for k, w in enumerate(words):
                t = ctx.toks[idx[k]] if same else None
                toks.append(_Tok(w, t.start if t else s.start, t.end if t else s.end, ctx.toks[i].chunk,
                                 t.text if t else ctx.q[s.start:s.end], False))
                r_used.append(False)
                r_own.append(bool(same and ctx.own[idx[k]]))   # a person's unchanged word stays the person's
                fixed.append(s)
            i = idx[-1] + 1
        read = _Question(ctx.q, toks, getattr(self.catalog, "design_phrases", None) or frozenset())
        read.used, read.own = r_used, r_own
        out = self._index_spots(read, fixed=fixed)
        for j, t in enumerate(ctx.toks):   # the typed words of a reading are used
            if any(sp["start"] <= t.start and t.end <= sp["end"] for sp in out):
                ctx.used[j] = True
        return out

    # ------------------------------------------------------------------ e-mail addresses and phone numbers
    def _contact_spots(self, q: str, ctx: _Question) -> list[dict]:
        """Spots of kind ``contact`` for an e-mail address or a phone number (10 to 15 digits, or ``+`` and 7 or more;
        never a date or a time) typed in the question that a contact field stores as its whole value (case and
        spacing aside; a phone by its last 10 digits): one statement per object with contact fields, at most
        :data:`VALUE_CONTACT_BUDGET_S` in all. The question's words of such a value are no other value. An e-mail
        address is strong; a phone number only when the question marks it as one (``asks_for_contact``, or "+",
        brackets, spaces or dashes in it), else a hint (a bare number stays a filter); a placeholder digit string
        (:data:`_DIGIT_RUNS`, one repeated digit) is never matched. What the tools show of a contact field follows
        ``engine/contact.py`` unchanged."""
        shapes: list[tuple[int, int, str, str]] = []   # (start, end, "<e-mail>" | "<phone>", the compared form)
        for m in _EMAIL_RE.finditer(q):
            a = m.start() + len(m.group(0)) - len(m.group(0).lstrip("([{<\"'`‘“"))
            shapes.append((a, m.end(), "<e-mail>", q[a:m.end()].strip().lower()))
        marked: set[int] = set()   # the shapes the question marks as phone numbers (review PPI-1)
        asked = None
        for m in _PHONE_RE.finditer(q):
            text = m.group(0)
            digits = re.sub(r"\D", "", text)
            if _DATE_IN_RE.search(text) or any(a < m.end() and m.start() < b for a, b, _k, _v in shapes):
                continue
            if len(set(digits)) == 1 or any(digits in run for run in _DIGIT_RUNS):
                continue   # a placeholder ("0000000000", "1234567890"): no phone number
            if 10 <= len(digits) <= 15 or (text.startswith("+") and 7 <= len(digits) <= 15):
                if asked is None:
                    asked = asks_for_contact(q)
                if asked or text.startswith("+") or _PHONE_FORMAT_RE.search(text):
                    marked.add(len(shapes))
                shapes.append((m.start(), m.end(), "<phone>", digits[-10:]))
        if not shapes:
            return []
        for j, t in enumerate(ctx.toks):   # the words of a typed e-mail address or phone number are no other value
            if any(a <= t.start and t.end <= b for a, b, _k, _v in shapes):
                ctx.used[j] = True
        cat = self.catalog
        extra = {(o, f) for (o, f), (level, _w) in FIELD_SENSITIVITY.items() if level == "contact"}
        deadline = time.monotonic() + VALUE_CONTACT_BUDGET_S
        out: list[dict] = []
        for spec in cat.queryable():
            if spec.sensitive or not spec.rows:
                continue
            try:
                fields = list(cat.contact_fields(spec))
            except Exception:  # noqa: BLE001
                fields = []
            have = {f.api for f in fields}
            fields += [f for f in (spec.fields.get(fa) for o, fa in extra if o == spec.api)
                       if f is not None and f.has_data and f.sensitivity == "contact" and f.api not in have]
            if not fields:
                continue
            left = deadline - time.monotonic()
            if left <= 0.02:
                break
            items: list[Frag] = []
            keys: list[tuple[int, Any]] = []
            for k, (_a, _b, kind, val) in enumerate(shapes):
                for f in fields:
                    col = f"CAST(x.{quote_ident(f.api)} AS VARCHAR)"
                    if kind == "<e-mail>":
                        items.append(Frag(f"COUNT(*) FILTER (WHERE lower(trim({col})) = ?)", (val,)))
                    else:
                        items.append(Frag(f"COUNT(*) FILTER (WHERE regexp_matches(trim({col}), '{_PHONE_VALUE_SQL}') "
                                          f"AND right(regexp_replace({col}, '[^0-9]', '', 'g'), 10) = ?)", (val,)))
                    keys.append((k, f))
            sel = Frag(", ".join(f"{it.sql} AS \"n{m}\"" for m, it in enumerate(items)),
                       tuple(p for it in items for p in it.params))
            try:
                st = self._exec(None, Frag("SELECT ") + sel + Frag(f" FROM main.{quote_ident(spec.api)} AS x"),
                                max_rows=1, timeout=max(0.05, left))
            except RecordError:
                continue
            row = st.rows[0] if st.rows else []
            for (k, f), n in zip(keys, row):
                if int(n or 0) > 0:
                    a, b, kind, _v = shapes[k]
                    # an e-mail address is a contact value; a bare number only when the question marks it as a phone
                    # number (it asks for contact details, or "+", brackets, spaces or dashes): else a hint, which
                    # never overrules the route call (numbers stay filters)
                    strong = kind == "<e-mail>" or k in marked
                    out.append({"span": q[a:b], "start": a, "end": b, "key": kind, "object": spec.api, "field": f.api,
                                "kind": "contact", "values": [], "records": int(n), "truncated": False,
                                "strength": "strong" if strong else "hint", "why": "contact", "cue": "",
                                "listed": True})
        return out

    # ================================================================== the hint section (object_hints)
    def _question_values(self, question: str) -> list[dict]:
        """The value spots of the current question: those the session gave (``begin_question(values=...)``), else
        spotted here (with the people the session gave). The corrected form of the current question (what
        ``object_hints`` searches with when the spelling help is on) stands for the question as typed."""
        q = str(question or "")
        cur = getattr(self, "_question", "")
        if getattr(self.catalog, "resolver", None) is not None and q != cur and cur and \
                q == corrected_text(cur, self._spelling_spans(cur)):
            q = cur
        given = getattr(self, "_given_values", None)
        if given is not None and q == cur:
            return [dict(v) for v in given]
        return self.spot_values(q, people=getattr(self, "_given_people", None) if q == cur else None)

    def value_hint_lines(self, question: str, shown: Any = ()) -> list[str]:
        """The "Records named in the question" section of the hints (:data:`RECORDS_VALUE_HINTS`), as lines, or []
        without a listed spot. One line per named span (at most :data:`VALUE_HINT_MAX_SPANS`, strong ones first, then
        in question order), each with up to :data:`VALUE_HINT_MAX_PLACES` places (Names and codes first, then text
        by records): the object, field, stored value and records; for a Name or code held by at most
        :data:`VALUE_HINT_LINK_RECORDS` records its record types and whether its description field is filled (one
        statement) and, for the first :data:`VALUE_HINT_LINKED_SPANS` such spans, the business lookups of other
        objects that link to it, with counts (the people hints' scan). Every such Name or code place of a span is
        counted, the strong one with the most linked records comes first and the others say their counts in the
        "Also" list; "nothing links to it" is never said while another Name place of the span was not counted (review
        F2). A hint spot says "(may be a plain word, not a record)", a spelling-help reading "(read as 'X')". ``shown``: the ``value_matches`` rows the hints' value line
        prints; a text place among them is not repeated. All statements within :data:`VALUE_HINT_BUDGET_S`; the
        section is cut to :data:`VALUE_HINT_MAX_CHARS` ("Also" places first, then the linked lists of the last
        spans, then whole spans from the end). No record Id and no stored value other than the named ones."""
        other = not_the_term(question)   # a business term's other reading is never hinted (engine.business_terms)
        spots = [s for s in self._question_values(question) if isinstance(s, dict) and s.get("listed")
                 and s.get("object") and s.get("field") and not (other and all(
                     is_not_the_term(other, s["object"], s["field"], x) for x in (s.get("values") or [s.get("value")])))]
        printed = {(str(r[0]), str(r[1]), str(r[2])) for r in shown or () if len(r) >= 3}
        spots = [s for s in spots if not (s.get("kind") == "text" and any(
            (s["object"], s["field"], str(v)) in printed for v in (s.get("values") or [s.get("value")])))]
        if not spots:
            return []
        spans: dict[tuple[int, int], list[dict]] = {}
        for s in spots:
            spans.setdefault((int(s.get("start") or 0), int(s.get("end") or 0)), []).append(s)
        ordered = sorted(spans.values(), key=lambda g: (0 if any(s.get("strength") == "strong" for s in g) else 1,
                                                        int(g[0].get("start") or 0)))[:VALUE_HINT_MAX_SPANS]
        deadline = time.monotonic() + VALUE_HINT_BUDGET_S
        items: list[dict[str, str]] = []
        linked_spans = 0
        for group in ordered:
            names = sorted((s for s in group if s.get("kind") in ("name", "code", "contact")),
                           key=lambda s: (0 if s.get("strength") == "strong" else 1, int(s.get("records") or 0),
                                          str(s["object"]), str(s["field"])))
            texts = sorted((s for s in group if s.get("kind") == "text"),
                           key=lambda s: (-int(s.get("records") or 0), str(s["object"]), str(s["field"])))
            # review F2: every Name or code place of the span (up to VALUE_HINT_MAX_PLACES, few records) gets its
            # record facts and, for the first VALUE_HINT_LINKED_SPANS spans, its linked records; the main place is the
            # strong one with the most linked records, so a Name stored in two objects points at the one with links
            few = [s for s in names if s.get("kind") in ("name", "code")
                   and int(s.get("records") or 0) <= VALUE_HINT_LINK_RECORDS][:VALUE_HINT_MAX_PLACES]
            facts_of: dict[int, dict | None] = {}
            links_of: dict[int, tuple[str, int]] = {}
            count_links = bool(few) and linked_spans < VALUE_HINT_LINKED_SPANS
            linked_spans += 1 if count_links else 0
            for s in few:
                try:
                    facts_of[id(s)] = self._record_facts(s, deadline)
                except Exception:  # noqa: BLE001 - optional: the place without its record facts
                    facts_of[id(s)] = None
                if count_links:
                    try:
                        links_of[id(s)] = self._linked_counts(s["object"], facts_of[id(s)], deadline)
                    except Exception:  # noqa: BLE001 - optional: no linked list
                        links_of[id(s)] = ("", 0)
            names.sort(key=lambda s: (0 if s.get("strength") == "strong" else 1, -links_of.get(id(s), ("", 0))[1],
                                      int(s.get("records") or 0), str(s["object"]), str(s["field"])))
            places = (names + texts)[:VALUE_HINT_MAX_PLACES]
            if not places:
                continue
            main = places[0]
            facts = facts_of.get(id(main))
            linked = links_of.get(id(main), ("", 0))[0]
            unscanned = any(s is not main and id(s) not in links_of for s in names if s.get("kind") in ("name", "code"))
            if linked.startswith("Linked: no ") and unscanned:
                linked = ""   # another Name place of the span was not counted: never say that nothing links to it
            marks = f" (read as '{main['read_as']}')" if main.get("read_as") else ""
            if not any(s.get("strength") == "strong" for s in group):
                marks += " (may be a plain number, not a phone number)" if main.get("kind") == "contact" else \
                    " (may be a plain word, not a record)"
            head = f"- '{_disp(main.get('span'))}'{marks}: {self._place_text(main, facts)}."
            also = [self._also_text(p, main, links_of.get(id(p), ("", 0))) for p in places[1:]]
            items.append({"head": head, "linked": linked, "also": ("Also " + "; ".join(also) + ".") if also else ""})
        return self._fit_value_section(items)

    def _fit_value_section(self, items: list[dict[str, str]]) -> list[str]:
        """The section text cut to :data:`VALUE_HINT_MAX_CHARS`: the "Also" places go first (last span first), then
        the "Linked" lists of the last spans, then whole spans from the end; every span keeps its first place while it
        fits."""
        if not items:
            return []
        use_also = [bool(it["also"]) for it in items]
        use_linked = [bool(it["linked"]) for it in items]
        keep = len(items)

        def text() -> str:
            lines = []
            for k, it in enumerate(items[:keep]):
                parts = [it["head"]] + ([it["linked"]] if use_linked[k] else []) + ([it["also"]] if use_also[k] else [])
                lines.append(" ".join(parts))
            return _prompt_files.fill(RECORDS_VALUE_HINTS, LINES="\n".join(lines))

        out = text()
        while len(out) > VALUE_HINT_MAX_CHARS:
            k = next((k for k in range(keep - 1, -1, -1) if use_also[k]), None)
            j = next((j for j in range(keep - 1, -1, -1) if use_linked[j]), None) if k is None else None
            if k is not None:
                use_also[k] = False
            elif j is not None:
                use_linked[j] = False
            elif keep > 1:
                keep -= 1
            else:   # one span alone is too long: its line is cut
                room = max(80, len(items[0]["head"]) - (len(out) - VALUE_HINT_MAX_CHARS))
                items[0] = dict(items[0], head=_cut_text(items[0]["head"], room))
                out = text()
                break
            out = text()
        return out.split("\n")

    def _place_text(self, s: dict, facts: dict | None) -> str:
        """The first place of a span: ``Obj.Field 'Value' (n records, record type X; Description__c empty)``; a text
        field says "(text)"; a contact field says where the typed e-mail address or phone number is stored."""
        obj, fld, n = s["object"], s["field"], int(s.get("records") or 0)
        if s.get("kind") == "contact":
            what = "e-mail address" if s.get("key") == "<e-mail>" else "phone number"
            return (f"the {what} in the question is stored in {obj}.{fld} on {_records(n)} (a contact field: filter "
                    "on it; show it only when the question asks for contact details)")
        inner = _records(n)
        facts = facts if facts and not facts.get("late") else None
        if facts and facts.get("types"):
            types = facts["types"]
            inner += (", record type " if len(types) == 1 else ", record types ") + ", ".join(types)
        if facts and facts.get("description"):
            inner += "; " + facts["description"]
        if s.get("truncated"):
            inner += "; index cut: the count may be higher"
        kind = " (text)" if s.get("kind") == "text" else ""
        return f"{obj}.{fld}{kind} '{_disp(s.get('value'))}' ({inner})"

    @staticmethod
    def _also_text(s: dict, main: dict, links: tuple[str, int] = ("", 0)) -> str:
        """Another place of a span: ``Obj.Field (n records)``, ``Obj.Field (text, n records)``; its stored value when
        it is spelt otherwise than the first place's; ``links`` (:meth:`_linked_counts`): a Name place with linked
        records says them too, ``Obj.Field (1 record; Linked: Child.Lookup 11)``."""
        n = int(s.get("records") or 0)
        val = _disp(s.get("value"))
        other = bool(val) and s.get("kind") != "contact" and val.casefold() != _disp(main.get("value")).casefold()
        spelt = f" '{val}'" if other else ""
        kind = "text, " if s.get("kind") == "text" else ""
        cut = "; index cut: the count may be higher" if s.get("truncated") else ""
        linked = f"; {links[0].rstrip('.')}" if links[1] > 0 else ""
        return f"{s['object']}.{s['field']}{spelt} ({kind}{_records(n)}{cut}{linked})"

    def _description_field(self, spec: Any) -> Any:
        """The object's text or long text field whose API name or label says "description" (``Description`` or
        ``Description__c`` first), never a blocked or contact field; None when it has none."""
        cat = self.catalog
        found = [f for f in spec.fields.values() if f.kind in ("text", "longtext") and not f.sensitivity
                 and f.api.lower() not in cat.blocked_columns
                 and ("description" in f.api.lower() or "description" in str(f.label or "").lower())]
        found.sort(key=lambda f: (0 if f.api in ("Description", "Description__c") else 1, 0 if f.documented else 1,
                                  -f.filled, f.api))
        return found[0] if found else None

    def _record_facts(self, s: dict, deadline: float) -> dict | None:
        """The records of a named Name or code (at most :data:`VALUE_HINT_LINK_RECORDS`), read with one statement:
        ``{"ids", "types", "description"}`` (record type names; "<field> empty", "<field> filled" or "<field> filled
        on k of n"); ``{"late": True}`` when the statement could not run in time (or failed); None when the object
        cannot be read. An Account place reads only the accounts that are no person, the rows the index read. The Ids
        stay in code."""
        cat = self.catalog
        spec = cat.objects.get(str(s.get("object")))
        if spec is None or not spec.has_table or spec.sensitive or "Id" not in spec.fields:
            return None
        f = spec.fields.get(str(s.get("field")))
        if f is None or f.sensitivity == "blocked" or f.api.lower() in cat.blocked_columns:
            return None
        values = [str(v) for v in (s.get("values") or [s.get("value")]) if v is not None]
        if not values:
            return None
        left = deadline - time.monotonic()
        if left <= 0.05:
            return {"late": True}
        rt = spec.fields.get("RecordTypeId")
        rt = rt if rt is not None and rt.has_data and rt.sensitivity != "blocked" else None
        desc = self._description_field(spec)
        cols = ['x."Id" AS id', 'x."RecordTypeId" AS rt' if rt is not None else "CAST(NULL AS VARCHAR) AS rt"]
        cols.append(f"(NULLIF(trim(CAST(x.{quote_ident(desc.api)} AS VARCHAR)), '') IS NOT NULL) AS d"
                    if desc is not None else "CAST(NULL AS BOOLEAN) AS d")
        where = f"trim(CAST(x.{quote_ident(f.api)} AS VARCHAR)) = ANY(?)"
        if spec.api == "Account" and "IsPersonAccount" in spec.fields:
            where += ' AND NOT COALESCE(x."IsPersonAccount", false)'   # client companies: the rows the index read
        try:
            st = self._exec(None, Frag(f"SELECT {', '.join(cols)} FROM main.{quote_ident(spec.api)} AS x WHERE {where} "
                                       f"LIMIT {VALUE_HINT_LINK_RECORDS + 1}", (values,)),
                            max_rows=VALUE_HINT_LINK_RECORDS + 1, timeout=max(0.05, left))
        except RecordError:
            return {"late": True}
        rows = list(st.rows or [])
        names = {str(r.get("id")): str(r.get("name") or "") for r in spec.record_types if r.get("id")}
        types = list(dict.fromkeys(names[str(r[1])] for r in rows if r[1] is not None and names.get(str(r[1]))))
        out: dict[str, Any] = {"ids": [str(r[0]) for r in rows if r[0] is not None], "types": types, "description": ""}
        if desc is not None and rows:
            k = sum(1 for r in rows if r[2])
            out["description"] = (f"{desc.api} empty" if k == 0 else f"{desc.api} filled" if k == len(rows)
                                  else f"{desc.api} filled on {k} of {len(rows)}")
        return out

    def _linked_text(self, obj: str, facts: dict | None, deadline: float) -> str:
        """"Linked: Child.Lookup n, ..." for the named records (:meth:`_linked_counts`, its text alone)."""
        return self._linked_counts(obj, facts, deadline)[0]

    def _linked_counts(self, obj: str, facts: dict | None, deadline: float) -> tuple[str, int]:
        """``(text, linked records)``: "Linked: Child.Lookup n, ..." for the named records, the business lookups of
        other objects that point to them, with their counts, most first (:data:`VALUE_HINT_LOOKUPS`, then "and n more
        lookups"), and the sum of the counts; the people hints' helpers (:meth:`_child_pairs` without system lookups
        and identity links, :meth:`_scan`). A scan past the deadline says the linked records were not counted."""
        late = "Linked records not counted (time budget)."
        if facts is None:
            return "", 0
        if facts.get("late"):
            return late, 0
        ids = list(facts.get("ids") or [])
        if not ids:
            return "", 0
        pairs = self._child_pairs([(obj, rid) for rid in ids], system=False)
        if not pairs:
            return "Linked: no business lookup of another object points to it.", 0
        try:
            counts = self._scan(None, pairs, deadline=deadline)
        except RecordError:
            return late, 0
        agg: dict[tuple[str, str], int] = {}
        for (c, via, _rid), n in counts.items():
            if n > 0:
                agg[(c, via)] = agg.get((c, via), 0) + int(n)
        if not agg:
            return "Linked: no record of another object links to it.", 0
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
        more = len(ranked) - VALUE_HINT_LOOKUPS
        return ("Linked: " + ", ".join(f"{c}.{v} {n}" for (c, v), n in ranked[:VALUE_HINT_LOOKUPS])
                + (f", and {more} more lookup{'s' if more > 1 else ''}" if more > 0 else "") + "."), sum(agg.values())
