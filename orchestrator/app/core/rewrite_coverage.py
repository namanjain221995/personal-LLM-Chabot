"""Does a rewrite actually carry the whole source? (platform audit #13)

THE DEFECT. A long rewrite of a big pasted source declares itself finished
after silently dropping part of it: the run ends with `stop_reason=complete`
and `truncated=false`, because the MODEL decided it was done — nothing had
stopped it. The person is shown a clean, well-formatted answer with no sign
that most of their document is not in it.

REPRODUCED LIVE, 2026-09-21, Fast, in-process against the real engine, one
call at a time. It is not about size: a synthetic rulebook came back whole at
every size (7 of 7, 68 of 68, 204 of 204, 409 of 409 items at 2k/20k/60k/120k
characters, and 421 of 421 on a second shape). A REAL heterogeneous document
— docs/developer-platform/STANDARDS.md, 120,000 characters, 647 list items —
came back as a 17,431-character answer carrying 44 of its 523 measurable
items, 8.4%, in ONE segment, with `stop_reason=complete`, `truncated=false`
and nothing said. Every "why:" rationale and every "source:" citation in the
document had been dropped.

Two things are wrong and each is fixed at its own layer:

  * the prompt never asked for completeness on this shape of turn. The
    REWRITES paragraph in engines/__init__.py is scoped to "when the user
    gives a sample, template or earlier answer" — a plain "rewrite the rules
    below" has no sample, so that whole paragraph reads as not applying.
    `instruction()` is the clause for this turn, and the count in it is
    COUNTED HERE, never estimated by the model.
  * nothing checked the answer against the source. `shortfall()` counts what
    is measurably missing and writes the sentence the person sees, so a
    truthful ending is a fact this code computed rather than a claim the
    model made about itself.

HOW COVERAGE IS MEASURED. An item is a numbered or bulleted line of the
pasted material. Each item's ANCHOR is a word that occurs in that item and
nowhere else in the source; an item with no such word is not measurable and
is left out of both the numerator and the denominator. An item is covered
when its anchor appears in the answer. This is deliberately blind to shape:
the model restructures 68 numbered rules into 272 nested bullets under
headings and every anchor is still there (measured live, 2026-09-21, 68 of
68 items, 0 reported missing), and it is blind to renumbering, which a
number-matching check is not.

What is reported is therefore a FLOOR, never an estimate: every item counted
missing is an item whose own distinctive word is absent from the answer. A
rewrite that rewords an item away from its anchor is counted missing, so the
note only fires past MIN_MISSING and MISSING_SHARE of the measurable items —
far above the reword noise measured on complete answers (0 of 68 and 0 of 7).

Pure: regex and counting over strings, no model, no I/O. English only (owner
decision, 2026-09-16).
"""
from __future__ import annotations

import functools
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from . import pasted

#: A numbered or bulleted line: "12. ", "12) ", "- ", "* ", "• ".
_ITEM_LINE = re.compile(r"^[ \t]{0,12}(?:\d{1,4}[.)]|[-*+•])[ \t]+\S")
#: A word for anchoring. Hyphens and digits are kept inside the token, so a
#: part number ("BZ-3140") or a version ("v2.1") stays one word.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9/'’-]*")
#: Too common in any English document to identify an item.
_STOP = frozenset(
    """the a an and or of to in on for with by at from as is are be been was were will shall must may
    this that these those it its their there here not no any all each every other than then when where which
    who whom whose how what why into onto over under within without before after during while per via if else
    one two three four five six seven eight nine ten only same also more most less least such about above below
    you your we our they them he she his her i me my us do does did done has have had can could would should
    """.split()
)
#: Shorter than this is not distinctive enough to anchor an item.
MIN_ANCHOR_CHARS = 4

#: Below this many items the source is not the shape this defect is about,
#: and the clause is not worth its tokens. Measured live 2026-09-21: 7 items
#: and 68 items both came back 100% covered with no clause at all.
MIN_ITEMS = 25
#: A shortfall read off fewer anchors than this is a fluke, not a finding.
MIN_MEASURABLE = 8
#: The note needs enough items missing to matter, and enough of them relative
#: to what could be measured.
MIN_MISSING = 3
#: A TRUNCATION — every measurable item past some point absent — is a clean
#: signal, so it needs only this share of the items.
TAIL_SHARE = 0.05
#: SCATTERED gaps are not. Measured 2026-09-21 on a COMPLETE live rewrite (210
#: of 210 items carried over, verified by a marker the harness planted in each
#: one): 16 of 210 anchors were reworded away, 7.6% scattered through an
#: answer that had dropped nothing at all. A scattered shortfall must be far
#: above that reword noise before anything is said about it.
SCATTERED_SHARE = 0.25

