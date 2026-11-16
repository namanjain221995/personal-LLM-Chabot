"""Grading: deterministic fact checks plus an optional LLM judge.

Deterministic checks (no LLM):

* **fact recall** – share of ``expected_facts`` found in the answer. Matching is
  case-insensitive with whitespace/punctuation normalised. How a fact may be stated depends
  on its shape:

  - *API-name facts* (``Interview_Type__c``, ``Session__c.Status__c``, or a field api name
    such as ``Name``) also match the field's label and the de-suffixed split form
    ("Interview Type"), resolved through an exact api-name lookup in the knowledge.
  - *Exact facts* (``Question.exact_facts``, every API-name fact in the categories of
    :data:`EXACT_API_CATEGORIES`, every fact in :data:`EXACT_VALUE_CATEGORIES`) match only
    literally: an API name must appear as that exact token (a misspelling, a near-miss
    sibling or the label does not count) and a picklist value must appear as that exact
    phrase, after longer sibling values that contain it ("No Hire" for "Hire") are removed.
  - *Plain facts* (object names, values, descriptive phrases) are never expanded to field
    forms, never match inside an API token ("Lead" does not match ``Lead__c``) and are
    rejected when the matched words are negated ("not active" does not state "active").
    Single words also match their inflections; short phrases match their content words in
    order inside one clause; long descriptive phrases match on 75% content-word overlap
    inside one clause, with the same negation polarity as the fact.

  A fact that the question text itself already states is matched against the answer with
  the question echo removed, so repeating the question back never earns recall.
* **forbidden facts** – ``Question.forbidden_facts`` (a confusable sibling API name, the
  "correct" spelling of a typo'd API name, the opposite polarity) fail the answer when it
  asserts them; mentions in a negating or contrasting sentence ("not Position__c") are allowed.
* **tool hit** – at least one of ``expected_tools`` was called (vacuously true when none).
* **refusal** – for ``answerable=False`` questions the answer must say the information is
  not available (:data:`REFUSAL_RE`).

The judge (``grade_with_judge``) sends the question, the answer, the expected facts and
the texts of the question's gold knowledge chunks to ``llm.chat_json`` using the
``JUDGE_SYSTEM`` / ``JUDGE_USER`` templates.
"""

from __future__ import annotations

import json
import re
import string
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from sf_assistant.agent import knowledge_prompts as prompts

if TYPE_CHECKING:
    from sf_assistant.agent.knowledge_agent import AgentResult
    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.question import Question
    from sf_assistant.config import Config
    from sf_assistant.knowledge.store import ObjectKnowledge

# Pass thresholds (SPEC).
RECALL_PASS = 0.8
JUDGE_CORRECTNESS_PASS = 1

# Character budget for knowledge context handed to the judge (gold chunks first, then the
# other chunks the agent retrieved), and per-chunk cap on the rendered JSON record.
MAX_GOLD_CONTEXT_CHARS = 24_000
MAX_CHUNK_CHARS = 4_000
# Budget for the replayed tool results (what the agent actually read) shown to the judge.
MAX_TOOL_CONTEXT_CHARS = 30_000
MAX_TOOL_RESULT_CHARS = 6_000

# Categories whose point is the exact API name: API-name facts there match only literally.
EXACT_API_CATEGORIES = frozenset({"field_lookup", "typo_api_names", "concept_to_field", "legacy_vs_current"})
# Categories whose facts are picklist values: every fact matches as an exact phrase.
EXACT_VALUE_CATEGORIES = frozenset({"picklist_values"})

# Adjectives / participles that, after "not", say the information is absent.
_ABSENT_ADJ = (r"available|present|included|listed|documented|specified|defined|recorded|stored|known|"
               r"provided|captured|covered|described|contained|mentioned|exposed|tracked|part\s+of")
# Verbs that, after "does not / isn't / can't", say the source lacks the information.
_ABSENT_VERB = (r"contain|include|specify|have|hold|store|record|provide|define|list|describe|document|"
                r"cover|mention|exist|say|state|capture|track|expose|indicate")

# Refusal detection: the SPEC phrases plus the common ways a model says the same thing.
# Every alternative ends on a word boundary ("not in" must not match "not intended"), the
# answer is normalised first (curly apostrophes -> ', markdown marks removed), and "contains
# no" only counts when followed by a data/information noun.
REFUSAL_RE = re.compile(
    rf"\bnot\s+(?:be\s+|been\s+)?(?:{_ABSENT_ADJ}|in)\b"
    rf"|\b(?:does|do|did|is|are|was|were|has|have|had|can|could|will|would)\s*(?:n't|not)\s+(?:be\s+)?"
    rf"(?:(?:{_ABSENT_VERB})(?:s|es|ed)?|{_ABSENT_ADJ})\b"
    r"|\bno\s+information\b"
    r"|\b(?:cannot|can\s*not|can't|unable\s+to)\s+(?:determine|answer|tell|say|provide|know|see|identify|find|confirm|give)\b"
    r"|\bneeds?\s+(?:live\s+)?record\s+data\b|\brequires?\s+(?:live\s+)?record\s+data\b"
    r"|\bno\s+(?:record|live|actual)\s+data\b"
    r"|\bunavailable\b|\bnon-?existent\b"
    r"|\bcontains?\s+no\s+(?:record|data|information|details?|such|mention|values?|live)\b"
    r"|\bthere\s+(?:is|are)\s+no\b"
    r"|\bno\s+(?:such\s+)?(?:field|object|picklist|value|automation|rule|record\s+type|mention|details?)s?\s+"
    r"(?:called|named|for|on|in|with|about|like|exists?|is|are)\b"
    r"|\bno\s+such\b"
    r"|\bno\s+[\w\s-]{1,40}\s+(?:are|is)\s+(?:defined|listed|documented|described|specified)\b"
    r"|\bno\s+[\w\s-]{1,40}\s+defined\s+in\s+the\b"
    r"|\boutside\s+(?:the\s+)?(?:scope|object\s+knowledge)\b",
    re.IGNORECASE,
)

