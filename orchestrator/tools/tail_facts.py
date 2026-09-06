"""Is the text BEYOND the chunk cap worth indexing? — an instrument, not an opinion.

    python -m tools.tail_facts scan --out /tmp/tail_facts.json
    python -m tools.tail_facts scan --sample 12          # eyeball the probes
    python -m tools.tail_facts show --page-id 2465       # read one tail by hand

WHY THIS EXISTS. ``web_index.chunk_page`` stops at ``_MAX_CHUNKS_PER_PAGE``
chunks, which is ``INDEXED_CHARS_PER_PAGE`` = 179,600 characters of one page.
Measured on the live corpus (2026-09-07): only **59 of 2,063 indexable pages**
exceed that, and those 59 hold **26.5% of every character the platform has
stored**. That coverage number has been quoted in three documents as if it
were a reason to raise the cap. It is not a reason. Coverage says how much
text is missing; it says nothing about whether anything a user would ask is
in it.

THE QUESTION THIS TOOL ANSWERS. Take the part of each oversized page the
chunker never reaches — the UNINDEXED TAIL — and try, mechanically, to build
a factual probe out of it: a distinctive checkable string plus a question
whose answer is exactly that string. Then count.

**The single number that matters is the ratio of pages that yielded a
plausible probe to pages that yielded only junk.** If most tails are an
atom feed's escaped MathML, a uuencoded EDGAR attachment or six years of
somebody's quote file, then 26.5% of the corpus is 26.5% of nothing and the
cap should stay where it is. That negative result is worth more than a set of
unanswerable probes, so this tool is built to be able to return it: every
rejection is counted and named, and a page with no probe is reported with the
reason it had none rather than quietly omitted.

A SECOND GATE, AND IT IS THE STRICTER ONE. A fact in the tail is only worth
indexing if the indexed HEAD does not already state it. A 2 MB journal
archive whose tail repeats the same "about me" block as its head adds nothing
retrievable however many characters it holds. Every probe therefore carries
``novel``: false when the answer string occurs in the head within 1,200
characters of its own subject. **Pages yielding a novel probe is the number
that argues for raising the cap; pages yielding any probe is the ceiling on
it.**

WHAT IT MEASURED, 2026-09-07, and the correction the numbers need. Mechanical
yield: **51 of 59 pages produced a probe, 8 produced only junk** (86.4%), and
50 of those 51 produced a probe the head does not already state. That is an
UPPER BOUND and must never be quoted alone. A hand grading of the same output
— 40 probes drawn at random with `--sample-random`, then every page's emitted
probes read — put probe-level precision at **12/40 (30%)** and page-level
yield at **41 of 59 (69%)**, because a page needs only one good probe out of
five. Ten pages the tool called "yielded" hold nothing anybody would ask: a
2 MB quote file, a scanned admission list, five personal microblog archives,
two Wikipedia bibliographies, a prospectus signature block. Applying the
stricter reading — a fact a user of THIS corpus would plausibly ask — leaves
**22 of 59 pages and 35% of the 15.3 M tail characters**. `--grade-file`
exists so that grading is redone rather than inherited.

NO MODEL IS CALLED. Not the LLM, not the embedding service, not the reranker.
The GPU on this box is shared with production inference and every number in
this engagement that touches it is being measured serially elsewhere. Probe
extraction here is regexes, character-class statistics and repetition counts
— which is also why the output is a CANDIDATE set: a human (or a later,
budgeted model pass) grades it. ``score`` is this tool's confidence that a
probe is askable, never a claim that it is a good question.

READ-ONLY, STRUCTURALLY. The connection is opened with
``default_transaction_read_only=on`` and ``connection.read_only``, so a
mistaken UPDATE raises instead of writing; the only statement in this file is
one SELECT against ``web_pages``. It touches no conversation, message, upload
or Salesforce table, and it never opens LanceDB — a page's stored text is all
this measurement needs.

ON THE OUTPUT FILE. It contains verbatim substrings of ``web_pages.text``,
which is the SHARED PUBLIC corpus — pages fetched from public URLs through
``core.net.safe_fetch``, with no owner column and no private data in it (the
privacy boundary was verified separately and holds). It still holds third-
party page text, so treat the JSON as an artefact to hand to a reviewer, not
as something to paste into a log.

INVOCATION. The deployed image does not carry this file, and copying it into
a running production container is not worth the blast radius, so it runs on
the host against the published port:

    cd orchestrator
    APP_DATABASE_URL="postgresql://techsara:$PW@127.0.0.1:5432/techsara" \\
        .venv/bin/python -m tools.tail_facts scan --out ../tail_facts.json

``--dsn`` and the libpq ``PG*`` environment variables work too. Inside the
container (if this file is ever baked in) ``APP_DATABASE_URL`` is already set
and no argument is needed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

if os.path.isdir("/app"):  # inside the orchestrator container
    sys.path.insert(0, "/app")
else:  # running from the checkout: orchestrator/ is the import root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app import web_index  # noqa: E402

#: The ceiling under test. Imported, never re-derived: the whole point is to
#: measure the cap the running chunker actually applies, and it is a function
#: of three constants that have moved before.
CAP = web_index.INDEXED_CHARS_PER_PAGE

#: What counts as a pipe table. Reused from the chunker on purpose — the
#: table-header repair (CHUNKER_VERSION 2) and this measurement must agree on
#: which lines are a table, or the probes would describe a structure the index
#: does not see. Private name, deliberately: there is no public spelling, and
#: a tool breaking loudly on a rename is better than two divergent copies.
_pipe_table_runs = web_index._pipe_table_runs

# ---------------------------------------------------------------------------
# Tunables. Every one of these is a judgement about what a person would ask,
# so each says what it is protecting against rather than just holding a number.
# ---------------------------------------------------------------------------

#: A line longer than this is not a line, it is a paragraph that lost its
#: newlines (an SEC 10-K arrives as ONE 315,854-character line). Split it into
#: sentences or every probe on such a page would quote a page-sized "segment".
_LONG_SEGMENT_CHARS = 600

#: Shorter than this and there is nothing to build a question out of — it is a
#: nav label, a date stamp or a table caption fragment.
_MIN_SEGMENT_CHARS = 24

#: Longer than this and the "answer" is a paragraph, not a checkable string.
_MAX_ANSWER_CHARS = 80

#: A subject phrase longer than this is a sentence, and a question built on it
#: reads as a quotation rather than a question.
_MAX_SUBJECT_CHARS = 70

#: Exact repetition INSIDE one tail at or above this count is furniture: a
#: feed's "Read more", a template sentence, a repeated table caption. It is
#: also unanswerable by construction — a question whose context occurs eight
#: times has eight answers.
_REPEAT_IN_PAGE = 3

#: The same normalised segment on this many DIFFERENT pages is boilerplate
#: that travels (cookie notices, CMS chrome, syndicated blurbs).
_REPEAT_ACROSS_PAGES = 3

#: How far apart the answer and its subject may sit in the indexed head before
#: we stop calling it "the head already says this". One chunk is 3,200 chars;
#: 1,200 keeps it well inside a single retrievable window.
_NOVELTY_WINDOW = 1200

#: Rows sampled per table. The unicorn-companies page alone would otherwise
#: contribute thousands of probes and drown the ratio this tool exists to
#: report. Sampling is spread across the table, not taken from the top.
_ROWS_PER_TABLE = 4

#: Probes kept per page in the emitted set. Does NOT affect the yielded /
#: junk-only ratio, which is computed before any capping.
_PROBES_PER_PAGE = 5

#: Below this a candidate is recorded but not counted as "a plausible probe".
_DEFAULT_MIN_SCORE = 0.50

#: How many segments back a labelled field may look for the heading it belongs
#: to. Twelve was too generous: a `Processor: …` line 11 segments below an
#: unrelated article title produced a question about the wrong thing. Six keeps
#: the subject in the same visual block as the field.
_SUBJECT_LOOKBACK = 6

#: Segments either side of a candidate that are inspected for shell prompts.
_TRANSCRIPT_RADIUS = 2

# ---------------------------------------------------------------------------
# Junk detection
# ---------------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

#: A base64 / uuencode / hex run. EDGAR ships whole binary attachments inside
#: the .txt filing and trafilatura keeps them: 2.2 MB of one page is this.
_BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{60,}|(?:[0-9a-fA-F]{2}\s?){40,}")

#: uuencoded lines: a length byte then 60-ish printable non-space characters.
_UU_RE = re.compile(r"^[!-~]{45,90}$")

#: A surviving HTML/XML tag AFTER unescaping. An atom feed's content is
#: double-escaped markup: `&lt;math&gt;` unescapes to `<math>` and is furniture
#: in any question we could ask about it.
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]{0,30}(?:\s[^<>]{0,120})?/?>")

#: Numeric character references that survive one unescape pass (`&amp;#160;`).
_ENTITY_RE = re.compile(r"&(?:#x?[0-9A-Fa-f]+|[a-zA-Z]{2,10});")

_URL_RE = re.compile(r"https?://\S+|\[[^\]]{0,80}\]\([^)]{0,200}\)")

#: A bibliography entry. Wikipedia's tail past 179,600 characters is almost
#: entirely this — "↑ Bump, Philip (April 4, 2024). \"…\". The Washington Post.
#: Archived from the original on … Retrieved …" — and every one of them is a
#: dated statement naming a recurring entity, so without this rule the two
#: largest Wikipedia pages in the corpus "yield" 343 probes about who
#: retrieved which newspaper when. Citation metadata is provenance, not
#: knowledge: the fact is in the article, which is in the head.
_CITATION_RE = re.compile(
    r"^\s*[-*•]?\s*↑"
    r"|\bRetrieved\s+(?:\d{1,2}\s+\w+|\w+\s+\d{1,2},?)\s+\d{4}"
    r"|\bArchived from the original\b"
    r"|\bdoi:|\bISBN\b|\bISSN\b|\barXiv:|\bPMID\b"
    r"|\bWayback Machine\b|\(Podcast\)|\(PDF\)"
    # "Surname, Forename (12 March 2024). \"Headline\". Publisher." — the
    # house style of a Wikipedia reference, and the half of them that carry
    # no "Retrieved" survived the rule above until this alternative existed.
    r"|^\s*[-*•]?\s*(?:[0-9 ]+)?[A-Z][A-Za-z'’-]+,\s+[A-Z][A-Za-z.'’-]+"
    r"(?:;|\s*\([^)]{4,30}\))\s*[.\"“]"
    # `- 1 2 3 "Headline". Publisher.` — a reference reused by several
    # footnotes, which is how Wikipedia writes its most-cited sources.
    r"|^\s*[-*•]?\s*(?:\d+\s+){0,6}[\"“][^\"”]{6,140}[\"”]\s*\.",
    re.IGNORECASE,
)

#: Site furniture that survives extraction as prose. Each of these is a phrase
#: no page ASSERTS — it is the platform talking about itself. The scribd
#: "related documents" rail alone produced 243 confident probes about how many
#: pages some unrelated PDF has.
_CHROME_PHRASES = (
    "no ratings yet",
    "found this document useful",
    "related titles",
    "download as pdf",
    "save for later",
    "add to wishlist",
    "you might also like",
    "recommended for you",
    "share on facebook",
    "share on twitter",
    "jump to content",
    "table of contents",
    "click here to",
    "sign in to",
    "cookies to improve",
)

#: Site chrome. Anchored, because "comments" inside a sentence is not chrome.
_BOILERPLATE_RE = re.compile(
    r"^(?:"
    r"read (?:more|the rest)|continue reading|comments?(?:\s*\(\d+\))?|"
    r"share (?:this|on)|posted (?:in|on|by|at)|filed under|tags?|categor(?:y|ies)|"
    r"permalink|back to top|skip to (?:main )?content|subscribe|newsletter|"
    r"copyright|all rights reserved|privacy policy|terms of (?:use|service)|"
    r"cookie|advertisement|sponsored|next(?: page| post)?|previous(?: page| post)?|"
    r"older posts?|newer posts?|home|menu|navigation|search|log ?in|sign ?up|"
    r"follow (?:us|me)|rss|atom|toggle|edit|source|view source|"
    r"©\s*\d{4}|\d+\s+comments?"
    r")\b[\s:|-]*$",
    re.IGNORECASE,
)

#: A bare archive stamp: "March 2019", "2019", "Week 3", "- 2008".
_STAMP_RE = re.compile(
    rf"^[-*\s]*(?:(?:{_MONTHS})\s+)?\d{{1,4}}(?:\s*[-–]\s*\d{{1,4}})?\s*$",
    re.IGNORECASE,
)

#: A quotation-file entry: the attribution line under an aphorism. This is
#: what 1.8 MB of drwho.plan.txt's tail is, and no user asks it anything.
_ATTRIBUTION_RE = re.compile(r"^\s*(?:--|—|―)\s*\S")

_STOPWORDS = frozenset(
    """a an and are as at be been but by for from had has have he her his i if in
    into is it its of on or our she that the their them then there these they this
    to was we were what when where which who will with would you your not no can
    could may might must should do does did done been being about after all also
    any because before between both during each few how more most other over same
    so some such than too under until up very via while""".split()
)

#: Capitalised words that are not names. Without this every sentence-initial
#: "The" becomes a proper noun and every prose line looks like a fact.
_NOT_A_NAME = frozenset(
    w.lower()
    for w in """The This That These Those There Here It Its A An And But Or For Nor
    So Yet If When While Where What Which Who Whom Whose How Why After Before During
    In On At By From To With Without Within Into Over Under Above Below As Because
    Although Though Since Until Unless However Moreover Therefore Thus Also Not No
    Yes We You They He She I Our Your Their His Her My One Two Three Four Five Six
    Seven Eight Nine Ten First Second Third Next Last New Old Total All Some Most
    Many Much More Less Every Each Both Either Neither Other Another Same Such Only
    Just Now Then Today Tomorrow Yesterday Monday Tuesday Wednesday Thursday Friday
    Saturday Sunday January February March April May June July August September
    October November December Read More See Note Source Update Edit Posted""".split()
)

# ---------------------------------------------------------------------------
# Value shapes — what counts as a checkable answer
# ---------------------------------------------------------------------------

_CURRENCY = r"[$€£¥₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:billion|million|trillion|bn|mn|k|crore|lakh))?"
_PERCENT = r"\d[\d,]*(?:\.\d+)?\s?(?:%|per\s?cent)"
_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_SLASH_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}"
_LONG_DATE = rf"(?:\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}|(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}})"
_VERSION = r"\d+(?:\.\d+){1,3}"
_UNIT = (
    r"\d[\d,]*(?:\.\d+)?\s?(?:GB|MB|KB|TB|GiB|MiB|kW|MW|GW|ms|s|kg|km|m|ft|mi|"
    r"tokens?|tok/s|pages?|words?|users?|employees?|seats?|hours?|days?|weeks?|"
    r"months?|years?|people|deaths?|casualties)\b"
)
_PLAIN_NUMBER = r"\d[\d,]*(?:\.\d+)?"

#: Ordered: the first alternative that matches wins, so `$1.2 billion` is one
#: currency answer and not the plain number `1.2`.
_VALUE_RE = re.compile(
    "|".join(
        f"(?P<{name}>{pat})"
        for name, pat in (
            ("currency", _CURRENCY),
            ("percent", _PERCENT),
            ("iso_date", _ISO_DATE),
            ("long_date", _LONG_DATE),
            ("slash_date", _SLASH_DATE),
            ("unit", _UNIT),
            ("version", _VERSION),
            ("number", _PLAIN_NUMBER),
        )
    ),
    re.IGNORECASE,
)

_DATE_FLAVOURS = frozenset({"iso_date", "long_date", "slash_date"})

#: `Label: value` where the WHOLE line is the pair. Deliberately narrow: an
#: earlier version accepted any colon, and a page of prose ("The four
#: sysadmins of the apocalypse: edquota, rm -rf /, …", a headline inside a
#: citation) turned into hundreds of confident nonsense probes. A colon in a
#: sentence is punctuation, not a schema.
_INLINE_LABEL_RE = re.compile(
    r"^[-*•]?\s*(?P<label>[A-Za-z][A-Za-z /()%.#-]{1,28}?)\s*:\s+(?P<value>\S.{0,59})$"
)

#: A list-shaped label with its value on the NEXT line — how trafilatura
#: renders a `<dl>` and how deanebarker.net's reading list arrives:
#:   - Pages
#:   - 224
_LIST_ITEM_RE = re.compile(r"^\s*[-*•]\s*(\S.{0,60})$")

_PROPER_NOUN_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&.'’-]{1,}(?:\s+(?:of|the|and|for|de|van|von)\s+)?)"
    r"(?:\s+[A-Z][A-Za-z0-9&.'’-]{1,}){0,3}\b"
)

#: A shell / REPL transcript. A technical blog pastes whole terminal sessions
#: and trafilatura keeps them, so `nginx: 1.14.1,2` and `FS type: [4.2BSD]`
#: look exactly like labelled fields. They are facts about the author's
#: machine at one moment, not about the world, and nobody asks the corpus for
#: them — so a label/value pair with a command line in its neighbourhood is
#: dropped.
_COMMAND_RE = re.compile(
    r"^\s*(?:[\$#%>]\s|\>\>\>\s|(?:sudo|doas|apt|apt-get|yum|dnf|pacman|"
    r"pkg|pkg_add|brew|git|npm|yarn|pip|pip3|curl|wget|scp|ssh|make|cmake|cd|ls|"
    r"cat|less|tail|head|grep|sed|awk|vim|vi|nano|emacs|docker|podman|kubectl|"
    r"systemctl|service|mount|umount|fdisk|disklabel|newfs|ifconfig|ip|dmesg|"
    r"python|python3|node|go|cargo|rustc|gcc|cc|man|export|chmod|chown|mkdir|rm|"
    r"cp|mv|tar|unzip|gzip)\b)",
    re.IGNORECASE,
)

_ATTRIBUTE_RE = re.compile(
    r"\b(?P<subj>[A-Z][A-Za-z0-9&.'’-]{2,}(?:\s+[A-Z][A-Za-z0-9&.'’-]{1,}){0,3})\s+"
    r"(?P<verb>is|was|are|were|has|have|had|became|remains|reached|costs?|"
    r"scored|ranks?|holds?|reports?|employs?)\s+"
    r"(?P<obj>[^.;:!?]{4,80}?)(?=[.;:!?]|$)"
)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """One addressable piece of the tail, with an exact offset into it."""

    start: int
    text: str
    #: html-unescaped and whitespace-collapsed; junk tests read this, probes
    #: quote `text` so an emitted answer is always a real substring of the page.
    norm: str
    is_table_row: bool = False
    #: True when this segment is a whole source LINE. A `Label: value` shape is
    #: only trustworthy on a whole line: inside a paragraph a colon is
    #: punctuation, and "assassins. Japan: Japan." became a probe until this
    #: distinction existed.
    is_line: bool = True


def _sentence_spans(text: str) -> List[Tuple[int, str]]:
    """Split a runaway line on sentence ends, keeping offsets.

    Fragments shorter than `_MIN_SEGMENT_CHARS` are glued onto the next one:
    "Inc." and "No. 3" split badly and a two-word fragment cannot host a probe.
    """
    out: List[Tuple[int, str]] = []
    pending_start: Optional[int] = None
    pos = 0
    for part in re.split(r"(?<=[.!?])\s+", text):
        if not part:
            pos += 1
            continue
        start = text.find(part, pos)
        if start < 0:  # defensive: re.split never loses text, but offsets rule
            start = pos
        if pending_start is None:
            pending_start = start
        end = start + len(part)
        if end - pending_start >= _MIN_SEGMENT_CHARS:
            out.append((pending_start, text[pending_start:end]))
            pending_start = None
        pos = end
    if pending_start is not None and pending_start < len(text):
        out.append((pending_start, text[pending_start:].rstrip()))
    return out


def _normalise(text: str) -> str:
    """What the junk tests read: entities resolved, whitespace collapsed."""
    out = html.unescape(text)
    out = unicodedata.normalize("NFKC", out)
    return re.sub(r"\s+", " ", out).strip()


def segments(tail: str) -> List[Segment]:
    """Every candidate-bearing piece of `tail`, in order, with true offsets.

    Invariant asserted by `--self-check`: `tail[s.start:s.start+len(s.text)]`
    is `s.text` for every segment.
    """
    out: List[Segment] = []
    pos = 0
    for line in tail.splitlines(keepends=True):
        lead = len(line) - len(line.lstrip())
        core = line.strip()
        if core:
            base = pos + lead
            table_row = core.startswith("|") and core.count("|") >= 2
            if len(core) > _LONG_SEGMENT_CHARS and not table_row:
                for off, sent in _sentence_spans(core):
                    out.append(
                        Segment(base + off, sent, _normalise(sent), is_line=False)
                    )
            else:
                out.append(Segment(base, core, _normalise(core), table_row))
        pos += len(line)
    return out


# ---------------------------------------------------------------------------
# Junk gates
# ---------------------------------------------------------------------------


def _word_tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’-]+", text)


def junk_reason(
    seg: Segment,
    *,
    next_text: str,
    in_page_count: int,
    across_page_count: int,
) -> Optional[str]:
    """Why this segment cannot host a question — or None if it might.

    Ordered most-structural first, so the reported reason is the one an
    operator can act on: "the tail is a binary blob" beats "the tail has few
    content words", which is merely its consequence.
    """
    raw, norm = seg.text, seg.norm

    if len(norm) < _MIN_SEGMENT_CHARS and not seg.is_table_row:
        return "too_short"

    if _BLOB_RE.search(raw) or (_UU_RE.match(raw) and " " not in raw):
        return "encoded_blob"

    # Character-class shape: a line with almost no spaces and heavy symbol use
    # is machine payload however it was encoded.
    symbols = sum(1 for ch in raw if not ch.isalnum() and not ch.isspace())
    if not seg.is_table_row and len(raw) > 40 and symbols / len(raw) > 0.35:
        return "encoded_blob"

    tags = len(_TAG_RE.findall(norm))
    if tags >= 2 or (tags and len(norm) < 200):
        return "markup"
    if len(_ENTITY_RE.findall(norm)) >= 3:
        return "markup"

    if _BOILERPLATE_RE.match(norm) or _STAMP_RE.match(norm):
        return "boilerplate"

    low = norm.lower()
    if any(phrase in low for phrase in _CHROME_PHRASES):
        return "site_chrome"

    if _CITATION_RE.search(norm):
        return "citation"

    if norm.endswith(("…", "...")):
        # An archive index renders each entry as a truncated teaser. The
        # extractor is telling us the text is elided; a fact cut mid-sentence
        # cannot be an answer.
        return "teaser"

    if _ATTRIBUTION_RE.match(norm) or _ATTRIBUTION_RE.match(next_text.strip()):
        # The quote itself and its "--Orson Welles" line are both furniture in
        # a quote file. This is what drwho.plan.txt's 1.8 MB tail is made of.
        return "aphorism"

    stripped_links = _URL_RE.sub("", norm).strip()
    if len(stripped_links) < max(24, int(0.4 * len(norm))):
        return "link_only"

    if in_page_count >= _REPEAT_IN_PAGE:
        return "repeated_in_page"
    if across_page_count >= _REPEAT_ACROSS_PAGES:
        return "repeated_across_pages"

    words = _word_tokens(norm)
    if not seg.is_table_row and len(words) < 5:
        return "no_content_words"

    # Vowel-free alphabetic tokens dominate in hashes, mangled encodings and
    # minified identifiers; real prose has vowels.
    if words:
        voiceless = sum(1 for w in words if not re.search(r"[aeiouyAEIOUY]", w))
        if voiceless / len(words) > 0.4:
            return "encoded_blob"

    return None


# ---------------------------------------------------------------------------
# Probe construction
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    kind: str
    question: str
    question_style: str  # "direct" | "cloze"
    answer: str
    tail_offset: int
    subject: str
    label: str
    context: str
    context_tail_offset: int
    score: float = 0.0
    novel: bool = True
    why: str = ""

    def as_json(self, page: "Page", probe_id: str) -> Dict[str, Any]:
        return {
            "probe_id": probe_id,
            "page_id": page.page_id,
            "url": page.url,
            "kind": self.kind,
            "question": self.question,
            "question_style": self.question_style,
            "answer": self.answer,
            "tail_offset": self.tail_offset,
            "tail_length": len(self.answer),
            "page_offset": CAP + self.tail_offset,
            "context": self.context,
            "context_tail_offset": self.context_tail_offset,
            "subject": self.subject,
            "label": self.label,
            "novel": self.novel,
            "score": round(self.score, 3),
            "why": self.why,
        }


def _looks_like_name(phrase: str) -> bool:
    """A phrase a person could put in a question and mean one thing."""
    words = _word_tokens(phrase)
    if not words:
        return bool(re.search(r"\d", phrase))
    named = [w for w in words if w[:1].isupper() and w.lower() not in _NOT_A_NAME]
    if named:
        return True
    return bool(re.search(r"[\d]", phrase)) and len(words) >= 2


def _content_terms(phrase: str) -> List[str]:
    return [w.lower() for w in _word_tokens(phrase) if w.lower() not in _STOPWORDS]


def _value_matches(text: str) -> List[Tuple[int, int, str, str]]:
    """(start, end, flavour, text) for every checkable value in `text`."""
    out: List[Tuple[int, int, str, str]] = []
    for m in _VALUE_RE.finditer(text):
        flavour = m.lastgroup or "number"
        out.append((m.start(), m.end(), flavour, m.group(0)))
    return out


#: Flavours worth building a prose probe around. A BARE number in running text
#: is nearly always a footnote marker, a roll number or half a date, and a
#: two-part `4/3` is a British date shorthand nobody can grade. Structured
#: shapes (a table cell, a labelled field) may use any flavour, because the
#: structure is what makes them checkable; prose may not.
_STRONG_FLAVOURS = frozenset(
    {"currency", "percent", "unit", "iso_date", "long_date", "version"}
)


def _url_spans(text: str) -> List[Tuple[int, int]]:
    """Where the links are. A number inside a URL is not a fact about anything:
    a DOI in `journals.plos.org/…?id=10.1371/…` produced probes asking what
    value belongs in the middle of a hyperlink."""
    return [(m.start(), m.end()) for m in _URL_RE.finditer(text)]


def _is_field_label(label: str) -> bool:
    """Does this read as a field NAME rather than a fragment of a sentence?"""
    label = label.strip().rstrip(":")
    if not label or len(label) > 30:
        return False
    if any(ch in label for ch in '"“”‘’<>[]{}|'):
        return False
    words = _word_tokens(label)
    if not (1 <= len(words) <= 3):
        return False
    return all(len(w) >= 2 for w in words)


#: A bullet, at the start of a line. A list item is an ITEM, not the name of
#: the thing the items belong to, and one that was merely too long for
#: `_LIST_ITEM_RE` became the subject of a question ("what is the Failure mode
#: analysis of - Make explicit the goal of maintaining human cognitive
#: capability eve?").
_BULLET_RE = re.compile(r"^\s*[-*•]\s")


def _is_heading(norm: str) -> bool:
    """Can this line name the thing a labelled field belongs to?

    Headings bypass the repetition and length gates (a section title is short
    and may recur), so they get their own test — and it has to be strict,
    because whatever passes here becomes the SUBJECT of a question. An early
    version accepted any short unique line, and a boot log ("Using drive 0,
    partition 3.") and a curl error became the subjects of confident questions
    about RAM sizes. A heading is a NAME: it does not end in sentence
    punctuation, does not carry a measurement, and is not machine output.
    """
    # Never truncated: a subject clipped at `_MAX_SUBJECT_CHARS` reads as a
    # sentence fragment, and a question built on a fragment cannot be graded.
    if not (8 <= len(norm) <= _MAX_SUBJECT_CHARS):
        return False
    if _BULLET_RE.match(norm):
        return False
    if norm.endswith((".", "!", "?", ",", ";")):
        return False
    if norm.count(":") > 1 or any(ch in norm for ch in "=|\\{}<>$~#\"“”"):
        return False
    if "->" in norm or "0x" in norm or "/" in norm.replace(" / ", ""):
        return False
    words = _word_tokens(norm)
    if len(words) < 3:
        return False
    alpha = sum(1 for ch in norm if ch.isalpha() or ch.isspace())
    if alpha / len(norm) < 0.6:
        return False
    if any(f in _STRONG_FLAVOURS for _, _, f, _ in _value_matches(norm)):
        return False  # a measurement is a value, not the name of a thing
    voiceless = sum(1 for w in words if not re.search(r"[aeiouyAEIOUY]", w))
    if voiceless / len(words) > 0.4:
        return False  # a hash, an identifier, a mangled encoding
    return _looks_like_name(norm)


def _is_value_phrase(value: str) -> bool:
    """Short enough, and checkable: a measurement or a short proper name."""
    value = value.strip().rstrip(".,;")
    if not value or len(value) > 60:
        return False
    if _URL_RE.search(value):
        return False
    words = value.split()
    if len(words) > 6:
        return False
    if _value_matches(value):
        return True
    names = [w for w in _word_tokens(value) if w[:1].isupper() and w.lower() not in _NOT_A_NAME]
    return bool(names) and len(_word_tokens(value)) <= 4


def _cells(line: str, base: int) -> List[Tuple[int, str]]:
    """(offset, text) per cell of a pipe row — offsets into the tail."""
    parts = line.split("|")
    if len(parts) < 3:
        return []
    out: List[Tuple[int, str]] = []
    idx = 0
    for i, part in enumerate(parts):
        if 0 < i < len(parts) - 1:
            lead = len(part) - len(part.lstrip())
            out.append((base + idx + lead, part.strip()))
        idx += len(part) + 1
    return out


def _describe(page: "Page") -> str:
    """How a question names its source. Domain is derived, never read straight
    off the row: `web_pages.domain` is '' for pages stored before V16."""
    title = (page.title or "").strip()
    domain = _domain_of(page)
    if title and len(title) <= 90:
        return f'"{title}" ({domain})' if domain else f'"{title}"'
    return domain or page.url


def _cloze(sentence: str, start: int, end: int) -> str:
    """The sentence with the answer replaced by a blank, trimmed to a clause."""
    blanked = sentence[:start] + "___" + sentence[end:]
    blanked = re.sub(r"\s+", " ", blanked).strip()
    if len(blanked) > 300:  # keep the blank in view
        lo = max(0, start - 140)
        hi = min(len(blanked), start + 160)
        blanked = ("… " if lo else "") + blanked[lo:hi].strip() + (" …" if hi < len(blanked) else "")
    return blanked


def _table_probes(
    page: "Page", clean: str, segs: List[Segment], usable: set
) -> List[Probe]:
    """A cell answered by its column header and its row's subject.

    The strongest shape available without a model: the question is genuinely
    natural ("what is the Valuation for FourKites?") and the answer is one
    cell, so grading is exact.

    TABLES ARE DETECTED OVER THE WHOLE PAGE, NOT THE TAIL. A table long enough
    to cross the cap has its header in the indexed HEAD, so a tail-only scan
    sees a header-less block of rows and either finds no table or promotes a
    data row to header. The chunker has exactly this problem and solves it the
    same way — `chunk_page` runs `_pipe_table_runs` over the whole page and
    repeats the header into every chunk that starts inside the table
    (CHUNKER_VERSION 2, findings C1/K3). Measuring the tail with a narrower
    view than the indexer's would have reported "no probe" for
    cbinsights.com/research-unicorn-companies, whose entire tail is unicorn
    rows cut off mid-table — the single clearest case for raising the cap in
    the whole corpus.
    """
    out: List[Probe] = []
    row_starts = {s.start for s in segs if s.is_table_row and s.start in usable}
    for run in _pipe_table_runs(clean):
        if run["end"] <= CAP:
            continue  # entirely inside the indexed head
        header_line = run["block"].splitlines()[0]
        headers = [c for _, c in _cells(header_line, 0)]
        if not headers or sum(1 for h in headers if _word_tokens(h)) < 2:
            continue
        body: List[Tuple[int, str]] = []
        pos = run["start"]
        for line in clean[run["start"] : run["end"]].splitlines(keepends=True):
            core = line.strip()
            lead = len(line) - len(line.lstrip())
            offset = pos + lead - CAP  # offsets are reported into the TAIL
            pos += len(line)
            if not core or core == header_line.strip():
                continue
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", core):
                continue
            if offset < 0:
                continue  # this row is indexed already
            if offset in row_starts:
                body.append((offset, core))
        if not body:
            continue
        step = max(1, len(body) // _ROWS_PER_TABLE)
        for offset, row in body[::step][:_ROWS_PER_TABLE]:
            cells = _cells(row, offset)
            if len(cells) < 2:
                continue
            subject = ""
            subject_idx = -1
            for i, (_, text) in enumerate(cells):
                if len(_word_tokens(text)) >= 1 and not _VALUE_RE.fullmatch(text):
                    subject, subject_idx = text, i
                    break
            if not subject or len(subject) > _MAX_SUBJECT_CHARS:
                continue
            if not _looks_like_name(subject):
                continue
            for i, (cell_off, cell) in enumerate(cells):
                if i == subject_idx or not cell or cell in {"-", "—", "–"}:
                    continue
                if i >= len(headers):
                    continue
                label = headers[i]
                if not label or not _word_tokens(label) or len(label) > 40:
                    continue
                if len(cell) > _MAX_ANSWER_CHARS:
                    continue
                if not _value_matches(cell) and not _looks_like_name(cell):
                    continue
                out.append(
                    Probe(
                        kind="table_cell",
                        question=(
                            f"According to the table on {_describe(page)}, "
                            f"what is the {label} for {subject}?"
                        ),
                        question_style="direct",
                        answer=cell,
                        tail_offset=cell_off,
                        subject=subject,
                        label=label,
                        context=row,
                        context_tail_offset=offset,
                        why="pipe-table cell keyed by its column header and row subject",
                    )
                )
                break  # one probe per row keeps a wide table from dominating
    return out


def _in_transcript(segs: List[Segment], i: int) -> bool:
    """Is segment `i` sitting inside a pasted shell session?"""
    lo = max(0, i - _TRANSCRIPT_RADIUS)
    hi = min(len(segs), i + _TRANSCRIPT_RADIUS + 1)
    return any(_COMMAND_RE.match(segs[j].text) for j in range(lo, hi))


def _labelled_probes(
    page: "Page", segs: List[Segment], usable: set, unique: set, anchors: "Anchors"
) -> List[Probe]:
    """`Label: value`, and the two-line list shape a `<dl>` extracts to.

    Three things have to hold together or the pair is discarded:

    * the LABEL must read as a field name (`_is_field_label`), not as a clause
      that happens to precede a colon;
    * the VALUE must be a measurement or a short proper name, never a sentence;
    * a SUBJECT must exist — the nearest preceding heading-like line that
      occurs exactly once in the tail. A stream of bare `Pages / 224 / Author
      / …` with nothing to attach it to is the "structured but unanswerable"
      case and is rejected rather than guessed at, because the question
      "what is the Author?" has as many answers as the page has books.

    The repetition gate is deliberately NOT applied to the label line: in a
    real record list the label repeats on every record, which is the evidence
    that it IS a schema rather than prose.
    """
    out: List[Probe] = []
    titles: List[Tuple[int, str]] = []
    for i, seg in enumerate(segs):
        norm = seg.norm
        if seg.is_table_row:
            continue
        # A heading can be short and can be junk-gated for shortness; neither
        # stops it anchoring a question, so headings are tracked separately.
        if (
            norm in unique
            and not _LIST_ITEM_RE.match(norm)
            and not _INLINE_LABEL_RE.match(norm)
            and _is_heading(norm)
        ):
            titles.append((i, norm))

        if seg.start not in usable:
            continue

        pairs: List[Tuple[str, str, int]] = []
        m = _INLINE_LABEL_RE.match(seg.text) if seg.is_line else None
        if m and len(seg.text) <= 110:
            pairs.append(
                (m.group("label").strip(), m.group("value").strip(), seg.start + m.start("value"))
            )
        else:
            item = _LIST_ITEM_RE.match(seg.text)
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            nxt_item = _LIST_ITEM_RE.match(nxt.text) if nxt else None
            if item and nxt_item and nxt.start in usable:
                pairs.append(
                    (
                        item.group(1).strip(),
                        nxt_item.group(1).strip(),
                        nxt.start + nxt_item.start(1),
                    )
                )

        for label, value, off in pairs:
            if not _is_field_label(label) or not _is_value_phrase(value):
                continue
            value = value.rstrip(".,;")
            if not value or value.endswith(('"', "!", "”")):
                continue
            if _in_transcript(segs, i):
                continue
            subject = ""
            for j, cand in reversed(titles):
                if 0 < i - j <= _SUBJECT_LOOKBACK and cand.lower() != label.lower():
                    subject = cand.strip().rstrip(":,;.")
                    break
            if not subject:
                continue
            # Same topicality bar the prose shapes carry: a field belonging to
            # something the page never mentions again is not askable.
            if not (anchors.topical(subject) or anchors.recurring(subject)):
                continue
            out.append(
                Probe(
                    kind="labelled_value",
                    question=(
                        f"According to {_describe(page)}, "
                        f"what is the {label} of {subject}?"
                    ),
                    question_style="direct",
                    answer=value,
                    tail_offset=off,
                    subject=subject,
                    label=label,
                    context=seg.text,
                    context_tail_offset=seg.start,
                    why="label/value pair anchored to the nearest preceding heading",
                )
            )
    return out


class Anchors:
    """What this page is ABOUT, so a probe can be required to be about it too.

    "Would a user plausibly ask this?" has no mechanical answer, but its
    contrapositive nearly does: nobody asks a page about an entity the page
    mentions once in passing. A prose probe therefore has to name something
    that is either in the page's own title/URL or recurs through the document.
    Cheap by construction — one token pass over the page, no substring scans.
    """

    def __init__(self, page: "Page") -> None:
        self.terms = Counter(w.lower() for w in _word_tokens(page.clean))
        topic = f"{page.title} {page.url}"
        self.topic_terms = {
            t for t in _content_terms(re.sub(r"[/_.-]+", " ", topic)) if len(t) >= 4
        }

    def topical(self, subject: str) -> bool:
        return any(t in self.topic_terms for t in _content_terms(subject))

    def recurring(self, subject: str, minimum: int = 3) -> bool:
        terms = [t for t in _content_terms(subject) if len(t) >= 4]
        if not terms:
            return False
        return min(self.terms.get(t, 0) for t in terms) >= minimum


def _statement_probes(
    page: "Page", segs: List[Segment], usable: set, anchors: Anchors
) -> List[Probe]:
    """A measurement or a date inside a sentence that names a page entity.

    Weaker than the structured shapes and marked so: the question is a cloze,
    because turning an arbitrary sentence into an interrogative without a model
    produces confident nonsense. A cloze still grades exactly.

    Only `_STRONG_FLAVOURS` are eligible. A bare integer in prose matched
    footnote markers, examination roll numbers and half-dates, and every one of
    those produced a probe nobody could answer or would ask.
    """
    out: List[Probe] = []
    for seg in segs:
        if seg.start not in usable or seg.is_table_row:
            continue
        text = seg.text
        if len(text) > 500 or len(_word_tokens(text)) < 8:
            continue
        names = [
            n.strip()
            for n in _PROPER_NOUN_RE.findall(text)
            if n.strip().lower() not in _NOT_A_NAME and len(n.strip()) > 2
        ]
        names = [n for n in names if _looks_like_name(n)]
        names = [n for n in names if anchors.topical(n) or anchors.recurring(n)]
        if not names:
            continue
        subject = max(names, key=len)[:_MAX_SUBJECT_CHARS].strip()
        links = _url_spans(text)
        for start, end, flavour, value in _value_matches(text):
            if flavour not in _STRONG_FLAVOURS or len(value) > _MAX_ANSWER_CHARS:
                continue
            if any(lo <= start and end <= hi for lo, hi in links):
                continue
            if text.count(value) != 1:  # a blank with two fillers is not a probe
                continue
            if value in subject:
                continue
            dated = flavour in _DATE_FLAVOURS
            out.append(
                Probe(
                    kind="dated_statement" if dated else "numeric_statement",
                    question=(
                        f"According to {_describe(page)}, complete this statement "
                        f'from the page — "{_cloze(text, start, end)}" — what '
                        f"{'date' if dated else 'value'} belongs in the blank?"
                    ),
                    question_style="cloze",
                    answer=value,
                    tail_offset=seg.start + start,
                    subject=subject,
                    label="date" if dated else "value",
                    context=text,
                    context_tail_offset=seg.start,
                    why=f"{flavour} inside a sentence naming a recurring page entity",
                )
            )
            break  # one value per sentence
    return out


def _attribute_probes(
    page: "Page", segs: List[Segment], usable: set, anchors: Anchors
) -> List[Probe]:
    """`<Proper noun> is <something checkable>` — the weakest shape kept."""
    out: List[Probe] = []
    for seg in segs:
        if seg.start not in usable or seg.is_table_row:
            continue
        if len(seg.text) > 400 or len(_word_tokens(seg.text)) < 8:
            continue
        m = _ATTRIBUTE_RE.search(seg.text)
        if not m:
            continue
        subj, verb, obj = m.group("subj").strip(), m.group("verb"), m.group("obj").strip()
        if subj.lower() in _NOT_A_NAME or not _looks_like_name(subj):
            continue
        if not (anchors.topical(subj) or anchors.recurring(subj)):
            continue
        if len(obj) < 6 or len(obj) > _MAX_ANSWER_CHARS:
            continue
        # A clause cut mid-phrase is not an answer: "told investors they have
        # so far taken a $872 million" ends where the sentence kept going.
        tail_words = _word_tokens(obj)
        if not tail_words or tail_words[-1].lower() in _STOPWORDS:
            continue
        if not obj[-1].isalnum() and obj[-1] not in ")%":
            continue
        # The object must carry something checkable, or this is opinion.
        if not _value_matches(obj) and not any(
            w[:1].isupper() and w.lower() not in _NOT_A_NAME for w in _word_tokens(obj)
        ):
            continue
        out.append(
            Probe(
                kind="entity_attribute",
                question=f"According to {_describe(page)}, what {verb} {subj}?",
                question_style="direct",
                answer=obj,
                tail_offset=seg.start + m.start("obj"),
                subject=subj,
                label=verb,
                context=seg.text,
                context_tail_offset=seg.start,
                why="proper noun with a stated attribute",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@dataclass
class Page:
    page_id: int
    url: str
    title: str
    domain: str
    origin: str
    retrieval_count: int
    text: str

    @property
    def clean(self) -> str:
        # chunk_page strips before it counts, so the cap lands on the stripped
        # text. Measuring against the raw column would be off by the leading
        # whitespace and every offset in the output would be wrong.
        return self.text.strip()

    @property
    def head(self) -> str:
        return self.clean[:CAP]

    @property
    def tail(self) -> str:
        return self.clean[CAP:]


def _domain_of(page: Page) -> str:
    if page.domain:
        return page.domain
    try:
        return urlsplit(page.url).netloc
    except ValueError:
        return ""


def _is_novel(head: str, head_lower: str, probe: Probe) -> bool:
    """False when the INDEXED head already states this fact.

    Not "the answer string appears" — `2024` appears in every long page. The
    test is co-occurrence: the answer within `_NOVELTY_WINDOW` characters of
    its own subject, which is roughly "inside one retrievable chunk together".
    """
    answer = probe.answer.strip()
    if not answer:
        return True
    subject_terms = [t for t in _content_terms(probe.subject) if len(t) >= 4]
    if not subject_terms:
        # Nothing to anchor on: fall back to the strict reading, which
        # under-reports novelty rather than over-reporting it.
        return answer.lower() not in head_lower
    needle = answer.lower()
    pos = head_lower.find(needle)
    while pos >= 0:
        lo = max(0, pos - _NOVELTY_WINDOW)
        window = head_lower[lo : pos + len(needle) + _NOVELTY_WINDOW]
        if any(t in window for t in subject_terms):
            return False
        pos = head_lower.find(needle, pos + 1)
    return True


#: How much each shape is trusted before any evidence about THIS probe. A
#: table cell is checkable by construction; a cloze over a sentence is a guess
#: that a value in that sentence is the interesting one. Calibrated so a prose
#: probe must ALSO be topical, strongly-valued and novel to clear the default
#: 0.50 threshold, while a table cell or a labelled field clears it on shape.
_KIND_BASE = {
    "table_cell": 0.55,
    "labelled_value": 0.50,
    "dated_statement": 0.30,
    "numeric_statement": 0.30,
    "entity_attribute": 0.20,
}


def _score(probe: Probe, page: Page, anchors: Anchors) -> float:
    score = _KIND_BASE[probe.kind]
    if anchors.topical(probe.subject):
        score += 0.15
    elif anchors.recurring(probe.subject, minimum=5):
        score += 0.10
    if any(f in _STRONG_FLAVOURS for _, _, f, _ in _value_matches(probe.answer)):
        score += 0.10
    if probe.novel:
        score += 0.10
    if probe.question_style == "direct":
        score += 0.05
    if len(_content_terms(probe.subject)) >= 2:
        score += 0.05
    return max(0.0, min(1.0, score))


@dataclass
class PageResult:
    page: Page
    verdict: str
    probes: List[Probe] = field(default_factory=list)
    all_probe_count: int = 0
    novel_probe_count: int = 0
    weak_candidate_count: int = 0
    segment_count: int = 0
    kept_segment_count: int = 0
    junk: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)


#: Junk a STRUCTURED shape cannot survive either. A table row or a labelled
#: field is checkable because of its structure, so the softer prose gates
#: (repetition, too few content words) must not apply to it — in a record list
#: the label repeats on every record, which is the evidence it is a schema.
#: Being a binary blob or markup is disqualifying for anything.
_HARD_JUNK = frozenset({"encoded_blob", "markup"})


def analyse_page(
    page: Page,
    *,
    across_page_counts: Counter,
    min_score: float,
    per_page: int,
) -> PageResult:
    tail = page.tail
    segs = segments(tail)
    in_page = Counter(s.norm for s in segs)
    unique = {norm for norm, n in in_page.items() if n == 1}
    anchors = Anchors(page)

    junk: Counter = Counter()
    prose_ok: set = set()  # survives every gate — may host a prose probe
    structural_ok: set = set()  # survives the hard gates — may host a table/field
    for i, seg in enumerate(segs):
        nxt = segs[i + 1].text if i + 1 < len(segs) else ""
        reason = junk_reason(
            seg,
            next_text=nxt,
            in_page_count=in_page[seg.norm],
            across_page_count=across_page_counts[seg.norm],
        )
        if reason:
            junk[reason] += 1
            if reason not in _HARD_JUNK:
                structural_ok.add(seg.start)
        else:
            prose_ok.add(seg.start)
            structural_ok.add(seg.start)

    candidates: List[Probe] = []
    candidates += _table_probes(page, page.clean, segs, structural_ok)
    candidates += _labelled_probes(page, segs, structural_ok, unique, anchors)
    candidates += _statement_probes(page, segs, prose_ok, anchors)
    candidates += _attribute_probes(page, segs, prose_ok, anchors)

    rejected: Counter = Counter()
    head, head_lower = page.head, page.head.lower()
    keyed: Dict[Tuple[str, str], set] = {}
    for c in candidates:
        keyed.setdefault((c.subject.lower(), c.label.lower()), set()).add(c.answer)

    accepted: List[Probe] = []
    weak = 0
    for c in candidates:
        # A context that occurs twice has two answers; the question cannot be
        # graded, so it is not a probe however factual it looks.
        if in_page[_normalise(c.context)] > 1:
            rejected["not_distinctive"] += 1
            continue
        if len(keyed[(c.subject.lower(), c.label.lower())]) > 1:
            rejected["ambiguous_anchor"] += 1
            continue
        if tail[c.tail_offset : c.tail_offset + len(c.answer)] != c.answer:
            rejected["offset_mismatch"] += 1
            continue
        c.novel = _is_novel(head, head_lower, c)
        c.score = _score(c, page, anchors)
        if c.score < min_score:
            rejected["below_score"] += 1
            weak += 1
            continue
        accepted.append(c)

    accepted.sort(key=lambda p: (-p.score, p.tail_offset))
    novel = [p for p in accepted if p.novel]
    if accepted:
        verdict = "yielded"
    elif weak:
        # Candidates existed and none was solid enough to ask. Reported apart
        # from "nothing at all" because the two argue for the cap differently:
        # this one says the tail has prose but no gradeable fact.
        verdict = "weak_only"
    else:
        verdict = "junk_only"

    # Spread the kept probes across kinds so a page is not represented by five
    # cells of one table when it also has prose facts.
    kept: List[Probe] = []
    seen_kind: Counter = Counter()
    for p in accepted:
        if len(kept) >= per_page:
            break
        if seen_kind[p.kind] >= max(1, per_page // 2):
            continue
        seen_kind[p.kind] += 1
        kept.append(p)
    chosen = {id(p) for p in kept}
    for p in accepted:
        if len(kept) >= per_page:
            break
        if id(p) not in chosen:
            kept.append(p)

    return PageResult(
        page=page,
        verdict=verdict,
        probes=kept,
        all_probe_count=len(accepted),
        novel_probe_count=len(novel),
        weak_candidate_count=weak,
        segment_count=len(segs),
        kept_segment_count=len(prose_ok),
        junk=junk,
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Database — one SELECT, read-only by construction
# ---------------------------------------------------------------------------


_SELECT = """
SELECT id, url, coalesce(title, '') AS title, coalesce(domain, '') AS domain,
       coalesce(origin, '') AS origin, retrieval_count, text
  FROM web_pages
 WHERE quarantined_at IS NULL
   AND length(text) > %s
 ORDER BY length(text) DESC
"""

_CORPUS_STATS = """
SELECT count(*) AS pages,
       count(*) FILTER (WHERE length(btrim(text)) >= 200) AS indexable,
       count(*) FILTER (WHERE length(btrim(text)) > %s) AS over_cap,
       count(*) FILTER (WHERE quarantined_at IS NOT NULL) AS quarantined,
       coalesce(sum(length(btrim(text))), 0) AS total_chars,
       coalesce(sum(GREATEST(0, length(btrim(text)) - %s)), 0) AS tail_chars
  FROM web_pages
"""


def resolve_dsn(explicit: str = "") -> Optional[str]:
    """`--dsn`, then TAIL_FACTS_DSN, then APP_DATABASE_URL, then libpq PG*."""
    if explicit:
        return explicit
    for var in ("TAIL_FACTS_DSN", "APP_DATABASE_URL"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    if os.environ.get("PGDATABASE") or os.environ.get("PGHOST"):
        return ""  # libpq reads the environment itself
    return None


def open_read_only(dsn: Optional[str]) -> psycopg.Connection:
    """A connection the server itself will refuse to let us write through.

    Two belts: the session option makes every transaction read-only at the
    server, and `read_only` on the connection makes psycopg say so as well. A
    tool pointed at production earns both.
    """
    if dsn is None:
        raise SystemExit(
            "No database configured. Pass --dsn, or set APP_DATABASE_URL "
            "(inside the container it is already set), or the libpq PG* "
            "variables. Example on the host:\n"
            '  APP_DATABASE_URL="postgresql://techsara:$PW@127.0.0.1:5432/techsara" \\\n'
            "      .venv/bin/python -m tools.tail_facts scan"
        )
    con = psycopg.connect(
        dsn,
        row_factory=dict_row,
        autocommit=False,
        options=(
            "-c default_transaction_read_only=on"
            " -c statement_timeout=180000"
            " -c idle_in_transaction_session_timeout=180000"
            " -c timezone=UTC"
        ),
    )
    con.read_only = True
    return con


def load_pages(con: psycopg.Connection, limit: Optional[int]) -> List[Page]:
    rows = con.execute(_SELECT, (CAP,)).fetchall()
    if limit:
        rows = rows[:limit]
    return [
        Page(
            page_id=int(r["id"]),
            url=r["url"],
            title=r["title"],
            domain=r["domain"],
            origin=r["origin"],
            retrieval_count=int(r["retrieval_count"] or 0),
            text=r["text"] or "",
        )
        for r in rows
    ]


def corpus_stats(con: psycopg.Connection) -> Dict[str, int]:
    row = con.execute(_CORPUS_STATS, (CAP, CAP)).fetchone()
    return {k: int(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _pct(part: float, whole: float) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def build_report(
    results: List[PageResult], stats: Dict[str, int], min_score: float
) -> Dict[str, Any]:
    yielded = [r for r in results if r.verdict == "yielded"]
    weak_only = [r for r in results if r.verdict == "weak_only"]
    junk_only = [r for r in results if r.verdict == "junk_only"]
    no_probe = weak_only + junk_only
    novel_pages = [r for r in results if r.novel_probe_count > 0]
    tail_all = sum(len(r.page.tail) for r in results)
    tail_yielded = sum(len(r.page.tail) for r in yielded)
    tail_novel = sum(len(r.page.tail) for r in novel_pages)

    junk: Counter = Counter()
    rejected: Counter = Counter()
    kinds: Counter = Counter()
    for r in results:
        junk.update(r.junk)
        rejected.update(r.rejected)
        for p in r.probes:
            kinds[p.kind] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cap": {
            "indexed_chars_per_page": CAP,
            "max_chunks_per_page": web_index._MAX_CHUNKS_PER_PAGE,
            "chunk_chars": web_index._CHUNK_CHARS,
            "overlap_chars": web_index._OVERLAP_CHARS,
            "chunker_version": web_index.CHUNKER_VERSION,
        },
        "corpus": stats,
        "settings": {
            "min_score": min_score,
            "probes_per_page": _PROBES_PER_PAGE,
            "rows_per_table": _ROWS_PER_TABLE,
            "novelty_window_chars": _NOVELTY_WINDOW,
        },
        "headline": {
            "pages_over_cap_scanned": len(results),
            "pages_yielding_a_probe": len(yielded),
            "pages_yielding_only_junk": len(no_probe),
            "pages_yielding_only_junk_breakdown": {
                # candidates existed, none was gradeable
                "weak_only": len(weak_only),
                # nothing in the tail even reached candidate stage
                "junk_only": len(junk_only),
            },
            "pages_yielding_a_novel_probe": len(novel_pages),
            "yield_ratio": round(len(yielded) / len(results), 3) if results else 0.0,
            "novel_yield_ratio": (
                round(len(novel_pages) / len(results), 3) if results else 0.0
            ),
            "tail_chars_total": tail_all,
            "tail_chars_on_yielding_pages": tail_yielded,
            "tail_chars_on_novel_yielding_pages": tail_novel,
            "tail_chars_on_junk_only_pages": tail_all - tail_yielded,
        },
        "junk_reasons": dict(junk.most_common()),
        "candidate_rejections": dict(rejected.most_common()),
        "probe_kinds": dict(kinds.most_common()),
    }


def print_report(
    report: Dict[str, Any],
    results: List[PageResult],
    sample: int,
    random_sample: int = 0,
    seed: int = 0,
) -> None:
    h = report["headline"]
    c = report["corpus"]
    out = print

    out("")
    out("tail_facts — is the text beyond the chunk cap worth indexing?")
    out("=" * 78)
    out(
        f"cap    INDEXED_CHARS_PER_PAGE = {CAP:,}"
        f"  ({report['cap']['max_chunks_per_page']} chunks x"
        f" {report['cap']['chunk_chars']:,} / {report['cap']['overlap_chars']:,} overlap)"
    )
    out(
        f"corpus {c['pages']:,} stored pages, {c['indexable']:,} indexable,"
        f" {c['over_cap']:,} over the cap"
    )
    out(
        f"tail   {c['tail_chars']:,} chars beyond the cap"
        f"  ({_pct(c['tail_chars'], c['total_chars'])} of {c['total_chars']:,} stored)"
    )
    out("")
    out("THE ANSWER")
    out("-" * 78)
    n = h["pages_over_cap_scanned"]
    out(
        f"  pages that yielded a plausible probe : {h['pages_yielding_a_probe']:3d} of {n}"
        f"   ({_pct(h['pages_yielding_a_probe'], n)})"
    )
    b = h["pages_yielding_only_junk_breakdown"]
    out(
        f"  pages that yielded only junk         : {h['pages_yielding_only_junk']:3d} of {n}"
        f"   ({_pct(h['pages_yielding_only_junk'], n)})"
    )
    out(
        f"      of which nothing gradeable at all: {b['junk_only']:3d}"
        f"   |  candidates too weak to ask: {b['weak_only']:3d}"
    )
    out(
        f"  pages with a probe the HEAD lacks    : {h['pages_yielding_a_novel_probe']:3d} of {n}"
        f"   ({_pct(h['pages_yielding_a_novel_probe'], n)})   <- the marginal case"
    )
    out("")
    out(
        f"  tail characters on yielding pages    : {h['tail_chars_on_yielding_pages']:,}"
        f" ({_pct(h['tail_chars_on_yielding_pages'], h['tail_chars_total'])} of the tail)"
    )
    out(
        f"  tail characters that yielded nothing : {h['tail_chars_on_junk_only_pages']:,}"
        f" ({_pct(h['tail_chars_on_junk_only_pages'], h['tail_chars_total'])} of the tail)"
    )
    if report.get("graded"):
        g = report["graded"]
        out("")
        out("GRADED BY HAND — the mechanical ratio above is an UPPER BOUND")
        out("-" * 78)
        out(
            f"  probes graded {g['probes_graded']} of "
            f"{g['probes_graded'] + g['probes_ungraded']}"
            f"   askable {g['probes_askable']}"
            f"   probe precision {g['probe_precision']}"
        )
        out(
            f"  pages with an askable probe          : "
            f"{g['pages_with_an_askable_probe']:3d}"
            f"   (ratio {g['graded_yield_ratio']})"
        )
        out(f"  pages with none                      : {g['pages_with_none']:3d}")
        out(f"  pages not fully graded               : {g['pages_not_fully_graded']:3d}")
        out(
            f"  tail chars on pages with an askable probe: "
            f"{g['tail_chars_on_askable_pages']:,}"
            f" ({100 * g['tail_chars_share_on_askable_pages']:.1f}% of the tail)"
        )

    out("")
    out("WHY SEGMENTS WERE DISCARDED (tail segments, all pages)")
    out("-" * 78)
    total_junk = sum(report["junk_reasons"].values()) or 1
    for reason, count in report["junk_reasons"].items():
        out(f"  {reason:<24} {count:>9,}  {_pct(count, total_junk)}")
    if report["candidate_rejections"]:
        out("")
        out("CANDIDATES BUILT THEN REJECTED")
        out("-" * 78)
        for reason, count in report["candidate_rejections"].items():
            out(f"  {reason:<24} {count:>9,}")
    out("")
    out("PROBE KINDS KEPT")
    out("-" * 78)
    for kind, count in report["probe_kinds"].items():
        out(f"  {kind:<24} {count:>9,}")
    out("")
    out("PER PAGE")
    out("-" * 78)
    out(
        f"{'id':>5} {'tail':>9} {'seg':>7} {'kept':>6} {'probe':>6} {'novel':>6}"
        f" {'ret':>4}  {'verdict':<10} {'top junk reason':<22} url"
    )
    for r in sorted(results, key=lambda x: -len(x.page.tail)):
        top = r.junk.most_common(1)
        out(
            f"{r.page.page_id:5d} {len(r.page.tail):9,} {r.segment_count:7,}"
            f" {r.kept_segment_count:6,} {r.all_probe_count:6,} {r.novel_probe_count:6,}"
            f" {r.page.retrieval_count:4d}  {r.verdict:<10}"
            f" {(top[0][0] if top else '-'):<22} {r.page.url[:60]}"
        )

    pool: List[Tuple[PageResult, Probe]] = [(r, p) for r in results for p in r.probes]

    def _dump(title: str, chosen: List[Tuple[PageResult, Probe]]) -> None:
        out("")
        out(title)
        out("-" * 78)
        for r, p in chosen:
            out("")
            out(f"  [{p.kind} score={p.score:.2f} novel={p.novel}] page {r.page.page_id}")
            out(f"  Q: {p.question}")
            out(
                f"  A: {p.answer!r}   @tail+{p.tail_offset:,}"
                f" (page char {CAP + p.tail_offset:,})"
            )
            ctx = re.sub(r"\s+", " ", p.context)[:180]
            out(f"  ctx: {ctx}")

    if sample:
        _dump(f"SAMPLE PROBES (highest scoring, {sample})", sorted(pool, key=lambda rp: -rp[1].score)[:sample])
    if random_sample:
        # A RANDOM sample, not the best ones. Precision cannot be estimated
        # from the top of a ranking, and the seed makes the grading repeatable
        # by whoever disagrees with it.
        rng = random.Random(seed)
        chosen = rng.sample(pool, min(random_sample, len(pool)))
        _dump(
            f"RANDOM SAMPLE FOR GRADING (n={len(chosen)}, seed={seed}) — "
            "mark each askable / not, and divide",
            chosen,
        )
    out("")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_grades(path: str) -> Dict[str, Optional[bool]]:
    """Read a human's verdicts on individual probes.

    Two shapes accepted: a flat ``{"p2465-1": true}`` map, and the richer one
    ``--grade-template`` writes, ``{"p2465-1": {"question": …, "askable": null}}``
    — the same file a reviewer fills in. ``null`` means "not graded" and is
    kept apart from ``false``: ungraded is not the same as rejected, and
    conflating them is how a partial review turns into a fabricated ratio.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: Dict[str, Optional[bool]] = {}
    for probe_id, value in raw.items():
        if isinstance(value, dict):
            value = value.get("askable")
        out[probe_id] = None if value is None else bool(value)
    return out


def apply_grades(
    report: Dict[str, Any], grades: Dict[str, Optional[bool]]
) -> Dict[str, Any]:
    """Recompute the headline from a human's verdicts.

    THE POINT OF THE WHOLE TOOL. The mechanical ratio is an UPPER BOUND: a
    regex cannot tell a fact from a sentence shaped like one, so every probe it
    emits is a candidate. This turns a graded sample into the real number, and
    reports how much of the set the grading actually covers so nobody quotes a
    ratio derived from four opinions.
    """
    by_page: Dict[int, List[Dict[str, Any]]] = {}
    for probe in report["probes"]:
        by_page.setdefault(probe["page_id"], []).append(probe)

    graded = sum(1 for v in grades.values() if v is not None)
    askable = sum(1 for v in grades.values() if v)
    pages_yes, pages_no, pages_ungraded = [], [], []
    for page in report["pages"]:
        probes = by_page.get(page["page_id"], [])
        verdicts = [grades.get(p["probe_id"]) for p in probes]
        if any(v is True for v in verdicts):
            pages_yes.append(page)
        elif probes and all(v is False for v in verdicts):
            pages_no.append(page)
        elif not probes:
            pages_no.append(page)  # the tool found nothing; nothing to grade
        else:
            pages_ungraded.append(page)

    tail_total = report["headline"]["tail_chars_total"] or 1
    return {
        "probes_graded": graded,
        "probes_ungraded": len(report["probes"]) - graded,
        "probes_askable": askable,
        "probe_precision": round(askable / graded, 3) if graded else None,
        "pages_with_an_askable_probe": len(pages_yes),
        "pages_with_none": len(pages_no),
        "pages_not_fully_graded": len(pages_ungraded),
        "graded_yield_ratio": (
            round(len(pages_yes) / (len(pages_yes) + len(pages_no)), 3)
            if (pages_yes or pages_no)
            else None
        ),
        "tail_chars_on_askable_pages": sum(p["tail_chars"] for p in pages_yes),
        "tail_chars_share_on_askable_pages": round(
            sum(p["tail_chars"] for p in pages_yes) / tail_total, 3
        ),
        "pages_with_none_ids": [p["page_id"] for p in pages_no],
    }


def cmd_scan(args: argparse.Namespace) -> int:
    con = open_read_only(resolve_dsn(args.dsn))
    try:
        stats = corpus_stats(con)
        pages = load_pages(con, args.limit)
    finally:
        con.close()

    if not pages:
        print("No page exceeds the cap — nothing to measure.")
        return 0

    # Pass 1: segment every tail once, so cross-page furniture can be counted
    # before any page is judged. A blurb that appears on five sites is chrome,
    # and a per-page counter cannot see it.
    per_page_segments: Dict[int, List[Segment]] = {}
    across: Counter = Counter()
    for page in pages:
        segs = segments(page.tail)
        per_page_segments[page.page_id] = segs
        for norm in {s.norm for s in segs}:
            across[norm] += 1

    results = [
        analyse_page(
            page,
            across_page_counts=across,
            min_score=args.min_score,
            per_page=args.per_page,
        )
        for page in pages
    ]

    # Self-check: every emitted offset must address its own answer, in the tail
    # AND in the stripped page. A probe that cannot be located is not evidence.
    bad = 0
    for r in results:
        clean = r.page.clean
        tail = r.page.tail
        for p in r.probes:
            if tail[p.tail_offset : p.tail_offset + len(p.answer)] != p.answer:
                bad += 1
            if clean[CAP + p.tail_offset : CAP + p.tail_offset + len(p.answer)] != p.answer:
                bad += 1
            if p.tail_offset < 0:
                bad += 1
    if bad:
        print(f"SELF-CHECK FAILED: {bad} probe offset(s) do not address their answer")
        return 2

    report = build_report(results, stats, args.min_score)
    probes: List[Dict[str, Any]] = []
    for r in results:
        for i, p in enumerate(r.probes, 1):
            probes.append(p.as_json(r.page, f"p{r.page.page_id}-{i}"))
    report["pages"] = [
        {
            "page_id": r.page.page_id,
            "url": r.page.url,
            "title": r.page.title,
            "domain": _domain_of(r.page),
            "origin": r.page.origin,
            "retrieval_count": r.page.retrieval_count,
            "text_chars": len(r.page.clean),
            "tail_chars": len(r.page.tail),
            "verdict": r.verdict,
            "segments": r.segment_count,
            "segments_kept": r.kept_segment_count,
            "probes_accepted": r.all_probe_count,
            "probes_novel": r.novel_probe_count,
            "probes_emitted": len(r.probes),
            "junk_reasons": dict(r.junk.most_common()),
            "candidate_rejections": dict(r.rejected.most_common()),
        }
        for r in sorted(results, key=lambda x: -len(x.page.tail))
    ]
    report["probes"] = probes
    report["self_check"] = {"probe_offsets_verified": len(probes), "failures": 0}

    if args.grade_template:
        template = {
            probe["probe_id"]: {
                "page_id": probe["page_id"],
                "url": probe["url"],
                "kind": probe["kind"],
                "question": probe["question"],
                "answer": probe["answer"],
                "askable": None,
            }
            for probe in probes
        }
        with open(args.grade_template, "w", encoding="utf-8") as fh:
            json.dump(template, fh, ensure_ascii=False, indent=2)
        print(
            f"grading template written: {args.grade_template} "
            f"({len(template)} probes, all askable=null)"
        )

    if args.grade_file:
        report["graded"] = apply_grades(report, load_grades(args.grade_file))

    print_report(report, results, args.sample, args.sample_random, args.seed)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"eval set written: {args.out}  ({len(probes)} probes)")
    else:
        print("(no --out given; JSON not written)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print a bounded window of one tail, for grading a probe by hand."""
    con = open_read_only(resolve_dsn(args.dsn))
    try:
        row = con.execute(
            "SELECT id, url, coalesce(title,'') AS title, text FROM web_pages "
            "WHERE id = %s",
            (args.page_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        print(f"no page {args.page_id}")
        return 1
    clean = (row["text"] or "").strip()
    if len(clean) <= CAP:
        print(f"page {args.page_id} is {len(clean):,} chars — it fits under the cap")
        return 0
    tail = clean[CAP:]
    start = max(0, min(args.offset, len(tail)))
    window = tail[start : start + args.chars]
    print(f"page {row['id']}  {row['url']}")
    print(f"title: {row['title']}")
    print(f"stored {len(clean):,} chars; tail {len(tail):,} chars; showing "
          f"tail[{start:,}:{start + len(window):,}]")
    print("-" * 78)
    print(window)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.tail_facts",
        description=(
            "Measure whether the page text beyond web_index.INDEXED_CHARS_PER_PAGE "
            "holds anything a user would ask about. Read-only; no model calls."
        ),
    )
    parser.add_argument("--dsn", default="", help="PostgreSQL DSN (default: APP_DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="build the eval set and report the ratio")
    scan.add_argument("--out", default="", help="write the JSON eval set here")
    scan.add_argument("--limit", type=int, default=0, help="only the N largest pages")
    scan.add_argument("--per-page", type=int, default=_PROBES_PER_PAGE)
    scan.add_argument("--min-score", type=float, default=_DEFAULT_MIN_SCORE)
    scan.add_argument("--sample", type=int, default=8, help="top probes to print (0 = none)")
    scan.add_argument(
        "--sample-random",
        type=int,
        default=0,
        help="print N probes drawn at random, for estimating precision by hand",
    )
    scan.add_argument("--seed", type=int, default=20260907, help="seed for --sample-random")
    scan.add_argument(
        "--grade-template",
        default="",
        help="write a JSON skeleton for a human to mark each probe askable or not",
    )
    scan.add_argument(
        "--grade-file",
        default="",
        help="read those verdicts back and report the GRADED yield beside the mechanical one",
    )
    scan.set_defaults(func=cmd_scan)

    show = sub.add_parser("show", help="print a window of one page's tail")
    show.add_argument("--page-id", type=int, required=True)
    show.add_argument("--offset", type=int, default=0, help="offset into the tail")
    show.add_argument("--chars", type=int, default=2000)
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    if args.command == "scan" and args.limit == 0:
        args.limit = None
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
