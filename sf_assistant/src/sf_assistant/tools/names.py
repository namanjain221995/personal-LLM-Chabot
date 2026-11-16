"""The person-name index of the record tools: spotting names in a question, object and people hints.

Moved verbatim from ``okrec/tools.py``. The index is ``RecordTools`` state built with the warehouse connection, so it
lives here as a mixin, not in the engine.

With the spelling help on (``SFA_RESOLVER=on``: ``catalog.resolver`` is the session's term resolver,
docs/resolver_integration.md step (5)) misspelt person names are read through the resolver instead of the close-surname
matcher (``_close_surname``) and the difflib staff-name check: :meth:`NamesMixin._spot`,
:meth:`NamesMixin.resolve_person_name`, :meth:`NamesMixin.staff_close_name` and the hint line of a missing name. A
person is used only when the resolver decides ``corrected`` (a single first or last name only for doubled letters or an
identical sound, :func:`_one_name_slip`), and every such use is stated first ("'x' is not a stored name; the closest
stored name is 'Y'"); an ``ambiguous`` name is never used: it is listed as a did-you-mean. The resolver's spellings are
mapped back to stored people by name through the name index (:func:`_stored_people`), never by the resolver's own Ids.
With it off (``SFA_RESOLVER=off``: ``catalog.resolver`` None) every result is the one without it.

Group B (docs/people.md): :meth:`NamesMixin.first_name_fit` tells the session whether a first name that neither the
route call nor the names call read names one stored person a lookup of the question holds (item 13), and the hints
rank a lookup copied from a child object after its source (item 9).
"""

from __future__ import annotations

import re
import time
from typing import Any

from sf_assistant.knowledge.store import split_api_name
from sf_assistant.engine.catalog import OBJECT_NOTES, PERSON_OBJECTS, SYSTEM_LOOKUPS, FieldSpec, RecordError, norm_name
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.engine.values import normalize
from sf_assistant.engine.warehouse import WarehouseError

from sf_assistant.tools import record_base as _base   # the limits MAX_PEOPLE, SCAN_BUDGET_S: read (and patched) there
from sf_assistant.tools.record_base import (_HINT_STOPWORDS, _PERSON_ORDER, _short, _disp, _cut_text, _without_words,
                                            _close_surname, name_key, _q_tokens)


#: Objects that one unqualified noun of a question can mean for a person, documented as different objects of the same
#: kind of record (``OBJECT_NOTES`` / ``INTERVIEW_NOTE``; records prompt step 1: "interviews" with no role word counts
#: both). Objects whose labels only end with the same word (Session__c and Candidate_Portal_Session__c, the two
#: evaluation or attendee objects) are not counted together.
SAME_NOUN_OBJECTS: tuple[frozenset[str], ...] = (frozenset({"Interview__c", "Internal_Interview__c"}),)

NAME_NGRAM_MAX = 6        # longest word sequence matched as a stored full name
MANY_PEOPLE = 6           # above this, a first name gets one compact hint line (no reference scan)

NAME_INDEX_BUDGET_S = 3.0   # the person-name index (hints, N10): built once; its statements stop after this in total

HINT_MAX_CHARS = 2_000
_COMMON_WORDS: set[str] | None = None   # lower-case entries of the system English word lists (first_name_fit), lazily


def _common_words() -> set[str]:
    """The lower-case entries of the system English word lists (``knowledge.terms.LEXICON_PATHS``: common words, not
    the capitalised proper nouns), read once; empty when no list is installed (then no word is common)."""
    global _COMMON_WORDS
    if _COMMON_WORDS is None:
        from sf_assistant.knowledge.terms import LEXICON_PATHS

        words: set[str] = set()
        for path in LEXICON_PATHS:
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    words.update(w for w in (ln.strip() for ln in fh) if w.isascii() and w.isalpha() and w.islower())
            except OSError:
                continue
        _COMMON_WORDS = words
    return _COMMON_WORDS


def _written_as_name(question: str, word: str) -> bool:
    """The question writes ``word`` capitalised after its first word (a name inside a sentence: "that Love hosted"),
    not only at the start of a sentence."""
    toks = re.findall(r"[A-Za-z]+|[.!?]", str(question or ""))
    for i, t in enumerate(toks):
        if t.lower() == word and t[:1].isupper() and i > 0 and toks[i - 1] not in ".!?":
            return True
    return False

#: Question words (stems) that can mean every assigned record or only the finished ones ("taken" = all mocks the
#: person is the interviewer of, or only the completed ones). When the role a hint marks as fitting came from one of
#: them, the hints ask for the status split up front, so no extra model round is needed (RECORDS_SPEC §21, L7).
_COMPLETION_CUES = ("take", "taken", "took", "conduct", "held", "done", "attend", "join", "participat", "train",
                    "taught", "teach", "coach")

_TIME_WORDS = frozenset("this last next today yesterday since before after during between in on from".split()) | frozenset(
    "january february march april may june july august september october november december".split())


