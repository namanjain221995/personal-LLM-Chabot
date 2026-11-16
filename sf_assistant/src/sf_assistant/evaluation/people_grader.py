"""Automatic grading of the person question set (``okeval_run/lookup_eval/people/expected.json``).

Each expected entry has ``must`` items (free text written from the SQL facts), ``partial`` and ``wrong`` examples, the
facts themselves (``expected``) and their SQL. The grader follows the set's rules (people/README.md, "Grading"):

1. **wrong** first: no answer or a non-answer; a "not stored" / knowledge-only refusal for a person who is stored
   (every category but ``not_stored``); a yes / no that contradicts the first ``must`` item; the main count of the
   first item missing while the answer gives a number of a ``wrong`` example; or more than half of the first item
   (the main answer, at least 5 atoms) missing.
2. every ``must`` item is split into **atoms** and each atom must be in the answer:

   * numbers (and ISO dates) of the item, each with its **context**: the words the number counts, i.e. the content
     words right after it up to the next comma / bracket ("15 sessions", "2 intake interviews"), else the words
     before it ("owns 429"). The answer must have the number within a few words of one of them (stems: "hosted" =
     "hosts"; synonyms: "taken" = "completed" = "conducted"; API names read as words: ``CandidateTrainingStep__c`` =
     "candidate training step"). A list of exactly N items that name the context states N; a 0 is also stated by
     "no" / "none" next to a context word; dates written in words count ("March 1, 2026"). After a data refresh the
     numbers are re-derived from the SQL (:mod:`expected_data`). An item that names a ``{label: count}`` fact ("status
     split from leads_owned_by_status") needs each of its counts next to its label;
   * values the item quotes from the expected facts (names, departments, levels, statuses, trainings, record types:
     any string fact of the entry that the item contains). A one-word value needs a context word near it
     ("Recruiter" in "Account (record type Recruiter)" near "account" / "type"); a name right after a record kind
     ("a Lead also named X") needs that kind in the same sentence of the answer;
   * API names only for ``person_metadata`` questions (the field is the answer there; the object may be written in
     words); elsewhere they explain a role and are not required;
   * a "says ... not stored / no one named" item needs a not-found statement (and its numbers are no facts: a made-up
     name has no counts); a "notes the spelling" / "close spelling" item a spelling note.

   Items marked optional ("may be named", "may be mentioned", "if mentioned", "optional", "need not", or the whole item
   in parentheses) are not required. An item with no atom is not checked (listed in ``checks.unchecked``).
3. **correct** when every required atom is found, else **partial** (the reasons list what is missing).

Leaked merge text and unsupported numbers make a correct answer ``needs_review`` (:func:`autograde._common`), and so
does a fact that cannot be re-derived after a data change.

The answer graded is ``answer_body`` (the code footer and the code's status lines of Group B item 11 are never
graded). When the row has ``status_lines`` and atoms are missing, ``checks["status_lines_add"]`` lists the missing
atoms those lines hold: what item 11 would add for the reader, measured apart (the verdict does not change).
"""
from __future__ import annotations

import re
from typing import Any

from sf_assistant.evaluation import expected_data
from sf_assistant.evaluation.autograde import (Grade, _NOT_FOUND_EXTRA_RE, _SUGGEST_RE, _common, _non_answer,
                                               answer_text, without_source)
from sf_assistant.evaluation.score_records import _NOT_FOUND_RE

__all__ = ["grade_people", "must_atoms", "Atom"]

#: stopwords: never context
_STOP = frozenset("""a an the of to in on for and or by with from is are be as at it its this that one any his her
their them they he she so also each both all other others only not no but if than then via has have had who whom which
what when where how there here was were will would can could should may might must do does did same own very just
into onto out over under up down about after before through per x vs etc e g i ie eg s n""".split())
#: stems that mean the same in a count's context
_SYNONYMS = [
    {"complet", "finish", "done", "taken", "take", "took", "conduct"},
    {"mock", "intern", "internal"},
    {"session", "meet", "meeting"},
    {"train", "trainer"},
    {"support", "supporter"},
    {"own", "owner"},
    {"creat", "creator"},
    {"edit", "editor", "modifi"},
    {"application", "appli", "apply"},
    {"candidat", "student"},
    {"row", "record", "entry"},
    {"lead", "prospect"},
    {"employe", "staff", "recruit"},
    {"engagement", "market"},
    {"slot", "availability", "availabl"},
]
_SYN: dict[str, set[str]] = {}
for _group in _SYNONYMS:
    for _w in _group:
        _SYN.setdefault(_w, set()).update(_group)