#: The asks that mean "carry all of it across". A summarise/shorten/condense
#: ask is the opposite instruction and never gets any of this.
_REWRITE_ASK = re.compile(
    r"\b(?:re-?writ(?:e|es|ing|ten)|rewrote|re-?format(?:s|ted|ting)?|format(?:s|ted|ting)?|"
    r"re-?structur(?:e|es|ed|ing)|re-?phras(?:e|es|ed|ing)|re-?word(?:s|ed|ing)?|paraphras(?:e|es|ed|ing)|"
    r"proof-?read(?:s|ing)?|tidy|clean(?:ed|ing)?\s+up|polish(?:es|ed|ing)?|edit(?:s|ed|ing)?|"
    r"(?:change|make|turn|put|modify|update|adapt|adjust|arrange|redo)\s+(?:it|this|them|these|that)|"
    r"same\s+(?:format|way|shape|style|structure|layout|pattern|template|manner))\b",
    re.I,
)
#: A rewrite INTO ANOTHER LANGUAGE is not measurable here, and a wrong answer
#: is worse than none: the anchors are the SOURCE's own words, so a faithful
#: Spanish rewrite of an English source would measure as every item missing
#: and the person would be told a complete answer is incomplete. Excluded
#: outright — no clause, no note — rather than measured badly. "convert" goes
#: with it: converting to CSV or JSON is a shape change this cannot read.
_NOT_MEASURABLE_ASK = re.compile(
    r"\b(?:translat(?:e|es|ed|ing|ion)|convert(?:s|ed|ing)?|arabic|bengali|chinese|mandarin|"
    r"dutch|english|filipino|french|german|greek|gujarati|hebrew|hindi|indonesian|italian|"
    r"japanese|kannada|korean|malay|malayalam|marathi|nepali|persian|polish|portuguese|"
    r"punjabi|romanian|russian|spanish|swahili|swedish|tamil|telugu|thai|turkish|ukrainian|"
    r"urdu|vietnamese)\b",
    re.I,
)
_SHORTEN_ASK = re.compile(
    r"\b(?:summari[sz](?:e|es|ed|ing)|summary|tl;?dr|condens(?:e|es|ed|ing)|shorten(?:s|ed|ing)?|"
    r"abridge(?:d|s)?|cut\s+(?:it\s+)?down|key\s+points|highlights|bullet\s+points?\s+only|"
    r"in\s+(?:a\s+)?(?:few|\d+)\s+(?:words|lines|sentences|bullets))\b",
    re.I,
)


def _words(text: str) -> List[str]:
    return [
        t for t in _TOKEN.findall(text.lower())
        if len(t) >= MIN_ANCHOR_CHARS and t not in _STOP
    ]


def _item_lines(material: str) -> List[str]:
    return [ln for ln in material.split("\n") if _ITEM_LINE.match(ln)]


def _pairs(words: Sequence[str]) -> Set[str]:
    """Adjacent content-word pairs, as "first second"."""
    return {f"{a} {b}" for a, b in zip(words, words[1:])}


def _pick(candidates: Set[str], counts: Counter) -> Optional[str]:
    # The rarest, longest first among equals: a long rare word survives a
    # rewording more often than a short one. Only something that occurs in
    # exactly ONE item identifies that item.
    best = min(candidates, key=lambda t: (counts[t], -len(t)), default=None)
    return best if best is not None and counts[best] == 1 else None


def _anchors(items: Sequence[str]) -> List[Optional[str]]:
    """Per item, something of its own that occurs in no other item, or None.

    A single word where one is unique; otherwise an adjacent PAIR of words.
    The pair matters: a rulebook that names its assets by place and type
    ("the Ashfield pump", "the Ashfield chiller", "the Barrowden pump")
    shares every individual word between items and has no unique word at all
    — 0 of 421 items measurable on words alone, 421 of 421 on pairs.
    """
    words = [_words(it) for it in items]
    uni: Counter = Counter()
    bi: Counter = Counter()
    for ws in words:
        uni.update(set(ws))
        bi.update(_pairs(ws))
    out: List[Optional[str]] = []
    for ws in words:
        out.append(_pick(set(ws), uni) or _pick(_pairs(ws), bi))
    return out


