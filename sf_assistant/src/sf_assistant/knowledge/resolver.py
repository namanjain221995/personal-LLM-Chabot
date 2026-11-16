"""Typo-tolerant term resolver: one matcher for every name the assistant knows (docs/resolver.md).

A :class:`Vocabulary` lists :class:`Term` s (objects, fields, values, record types, components and people, each
with the target it names) and the extra correct words that are never corrected and never suggested (a generic English
word list and the words of the knowledge text). :func:`build` indexes it once into a :class:`Resolver`; the resolver
is read-only afterwards, so one instance can be shared by every thread.

* :meth:`Resolver.resolve_term` matches one term (a tool argument: an object, field, value or person name) and
  returns a :class:`Resolution`: the decision (``exact`` | ``corrected`` | ``ambiguous`` | ``none``), the corrected
  spelling and the ranked :class:`Candidate` s with their targets.
* :meth:`Resolver.spell_correct` finds the misspelt words and phrases of a question and returns one
  :class:`Correction` per span (``'accont'`` -> ``Account``), for the question hints.

Matching tiers, best first: ``exact`` (normalised: case, accents, punctuation, possessive 's, ``__c`` and
underscores, CamelCase, plural -> singular for metadata; person names are never singularised), ``normalized``
(doubled letters collapsed), then ``sound`` (same first letter + consonant skeleton) and ``edit``
(Damerau-Levenshtein 1 for typed words of up to 5 letters, 2 for longer ones, with Jaro-Winkler >= 0.88), which are
scored together (an equal sound key is a bonus, :data:`STRICT_TIERS`), then ``phrase`` (word-by-word alignment of a
multi-word text, with missing, extra or reordered words).

A tool lookup (:meth:`Resolver.resolve_term`, ``wide=True``) scores the sound and edit matches by a noisy-channel
slip model instead (:func:`slip_cost`: how likely a typist turns the term into the typed word, from the kinds of
slips: another vowel or a neighbouring key, a letter left out, doubled or added, a swap, another spelling of the same
sound, a slip on the first letter), and also lists spellings up to :func:`wide_edits` away, which are only
did-you-means. :meth:`Resolver.spell_correct` keeps the strict budget and scores: a question has no argument slot
that says a word must be a name.

Guard rails: only vocabulary is ever suggested; numbers, dates, Ids, e-mails, URLs, words under 4 letters, words
longer than any vocabulary word, generic English function words and words that are already correct (vocabulary,
English word list, knowledge text) are never corrected. ``corrected`` is never given when the typed word is itself a
correct word, differs only by a whole affix (``Uncompleted``/``Completed``, ``Trainee``/``Trainer``), leaves words of
the term out, keeps words the term does not have, changes the first sound of a person name, or turns one single name
into another (only doubled letters and identical sounds are corrected there); those come back ``ambiguous`` (a
did-you-mean). A close second spelling (a tie) is ``ambiguous``; a word close to many targets only loosely is not
corrected.

Standard library only; no import of any other sf_assistant module. It lives in the knowledge layer (config/prompts
< knowledge < engine < tools < agent), so the knowledge side (knowledge tools, BM25 search, a ``--no-records`` run)
and the record side (engine, tools) can both use it.
"""

from __future__ import annotations

import functools
import gc
import heapq
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

__all__ = ["KINDS", "TIERS", "DECISIONS", "STOPWORDS", "CONNECTORS", "HONORIFICS", "FILLERS", "Term", "Vocabulary",
           "Candidate", "Resolution", "Correction", "Resolver", "build", "split_words", "norm_words", "singular",
           "dedup", "phonetic", "sound_key", "alt_sound_key", "jaro_winkler", "osa_distance", "max_edits",
           "SLIP_COSTS", "slip_cost", "slip_conf", "wide_edits"]

#: Kinds of terms, in the order candidates of the same confidence are listed.
KINDS = ("object", "field", "value", "record_type", "component", "person")
_KIND_RANK = {k: i for i, k in enumerate(KINDS)}
_META_KINDS = frozenset(KINDS) - {"person"}
#: Matching tiers, best first.
TIERS = ("exact", "normalized", "sound", "edit", "phrase")
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}
DECISIONS = ("exact", "corrected", "ambiguous", "none")

# ---------------------------------------------------------------------------------------------------------- settings
MIN_FUZZY_LEN = 4          #: shorter words are matched exactly only (never corrected), and never suggested
JW_MIN = 0.88              #: Jaro-Winkler floor of an edit-tier match
SOUND_JW_MIN = 0.80        #: Jaro-Winkler floor of a sound-tier match (the sound key already agrees)
NORMALIZED_CONF = 0.97     #: confidence of a doubled-letter match
FUZZY_CAP = 0.96           #: no sound or edit match scores above this
CORRECT_MIN = 0.80         #: a correction needs at least this confidence ...
MARGIN = 0.03              #: ... and this lead over the next different spelling
TIE_MARGIN = 0.06          #: a runner-up this close with no more edits than the best (or fewer) makes it ambiguous
AMBIGUOUS_MIN = 0.76       #: several spellings at least this close: ambiguous (below: none, with the closest ones)
WEAK_MIN = 0.50            #: below this a phrase candidate is not listed at all
PHRASE_SUGGEST_MIN = 0.55  #: a phrase whose words match a term exactly but for one: a did-you-mean from this on
WEAK_STORED_MAX = 5        #: a word only stored values use, on fewer records than this, may itself be a typo
LOOSE_MANY = 6             #: this many fuzzy spellings for one word ...
LOOSE_CONF = 0.88          #: ... and a best one under this: the word matches many targets loosely, not corrected
CONTEXT_BOOST = 0.04       #: added to candidates of the context object
MAX_WORD_CANDIDATES = 8    #: fuzzy spellings kept per word
MAX_PHRASE_TERMS = 400     #: terms scored per phrase lookup (the ones sharing most words with the text)
WINDOW_OPTIONS = 6         #: close spellings per question word tried when a run of words may spell a whole term
PHRASE_BEAM = 216          #: best partial corrections kept while spelling a run of words
SOUND_BONUS = 0.04         #: added to a match with the same sound key
ONE_EDIT_BONUS = 0.03      #: added to a match one edit away
LIKELY_EDIT_BONUS = 0.01   #: ... plus this when that edit is a likely slip (keyboard neighbour, vowel, swap, double)
ALT_SOUND_PENALTY = 0.02   #: subtracted from a match found only by the second sound key (v/b, z/j)
FREQ_WEIGHT = 0.003        #: times log10(1 + terms using the word): the more common word wins a near tie ...
FREQ_CAP = 0.008           #: ... by at most this much
ANCHORED_JW_MIN = 0.80     #: Jaro-Winkler floor of a word matched only inside a phrase whose other words match
ANCHORED_EDITS = 2         #: edits such an anchored word of 5+ letters may have (the other words carry the phrase)
JW_MIN_LONG = 0.84         #: Jaro-Winkler floor of an edit-tier match of a typed word of 8+ letters
CONNECTOR_COST = 0.15      #: cost of a connector word (CONNECTORS) the text or the term has and the other lacks
LEN_SLACK = 2              #: a typed word longer than the longest vocabulary word + this is never matched fuzzily
MAX_TEXT_WORDS = 12        #: a term text with more words than this (and than any term + 2) is not looked up
STRICT_TIERS = False       #: True: any sound-key match hides every edit match; False: both are scored together and
                           #: an equal sound key is a bonus (measured better, docs/resolver.md)