_OPTIONAL_RE = re.compile(r"\b(?:may be (?:named|mentioned|said|listed|given)|if mentioned|optional|need not|"
                          r"ideally|can be mentioned)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{1,5}-\d{2,}\b")
_API_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*\.)*[A-Za-z][A-Za-z0-9_]*__[cr]\b|\b(?:CreatedById|OwnerId|"
                     r"LastModifiedById|AccountId|ConvertedAccountId)\b")
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
#: dates, numbers (not glued to letters: 'V1', 'x2', 'Smith2'), words; ``|`` = a clause boundary (must items only)
_TOKEN_RE = re.compile(r"(\d{4}-\d{2}-\d{2})|(?<![A-Za-z_])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![A-Za-z_])|"
                       r"([a-z][a-z']*)|([,;:()\[\]]|[.!?](?=\s|$))", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
             r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_WORD_DATE_RES = (re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE),
                  re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\.?,?\s+(\d{4})\b",
                             re.IGNORECASE))
_WORDNUM = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_WORDNUM.update({"single": "1", "twice": "2"})
#: words of a value that do not make it distinctive ("Fresher level" = "fresher")
_GENERIC_VALUE_WORDS = frozenset({"level", "department", "dept"})
#: structural labels of the facts (link kinds, reference roles, object kinds): no values of the person
_LABELS = frozenset({"undocumented", "(blank)", "true", "false", "same record", "parent", "shared parent",
                     "record creator", "record owner", "last editor", "business", "system", "candidate", "user",
                     "account", "lead", "employee"})
#: Salesforce codes in the facts and how an answer says them
_VALUE_SYNONYMS = {
    "powercustomersuccess": ("powercustomersuccess", "portal", "customer success", "community"),
    "personaccount": ("person account", "personaccount", "candidate account"),
}
#: record kinds: a name right after one of them in a must item needs that kind next to it in the answer
_KINDS = frozenset({"lead", "user"})
#: answer words that say "none" next to a context word (a required 0)
_ZERO_WORDS = frozenset({"no", "none", "zero", "never", "not", "nothing", "nobody", "without", "neither", "nor"})
#: an answer that refuses for lack of knowledge or record data
_REFUSAL_RE = re.compile(r"\b(?:knowledge (?:file |base )?(?:does not|doesn't) (?:contain|include|have)|check the User "
                         r"object|not (?:available|present|included) in the (?:object )?knowledge|needs? (?:live )?record "
                         r"data|requires? (?:live )?record data|no (?:record|live|actual) data)\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^(\s*)(?:[-*•]|\d+[.)]|\|(?!\s*:?-{3}))\s*(.*)$")   # list items and table rows
_ANSWER_API_RE = re.compile(r"\b(?:[A-Za-z][A-Za-z0-9]*_)*[A-Za-z][A-Za-z0-9]*__[cr]\b")

#: wrong when the first must item (the main answer) has at least this many atoms and more than this share is missing
MAIN_MIN_ATOMS = 5
MAIN_MISSING_SHARE = 0.5
CONTEXT_WINDOW_MUST = 4      # content words on each side of a one-word value in the must item
CONTEXT_WINDOW_ANSWER = 4    # tokens on each side of a number in the answer
B = "|"                      # clause boundary token


# ------------------------------------------------------------------------------------------------ text
def _iso_dates(text: str) -> str:
    """'March 1, 2026' / '1 March 2026' / 'Mar 1st, 2026' -> '2026-03-01' (answers write dates in words)."""
    def month_first(m):
        return f"{m.group(3)}-{_MONTHS[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"

    def day_first(m):
        return f"{m.group(3)}-{_MONTHS[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}"
    return _WORD_DATE_RES[1].sub(day_first, _WORD_DATE_RES[0].sub(month_first, text or ""))