class NamesMixin:
    """Person-name index, name spotting and hints of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    def object_hints(self, question: str, missing: Any = ()) -> str:
        """Objects and stored values matching the question words (no counts), or ''. ``missing``: names of the
        question that no stored person has (the session's triage), each with one hint line. After the people sections
        and before the stored values line: the records the question names (``value_hint_lines`` of
        :mod:`sf_assistant.tools.value_spots`: where each is stored and which records link to it)."""
        try:
            hits = self.catalog.search_objects(question, k=3)
        except Exception:
            return ""
        top = [s.api for s, _, _ in hits]
        lines: list[str] = []
        if hits:
            lines.append("Objects whose knowledge matches the question words:")
            for spec, _score, _m in hits:
                grain = spec.record_grain or spec.one_line or spec.description
                text = f"- {spec.api} ({spec.label})"
                if grain:
                    text += ": " + _short(grain, 120)
                if not spec.has_table:
                    text += " [no records in the warehouse]"
                lines.append(text)
                if spec.api in OBJECT_NOTES:
                    lines.append("  note: " + _hint_note(spec.api))
        try:
            lines += self._people_hints(question, top, missing)
        except Exception:  # hints are optional: a failure leaves them out
            pass
        vals = self.catalog.value_matches(question, top_objects=top, n=5)
        value_lines = getattr(self, "value_hint_lines", None)   # the records the question names (tools.value_spots)
        if value_lines is not None:
            try:
                lines += value_lines(question, shown=vals)
            except Exception:  # hints are optional: a failure leaves them out
                pass
        if vals:
            conds = self.catalog.value_conditions(question)
            lines.append("Stored values that match words in the question:")
            for obj, fld, val, _n in vals:
                mark = ""
                if obj in conds.get(normalize(val), ()):   # "training sessions": the value is a condition
                    mark = (f" <- the question says '{normalize(val)}' right before '{self.catalog.label_of(obj).lower()}':"
                            " a condition of the question, filter on it")
                lines.append(f"- '{val}' in {obj}.{fld}{mark}")
        if not lines:
            return ""
        return "[Hints from code, not from the user]\n" + "\n".join(lines)

    def first_name_fit(self, question: str, spot: Any) -> dict | None:
        """Whether a first name of the question that neither the route call nor the names call read still names one
        stored person whose records a lookup the question asks about holds (RECORDS_SPEC §22.13; docs/people.md
        item 13): ``{"word", "lookup", "records", "name", "name_word"}`` or None. ``spot``: a :meth:`spot_people`
        entry (its ``words`` and ``recs``) or the word itself. All of these must hold, from the data: (a) the word is
        no design word (:meth:`is_design_word`); (b) every record whose first name it is has the same stored full
        name; (c) a business lookup of a child object holds those records on at least one record and its role fits
        a word of the question without the name words (:meth:`Catalog.role_fits`). ``word`` is that question word,
        ``lookup`` "<Obj>.<field>", ``records`` its count, ``name`` the stored full name and ``name_word`` the first
        name. (d) A common English word (a lower-case entry of the system word lists: "love", "rose") counts only
        when the question writes it capitalised inside a sentence ("that Love hosted"): both calls read the
        question and found no name, and in lower case such a word is the word ("i love how sessions are hosted").
        Without a word list (d) holds for every word. The reference scan stops after SCAN_BUDGET_S (then None).
        Never raises."""
        try:
            cat = self.catalog
            if isinstance(spot, dict):
                w = str(spot.get("words") or "")
                recs = list(spot.get("recs") or [])
            else:
                w = str(spot or "")
                recs = []
            toks = _q_tokens(w)
            if len(toks) != 1 or len(toks[0]) < 3 or toks[0] in _HINT_STOPWORDS or self.is_design_word(toks[0]):
                return None
            w = toks[0]
            if w in _common_words() and not _written_as_name(question, w):
                return None
            _full, _fl, first = self._name_index()
            if not recs:
                recs = list(first.get(w) or (first.get(w[:-1]) if w.endswith("s") and len(w) > 3 else None) or [])
            recs = [r for r in recs if r[0] in PERSON_OBJECTS]
            names = {name_key(r[2]) for r in recs}
            if not recs or len(names) != 1:
                return None
            rest = _without_words(question, [w] + [str(r[2]) for r in recs])
            deadline = time.monotonic() + _base.SCAN_BUDGET_S
            pairs = self._child_pairs([(r[0], r[1]) for r in recs], system=False)
            try:
                counts = self._scan(None, pairs, deadline=deadline)
            except RecordError:
                return None
            kind_of = {str(r[1]): cat.person_kind(r[0], r[3]) for r in recs}
            agg: dict[tuple[str, str], tuple[int, str]] = {}
            for (c, via, rid), n in counts.items():
                if n > 0:
                    old = agg.get((c, via), (0, kind_of.get(str(rid), "other")))
                    agg[(c, via)] = (old[0] + n, old[1])
            for (c, via), (n, kind) in sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0])):
                cs = cat.objects[c]
                fit = cat.role_fits(rest, cs, cs.fields[via], kind)
                if fit:
                    return {"word": fit, "lookup": f"{c}.{via}", "records": n, "name": _disp(recs[0][2]), "name_word": w}
            return None
        except Exception:  # noqa: BLE001 - optional: None keeps today's triage
            return None

    def names_person(self, question: str) -> bool:
        """True when the question holds the full name (2 to 4 words) of a stored person (Employee, User, Account or
        Lead, from the name index). Never raises. The session keeps such a question with the record agent even when
        the route call says it needs no records ("where does <name> appear")."""
        try:
            return any(len(words.split()) >= 2 for words, _recs in self._spot_people(question))
        except Exception:  # noqa: BLE001 - optional
            return False

    # ================================================================== object hints: people (RECORDS_SPEC §21.8)
    def _name_index(self) -> tuple[dict[str, list[tuple]], dict[str, list[tuple]], dict[str, list[tuple]]]:
        """Person names (built once, at the first use; RECORDS_SPEC §22.2): ``(full, fl, first)``. ``full``:
        :func:`name_key` of the stored name -> records of every person object; ``fl``: "first last" key of stored names
        of 3 or more words -> records; ``first``: first word -> records. ``fl`` and ``first`` (and ``_names_last``, the
        last word of names of 2 or more words, read only for a name the names call returned) leave out Account records
        that are no person (kind "other" and not ``IsPersonAccount``: client companies, whose Ids are kept in
        ``_company_ids``). Records are ``(object, Id, name, detail)``. One statement per person object, not logged (no
        counts shown)."""
        with self._index_lock:
            if self._names_full is None:
                full: dict[str, list[tuple]] = {}
                fl: dict[str, list[tuple]] = {}
                first: dict[str, list[tuple]] = {}
                last: dict[str, list[tuple]] = {}
                companies: set[str] = set()
                deadline = time.monotonic() + NAME_INDEX_BUDGET_S   # bounded: it can run inside a tool call (N10)
                self._names_partial = False
                cat = self.catalog
                for o in PERSON_OBJECTS:
                    if not self._queryable(o):
                        continue
                    left = deadline - time.monotonic()
                    if left <= 0.05:
                        self._names_partial = True   # the rest of the person objects are left out of the index
                        break
                    det, join = self._detail_sql(o, "x")
                    # Account: the standard IsPersonAccount says which accounts are people (candidates and employees'
                    # own accounts) and which are client companies
                    pers = ('x."IsPersonAccount"' if o == "Account" and "IsPersonAccount" in cat.objects[o].fields
                            else "CAST(NULL AS BOOLEAN)")
                    try:
                        st = self.warehouse.run(f'SELECT x."Id" AS id, trim(x."Name") AS name, {det} AS detail, {pers} '
                                                f'AS person FROM main.{quote_ident(o)} AS x{join} WHERE x."Name" IS NOT '
                                                "NULL", [], max_rows=500_000, max_bytes=100_000_000,
                                                timeout_s=min(float(self.rcfg.timeout_s), left))
                    except WarehouseError:
                        self._names_partial = True
                        continue
                    if st.truncated:
                        self._names_partial = True
                    for rid, nm, d, person in st.rows:
                        key = name_key(nm)
                        if not key:
                            continue
                        rec = (o, str(rid), str(nm), d)
                        full.setdefault(key, []).append(rec)
                        if o == "Account" and cat.person_kind(o, d) == "other" and person is not True:
                            companies.add(str(rid))
                            continue   # a client company: no first name
                        ws = key.split()
                        first.setdefault(ws[0], []).append(rec)
                        if len(ws) >= 2:
                            last.setdefault(ws[-1], []).append(rec)
                        if len(ws) >= 3:
                            fl.setdefault(f"{ws[0]} {ws[-1]}", []).append(rec)
                self._names_full, self._names_fl, self._names_first = full, fl, first
                self._names_last, self._company_ids = last, companies
        return self._names_full or {}, self._names_fl or {}, self._names_first or {}

    def _person_names(self, value: str, contains: bool = False) -> list[str]:
        """Person objects with a record of this full name (contains: also a first name of an Employee or User)."""
        try:
            full, _fl, first = self._name_index()
        except Exception:
            return []
        key = name_key(value)
        if not key:
            return []
        recs = full.get(key, [])
        if contains:
            recs = [r for r in recs if r[0] in ("Recruiter__c", "User")] or \
                [r for r in first.get(key, []) if r[0] in ("Recruiter__c", "User")]
        return list(dict.fromkeys(r[0] for r in recs))

    def _hint_vocab(self) -> set[str]:
        if self._vocab is None:
            cat = self.catalog
            v: set[str] = set()
            for spec in cat.objects.values():
                for t in (spec.label, spec.plural_label, split_api_name(spec.api)):
                    v.update(normalize(t).split())
                for f in spec.fields.values():
                    v.update(normalize(f.label).split())
                    v.update(normalize(split_api_name(f.api)).split())
            for key in cat.global_values:
                v.update(key.split())
            self._vocab = v
        return self._vocab

    def design_words(self) -> frozenset[str]:
        """Words of the org's design (RECORDS_SPEC §22.2), cached at the first use: the catalog vocabulary of the hints
        (object and field labels and API words, stored picklist values), every word of every loaded knowledge file and
        the function words of :data:`_HINT_STOPWORDS`. A full name made only of such words, or a first name alone,
        is a person only when the names call lists it (:class:`okrec.session.Triage`). Built from data, no list."""
        if self._design is None:
            words: set[str] = set(self._hint_vocab()) | set(_HINT_STOPWORDS)

            def walk(x: Any) -> None:
                if isinstance(x, str):
                    words.update(_q_tokens(x))
                elif isinstance(x, dict):
                    for k, v in x.items():
                        walk(k)
                        walk(v)
                elif isinstance(x, (list, tuple)):
                    for v in x:
                        walk(v)

            for kn in self.catalog.knowledge.values():
                walk(getattr(kn, "raw", None))
            self._design = frozenset(words)
        return self._design

    def _spot_people(self, question: str) -> list[tuple[str, list[tuple]]]:
        """Words of the question that name stored people: full names (6 to 2 words, longest first), then first names.
        ``[(words, [(object, Id, name, detail)])]``, at most :data:`MAX_PEOPLE`."""
        return [(d["words"], d["recs"]) for d in self._spot(question)]

    def _spot(self, question: str) -> list[dict]:
        """:meth:`_spot_people` with what kind of match each one is: ``{"words", "recs", "strength", "first_only",
        "asked", "close"}``. Full names first: word sequences of :data:`NAME_NGRAM_MAX` down to 2 words against the
        stored full names, then (2 words) against the first and last word of stored names of 3 or more words. Then
        first names of every person object (at least 3 characters, no function word, no catalog word; a trailing "s"
        is also tried without it). A full name held only by client companies (an Account that is no person) is no
        person. ``first_only``: only a first name matched. ``asked``: the question's first name plus
        the next word, when that word is a close but not exact spelling of a stored surname of the first-name matches
        ("Delta Persn" for a stored "Delta Person"); ``close`` holds those stored names (their records first in
        ``recs``). ``strength``: "strong" for 2 or more words of which one is no design word (:meth:`design_words`),
        else "weak" (a first name, or a name made only of design words). At most :data:`MAX_PEOPLE`, in question
        order.

        With the spelling help on (``catalog.resolver``), the close-surname step is replaced by the resolver
        (:func:`_spelling_spots`, between the full names and the first names): a misspelt name the resolver reads as
        a stored one (``corrected``) is a spot with ``asked`` (the question's words), ``close`` (the stored names; one
        word: the stored first name) and ``read_as`` (the stored spelling); its ``strength`` comes from the stored
        spelling's words. A did-you-mean (``ambiguous``) gives no spot."""
        full, fl, first = self._name_index()
        vocab = self._hint_vocab()
        toks = _q_tokens(question)   # "person?" -> "person": a name at the end of a question still matches
        used = [False] * len(toks)
        found: list[tuple[int, dict]] = []
        for n in range(min(NAME_NGRAM_MAX, len(toks)), 1, -1):
            for i in range(len(toks) - n + 1):
                if any(used[i:i + n]):
                    continue
                words = toks[i:i + n]
                if all(w in _HINT_STOPWORDS for w in words):
                    continue
                key = " ".join(words)
                recs = self._person_recs(key)   # client companies left out; 2 words: also first and last word
                if recs:
                    found.append((i, {"words": key, "recs": recs, "first_only": False, "asked": None, "close": []}))
                    for j in range(i, i + n):
                        used[j] = True
        speller = getattr(self.catalog, "resolver", None)
        if speller is not None:   # spelling help: misspelt names (in place of the close-surname step below)
            try:
                found += _spelling_spots(self, speller, question, toks, used, vocab)
            except Exception:  # noqa: BLE001 - optional: a failure leaves the exact spots as they are
                pass
        for i, tok in enumerate(toks):
            if used[i] or len(tok) < 3:
                continue
            for cand in (tok, tok[:-1] if tok.endswith("s") and len(tok) > 3 else None):
                if not cand or cand in _HINT_STOPWORDS or cand in vocab or len(cand) < 3:
                    continue
                recs = first.get(cand)
                if recs:
                    d = {"words": tok, "recs": recs, "first_only": True, "asked": None, "close": []}
                    nxt = toks[i + 1] if i + 1 < len(toks) and not used[i + 1] else ""
                    if speller is None and len(nxt) >= 3 and nxt not in _HINT_STOPWORDS:
                        close = [r for r in recs if _close_surname(nxt, name_key(r[2]).split()[1:])]
                        if close:   # a misspelled surname: the asked full name is not stored
                            kinds = {self.catalog.person_kind(r[0], r[3]) for r in close}
                            d.update({"words": f"{tok} {nxt}", "asked": f"{tok} {nxt}", "first_only": False,
                                      "close": list(dict.fromkeys(_disp(r[2]) for r in close)),
                                      "recs": close + [r for r in recs if r not in close and
                                                       self.catalog.person_kind(r[0], r[3]) in kinds]})
                            used[i + 1] = True
                    found.append((i, d))
                    used[i] = True
                    break
        found.sort(key=lambda x: x[0])
        out = []
        for _i, d in found[:_base.MAX_PEOPLE]:
            # a name the spelling help read: the stored spelling's words decide (a typo of a design word is none)
            ws = (name_key(d["read_as"]) if d.get("read_as") else d["words"]).split()
            d["strength"] = "strong" if len(ws) >= 2 and any(not self.is_design_word(w) for w in ws) else "weak"
            out.append(d)
        return out

    def is_design_word(self, word: str) -> bool:
        """``word`` (or the same word in singular or plural) is a word of the org's design (:meth:`design_words`)."""
        design = self.design_words()
        w = str(word or "")
        return w in design or (w + "s") in design or (w.endswith("s") and w[:-1] in design) or \
            (w.endswith("es") and w[:-2] in design) or (w.endswith("ies") and w[:-3] + "y" in design)

    def spot_people(self, question: str) -> list[dict]:
        """The people the question's words name, from the stored names (:meth:`_spot`): ``[{"words", "recs",
        "strength", "first_only", "asked", "close"}]``. Never raises ([] on any failure)."""
        try:
            return [dict(d) for d in self._spot(question)]
        except Exception:  # noqa: BLE001 - optional
            return []

    def _person_recs(self, key: str) -> list[tuple]:
        """People stored under the name ``key`` (:func:`name_key`): the records of that full name (no client
        company), and for 2 words also the records whose first and last word they are ("first last" of a longer
        name): a stored 'First Last' never hides a 'First Middle Last' person."""
        full, fl, _first = self._name_index()
        recs = [r for r in full.get(key) or [] if r[1] not in self._company_ids]
        if len(key.split()) == 2:
            recs = list(dict.fromkeys(recs + list(fl.get(key) or [])))
        return recs

    def _close_people(self, toks: list[str], objs: Any = None) -> list[tuple]:
        """Stored people whose name is the asked name of 2 or more words written another way, by general name rules
        (no word lists): the same words in another order, extra asked words (a title or a middle name: every word of
        the stored name is asked), fewer asked words (every asked word is in the stored name), a 1-letter word for a
        stored word's initial, a close spelling of a word (:func:`_close_word`). At least two words pair up, one of
        them exactly with 3 or more letters. Candidates are the people with an asked word as their first or last word
        (``objs``: only these objects). Records of at most 8 names."""
        _full, _fl, first = self._name_index()
        last = self._names_last
        key = " ".join(toks)
        cands: dict[str, list[tuple]] = {}
        for w in toks:
            if len(w) < 3 or w in _HINT_STOPWORDS:
                continue
            for r in list(first.get(w) or []) + list(last.get(w) or []):
                k = name_key(r[2])
                if k != key and (objs is None or r[0] in objs) and r not in cands.get(k, []):
                    cands.setdefault(k, []).append(r)
        out: list[tuple] = []
        n = 0
        for k, recs in sorted(cands.items(), key=lambda kv: min(self._order_key(r[0], r[3]) for r in kv[1])):
            if _same_name_words(toks, k.split()):
                out += recs
                n += 1
                if n >= 8:
                    break
        return out

    def _name_word(self, toks: list[str], objs: Any = None) -> tuple[str, list[tuple], str]:
        """The first word of an asked name of 2 or more words that is a stored first name (else a stored last name),
        has 3 or more letters and is no design word: ``(word, records, "first name" | "last name")``, or ("", [], "")
        (``objs``: only these objects)."""
        _full, _fl, first = self._name_index()
        for how, index in (("first name", first), ("last name", self._names_last)):
            for w in toks:
                if len(w) < 3 or w in _HINT_STOPWORDS or self.is_design_word(w):
                    continue
                recs = [r for r in index.get(w) or [] if objs is None or r[0] in objs]
                if recs:
                    return w, recs, how
        return "", [], ""

    def company_name(self, name: str) -> bool:
        """``name`` is the stored name of a client company (an Account that is no person) and of no person: no
        person and not a missing person either. Never raises."""
        try:
            full, _fl, _first = self._name_index()
            recs = full.get(" ".join(_q_tokens(name))) or []
            return bool(recs) and all(r[1] in self._company_ids for r in recs)
        except Exception:  # noqa: BLE001
            return False

    def name_index_complete(self) -> bool:
        """The person-name index read every person object to the end (no time budget or row cap stopped it): only
        then may a name that it lacks be called not stored. Never raises."""
        try:
            self._name_index()
            return not self._names_partial
        except Exception:  # noqa: BLE001
            return False

    def staff_close_name(self, name: str) -> bool:
        """A one-word name that no stored person has is a close spelling of the first name of a stored Employee or
        User (:func:`_close_word`; only staff names: the thousands of candidate and lead first names make most
        unknown words close to one). Never raises."""
        try:
            toks = _q_tokens(name)
            if len(toks) != 1 or len(toks[0]) < 4:
                return False
            _full, _fl, first = self._name_index()
            w = toks[0]
            speller = getattr(self.catalog, "resolver", None)
            if speller is not None:   # spelling help: the resolver's close spellings in place of _close_word
                return _staff_spelling(self, speller, w)
            return any(_close_word(w, k) and any(self.kind_of(r[0], r[3]) in ("employee", "user") for r in recs)
                       for k, recs in first.items())
        except Exception:  # noqa: BLE001 - optional
            return False

    def resolve_person_name(self, name: str) -> dict | None:
        """A name the names call returned (RECORDS_SPEC §22.3), as a :meth:`spot_people` entry with ``strength``
        "strong", ``from`` "names" and ``match``, or None when no stored person has it or a word of it. In order: the
        stored full name (no client company; 2 words also the first and last word of a longer name), a first name
        alone (any word that is no function word and has 2 or more characters: the catalog vocabulary does not apply,
        since the model read the word as a name), a last name alone; for 2 or more words the same name written another
        way (:meth:`_close_people`: ``asked`` is the question's name, ``close`` the stored names), else a word of it
        that is a stored first (or last) name (:meth:`_name_word`): ``partial`` holds that word and ``asked`` the
        question's name, which is not stored ("<first> <unknown word>": a title, an honorific or an unknown surname
        look the same). Never raises.

        With the spelling help on (``catalog.resolver``): a name the resolver reads as a stored one (``corrected``;
        one word: after the exact first and last name, only for doubled letters or an identical sound; 2 or more
        words: after :meth:`_close_people`) gives ``match`` "closest stored name" ("closest stored last name" for a
        last name alone) with ``asked``, ``close`` and ``read_as`` (the stored spelling); a name of 2 or more words
        the resolver finds only did-you-means of 2 or more words for (``ambiguous``) gives None (the triage lists it
        as missing; the hint line offers them)."""
        try:
            _full, _fl, first = self._name_index()
            toks = _q_tokens(name)
            key = " ".join(toks)
            if len(key) < 2:
                return None
            d = {"words": key, "recs": [], "strength": "strong", "first_only": False, "asked": None, "close": [],
                 "from": "names", "match": "full name"}
            recs = self._person_recs(key)
            if recs:
                d["recs"] = recs
                return d
            speller = getattr(self.catalog, "resolver", None)
            if len(toks) == 1:
                w = toks[0]
                if w in _HINT_STOPWORDS or len(w) < 2:
                    return None
                recs = first.get(w) or (first.get(w[:-1]) if w.endswith("s") and len(w) > 3 else None)
                how = "first name"
                if not recs and len(w) >= 3:
                    recs, how = self._names_last.get(w), "last name"
                if not recs and speller is not None:   # spelling help: doubled letters or an identical sound only
                    got = _person_reading(self, speller, name)
                    if got is not None:
                        stored, recs, how = got
                        d.update({"recs": list(recs), "first_only": True, "asked": key, "close": [stored],
                                  "read_as": stored, "match": "closest stored last name" if how == "last name"
                                  else "closest stored name"})
                        return d
                if not recs:
                    return None
                d.update({"recs": list(recs), "first_only": True, "match": how})
                return d
            close = self._close_people(toks)
            if close:
                d.update({"recs": close, "asked": key, "match": "closest stored name",
                          "close": list(dict.fromkeys(_disp(r[2]) for r in close))})
                return d
            if speller is not None:   # spelling help: one clear stored spelling, else a did-you-mean (not stored)
                got = _person_reading(self, speller, name)
                if got is not None:
                    stored, recs, _how = got
                    d.update({"recs": list(recs), "asked": key, "match": "closest stored name", "read_as": stored,
                              "close": list(dict.fromkeys(_disp(r[2]) for r in recs))})
                    return d
                if _full_name_suggestions(self, speller, name):
                    return None
            w, recs, how = self._name_word(toks)
            if not recs:
                return None
            d.update({"recs": list(recs), "first_only": True, "asked": key, "partial": w, "match": f"partial {how}"})
            return d
        except Exception:  # noqa: BLE001 - optional
            return None

    def _question_spots(self, question: str) -> list[dict]:
        """The people of the current question: those the session gave (``begin_question(people=...)``), else spotted
        here."""
        if self._given_people is not None and question == self._question:
            return [dict(p) for p in self._given_people]
        return self._spot(question)

    def question_people(self) -> list[dict]:
        """People named in the current question (``begin_question``), for the session's answer checks; never raises.
        ``[{"words", "names", "first_only", "asked"}]``: ``names`` are the distinct stored full names that fit (the
        close spellings first when ``asked`` is set; Employees, then Users, candidates and leads, at most 8).
        ``first_only``: only a first name matched, so ``names`` are different people unless there is one. ``asked``:
        the question's full name, which is not stored (a close spelling of the names). ``read_as`` (only with the
        spelling help on, only when it read the words): the stored spelling the asked words were read as."""
        try:
            out = []
            for d in self._question_spots(self._question):
                recs = sorted(d["recs"], key=lambda r: (0 if _disp(r[2]) in d["close"] else 1,
                                                        _PERSON_ORDER.get((r[0], self.catalog.person_kind(r[0], r[3])), 9)))
                # one name per person: records linked to one another (an employee's user and account, a lead converted
                # to the candidate's account) are one person, whatever their names
                try:
                    ident = self._identities(None, [(r[0], r[1]) for r in recs if r[0] in PERSON_OBJECTS][:45],
                                             deadline=time.monotonic() + 0.5)
                except RecordError:
                    ident = {}
                seen: set[str] = set()
                one: list[tuple] = []
                for r in recs:
                    anchor = (ident.get(r[1]) or (None, []))[0]
                    key = str(anchor[1]) if anchor is not None else str(r[1])
                    if key not in seen:
                        seen.add(key)
                        one.append(r)
                names = list(dict.fromkeys(" ".join(str(r[2]).replace("\xa0", " ").split()) for r in one))[:8]
                person = {"words": d["words"], "names": names, "first_only": d["first_only"], "asked": d["asked"]}
                if d.get("read_as"):   # the spelling help read the asked words as this stored spelling (never off)
                    person["read_as"] = d["read_as"]
                out.append(person)
            return out
        except Exception:  # noqa: BLE001 - optional
            return []

    def question_objects(self) -> list[dict]:
        """Objects the current question's counted noun can mean for a person it names (from the hint-time reference
        scan of :meth:`object_hints`; [] before it ran or when nothing is ambiguous). Never raises. ``[{"name", "word",
        "objects": [{"object", "label", "refs": [{"field", "role", "records"}]}]}]``: the question names no role word
        for the person, ``word`` (unqualified in the question) is the last word of the label of every listed object,
        and the person is referenced on each of them."""
        try:
            return [dict(x) for x in self._noun_refs]
        except Exception:  # noqa: BLE001 - optional
            return []

    def _noun_objects(self, question: str, persons: list[dict]) -> list[dict]:
        """:meth:`question_objects` for the people of the hints (``question`` without their name words). A word of the
        question is the counted noun of an object when it is the last word of the object's label or plural label
        (singular or plural); it is ambiguous when two or more objects that reference the person share it and are
        documented as the same kind of record (:data:`SAME_NOUN_OBJECTS`; "sessions" for Session__c and
        Candidate_Portal_Session__c is no such case), no occurrence of it in the question has a qualifier before it (a
        word that is no function word: "internal interviews", "client interviews", "mock interviews") and the question
        has no role word for the person's kind (with a role word the lookup roles decide)."""
        cat = self.catalog
        toks = _q_tokens(question)
        out: list[dict] = []
        for p in persons:
            agg = p.get("agg") or {}
            if not agg or p.get("kind") not in ("employee", "candidate") or cat.question_cues(question, p["kind"]):
                continue
            by_obj: dict[str, list[tuple[str, int]]] = {}
            for (c, via), n in sorted(agg.items(), key=lambda kv: -kv[1]):
                by_obj.setdefault(c, []).append((via, n))
            by_head: dict[str, list[str]] = {}   # label head word -> objects whose label ends with it
            for c in by_obj:
                spec = cat.objects.get(c)
                if spec is None or not spec.has_table:
                    continue
                for t in (spec.label, spec.plural_label):
                    ws = re.findall(r"[a-z0-9]+", normalize(t))
                    if not ws:
                        continue
                    head = next((h for h in by_head if _same_noun(h, ws[-1])), ws[-1])
                    if c not in by_head.setdefault(head, []):
                        by_head[head].append(c)
            for head, objs in by_head.items():
                objs = [c for c in objs
                        if any(c in g and sum(1 for o in objs if o in g) >= 2 for g in SAME_NOUN_OBJECTS)]
                spots = [i for i, tok in enumerate(toks) if _same_noun(tok, head)]
                if len(objs) < 2 or not spots or any(i > 0 and _qualifies(toks[i - 1]) for i in spots):
                    continue   # not in the question, or narrowed somewhere ("internal interviews")
                counted = [i for i in spots if i + 1 >= len(toks) or not _qualifies(toks[i + 1])]
                if not counted:
                    continue   # only as a modifier of another noun ("interview readiness person")
                if self._narrowed(toks, head, objs):
                    continue   # another word of the question names only one of them ("... in mocks")
                word = toks[counted[0]]
                out.append({"name": _disp(p["main"][2]), "word": word,
                            "objects": [{"object": c, "label": cat.label_of(c),
                                         "refs": [{"field": v, "role": _hint_role(cat.objects[c].fields[v]), "records": n}
                                                  for v, n in by_obj[c][:3]]} for c in objs[:4]]})
        return out

    def _narrowed(self, toks: list[str], head: str, objs: list[str]) -> bool:
        """True when a question word other than ``head`` (3 or more letters, no function word) matches the knowledge
        of exactly one of ``objs`` (the object search finds it for that word alone, and not the others): that word
        narrows the noun to one object, as a qualifier before it would."""
        for w in dict.fromkeys(toks):
            if len(w) < 3 or not _qualifies(w) or _same_noun(w, head):
                continue
            hits = {s.api for s, score, _m in self.catalog.search_objects(w, k=len(self.catalog.objects)) if score > 0}
            if sum(1 for o in objs if o in hits) == 1:
                return True
        return False

    def _people_hints(self, question: str, top: list[str], missing: Any = ()) -> list[str]:
        """The People, Where referenced and Other lookups sections of the hints (empty when no person is named).
        ``missing``: names the session found in the question that no stored person has (RECORDS_SPEC §22.7)."""
        cat = self.catalog
        spots = self._question_spots(question)
        missing = [str(m) for m in missing or () if str(m or "").strip()]
        if not spots and not missing:
            return []
        spotted = [(d["words"], d["recs"]) for d in spots]
        spot_of = {d["words"]: d for d in spots}
        names = [w for w, recs in spotted] + [r[2] for _w, recs in spotted for r in recs]
        question = _without_words(question, names)   # a name is never a role word
        links = {(lk.src, lk.col) for lk in cat.links}
        deadline = time.monotonic() + _base.SCAN_BUDGET_S
        people_lines: list[str] = []
        where_lines: list[str] = []
        where_other: list[str] = []   # different records with the same name: after the instruction lines (cut first)
        persons: list[dict] = []
        held: set[tuple[str, str]] = set()
        fit_words: set[str] = set()   # question words that made a "Where referenced" lookup fit
        fit_objs: dict[str, list[str]] = {}   # such a word -> the objects of the lookups it made fit
        kind_fits: dict[str, dict[str, list[tuple[dict, str, int, str]]]] = {}   # word -> kind -> (person, lookup, n, target)
        for words, recs in spotted:
            d = spot_of.get(words) or {}
            w1 = str(d.get("partial") or words)   # the stored first (or last) name of a name that is not stored
            if d.get("read_as"):   # spelling help: the stored spelling read for the words, stated first
                if d.get("first_only"):
                    w1 = name_key(d["read_as"])
                people_lines.append(_read_as_line(words, d.get("close") or [d["read_as"]]))
            which = "last" if str(d.get("match") or "").endswith("last name") else "first"
            if d.get("partial"):
                people_lines.append(f"- '{words}' is not a stored name; '{w1}' is a stored {which} name of the people "
                                    f"below: one of them may be meant, or nobody stored is. If you answer for one of "
                                    f"them, say that no record is named '{words}'.")
            ordered = sorted(recs, key=lambda r: (0 if _disp(r[2]) in (d.get("close") or ()) else 1,
                                                  self._order_key(r[0], r[3]), str(r[1])))
            capped, beyond = ordered[:45], ordered[45:]
            try:   # bounded by the hint deadline: past it, every record is its own person
                ident = self._identities(None, [(r[0], r[1]) for r in capped if r[0] in PERSON_OBJECTS],
                                         deadline=deadline)
            except RecordError:
                ident = {}
            groups: dict[str, dict] = {}
            for o, rid, nm, det in capped:
                anchor, members = ident.get(rid, (None, []))
                if anchor is not None and not anchor[2] and anchor[1] == rid:
                    anchor = (o, rid, nm, det)
                key = anchor[1] if anchor is not None else rid
                g = groups.get(key)
                if g is None:
                    main = anchor if anchor is not None else (o, rid, nm, det)
                    g = groups[key] = {"main": main, "records": [(main[0], main[1])], "linked": list(members),
                                       "kind": cat.person_kind(main[0], main[3])}
                    for x in members:
                        g["records"].append((x[1], x[2]))
                if (o, rid) not in g["records"]:
                    g["records"].append((o, rid))
            for g in groups.values():   # C5: no employee in the group: the candidate Account before the Lead
                cands = [g["main"]] + [(x[1], x[2], x[3], x[4]) for x in g["linked"]]
                best = min(cands, key=lambda r: (self._order_key(r[0], r[3]), cands.index(r)))
                if best[1] != g["main"][1]:
                    old = g["main"]
                    g["linked"] = [(None, old[0], old[1], old[2], old[3])] + [x for x in g["linked"] if x[2] != best[1]]
                    g["main"], g["kind"] = best, cat.person_kind(best[0], best[3])
            ppl = list(groups.values())
            more = 0
            if d.get("first_only") and len(ppl) + len(beyond) > MANY_PEOPLE:
                # A first name of many people: one compact line, no reference scan (about_people lists them).
                kinds: dict[str, int] = {}
                for k in [self._person_of(p["main"][0], p["main"][3]) for p in ppl] + \
                        [self._person_of(r[0], r[3]) for r in beyond]:
                    kinds[k] = kinds.get(k, 0) + 1
                plural = {"employee": "employees", "user": "users", "candidate": "candidates", "lead": "leads",
                          "other account": "other accounts", "record": "records"}
                people_lines.append(f"- '{w1}': {which} name of {len(ppl) + len(beyond)} stored people ("
                                    + ", ".join(f"{n} {plural.get(k, k)}" for k, n in
                                                sorted(kinds.items(), key=lambda kv: list(plural).index(kv[0])
                                                       if kv[0] in plural else 9))
                                    + "); about_people lists them")
                continue
            # the matched records' own names (a login linked to another employee keeps the name the question matched)
            pnames = list(dict.fromkeys(_disp(r[2]) for r in sorted(
                recs, key=lambda r: _PERSON_ORDER.get((r[0], cat.person_kind(r[0], r[3])), 9))))
            if d.get("partial"):
                # a name that is not stored: the people who share its word are listed, never counted for it
                people_lines.append(f"- stored people with the {which} name '{w1}': "
                                    + ", ".join(f"'{n}'" for n in pnames[:5]) + (", and more" if len(pnames) > 5 else "")
                                    + f". None of them is named '{words}': never give a count for '{words}'.")
                if len(ppl) > 3:
                    continue
            elif d.get("first_only") and len({norm_name(n) for n in pnames}) >= 2:
                # A first name of several people: never pick one silently (one query can count each of them).
                people_lines.append(f"- '{w1}' is only a {which} name, and several people have it: "
                                    + ", ".join(f"'{n}'" for n in pnames[:5]) + (", and more" if len(pnames) > 5 else "")
                                    + ". Answer for each of them (one query: filter the lookup with op in and these full "
                                    "names, group_by that lookup) or ask which one; never answer for one of them "
                                    "without naming the others (about_people lists who each is).")
                if len(ppl) > 3:
                    continue
            elif len(ppl) > 3 and len(words.split()) < 2 and len({norm_name(n) for n in pnames}) >= 2:
                # a first name shared by many records with different names: no scan (one stored name shared by
                # several records reads like a full name: the employee first, below)
                people_lines.append(f"- '{words}': several records with this name; call about_people")
                continue
            if d.get("asked") and d.get("close") and not d.get("read_as"):
                people_lines.append(f"- '{words}' is not a stored name; the closest stored name is "
                                    + " or ".join(f"'{_disp(n)}'" for n in d["close"][:2])
                                    + f". If you answer for it, say that no record is named '{words}' and which stored "
                                    "name you used.")
            if len(ppl) > 3:
                # A full name: the employees first, then users, candidates and leads; the rest is only mentioned.
                ppl.sort(key=lambda p: _PERSON_ORDER.get((p["main"][0], p["kind"]), 9))
                more = len(ppl) - 3
                ppl = ppl[:3]
            descs: list[str] = []
            by_name: dict[tuple[str, str], list[dict]] = {}
            for p in ppl:
                by_name.setdefault((p["main"][0], norm_name(p["main"][2])), []).append(p)
            first = True
            for (o, _nv), plist in by_name.items():
                tt = cat.target_text(o)
                extra = "" if first else ", a different record"
                if len(plist) > 1:
                    dets = ", ".join(str(p["main"][3]) for p in plist if p["main"][3]) or "no detail"
                    descs.append(f"{tt} '{_disp(plist[0]['main'][2])}': several records with this name ({dets}{extra})")
                else:
                    p = plist[0]
                    det = str(p["main"][3] or "")
                    link = ""
                    if p["linked"]:
                        link = "; linked: " + ", ".join(f"{cat.label_of(x[1])} '{_disp(x[3])}'" + (f" ({x[4]})" if x[4] else "")
                                                        for x in p["linked"][:4])
                    inner = ", ".join(x for x in (det, extra.lstrip(", ")) if x)
                    descs.append(f"{tt} '{_disp(p['main'][2])}'" + (f" ({inner})" if inner else "") + link)
                for p in plist:
                    p["different"] = not first
                first = False
            if more:
                descs.append(f"and {'one more record' if more == 1 else 'more records'} with this name (call "
                             "about_people)")
            people_lines.append(f"- '{words}': " + "; ".join(descs))
            for p in ppl:
                p["words"] = words
                persons.append(p)
        asked_words = list(dict.fromkeys([w for w, _r in spotted] + missing))
        speller = getattr(cat, "resolver", None)
        for m in missing:
            line = (f"- '{m}': no stored employee, user, candidate or lead has this name (about_people gives "
                    "the closest stored names)")
            if speller is not None:   # spelling help: the resolver's did-you-means, never used as the name
                line += _did_you_mean_clause(self, speller, m)
            people_lines.append(line)
        if len(asked_words) >= 2:
            people_lines.insert(0, "Two or more people are named: about_people with names ["
                                + ", ".join(asked_words[:_base.MAX_PEOPLE]) + "] gives who each is and every stored record "
                                "that links them.")
        if not persons and not people_lines:
            return []
        for p in persons:
            if time.monotonic() > deadline:
                break
            pairs = self._child_pairs(p["records"], system=False)
            try:
                counts = self._scan(None, pairs, deadline=deadline)
            except RecordError:
                continue
            agg: dict[tuple[str, str], int] = {}
            for (c, via, _i), n in counts.items():
                if n > 0:
                    agg[(c, via)] = agg.get((c, via), 0) + n
            held |= set(agg)
            ranked: list[tuple[int, int, int, str, str]] = []
            fits_of = {(c, via): cat.role_fits(question, cat.objects[c], cat.objects[c].fields[via], p["kind"])
                       for (c, via) in agg}
            for (c, via), n in agg.items():
                cs = cat.objects[c]
                g = cs.fields[via]
                fit = fits_of[(c, via)]
                if fit:
                    fit_words.add(fit)
                    kind_fits.setdefault(fit, {}).setdefault(p["kind"], []).append(
                        (p, f"{c}.{via}", n, cat.target_of(g) or ""))
                text = f"{c}.{via} ({_hint_role(g)}{_copy_label(g)})" + (f" <- fits '{fit}'" if fit else "")
                # a copy of a child object's lookup comes right after its source when the source fits a question word
                # too (moved below, after the sort)
                after = f"{g.child_copy[0]}.{g.child_copy[2]}" if (
                    g.child_copy and fit and fits_of.get((g.child_copy[0], g.child_copy[2]))) else ""
                ranked.append((0 if fit else 1, 0 if c in top else 1, -n, f"{c}.{via}", text, fit or "", after))
            if self._system_cue(question) and any(o == "User" for o, _i in p["records"]):
                # the question's verb is one of a system lookup's (own, create, modify): the person's user records
                # that such a lookup holds where its role fits, after the business lookups of the same fit
                spairs = [k for k in self._child_pairs(p["records"], system=True) if k[1] in SYSTEM_LOOKUPS]
                try:
                    scounts = self._scan(None, spairs, deadline=deadline)
                except RecordError:
                    scounts = {}
                sagg: dict[tuple[str, str], int] = {}
                for (c, via, _i), n in scounts.items():
                    if n > 0:
                        sagg[(c, via)] = sagg.get((c, via), 0) + n
                for (c, via), n in sagg.items():
                    cs = cat.objects[c]
                    fit = cat.role_fits(question, cs, cs.fields[via], p["kind"])
                    if fit:
                        held.add((c, via))
                        ranked.append((0, 0 if c in top else 1, -n, f"{c}.{via}",
                                       f"{c}.{via} ({_hint_role(cs.fields[via])}; system) <- fits '{fit}'", fit, ""))
            ranked.sort()
            for r in [x for x in ranked if x[6] and any(y[3] == x[6] for y in ranked)]:
                ranked.remove(r)   # the copy right after its source (both fit; the source is the record of the role)
                ranked.insert(next(i for i, y in enumerate(ranked) if y[3] == r[6]) + 1, r)
            p["agg"] = agg
            for r in ranked:   # in the order shown: fitting lookups first, hinted objects, then by records
                if r[5]:
                    fit_objs.setdefault(r[5], []).append(r[3].split(".", 1)[0])
            items = [(r[0], r[4]) for r in ranked[:6]]
            if p.get("different"):   # a different record with the same name: its fitting lookups first, short (the
                items = [x for x in items if x[0] == 0][:3] or items[:2]   # line comes last and must fit the hints)
            p["children"] = [k for k, _n in sorted(agg.items(), key=lambda kv: -kv[1])]
            diff = ", a different record" if p.get("different") else ""
            head = f"- '{_disp(p['main'][2])}' ({cat.label_of(p['main'][0])}{diff}): "
            (where_other if p.get("different") else where_lines).append(
                head + ("; ".join(t for _k, t in items) if items else "no business record references this person; who "
                        "they are is on their own record (about_people)"))
        objs: list[str] = []
        for p in persons:
            objs += [c for c, _v in p.get("children", [])]
        # The hinted objects that hold the people first, then the other hinted objects, then where they are held.
        objs = [o for o in dict.fromkeys([o for o in top if o in objs] + list(top) + objs)
                if o in cat.objects and cat.objects[o].has_table][:4]
        pobjs: list[str] = []
        kind_of: dict[str, str] = {}
        for p in persons:
            for o, _i in p["records"]:
                if o not in pobjs:
                    pobjs.append(o)
                kind_of.setdefault(o, p["kind"])
        other_lines: list[str] = []
        unsynced_lines: list[str] = []
        for o in objs:
            spec = cat.objects[o]
            for x in cat.not_synced_fits(question, spec, kind_of)[:1]:
                unsynced_lines.append(f"- {o}.{x['field']} ({x['role']}) fits '{x['fits']}' but is documented only, not "
                                      "in the warehouse: the data does not record it. Say so first (never a zero or none); "
                                      "a "
                                      "count through another lookup or path is only a proxy: call it one.")
            looks = cat.person_lookups(spec)
            for po in pobjs:
                lst = [g for g in looks.get(po, []) if g.filled > 0 and (o, g.api) not in held and (o, g.api) not in links][:8]
                if not lst:
                    continue
                items = []
                for g in lst:
                    fit = cat.role_fits(question, spec, g, kind_of.get(po, "other"))
                    if cat.almost_empty(spec, g):
                        t = f"{g.api} (almost empty)"
                    elif g.legacy:
                        t = f"{g.api} (legacy; {_hint_role(g)}{_copy_label(g)})"
                    else:
                        t = f"{g.api} ({_hint_role(g)}{_copy_label(g)})"
                    # a copy of a child object's lookup after the other lookups the same word fits
                    items.append(((0 if fit else 1) + (0.5 if g.child_copy else 0),
                                  t + (f" <- fits '{fit}'" if fit else "")))
                items.sort(key=lambda x: x[0])
                other_lines.append(f"- {o} -> {cat.label_of(po)}: " + "; ".join(t for _k, t in items))
            if self._system_cue(question):
                sl = [spec.fields[s] for s in SYSTEM_LOOKUPS if s in spec.fields and spec.fields[s].filled > 0
                      and (o, s) not in held]
                if sl:
                    kind = kind_of.get("User", "other")
                    parts = []
                    for g in sl:
                        fit = cat.role_fits(question, spec, g, kind) if "User" in kind_of else None
                        parts.append(f"{g.api} ({g.role})" + (f" <- fits '{fit}'" if fit else ""))
                    other_lines.append(f"- {o} -> User (system): " + ", ".join(parts))
        out = ["People named in the question (code matched these words to stored names; filter with the full name):"]
        out += [_cut_text(x, 400) for x in people_lines]
        try:
            self._noun_refs = self._noun_objects(question, persons)
        except Exception:  # noqa: BLE001 - optional
            self._noun_refs = []
        if where_lines or where_other:
            out.append("Where these people are referenced (lookups holding them on at least one record; pick the one "
                       "whose role fits the question):")
            out += [_cut_text(x, 400) for x in where_lines]
            for x in self._noun_refs[:2]:
                objs = " and ".join(f"{o['object']} ({o['label']})" for o in x["objects"])
                out.append(_cut_text(f"'{x['word']}' (no qualifier, no role word) fits {objs}, and '{x['name']}' is "
                                     "referenced on each: count each object with its own lookup and give each count "
                                     "on its own (never add them).", 300))
            out += self._both_kinds_lines(kind_fits)
            ambiguous = sorted(w for w in fit_words if w.lower().startswith(_COMPLETION_CUES))
            if ambiguous:
                named = []   # name the status field of the objects the verb fits (saves a describe_fields round)
                for o in list(dict.fromkeys(fit_objs.get(ambiguous[0], [])))[:1]:   # the top fitting object
                    st_f = self._status_field(cat.objects[o])
                    if st_f is not None and not _digits_in(st_f.api + o):
                        named.append(f"{st_f.api} on {o}")
                which = f" ({'; '.join(named)})" if named else ""
                out.append(f"'{ambiguous[0]}' can mean every record the person is assigned to or only the completed "
                           f"ones: in the same query keep the question's group_by and add split_by the counted object's "
                           f"status field{which} (group_by it when the question asks for no grouping), then give both "
                           "numbers.")
            # different records with the same name: the ones with a lookup that fits the question first (the hint
            # limit cuts from the end)
            out += [_cut_text(x, 400) for x in sorted(where_other, key=lambda x: 0 if "<- fits" in x else 1)]
        if unsynced_lines:
            out.append("Lookups the question's words fit that are not in the warehouse:")
            out += [_cut_text(x, 300) for x in unsynced_lines[:2]]
        if other_lines:
            out.append("Other lookups to these people's objects on the same and the hinted objects (none of them holds "
                       "the people above):")
            out += [_cut_text(x, 300) for x in other_lines]
        while len("\n".join(out)) > HINT_MAX_CHARS and len(out) > 2:
            out.pop()
            if out[-1].endswith(":") and not out[-1].startswith("-"):
                out.pop()
        return out

    def _both_kinds_lines(self, kind_fits: dict[str, dict[str, list[tuple[dict, str, int, str]]]]) -> list[str]:
        """One line per question word that fits lookups of an Employee (or User) AND of a different candidate record
        with the same name ("take": the employee as interviewer, the candidate record as candidate): the employee is
        meant unless the question says candidate, and the two counts are different records, never added."""
        cat = self.catalog
        out: list[str] = []
        for word, kinds in kind_fits.items():
            emp, cand = kinds.get("employee") or [], kinds.get("candidate") or []
            pairs = [(e, c) for e in emp for c in cand
                     if e[0] is not c[0] and norm_name(e[0]["main"][2]) == norm_name(c[0]["main"][2])]
            if not pairs:
                continue
            # the candidate lookup on the same object as the employee's first (both on the interview object), then a
            # lookup to the candidate's own main object (not a linked Lead), then the one with the most records
            pairs.sort(key=lambda pc: (0 if pc[0][1].split(".", 1)[0] == pc[1][1].split(".", 1)[0] else 1,
                                       0 if pc[1][3] == pc[1][0]["main"][0] else 1, -pc[1][2], -pc[0][2]))
            (pe, le, _ne, _te), (pc, lc, _nc, tc) = pairs[0]
            via = f", a {cat.label_of(tc)} lookup" if tc and tc != pc["main"][0] else ""
            out.append(f"'{word}' fits both the {cat.label_of(pe['main'][0])} '{_disp(pe['main'][2])}' ({le}) and a "
                       f"different {cat.label_of(pc['main'][0])} record with the same name ({lc}{via}): unless the question "
                       f"says candidate, answer for the {cat.label_of(pe['main'][0])} with {le.split('.', 1)[1]}, then "
                       "give the other record's count on its own as a different record with the same name; never add "
                       "them.")
            if len(out) >= 2:
                break
        return out

    @staticmethod
    def _system_cue(question: str) -> bool:
        """A question word starting with creat, modif or own that is not directly followed by a time phrase."""
        toks = normalize(question).split()
        for i, t in enumerate(toks):
            if t.startswith(("creat", "modif", "own")):
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if nxt in _TIME_WORDS or re.fullmatch(r"\d{4}", nxt or ""):
                    continue
                return True
        return False


def _digits_in(text: str) -> bool:
    """True when ``text`` holds a digit (the hints never show digits)."""
    return any(ch.isdigit() for ch in text)


def _hint_note(api: str) -> str:
    note = OBJECT_NOTES.get(api, "")
    return re.sub(r" \(RecordTypeId DeveloperName PersonAccount, \{person\} rows in this snapshot\)",
                  " (RecordTypeId DeveloperName PersonAccount)", note)


def _copy_label(f: FieldSpec) -> str:
    """"; partial copy of <C>.<L>" for a lookup that copies a lookup of a child object (``FieldSpec.child_copy``),
    else ''."""
    if not f.child_copy:
        return ""
    return ("; partial copy of " if f.copy_source_filled > f.filled else "; copy of ") + \
        f"{f.child_copy[0]}.{f.child_copy[2]}"


def _hint_role(f: FieldSpec) -> str:
    """Role text for the hints: no digits, at most 60 characters."""
    return _short(re.sub(r"\s+", " ", re.sub(r"\d+", "", f.role_text())).strip(), 60)


def _same_noun(a: str, b: str) -> bool:
    """True when two words are the same noun in singular or plural ("interview", "interviews"; "entry", "entries")."""
    if not a or not b:
        return False
    if a == b or a == b + "s" or b == a + "s" or a == b + "es" or b == a + "es":
        return True
    return (a.endswith("ies") and b.endswith("y") and a[:-3] == b[:-1]) or \
        (b.endswith("ies") and a.endswith("y") and b[:-3] == a[:-1])


def _qualifies(word: str) -> bool:
    """True when a question word right before a noun narrows it ("internal", "client", "mock"): any word that is no
    English function word, no number and no possessive ("x's")."""
    w = str(word or "").strip("'\u2019")
    return bool(w) and w not in _HINT_STOPWORDS and not w.isdigit() and not re.search(r"['\u2019]s$", w)


def _close_word(a: str, b: str) -> bool:
    """Two different words of 4 or more letters with the same first letter that are close spellings: similarity at
    least 0.8, or the same letters in another order ("jian" / "jain")."""
    if a == b or len(a) < 4 or len(b) < 4 or a[0] != b[0]:
        return False
    import difflib
    return sorted(a) == sorted(b) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _same_name_words(asked: list[str], stored: list[str]) -> bool:
    """The asked name and a stored name are one name written another way (:meth:`RecordTools._close_people`): words
    pair up exactly, then a 1-letter word with a word of that initial, then close spellings (:func:`_close_word`); at
    least two pairs, one of them exact with 3 or more letters, and every asked word is paired, or every stored word is
    paired without an initial."""
    used: set[int] = set()
    pairs = exact = 0
    initial = False
    rest: list[str] = []
    for a in asked:
        j = next((j for j, w in enumerate(stored) if j not in used and w == a), None)
        if j is None:
            rest.append(a)
            continue
        used.add(j)
        pairs += 1
        exact += len(a) >= 3
    left = 0
    for a in rest:
        j = next((j for j, w in enumerate(stored) if j not in used and
                  ((len(a) == 1 and w[:1] == a) or (len(w) == 1 and a[:1] == w) or _close_word(a, w))), None)
        if j is None:
            left += 1
            continue
        initial = initial or len(a) == 1 or len(stored[j]) == 1
        used.add(j)
        pairs += 1
    # every asked word paired (an abbreviation: initials count), or every stored word paired by a full word (extra
    # asked words: a title or a middle name; an initial never explains a stored word then)
    return pairs >= 2 and exact >= 1 and (left == 0 or (len(used) == len(stored) and not initial))


# ------------------------------------------------------------- spelling help (docs/resolver_integration.md, step (5))
# Used only when ``catalog.resolver`` is set (SFA_RESOLVER=on). The resolver (sf_assistant.knowledge.resolver) is
# passed in, never imported here: with the switch off no resolver module is loaded.
SPELLING_SUGGESTIONS = 3   # did-you-mean names in the hint line of a name no stored person has


def _person_term(resolver: Any, text: str) -> Any:
    """``resolver.resolve_term(text, kinds=["person"])``, or None when it fails (the spelling help is optional: a
    failure reads as no close stored name)."""
    try:
        return resolver.resolve_term(text, kinds=["person"])
    except Exception:  # noqa: BLE001
        return None


def _one_name_slip(resolver: Any, typed: str, stored: str) -> bool:
    """A single first or last name may be read as another only for doubled letters or an identical sound (resolver.md
    "Decisions", single name: a correctly spelt name of someone who is not stored is usually one edit from a stored
    name). The resolver already decides so; this keeps the rule at the call site too."""
    from sf_assistant.knowledge.resolver import dedup, phonetic   # loaded with the resolver itself

    a, b = resolver.key(typed, person=True), resolver.key(stored, person=True)
    if not a or not b or a == b or " " in a or " " in b:
        return False
    return dedup(a) == dedup(b) or phonetic(a) == phonetic(b)


def _stored_people(tools: Any, spelling: str, objs: Any = None) -> tuple[list[tuple], str]:
    """The records of the name index stored under one of the resolver's spellings, by name (the resolver's own
    ``person:Obj/Id`` targets come from its own read, which leaves out test, demo, non-human and digit names): 2 or more
    words, the full name (no client company; 2 words also the first and last word of a longer name, as
    :meth:`NamesMixin._person_recs`); one word, the first name, else the last name. ``(records, "full name" | "first
    name" | "last name")``; ``objs``: only records of these objects."""
    key = name_key(spelling)
    ws = key.split()
    if not ws:
        return [], ""
    try:
        _full, _fl, first = tools._name_index()
    except Exception:  # noqa: BLE001 - no index: nothing to map the spelling to
        return [], ""

    def keep(recs: Any) -> list[tuple]:
        return [r for r in recs or [] if objs is None or r[0] in objs]

    if len(ws) >= 2:
        return keep(tools._person_recs(key)), "full name"
    recs = keep(first.get(key))
    if recs:
        return recs, "first name"
    return keep(tools._names_last.get(key)), "last name"


def _person_reading(tools: Any, resolver: Any, text: str, objs: Any = None) -> tuple[str, list[tuple], str] | None:
    """The stored person the resolver reads ``text`` as: ``resolve_term(text, kinds=["person"])`` decided
    ``corrected`` and its spelling is in the name index (:func:`_stored_people`, ``objs``: only these objects).
    ``(stored spelling, records, how)`` or None. A single name is read only for doubled letters or an identical sound
    (:func:`_one_name_slip`), and only from a single word: words around a single name (a title, an unknown surname)
    are the partial-name case, never a correction."""
    res = _person_term(resolver, text)
    if res is None or res.decision != "corrected" or not res.suggestion:
        return None
    if len(name_key(res.suggestion).split()) == 1:
        typed = name_key(text).split()
        if len(typed) != 1 or not _one_name_slip(resolver, typed[0], res.suggestion):
            return None
    recs, how = _stored_people(tools, res.suggestion, objs)
    if not recs:
        return None
    return _disp(res.suggestion), recs, how


def _suggested_people(tools: Any, resolver: Any, text: str, objs: Any = None,
                      decisions: tuple[str, ...] = ("corrected", "ambiguous")) -> list[tuple[str, list[tuple]]]:
    """The resolver's spellings for a name no stored person has (``resolve_term(text, kinds=["person"])`` with one of
    ``decisions``), each with its stored records (:func:`_stored_people`, ``objs``: only these objects), in the
    resolver's order: ``[(stored spelling, records)]``; spellings the name index lacks are left out."""
    res = _person_term(resolver, text)
    if res is None or res.decision not in decisions:
        return []
    out: list[tuple[str, list[tuple]]] = []
    for s in res.suggestions:
        recs, _how = _stored_people(tools, s, objs)
        if recs and all(name_key(s) != name_key(x) for x, _r in out):
            out.append((_disp(s), sorted(recs, key=lambda r: (tools._order_key(r[0], r[3]), str(r[1])))))
    return out


def _suggested_names(tools: Any, resolver: Any, text: str, objs: Any = None, limit: int = 8) -> list[str]:
    """:func:`_suggested_people` as stored full names for a not-found list ("Closest stored names"): the records'
    own names, the resolver's best spelling first, at most ``limit``."""
    out: list[str] = []
    for _s, recs in _suggested_people(tools, resolver, text, objs):
        for r in recs:
            d = _disp(r[2])
            if d and d not in out:
                out.append(d)
    return out[:limit]


def _full_name_suggestions(tools: Any, resolver: Any, text: str) -> list[str]:
    """The resolver's did-you-means of 2 or more words for ``text`` (``ambiguous``) that the name index holds: a name
    close to stored full names that is none of them (not stored; the triage lists it as missing)."""
    return [s for s, _r in _suggested_people(tools, resolver, text, decisions=("ambiguous",)) if len(s.split()) >= 2]


def _did_you_mean_clause(tools: Any, resolver: Any, name: str) -> str:
    """'; did you mean ...?' for the hint line of a name no stored person has, or '' when the resolver has no
    did-you-mean: a spelling of 2 or more words, or a single name held by one person, as the stored full names; a
    single name of several people as "'X' (first name of N stored people)". The name is never used for the asked
    one: the agent asks, or says first which stored name it used."""
    items: list[str] = []
    for s, recs in _suggested_people(tools, resolver, name):
        full = list(dict.fromkeys(_disp(r[2]) for r in recs))
        if len(s.split()) >= 2 or len(full) == 1:
            items += [f"'{x}'" for x in full if f"'{x}'" not in items]
        else:
            which = "first" if tools._names_first and name_key(s) in tools._names_first else "last"
            items.append(f"'{s}' ({which} name of {len(full)} stored people)")
    items = items[:SPELLING_SUGGESTIONS]
    if not items:
        return ""
    said = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]
    return (f"; did you mean {said}? Never answer for a did-you-mean name as if it were '{name}': ask which one is "
            f"meant, or say first that no record is named '{name}' and which stored name you used")