#: Generic English function and question words (no business words). The English word list and the vocabulary are the
#: rest of the "already correct" words; month and day names stay here because word lists spell them capitalised.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be been before being below between both but by
can could did do does doing done down during each either else ever every few for from further get gets got had has
have having he her here hers him his how however i if in into is it its itself just last least less let like many
may me might more most much must my next no nor not now of off on once one only or other our ours out over own per
please same she should so some such than that the their theirs them then there these they this those through to
today too tomorrow under until up upon us very was we well were what when where which while who whom whose why will
with within without would yesterday yet you your yours since till ago
january february march april june july august september october november december
monday tuesday wednesday thursday friday saturday sunday
""".split())
#: Words a label may have or lack without changing what it names (``Number of Interviews``); never negations.
CONNECTORS = frozenset({"a", "an", "the", "of", "for", "to", "and"})
#: Titles around a person name (``Mr. X``, ``X sir``).
HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam", "maam", "shri", "smt"})
#: Words an argument may carry around the name it means (``the X``, ``X records``, ``Mr. X``): dropped at the edges
#: when the whole text does not match. Determiners, titles and container nouns only: never a negation or a
#: preposition (``Not Started``, ``On Hold`` keep theirs).
FILLERS = HONORIFICS | frozenset({"a", "an", "the", "this", "that", "these", "those", "all", "any", "some", "each",
                                  "every", "my", "our", "your", "his", "her", "their", "its", "please", "record",
                                  "records", "object", "objects", "field", "fields", "value", "values", "picklist",
                                  "named", "called"})

# ------------------------------------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class Term:
    """One name of one target.

    ``kind``: one of :data:`KINDS`. ``target``: what the name stands for: ``Obj`` (object), ``Obj.Field`` (field),
    ``Obj.Field=value`` (value), ``Obj.RecordType=DeveloperName`` (record type), ``flow:Name`` / ``apex:Name`` /
    ``validation_rule:Obj.Name`` / ``process:id`` (component), ``person:Obj/Id`` (person). ``text``: the name as
    written in the source (label, API name, value, stored name). ``object``: the object the target belongs to (for
    context scoring). ``source``: where the name came from (``label``, ``api``, ``plural``, ``picklist``, ``stored``,
    ``stored_text``, ``full``, ``first``, ``last`` ...). ``weight``: popularity (records holding a stored value), a
    tie-breaker only.
    """

    kind: str
    target: str
    text: str
    object: str | None = None
    source: str = ""
    weight: float = 1.0


@dataclass
class Vocabulary:
    """The terms and the extra known-correct words (never corrected, never suggested) of one resolver."""

    terms: list[Term] = field(default_factory=list)
    known_words: set[str] = field(default_factory=set)
    info: dict[str, Any] = field(default_factory=dict)

    def add(self, kind: str, target: str, text: Any, object: str | None = None, source: str = "",
            weight: float = 1.0) -> None:
        """Add one term (blank texts are skipped)."""
        if kind not in _KIND_RANK:
            raise ValueError(f"unknown kind {kind!r}; one of {KINDS}")
        s = str(text if text is not None else "").strip()
        if s:
            self.terms.append(Term(kind, target, s, object, source, float(weight)))

    def add_known(self, words: Iterable[str]) -> None:
        self.known_words.update(str(w) for w in words if w)


@dataclass(frozen=True)
class Candidate:
    """One ranked match: the vocabulary ``text`` matched for the input ``matched``, how (``tier``) and how surely."""

    kind: str
    target: str
    text: str
    matched: str
    tier: str
    confidence: float
    object: str | None = None
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "text": self.text, "matched": self.matched,
                "tier": self.tier, "confidence": round(self.confidence, 3), "object": self.object,
                "source": self.source}


@dataclass(frozen=True)
class Resolution:
    """Result of :meth:`Resolver.resolve_term`.

    ``decision``: ``exact`` (the text already names vocabulary), ``corrected`` (one clear close spelling:
    ``suggestion``), ``ambiguous`` (a did-you-mean: several close spellings, or one that must be confirmed:
    ``suggestions``) or ``none`` (no close match; the ``candidates`` then hold the closest weak matches, if any, to
    show as "closest names"). ``candidates`` are ranked, best first; for exact and corrected they all carry the chosen
    spelling. ``reason`` says why a text was not matched (``protected: number``) or not corrected (``not corrected:
    single name``). ``extra_words``: words of the text the best match does not use (fillers included);
    ``missing_words``: words of the best match the text lacks."""

    text: str
    decision: str
    suggestion: str | None
    suggestions: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    tier: str | None = None
    confidence: float = 0.0
    reason: str = ""
    extra_words: tuple[str, ...] = ()
    missing_words: tuple[str, ...] = ()

    @property
    def targets(self) -> list[str]:
        return list(dict.fromkeys(c.target for c in self.candidates))

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "decision": self.decision, "suggestion": self.suggestion,
                "suggestions": list(self.suggestions), "tier": self.tier, "confidence": round(self.confidence, 3),
                "reason": self.reason, "extra_words": list(self.extra_words),
                "missing_words": list(self.missing_words), "candidates": [c.as_dict() for c in self.candidates]}


@dataclass(frozen=True)
class Correction:
    """One span of a question found by :meth:`Resolver.spell_correct`: ``question[start:end] == text``."""

    start: int
    end: int
    text: str
    decision: str
    suggestion: str | None
    suggestions: tuple[str, ...]
    tier: str | None
    confidence: float
    kinds: tuple[str, ...]
    candidates: tuple[Candidate, ...] = ()

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    def as_dict(self) -> dict[str, Any]:
        return {"span": [self.start, self.end], "text": self.text, "decision": self.decision,
                "suggestion": self.suggestion, "suggestions": list(self.suggestions), "tier": self.tier,
                "confidence": round(self.confidence, 3), "kinds": list(self.kinds),
                "candidates": [c.as_dict() for c in self.candidates]}


# ----------------------------------------------------------------------------------------------------- normalising
_API_SUFFIX_RE = re.compile(r"__(c|r|mdt|e|x|b|kav|ka|history|share|feed)$", re.IGNORECASE)
_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_POSSESSIVE_RE = re.compile(r"(?<=\w)['’`]s\b", re.IGNORECASE)
_PLURAL_POSS_RE = re.compile(r"(?<=s)['’`](?=\s|$)")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_DOUBLE_RE = re.compile(r"(.)\1+")
#: Doubled letters only (digits kept): the compact keys of the doubled-letter tier, where ``01:55 PM`` must never
#: read as ``01:15 PM`` (``0155pm`` / ``0115pm``) nor ``1:30 PM`` as ``11:30 PM``.
_DOUBLE_LETTER_RE = re.compile(r"([^\W\d_])\1+")
_HAS_DIGIT_RE = re.compile(r"\d")
_DIGITS_RE = re.compile(r"\d+")
_VOWELS = frozenset("aeiouy")
#: Letters NFKD does not decompose.
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ß": "ss", "ẞ": "SS",
                           "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th",
                           "ı": "i", "ħ": "h", "Ħ": "H"})


def _fold(text: Any) -> str:
    """Letters NFKD keeps whole transliterated (ø, æ, ß, ł, đ, þ), then NFKD without combining marks (accents
    dropped); curly quotes read as straight ones."""
    s = str(text if text is not None else "").translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.replace("’", "'").replace("‘", "'").replace("\xa0", " ")


def split_words(text: Any) -> list[str]:
    """Lower-case words of a name or a question: accents dropped, possessive 's removed, an API suffix (``__c``,
    ``__mdt`` ...) dropped, underscores and CamelCase split, every other non-alphanumeric read as a space.
    ``"Background_Check__c"`` and ``"background check's"`` -> ``["background", "check"]``."""
    s = _fold(text)
    s = _POSSESSIVE_RE.sub("", s)
    s = _PLURAL_POSS_RE.sub("", s)
    out: list[str] = []
    for part in s.split():
        if "__" in part:
            part = _API_SUFFIX_RE.sub("", part)
        part = _CAMEL_2.sub(r"\1 \2", _CAMEL_1.sub(r"\1 \2", part))
        out.extend(w for w in _NON_ALNUM_RE.split(part.lower()) if w)
    return out


def singular(w: str, known: frozenset[str] | set[str] | None = None) -> str:
    """A crude singular that only has to map both sides the same way: ``logs`` -> ``log``, ``statuses`` ->
    ``status``, ``categories`` -> ``category``; ``status``, ``class``, ``analysis`` stay. With ``known`` (correct
    words), a plural in -es or -ies becomes the stem that is a known word: ``niches`` -> ``niche`` (not ``nich``),
    ``causes`` -> ``cause``, ``cookies`` -> ``cookie``, ``boxes`` -> ``box``."""
    if len(w) <= 3 or not w.isalpha():
        return w
    if known is not None and w.endswith("es"):
        if w[:-1] in known:
            return w[:-1]
        if w.endswith("ies") and w[:-3] + "y" in known:
            return w[:-3] + "y"
        if w[:-2] in known and len(w) > 4:
            return w[:-2]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "shes", "ches", "xes", "zes", "uses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def norm_words(text: Any, known: frozenset[str] | set[str] | None = None) -> list[str]:
    """:func:`split_words` with every word made :func:`singular`: the key words of a metadata term."""
    return [singular(w, known) for w in split_words(text)]


def dedup(w: str) -> str:
    """Doubled letters collapsed: ``zorrvan`` -> ``zorvan``, ``enrollment`` -> ``enrolment``."""
    return _DOUBLE_RE.sub(r"\1", w)


def dedup_letters(w: str) -> str:
    """:func:`dedup` for a key that may hold digits: doubled letters collapsed, every digit kept (``h11b`` stays
    ``h11b``, ``0155pm`` stays ``0155pm``): a number is never read as another number."""
    return _DOUBLE_LETTER_RE.sub(r"\1", w)


def numbers(text: Any) -> tuple[int, ...]:
    """The numbers of a text, as integers in order (``'09:30 AM'`` -> ``(9, 30)``, the same as ``'9:30 AM'``). A
    reading that changes them is another value (a time, a round, a visa class), never a spelling slip."""
    return tuple(int(x) for x in _DIGITS_RE.findall(_fold(text)))


_SOUND_RULES = (("tch", "ch"), ("ksh", "ks"), ("ph", "f"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("w", "v"),
                ("z", "s"), ("th", "t"), ("dh", "d"), ("bh", "b"), ("kh", "k"), ("gh", "g"), ("sh", "s"),
                ("ee", "i"), ("oo", "u"), ("ch", "9"))
_SOFT_C_RE = re.compile(r"c(?=[eiy])")
_W_AS_VOWEL_RE = re.compile(r"(?<=[aeiou])w(?![aeiou])")


def phonetic(w: str) -> str:
    """The word as it sounds, vowels kept: a ``w`` after a vowel and not before one is ``u`` (``rowvan`` =
    ``rouvan``), else ``w``=v; ``ph``=f, ``z``=s, ``q``=k, ``x``=ks, aspirated ``th dh bh kh gh sh`` = plain,
    ``ee``=i, ``oo``=u, ``c`` = k (s before e, i, y), ``ch`` its own sound, doubled letters collapsed. ``forquee`` and
    ``phorquee`` -> ``forkui``; ``neeche`` and ``niche`` -> ``ni9e``."""
    s = "".join(ch for ch in w.lower() if "a" <= ch <= "z")
    s = _W_AS_VOWEL_RE.sub("u", s)
    for a, b in _SOUND_RULES:
        s = s.replace(a, b)
    s = _SOFT_C_RE.sub("s", s).replace("c", "k")
    return dedup(s)


def sound_key(w: str) -> str:
    """First letter + consonant skeleton of :func:`phonetic` (``h`` and vowels dropped after the first letter, a
    first vowel read as ``a``). ``zorvan``/``zorwan``/``zorrvan`` -> ``srvn``; ``fysics``/``physics`` -> ``fsks``;
    ``account``/``accont`` -> ``aknt``."""
    s = phonetic(w)
    if not s:
        return ""
    head, tail = s[0], s[1:]
    if head in _VOWELS:
        head = "a"
    tail = "".join(ch for ch in tail if ch not in _VOWELS and ch != "h")
    return dedup(head + tail)


def _alt_fold(w: str) -> str:
    """The spelling with the transliteration pairs ``b``/``v`` and ``z``/``j`` merged (second sound key only)."""
    return w.lower().replace("bh", "v").replace("b", "v").replace("z", "j")


def alt_sound_key(w: str) -> str:
    """:func:`sound_key` with ``b``=v and ``z``=j (``vorbel``/``borbel``, ``zelvar``/``jelvar``): finds candidates
    only, and never decides ``corrected`` alone."""
    return sound_key(_alt_fold(w))


def max_edits(typed: str, word: str) -> int:
    """Edits allowed between a typed word and a vocabulary word: 1 when the typed word has up to 5 letters (or the
    vocabulary word up to 4), else 2."""
    return 1 if len(typed) <= 5 or len(word) <= 4 else 2


def osa_distance(a: str, b: str, max_d: int | None = None) -> int:
    """Damerau-Levenshtein distance (optimal string alignment: adjacent swaps count 1). With ``max_d`` it stops early
    and returns ``max_d + 1`` once the distance is known to be larger."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if max_d is not None and abs(la - lb) > max_d:
        return max_d + 1
    if not la or not lb:
        return max(la, lb)
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if max_d is not None and row_min > max_d:
            return max_d + 1
        prev2, prev = prev, cur
    return prev[lb]