def _stem(w: str) -> str:
    """A crude stem shared by must items and answers: plural, -ing, -ed and a final e removed ("employees" =
    "employee", "hosted" = "hosts", "completed" = "complete"); no "-er" (an interviewer is no interview)."""
    w = w.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes", "zes", "uses")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _syn(stem: str) -> set[str]:
    return _SYN.get(stem, {stem})


def _spans(text: str, answer: bool = True) -> list[tuple[str, int, int]]:
    """``(token, start, end)``: ISO dates, canonical numbers, lower-case words (number words -> digits in answers),
    and (must items only) ``|`` for a comma, colon, semicolon, bracket or full stop."""
    out = []
    for m in _TOKEN_RE.finditer(text or ""):
        date, num, word, punct = m.groups()
        if date:
            out.append((date, m.start(), m.end()))
        elif num:
            n = expected_data.canon_number(float(num.replace(",", ""))) if "." in num else str(int(num.replace(",", "")))
            out.append((n, m.start(), m.end()))
        elif word:
            low = word.lower()
            out.append((_WORDNUM.get(low, low) if answer else low, m.start(), m.end()))
        elif not answer:
            out.append((B, m.start(), m.end()))
    return out


def _tokens(text: str) -> list[str]:
    """Answer tokens (see :func:`_spans`; dates in words become ISO dates first)."""
    return [t for t, _, _ in _spans(_iso_dates(text or ""))]


def _plain(text: str) -> str:
    """Markdown emphasis, backticks and table bars removed."""
    return re.sub(r"[*`|>#]+", " ", text or "")


