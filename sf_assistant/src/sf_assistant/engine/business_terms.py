"""Business terms the owner defined: one definition that the record agent reads and code acts on (2026-11-16, after
the page answered "placement rate" from onboarding completion and "placed" from the candidate status).

The definitions are the prompt file ``prompts/business_terms.md`` (one line per term, ``<key>: <definition>``), bound
as :data:`BUSINESS_TERMS_TEXT` and read into :data:`DEFINITIONS`. Code holds, per term, the question words that name it
(:data:`TERM_WORDS`, common misspellings included) and the stored values that only share its word
(:data:`NOT_THE_TERM`, object, field and value). For a question that names a term, the record hints
(``tools/names.py`` ``object_hints``) list its definition and leave those values out of the stored values, of the
records named in the question (``tools/value_spots.py``) and of the spelling help's did-you-means
(``tools/record_tools.py``), so the hint never points the model at the other reading; the other values of the same
fields stay (a question may filter on them). The knowledge files say the same in words (``kb_build/curated``:
Account, Interview__c and Onboarding__c).

Business-term contracts of 2026-11-24: the terms the record tools enforce by code (candidates, support person
performance) keep their question words with their contract in :mod:`sf_assistant.engine.term_contracts`; a term that
the hints show too (support person performance) is added to the words here with :func:`register` when that module is
imported, which :func:`terms_in` makes sure of. Placement keeps its words here.

Standard library and :mod:`sf_assistant.prompts` only (``engine.term_contracts`` is imported inside a function: it
imports this module).
"""

from __future__ import annotations

import re
from typing import Any

from sf_assistant import prompts as _prompt_files

__all__ = ["BUSINESS_TERMS_TEXT", "DEFINITIONS", "TERM_WORDS", "NOT_THE_TERM", "terms_in", "term_lines",
           "not_the_term", "is_not_the_term", "TERMS_HEADER", "register", "HINGLISH_CANDIDATES"]

BUSINESS_TERMS_TEXT = _prompt_files.text("business_terms")
DEFINITIONS: dict[str, str] = dict(ln.split(": ", 1) for ln in BUSINESS_TERMS_TEXT.split("\n") if ": " in ln)

#: "placement" with its common misspellings (placment, plcement, placemnt, placments) and "placed" with its (placd,
#: plced). Review of 2026-11-16: a misspelt word skipped the definition.
_PLACEMENT = r"pla?ce?m[ea]?n?ts?"
#: "placed", never "placed on hold / pause" (the Hold status) and never "placed an order / a call / a request".
_PLACED = r"pla?ce?d(?!\s+(?:on\s+(?:hold|pause)|an?\s+(?:order|call|request|bid|booking))\b)"
#: The people a placement is about: "placed" names the term only with one of them as its subject (or in a hiring
#: phrase), so "sections placed in a template" and "calls placed by recruiters" name no placement.
#: The Hinglish words for candidates (ummeedwar, chhatra, vidyarthi, bacche and their common spellings); the candidate
#: contract (``engine.term_contracts``) reads the same list (review fixes of 2026-11-24: "kitne bacche placed hue").
HINGLISH_CANDIDATES = (r"ummeedwar|umeedwar|ummedwar|umedwar|ummidwar|umidwar|ummidvar|umidvar|chhatra|chatra|"
                       r"vidyarthi|vidyarthiyon|bacche|bachche|bachhe|bacchon|bachchon|bachon|bachhon")
_PERSON = rf"(?:candidates?|students?|people|persons?|consultants?|trainees?|who|anyone|someone|{HINGLISH_CANDIDATES})"
#: The words of a question that name a term. "placed on hold" is the Hold status, the onboarding's "placement
#: services agreement" is a document, "placement of ads" and "pre-placement checks" are no placement, and "placed"
#: without a person or a hiring phrase is another verb (review of 2026-11-16).
TERM_WORDS: dict[str, re.Pattern] = {
    "placement": re.compile(
        rf"(?<![\w-]){_PLACEMENT}\b(?!\s+(?:services?|agreements?|of)\b)"
        rf"|\b{_PERSON}\b[^.?!]{{0,40}}?\b{_PLACED}\b"
        rf"|\b(?:got|get|gets|getting|been|be)\s+{_PLACED}\b"
        rf"|\b{_PLACED}\s+(?:candidates?|students?|people|consultants?)\b"
        rf"|\bhow\s+many\s+(?:were\s+|got\s+|have\s+been\s+)?{_PLACED}\b",
        re.IGNORECASE),
}
#: The stored values (object, field, value) that only share a term's word: never hinted for that term. Only the
#: value itself: the other values of the field stay (a question that names the term may filter on them).
NOT_THE_TERM: dict[str, frozenset[tuple[str, str, str]]] = {
    "placement": frozenset({("Account", "Candidate_Status__c", "Placed")}),
}
TERMS_HEADER = "Business terms of the question (the owner's definitions; use exactly these):"
#: The hinted terms whose words live with their contract (engine.term_contracts, 2026-11-24), by :func:`register`.
_REGISTERED: dict[str, re.Pattern] = {}


def register(key: str, words: re.Pattern, not_the_term: frozenset[tuple[str, str, str]] = frozenset()) -> None:
    """Add a term whose question words live with its contract (``engine.term_contracts``): the hints show its
    definition for a question its words match, and leave ``not_the_term`` values out (:data:`NOT_THE_TERM`)."""
    _REGISTERED[key] = words
    if not_the_term:
        NOT_THE_TERM[key] = frozenset(not_the_term)


def _words() -> dict[str, re.Pattern]:
    """:data:`TERM_WORDS` and then the registered terms (the contracts module registers its own at import)."""
    from sf_assistant.engine import term_contracts  # noqa: F401 - registers the contracts' hinted terms
    return {**TERM_WORDS, **_REGISTERED}


def terms_in(question: Any) -> list[str]:
    """The keys of the terms the question names, in :data:`TERM_WORDS` order, then the registered terms."""
    text = question if isinstance(question, str) else ""
    return [k for k, rx in _words().items() if k in DEFINITIONS and rx.search(text)]


def term_lines(question: Any) -> list[str]:
    """The hint lines of the terms the question names (:data:`TERMS_HEADER` first), or []."""
    keys = terms_in(question)
    return [TERMS_HEADER] + [f"- {DEFINITIONS[k]}" for k in keys] if keys else []


def not_the_term(question: Any) -> frozenset[tuple[str, str, str]]:
    """The (object, field, value) stored values the hints leave out for this question: those of every term it
    names."""
    out: set[tuple[str, str, str]] = set()
    for k in terms_in(question):
        out |= NOT_THE_TERM.get(k, frozenset())
    return frozenset(out)


def is_not_the_term(other: Any, obj: Any, field: Any, value: Any) -> bool:
    """The stored value ``value`` of ``obj.field`` is one of ``other`` (:func:`not_the_term`), case aside."""
    v = str(value).strip().casefold()
    return any(o == str(obj) and f == str(field) and x.casefold() == v for o, f, x in other or ())