# Field-type synonyms: the knowledge file's type token -> how people (and models) say it.
TYPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "multipicklist": ("multi select picklist", "multi-select picklist", "multiselect picklist",
                      "multi picklist", "multi-picklist"),
    "boolean": ("checkbox",),
    "reference": ("lookup", "master detail", "master-detail"),
    "long text area": ("long_text_area", "longtextarea", "long text"),
    "rich text area": ("rich_text_area", "rich text"),
    "datetime": ("date time", "date/time", "date-time"),
    "string": ("text",),
    "number": ("numeric", "double", "decimal"),
    "percent": ("percentage",),
    "auto number": ("autonumber", "auto-number"),
    "many to one": ("many-to-one", "n:1", "n to 1"),
}
# A synonym followed by one of these words names a different type ("text" vs "text area").
_SYNONYM_BLOCKED_NEXT: dict[str, frozenset[str]] = {"text": frozenset({"area", "areas"})}

# Suffixes accepted on a single-word fact ("create" ~ "created"/"creation", "required" ~
# "requiredness"), and stopwords ignored when matching long descriptive facts by content words.
_WORD_SUFFIXES = ("", "s", "es", "d", "ed", "ing", "ion", "ions", "ness", "ly", "r", "er")
_STOPWORDS = frozenset(
    "a an the of to in on for and or by with from is are be as at it its this that one any".split())
# Negation tokens (after "n't" is expanded to " not"). A plain fact whose matched words are
# negated in the answer is not stated; long facts must have the same polarity as the answer.
_NEGATIONS = frozenset({"not", "no", "never", "without", "cannot", "none", "nor", "non"})
# How many tokens before a match are inspected for a negation.
NEGATION_LOOKBACK_SHORT = 2
NEGATION_LOOKBACK_LONG = 3
# A multi-word fact with at least this many content words matches when this share of them
# (by crude stem) appears in one clause of the answer: long LLM-written facts are rarely
# quoted verbatim.
FUZZY_MIN_WORDS = 3
FUZZY_COVERAGE = 0.75
# Short multi-word facts ("attendee rows") also match when all their content words occur in
# order within a window of (fact length + FUZZY_WINDOW_SLACK) words of one clause.
FUZZY_WINDOW_SLACK = 2

_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")
_SUFFIX_RE = re.compile(r"__(c|r|mdt|e|x|b|kav|share|history)$", re.IGNORECASE)
# Identifier-like tokens inside free text. Those that are API names (a Salesforce suffix such
# as ``Lead__c``, or underscores plus a capital as in ``internal_interview_Status_Update``) are
# masked when matching plain facts and field labels, so "Lead" or "Status" cannot match the
# words inside them. Lower-case snake tokens (``many_to_one``) are type values and stay.
_IDENT_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*(?![A-Za-z0-9_])")
_API_SUFFIX_IN_RE = re.compile(r"__(?:c|r|mdt|e|x|b|kav|share|history)\b", re.IGNORECASE)
_API_MASK = " zzapitoken "


def _mask_api_tokens(text: str) -> str:
    """Replace API-name tokens in ``text`` by a placeholder word."""
    def repl(m: re.Match) -> str:
        tok = m.group(0)
        if _API_SUFFIX_IN_RE.search(tok) or ("_" in tok and any(ch.isupper() for ch in tok)):
            return _API_MASK
        return tok
    return _IDENT_RE.sub(repl, text)
# Clause boundaries: sentence punctuation followed by space/end, semicolons, colons, commas,
# newlines and bullet dashes. Facts never span a clause.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?](?=\s|$)|[;:\n]|\s[-–—]\s")
_CLAUSE_SPLIT_COMMA_RE = re.compile(r"[.!?](?=\s|$)|[;:,\n]|\s[-–—]\s")
# Sentence boundaries (for forbidden-fact checks).
_SENTENCE_SPLIT_RE = re.compile(r"[.!?](?=\s|$)|[;\n]")
# Words that mark a sentence as negating or contrasting a forbidden fact rather than asserting it.
_CONTRAST_RE = re.compile(
    r"\b(not|no|never|n't|instead|rather|wrong|incorrect|typo|misspel\w*|mistak\w*|exist\w*|"
    r"legacy|older|newer|current|also|differ\w*|distinct|separate|other|vs|versus|confus\w*|"
    r"unlike|whereas|while|but|fail\w*|avoid|beware)\b", re.IGNORECASE)