def _api_words(text: str) -> str:
    """API names of an answer as words, for the context of a count ("127 CandidateTrainingStep__c records" ->
    "127 candidate training step records"; the field checks read the answer as written)."""
    def words(m: re.Match) -> str:
        name = re.sub(r"__[cr]$", "", m.group(0))
        return " ".join(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").split())
    return _ANSWER_API_RE.sub(words, text or "")


def _answer_view(text: str) -> str:
    """The answer as the atom checks read it: no Source line, no markdown, API names as words."""
    return _api_words(without_source(_plain(text)))


# ------------------------------------------------------------------------------------------------ atoms
class Atom:
    """One required fact of a must item: ``kind`` number | split (a count of a breakdown the item points to) | date |
    value | api | not_found | spelling."""

    def __init__(self, kind: str, value: str, context: set[str] | None = None, item: int = 0) -> None:
        self.kind, self.value, self.context, self.item = kind, value, set(context or ()), item

    def __repr__(self) -> str:   # pragma: no cover - debugging aid
        return f"Atom({self.kind}, {self.value!r}, {sorted(self.context)})"

    def label(self) -> str:
        return self.value if not self.context else f"{self.value} ({'/'.join(sorted(self.context)[:3])})"


def _string_facts(v: Any, out: set[str]) -> set[str]:
    if isinstance(v, dict):
        for x in v.values():
            _string_facts(x, out)
    elif isinstance(v, list):
        for x in v:
            _string_facts(x, out)
    elif isinstance(v, str):
        s = " ".join(v.split())
        if 3 <= len(s) <= 60 and not _SF_ID_RE.match(s) and "_" not in s and not _DATE_RE.fullmatch(s) \
                and s.lower() not in _LABELS and ":" not in s:
            out.add(s)
    return out


def _content(words: list[str], rng, limit: int, *, stop_at_number: bool, stop_at_boundary: bool,
             skip: frozenset[int] | set[int] = frozenset()) -> list[str]:
    out: list[str] = []
    for p in rng:
        w = words[p]
        if w == B:
            if stop_at_boundary:
                break
            continue
        if p in skip:
            continue
        if w[0].isdigit():
            if stop_at_number:
                break
            continue
        if w in _STOP or len(w) < 3:
            continue
        out.append(_stem(w))
        if len(out) >= limit:
            break
    return out


def _number_context(words: list[str], i: int, names: set[int]) -> set[str]:
    """What a number counts: the content words right after it up to the next number or clause boundary ("15
    sessions", "2 intake interviews", "4 Unscheduled"; names skipped); when nothing follows, the words before it
    ("owns 429")."""
    after = _content(words, range(i + 1, len(words)), 2, stop_at_number=True, stop_at_boundary=True, skip=names)
    return set(after or _content(words, range(i - 1, -1, -1), 2, stop_at_number=True, stop_at_boundary=True,
                                 skip=names))


def must_atoms(entry: dict, category: str | None = None) -> tuple[list[list[Atom]], list[int], list[int]]:
    """``(atoms per must item, optional item indexes, unchecked item indexes)`` of a person-set entry."""
    category = category or entry.get("category") or ""
    facts = sorted(_string_facts(entry.get("expected") or {}, set()), key=len, reverse=True)
    out: list[list[Atom]] = []
    optional: list[int] = []
    unchecked: list[int] = []
    for idx, item in enumerate(entry.get("must") or []):
        text = item.strip()
        atoms: list[Atom] = []
        if _OPTIONAL_RE.search(text) or (text.startswith("(") and text.endswith(")")):
            optional.append(idx)
        low = text.lower()
        if low.startswith("says") and re.search(r"\bnot (?:stored|found)\b|\bno (?:employee|user|one|person|record|"
                                                 r"candidate|lead)\b", low):
            atoms.append(Atom("not_found", "says not stored", item=idx))
        if re.search(r"\bnotes the spelling\b|\bclose spelling\b|\bmisspel", low):
            atoms.append(Atom("spelling", "notes the spelling", item=idx))
        if category == "person_metadata":   # the field is the answer there
            atoms += [Atom("api", a, item=idx) for a in dict.fromkeys(_API_RE.findall(text))]
        scrub = _API_RE.sub(" ", text)
        scrub = _CODE_TOKEN_RE.sub(lambda m: m.group(0).replace("-", "_"), scrub)
        scrub = re.sub(r"\btoday\s+\d{4}-\d{2}-\d{2}", "today", scrub)   # the question's reference day: no fact
        low = scrub.lower()
        # values the item quotes from the facts (longest first, no overlaps)
        taken: list[tuple[int, int, Atom]] = []
        for fact in facts:
            m = next((m for m in re.finditer(r"(?<![\w'])" + re.escape(fact.lower()) + r"(?![\w'])", low)
                      if not any(m.start() < b and a < m.end() for a, b, _ in taken)), None)
            if m is not None:
                atom = Atom("value", fact, item=idx)
                taken.append((m.start(), m.end(), atom))
                atoms.append(atom)
        # a quoted name the facts do not hold ("Users (incl. 'Golf Hotel' ...)"); not for a name that is not
        # stored (a not_stored item quotes the asked, misspelt name)
        for m in re.finditer(r"'([A-Z][\w.]*(?: [A-Za-z][\w.]*){1,3})'", scrub) if category != "not_stored" else ():
            if not any(a <= m.start(1) < b for a, b, _ in taken) and all(w[:1].isupper() for w in m.group(1).split()):
                atom = Atom("value", m.group(1), item=idx)
                taken.append((m.start(1), m.end(1), atom))
                atoms.append(atom)
        spans = _spans(_plain(scrub), answer=False)
        words = [t for t, _, _ in spans]
        in_value = {k: next((v for v, (s0, e0, _) in enumerate(taken) if s0 <= s < e0), None)
                    for k, (_, s, _) in enumerate(spans)}
        names = {k for k, v in in_value.items() if v is not None and len(taken[v][2].value.split()) > 1}
        for k, w in enumerate(words):
            if in_value.get(k) is not None or w == B:
                continue
            if _DATE_RE.fullmatch(w):
                atoms.append(Atom("date", w, item=idx))
            elif w[0].isdigit() and not re.fullmatch(r"20\d\d", w):
                same = next((a for a in atoms if a.kind == "number" and a.value == w), None)
                if same is not None:   # the same number twice in one item: either reading counts
                    same.context |= _number_context(words, k, names)
                else:
                    atoms.append(Atom("number", w, _number_context(words, k, names), item=idx))
        # "status split from <fact key>": every count of that {label: count} fact, next to its label
        for key, fact in (entry.get("expected") or {}).items():
            if isinstance(fact, dict) and fact and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                       for v in fact.values()) \
                    and re.search(r"(?<![\w])" + re.escape(str(key)) + r"(?![\w])", text):
                for label, n in fact.items():
                    ctx = {_stem(w) for w in _tokens(str(label)) if not w[0].isdigit() and w not in _STOP}
                    atoms.append(Atom("split", expected_data.canon_number(n), ctx or None, item=idx))
        for v, (_, _, atom) in enumerate(taken):
            ks = [k for k, x in in_value.items() if x == v]
            if not ks:
                continue
            if len(ks) == 1:   # a one-word value: the words around it in the item
                atom.context = set(_content(words, range(ks[0] - 1, -1, -1), CONTEXT_WINDOW_MUST,
                                            stop_at_number=False, stop_at_boundary=False)
                                   + _content(words, range(ks[-1] + 1, len(words)), CONTEXT_WINDOW_MUST,
                                              stop_at_number=False, stop_at_boundary=False)) - {_stem(atom.value)}
            else:   # a name right after a record kind ("a Lead also named X", "Standard User X"; not "team lead X")
                before = _content(words, range(ks[0] - 1, -1, -1), 2, stop_at_number=True, stop_at_boundary=True)
                atom.context = {w for n, w in enumerate(before) if w in _KINDS
                                and not (n + 1 < len(before) and before[n + 1] == "team")}
        if not atoms:
            unchecked.append(idx)
        out.append(atoms)
    return out, optional, unchecked