def _read_as_line(words: str, close: list[str]) -> str:
    """The hint line of a name read as a stored one (the line of a close spelling, stated first)."""
    return (f"- '{words}' is not a stored name; the closest stored name is "
            + " or ".join(f"'{_disp(n)}'" for n in close[:2])
            + f". If you answer for it, say that no record is named '{words}' and which stored name you used.")


def _staff_spelling(tools: Any, resolver: Any, word: str) -> bool:
    """:meth:`NamesMixin.staff_close_name` with the spelling help: a spelling the resolver offers for ``word``
    (``corrected`` or ``ambiguous``) is the stored first name of an Employee or User (mapped by name: the first-name
    index), other than ``word`` itself."""
    res = _person_term(resolver, word)
    if res is None or res.decision not in ("corrected", "ambiguous"):
        return False
    first = tools._names_first or {}
    for s in res.suggestions:
        k = name_key(s)
        if k != word and len(k.split()) == 1 and any(tools.kind_of(r[0], r[3]) in ("employee", "user")
                                                     for r in first.get(k) or []):
            return True
    return False


def _surname_like(tools: Any, resolver: Any, word: str, vocab: set[str], capitalized: bool = False) -> bool:
    """``word`` (the question word after a single name) reads as a surname: 3 or more letters, no function or catalog
    word, and a stored last name, a word written with a capital letter in the question ("Killo Brown"), or no known
    (English, knowledge or spoken) word."""
    if len(word) < 3 or word in _HINT_STOPWORDS or word in vocab:
        return False
    return word in (tools._names_last or {}) or capitalized or not resolver.is_known_word(word)