@dataclass
class Grade:
    """Grade for one question (see SPEC ``grading.py``).

    ``forbidden_found`` lists the question's ``forbidden_facts`` the answer asserted; any
    entry fails the question.
    """

    qid: str
    fact_recall: float
    facts_missing: list[str] = field(default_factory=list)
    tool_hit: bool = True
    refusal_correct: bool | None = None
    judge: dict | None = None
    passed: bool = False
    forbidden_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Plain-dict form (JSON-serialisable) for export."""
        return asdict(self)


# --------------------------------------------------------------------------------------
# text normalisation
# --------------------------------------------------------------------------------------

_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation + "“”‘’–—…"})


def normalize(text: str) -> str:
    """Lowercase, turn punctuation/underscores into spaces, collapse whitespace."""
    return " ".join(str(text).lower().translate(_PUNCT_TABLE).split())


def _prep(text: str) -> str:
    """Unify apostrophes, expand "n't" to " not", and drop markdown emphasis/code marks."""
    text = str(text or "").replace("’", "'").replace("‘", "'")
    text = re.sub(r"(?i)n't\b", " not", text)
    text = re.sub(r"(?<=\d)[,\u202f\u00a0](?=\d{3}\b)", "", text)  # 131,072 -> 131072
    return re.sub(r"[*`]+", "", text)


def _split_api(name: str) -> str:
    """``Previous_internal_interview__c`` → ``previous internal interview``; ``CreatedById`` → ``created by id``."""
    parts = []
    for seg in name.split("."):
        seg = _SUFFIX_RE.sub("", seg)
        seg = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", seg)  # camelCase boundaries
        parts.append(seg)
    return normalize(" ".join(parts))


def _api_shaped(fact: str) -> bool:
    """True for strings that look like an API name (underscore, dotted path or camelCase)."""
    fact = fact.strip()
    return bool(_API_NAME_RE.match(fact)) and ("_" in fact or "." in fact or bool(re.search(r"[a-z][A-Z]", fact)))


def _field_exact(knowledge: "ObjectKnowledge | None", api: str) -> dict | None:
    """The field whose api name equals ``api`` (case-insensitive); never a loose/label match."""
    if knowledge is None or not api:
        return None
    try:
        f = knowledge.field(api)
    except Exception:  # knowledge lookups must never break grading
        return None
    if f and str(f.get("api_name") or "").lower() == api.lower():
        return f
    return None


def is_api_fact(fact: str, knowledge: "ObjectKnowledge | None" = None) -> bool:
    """API-name facts: API-shaped strings, or the exact api name of a field (``Name``)."""
    fact = str(fact).strip()
    if _api_shaped(fact):
        return True
    f = _field_exact(knowledge, fact)
    return bool(f and str(f.get("api_name")) == fact)  # case-sensitive: "name" is not ``Name``


def fact_variants(fact: str, knowledge: "ObjectKnowledge | None" = None, *, exact: bool = False) -> set[str]:
    """All normalised surface forms that count as stating ``fact``.

    * the fact itself (always, and the only form for exact facts);
    * for API-name facts: the split / de-suffixed form and, when the knowledge has a field
      with exactly that api name, its label. For ``Other__c.Field__c`` references only the
      object-qualified label is added (``Session__c.Status__c`` also matches "Session Status").

    Plain facts (object names, picklist values, phrases) are never expanded: "Lead" stays
    "lead" even though a field is labelled "Lead".
    """
    fact = str(fact).strip()
    variants = {normalize(fact)}
    if exact or not is_api_fact(fact, knowledge):
        return {v for v in variants if v}
    if _api_shaped(fact):
        variants.add(_split_api(fact))
    if knowledge is not None:
        own = str(getattr(knowledge, "api_name", "") or "").lower()
        candidates = [fact]
        if "." in fact:
            candidates.append(fact.rsplit(".", 1)[1])
        for cand in candidates:
            # For "Other__c.Field__c" the bare field forms would be ambiguous ("status"),
            # so only the object-qualified label is added; same-object refs resolve fully.
            cross_object = cand != fact and fact.rsplit(".", 1)[0].lower() != own
            f = _field_exact(knowledge, cand)
            if not f:
                continue
            api, label = f.get("api_name"), f.get("label")
            if cross_object:
                if label:
                    variants.add(normalize(f"{_split_api(fact.rsplit('.', 1)[0])} {label}"))
                continue
            if api:
                variants.add(normalize(api))
                variants.add(_split_api(api))
            if label:
                variants.add(normalize(label))
    return {v for v in variants if v}


def _stem(word: str) -> str:
    """Very small suffix stripper, enough to equate conducting/conducts, mirrored/mirror."""
    for suf in ("ations", "ation", "ings", "ing", "ions", "ion", "ness", "ed", "es", "s", "e", "ly"):
        if len(word) - len(suf) >= 4 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _word_forms(word: str) -> set[str]:
    """Inflected forms of one lowercase word: create -> created, creates, creation, creating."""
    bases = {word}
    if word.endswith("e"):
        bases.add(word[:-1])
    return {b + suf for b in bases for suf in _WORD_SUFFIXES if len(b + suf) >= len(word) - 1}


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _synonym_variants(norm_fact: str) -> set[str]:
    """Type-name synonyms (and number words <-> digits) for a normalised fact (both directions)."""
    out: set[str] = set()
    for word, digit in _NUMBER_WORDS.items():
        if norm_fact in (word, digit):
            out |= {word, digit}
    for key, syns in TYPE_SYNONYMS.items():
        group = {normalize(key)} | {normalize(x) for x in syns}
        if norm_fact in group:
            out |= group
    return out


# --------------------------------------------------------------------------------------
# clause-level matching
# --------------------------------------------------------------------------------------

def _clauses(text: str, *, mask_api: bool, split_commas: bool = True) -> list[list[str]]:
    """Normalised token lists, one per clause of ``text``.

    ``mask_api`` replaces Salesforce API tokens (``Lead__c``) by a placeholder so plain facts
    cannot match the words inside them.
    """
    text = _prep(text)
    if mask_api:
        text = _mask_api_tokens(text)
    splitter = _CLAUSE_SPLIT_COMMA_RE if split_commas else _CLAUSE_SPLIT_RE
    out = []
    for part in splitter.split(text):
        toks = normalize(part).split()
        if toks:
            out.append(toks)
    return out


def _negated_before(tokens: Sequence[str], start: int, lookback: int) -> bool:
    """A negation token occurs in the ``lookback`` tokens before ``start``."""
    return any(t in _NEGATIONS for t in tokens[max(0, start - lookback):start])


def _phrase_occurs(phrase: Sequence[str], clauses: list[list[str]], *, guard: bool,
                   blocked_next: frozenset[str] = frozenset()) -> bool:
    """``phrase`` occurs as a token run in some clause.

    With ``guard`` an occurrence preceded (within :data:`NEGATION_LOOKBACK_SHORT` tokens) by
    a negation does not count. ``blocked_next`` rejects an occurrence followed by one of
    those tokens ("text" followed by "area").
    """
    n = len(phrase)
    if not n:
        return False
    for toks in clauses:
        for i in range(len(toks) - n + 1):
            if toks[i:i + n] != list(phrase):
                continue
            if blocked_next and i + n < len(toks) and toks[i + n] in blocked_next:
                continue
            if guard and _negated_before(toks, i, NEGATION_LOOKBACK_SHORT):
                continue
            return True
    return False


def _content_stems(fact: str) -> list[str]:
    """Stems of the fact's content words (stopwords and negations dropped)."""
    return [_stem(w) for w in normalize(fact).split() if w not in _STOPWORDS and w not in _NEGATIONS]