# ------------------------------------------------------------------------------------------------ matching
def _want(context: set[str]) -> set[str]:
    want: set[str] = set()
    for c in context:
        want |= _syn(c)
    return want


def _near(tokens: list[str], positions: list[int], context: set[str], width: int = CONTEXT_WINDOW_ANSWER) -> bool:
    if not context:
        return bool(positions)
    want = _want(context)
    for p in positions:
        for q in range(max(0, p - width), min(len(tokens), p + width + 1)):
            if q != p and not tokens[q][0].isdigit() and (_syn(_stem(tokens[q])) & want):
                return True
    return False


def _implied_count(n: str, context: set[str], text: str) -> bool:
    """A count stated by a list: the answer has a run of exactly ``n`` consecutive list items (3 to 30) at one
    indentation, each naming a context word ("7 other employees ..." answered by 7 bullets, one per employee)."""
    if not n.isdigit() or not 3 <= int(n) <= 30 or not context:
        return False
    want = _want(context)
    runs: dict[int, int] = {}
    closed: list[int] = []
    for line in (text or "").split("\n"):
        m = _BULLET_RE.match(line)
        if not m:
            if line.strip():
                closed += runs.values()
                runs = {}
            continue
        ind = len(m.group(1).expandtabs(4))
        for k in [k for k in runs if k > ind]:
            closed.append(runs.pop(k))
        stems = {_stem(t) for t in _tokens(_api_words(_plain(m.group(2))))}
        if any(w in st for st in stems for w in want if len(w) >= 4) or any(_syn(st) & want for st in stems):
            runs[ind] = runs.get(ind, 0) + 1   # "retraining" names a training
        elif ind in runs:
            closed.append(runs.pop(ind))
    closed += runs.values()
    return int(n) in closed


def _name_starts(tokens: list[str], words: list[str]) -> list[int]:
    """Where a multi-word value starts in ``tokens``: its words in order within a short span, or first + last word."""
    starts = []
    for i, t in enumerate(tokens):
        if t != words[0]:
            continue
        k = 0
        for s in tokens[i:i + len(words) + 3]:
            if k < len(words) and s == words[k]:
                k += 1
        if k == len(words) or words[-1] in tokens[i + 1:i + 4]:
            starts.append(i)
    return starts