def _jaro(a: str, b: str, positional: bool) -> float:
    la, lb = len(a), len(b)
    window = max(max(la, lb) // 2 - 1, 0)
    a_hit = [False] * la
    b_hit = [False] * lb
    m = 0
    if positional:
        for i in range(min(la, lb)):
            if a[i] == b[i]:
                a_hit[i] = b_hit[i] = True
                m += 1
    for i, ch in enumerate(a):
        if a_hit[i]:
            continue
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t = 0
    k = 0
    for i in range(la):
        if a_hit[i]:
            while not b_hit[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    return (m / la + m / lb + (m - t / 2) / m) / 3


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    """Jaro-Winkler similarity in [0, 1] (prefix bonus over at most 4 letters). The Jaro part is the better of two
    pairings: letters at the same position first, then the rest within the usual window (so a repeated letter is not
    paired with the wrong copy: plain greedy Jaro scores ``cinfidential``/``confidential``, one substitution, 0.84;
    this scores it 0.95), and the plain greedy pairing (so a letter added or left out near the start does not shift
    every later letter off its position: ``oova``/``zoova`` 0.93, ``vestt``/``vett`` 0.95)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    jaro = max(_jaro(a, b, True), _jaro(a, b, False))
    pre = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        pre += 1
    return jaro + pre * p * (1 - jaro)


_KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_KEY_POS = {ch: (r, c) for r, row in enumerate(_KEY_ROWS) for c, ch in enumerate(row)}


def _near_keys(a: str, b: str) -> bool:
    """Neighbouring keys on a QWERTY keyboard (same or next row, at most one column apart)."""
    pa, pb = _KEY_POS.get(a), _KEY_POS.get(b)
    return bool(pa and pb) and abs(pa[0] - pb[0]) <= 1 and abs(pa[1] - pb[1]) <= 1 and a != b


def _likely_slip(typed: str, word: str) -> bool:
    """One edit that people often make: a neighbouring key or another vowel typed, two letters swapped, a letter
    doubled or a neighbouring key added, a vowel or a doubled letter left out (a tie-breaker between near names)."""
    lt, lw = len(typed), len(word)
    if lt == lw:
        diff = [i for i in range(lt) if typed[i] != word[i]]
        if len(diff) == 1:
            x, y = typed[diff[0]], word[diff[0]]
            return (x in _VOWELS and y in _VOWELS) or _near_keys(x, y)
        return len(diff) == 2 and diff[1] == diff[0] + 1 and typed[diff[0]] == word[diff[1]] \
            and typed[diff[1]] == word[diff[0]]
    if lt == lw + 1:          # one letter added
        for i in range(lt):
            if typed[:i] + typed[i + 1:] == word:
                ch = typed[i]
                return (i > 0 and (typed[i - 1] == ch or _near_keys(typed[i - 1], ch))) or \
                    (i + 1 < lt and (typed[i + 1] == ch or _near_keys(typed[i + 1], ch)))
        return False
    if lw == lt + 1:          # one letter left out
        for i in range(lw):
            if word[:i] + word[i + 1:] == typed:
                ch = word[i]
                return ch in _VOWELS or (i > 0 and word[i - 1] == ch) or (i + 1 < lw and word[i + 1] == ch)
    return False


# ------------------------------------------------------------------------------------------------------ slip model
#: Costs of the slips people make when they type a word, as -log of how often (up to a constant; a lower cost is a
#: likelier slip). :func:`slip_cost` adds them up along the cheapest way from the intended word to the typed one; the
#: tool lookups (:meth:`Resolver.resolve_term`) rank close spellings by it (docs/resolver.md#slip-model).
SLIP_COSTS = {
    "sub_vowel": 2.5,       # another vowel typed (velora / velori)
    "sub_sound": 2.2,       # a letter of the same sound typed (c/k, s/z, v/w, i/y, b/v, z/j, d/t ...)
    "sub_near": 2.7,        # a neighbouring key typed
    "sub_other": 4.8,       # any other letter typed
    "ins_double": 2.5,      # a letter typed twice
    "ins_near": 2.9,        # a neighbouring key typed as well
    "ins_other": 4.0,       # any other letter added
    "del_double": 2.0,      # a doubled letter typed once
    "del_other": 2.2,       # a letter left out
    "swap": 1.6,            # two neighbouring letters swapped
    "spelling": 2.0,        # another spelling of the same sound (ph/f, ee/i, oo/u, x/ks, sh/s, th/t ...)
    "first": 0.8,           # added when the slip touches the first letter (people get it right more often)
}
#: Letter pairs of one sound (any order) for ``sub_sound``.
_SOUND_PAIRS = frozenset(frozenset(p) for p in ("ck", "sz", "vw", "iy", "qk", "jz", "bv", "dt", "fv", "gj", "cs"))
#: Spellings of one sound (``spelling``), from the sound-key rules: (intended, typed), both ways.
_SPELLINGS = tuple({(a, b) for x, y in (("ph", "f"), ("ee", "i"), ("oo", "u"), ("x", "ks"), ("ck", "k"), ("kh", "k"),
                                       ("sh", "s"), ("th", "t"), ("dh", "d"), ("bh", "b"), ("gh", "g"), ("aa", "a"),
                                       ("ou", "u"), ("ai", "e"), ("au", "o"), ("tch", "ch"), ("ksh", "ks"))
                    for a, b in ((x, y), (y, x))})
SLIP_SCALE = 0.03          #: confidence lost per unit of slip cost in a tool lookup ...
SLIP_TOP = 1.02            #: ... from this (a cost of 2.2, one letter left out, scores 0.954) ...
SLIP_CAP = 0.99            #: ... up to this
SLIP_MAX = 7.5             #: a spelling whose slips cost more than this is not a candidate of a tool lookup
SLIP_GAP = 0.4             #: a tool lookup corrects only when the runner-up's slips cost this much more
SOUND_SPELLING_COST = 2.2  #: slip cost of a word written as it sounds (same :func:`phonetic` form: secion/session)
STRICT_DISCOUNT = 0.05     #: a strict match keeps at least its strict confidence minus this in a tool lookup


def _slip_cost_raw(w: str, t: str, c: dict[str, float]) -> float:
    n, m = len(w), len(t)
    inf = float("inf")
    d = [[inf] * (m + 1) for _ in range(n + 1)]
    d[0][0] = 0.0
    spell_at = [[(a, b) for a, b in _SPELLINGS if w.startswith(a, i)] for i in range(n)]
    for i in range(n + 1):
        row = d[i]
        for j in range(m + 1):
            v = row[j]
            if v == inf:
                continue
            fp = c["first"] if i == 0 or j == 0 else 0.0
            if i < n and j < m:
                a, b = w[i], t[j]
                if a == b:
                    x = v
                elif a in _VOWELS and b in _VOWELS:
                    x = v + c["sub_vowel"] + fp
                elif frozenset((a, b)) in _SOUND_PAIRS:
                    x = v + c["sub_sound"] + fp
                elif _near_keys(a, b):
                    x = v + c["sub_near"] + fp
                else:
                    x = v + c["sub_other"] + fp
                if x < d[i + 1][j + 1]:
                    d[i + 1][j + 1] = x
                if i + 1 < n and j + 1 < m and a == t[j + 1] and w[i + 1] == b and a != b:
                    x = v + c["swap"] + fp
                    if x < d[i + 2][j + 2]:
                        d[i + 2][j + 2] = x
                for sa, sb in spell_at[i]:
                    if t.startswith(sb, j):
                        x = v + c["spelling"] + fp
                        if x < d[i + len(sa)][j + len(sb)]:
                            d[i + len(sa)][j + len(sb)] = x
            if i < n:           # w[i] left out
                a = w[i]
                k = c["del_double"] if (i and w[i - 1] == a) or (i + 1 < n and w[i + 1] == a) else c["del_other"]
                x = v + k + (c["first"] if i == 0 else 0.0)
                if x < d[i + 1][j]:
                    d[i + 1][j] = x
            if j < m:           # t[j] typed in addition
                b = t[j]
                prev = t[j - 1] if j else ""
                nxt = w[i] if i < n else ""
                if b == prev or b == nxt:
                    k = c["ins_double"]
                elif (prev and _near_keys(prev, b)) or (nxt and _near_keys(nxt, b)):
                    k = c["ins_near"]
                else:
                    k = c["ins_other"]
                x = v + k + (c["first"] if j == 0 else 0.0)
                if x < row[j + 1]:
                    row[j + 1] = x
    return d[n][m]


@functools.lru_cache(maxsize=1 << 16)
def slip_cost(intended: str, typed: str) -> float:
    """Noisy-channel cost of typing ``typed`` for ``intended`` (both lower-case words): the cheapest sum of
    :data:`SLIP_COSTS` over the slips that turn one into the other (another vowel or a neighbouring key typed, a
    letter left out, doubled or added, two letters swapped, another spelling of the same sound), plus ``first`` for a
    slip on the first letter. 0 for the same word. ``slip_cost("velora", "velra")`` = 2.2 (a letter left out);
    ``slip_cost("kelvo", "celvo")`` = 3.0 (a sound-alike first letter)."""
    if intended == typed:
        return 0.0
    cost = _slip_cost_raw(intended, typed, SLIP_COSTS)
    if cost > SOUND_SPELLING_COST and phonetic(intended) == phonetic(typed):
        cost = SOUND_SPELLING_COST
    return round(cost, 6)


def slip_conf(cost: float) -> float:
    """The confidence of a spelling whose slips cost ``cost`` (:data:`SLIP_TOP` - :data:`SLIP_SCALE` x cost)."""
    return SLIP_TOP - SLIP_SCALE * cost


def wide_edits(typed: str, word: str) -> int:
    """Edits a tool lookup may allow (wider than :func:`max_edits`; the extra matches are only did-you-means): 2 when
    both words have 5 letters or more, or when the typed word lacks two letters of a longer one; else 1."""
    return 2 if min(len(typed), len(word)) >= 5 or len(word) - len(typed) == 2 else 1


def _deletes(w: str, d: int) -> set[str]:
    out = {w}
    frontier = {w}
    for _ in range(d):
        nxt: set[str] = set()
        for x in frontier:
            if len(x) > 1:
                nxt.update(x[:i] + x[i + 1:] for i in range(len(x)))
        out |= nxt
        frontier = nxt
    return out


#: Whole affixes that turn a word into another word (``Uncompleted``/``Completed``, ``Trainee``/``Trainer``): never
#: a typo.
_AFFIX_PREFIXES = ("un", "in", "im", "ir", "il", "non", "dis", "bi", "re", "pre", "de", "mis", "sub")
_ROLE_SUFFIXES = ("ee", "er", "or")


def _affix_variant(a: str, b: str) -> bool:
    if a == b:
        return False
    for p in _AFFIX_PREFIXES:
        if a == p + b or b == p + a:
            return True
    for s1 in _ROLE_SUFFIXES:
        for s2 in _ROLE_SUFFIXES:
            if s1 != s2 and a.endswith(s1) and b.endswith(s2) and a[:-2] == b[:-2] and len(a) > 4:
                return True
    return False


# ------------------------------------------------------------------------------------------------- protected tokens
_DATE_RE = re.compile(r"^\d{1,4}([-/.]\d{1,2}){1,2}([-/.]\d{2,4})?$")
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_EMAIL_URL_RE = re.compile(r"\S+@\S+|\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ɏ_]+(?:['’][A-Za-z]+)?")
_API_LIKE_RE = re.compile(r"_|[a-z][A-Z]")
#: English suffixes a known word may carry (``handlers``, ``unassigned``); ``e_drop``: the stem may have lost an e
#: (``managing`` = manage + ing). Only these, so a typo such as ``completd`` or ``enginer`` is no "known" word.
_MORPH_SUFFIXES = ("ing", "ers", "er", "ed", "es", "s", "ly", "ment", "ments", "ness")
_E_DROP_SUFFIXES = ("ing", "ed")
_MORPH_PREFIXES = ("un", "re", "non", "pre", "sub", "in", "im", "dis", "bi")


def _protected(raw: str) -> str:
    """Why a raw token or text must never be corrected ('' when it may be)."""
    s = raw.strip()
    if not s:
        return "empty"
    if _DATE_RE.match(s):
        return "date"
    if re.fullmatch(r"[-+]?\d[\d,]*(\.\d+)?%?", s):
        return "number"
    if _ID_RE.match(s) and _HAS_DIGIT_RE.search(s):
        return "id"
    if _EMAIL_URL_RE.search(s):
        return "email or url"
    return ""


_STORED_SOURCES = ("stored", "stored_text")
#: Which text of one target survives when several sources give it (sorted before the duplicates are dropped).
_SOURCE_RANK = {"api": 0, "label": 1, "plural": 2, "picklist": 3, "picklist_label": 4, "stored": 5,
                "stored_text": 6, "full": 7, "first_last": 8, "first": 9, "last": 10}


def _order(kind: str, source: str) -> int:
    """Listing order of candidates of equal confidence: metadata first, people, then stored values (record data
    such as display names, which repeat person names and can hold typos)."""
    if kind == "value" and source in _STORED_SOURCES:
        return len(KINDS)
    return _KIND_RANK.get(kind, len(KINDS) + 1)


def _trank(tier: str) -> int:
    """Tier rank for ties: sound and edit count as one tier unless :data:`STRICT_TIERS`."""
    r = _TIER_RANK.get(tier, 9)
    if not STRICT_TIERS and tier == "edit":
        return _TIER_RANK["sound"]
    return r


# ---------------------------------------------------------------------------------------------------------- resolver
@dataclass(frozen=True)
class _WordMatch:
    word: str               # vocabulary word (normalised)
    tier: str               # exact | normalized | sound | edit
    conf: float
    edits: int
    ns: str = "any"         # "any": the typed form (all kinds); "meta": its singular (metadata terms only)
    same_phon: bool = False  # the phonetic forms are identical
    alt: bool = False       # found only by the second sound key
    wide: bool = False      # found only by the wider budget of a tool lookup (a did-you-mean, never corrected)
    cost: float = 0.0       # slip cost (tool lookups only)


@dataclass(frozen=True)
class _Align:
    conf: float
    tier: str
    edits: int
    pairs: tuple[tuple[int, int, _WordMatch], ...]
    extra: tuple[int, ...]      # text word indexes the term does not use
    missing: tuple[int, ...]    # term word indexes the text lacks


class Resolver:
    """Indexes of one :class:`Vocabulary`; read-only after :func:`build`, safe to share between threads."""

    def __init__(self, vocab: Vocabulary) -> None:
        # sort first, then drop duplicates, so the surviving source never depends on the order terms were added
        ordered = sorted(vocab.terms, key=lambda t: (_KIND_RANK.get(t.kind, 99), t.target, t.text,
                                                     _SOURCE_RANK.get(t.source, 50), t.source, -t.weight,
                                                     t.object or ""))
        terms: list[Term] = []
        seen: set[tuple[str, str, str]] = set()
        for t in ordered:
            k = (t.kind, t.target, t.text)
            if k not in seen:
                seen.add(k)
                terms.append(t)
        self.terms: tuple[Term, ...] = tuple(terms)
        self.info = dict(vocab.info)
        split_cache: dict[str, tuple[str, ...]] = {}
        for t in self.terms:
            if t.text not in split_cache:
                split_cache[t.text] = tuple(split_words(t.text))
        # known words (English word list, knowledge text), and the words a plural may stem to
        known_raw: set[str] = set()
        for w in vocab.known_words:
            if w.isascii() and w.isalpha():
                known_raw.add(w.lower())
            else:
                known_raw.update(split_words(w))
        stems: set[str] = set(known_raw)
        for ws in split_cache.values():
            stems.update(ws)
        self._sing_known = frozenset(stems)
        sing = self._sing
        self._words: list[tuple[str, ...]] = []
        by_key: dict[str, list[int]] = {}
        by_compact: dict[str, list[int]] = {}
        by_dedup: dict[str, list[int]] = {}
        word_terms: dict[str, list[int]] = {}
        word_kinds: dict[str, set[str]] = {}
        for i, t in enumerate(self.terms):
            raw_ws = split_cache[t.text]
            ws = raw_ws if t.kind == "person" else tuple(sing(w) for w in raw_ws)   # names are never singularised
            self._words.append(ws)
            if not ws:
                continue
            key = " ".join(ws)
            compact = "".join(ws)
            by_key.setdefault(key, []).append(i)
            if " " in key:
                by_compact.setdefault(compact, []).append(i)
            by_dedup.setdefault(dedup_letters(compact), []).append(i)
            for w in set(ws):
                word_terms.setdefault(w, []).append(i)
                word_kinds.setdefault(w, set()).add(t.kind)
        self._by_key = by_key
        self._by_compact = by_compact
        self._by_dedup = by_dedup
        self._word_terms = word_terms
        self._word_kinds = {w: frozenset(k) for w, k in word_kinds.items()}
        self._word_freq = {w: len(ids) for w, ids in word_terms.items()}
        weight: dict[str, float] = {}
        for t in vocab.terms:     # all of them: a stored value repeats a picklist value with its record count
            weight[t.target] = max(weight.get(t.target, 0.0), t.weight)
        self._target_weight = weight
        first_words: set[str] = set()
        last_words: set[str] = set()
        for i, t in enumerate(self.terms):
            if t.kind == "person" and len(self._words[i]) == 1:
                (first_words if t.source == "first" else last_words if t.source == "last" else set()).add(
                    self._words[i][0])
        self._person_first = frozenset(first_words)
        self._person_last = frozenset(last_words)
        words_by_dedup: dict[str, list[str]] = {}
        words_by_sound: dict[str, list[str]] = {}
        words_by_alt: dict[str, list[str]] = {}
        deletes: dict[str, list[str]] = {}
        longest = 0
        for w in sorted(word_terms):
            if not w.isalpha() or len(w) < 3:
                continue
            longest = max(longest, len(w))
            words_by_dedup.setdefault(dedup(w), []).append(w)
            if len(w) < MIN_FUZZY_LEN or w in STOPWORDS:
                continue          # matched exactly or by doubled letters only, never suggested by sound or edits
            words_by_sound.setdefault(sound_key(w), []).append(w)
            ak = alt_sound_key(w)
            if ak != sound_key(w):
                words_by_alt.setdefault(ak, []).append(w)
            for x in _deletes(w, 1 if len(w) <= 4 else 2):
                deletes.setdefault(x, []).append(w)
        self._words_by_dedup = words_by_dedup
        self._words_by_sound = words_by_sound
        self._words_by_alt = words_by_alt
        self._deletes = deletes
        self._max_len = longest + LEN_SLACK
        self._max_words = max(MAX_TEXT_WORDS, max((len(ws) for ws in self._words), default=0) + 2)
        known: set[str] = set(STOPWORDS)
        for w in known_raw:
            known.add(w)
            known.add(sing(w))
        self._known = frozenset(known)
        # words only rare stored values use, one edit from a correct word: data-entry typos (``deliverble``) that
        # must not make the same typo in a question look correct
        weak: set[str] = set()
        for w, ids in word_terms.items():
            if len(w) < MIN_FUZZY_LEN or not w.isalpha() or w in known:
                continue
            if all(self.terms[i].kind == "value" and self.terms[i].source in _STORED_SOURCES for i in ids) and \
                    sum(weight.get(self.terms[i].target, 0.0) for i in ids) < WEAK_STORED_MAX and \
                    self._near_correct(w):
                weak.add(w)
        self._weak_words = frozenset(weak)

    # ------------------------------------------------------------------------------------------------- word level
    def _near_correct(self, w: str) -> bool:
        """One edit from a known word or a metadata word."""
        letters = "abcdefghijklmnopqrstuvwxyz"
        cands = {w[:i] + w[i + 1:] for i in range(len(w))}
        cands |= {w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(len(w) - 1)}
        cands |= {w[:i] + c + w[i:] for i in range(len(w) + 1) for c in letters}
        cands |= {w[:i] + c + w[i + 1:] for i in range(len(w)) for c in letters}
        cands.discard(w)
        return any(c in self._known or any(self.terms[i].kind != "value" for i in self._word_terms.get(c, ()))
                   for c in cands)

    def _sing(self, w: str) -> str:
        return singular(w, self._sing_known)

    def key(self, text: Any, person: bool = False) -> str:
        """The lookup key of a text (its normalised words; person names are not singularised)."""
        ws = split_words(text)
        return " ".join(ws if person else [self._sing(w) for w in ws])

    def is_vocabulary_word(self, word: str) -> bool:
        w = word.lower()
        return w in self._word_terms or self._sing(w) in self._word_terms

    def is_known_word(self, word: str) -> bool:
        """True when ``word`` is already correct: vocabulary, English word list, knowledge text, a function word, or
        one of them with a common English prefix or suffix (``handlers``, ``unassigned``, ``managing``)."""
        raw = word.lower()
        w = self._sing(raw)
        ok = self._is_known_stem
        if ok(w) or ok(raw):
            return True
        for suf in _MORPH_SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                stem = w[: -len(suf)]
                if ok(stem) or (suf in _E_DROP_SUFFIXES and ok(stem + "e")) \
                        or (len(stem) > 3 and stem[-1] == stem[-2] and suf in _E_DROP_SUFFIXES and ok(stem[:-1])):
                    return True
        for pre in _MORPH_PREFIXES:
            if w.startswith(pre) and len(w) - len(pre) >= 4 and ok(w[len(pre):]):
                return True
        return False

    def _is_known_stem(self, w: str) -> bool:
        return w in self._word_terms or w in self._known

    def _word_ok(self, w: str, kinds: frozenset[str] | None) -> bool:
        return kinds is None or bool(self._word_kinds.get(w, frozenset()) & kinds)

    def _exact_options(self, raw: str, ks: frozenset[str] | None) -> list[_WordMatch]:
        """The typed form matches every kind; its singular, when different, metadata only (``Kelms`` is no
        ``Kelm``)."""
        out = []
        if raw in self._word_terms and self._word_ok(raw, ks):
            out.append(_WordMatch(raw, "exact", 1.0, 0, "any", True))
        s = self._sing(raw)
        mk = _META_KINDS if ks is None else ks & _META_KINDS
        if s != raw and mk and s in self._word_terms and self._word_ok(s, mk):
            out.append(_WordMatch(s, "exact", 1.0, 0, "meta", True))
        return out

    def word_candidates(self, word: str, kinds: Iterable[str] | None = None,
                        fuzzy_only: bool = False, wide: bool = False) -> list[_WordMatch]:
        """Vocabulary words close to one word, best first. A vocabulary word matches only itself. Otherwise the
        doubled-letter matches (``normalized``) rank first, then the sound-key and edit matches, scored together
        (:data:`STRICT_TIERS`); all of them are returned so a phrase can still pick a lower one. The typed form is
        compared with every word; its singular (when different) with metadata words only. ``wide`` (tool lookups):
        every match is scored by its slip cost (:func:`slip_cost`), and words up to :func:`wide_edits` away whose
        slips cost at most :data:`SLIP_MAX` are added as did-you-means (``wide``)."""
        ks = frozenset(kinds) if kinds is not None else None
        raw = word.lower()
        exact = self._exact_options(raw, ks)
        if exact and not fuzzy_only:
            return exact
        if len(raw) < MIN_FUZZY_LEN or not raw.isalpha() or len(raw) > self._max_len:
            return []
        found: dict[tuple[str, str], _WordMatch] = {}
        self._fuzzy_into(found, raw, ks, "any", wide)
        s = self._sing(raw)
        mk = _META_KINDS if ks is None else ks & _META_KINDS
        if s != raw and mk and len(s) >= MIN_FUZZY_LEN:
            self._fuzzy_into(found, s, mk, "meta", wide)
        best: dict[str, _WordMatch] = {}
        for (w, ns), m in found.items():
            b = best.get(w)
            if b is None or (m.conf, m.ns == "any") > (b.conf, b.ns == "any"):
                best[w] = m
        return sorted(best.values(), key=lambda m: (-m.conf, m.edits, -self._word_freq.get(m.word, 0), m.word,
                                                    m.ns))[:MAX_WORD_CANDIDATES]

    def _fuzzy_into(self, found: dict[tuple[str, str], _WordMatch], q: str, ks: frozenset[str] | None,
                    ns: str, wide: bool = False) -> None:
        strict: dict[tuple[str, str], _WordMatch] = {}
        for w in self._words_by_dedup.get(dedup(q), ()):
            if w != q and self._word_ok(w, ks) and w not in STOPWORDS:
                e = osa_distance(q, w, 3)
                strict[(w, ns)] = _WordMatch(w, "normalized", NORMALIZED_CONF - 0.001 * e, e, ns, True)
        sk = sound_key(q)
        sound = False
        pq = phonetic(q)
        for w in self._words_by_sound.get(sk, ()):
            if w == q or (w, ns) in strict or not self._word_ok(w, ks):
                continue
            lim = max_edits(q, w)
            e = osa_distance(q, w, lim)
            pw = phonetic(w)
            if e > lim:
                # more edits than a spelling match may have: only when the words sound the same (zalaxi, zalakshi)
                if pq != pw:
                    continue
                e = lim
            # judged by spelling or by sound, whichever is closer (ph/f, ee/i, sh/s ...)
            jw = max(jaro_winkler(q, w), jaro_winkler(pq, pw))
            if jw < SOUND_JW_MIN:
                continue
            strict[(w, ns)] = _WordMatch(w, "sound", self._fuzzy_conf(q, w, jw, e, True), e, ns, pq == pw)
            sound = True
        pool: set[str] = set()
        if wide or not (STRICT_TIERS and sound):
            for x in _deletes(q, 2 if wide and len(q) >= 5 else 1 if len(q) <= 5 else 2):
                pool.update(self._deletes.get(x, ()))
        if not (STRICT_TIERS and sound):
            for w in sorted(pool):
                if w == q or (w, ns) in strict or not self._word_ok(w, ks):
                    continue
                lim = max_edits(q, w)
                e = osa_distance(q, w, lim)
                if e > lim:
                    continue
                jw = jaro_winkler(q, w)
                if jw < (JW_MIN_LONG if len(q) >= 8 else JW_MIN):
                    continue
                strict[(w, ns)] = _WordMatch(w, "edit", self._fuzzy_conf(q, w, jw, e, sound_key(w) == sk), e, ns,
                                             phonetic(w) == pq)
        # second sound key (b/v, z/j): only words that sound identical once those pairs are merged
        apq = phonetic(_alt_fold(q))
        for w in self._words_by_alt.get(alt_sound_key(q), ()):
            if w == q or (w, ns) in strict or not self._word_ok(w, ks) or phonetic(_alt_fold(w)) != apq:
                continue
            lim = max_edits(q, w)
            e = min(osa_distance(q, w, lim), lim)
            jw = max(jaro_winkler(q, w), jaro_winkler(pq, phonetic(w)))
            if jw < SOUND_JW_MIN:
                continue
            strict[(w, ns)] = _WordMatch(w, "sound", self._fuzzy_conf(q, w, jw, e, False) - ALT_SOUND_PENALTY, e,
                                         ns, False, True)
        if not wide:
            found.update(strict)
            return
        # tool lookup: every match scored by its slips, plus the wider did-you-means
        for k, m in strict.items():
            # the slip model ranks; the strict score, a little lower, still vouches for a spelling the slips cannot
            # explain (two slips on one letter)
            cost = slip_cost(m.word, q)
            found[k] = _WordMatch(m.word, m.tier, max(self._slip_conf(m.word, cost), m.conf - STRICT_DISCOUNT),
                                  m.edits, ns, m.same_phon, m.alt, False, cost)
        for w in sorted(pool):
            if w == q or (w, ns) in strict or not self._word_ok(w, ks) or len(w) < MIN_FUZZY_LEN \
                    or w in STOPWORDS:
                continue
            lim = wide_edits(q, w)
            e = osa_distance(q, w, lim)
            if e > lim:
                continue
            cost = slip_cost(w, q)
            if cost > SLIP_MAX:
                continue
            found[(w, ns)] = _WordMatch(w, "edit", self._slip_conf(w, cost), e, ns, phonetic(w) == pq, False, True,
                                        cost)

    def _slip_conf(self, w: str, cost: float) -> float:
        """Confidence of a tool-lookup match from its slip cost, plus the common-word tie-breaker."""
        return min(SLIP_CAP, slip_conf(cost) + min(FREQ_CAP, FREQ_WEIGHT * math.log10(1 + self._word_freq.get(w, 0))))

    def _fuzzy_conf(self, q: str, w: str, jw: float, edits: int, same_sound: bool) -> float:
        """Confidence of a sound or edit match: mostly Jaro-Winkler, plus small bonuses (same sound key, one edit,
        same first letter, a word used by many terms). Always below a doubled-letter match."""
        c = 0.88 * jw
        if same_sound:
            c += SOUND_BONUS
        if edits <= 1:
            c += ONE_EDIT_BONUS
            if edits == 1 and _likely_slip(q, w):
                c += LIKELY_EDIT_BONUS
        if q[0] == w[0]:
            c += 0.01
        c += min(FREQ_CAP, FREQ_WEIGHT * math.log10(1 + self._word_freq.get(w, 0)))
        return min(FUZZY_CAP, c)

    # ------------------------------------------------------------------------------------------------- term level
    def resolve_term(self, text: Any, kinds: Iterable[str] | None = None, context_object: str | None = None,
                     within: str | None = None, limit: int = 10, *, wide: bool = True) -> Resolution:
        """Resolve one term (a tool argument). ``kinds``: only terms of these kinds; ``context_object``: an object API
        name (or a target such as ``Obj.Field``) whose terms rank first; ``within``: only targets starting with this
        prefix (the values of one field: ``"Obj.Field="``). ``wide`` (the default, a tool lookup): close spellings
        are ranked by their slip cost (:func:`slip_cost`) and may be up to :func:`wide_edits` away, the ones beyond
        :func:`max_edits` only as did-you-means, and one clear but distant spelling is a did-you-mean too.
        ``wide=False``: the strict budget and scores that :meth:`spell_correct` uses (for example to expand search
        words)."""
        ks = frozenset(kinds) if kinds is not None else None
        if ks is not None and not ks <= set(KINDS):
            raise ValueError(f"unknown kinds {sorted(ks - set(KINDS))}; use {KINDS}")
        return self._resolve(str(text if text is not None else ""), ks, context_object, within, limit, wide)

    def _resolve(self, raw: str, ks: frozenset[str] | None, context_object: str | None, within: str | None,
                 limit: int, wide: bool) -> Resolution:
        """:meth:`resolve_term`; ``wide`` False (inside :meth:`spell_correct`): the strict budget and scores only."""
        why = _protected(raw)
        rw = split_words(raw)
        if len(rw) > self._max_words:
            return Resolution(raw, "none", None, (), (), None, 0.0, "too many words")
        sw = [self._sing(w) for w in rw]
        if why or not rw:
            exact = self._ids_exact(rw, sw, ks, within) if rw and why not in ("email or url",) else []
            if exact:
                return self._result(raw, "exact", exact, "exact", 1.0, context_object, limit, style=raw)
            return Resolution(raw, "none", None, (), (), None, 0.0, f"protected: {why}" if why else "no words")
        ids = self._ids_exact(rw, sw, ks, within)
        if ids:
            return self._result(raw, "exact", ids, "exact", 1.0, context_object, limit, style=raw)
        core = self._core(rw, sw)
        dropped = tuple(w for i, w in enumerate(rw) if i not in core)
        crw = [rw[i] for i in core]
        csw = [sw[i] for i in core]
        use_core = bool(dropped) and bool(core)
        if use_core:
            ids = self._ids_exact(crw, csw, ks, within)
            if ids:
                return self._result(raw, "exact", ids, "exact", 1.0, context_object, limit, style=raw,
                                    extra=dropped, reason="ignored: " + " ".join(dropped))
        res = self._normalized(raw, rw, sw, ks, within, context_object, limit)
        if res is None and use_core:
            res = self._normalized(raw, crw, csw, ks, within, context_object, limit, extra=dropped)
        if res is not None:
            return self._same_numbers(raw, res)
        res = self._fuzzy_term(raw, rw, sw, ks, context_object, within, limit, wide=wide)
        if use_core and res.decision != "corrected":
            alt = self._fuzzy_term(raw, crw, csw, ks, context_object, within, limit, dropped, wide)
            rank = {"corrected": 0, "ambiguous": 1, "none": 2}
            if rank[alt.decision] < rank[res.decision] or (alt.decision == res.decision == "none"
                                                           and alt.candidates and not res.candidates):
                res = alt
        return self._same_numbers(raw, res)

    @staticmethod
    def _same_numbers(raw: str, res: Resolution) -> Resolution:
        """A ``corrected`` reading whose numbers differ from the text's (:func:`numbers`: ``'01:55 PM'`` read as
        ``01:15 PM``, ``'Rond'`` as ``Round 2``) names another value, not a spelling: it is only a did-you-mean
        (``ambiguous``). Every other result is returned as it is."""
        if res.decision != "corrected" or numbers(raw) == numbers(res.suggestion or ""):
            return res
        return replace(res, decision="ambiguous", suggestion=None, reason="not corrected: another number")

    @staticmethod
    def _core(rw: list[str], sw: list[str]) -> list[int]:
        """Indexes of the words left once filler words (:data:`FILLERS`) are dropped from both edges."""
        lo, hi = 0, len(rw)
        while lo < hi and (rw[lo] in FILLERS or sw[lo] in FILLERS):
            lo += 1
        while hi > lo and (rw[hi - 1] in FILLERS or sw[hi - 1] in FILLERS):
            hi -= 1
        return list(range(lo, hi))

    def _normalized(self, raw: str, rw: list[str], sw: list[str], ks: frozenset[str] | None, within: str | None,
                    context: str | None, limit: int, extra: tuple[str, ...] = ()) -> Resolution | None:
        """Doubled-letter matches: the spelling(s) fewest edits away win; several at the same distance: ambiguous.
        A typed word that is itself a correct word is never corrected (``dinner`` is no ``diner``)."""
        rc, sc = "".join(rw), "".join(sw)
        if len(rc) < MIN_FUZZY_LEN:
            return None
        ids = [i for i in self._by_dedup.get(dedup_letters(rc), ()) if self.terms[i].kind == "person"
               and self._may_join(i, len(rw))]
        ids += [i for i in self._by_dedup.get(dedup_letters(sc), ()) if self.terms[i].kind != "person"]
        ids = self._filter(dict.fromkeys(ids), ks, within)
        if _HAS_DIGIT_RE.search(raw) or any(_HAS_DIGIT_RE.search(self.terms[i].text) for i in ids):
            typed = numbers(raw)   # the same numbers only: '1:30 PM' is no doubled-letter slip of '11:30 PM'
            ids = [i for i in ids if numbers(self.terms[i].text) == typed]
        if not ids:
            return None
        groups: dict[tuple[str, bool], list[int]] = {}
        for i in ids:
            groups.setdefault((" ".join(self._words[i]), self.terms[i].kind == "person"), []).append(i)

        def dist(k: tuple[str, bool]) -> tuple[int, int, int]:
            q = rw if k[1] else sw
            key = " ".join(q)
            return (osa_distance("".join(q), k[0].replace(" ", ""), 9), abs(len(q) - k[0].count(" ") - 1),
                    osa_distance(key, k[0], 9))
        d = {k: dist(k) for k in groups}
        best = min(d.values())
        keys = sorted(k for k in groups if d[k] == best)
        spellings = sorted({k[0] for k in keys})
        if len(spellings) == 1:
            all_ids = [i for k in keys for i in groups[k]]
            kw = set(spellings[0].split())
            changed = [w for w, x in zip(rw, sw) if w not in kw and x not in kw]
            if any(self.is_known_word(w) for w in changed):
                res = self._result(raw, "ambiguous", all_ids, "normalized", NORMALIZED_CONF, context, limit,
                                   style=raw, extra=extra, reason="not corrected: a correct word")
                return res
            return self._result(raw, "corrected", all_ids, "normalized", NORMALIZED_CONF, context, limit, style=raw,
                                extra=extra)
        by_spelling: dict[str, list[int]] = {}
        for k in keys:
            by_spelling.setdefault(k[0], []).extend(groups[k])
        cands = [Candidate(self.terms[i].kind, self.terms[i].target, self.terms[i].text, raw, "normalized",
                           NORMALIZED_CONF + self._boost(self.terms[i], context), self.terms[i].object,
                           self.terms[i].source) for s in spellings for i in by_spelling[s]]
        surf = tuple(self._surface(by_spelling[s], raw) for s in spellings[:5])
        return Resolution(raw, "ambiguous", None, surf,
                          tuple(self._dedup_targets(self._rank(cands, context))[:limit]), "normalized",
                          NORMALIZED_CONF, "", extra)

    def _filter(self, ids: Iterable[int], ks: frozenset[str] | None, within: str | None) -> list[int]:
        return [i for i in ids if (ks is None or self.terms[i].kind in ks)
                and (within is None or self.terms[i].target.startswith(within))]

    def _ids_exact(self, rw: list[str], sw: list[str], ks: frozenset[str] | None, within: str | None) -> list[int]:
        """Exact matches: person terms by the typed words, metadata terms by their singulars; the words written
        together also count (``gadgetorder``, ``InterViews``)."""
        ids: dict[int, None] = {}
        for words, person in ((rw, True), (sw, False)):
            key, compact = " ".join(words), "".join(words)
            for i in self._by_key.get(key, ()):
                if (self.terms[i].kind == "person") == person:
                    ids[i] = None
            joined = list(self._by_compact.get(compact, ()))
            if len(words) > 1:
                joined += self._by_key.get(compact, ())
            for i in joined:
                if (self.terms[i].kind == "person") == person and self._may_join(i, len(words)):
                    ids[i] = None
        return self._filter(ids, ks, within)

    def _may_join(self, i: int, n_words: int) -> bool:
        """A text written with other word breaks may match term ``i``, except a person name with an initial (``kora``
        is no ``Kor A``)."""
        ws = self._words[i]
        return self.terms[i].kind != "person" or len(ws) == n_words or min(len(w) for w in ws) >= 3

    @staticmethod
    def _in_context(t_object: str | None, target: str, context: str | None) -> bool:
        """A term belongs to the context: its object is it, or its target is it or lies under it (``Obj.Field`` and
        ``Obj.Field=value``; never ``Obj.Field_Other__c``)."""
        if not context:
            return False
        return t_object == context or target == context or target.startswith((context + ".", context + "="))

    def _boost(self, t: Term, context: str | None) -> float:
        return CONTEXT_BOOST if self._in_context(t.object, t.target, context) else 0.0

    def _surface(self, ids: list[int], style: str) -> str:
        """The spelling to suggest for a group of terms of one key: the API name for an API-like input, else a
        label (object label first), else the most common text."""
        api_like = bool(_API_LIKE_RE.search(style.strip()))
        order = {"label": 0, "plural": 1, "picklist_label": 2, "picklist": 3, "stored": 4, "stored_text": 5,
                 "full": 6}

        def rank(i: int) -> tuple:
            t = self.terms[i]
            api = t.source == "api"
            return (0 if api == api_like else 1, _order(t.kind, t.source), order.get(t.source, 7), t.text)
        return self.terms[min(ids, key=rank)].text

    def _result(self, raw: str, decision: str, ids: list[int], tier: str, conf: float, context: str | None,
                limit: int, style: str, extra: tuple[str, ...] = (), reason: str = "") -> Resolution:
        cands = [Candidate(self.terms[i].kind, self.terms[i].target, self.terms[i].text, raw, tier,
                           min(1.0, conf + self._boost(self.terms[i], context)), self.terms[i].object,
                           self.terms[i].source) for i in ids]
        cands = self._rank(cands, context)
        sugg = self._surface(ids, style)
        exactish = decision in ("exact", "corrected")
        return Resolution(raw, decision, sugg if exactish else None, (sugg,) if sugg else (),
                          tuple(self._dedup_targets(cands)[:limit]), tier, conf, reason, extra)

    def _rank(self, cands: list[Candidate], context: str | None = None) -> list[Candidate]:
        """Best first: confidence, the context object, tier, kind (stored values last), records holding the value."""
        return sorted(cands, key=lambda c: (-c.confidence, 0 if self._in_context(c.object, c.target, context) else 1,
                                            _TIER_RANK.get(c.tier, 9), _order(c.kind, c.source),
                                            -self._target_weight.get(c.target, 0.0), c.target, c.text))

    @staticmethod
    def _dedup_targets(cands: list[Candidate]) -> list[Candidate]:
        out, seen = [], set()
        for c in cands:
            if c.target not in seen:
                seen.add(c.target)
                out.append(c)
        return out

    def _options(self, raw: str, ks: frozenset[str] | None, wide: bool = False) -> list[_WordMatch]:
        exact = self._exact_options(raw, ks)
        if exact:
            return exact
        if _HAS_DIGIT_RE.search(raw) or len(raw) < MIN_FUZZY_LEN:
            return []
        return self.word_candidates(raw, ks, wide=wide)

    def _fuzzy_term(self, raw: str, rw: list[str], sw: list[str], ks: frozenset[str] | None, context: str | None,
                    within: str | None, limit: int, dropped: tuple[str, ...] = (), wide: bool = False) -> Resolution:
        options = [self._options(w, ks, wide) for w in rw]
        # terms sharing a (close) word with the text, most shared words first
        hits: dict[int, set[int]] = {}
        for qi, opts in enumerate(options):
            for m in opts:
                for tid in self._word_terms.get(m.word, ()):
                    if m.ns == "meta" and self.terms[tid].kind == "person":
                        continue
                    hits.setdefault(tid, set()).add(qi)
        if not hits:
            return Resolution(raw, "none", None, (), (), None, 0.0, "no close vocabulary", dropped)
        tids = self._filter(hits, ks, within)
        tids.sort(key=lambda i: (-len(hits[i]), abs(len(self._words[i]) - len(rw)), i))
        scored: list[tuple[float, str, int, str]] = []    # (conf, tier, term id, key)
        best: dict[str, tuple[float, str, _Align]] = {}
        person_key: dict[str, bool] = {}
        cache: dict[tuple[tuple[str, ...], bool], _Align | None] = {}
        direct: dict[tuple[str, str], _WordMatch | None] = {}
        for tid in tids[:MAX_PHRASE_TERMS]:
            person = self.terms[tid].kind == "person"
            ck = (self._words[tid], person)
            if ck not in cache:
                cache[ck] = self._align(rw, sw, options, self._words[tid], person, direct, wide)
            al = cache[ck]
            if al is None or al.conf < WEAK_MIN:
                continue
            conf = min(FUZZY_CAP, al.conf + self._boost(self.terms[tid], context))
            key = " ".join(self._words[tid])
            scored.append((conf, al.tier, tid, key))
            person_key[key] = person_key.get(key, False) or person
            b = best.get(key)
            if b is None or (conf, -_TIER_RANK[al.tier], -al.edits) > (b[0], -_TIER_RANK[b[1]], -b[2].edits):
                best[key] = (conf, al.tier, al)
        if not scored:
            return Resolution(raw, "none", None, (), (), None, 0.0, "no close vocabulary", dropped)
        keys = sorted(best, key=lambda k: (-best[k][0], _trank(best[k][1]), best[k][2].edits, k))
        top_conf, top_tier, top_al = best[keys[0]]
        # tool lookup, every word aligned: the slip cost already counts the edits, so a tie is a runner-up within
        # SLIP_GAP of it (a phrase with words missing or extra keeps the usual margins)
        margin, tie = (SLIP_SCALE * SLIP_GAP, SLIP_SCALE * SLIP_GAP) if wide and top_tier != "phrase" \
            else (MARGIN, TIE_MARGIN)
        close = [k for k in keys if best[k][0] >= top_conf - margin]
        for k in keys[1:]:
            if k in close or best[k][0] < top_conf - tie:
                continue
            ek, e0 = best[k][2].edits, top_al.edits
            if (ek <= e0 and _trank(best[k][1]) <= _trank(top_tier)) or ek < e0:
                close.append(k)      # a tie: as near (or fewer edits), no worse tier
        fuzzy_spellings = sum(1 for k in keys if best[k][0] >= AMBIGUOUS_MIN)
        loose = fuzzy_spellings >= LOOSE_MANY and top_conf < LOOSE_CONF
        reason = ""
        if top_conf >= CORRECT_MIN and len(close) == 1 and not loose:
            decision = "corrected"
            cap = self._cap(rw, top_al, keys[0].split(), person_key[keys[0]])
            if cap:
                decision, reason = "ambiguous", "not corrected: " + cap
        elif top_conf >= AMBIGUOUS_MIN and (len(close) > 1 or loose or top_conf >= CORRECT_MIN):
            decision = "ambiguous"
        elif top_conf >= AMBIGUOUS_MIN and wide:
            # a tool lookup lists one clear but distant spelling as a did-you-mean
            decision = "ambiguous"
            reason = "not corrected: " + (self._cap(rw, top_al, keys[0].split(), person_key[keys[0]])
                                          or "too far for a correction")
        elif top_conf >= PHRASE_SUGGEST_MIN and len(top_al.pairs) >= 2 \
                and sum(1 for _, _, m in top_al.pairs if m.tier == "exact") >= len(rw) - 1:
            decision = "ambiguous"   # every word but one is a word of the term: a did-you-mean
        else:
            decision = "none"
            reason = "weak match only"
        shown = close[:5] if decision in ("corrected", "ambiguous") else keys[:5]
        key_ids: dict[str, list[int]] = {}
        for conf, tier, tid, key in scored:
            key_ids.setdefault(key, []).append(tid)
        cands = [Candidate(self.terms[tid].kind, self.terms[tid].target, self.terms[tid].text, raw, tier, conf,
                           self.terms[tid].object, self.terms[tid].source)
                 for conf, tier, tid, key in scored if key in shown]
        cands = self._dedup_targets(self._rank(cands, context))
        if decision == "corrected":
            chosen = {self.terms[i].target for i in key_ids[keys[0]]}
            cands = [c for c in cands if c.target in chosen] or cands
        surf = [self._surface(key_ids[k], raw) for k in shown]
        sugg = surf[0] if decision == "corrected" else None
        t_words = keys[0].split()
        extra = dropped + tuple(rw[qi] for qi in top_al.extra)
        missing = tuple(t_words[tj] for tj in top_al.missing)
        return Resolution(raw, decision, sugg, tuple(surf), tuple(cands[:limit]), top_tier, top_conf, reason,
                          extra, missing)

    def _cap(self, rw: list[str], al: _Align, t_words: list[str], person: bool) -> str:
        """Why one clear best match must still be confirmed (a did-you-mean), or '' when it may be used directly."""
        if any(t_words[tj] not in CONNECTORS for tj in al.missing):
            return "words missing"
        if any(rw[qi] not in CONNECTORS for qi in al.extra):
            return "extra words"
        changed = [(qi, m) for qi, _, m in al.pairs if m.tier != "exact"]
        for qi, m in changed:
            q = rw[qi]
            if self.is_known_word(q):
                return "a correct word"
            if m.wide:
                return "too far for a correction"
            if _affix_variant(q, m.word) or _affix_variant(self._sing(q), m.word):
                return "another word (affix)"
            if m.alt:
                return "second sound key"
            if person and sound_key(q)[:1] != sound_key(m.word)[:1]:
                return "first sound changed"
        if person and len(t_words) == 1 and changed:
            m = changed[0][1]
            if m.tier != "normalized" and not m.same_phon:
                return "single name"
        return ""

    def _direct(self, q: str, w: str, cache: dict[tuple[str, str], _WordMatch | None],
                wide: bool = False) -> _WordMatch | None:
        """A text word against one word of a term whose other words matched (the phrase anchors it, so 3-letter
        words and a lower Jaro-Winkler floor are allowed)."""
        k = (q, w)
        if k in cache:
            return cache[k]
        m = None
        if q.isalpha() and w.isalpha() and len(q) >= 3 and len(w) >= 3 and abs(len(q) - len(w)) <= 2 \
                and len(q) <= self._max_len:
            lim = max(max_edits(q, w), ANCHORED_EDITS if len(q) >= 5 and len(w) >= 5 else 1)
            e = osa_distance(q, w, lim)
            if e <= lim:
                jw = jaro_winkler(q, w)
                if jw >= ANCHORED_JW_MIN:
                    if wide:
                        cost = slip_cost(w, q)
                        m = _WordMatch(w, "edit", self._slip_conf(w, cost) - 0.02, e, "any",
                                       phonetic(q) == phonetic(w), False, False, cost)
                    else:
                        m = _WordMatch(w, "edit", self._fuzzy_conf(q, w, jw, e, sound_key(q) == sound_key(w)) - 0.02,
                                       e, "any", phonetic(q) == phonetic(w))
            elif dedup(q) == dedup(w):
                m = _WordMatch(w, "normalized", NORMALIZED_CONF, e, "any", True)
        cache[k] = m
        return m

    def _align(self, rw: list[str], sw: list[str], options: list[list[_WordMatch]], t: tuple[str, ...],
               person: bool, direct: dict[tuple[str, str], _WordMatch | None] | None = None,
               wide: bool = False) -> _Align | None:
        """Alignment of a term's words ``t`` with the text words: each text word takes at most one term word (best
        similarity first); unmatched text words cost 1, unmatched term words 0.5 (connector words such as ``of``:
        :data:`CONNECTOR_COST` either way), a changed order 5%. Text words with no close vocabulary word in ``t`` are
        compared with ``t`` 's words directly when the term shares another word with the text."""
        q = rw if person else sw
        sims: list[tuple[float, int, int, _WordMatch]] = []
        matched_q: set[int] = set()
        sure_q: set[int] = set()      # matched by a spelling within the strict budget
        for qi, opts in enumerate(options):
            for m in opts:
                if person and m.ns == "meta":
                    continue
                for tj, tw in enumerate(t):
                    if tw == m.word:
                        sims.append((m.conf, qi, tj, m))
                        matched_q.add(qi)
                        if not m.wide:
                            sure_q.add(qi)
        if direct is not None and matched_q and len(q) > 1:
            # a tool lookup also compares a word matched only by wide did-you-means with the term's words (a
            # stopword such as "before" is never a fuzzy candidate, but the phrase anchors it)
            skip = sure_q if wide else matched_q
            for qi, w in enumerate(q):
                if qi in skip:
                    continue
                have = {tj for _, i, tj, _ in sims if i == qi}
                for tj, tw in enumerate(t):
                    if tj in have:
                        continue
                    m = self._direct(w, tw, direct, wide)
                    if m is not None:
                        sims.append((m.conf, qi, tj, m))
        sims.sort(key=lambda x: (-x[0], x[1], x[2]))
        used_q: set[int] = set()
        used_t: set[int] = set()
        pairs: list[tuple[int, int, _WordMatch]] = []
        total = 0.0
        for conf, qi, tj, m in sims:
            if qi in used_q or tj in used_t:
                continue
            used_q.add(qi)
            used_t.add(tj)
            pairs.append((qi, tj, m))
            total += conf
        if not pairs:
            return None
        p = len(pairs)
        extra = tuple(qi for qi in range(len(q)) if qi not in used_q)
        missing = tuple(tj for tj in range(len(t)) if tj not in used_t)
        cost_q = sum(CONNECTOR_COST if q[qi] in CONNECTORS else 1.0 for qi in extra)
        cost_t = sum(CONNECTOR_COST if t[tj] in CONNECTORS else 0.5 for tj in missing)
        edits = sum(m.edits for _, _, m in pairs) + len(extra) + len(missing)
        conf = total / (p + cost_q + cost_t)
        pairs.sort()
        in_order = all(pairs[i][1] < pairs[i + 1][1] for i in range(p - 1))
        if not in_order:
            conf *= 0.95
        tier = "phrase"
        if p == len(q) == len(t) and in_order:
            tiers = [m.tier for _, _, m in pairs if m.tier != "exact"]
            if not tiers:
                tier = "exact"
            else:
                worst = max(tiers, key=lambda x: _TIER_RANK[x])
                budget = 1 if sum(len(w) for w in q) <= 5 else 2
                if worst in ("normalized", "sound") or edits <= budget:
                    tier = worst
        return _Align(conf, tier, edits, tuple(pairs), extra, missing)

    # ---------------------------------------------------------------------------------------------- spell_correct
    def spell_correct(self, question: Any, include_exact: bool = False, max_window: int = 5) -> list[Correction]:
        """Misspelt words and phrases of a question, left to right. A word is looked at only when it is not already
        correct (vocabulary, English word list, knowledge text, function word), has 4 letters or more, is no longer
        than any vocabulary word, and is no number, date, Id, e-mail or URL. A run of words that, once corrected,
        spells a whole vocabulary term (``backgrond chech`` -> ``background check``, ``dvor quell`` -> a full name)
        is one correction, and decides between the close spellings of its words; when the next word belongs to every
        longer term too (``internl intrview logs``), the run is looked up with it. Two or three correct name words
        that are no stored name together (a misspelt surname that is another stored surname) are looked up as one
        name. ``include_exact``: also list the vocabulary terms spelt correctly (longest first)."""
        q = str(question if question is not None else "")
        masked = [(m.start(), m.end()) for m in _EMAIL_URL_RE.finditer(q)]
        toks: list[dict[str, Any]] = []
        for m in _TOKEN_RE.finditer(q):
            s, e = m.start(), m.end()
            if any(a <= s < b for a, b in masked):
                continue
            raw = m.group(0).strip("_")
            if not raw:
                continue
            toks.append(self._token(raw, s, e))
        out: list[Correction] = []
        claimed: set[int] = set()
        # phrases: windows whose corrected words spell a whole term, longest first
        for size in range(min(max_window, len(toks)), 1, -1):
            for i in range(0, len(toks) - size + 1):
                idx = list(range(i, i + size))
                if any(j in claimed for j in idx):
                    continue
                win = [toks[j] for j in idx]
                if not any(t["state"] in ("corrected", "ambiguous") or (t["state"] == "unknown" and t["options"])
                           for t in win):
                    continue
                if any(t["state"] == "protected" and not t["vocab"] for t in win):
                    continue
                found = self._phrase(q, win)
                if found is None:
                    continue
                corr, key_words = found
                ext = self._extend(q, toks, idx, key_words, claimed)
                if ext is not None:
                    corr, idx = ext
                out.append(corr)
                claimed.update(idx)
        out.extend(self._name_runs(q, toks, claimed))
        for j, t in enumerate(toks):
            if j in claimed or t["state"] not in ("corrected", "ambiguous", "api"):
                continue
            c = t.get("correction")
            if c is not None:
                out.append(c)
                claimed.add(j)
        if include_exact:
            out.extend(self._exact_spans(q, toks, claimed, max_window))
        out.sort(key=lambda c: (c.start, -c.end))
        return out

    def _token(self, raw: str, s: int, e: int) -> dict[str, Any]:
        """State of one question word: protected | short | known | exact | corrected | ambiguous | unknown | api."""
        tok: dict[str, Any] = {"raw": raw, "start": s, "end": e, "options": [], "vocab": False}
        why = _protected(raw)
        if why or _HAS_DIGIT_RE.search(raw):
            ws = split_words(raw)
            w = " ".join(ws) or raw.lower()
            tok.update(state="protected", word=w, sing=" ".join(self._sing(x) for x in ws) or w,
                       vocab=bool(ws) and all(self.is_vocabulary_word(x) for x in ws))
            return tok
        rw = split_words(raw)
        if not rw or len(raw) > 4 * self._max_len:
            tok.update(state="protected", word=raw.lower(), sing=raw.lower())
            return tok
        if _API_LIKE_RE.search(raw) and len(rw) > 1:
            tok.update(word=" ".join(rw), sing=" ".join(self._sing(x) for x in rw))
            ids = self._ids_exact(rw, [self._sing(x) for x in rw], None, None)
            tok["vocab"] = bool(ids)
            if ids:
                tok.update(state="exact")
            elif all(self.is_known_word(x) for x in rw):
                tok.update(state="known")       # JavaScript, ServiceNow, PhoneNumber: correct words written together
            else:
                res = self._resolve(raw, None, None, None, 10, False)
                if res.decision in ("corrected", "ambiguous"):
                    tok.update(state="api", correction=self._from_resolution(res, s, e))
                else:
                    tok.update(state="unknown")
            return tok
        if len(rw) != 1:
            tok.update(state="protected", word=" ".join(rw), sing=" ".join(self._sing(x) for x in rw))
            return tok
        w = rw[0]
        sg = self._sing(w)
        tok.update(word=w, sing=sg)
        exact = self._exact_options(w, None)
        if exact and w in self._weak_words:
            # spelt like a rare stored value only (a data-entry typo): a did-you-mean toward the metadata word
            cands = [m for m in self.word_candidates(w, _META_KINDS, fuzzy_only=True) if self._single_ids(m)]
            if cands and cands[0].conf >= CORRECT_MIN:
                top = cands[0]
                stored = sorted({" ".join(self._words[i]): i for i in self._word_terms.get(w, ())}.items(),
                                key=lambda kv: (kv[0].count(" "), kv[0]))[:2]
                surf = [self._word_surface(top)] + [self._surface([i], k) for k, i in stored]
                tok.update(state="ambiguous", vocab=True, options=exact + cands[:WINDOW_OPTIONS - 1],
                           correction=Correction(s, e, raw, "ambiguous", None, tuple(dict.fromkeys(surf)), top.tier,
                                                 top.conf, tuple(sorted({self.terms[i].kind for i in
                                                                         self._single_ids(top)},
                                                                        key=lambda k: _KIND_RANK[k])),
                                                 tuple(self._word_term_cands(top, raw))))
                return tok
        if exact:
            tok.update(state="exact", vocab=True, options=exact)
            return tok
        if w in STOPWORDS or raw.lower() in STOPWORDS:
            tok.update(state="protected")
            return tok
        if self.is_known_word(w):
            tok.update(state="known")
            return tok
        if len(w) < MIN_FUZZY_LEN:
            tok.update(state="short" if w.isalpha() else "protected")
            return tok
        if len(w) > self._max_len:
            tok.update(state="protected")       # longer than any vocabulary word: never a typo of one
            return tok
        cands = self.word_candidates(w)
        tok["options"] = cands[:WINDOW_OPTIONS]
        if not cands:
            tok.update(state="unknown")
            return tok
        top = cands[0]
        close = [top] + [c for c in cands[1:] if c.conf >= top.conf - MARGIN
                         or (c.conf >= top.conf - TIE_MARGIN and ((c.edits <= top.edits and _trank(c.tier)
                                                                    <= _trank(top.tier)) or c.edits < top.edits))]
        loose = len(cands) >= LOOSE_MANY and top.conf < LOOSE_CONF
        if not self._single_ids(top):
            # a word of multi-word terms only: a clear typo of it lists those terms (did you mean); anything weaker is
            # left to the phrase windows
            terms = self._containing(top)
            if terms and top.conf >= CORRECT_MIN and len(close) == 1 and not loose and not top.alt \
                    and (len(w) > 5 or top.tier == "normalized" or sound_key(w) == sound_key(top.word)) \
                    and not _affix_variant(w, top.word):
                cands_t = [Candidate(self.terms[i].kind, self.terms[i].target, self.terms[i].text, raw, top.tier,
                                     top.conf, self.terms[i].object, self.terms[i].source) for k in terms
                           for i in k[1]]
                tok.update(state="ambiguous", correction=Correction(
                    s, e, raw, "ambiguous", None, tuple(self._surface(k[1], k[0]) for k in terms), top.tier,
                    top.conf, tuple(sorted({c.kind for c in cands_t}, key=lambda k: _KIND_RANK[k])),
                    tuple(self._dedup_targets(self._rank(cands_t))[:5])))
            else:
                tok.update(state="unknown")
            return tok
        if top.conf >= CORRECT_MIN and len(close) == 1 and not loose:
            state = "corrected"
            sugg = [top]
            ids = self._single_ids(top)
            person = any(self.terms[i].kind == "person" for i in ids)
            if (len(w) <= 5 and top.tier != "normalized" and sound_key(w) != sound_key(top.word)) or top.alt \
                    or _affix_variant(w, top.word) or _affix_variant(sg, top.word) \
                    or (person and (sound_key(w)[:1] != sound_key(top.word)[:1]
                                    or (top.tier != "normalized" and not top.same_phon))):
                state = "ambiguous"       # a did-you-mean: short word with another sound, affix, single name
        elif top.conf >= AMBIGUOUS_MIN and (len(close) > 1 or loose or top.conf >= CORRECT_MIN):
            state = "ambiguous"
            sugg = [c for c in close if self._single_ids(c)][:5]
        else:   # one weak match only: not flagged (a phrase around it may still use it)
            tok.update(state="unknown")
            return tok
        tok["state"] = state
        surf = [self._word_surface(m) for m in sugg]
        tok["correction"] = Correction(s, e, raw, state, surf[0] if state == "corrected" else None, tuple(surf),
                                       top.tier, top.conf, tuple(sorted({self.terms[i].kind for i in
                                                                         self._single_ids(top)},
                                                                        key=lambda k: _KIND_RANK[k])),
                                       tuple(self._word_term_cands(top, raw)))
        return tok

    def _single_ids(self, m: _WordMatch) -> list[int]:
        """The one-word terms spelt ``m.word`` (metadata only when it matched the typed word's singular)."""
        ids = self._by_key.get(m.word, ())
        if m.ns == "meta":
            return [i for i in ids if self.terms[i].kind != "person"]
        return list(ids)

    def _containing(self, m: _WordMatch, n: int = 5) -> list[tuple[str, list[int]]]:
        """The shortest metadata terms that contain the word ``m.word`` (key, term ids), for a did-you-mean."""
        groups: dict[str, list[int]] = {}
        for i in self._word_terms.get(m.word, ()):
            if self.terms[i].kind != "person":
                groups.setdefault(" ".join(self._words[i]), []).append(i)
        keys = sorted(groups, key=lambda k: (k.count(" "), -sum(self._target_weight.get(self.terms[i].target, 0)
                                                                  for i in groups[k]), k))
        return [(k, groups[k]) for k in keys[:n]]

    def _word_surface(self, m: _WordMatch) -> str:
        ids = self._single_ids(m)
        if ids:
            return self._surface(ids, m.word)
        return m.word

    def _word_term_cands(self, m: _WordMatch, raw: str, n: int = 5) -> list[Candidate]:
        cands = [Candidate(self.terms[i].kind, self.terms[i].target, self.terms[i].text, raw, m.tier, m.conf,
                           self.terms[i].object, self.terms[i].source) for i in self._single_ids(m)]
        return self._dedup_targets(self._rank(cands))[:n]

    def _from_resolution(self, res: Resolution, s: int, e: int) -> Correction:
        kinds = tuple(sorted({c.kind for c in res.candidates}, key=lambda k: _KIND_RANK[k]))
        return Correction(s, e, res.text, res.decision, res.suggestion, res.suggestions, res.tier, res.confidence,
                          kinds, res.candidates[:5])

    def _tok_options(self, t: dict[str, Any]) -> list[_WordMatch] | None:
        if t["state"] in ("corrected", "ambiguous") or (t["state"] == "unknown" and t["options"]):
            return list(t["options"])
        if t["state"] in ("exact", "known", "protected", "unknown", "short"):
            w, sg = t["word"], t.get("sing", t["word"])
            opts = [_WordMatch(w, "exact", 1.0, 0, "any", True)]
            if sg != w:
                opts.append(_WordMatch(sg, "exact", 1.0, 0, "meta", True))
            return opts
        return None

    def _phrase(self, q: str, win: list[dict[str, Any]]) -> tuple[Correction, list[str]] | None:
        """A correction for a window of words when one way (or several) of correcting them spells a whole term."""
        choices: list[list[_WordMatch]] = []
        for t in win:
            opts = self._tok_options(t)
            if opts is None:
                return None
            choices.append(opts)
        beam: list[tuple[float, list[_WordMatch]]] = [(0.0, [])]
        for opts in choices:
            beam = heapq.nlargest(PHRASE_BEAM, ((c + m.conf, ms + [m]) for c, ms in beam for m in opts),
                                  key=lambda x: x[0])
        found: dict[str, tuple[float, list[_WordMatch], list[int]]] = {}
        for total, ms in beam:
            key = " ".join(m.word for m in ms)
            meta_only = any(m.ns == "meta" for m in ms)
            ids = [i for i in self._by_key.get(key, ()) if not (meta_only and self.terms[i].kind == "person")]
            if ids and key not in found:
                found[key] = (total / len(ms), ms, ids)
        if not found:
            return None
        keys = sorted(found, key=lambda k: (-found[k][0], k))
        top, ms, ids = found[keys[0]]
        close = [k for k in keys if found[k][0] >= top - MARGIN / 2]
        decision = "corrected" if len(close) == 1 else "ambiguous"
        fuzzy = [(t, m) for t, m in zip(win, ms) if m.tier != "exact"]
        if not fuzzy:
            return None                       # every word already correct (a known word next to a typo-free term)
        if decision == "corrected":
            person = any(self.terms[i].kind == "person" for i in ids)
            weak = min(m.conf for _, m in fuzzy) < AMBIGUOUS_MIN or top < CORRECT_MIN
            for t, m in fuzzy:
                typed = t["word"]
                if weak or m.alt or _affix_variant(typed, m.word) or _affix_variant(t.get("sing", typed), m.word) \
                        or (person and sound_key(typed)[:1] != sound_key(m.word)[:1]):
                    decision = "ambiguous"
                    break
        s, e = win[0]["start"], win[-1]["end"]
        raw = q[s:e]
        tier = max((m.tier for _, m in fuzzy), key=lambda x: _TIER_RANK[x])
        cands = [Candidate(self.terms[i].kind, self.terms[i].target, self.terms[i].text, raw, tier, top,
                           self.terms[i].object, self.terms[i].source) for i in ids]
        surf = [self._surface(found[k][2], k) for k in close[:5]]
        corr = Correction(s, e, raw, decision, surf[0] if decision == "corrected" else None, tuple(surf), tier, top,
                          tuple(sorted({c.kind for c in cands}, key=lambda k: _KIND_RANK[k])),
                          tuple(self._dedup_targets(self._rank(cands))[:5]))
        return corr, keys[0].split()

    def _extend(self, q: str, toks: list[dict[str, Any]], idx: list[int], key_words: list[str],
                claimed: set[int]) -> tuple[Correction, list[int]] | None:
        """When the vocabulary word next to a matched run belongs, with the run's words, to longer terms
        (``internl intrview`` + ``logs``), the longer run is looked up as one term instead."""
        base = None
        for tid_set in (self._word_terms.get(w) for w in key_words):
            ids = set(tid_set or ())
            base = ids if base is None else base & ids
        if not base:
            return None
        lo, hi = idx[0], idx[-1]
        grew = False
        for _ in range(3):
            moved = False
            for j in (hi + 1, lo - 1):
                if j < 0 or j >= len(toks) or j in claimed:
                    continue
                t = toks[j]
                if t["state"] != "exact" or t["word"] in STOPWORDS:
                    continue
                ids = set()
                for w in {t["word"], t.get("sing", t["word"])}:
                    ids |= set(self._word_terms.get(w, ()))
                if base & ids:
                    base &= ids
                    lo, hi = min(lo, j), max(hi, j)
                    moved = grew = True
                    break
            if not moved:
                break
        if not grew:
            return None
        s, e = toks[lo]["start"], toks[hi]["end"]
        res = self._resolve(q[s:e], None, None, None, 10, False)
        if res.decision not in ("corrected", "ambiguous") or res.extra_words:
            return None
        return self._from_resolution(res, s, e), list(range(lo, hi + 1))

    def _name_runs(self, q: str, toks: list[dict[str, Any]], claimed: set[int]) -> list[Correction]:
        """Two or three correct name words (a stored first name, then a stored surname) that are no stored name
        together: a misspelt surname that is someone else's surname (``<first> tuskovo`` for ``<first> tuskova``)."""
        out: list[Correction] = []
        for size in (3, 2):
            for i in range(0, len(toks) - size + 1):
                idx = list(range(i, i + size))
                if any(j in claimed for j in idx):
                    continue
                win = [toks[j] for j in idx]
                if any(t["state"] != "exact" or t["word"] in STOPWORDS or " " in t["word"] for t in win):
                    continue
                if win[0]["word"] not in self._person_first or win[-1]["word"] not in self._person_last:
                    continue
                words = [t["word"] for t in win]
                if self._in_stored_name(words):
                    continue                      # a stored name, or a run of words of one (first and middle names)
                s, e = win[0]["start"], win[-1]["end"]
                res = self._resolve(q[s:e], frozenset({"person"}), None, None, 10, False)
                if res.decision in ("corrected", "ambiguous") and res.tier != "exact" and not res.extra_words \
                        and res.confidence >= CORRECT_MIN and not res.missing_words:
                    out.append(self._from_resolution(res, s, e))
                    claimed.update(idx)
        return out

    def _in_stored_name(self, words: list[str]) -> bool:
        """The words appear, next to each other and in this order, in some stored person name."""
        ids: set[int] | None = None
        for w in words:
            ws = {i for i in self._word_terms.get(w, ()) if self.terms[i].kind == "person"}
            ids = ws if ids is None else ids & ws
            if not ids:
                return False
        n = len(words)
        for i in ids or ():
            t = self._words[i]
            if any(list(t[k:k + n]) == words for k in range(len(t) - n + 1)):
                return True
        return False

    def _exact_spans(self, q: str, toks: list[dict[str, Any]], claimed: set[int], max_window: int) -> list[Correction]:
        out: list[Correction] = []
        i = 0
        while i < len(toks):
            if i in claimed or not toks[i].get("vocab"):
                i += 1
                continue
            hit = None
            for size in range(min(max_window, len(toks) - i), 0, -1):
                idx = range(i, i + size)
                if any(j in claimed for j in idx):
                    continue
                rkey = " ".join(toks[j]["word"] for j in idx)
                skey = " ".join(toks[j].get("sing", toks[j]["word"]) for j in idx)
                ids = [x for x in self._by_key.get(rkey, ()) if self.terms[x].kind == "person"]
                ids += [x for x in self._by_key.get(skey, ()) if self.terms[x].kind != "person"]
                if ids and (size > 1 or toks[i]["word"] not in STOPWORDS):
                    hit = (size, ids)
                    break
            if hit is None:
                i += 1
                continue
            size, ids = hit
            s, e = toks[i]["start"], toks[i + size - 1]["end"]
            raw = q[s:e]
            cands = [Candidate(self.terms[j].kind, self.terms[j].target, self.terms[j].text, raw, "exact", 1.0,
                               self.terms[j].object, self.terms[j].source) for j in ids]
            surf = self._surface(ids, raw)
            out.append(Correction(s, e, raw, "exact", surf, (surf,), "exact", 1.0,
                                  tuple(sorted({c.kind for c in cands}, key=lambda k: _KIND_RANK[k])),
                                  tuple(self._dedup_targets(self._rank(cands))[:5])))
            i += size
        return out

    # ---------------------------------------------------------------------------------------------------- reports
    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for t in self.terms:
            kinds[t.kind] = kinds.get(t.kind, 0) + 1
        return {"terms": len(self.terms), "by_kind": kinds, "keys": len(self._by_key), "words": len(self._word_terms),
                "known_words": len(self._known), "delete_keys": len(self._deletes), "max_word_len": self._max_len}


def build(vocab: Vocabulary) -> Resolver:
    """Index a vocabulary (deterministic: the same vocabulary, in any order, always gives the same answers). Ends
    with one full garbage collection, so the first lookups do not pay for scanning the new indexes (measured: a
    40-60 ms pause in the first lookup otherwise)."""
    resolver = Resolver(vocab)
    gc.collect()
    return resolver