@dataclass(frozen=True)
class Source:
    """The pasted source of a rewrite, as countable items."""

    #: How many numbered/bulleted items the pasted material holds. This is
    #: the number the prompt's completeness clause states (engines/chat.py
    #: COVER_SOURCE_NOTE): counted here, never worked out by the model.
    items: int
    #: (item index, anchor) for the items that can be measured.
    anchors: Tuple[Tuple[int, str], ...]


@dataclass(frozen=True)
class Shortfall:
    """What is measurably missing from an answer, and the line to show."""

    items: int
    measurable: int
    missing: int
    #: 1-based source item the answer is proved to reach (0 = nothing matched).
    covered_through: int
    #: The missing items sit BEHIND `covered_through`, not scattered through
    #: the answer: the rewrite stopped rather than skipped about.
    tail: bool

    @property
    def note(self) -> str:
        # Only what was MEASURED is said. "Nothing after item k could be
        # found" is the exact reading of the anchors; "the items after that
        # are not in it" would also speak for the items no anchor could
        # measure.
        if self.tail and 0 < self.covered_through < self.items:
            return (
                "\n\n---\n\n**This rewrite is not complete.** Nothing after "
                f"item {self.covered_through} of the {self.items} items you "
                "pasted could be found in it. Ask me to carry on from item "
                f"{self.covered_through + 1} and I will write the rest."
            )
        return (
            "\n\n---\n\n**This rewrite is not complete.** At least "
            f"{self.missing} of the {self.items} items in the text you pasted "
            "are missing from it. Ask me for the missing ones and I will "
            "write them out."
        )

    def as_meta(self) -> dict:
        return {
            "items": self.items,
            "measurable": self.measurable,
            "missing": self.missing,
            "covered_through": self.covered_through,
        }


@functools.lru_cache(maxsize=pasted._CACHE_SIZE)
def source_of(message: str) -> Optional[Source]:
    """The countable source of a rewrite ask over pasted text, or None.

    None for everything else: a message with no paste, a summarise/shorten
    ask (where dropping detail IS the instruction), a translation or format
    conversion (which this cannot measure), and a source with fewer than
    MIN_ITEMS listed items.
    """
    read = pasted.read(message)
    if read is None:
        return None
    ask = " ".join(read.asks)
    if _SHORTEN_ASK.search(ask) or _NOT_MEASURABLE_ASK.search(ask):
        return None
    if not _REWRITE_ASK.search(ask):
        return None
    items = _item_lines("\n".join(read.material))
    if len(items) < MIN_ITEMS:
        return None
    anchors = _anchors(items)
    return Source(
        items=len(items),
        anchors=tuple((i, a) for i, a in enumerate(anchors) if a),
    )


def shortfall(message: str, answer: str) -> Optional[Shortfall]:
    """What this answer measurably failed to carry over, or None.

    None when the turn is not a countable rewrite, when too few items can be
    measured to say anything, or when what is missing is within the noise a
    rewording produces.
    """
    source = source_of(message)
    if source is None or len(source.anchors) < MIN_MEASURABLE:
        return None
    words = _words(answer)
    have = set(words) | _pairs(words)
    measurable = len(source.anchors)
    covered = [i for i, anchor in source.anchors if anchor in have]
    missing = measurable - len(covered)
    last = max(covered) if covered else -1
    # Everything measurable past `last` is missing by construction, and it is
    # missing as one CONTIGUOUS RUN: the answer stopped there. That run is
    # judged on its own size, never as a share of the total missing — a
    # truncated answer also carries the scattered reword noise below, and
    # netting the two against each other is what let a fifth of a source go
    # unreported while each gate separately stayed quiet.
    after = sum(1 for i, _ in source.anchors if i > last)
    tail = after >= max(MIN_MISSING, TAIL_SHARE * measurable)
    if not tail and missing < max(MIN_MISSING, SCATTERED_SHARE * measurable):
        return None
    return Shortfall(
        items=source.items,
        measurable=measurable,
        missing=missing,
        # 1-based, and only as far as the LAST item proved present.
        covered_through=last + 1,
        tail=tail,
    )