def _value_found(atom: Atom, view: str, tokens: list[str]) -> bool:
    words = _tokens(atom.value)
    distinctive = [w for w in words if w not in _GENERIC_VALUE_WORDS] or words
    joined = " ".join(tokens)
    forms = _VALUE_SYNONYMS.get(atom.value.lower())
    if forms and any(f in joined for f in forms):
        return True
    if len(distinctive) == 1:
        pos = [i for i, t in enumerate(tokens) if t == distinctive[0] or _stem(t) == _stem(distinctive[0])]
        return _near(tokens, pos, atom.context) if len(words) == 1 else bool(pos)
    if not _name_starts(tokens, distinctive):
        return False
    if not atom.context:
        return True
    want = _want(atom.context)   # the record kind of a name: the name and the kind in one sentence / list line
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", view):
        toks = _tokens(sentence)
        if _name_starts(toks, distinctive) and any(_syn(_stem(t)) & want for t in toks):
            return True
    return False


def _atom_found(atom: Atom, text: str, view: str, tokens: list[str]) -> bool:
    if atom.kind == "not_found":
        head = " ".join(re.split(r"(?<=[.!?])\s+", without_source(text).strip())[:3])
        return bool(_NOT_FOUND_RE.search(head) or _NOT_FOUND_EXTRA_RE.search(head))
    if atom.kind == "spelling":
        return (_SUGGEST_RE.search(text) is not None
                or re.search(r"\bspell|\btypo|\bmeant\b|\bstored as\b", text, re.IGNORECASE) is not None)
    if atom.kind == "api":
        *objs, fld = atom.value.split(".")
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(fld) + r"(?![A-Za-z0-9_])", text):
            return False
        joined = " ".join(tokens)
        for obj in objs:   # the object by its API name or in words ("candidate training")
            words = [w for w in re.sub(r"__[a-z]+$", "", obj).lower().split("_") if w]
            if not (re.search(r"(?<![A-Za-z0-9_])" + re.escape(obj) + r"(?![A-Za-z0-9_])", text)
                    or re.search(r"\b" + r"\W+".join(re.escape(w) for w in words), joined)):
                return False
        return True
    if atom.kind == "date":
        return atom.value in tokens
    if atom.kind in ("number", "split"):
        pos = [i for i, t in enumerate(tokens) if t == atom.value]
        if _near(tokens, pos, atom.context) or _implied_count(atom.value, atom.context, without_source(text)):
            return True
        if atom.value == "0":   # "no business records", "none of them"
            return _near(tokens, [i for i, t in enumerate(tokens) if t in _ZERO_WORDS], atom.context)
        return False
    return _value_found(atom, view, tokens)


def _polarity(text: str) -> str | None:
    head = re.sub(r"^[\s*_#>`\"']+", "", without_source(text or ""))[:40].lower()
    if re.match(r"yes\b", head):
        return "yes"
    if re.match(r"no\b(?![-\w])", head) and not re.match(r"no (?:employee|user|account|lead|record|person|one)\b", head):
        return "no"
    return None


def _expected_polarity(entry: dict) -> str | None:
    first = ((entry.get("must") or [""])[0]).strip().lower()
    if re.match(r"yes\b", first):
        return "yes"
    if re.match(r"no\b[:,;]", first):
        return "no"
    return None


def _numbers(text: str) -> set[str]:
    return {t for t in _tokens(text) if t[0].isdigit() and not _DATE_RE.fullmatch(t)}