def _has_negation(tokens: Iterable[str]) -> bool:
    return any(t in _NEGATIONS for t in tokens)


def _window_match(fact: str, clauses: list[list[str]]) -> bool:
    """All (2+) content words of ``fact`` appear in order within a short window of one clause,
    and the window (plus a few tokens before it) has the fact's negation polarity."""
    fact_words = normalize(fact).split()
    content = _content_stems(fact)
    if len(content) < 2:
        return False
    fact_neg = _has_negation(fact_words)
    span = len(fact_words) + FUZZY_WINDOW_SLACK
    for toks in clauses:
        stems = [_stem(w) for w in toks]
        for start, w in enumerate(stems):
            if w != content[0]:
                continue
            pos, ok = start, True
            for c in content[1:]:
                try:
                    pos = stems.index(c, pos + 1, start + span)
                except ValueError:
                    ok = False
                    break
            if not ok:
                continue
            region = toks[max(0, start - NEGATION_LOOKBACK_LONG):pos + 1]
            if _has_negation(region) == fact_neg:
                return True
    return False


def _fuzzy_content_match(fact: str, clauses: list[list[str]]) -> bool:
    """Long descriptive facts: enough of their content words (by stem) occur in one clause,
    and the matched region has the fact's negation polarity."""
    if _api_shaped(fact) and "_" in fact:
        return False
    words = _content_stems(fact)
    if len(words) < FUZZY_MIN_WORDS:
        return False
    fact_neg = _has_negation(normalize(fact).split())
    need = set(words)
    for toks in clauses:
        stems = [_stem(w) for w in toks]
        hit = sum(1 for w in words if w in stems)
        if hit / len(words) < FUZZY_COVERAGE:
            continue
        idx = [i for i, s in enumerate(stems) if s in need]
        region = toks[max(0, idx[0] - NEGATION_LOOKBACK_LONG):idx[-1] + 1]
        if _has_negation(region) == fact_neg:
            return True
    return False


def _cross_object_match(fact: str, hay: str, knowledge: "ObjectKnowledge | None") -> bool:
    """``Other__c.Field__c`` also matches when the answer names ``Other__c`` and the bare ``Field__c``
    (e.g. a "Reads from Interview_Attendee__c: Role__c, Candidate__c" list)."""
    if "." not in fact or not _API_NAME_RE.match(fact.strip()):
        return False
    obj, fld = fact.strip().rsplit(".", 1)
    own = str(getattr(knowledge, "api_name", "") or "").lower()
    if obj.lower() == own:
        return False  # same-object refs are already resolved by fact_variants
    has_obj = f" {normalize(obj)} " in hay or f" {_split_api(obj)} " in hay
    return has_obj and (f" {normalize(fld)} " in hay)