_NEXT_WORD_RE = re.compile(r"[\s,]+([^\W\d_][\w'’-]*)")


def _capitalized_next(question: str, end: int, word: str) -> bool:
    """The question's word right after ``question[:end]`` is ``word`` written with a capital first letter."""
    m = _NEXT_WORD_RE.match(question, end)
    return bool(m) and m.group(1)[:1].isupper() and _q_tokens(m.group(1)) == [word]


def _surname_fits(word: str, rest: list[str]) -> bool:
    """``word`` is one of a stored name's words after the first, or a close spelling of one (:func:`_close_word`)."""
    return any(word == x or _close_word(word, x) for x in rest)


def _spelling_spots(tools: Any, resolver: Any, question: str, toks: list[str], used: list[bool],
                    vocab: set[str]) -> list[tuple[int, dict]]:
    """The spots of misspelt person names (:meth:`NamesMixin._spot` with the spelling help; in place of its
    close-surname step): every span of ``resolver.spell_correct(question)`` decided ``corrected`` with ``person``
    among its kinds, whose words are question words that no exact pass used, and whose stored spelling the name index
    holds (:func:`_stored_people`; one word: a first name of 3 or more letters that is no function or catalog word,
    typed as a word that is none either, and only for doubled letters or an identical sound; when the next question
    word reads as a surname (:func:`_surname_like`), only if a stored record of that first name has it or a close
    spelling of it: an unstored full name is no misspelt first name). Each spot is ``{"words":
    the typed words, "asked": the same, "close": the stored names (one word: the stored first name), "recs": the
    stored records, "first_only": one word, "read_as": the stored spelling}``; its words are marked used. An
    ``ambiguous`` span gives no spot: the names call may name it, and it is then a missing name with did-you-means."""
    first = tools._names_first or {}
    found: list[tuple[int, dict]] = []
    taken = list(used)   # marked in ``used`` only at the end: a failure half-way changes nothing
    for c in resolver.spell_correct(question):
        if c.decision != "corrected" or "person" not in c.kinds or not c.suggestion:
            continue
        words = _q_tokens(c.text)
        n = len(words)
        if not words or n > NAME_NGRAM_MAX:
            continue
        i = next((i for i in range(len(toks) - n + 1) if toks[i:i + n] == words and not any(taken[i:i + n])), None)
        if i is None:
            continue
        skey = name_key(c.suggestion)
        if len(skey.split()) == 1:
            w = words[0]
            if n != 1 or len(w) < 3 or len(skey) < 3 or w in _HINT_STOPWORDS or w in vocab \
                    or skey in _HINT_STOPWORDS or skey in vocab or not _one_name_slip(resolver, w, c.suggestion):
                continue
            recs = list(first.get(skey) or [])
            nxt = toks[i + 1] if i + 1 < len(toks) and not taken[i + 1] else ""
            if _surname_like(tools, resolver, nxt, vocab, bool(nxt) and _capitalized_next(question, c.end, nxt)) and \
                    not any(_surname_fits(nxt, name_key(r[2]).split()[1:]) for r in recs):
                continue   # an unstored full name ("Killo Brown", only Kilo Zephyr stored): a missing name
            close = [_disp(c.suggestion)]
        else:
            recs = tools._person_recs(skey)
            close = list(dict.fromkeys(_disp(r[2]) for r in recs))
        if not recs:
            continue
        key = " ".join(words)
        found.append((i, {"words": key, "recs": recs, "first_only": len(skey.split()) == 1, "asked": key,
                          "close": close, "read_as": _disp(c.suggestion)}))
        for j in range(i, i + n):
            taken[j] = True
    used[:] = taken
    return found