# ------------------------------------------------------------------------------------------------ grader
def grade_people(item, row: dict, mode: str, con=None) -> Grade:
    """Grade one person-set answer (see the module docstring)."""
    entry = item.expected or {}
    category = entry.get("category") or ""
    text = answer_text(row)
    g = Grade("wrong", checks={"category": category, "answer_kind": row.get("answer_kind")})
    na = _non_answer(row, text)
    if na:
        g.reasons.append(na)
        return g
    chk = expected_data.check_entry(con, entry, "people")
    g.data = chk.summary()
    view = _answer_view(text)
    tokens = _tokens(view)
    # ---- wrong rules before the atoms
    if category != "not_stored":
        if _REFUSAL_RE.search(view) and (row.get("answer_kind") in ("knowledge", "none") or not _numbers(view)):
            g.reasons.append("refuses for lack of knowledge / record data although the person is stored")
            return g
        head = re.split(r"(?<=[.!?])\s+", view.strip())[0] if view.strip() else ""
        if (_NOT_FOUND_RE.search(head) or _NOT_FOUND_EXTRA_RE.search(head)) and not _numbers(view):
            g.reasons.append("says the person is not stored, but the person is stored")
            return g
    want_pol, got_pol = _expected_polarity(entry), _polarity(text)
    g.checks.update(polarity=got_pol, expected_polarity=want_pol)
    if want_pol and got_pol and want_pol != got_pol:
        g.reasons.append(f"answers {got_pol!r}; expected {want_pol!r}")
        return g
    # ---- atoms
    atoms, optional, unchecked = must_atoms(entry, category)
    # the counts (numbers, and the counts of a "status split" fact) follow a data change; a name, value or date the
    # changed data no longer holds cannot be checked
    numbers = sorted({a.value for item_atoms in atoms for a in item_atoms if a.kind in ("number", "split")})
    _, stale, notes = expected_data.refresh_numbers(numbers, chk)
    mapping = dict(note.split(": ", 1)[1].split(" -> ") for note in notes)
    texts = sorted({a.value for item_atoms in atoms for a in item_atoms if a.kind in ("value", "date")})
    stale = stale + expected_data.stale_texts(texts, chk)
    if chk.status == "error" and not stale:   # no count to map, but the facts cannot be checked either
        stale = ["(the expected facts)"]
    g.reasons += notes
    # a made-up name: the answer must say so and give no count, so the item's numbers are no facts to find
    no_counts = category == "not_stored" and any(a.kind == "not_found" for item_atoms in atoms for a in item_atoms)
    results: list[tuple[int, Atom, bool]] = []
    for idx, item_atoms in enumerate(atoms):
        for atom in item_atoms:
            if atom.kind == "number" and no_counts:
                continue
            if atom.kind in ("number", "split") and atom.value in mapping:
                atom = Atom(atom.kind, mapping[atom.value], atom.context, atom.item)
            results.append((idx, atom, _atom_found(atom, text, view, tokens)))
    missing = [f"must {idx + 1}: {atom.label()}" for idx, atom, ok in results if not ok and idx not in optional]
    first = [(atom, ok) for idx, atom, ok in results if idx == 0 and 0 not in optional]
    g.checks.update(atoms=len(results), missing=missing, optional=[i + 1 for i in optional],
                    unchecked=[i + 1 for i in unchecked], stale=stale)
    lines = [str(x) for x in row.get("status_lines") or [] if x]
    if lines and missing:   # Group B item 11: what the code's status lines under the answer would add (never graded)
        full = text + "\n" + "\n".join(lines)
        fview = _answer_view(full)
        ftokens = _tokens(fview)
        g.checks["status_lines_add"] = [f"must {idx + 1}: {atom.label()}" for idx, atom, ok in results
                                        if not ok and idx not in optional and _atom_found(atom, full, fview, ftokens)]
    # ---- wrong rules on the main answer (the first must item)
    main = next((a for a, ok in first if a.kind == "number" and a.context), None)
    main_ok = next((ok for a, ok in first if a is main), True)
    if main is not None and not main_ok:
        must_nums = {atom.value for _, atom, _ in results if atom.kind == "number"}
        wrong_nums = set()
        for w in entry.get("wrong") or []:
            wrong_nums |= {n for n in _numbers(_API_RE.sub(" ", w)) if n not in must_nums and n not in ("0", "1")}
        given_wrong = sorted(n for n in wrong_nums if n in tokens)
        if given_wrong:
            g.reasons.append(f"gives {given_wrong[0]} (a wrong example) instead of the main count {main.label()}")
            g.reasons += missing[:6]
            return g
    if len(first) >= MAIN_MIN_ATOMS and sum(not ok for _, ok in first) > MAIN_MISSING_SHARE * len(first):
        g.reasons.append("more than half of the main answer (first must item) is missing: " + "; ".join(missing[:8]))
        return g
    if stale:
        g.verdict = "needs_review"
        g.reasons.append(("expected SQL failed on the data: " + (chk.errors[0] if chk.errors else "?"))
                         if chk.status == "error" else "data changed: cannot re-derive " + ", ".join(stale))
    elif not missing:
        g.verdict = "correct"
    else:
        g.verdict = "partial"
        g.reasons.append(f"{len(missing)} must fact(s) missing: " + "; ".join(missing[:8]))
    _common(row, text, g)
    return g