def _exact_api_in(fact: str, text: str) -> bool:
    """The literal API token ``fact`` occurs in ``text`` (case-insensitive when it carries an
    underscore or dot, case-sensitive otherwise so ``Name`` does not match "API name")."""
    fact = fact.strip()
    flags = re.IGNORECASE if ("_" in fact or "." in fact) else 0
    pattern = rf"(?<![A-Za-z0-9_.]){re.escape(fact)}(?![A-Za-z0-9_]|\.[A-Za-z])"
    return bool(re.search(pattern, _prep(text), flags))


def _remove_siblings(text: str, fact: str, siblings: Iterable[str]) -> str:
    """Blank out longer sibling values that contain ``fact`` ("No Hire" when checking "Hire")."""
    nf = f" {normalize(fact)} "
    for s in sorted({str(x) for x in siblings}, key=len, reverse=True):
        ns = normalize(s)
        if not ns or ns == normalize(fact) or nf not in f" {ns} ":
            continue
        words = [re.escape(w) for w in ns.split()]
        text = re.sub(r"(?i)(?<![A-Za-z0-9])" + r"[\W_]+".join(words) + r"(?![A-Za-z0-9])", " ; ", text)
    return text


def strip_question_echo(answer: str, question: str) -> str:
    """``answer`` with any verbatim copy of the question (or of one of its sentences) removed.

    Used for facts the question already states: an answer only earns them by stating them
    again outside the echoed question text.
    """
    answer = _prep(answer)
    q = _prep(question).strip()
    if not q:
        return answer
    parts = [q] + [s.strip() for s in re.split(r"(?<=[?.!])\s+", q) if len(s.split()) >= 4]
    for part in sorted(set(parts), key=len, reverse=True):
        words = re.findall(r"[A-Za-z0-9]+", part)
        if len(words) < 3:
            continue
        pattern = r"(?i)" + r"[\W_]+".join(re.escape(w) for w in words) + r"[?.!]?"
        answer = re.sub(pattern, " . ", answer)
    return answer


def _match(fact: str, answer: str, knowledge: "ObjectKnowledge | None", exact: bool,
           siblings: Iterable[str]) -> bool:
    """Core matcher behind :func:`fact_in_answer` (no question-echo handling)."""
    fact = str(fact).strip()
    if not fact:
        return False
    api = is_api_fact(fact, knowledge)

    if api and exact:
        return _exact_api_in(fact, answer)

    if api:
        # The literal API token anywhere; its label / split forms only outside other API
        # tokens ("Status" inside internal_interview_Status_Update does not state Status__c).
        if _exact_api_in(fact, answer):
            return True
        masked = f" {normalize(_mask_api_tokens(_prep(answer)))} "
        if any(f" {v} " in masked for v in fact_variants(fact, knowledge)):
            return True
        return _cross_object_match(fact, f" {normalize(_prep(answer))} ", knowledge)

    # Plain facts: never inside an API token; negation-guarded; siblings removed.
    text = _remove_siblings(answer, fact, siblings)
    split_commas = "," not in fact
    # Facts that themselves contain identifier pieces (file names such as
    # "example_feature_map.html") are matched without masking.
    clauses = _clauses(text, mask_api="_" not in fact, split_commas=split_commas)
    norm_fact = normalize(fact)
    fact_tokens = norm_fact.split()
    guard = not _has_negation(fact_tokens)
    if _phrase_occurs(fact_tokens, clauses, guard=guard):
        return True
    if exact:
        return False  # exact values: no synonyms, inflections or fuzzy matching
    for syn in _synonym_variants(norm_fact) - {norm_fact}:
        if _phrase_occurs(syn.split(), clauses, guard=guard,
                          blocked_next=_SYNONYM_BLOCKED_NEXT.get(syn, frozenset())):
            return True
    if len(fact_tokens) == 1 and fact_tokens[0].isalpha() and len(fact_tokens[0]) >= 4:
        forms = _word_forms(fact_tokens[0])
        stem = _stem(fact_tokens[0])
        for toks in clauses:
            for i, t in enumerate(toks):
                if (t in forms or _stem(t) == stem) and not (guard and _negated_before(toks, i, NEGATION_LOOKBACK_SHORT)):
                    return True
    if _window_match(fact, clauses):
        return True
    return _fuzzy_content_match(fact, clauses)


def fact_in_answer(fact: str, answer: str, knowledge: "ObjectKnowledge | None" = None, *,
                   exact: bool = False, question: str | None = None,
                   siblings: Iterable[str] = ()) -> bool:
    """True when ``fact`` is stated in ``answer``.

    Args:
        fact: one expected fact.
        answer: the answer text.
        knowledge: resolves API-name facts to their labels (optional).
        exact: literal matching only (exact API token / exact value phrase).
        question: when given and the question already states ``fact``, the answer is checked
            with the question echo removed (:func:`strip_question_echo`).
        siblings: other values of the same set; longer ones that contain ``fact`` are removed
            from the answer before a plain fact is checked.
    """
    siblings = list(siblings)
    if question and _match(fact, question, knowledge, exact, siblings):
        answer = strip_question_echo(answer, question)
    return _match(fact, answer, knowledge, exact, siblings)


def fact_in_question(fact: str, question: str, knowledge: "ObjectKnowledge | None" = None, *,
                     exact: bool = False) -> bool:
    """True when the question text already states ``fact`` (a tautological expected fact)."""
    return _match(fact, question, knowledge, exact, ())


def forbidden_in_answer(fact: str, answer: str, knowledge: "ObjectKnowledge | None" = None) -> bool:
    """True when ``answer`` asserts the forbidden ``fact``.

    API names match as exact tokens, other facts as plain facts. Sentences that negate or
    contrast ("not Position__c", "the legacy Date__c differs from ...") do not count.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(_prep(answer)):
        if not sentence.strip() or _CONTRAST_RE.search(sentence):
            continue
        if is_api_fact(fact, knowledge):
            if _exact_api_in(fact, sentence):
                return True
        elif _match(fact, sentence, knowledge, True, ()):
            return True
    return False


def is_refusal(answer: str) -> bool:
    """The answer says the information is not available (:data:`REFUSAL_RE`).

    Curly apostrophes are unified and markdown emphasis/code marks removed first, so
    "**no record types defined** in the file" and "doesn’t contain" still match.
    """
    plain = re.sub(r"[*_`]+", "", (answer or "").replace("’", "'").replace("‘", "'"))
    return bool(REFUSAL_RE.search(plain))


# --------------------------------------------------------------------------------------
# deterministic grading
# --------------------------------------------------------------------------------------

def _judge_usable(judge: dict | None) -> bool:
    """A judge verdict counts only when it parsed and carries a correctness score."""
    return isinstance(judge, dict) and "error" not in judge and judge.get("correctness") is not None


def _decide_pass(q: "Question", g: Grade, has_error: bool) -> bool:
    """Apply the SPEC pass rule; a failed judge call falls back to deterministic checks.

    A forbidden fact in the answer always fails the question.
    """
    if has_error or g.forbidden_found:
        return False
    judge = g.judge if _judge_usable(g.judge) else None
    hallucination = bool(judge.get("hallucination")) if judge else False
    if q.answerable:
        ok = g.fact_recall >= RECALL_PASS
        if judge is not None:
            ok = ok and int(judge.get("correctness", 0)) >= JUDGE_CORRECTNESS_PASS
        return ok and not hallucination
    return bool(g.refusal_correct) and not hallucination


def exact_facts_for(q: "Question", knowledge: "ObjectKnowledge | None" = None) -> set[str]:
    """Expected facts of ``q`` that must match literally.

    ``q.exact_facts`` plus every API-name fact in :data:`EXACT_API_CATEGORIES` and every fact
    in :data:`EXACT_VALUE_CATEGORIES` (so question files written before ``exact_facts``
    existed are graded strictly too).
    """
    out = {str(f) for f in getattr(q, "exact_facts", None) or []}
    facts = [str(f) for f in q.expected_facts or []]
    if q.category in EXACT_VALUE_CATEGORIES:
        out |= set(facts)
    if q.category in EXACT_API_CATEGORIES:
        out |= {f for f in facts if is_api_fact(f, knowledge)}
    return out


def grade_deterministic(q: "Question", r: "AgentResult", knowledge: "ObjectKnowledge | None" = None) -> Grade:
    """Grade without an LLM.

    ``knowledge`` is optional (keeps the SPEC signature callable as ``(q, r)``); when given,
    API-name facts also match their field labels (outside exact categories).
    """
    answer = r.answer or ""
    facts = [str(f) for f in (q.expected_facts or []) if str(f).strip()]
    exact = exact_facts_for(q, knowledge)
    missing = [f for f in facts
               if not fact_in_answer(f, answer, knowledge, exact=f in exact, question=q.question,
                                     siblings=[s for s in facts if s != f])]
    recall = 1.0 if not facts else round((len(facts) - len(missing)) / len(facts), 4)

    # Forbidden facts the question itself names are left to the judge: the answer has to
    # address them, and doing so is not asserting them.
    forbidden = [str(f) for f in getattr(q, "forbidden_facts", None) or [] if str(f).strip()]
    found = [f for f in forbidden
             if not fact_in_question(f, q.question, knowledge, exact=True)
             and forbidden_in_answer(f, answer, knowledge)]

    called = {tc.get("name") for tc in (r.tool_calls or [])}
    tool_hit = True if not q.expected_tools else bool(called & set(q.expected_tools))

    refusal = None if q.answerable else is_refusal(answer)

    g = Grade(qid=q.qid, fact_recall=recall, facts_missing=missing, tool_hit=tool_hit,
              refusal_correct=refusal, judge=None, forbidden_found=found)
    g.passed = _decide_pass(q, g, has_error=bool(r.error) and not answer.strip())
    return g


# --------------------------------------------------------------------------------------
# LLM judge
# --------------------------------------------------------------------------------------

def _chunk_block(cid: str, c: dict) -> str:
    """One chunk for the judge: its summary text plus the full JSON record it came from.

    The search ``text`` is a lossy rendering (no precision/scale, no automation field lists, ...);
    the agent's tools return the full record, so the judge must see it too or it flags
    tool-grounded details as hallucinations.
    """
    data = c.get("data")
    record = json.dumps(data, ensure_ascii=False, default=str) if data else ""
    if len(record) > MAX_CHUNK_CHARS:
        record = record[:MAX_CHUNK_CHARS] + " …[truncated]"
    block = f"[{cid}] {c.get('title', '')}\n{c.get('text', '')}"
    return f"{block}\nRecord: {record}" if record else block


# One ToolRegistry per knowledge object, built lazily for replaying the agent's tool calls.
_REGISTRIES: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()
_REGISTRY_LOCK = threading.Lock()


def _registry_for(knowledge: "ObjectKnowledge") -> Any:
    """Cached :class:`~okeval.tools.ToolRegistry` over ``knowledge`` (None if it cannot be built)."""
    with _REGISTRY_LOCK:
        try:
            reg = _REGISTRIES.get(knowledge)
        except TypeError:  # not weak-referenceable
            reg = None
        if reg is None:
            try:
                from sf_assistant.knowledge.search import KnowledgeIndex
                from sf_assistant.tools.knowledge_tools import ToolRegistry
                reg = ToolRegistry(knowledge, KnowledgeIndex(knowledge.chunks()))
                _REGISTRIES[knowledge] = reg
            except Exception:
                return None
        return reg


def _tool_results_context(r: "AgentResult", knowledge: "ObjectKnowledge") -> str:
    """Replay the agent's tool calls (tools are deterministic) and render what it read.

    ``AgentResult.tool_calls`` keeps only names/arguments/chunk ids, but tools enrich their
    results (e.g. ``get_field`` adds the automations that write a field), so grading against
    chunks alone flags tool-grounded statements as hallucinations.
    """
    calls = [tc for tc in (r.tool_calls or []) if tc.get("name")]
    if not calls:
        return ""
    reg = _registry_for(knowledge)
    if reg is None:
        return ""
    parts, used, seen = [], 0, set()
    for tc in calls:
        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
        key = json.dumps([tc["name"], args], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out = reg.call(tc["name"], args)
        body = json.dumps(out.get("result") if out.get("ok") else {"error": out.get("error")},
                          ensure_ascii=False, default=str)
        if len(body) > MAX_TOOL_RESULT_CHARS:
            body = body[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
        block = f"{tc['name']}({json.dumps(args, ensure_ascii=False, default=str)}) -> {body}"
        if used + len(block) > MAX_TOOL_CONTEXT_CHARS:
            parts.append("[… further tool results omitted for length]")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _gold_context(q: "Question", r: "AgentResult", knowledge: "ObjectKnowledge") -> str:
    """Knowledge the judge grades against.

    A header naming the object (every field below belongs to it), the question's gold chunks,
    then every other chunk the agent's tool calls returned (what the answer could legitimately
    rely on). Each chunk carries its full JSON record.
    """
    try:
        by_id = {c["id"]: c for c in knowledge.chunks()}
    except Exception:
        by_id = {}
    gold = [cid for cid in (q.gold_chunk_ids or []) if cid in by_id]
    seen: list[str] = []
    for tc in r.tool_calls or []:
        for cid in tc.get("chunk_ids") or []:
            if cid in by_id and cid not in gold and cid not in seen:
                seen.append(cid)
    obj = getattr(knowledge, "api_name", "") or "the object"
    header = (f"All knowledge below describes the Salesforce object {obj}; every field: chunk is a field "
              f"of {obj}. A claim is grounded if it is supported by ANY chunk or tool result below, "
              f"including absence claims such as 'no automation writes this field' when the tool "
              f"results list none. A named replacement, successor, value or automation that no chunk or "
              f"tool result below states is NOT grounded: treat it as a hallucination. Judge correctness "
              f"against the question asked.")
    parts, used, omitted = [header], 0, 0
    tool_ctx = _tool_results_context(r, knowledge)
    sections = [("GOLD CHUNKS (expected source of the answer)", gold)]
    if not tool_ctx:  # replay unavailable: fall back to the raw chunks the tools touched
        sections.append(("OTHER CHUNKS THE SYSTEM RETRIEVED WITH ITS TOOLS", seen))
    for title, ids in sections:
        if not ids:
            continue
        parts.append(f"=== {title} ===")
        for cid in ids:
            block = _chunk_block(cid, by_id[cid])
            if used + len(block) > MAX_GOLD_CONTEXT_CHARS:
                omitted += 1
                continue
            parts.append(block)
            used += len(block)
    if omitted:
        parts.append(f"[… {omitted} chunk(s) omitted for length]")
    if tool_ctx:
        parts.append("=== TOOL RESULTS THE SYSTEM RECEIVED (exact replay; these are knowledge-file data) ===")
        parts.append(tool_ctx)
    if not gold and not seen and not tool_ctx:
        parts.append("(no gold knowledge chunks: the knowledge file should not contain the answer)")
    elif not gold and not q.answerable:
        parts.append("(no gold chunks: the knowledge file is not expected to contain the answer)")
    return "\n\n".join(parts)


def _template(name: str) -> str:
    """Current text of a prompt template, honouring runtime edits to ``prompts.TEMPLATES``."""
    templates = getattr(prompts, "TEMPLATES", None)
    if isinstance(templates, dict) and name in templates:
        return templates[name]
    return getattr(prompts, name)


def _judge_values(q: "Question", r: "AgentResult", knowledge: "ObjectKnowledge") -> dict[str, Any]:
    """Every value the judge template might reference.

    ``prompts.render`` ignores unused keywords, so an edited ``JUDGE_USER`` may use any of
    these names; a placeholder outside this set still fails loudly inside ``render``.
    """
    gold = _gold_context(q, r, knowledge)
    facts = json.dumps(list(q.expected_facts or []), ensure_ascii=False)
    tool_calls = json.dumps(
        [{"name": tc.get("name"), "arguments": tc.get("arguments"), "ok": tc.get("ok"),
          "chunk_ids": tc.get("chunk_ids") or []} for tc in r.tool_calls or []],
        ensure_ascii=False, default=str)
    obj = getattr(knowledge, "api_name", "")
    return {
        "question": q.question,
        "answer": r.answer or "",
        "category": q.category,
        "answerable": "true" if q.answerable else "false",
        "expected_facts": facts,
        "expected_tools": json.dumps(list(q.expected_tools or [])),
        "gold_chunks": gold,
        "gold_context": gold,
        "knowledge_excerpts": gold,
        "tool_calls": tool_calls,
        "tools_called": json.dumps([tc.get("name") for tc in r.tool_calls or []]),
        "object_name": obj,
        "api_name": obj,
    }


def _render_judge_user(q: "Question", r: "AgentResult", knowledge: "ObjectKnowledge") -> str:
    """Render ``JUDGE_USER`` through ``prompts.render`` (strict on missing placeholders)."""
    return prompts.render("JUDGE_USER", **_judge_values(q, r, knowledge))


def _coerce_score(v: Any) -> int | None:
    """Clamp a judge score to 0..2 (accepts ints, floats and numeric strings)."""
    try:
        return max(0, min(2, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1", "y"}
    return bool(v)


def _normalize_judge(raw: dict) -> dict:
    """Coerce the judge JSON into ``{correctness, completeness, groundedness, hallucination, missing_knowledge, notes}``."""
    out = dict(raw)
    for key in ("correctness", "completeness", "groundedness"):
        out[key] = _coerce_score(raw.get(key))
    for key in ("hallucination", "missing_knowledge"):
        out[key] = _coerce_bool(raw.get(key, False))
    out["notes"] = str(raw.get("notes") or "")
    return out


def grade_with_judge(q: "Question", r: "AgentResult", llm: "LLM", cfg: "Config",
                     knowledge: "ObjectKnowledge") -> Grade:
    """Deterministic grade plus an LLM-judge verdict. Never raises.

    The judge model is ``cfg.judge_model`` (falls back to ``cfg.model`` inside ``LLM`` when
    ``None``). If the judge call fails, ``judge = {"error": ...}`` and ``passed`` falls back
    to the deterministic rule.
    """
    g = grade_deterministic(q, r, knowledge)
    if r.error and not (r.answer or "").strip():
        g.judge = {"error": f"skipped: agent error ({r.error})"}
        return g
    try:
        messages = [
            {"role": "system", "content": _template("JUDGE_SYSTEM")},
            {"role": "user", "content": _render_judge_user(q, r, knowledge)},
        ]
        model = getattr(cfg, "judge_model", None) or getattr(cfg, "model", None)
        raw = llm.chat_json(messages, model=model,
                            schema_hint='{"correctness":0-2,"completeness":0-2,"groundedness":0-2,'
                                        '"hallucination":bool,"missing_knowledge":bool,"notes":str}')
        g.judge = _normalize_judge(raw if isinstance(raw, dict) else {"notes": str(raw)})
    except Exception as exc:
        g.judge = {"error": f"{type(exc).__name__}: {exc}"}
    g.passed = _decide_pass(q, g, has_error=False)
    return g


def grade_all(questions: Iterable["Question"], results: Iterable["AgentResult"], llm: "LLM | None",
              cfg: "Config", knowledge: "ObjectKnowledge", *, use_judge: bool = True,
              workers: int = 4) -> list[Grade]:
    """Grade every result against its question (matched by ``qid``), in question order.

    Questions without a result get a failing grade. ``llm`` may be ``None`` when
    ``use_judge=False``.
    """
    qs = list(questions)
    by_qid = {r.qid: r for r in results}
    if use_judge and llm is None:
        raise ValueError("grade_all(use_judge=True) needs an llm")

    def _one(q: "Question") -> Grade:
        r = by_qid.get(q.qid)
        if r is None:
            return Grade(qid=q.qid, fact_recall=0.0, facts_missing=list(q.expected_facts or []),
                         tool_hit=False, refusal_correct=None if q.answerable else False,
                         judge={"error": "no agent result"}, passed=False)
        if use_judge:
            return grade_with_judge(q, r, llm, cfg, knowledge)
        return grade_deterministic(q, r, knowledge)

    if not use_judge or workers <= 1:
        return [_one(q) for q in qs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, qs))
