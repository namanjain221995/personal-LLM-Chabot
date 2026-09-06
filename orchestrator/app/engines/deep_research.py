"""Deep Research: iterative, source-grounded investigation (2026-08-30; rebuilt 2026-09-03).

Web Search answers a question in one pass — search, read a handful of pages,
answer. Deep Research is the other shape: PLAN the question into
subquestions, search each of them, OPEN the pages, EXTRACT dated claims,
FOLLOW the links those pages give for their claims, FIND the primary source,
judge what is still MISSING, search again for exactly that, CROSS-CHECK the
important claims, and only then write a long report.

WHAT THE FIRST VERSION GOT WRONG, and what this one does about it. A live
test on a time-sensitive topic exposed four weaknesses, none of them about
that topic in particular:

  STOPPED TOO EARLY. The auditor judged "sufficient" from 400-character
    snippets of the evidence, so it stopped after one or two rounds, and a
    fixed cap of three rounds bounded the rest. Now the auditor reads real
    excerpts AND a resolution table of what each subquestion currently has,
    the loop stops on EVIDENCE — sufficiency, information gain, duplicate
    rate, budget, unanswered subquestions — and every stop is logged with
    its reason. "Not found yet" and "unknown" are different states.

  NO NOTION OF TIME. Nothing in the loop knew today's date; pages carried no
    publication date; an old article and a current official page ranked on
    topicality alone. Now every prompt carries the current date and the
    question's freshness level; every source carries its published/updated
    date and read time (core/provenance); claims are extracted WITH the date
    they held; and a code-level resolution (not a prompt) labels each
    subquestion's evidence CURRENT / HISTORICAL / SUPERSEDED / CONFLICTING /
    UNKNOWN — a newer authoritative value SUPERSEDES an older one (a change
    over time), comparable-date disagreement is a CONFLICT (surfaced, not
    resolved by fiat).

  SNIPPETS ONLY, NO PRIMARY SOURCES. Research read only what the search
    engine returned. Now the links inside the pages read are scored — the
    citation an article gives, the official page a summary points at, the
    PDF behind a story — and the best few are opened each round, so
    first-hand sources get found rather than hoped for.

  TEN COPIES = TEN CONFIRMATIONS. Syndicated copies of one report counted
    as independent corroboration. Now near-duplicates (word-shingle
    fingerprints) are detected at registration; a copy keeps its citation
    number but corroborates nothing, and ranking counts independent
    domains.

And a SELF-CORRECTION pass before the report: each subquestion's evidence
is audited — enough? primary opened? newer source likely? disagreement? a
change over time mistaken for a contradiction? — and a low-confidence claim
earns one more targeted round instead of a confident sentence.

WHAT IT REUSES. Everything expensive already exists in `engines/search.py`
and is called directly rather than reimplemented: `_collect_results` (the
round-robin merge over parallel SearXNG queries, per-domain capped),
`_rerank_results` (the Qwen3-Reranker cross-encoder), `_fetch_sources`
(SSRF-guarded fetch + readable extraction + the PostgreSQL warm-page store),
and `_persist_and_index` (write-behind logging and embedding). The pages
it reads land in the shared corpus; the claims it resolves land in
`web_claims`, dated, where the Fast-mode knowledge layer can find them for
the next user's question.

COST. Every model call is the LOCAL vLLM/Qwen deployment and every search is
the LOCAL SearXNG: no paid API is required or contacted on this path.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from .. import db, llm
from ..config import settings
# `extract` is deliberately NOT imported any more: every head slice this
# module used to take (`extract.truncate_chars`) is now the search path's
# query-centred `_select_text` — see `_trim_evidence` for what the head slice
# was costing (C1).
from ..core import provenance
from ..core.sf_intel.planner import extract_json_object
from ..freshness import Freshness, Verdict, classify_offline
from ..memory_recall import keywords
from ..search.base import SearchResult, SearchUnavailableError
from ..web_memory import authority_of
from . import recent_turns
from .search import (
    _TIER_A_SOURCES,
    _TIER_B_CHARS,
    _collect_results,
    _fetch_sources,
    _normalize_url,
    _rerank_results,
    _select_text,
    _spawn,
    _persist_and_index,
)

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: Research reasoning runs on the main model, but NEVER at an unbounded
#: concurrency: interactive chat shares this vLLM. Two in flight keeps a
#: research run moving without pushing a chat turn behind a queue (the engine
#: is memory-bandwidth bound, so the third concurrent generation costs every
#: other stream latency rather than adding throughput).
#:
#: PROCESS-WIDE, and shared by every concurrent run (R13): two runs do not get
#: two slots each, they share these two. That is the whole reason a second run
#: is now allowed at all — it interleaves inside a budget interactive chat
#: already tolerates, instead of adding a third generation stream. It costs the
#: first run latency, never the chat path.
_LLM_SEM = asyncio.Semaphore(2)

#: `_RUN_LOCK` used to live here: one research run at a time per process, and
#: a second request was REFUSED outright. That made one person's ten-minute run
#: fail every other user's Deep Research request org-wide (R13). It is replaced
#: by `_Admission` further down — a per-user allowance under a process-wide
#: ceiling, with a bounded queue and a refusal that says when to come back.

#: The share of `deep_research_timeout_s` kept back for the report.
#:
#: The budget exists to bound GATHERING, and a run that spends all of it must
#: still be able to say what it found — so the loop stops this much early and
#: the writing gets the rest. A run that stops on its own (sufficient, source
#: cap) keeps the WHOLE remainder, so nothing changes for a healthy run; only
#: a run that would have overrun is squeezed.
_REPORT_RESERVE_FRACTION = 0.25
#: …but never more than this. A longer budget buys more gathering, not a
#: proportionally longer report.
_REPORT_RESERVE_MAX_S = 240.0
#: The floor under the report's allowance whatever the configuration, so a run
#: that is already over budget writes a short honest partial rather than an
#: empty document.
_REPORT_FLOOR_S = 15.0


def _report_reserve_s(total: float) -> float:
    return min(_REPORT_RESERVE_MAX_S, max(0.0, total) * _REPORT_RESERVE_FRACTION)


#: A citation the model wrote: [1], [12].
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
#: The same, with the single space that usually precedes it. Removing an
#: invalid marker takes that space with it, so the sentence closes up cleanly
#: without a document-wide whitespace pass (which flattened YAML indentation
#: and nested bullets when it was tried).
_CITE_WITH_SPACE_RE = re.compile(r"[ \t]?\[(\d{1,3})\]")

#: Category routing. Measured on this host 2026-08-30: a default general query
#: reaches only google cse / bing / mwmbl / yahoo — and google cse, the one
#: high-volume engine, IP-blocked this host during testing. `categories=science`
#: (arxiv, pubmed) answers with FULL ABSTRACTS — 1100-1900 character snippets
#: against ~140 for bing — from a pool nothing else here queries, so it neither
#: competes for the general engines' quota nor arrives thin.
#:
#: `categories=it` is deliberately NOT routed to, despite being the healthiest
#: pool by result count. Its engines are github/stackoverflow/mdn/DOCKER HUB,
#: and on the first live research run 9 of 23 sources came back as Docker Hub
#: image pages ("vllm/vllm-openai - Docker Image", "redislabs/memtier_benchmark")
#: plus MDN's `eval()` page — matched on the words "vllm", "benchmark", "memory"
#: and "eval" in the queries. A registry listing is not evidence. The pool stays
#: reachable through the provider for callers that want it; research does not.
_SCIENCE_RE = re.compile(
    r"\b(paper|papers|study|studies|research|arxiv|preprint|publication|"
    r"evaluation|benchmark results|architecture|algorithm|survey|"
    r"state of the art|sota)\b",
    re.I,
)


def route_category(query: str) -> str:
    """The SearXNG category pool most likely to answer this query well.

    Empty means the default general pool, which is the right answer for most
    queries — routing is an upgrade for the minority that clearly want the
    academic index, not a classifier the loop depends on.
    """
    if _SCIENCE_RE.search(query or ""):
        return "science"
    return ""


#: Time-sensitive wording, so the report knows to prefer recent evidence and
#: to say when a stored copy may be stale. Reuses the search engine's shape.
_RECENCY_RE = re.compile(
    r"\b(latest|current|today|this week|this month|this year|recent|now|"
    r"20\d\d|newest|up to date|state of the art)\b",
    re.I,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

#: Link shapes that are never evidence: session/account pages, sharing
#: widgets, tag/category listings, pagination, tracking parameters.
_SKIP_LINK_RE = re.compile(
    r"(/login|/signin|/sign-in|/signup|/register|/privacy|/terms|/cookie|"
    r"/subscribe|/share|sharer\.php|intent/tweet|^mailto:|^javascript:|"
    r"/tag/|/tags/|/category/|/categories/|/author/|/page/\d+|"
    r"[?&](utm_|replytocom=|share=|print=)|/feed/?$|\.rss$|/rss/?$)",
    re.I,
)
_SKIP_EXT_RE = re.compile(
    r"\.(zip|tar|gz|tgz|whl|exe|dmg|iso|png|jpe?g|gif|svg|webp|ico|mp[34]|webm|"
    r"woff2?|ttf|css|js|map)$",
    re.I,
)

# Evidence status vocabulary — the resolution layer's output.
STATUS_CURRENT = "current"
STATUS_HISTORICAL = "historical"
STATUS_SUPERSEDED = "superseded"
STATUS_CONFLICTING = "conflicting"
STATUS_UNKNOWN = "unknown"

#: Sources whose evidence dates sit this far apart are a change over time,
#: not two opinions. Same constant the living-knowledge layer uses.
_SUPERSEDE_GAP_DAYS = 45
#: Authority points separating "comparable" sources from "much weaker".
_AUTHORITY_GAP = 30

#: A resolved value that NO supporting source states in its own words is not
#: something this run can stand behind. `_quote_in` — the same matcher the
#: shared claim store uses — decides that in `_resolve` now, BEFORE the report
#: prompt is built: until 2026-09-06 the check ran only in `_persist_claims`,
#: after the report was written, so a value no page states was dropped from
#: the shared store while still reaching the report with a citation and a
#: confidence number.
#:
#: A ceiling AND a penalty, because both properties are needed: the ceiling
#: keeps an unstated value out of "confident" territory whatever else it has
#: going for it, and the penalty preserves the ORDER within the unstated ones
#: (two independent sources still beat one), which a bare ceiling flattens.
_UNSTATED_CONFIDENCE_CAP = 0.45
_UNSTATED_CONFIDENCE_PENALTY = 0.25


# ---------------------------------------------------------------------------
# Structured-output schemas. The local vLLM supports response_format
# json_schema (verified live), so these CANNOT come back malformed; when a
# runtime without guided decoding is used, llm.json_completion downgrades to
# an unconstrained retry and the validation below still holds.
# ---------------------------------------------------------------------------

def _plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "subquestions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 8,
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
        },
        "required": ["subquestions", "queries"],
        "additionalProperties": False,
    }


def _gap_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "sufficient": {"type": "boolean"},
            "missing": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "contradictions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "followup_queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
            "primary_source_queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "entities_to_expand": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
        },
        "required": ["sufficient", "missing", "followup_queries"],
        "additionalProperties": False,
    }


def _claims_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": 40,
                "items": {
                    "type": "object",
                    "properties": {
                        "subquestion": {"type": "integer"},
                        "claim": {"type": "string"},
                        "value": {"type": "string"},
                        "source": {"type": "integer"},
                        "as_of": {"type": "string"},
                        "status": {"type": "string", "enum": ["current", "historical", "unclear"]},
                    },
                    "required": ["subquestion", "claim", "source", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    }


def _verify_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "subquestion": {"type": "integer"},
                        "enough_evidence": {"type": "boolean"},
                        "primary_source_opened": {"type": "boolean"},
                        "newer_source_likely": {"type": "boolean"},
                        "sources_disagree": {"type": "boolean"},
                        "changed_over_time": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "verification_queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 2,
                        },
                    },
                    "required": ["subquestion", "enough_evidence", "confidence"],
                    "additionalProperties": False,
                },
            },
            "overall_confidence": {"type": "number"},
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Research state
# ---------------------------------------------------------------------------


@dataclass
class SourceRecord:
    """One real, fetched page. The registry entry a citation resolves to."""

    n: int
    title: str
    url: str
    text: str
    query: str
    iteration: int
    # --- provenance (2026-09-03) ---
    authority: int = 0
    source_type: str = ""
    published_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    content_hash: str = ""
    #: "search" | "link:<n>" — how this page was found.
    discovered_via: str = "search"
    links: List[str] = field(default_factory=list)
    fingerprint: FrozenSet[int] = field(default_factory=frozenset)
    #: Set when this page is a near-duplicate of an earlier source: it keeps
    #: its citation number but corroborates nothing on its own.
    dup_of: Optional[int] = None
    primary: bool = False
    #: How much of the FIRST-HAND bonus this source has earned, in [0, 1]
    #: (R12). `primary` says the page is first-hand; this says whether it is
    #: also disinterested. A vendor's own page about the vendor is still a
    #: primary source — it is just not the most trustworthy possible source
    #: for a claim about itself, which is what the full bonus asserts. Kept
    #: beside `primary` rather than replacing it: rank, link score, citation
    #: and the "primary source opened" line all still key on the boolean.
    primary_weight: float = 0.0
    from_store: bool = False

    @property
    def domain(self) -> str:
        return urlparse(self.url).hostname or self.url

    @property
    def domain_key(self) -> str:
        return provenance.domain_of(self.url)

    @property
    def effective_time(self) -> Optional[datetime]:
        return provenance.effective_time(self.published_at, self.modified_at, self.fetched_at)

    @property
    def canonical_n(self) -> int:
        return self.dup_of or self.n

    def label(self) -> str:
        """'official · published 2026-03-12 · read 2026-09-02 · primary'."""
        bits: List[str] = []
        if self.source_type and self.source_type != "unknown":
            bits.append(self.source_type)
        if self.published_at:
            bits.append(f"published {self.published_at.date().isoformat()}")
        elif self.modified_at:
            bits.append(f"updated {self.modified_at.date().isoformat()}")
        else:
            bits.append("undated")
        if self.fetched_at:
            bits.append(f"read {self.fetched_at.date().isoformat()}")
        if self.primary:
            bits.append("primary source")
        if self.dup_of:
            bits.append(f"same text as [{self.dup_of}]")
        if self.discovered_via.startswith("link:"):
            bits.append(f"found via a link in [{self.discovered_via[5:]}]")
        return " · ".join(bits)


@dataclass
class Claim:
    """One dated statement a source makes about one subquestion."""

    subq: int
    text: str
    value: str
    source_n: int
    as_of: Optional[date]
    hint: str  # current | historical | unclear
    iteration: int


@dataclass
class Resolution:
    """What the evidence for one subquestion adds up to, and how sure."""

    subq: int
    question: str
    status: str
    value: str = ""
    as_of: Optional[date] = None
    #: Canonical source numbers supporting the resolved value.
    support: List[int] = field(default_factory=list)
    independent: int = 0
    primary: bool = False
    #: [{value, as_of, sources}] — older values the current one replaced.
    superseded: List[dict] = field(default_factory=list)
    #: [{value, as_of, sources}] — comparable-date disagreements. An entry
    #: with "weak": True is a disagreement from a much weaker, undated source:
    #: still an open disagreement, never a change over time.
    conflicts: List[dict] = field(default_factory=list)
    confidence: float = 0.0
    #: False when no supporting source states `value` in its own words
    #: (`_quote_in`). The report must not present it as an established fact.
    stated_verbatim: bool = True

    def line(self) -> str:
        head = f"[{self.subq}] {self.question} — {self.status.upper()}"
        if self.status == STATUS_UNKNOWN:
            return head + ": no source read so far states this"
        when = f"as of {self.as_of.isoformat()}" if self.as_of else "date not stated"
        cites = "".join(f"[{n}]" for n in self.support)
        tail = (
            f': "{self.value}" ({when}; sources {cites}; {self.independent} independent'
            f"{'; primary source opened' if self.primary else ''})"
        )
        for s in self.superseded[:3]:
            tail += (
                f' · superseded: "{s["value"]}" ('
                f"{('as of ' + s['as_of']) if s.get('as_of') else 'date not stated'}; "
                + "".join(f"[{n}]" for n in s.get("sources", []))
                + ")"
            )
        for c in self.conflicts[:3]:
            kind = "disputed by a weaker source" if c.get("weak") else "conflicting"
            tail += (
                f' · {kind}: "{c["value"]}" ('
                f"{('as of ' + c['as_of']) if c.get('as_of') else 'date not stated'}; "
                + "".join(f"[{n}]" for n in c.get("sources", []))
                + ")"
            )
        if not self.stated_verbatim:
            tail += " · NOT STATED VERBATIM by any cited source"
        return head + tail + f" · confidence {self.confidence:.2f}"

    def as_meta(self) -> dict:
        return {
            "subquestion": self.question,
            "status": self.status,
            "value": self.value,
            "as_of": self.as_of.isoformat() if self.as_of else "",
            "support": list(self.support),
            "independent": self.independent,
            "primary": self.primary,
            "superseded": list(self.superseded),
            "conflicts": list(self.conflicts),
            "confidence": round(self.confidence, 2),
            "stated_verbatim": self.stated_verbatim,
        }


@dataclass
class RoundStats:
    iteration: int
    label: str
    queries: List[str]
    attempted: int = 0
    fetched: int = 0
    new_sources: int = 0
    duplicates: int = 0
    links_followed: int = 0
    new_claims: int = 0
    #: Query groups this round lost to a search-provider outage. A round that
    #: searched nothing is not a round that found nothing.
    search_outages: int = 0
    elapsed_s: float = 0.0

    @property
    def gain(self) -> float:
        """Share of what this round tried that turned into NEW evidence."""
        if self.attempted <= 0:
            return 0.0
        return min(1.0, (self.new_sources + 0.5 * self.new_claims) / float(self.attempted))

    @property
    def duplicate_rate(self) -> float:
        return self.duplicates / float(self.fetched) if self.fetched else 0.0

    def as_meta(self) -> dict:
        return {
            "iteration": self.iteration,
            "label": self.label,
            "queries": list(self.queries),
            "attempted": self.attempted,
            "fetched": self.fetched,
            "new_sources": self.new_sources,
            "duplicates": self.duplicates,
            "links_followed": self.links_followed,
            "new_claims": self.new_claims,
            "search_outages": self.search_outages,
            "gain": round(self.gain, 2),
            "elapsed_s": round(self.elapsed_s, 1),
        }


@dataclass
class ResearchState:
    research_id: str
    conversation_id: str
    question: str
    #: Who asked. Without it the search log this run writes is stamped
    #: user_id=NULL / conversation_id='' and delete_conversation can never
    #: match it — the question text would outlive the conversation forever.
    #: search.py had exactly this bug and it was fixed on 2026-08-30; passing
    #: four positional args to _persist_and_index reintroduced it here.
    user_id: Optional[int] = None
    subquestions: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    queries_run: List[str] = field(default_factory=list)
    seen_urls: Set[str] = field(default_factory=set)
    sources: List[SourceRecord] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    iterations: int = 0
    started_at: float = field(default_factory=time.monotonic)
    # --- temporal awareness ---
    today: str = ""
    now_year: int = 0
    temporal: Optional[Verdict] = None
    # --- evidence ---
    claims: List[Claim] = field(default_factory=list)
    resolutions: Dict[int, Resolution] = field(default_factory=dict)
    rounds: List[RoundStats] = field(default_factory=list)
    stop_reason: str = ""
    links_followed: int = 0
    duplicates: List[dict] = field(default_factory=list)
    stale_downranked: List[str] = field(default_factory=list)
    verification_rounds: int = 0
    verification: Optional[dict] = None
    #: Query groups lost to a SearchUnavailableError across the whole run.
    #: A run whose later rounds searched nothing must not read, in meta or in
    #: the report, like a run that mined the web and found little.
    search_outages: int = 0
    #: True when the evidence auditor (`_assess`) could not be reached at all.
    auditor_failed: bool = False
    #: Set when the report stream stopped at its token ceiling (R9).
    report_truncated: bool = False
    #: Stages the wall-clock budget cut short or skipped, in order (R8). A run
    #: that ran out of time must not read like a run that finished its plan.
    cut_short: List[str] = field(default_factory=list)
    #: Set when the REPORT stream itself was stopped by the budget.
    report_cut_short: bool = False
    #: source.n → folded text + sentence spans, so the verbatim check in
    #: `_resolve` folds each page once per run rather than once per round.
    folded: Dict[int, "_Prepared"] = field(default_factory=dict, repr=False)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def gather_budget_s(self) -> float:
        """The wall clock this run may spend BEFORE the report is written.

        `deep_research_timeout_s` used to be advisory (R8): it was consulted
        between rounds and before the verify follow-up, and nowhere else. A
        run at 599 s of a 600 s budget could still start a whole round, a
        verification pass and a long report — a nominal ten-minute run taking
        twenty, while holding one of the process's few research slots against
        every other user. Every expensive stage is now bounded by what is left
        of THIS, and the remainder belongs to the report (`report_budget_s`).

        Admission quotes this budget back to a refused request ("the earliest
        finishes in about N minutes"), so it is now load-bearing for a promise
        made to a DIFFERENT user, not only for this run's own honesty.
        """
        total = float(settings.deep_research_timeout_s or 0.0)
        if total <= 0:
            # A non-positive budget already meant "one round then stop" here;
            # keep exactly that rather than inventing a new behaviour.
            return 0.0
        return max(0.0, total - _report_reserve_s(total))

    def gather_left(self) -> float:
        """Seconds left for gathering; `inf` when no budget is configured.

        A non-positive `deep_research_timeout_s` has always meant "one round,
        then stop" here (`budget_left` compared against it directly), and it
        still does: the loop ends after the round, but the round itself runs
        unbounded rather than being skipped before it starts. Bounding stages
        must not turn a zero budget into a run that gathers nothing.
        """
        if float(settings.deep_research_timeout_s or 0.0) <= 0:
            return float("inf")
        return self.gather_budget_s - self.elapsed

    def report_budget_s(self) -> float:
        """Seconds the report stream may take.

        A run that stopped early gets everything left of the budget; a run
        that spent the whole gathering budget still gets the reserve, because
        a run that reached its limit must still be able to report what it
        read. With no research budget configured, `llm.py`'s own wall clock is
        the only bound — exactly as before.
        """
        total = float(settings.deep_research_timeout_s or 0.0)
        if total <= 0:
            return float(settings.gen_wall_clock_s or 0.0) or _REPORT_FLOOR_S
        return max(
            _REPORT_FLOOR_S,
            _report_reserve_s(total),
            (self.started_at + total) - time.monotonic(),
        )

    def budget_left(self) -> bool:
        return (
            self.iterations < settings.deep_research_max_iterations
            and len(self.sources) < settings.deep_research_max_sources
            and self.elapsed < self.gather_budget_s
        )

    def budget_reason(self) -> str:
        if self.iterations >= settings.deep_research_max_iterations:
            return "iteration_cap"
        if len(self.sources) >= settings.deep_research_max_sources:
            return "source_cap"
        if self.elapsed >= self.gather_budget_s:
            return "timeout"
        return ""

    @property
    def time_sensitive(self) -> bool:
        return bool(self.temporal) and self.temporal.requirement is not Freshness.STATIC

    def source(self, n: int) -> Optional[SourceRecord]:
        if 1 <= n <= len(self.sources):
            return self.sources[n - 1]
        return None

    @property
    def canonical_sources(self) -> List[SourceRecord]:
        return [s for s in self.sources if s.dup_of is None]

    @property
    def primary_sources(self) -> List[SourceRecord]:
        return [s for s in self.sources if s.primary and s.dup_of is None]

    @property
    def confidence(self) -> float:
        """Mean resolution confidence over the subquestions (0 when none)."""
        if not self.resolutions:
            return 0.0
        return sum(r.confidence for r in self.resolutions.values()) / len(self.resolutions)

    def unresolved(self) -> List[int]:
        return [i for i, r in self.resolutions.items() if r.status == STATUS_UNKNOWN]


def _rlog(state: ResearchState, msg: str, *args) -> None:
    log.info("research[%s] " + msg, state.research_id[:8], *args)


async def _bounded(state: ResearchState, stage: str, coro, default=None):
    """Await `coro` inside what is left of the gathering budget (R8).

    Returns `(finished, value)`. A stage that does not finish is RECORDED, not
    raised: everything it registered before the cut — sources, claims,
    resolutions — is already in `state` and stays there, the run goes on to
    write its report from exactly that, and `stop_reason` says the clock ended
    it. A cut round is a SHORT round, not a lost one.

    The log carries the stage name and the numbers only. A stage cut mid-flight
    may be inside a third-party fetch or an LLM call whose exception text can
    carry credentials, and this module's logs are shipped.
    """
    left = state.gather_left()
    if left == float("inf"):
        return True, await coro
    if left <= 0:
        coro.close()  # never started; do not leave an un-awaited coroutine
        state.cut_short.append(stage)
        _rlog(
            state,
            "%s skipped: the %.0fs gathering budget is spent (%.1fs elapsed)",
            stage, state.gather_budget_s, state.elapsed,
        )
        return False, default
    try:
        return True, await asyncio.wait_for(coro, left)
    except asyncio.TimeoutError:
        state.cut_short.append(stage)
        log.warning(
            "research[%s] %s stopped at the run's time budget "
            "(%.1fs elapsed of a %.0fs budget) — the report will be written "
            "from what was already gathered",
            state.research_id[:8], stage, state.elapsed,
            float(settings.deep_research_timeout_s or 0.0),
        )
        return False, default


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _conversation_turns(history: Sequence[dict], n: int) -> List[dict]:
    """The last `n` real turns, WITHOUT the pinned system blocks.

    `recent_turns` keeps every system message on purpose — the chat engine
    needs the user's saved facts and cross-chat recall. A research PLAN does
    not: on the first live run the planner read the memory block and listed
    the signed-in user's own name as an entity to research. Only what the
    user actually asked is research context."""
    return [m for m in recent_turns(history, n) if m.get("role") != "system"]


def _temporal_note(state: ResearchState) -> str:
    if not state.temporal:
        return ""
    level = state.temporal.requirement
    if level is Freshness.STATIC:
        return (
            f"Current date: {state.today}. The question is timeless: prefer "
            "primary and reference sources over recency."
        )
    horizon = "hours" if level is Freshness.REALTIME else "months"
    return (
        f"Current date: {state.today}. The question is TIME-SENSITIVE (its answer "
        f"changes on the scale of {horizon}): prefer the newest authoritative "
        "evidence, look for the current official statement, and treat anything "
        "older than the latest change as history."
    )


async def _plan(question: str, history: Sequence[dict], effort: str, state: Optional[ResearchState] = None) -> Tuple[List[str], List[str]]:
    """Decompose the question into subquestions + a first round of queries."""
    temporal = _temporal_note(state) if state else ""
    system = (
        "You are a research planner. Break the user's question into the "
        "distinct SUBQUESTIONS that must each be answered for the whole "
        "question to be answered well, then write web-search queries that "
        "would find evidence for them. Each query must look for something "
        "DIFFERENT — never paraphrase another query. Cover three angles: the "
        "direct question, the primary/official source (the organisation, "
        "product, standard or paper itself), and the most recent development. "
        "Prefer specific, technical phrasing over conversational phrasing. "
        "List the named entities (people, organisations, products, places) "
        "the question is about. Return JSON only."
        + (f"\n{temporal}" if temporal else "")
    )
    msgs = [
        {"role": "system", "content": system},
        *_conversation_turns(history, 4),
        {"role": "user", "content": question},
    ]
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                msgs,
                json_schema=_plan_schema(),
                schema_name="research_plan",
                temperature=0.2,
                max_tokens=900,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001 — planning is an upgrade, never a gate
        log.warning("research planning failed; falling back to the raw question", exc_info=True)
        data = {}

    subs = [s.strip() for s in (data.get("subquestions") or []) if isinstance(s, str) and s.strip()]
    queries = [q.strip() for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
    if state is not None:
        state.entities = [
            e.strip() for e in (data.get("entities") or []) if isinstance(e, str) and e.strip()
        ][:8]
    if not queries:
        queries = [question]
    cap = settings.deep_research_max_queries_per_iteration
    queries = queries[:cap]
    if state is not None:
        queries = _augment_queries(state, queries, cap)
    return subs[:8], queries


def _augment_queries(state: ResearchState, queries: List[str], cap: int) -> List[str]:
    """Add the year to one query for a time-sensitive question that names
    none — the cheapest way to make a search engine prefer this year's
    pages over the evergreen ones. Deterministic, derived from the clock."""
    if not state.time_sensitive or not state.temporal or not state.temporal.reason.startswith("lexical"):
        return queries
    if any(_YEAR_RE.search(q) for q in queries):
        return queries
    dated = f"{queries[0]} {state.now_year}"
    if dated in queries:
        return queries
    out = list(queries)
    if len(out) < cap:
        out.append(dated)
    else:
        out[-1] = dated
    _rlog(state, "query augmented with the current year: %r", dated)
    return out


# ---------------------------------------------------------------------------
# Registration: provenance, duplicates, primaries
# ---------------------------------------------------------------------------


def _register(
    state: ResearchState, src, query: str, discovered_via: str = "search"
) -> Optional[SourceRecord]:
    """Turn a fetched page into a registry entry, with provenance, and note
    when it is a syndicated copy of a page already read."""
    text = (getattr(src, "text", "") or "")
    if not text.strip():
        return None
    url = src.url
    authority = int(getattr(src, "authority", 0) or 0) or authority_of(url)
    kind = getattr(src, "source_type", "") or provenance.source_type(url)
    rec = SourceRecord(
        n=len(state.sources) + 1,
        title=src.title,
        url=url,
        text=text,
        query=query,
        iteration=state.iterations,
        authority=authority,
        source_type=kind,
        published_at=getattr(src, "published_at", None),
        modified_at=getattr(src, "modified_at", None),
        fetched_at=getattr(src, "fetched_at", None) or datetime.now(timezone.utc),
        content_hash=getattr(src, "content_hash", "") or "",
        discovered_via=discovered_via,
        links=list(getattr(src, "links", []) or [])[:500],
        fingerprint=provenance.shingles(text),
        primary=provenance.is_primary(url, kind, authority),
        # The plan's entities are what makes self-publication visible: with an
        # empty list this is exactly `is_primary` as a float, so a run whose
        # planner named nothing behaves as before rather than half-trusting
        # everything.
        primary_weight=provenance.primary_weight(url, kind, authority, state.entities),
        from_store=bool(getattr(src, "from_store", False)),
    )
    threshold = float(settings.deep_research_duplicate_threshold or 0)
    if rec.fingerprint and threshold > 0:
        for other in state.sources:
            if other.dup_of is not None or not other.fingerprint:
                continue
            if provenance.near_duplicate(rec.fingerprint, other.fingerprint, threshold):
                rec.dup_of = other.n
                state.duplicates.append({"n": rec.n, "url": url, "dup_of": other.n, "of_url": other.url})
                break
    state.sources.append(rec)
    _rlog(
        state,
        "opened [%d] %s (%s) — %s, %d chars, %s%s",
        rec.n, rec.domain_key, discovered_via, rec.label(), len(text),
        "from store" if rec.from_store else "fetched",
        f", DUPLICATE of [{rec.dup_of}]" if rec.dup_of else "",
    )
    if rec.primary and rec.dup_of is None:
        _rlog(state, "primary source: [%d] %s (%s, authority %d)", rec.n, rec.url, rec.source_type, rec.authority)
    return rec


# ---------------------------------------------------------------------------
# Candidate ranking (before fetching)
# ---------------------------------------------------------------------------


def _stale_years(text: str, now_year: int) -> bool:
    """Every year the snippet mentions is clearly old — for a time-sensitive
    question, a page about an earlier state of the world."""
    years = [int(m.group(0)) for m in _YEAR_RE.finditer(text or "")]
    return bool(years) and max(years) <= now_year - 3


async def _rank_candidates(
    state: ResearchState, results: List[SearchResult]
) -> List[SearchResult]:
    """Order search results by topicality × authority × freshness hints.

    The reranker supplies topical order (an authority-blind cross-encoder);
    the domain's authority prior and the structural source class are added
    so the official page and the first-party documentation are read before
    the SEO rewrite of them; and for a time-sensitive question a snippet that
    only mentions years long past is pushed down — never dropped, because it
    may be the history the report needs."""
    if not results:
        return []
    ordered = await _rerank_results(state.question, results, len(results))
    n = len(ordered)
    scored: List[Tuple[float, int, SearchResult]] = []
    for i, r in enumerate(ordered):
        topical = 1.0 - (i / float(max(1, n)))
        auth = authority_of(r.url) / 100.0
        kind = provenance.source_type(r.url)
        type_bonus = 0.15 if kind in provenance.PRIMARY_TYPES else (-0.15 if kind in ("social", "community") else 0.0)
        score = 0.6 * topical + 0.25 * auth + type_bonus
        if state.time_sensitive and _stale_years(f"{r.title} {r.snippet}", state.now_year):
            score -= 0.2
            state.stale_downranked.append(r.url)
        scored.append((score, i, r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _s, _i, r in scored]


# ---------------------------------------------------------------------------
# Link following
# ---------------------------------------------------------------------------


def _topic_keywords(state: ResearchState) -> Set[str]:
    text = " ".join([state.question, *state.subquestions, *state.entities])
    return set(keywords(text, max_keywords=24))


def _entity_tokens(state: ResearchState) -> Set[str]:
    """Alphanumeric tokens of the plan's named entities ("openai", "acme"),
    for recognising the entity's OWN domain in a link's host."""
    out: Set[str] = set()
    for e in state.entities:
        for tok in re.findall(r"[a-z0-9]+", e.lower()):
            if len(tok) >= 3:
                out.add(tok)
    return out


def _link_score(state: ResearchState, src: SourceRecord, link: str, kw: Set[str]) -> Optional[float]:
    try:
        parts = urlparse(link)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    path = (parts.path or "/").lower()
    if _SKIP_EXT_RE.search(path) or _SKIP_LINK_RE.search(link):
        return None
    host = provenance.domain_of(link)
    tokens = set(re.findall(r"[a-z0-9]+", path.replace("-", " ").replace("_", " ")))
    overlap = len(tokens & kw)
    link_auth = authority_of(link)
    kind = provenance.source_type(link)
    entity_host = any(tok in host for tok in _entity_tokens(state))
    known_domain = host in {s.domain_key for s in state.sources}
    score = min(3, overlap) * 1.0
    if entity_host:
        score += 2.0  # the entity's own site: first-hand by definition
    if link_auth >= src.authority + 20:
        score += 1.0  # an article pointing at a more authoritative page
    if kind in provenance.PRIMARY_TYPES:
        score += 1.5
    if path.endswith(".pdf"):
        score += 1.0
    if host == src.domain_key and src.primary:
        score += 0.5  # deeper into a primary site
    if kind in ("social", "community"):
        score -= 2.0
    if path.count("/") > 6:
        score -= 1.0
    if parts.query:
        score -= 0.5
    if overlap == 0 and not entity_host and not known_domain and not path.endswith(".pdf"):
        # Nothing ties this link to the question: not its words, not the
        # entity's own site, not a domain this run already found relevant. A
        # high-authority target alone is not a reason — on the first live run
        # an article's incidental links to government statistics and a
        # vendor's press page were opened for a question about neither.
        return None
    return score


def _candidate_links(state: ResearchState, new_sources: List[SourceRecord], limit: int) -> List[Tuple[str, SourceRecord]]:
    kw = _topic_keywords(state)
    scored: List[Tuple[float, str, SourceRecord]] = []
    for src in new_sources:
        per_page = 0
        page_scores: List[Tuple[float, str]] = []
        for link in src.links:
            key = _normalize_url(link)
            if key in state.seen_urls or key == _normalize_url(src.url):
                continue
            s = _link_score(state, src, link, kw)
            if s is None or s <= 0:
                continue
            page_scores.append((s, link))
        page_scores.sort(key=lambda t: -t[0])
        for s, link in page_scores:
            if per_page >= 2:
                break
            scored.append((s, link, src))
            per_page += 1
    scored.sort(key=lambda t: -t[0])
    out: List[Tuple[str, SourceRecord]] = []
    seen: Set[str] = set()
    for _s, link, src in scored:
        key = _normalize_url(link)
        if key in seen:
            continue
        seen.add(key)
        out.append((link, src))
        if len(out) >= limit:
            break
    return out


async def _follow_links(
    state: ResearchState,
    new_sources: List[SourceRecord],
    effort: str,
    emit: Optional[Emit],
    stats: RoundStats,
) -> List[SourceRecord]:
    """Open the most promising links FROM the pages just read."""
    limit = int(settings.deep_research_links_per_round or 0)
    room = max(0, settings.deep_research_max_sources - len(state.sources))
    limit = min(limit, room)
    if limit <= 0 or not new_sources:
        return []
    picks = _candidate_links(state, new_sources, limit)
    if not picks:
        return []
    results = [SearchResult(title=link, url=link, snippet="") for link, _src in picks]
    for r in results:
        state.seen_urls.add(_normalize_url(r.url))
    stats.attempted += len(results)
    _rlog(state, "following %d link(s): %s", len(results), "; ".join(
        f"{link} (from [{src.n}])" for link, src in picks))
    try:
        fetched = await _fetch_sources(results, state.question)
    except Exception:  # noqa: BLE001
        log.warning("link fetch round failed", exc_info=True)
        return []
    by_url = {link: src for link, src in picks}
    added: List[SourceRecord] = []
    for src in fetched:
        origin = by_url.get(src.url)
        rec = _register(state, src, state.question, f"link:{origin.n}" if origin else "link")
        if rec is None:
            continue
        stats.fetched += 1
        if rec.dup_of:
            stats.duplicates += 1
        else:
            stats.new_sources += 1
        added.append(rec)
    stats.links_followed += len(added)
    state.links_followed += len(added)
    if emit is not None and added:
        # Shown in the Research panel as its own group — the same event
        # shape a search uses, so no client change is needed.
        by_domain = sorted({s.domain_key for s in new_sources if any(p[1] is s for p in picks)})
        await emit(
            "research",
            {
                "phase": "query",
                "query": "↳ links followed from " + ", ".join(by_domain[:3]),
                "results": [
                    {"title": s.title, "url": s.url, "domain": s.domain_key} for s in added
                ],
            },
        )
    return added


# ---------------------------------------------------------------------------
# Gather: one round
# ---------------------------------------------------------------------------


async def _gather(
    state: ResearchState,
    queries: List[str],
    effort: str,
    emit: Optional[Emit],
    label: str = "search",
) -> List[SourceRecord]:
    """One round: search → rank → fetch → register → follow links → extract
    claims → resolve. Never raises."""
    stats = RoundStats(iteration=state.iterations, label=label, queries=list(queries))
    state.rounds.append(stats)
    started = time.monotonic()
    _rlog(state, "round %d (%s): %d queries: %s", state.iterations, label, len(queries), " | ".join(queries))

    # Group by routed category so each group is one _collect_results call
    # (which already parallelises within itself and merges round-robin).
    by_category: Dict[str, List[str]] = {}
    for q in queries:
        by_category.setdefault(route_category(q), []).append(q)

    results: List[SearchResult] = []
    for category, group in by_category.items():
        try:
            found = await _collect_results(group, effort, emit, category)
        except SearchUnavailableError:
            # A mid-run outage used to be a bare `continue`: no log, no
            # counter. Search dying after round 1 then looked exactly like a
            # web with nothing left to give — the loop stopped on
            # `no_information_gain` and a thin report was presented as a
            # mined web. Counted here, surfaced in meta and in the report
            # prompt. The CATEGORY only: the exception carries the upstream
            # message, which can hold credentials, and this module's logs are
            # asserted on for exactly that.
            stats.search_outages += 1
            state.search_outages += 1
            log.warning(
                "research search provider unavailable (category=%s, round=%d, "
                "queries=%d) — this round searched nothing in that pool",
                category or "general", state.iterations, len(group),
            )
            continue
        except Exception:  # noqa: BLE001 — one bad group must not end the run
            log.warning("research search group failed (%s)", category or "general", exc_info=True)
            continue
        results.extend(found)
    if not results:
        stats.elapsed_s = time.monotonic() - started
        _rlog(state, "round %d: no results", state.iterations)
        return []
    state.queries_run.extend(queries)

    # Drop anything already read in an earlier round BEFORE spending a fetch.
    fresh = [r for r in results if _normalize_url(r.url) not in state.seen_urls]
    if not fresh:
        stats.elapsed_s = time.monotonic() - started
        _rlog(state, "round %d: %d results, all already read", state.iterations, len(results))
        return []

    room = max(0, settings.deep_research_max_sources - len(state.sources))
    want = min(settings.deep_research_sources_per_iteration, room)
    if want <= 0:
        stats.elapsed_s = time.monotonic() - started
        return []
    fresh = await _rank_candidates(state, fresh)
    fresh = fresh[:want]

    for r in fresh:
        state.seen_urls.add(_normalize_url(r.url))
    stats.attempted += len(fresh)

    try:
        fetched = await _fetch_sources(fresh, state.question)
    except Exception:  # noqa: BLE001
        log.warning("research round fetch failed", exc_info=True)
        stats.elapsed_s = time.monotonic() - started
        return []

    added: List[SourceRecord] = []
    for src in fetched:
        rec = _register(state, src, queries[0] if queries else state.question)
        if rec is None:
            continue
        stats.fetched += 1
        if rec.dup_of:
            stats.duplicates += 1
        else:
            stats.new_sources += 1
        added.append(rec)

    # Open the citations the pages themselves give — the primary sources.
    followed = await _follow_links(state, [s for s in added if s.dup_of is None], effort, emit, stats)
    added.extend(followed)

    # Remember the round for the next question, exactly like a plain search.
    _spawn(
        _persist_and_index(
            state.question,
            queries,
            results,
            effort,
            state.user_id,
            state.conversation_id,
        )
    )

    # Extract dated claims from what was just read, then resolve.
    new_canonical = [s for s in added if s.dup_of is None]
    if new_canonical:
        before = len(state.claims)
        await _extract_claims(state, new_canonical, effort, emit)
        stats.new_claims = len(state.claims) - before
        _resolve(state)
    stats.elapsed_s = time.monotonic() - started
    _rlog(
        state,
        "round %d done: attempted %d, fetched %d, new %d, duplicates %d, links %d, "
        "claims +%d, gain %.2f, %.1fs",
        state.iterations, stats.attempted, stats.fetched, stats.new_sources,
        stats.duplicates, stats.links_followed, stats.new_claims, stats.gain, stats.elapsed_s,
    )
    return added


async def _gather_bounded(
    state: ResearchState,
    queries: List[str],
    effort: str,
    emit: Optional[Emit],
    label: str,
) -> Tuple[List[SourceRecord], bool]:
    """One round under the run's remaining wall clock. → (added, was_cut).

    `_gather` is the expensive stage — searches, fetches, an extraction call
    per round — and it was entirely unbounded: the budget was only ever read
    BETWEEN rounds (R8).
    """
    started = time.monotonic()
    round_no = state.iterations
    ok, added = await _bounded(
        state,
        f"round {round_no} ({label})",
        _gather(state, queries, effort, emit, label),
        [],
    )
    # A round cancelled mid-flight never reached its own bookkeeping, and a
    # round reported as 0.0s in meta reads as one that never ran.
    stats = state.rounds[-1] if state.rounds else None
    if stats is not None and stats.iteration == round_no and not stats.elapsed_s:
        stats.elapsed_s = time.monotonic() - started
    return list(added or []), not ok


# ---------------------------------------------------------------------------
# Claims: extract with dates, resolve in code
# ---------------------------------------------------------------------------


def _parse_as_of(value: object) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", text)
    if not m:
        dt = provenance.parse_date(text)
        return dt.date() if dt else None
    y, mo, d = m.groups()
    try:
        return date(int(y), int(mo or 1), int(d or 1))
    except ValueError:
        return None


def _claim_time(state: ResearchState, c: Claim) -> Optional[date]:
    if c.as_of:
        return c.as_of
    src = state.source(c.source_n)
    if src and src.effective_time and (src.published_at or src.modified_at):
        return src.effective_time.date()
    return None


async def _extract_claims(
    state: ResearchState, new_sources: List[SourceRecord], effort: str, emit: Optional[Emit]
) -> None:
    """Pull dated claims out of the sources just read. One batched call."""
    subqs = state.subquestions or [state.question]
    if emit is not None:
        await emit("status", {"text": f"Extracting claims from {len(new_sources)} source(s)…"})
    system = (
        "You extract DATED factual claims from web sources for a research "
        "question. For each claim give: the subquestion number it answers, "
        "the claim as one sentence, its value (the specific name, number, "
        "date or version it asserts — or empty), the source number, as_of "
        "(YYYY-MM-DD, YYYY-MM or YYYY: WHEN the fact held according to the "
        "source — an effective date, event date or the article's own date; "
        "empty when the source does not say; NEVER today's date unless the "
        "source itself states it), and status: 'current' when the "
        "source presents it as the present state, 'historical' when the source "
        "presents it as past (former, previously, until, was replaced), "
        "'unclear' otherwise. Only claims the sources ACTUALLY state — never "
        "from your own memory. Prefer claims that answer the subquestions; "
        "skip boilerplate. Return JSON only."
        + f"\n{_temporal_note(state)}"
    )
    blocks = []
    for s in new_sources:
        # Query-centred, not the first 2,500 characters (C1). This excerpt is
        # what BECOMES the evidence: a fact outside it is never extracted as a
        # claim, never resolved, and never verified, however many times the
        # page is cited. Measured on `leaderboard_long.html`: the head slice
        # drops the answer row, the selection keeps it. Lines are kept — the
        # rows of a table are one per line, and flattening them to a single
        # paragraph is what made a leaderboard read as a run of bare numbers.
        excerpt = _select_text(s.text or "", state.question, 2500)
        blocks.append(f"[{s.n}] {s.title} ({s.label()})\n{excerpt}")
    user = (
        f"QUESTION: {state.question}\n\nSUBQUESTIONS:\n"
        + "\n".join(f"{i}. {q}" for i, q in enumerate(subqs, 1))
        + "\n\nNEW SOURCES:\n" + "\n\n".join(blocks)
    )
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_schema=_claims_schema(),
                schema_name="research_claims",
                temperature=0.0,
                max_tokens=2500,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001 — claims are an upgrade, never a gate
        log.warning("claim extraction failed for round %d", state.iterations, exc_info=True)
        return
    valid_n = {s.n for s in new_sources}
    added = 0
    for item in data.get("claims") or []:
        if not isinstance(item, dict):
            continue
        try:
            subq = int(item.get("subquestion") or 0)
            source_n = int(item.get("source") or 0)
        except (TypeError, ValueError):
            continue
        text = " ".join(str(item.get("claim") or "").split())
        if not text or source_n not in valid_n:
            continue
        if not (1 <= subq <= len(subqs)):
            subq = 1 if len(subqs) == 1 else 0
        if subq == 0:
            continue
        hint = str(item.get("status") or "unclear").lower()
        if hint not in ("current", "historical", "unclear"):
            hint = "unclear"
        state.claims.append(
            Claim(
                subq=subq,
                text=text[:600],
                value=" ".join(str(item.get("value") or "").split())[:300],
                source_n=source_n,
                as_of=_parse_as_of(item.get("as_of")),
                hint=hint,
                iteration=state.iterations,
            )
        )
        added += 1
    _rlog(state, "claims extracted: %d from %d source(s)", added, len(new_sources))


_VALUE_NOISE_RE = re.compile(r"[^a-z0-9. ]+")


def _norm_value(value: str) -> str:
    t = _VALUE_NOISE_RE.sub(" ", (value or "").lower())
    t = re.sub(r"(\d),(\d)", r"\1\2", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _value_is_stated(state: ResearchState, value: str, support: Sequence[int]) -> bool:
    """Does one of the supporting sources actually state `value`?

    The same matcher the shared claim store gates on (`_quote_in`: fold, whole
    words, a bounded fuzzy path for long values) — moved in front of the
    report instead of only behind it. Folding a page is a per-character Python
    loop, and `_resolve` runs after every round, so each page is folded once
    per run and cached on the state.
    """
    value = " ".join((value or "").split())
    if not value:
        return False
    for src in _supporting_sources(state, support):
        prepared = state.folded.get(src.n)
        if prepared is None:
            prepared = _prepare(src.text)
            state.folded[src.n] = prepared
        if _quote_in(src.text, value, prepared):
            return True
    return False


def _resolve(state: ResearchState) -> None:
    """Decide, in code, what each subquestion's evidence adds up to.

    Groups claims by value; picks the best-supported group on a blend of
    recency, authority and independent corroboration; labels the rest as
    SUPERSEDED (an earlier value with an earlier date — a change over time)
    or CONFLICTING (a different value of comparable date and authority — a
    real disagreement the report must surface). Claims the source itself
    presents as history never compete for "current".
    """
    subqs = state.subquestions or [state.question]
    max_age = state.temporal.max_age_seconds if state.temporal else 14 * 86400
    today = date.today()
    for i, subq in enumerate(subqs, 1):
        claims = [c for c in state.claims if c.subq == i]
        if not claims:
            state.resolutions[i] = Resolution(i, subq, STATUS_UNKNOWN)
            continue
        # TWO CLAIMS ONLY DISAGREE IF THEY SAY DIFFERENT THINGS ABOUT THE
        # SAME SLOT. The key used to be `value, or failing that the first 80
        # characters of the SENTENCE` — so two sources describing one fact in
        # different words landed in different groups, and the ranking below
        # then read every group it did not pick as a rival. Measured
        # 2026-09-04: 17 of 18 logged resolutions came out CONFLICTING, run
        # confidence sat at 0.28, and reports opened with a "CONTRADICTIONS
        # BETWEEN SOURCES" section listing things that did not contradict.
        #
        # A claim the extractor gave no `value` has nothing to compare. It is
        # EVIDENCE for the answer, not a competing answer, so all such claims
        # pool into one non-comparable group whose sources are folded into the
        # winner instead of being ranked against it.
        groups: Dict[str, List[Claim]] = {}
        loose: List[Claim] = []
        for c in claims:
            key = _norm_value(c.value)
            if key:
                groups.setdefault(key, []).append(c)
            else:
                loose.append(c)
        # Nothing had a value: the subquestion is prose, and prose that agrees
        # is agreement. One group, one answer, no conflict.
        if not groups and loose:
            groups["_prose"] = loose
            loose = []

        scored: List[dict] = []
        for key, cs in groups.items():
            srcs = [state.source(c.source_n) for c in cs]
            srcs = [s for s in srcs if s is not None]
            canonical = sorted({s.canonical_n for s in srcs})
            domains = {s.domain_key for s in srcs if s.dup_of is None}
            auth = max((s.authority for s in srcs), default=40)
            primary = any(s.primary for s in srcs)
            # The BEST first-hand claim in the group, not the average: one
            # disinterested primary source earns the whole bonus even when the
            # subject's own page is sitting next to it.
            primary_weight = max((s.primary_weight for s in srcs), default=0.0)
            times = [t for t in (_claim_time(state, c) for c in cs) if t]
            when = max(times) if times else None
            historical = bool(cs) and all(c.hint == "historical" for c in cs)
            scored.append(
                {
                    "key": key, "claims": cs, "support": canonical,
                    "independent": max(1, len(domains)) if srcs else 0,
                    "authority": auth, "primary": primary,
                    "primary_weight": primary_weight, "when": when,
                    "historical": historical, "value": (cs[0].value or cs[0].text)[:200],
                }
            )
        candidates = [g for g in scored if not g["historical"]] or scored
        known = [g["when"] for g in candidates if g["when"]]
        newest = max(known) if known else None
        oldest = min(known) if known else None

        def composite(g: dict) -> float:
            if g["when"] and newest and oldest and newest != oldest:
                t = (g["when"] - oldest).days / float(max(1, (newest - oldest).days))
            elif g["when"]:
                t = 1.0
            else:
                t = 0.3
            if not state.time_sensitive:
                t = 0.5  # recency is not the deciding signal for a timeless fact
            return 0.5 * t + 0.35 * (g["authority"] / 100.0) + 0.15 * min(1.0, g["independent"] / 3.0)

        candidates.sort(key=composite, reverse=True)
        winner = candidates[0]
        # The prose claims corroborate whichever value won; their sources
        # belong in its support, not in a contradiction list.
        for c in loose:
            src = state.source(c.source_n)
            if src is not None and src.canonical_n not in winner["support"]:
                winner["support"].append(src.canonical_n)
        winner["support"].sort()
        superseded: List[dict] = []
        conflicts: List[dict] = []
        for g in scored:
            if g is winner or g["key"] == winner["key"]:
                continue
            entry = {
                "value": g["value"],
                "as_of": g["when"].isoformat() if g["when"] else "",
                "sources": g["support"],
                "authority": g["authority"],
            }
            if g["historical"]:
                superseded.append(entry)
                continue
            gw, ww = g["when"], winner["when"]
            if gw and ww and gw < ww - timedelta(days=1):
                superseded.append(entry)  # older value, earlier date: a change over time
            elif gw and ww and gw > ww + timedelta(days=1):
                # Newer date but it LOST the ranking (much weaker source): a
                # disagreement to verify, not a fact to adopt.
                conflicts.append(entry)
            elif g["authority"] >= winner["authority"] - _AUTHORITY_GAP:
                conflicts.append(entry)  # comparable authority, comparable/unknown date
            else:
                # A MUCH weaker source, and no date on either side. Filing
                # this as `superseded` invented history: rule 7 of the report
                # prompt then presents a superseded value as a change over
                # time ("previously X until Y"), so an open disagreement was
                # narrated as a settled sequence of events that no source
                # dated, let alone stated. It is a disagreement — recorded as
                # one, with the weakness of the dissenting source marked.
                entry["weak"] = True
                conflicts.append(entry)

        status = STATUS_CONFLICTING if conflicts else STATUS_CURRENT
        confidence = 0.3
        confidence += 0.25 if winner["independent"] >= 2 else 0.0
        confidence += 0.2 if winner["authority"] >= 70 else (0.05 if winner["authority"] >= 40 else 0.0)
        # R12: SCALED by how first-hand the source actually is. It was
        # `0.1 if primary`, which paid a vendor's own benchmark page the same
        # first-hand bonus as an independent laboratory's measurement of it.
        # A self-published primary keeps half — it is genuinely first-hand,
        # it is just not disinterested (provenance.SELF_PUBLISHED_PRIMARY_WEIGHT).
        confidence += 0.1 * float(winner.get("primary_weight") or 0.0)
        if winner["when"] and state.time_sensitive:
            age_days = (today - winner["when"]).days
            confidence += 0.1 if age_days * 86400 <= max_age else -0.05
        elif not state.time_sensitive:
            confidence += 0.05
        if conflicts:
            # A disagreement between comparable sources is a real open
            # question; one raised only by a much weaker source is a doubt.
            confidence -= 0.1 if all(c.get("weak") for c in conflicts) else 0.25
        # Does any source actually SAY this? Same matcher, same rule as the
        # shared claim store — run here, before the value reaches the report
        # prompt, the evidence table and meta, instead of only afterwards.
        # Skipped for a prose group: it has no value to check, only the
        # extractor's sentence, and holding a paraphrase to a verbatim test
        # would mark every prose answer unstated.
        stated = True
        if winner["key"] != "_prose":
            stated = _value_is_stated(state, winner["value"], winner["support"])
            if not stated:
                confidence = min(
                    confidence - _UNSTATED_CONFIDENCE_PENALTY, _UNSTATED_CONFIDENCE_CAP
                )
        confidence = max(0.05, min(0.98, confidence))
        state.resolutions[i] = Resolution(
            subq=i,
            question=subq,
            status=status,
            value=winner["value"],
            as_of=winner["when"],
            support=winner["support"],
            independent=winner["independent"],
            primary=winner["primary"],
            superseded=superseded,
            conflicts=conflicts,
            confidence=confidence,
            stated_verbatim=stated,
        )
    for r in state.resolutions.values():
        _rlog(state, "resolution %s", r.line())


def _resolution_table(state: ResearchState) -> str:
    if not state.resolutions:
        return "(no claims extracted yet)"
    return "\n".join(state.resolutions[i].line() for i in sorted(state.resolutions))


# ---------------------------------------------------------------------------
# Assess: what is still missing, and where to look
# ---------------------------------------------------------------------------


async def _assess(state: ResearchState, effort: str) -> dict:
    """Read what we have and decide whether another round is warranted."""
    covered = "\n".join(
        # Query-centred (C1): an auditor shown the first 1,000 characters of
        # a page judges the lede, not the evidence, and reports a gap the page
        # actually closes — which costs another round of searching.
        f"[{s.n}] {s.title} ({s.domain_key}; {s.label()}) — {_select_text(s.text or '', state.question, 1000)}"
        for s in state.sources[-24:]
    )
    system = (
        "You are a research auditor. Given the question, its subquestions, "
        "the EVIDENCE STATUS table (what the claims read so far establish, "
        "with dates and confidence) and the sources, decide whether the "
        "evidence is enough to write a well-supported report. Be strict: a "
        "subquestion whose status is UNKNOWN, or whose only support is one "
        "second-hand source, is NOT sufficient. Distinguish 'unknown' from "
        "'not found yet': before concluding that information does not exist, "
        "write follow-up queries that look elsewhere — different wording, the "
        "primary/official source, a document or PDF, the named entities "
        "themselves. List what is missing, note contradictions, and give "
        "primary_source_queries that would open the first-hand source for the "
        "important claims. Do not repeat queries already run. Return JSON only."
        + f"\n{_temporal_note(state)}"
    )
    user = (
        f"QUESTION: {state.question}\n\n"
        f"SUBQUESTIONS:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(state.subquestions or [state.question], 1)) + "\n\n"
        f"EVIDENCE STATUS:\n{_resolution_table(state)}\n\n"
        f"QUERIES ALREADY RUN:\n" + "\n".join(f"- {q}" for q in state.queries_run) + "\n\n"
        f"SOURCES ({len(state.sources)}, {len(state.canonical_sources)} distinct, "
        f"{len(state.primary_sources)} primary):\n{covered}"
    )
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_schema=_gap_schema(),
                schema_name="research_gaps",
                temperature=0.1,
                max_tokens=900,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001
        # NOT "sufficient". Returning that made an auditor OUTAGE indis-
        # tinguishable from an audit that found nothing missing: the step
        # detail said "no gaps found", meta.research_run.stop_reason said
        # `sufficient`, and the run claimed to have been judged adequate by a
        # check that never ran. An outage is its own answer.
        state.auditor_failed = True
        log.warning("gap analysis failed; the evidence was NOT audited")
        return {
            "sufficient": False,
            "auditor_failed": True,
            "missing": [],
            "contradictions": [],
            "followup_queries": [],
        }
    if not isinstance(data.get("sufficient"), bool):
        data["sufficient"] = True
    return data


def _synthetic_followups(state: ResearchState, missing: List[str]) -> List[str]:
    """Targeted queries the CODE adds when the auditor reports gaps: search
    the authoritative domains this run has already found for the unresolved
    subquestions. General by construction — the domains come from the run's
    own evidence, never from a list."""
    if not missing:
        return []
    domains: List[str] = []
    for s in sorted(state.canonical_sources, key=lambda x: -x.authority):
        if s.authority >= 70 and s.domain_key and s.domain_key not in domains:
            domains.append(s.domain_key)
        if len(domains) >= 2:
            break
    if not domains:
        return []
    out: List[str] = []
    targets = [state.resolutions[i].question for i in state.unresolved()] or missing[:2]
    for subq in targets[:2]:
        for dom in domains:
            q = f"site:{dom} {subq}"
            if q not in state.queries_run:
                out.append(q)
    return out[:3]


def _accumulate(existing: Sequence[str], incoming: object, cap: int = 24) -> List[str]:
    """`existing` + the new strings of `incoming`, de-duplicated, first-seen
    order kept.

    The gap and contradiction lists used to be REASSIGNED from each audit, so
    a gap found in round 2 and not repeated in round 4 never reached the
    report's "What this report could not establish" section — the one place a
    reader learns what the run could not do. Nothing else in the loop closes
    a gap explicitly, so forgetting one is a silent claim to have answered it.
    """
    out = list(existing)
    seen = {" ".join(s.lower().split()) for s in out}
    for item in incoming or []:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:cap]


def _followup_queries(state: ResearchState, verdict: dict) -> List[str]:
    cap = settings.deep_research_max_queries_per_iteration
    wanted: List[str] = []
    for key in ("followup_queries", "primary_source_queries"):
        for q in verdict.get(key) or []:
            if isinstance(q, str) and q.strip() and q not in state.queries_run and q not in wanted:
                wanted.append(q.strip())
    for q in _synthetic_followups(state, list(verdict.get("missing") or [])):
        if q not in wanted:
            wanted.append(q)
    return wanted[:cap]


def _should_stop(state: ResearchState, verdict: dict, followups: List[str]) -> str:
    """The stop reason, or '' to keep going. Evidence-driven, in this order."""
    if verdict.get("auditor_failed"):
        # The auditor is what decides whether to keep going, and it is down.
        # Another round would search whatever the last one searched (it
        # produces no follow-ups), so the run stops — under its own name, so
        # the report and the run record say the evidence was never audited
        # rather than that it was audited and found sufficient.
        return "auditor_unavailable"
    if verdict.get("sufficient") and not state.unresolved():
        return "sufficient"
    if verdict.get("sufficient") and not followups:
        return "sufficient"
    if not state.budget_left():
        return state.budget_reason() or "budget"
    rounds = state.rounds
    min_gain = float(settings.deep_research_min_gain or 0)
    if len(rounds) >= 2 and rounds[-1].gain < min_gain and rounds[-2].gain < min_gain:
        return "no_information_gain"
    if rounds and rounds[-1].fetched >= 4 and rounds[-1].duplicate_rate >= 0.7 and rounds[-1].new_claims == 0:
        return "duplicate_rate"
    if not followups:
        return "no_new_queries"
    if verdict.get("sufficient"):
        # The auditor is satisfied but a subquestion is still UNKNOWN and
        # there are places left to look: one more round, budget permitting.
        return ""
    return ""


# ---------------------------------------------------------------------------
# Verify: the self-correction pass
# ---------------------------------------------------------------------------


async def _verify(state: ResearchState, effort: str) -> Tuple[dict, List[str]]:
    """Audit the resolved claims before writing. → (verdict, queries to run)."""
    subqs = state.subquestions or [state.question]
    excerpts = "\n".join(
        # Query-centred (C1), with the same caveat measured on the fixture:
        # under ~1,000 characters the selector cannot always reach a passage
        # 19,000 characters into a page. Never worse than the head slice; not
        # always enough on its own, which is why this pass ASKS for a round
        # rather than concluding absence.
        f"[{s.n}] {s.title} ({s.domain_key}; {s.label()}) — {_select_text(s.text or '', state.question, 700)}"
        for s in state.canonical_sources[-20:]
    )
    system = (
        "You are auditing a research run BEFORE its report is written. For "
        "each subquestion, judge the EVIDENCE STATUS and the sources: is there "
        "enough evidence; was a first-hand/official/primary source actually "
        "opened for it; is a newer authoritative source likely to exist that "
        "was not read; do the sources disagree; where values differ, is it a "
        "genuine contradiction or a change over time (a historical value "
        "mistaken for the current one); and how confident (0-1) a careful "
        "analyst would be in the resolved value. For each subquestion below "
        "0.6, give up to two verification web-search queries that would raise "
        "confidence — the primary source, the newest official statement, or "
        "the disagreement itself. Return JSON only."
        + f"\n{_temporal_note(state)}"
    )
    user = (
        f"QUESTION: {state.question}\n\nSUBQUESTIONS:\n"
        + "\n".join(f"{i}. {q}" for i, q in enumerate(subqs, 1))
        + f"\n\nEVIDENCE STATUS:\n{_resolution_table(state)}\n\nSOURCES:\n{excerpts}"
    )
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_schema=_verify_schema(),
                schema_name="research_verify",
                temperature=0.0,
                max_tokens=900,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001 — verification is an upgrade, never a gate
        log.warning("verification pass failed; writing from the evidence as is", exc_info=True)
        return {}, []
    verdicts = [v for v in (data.get("verdicts") or []) if isinstance(v, dict)]
    threshold = float(settings.deep_research_min_confidence or 0.6)
    queries: List[str] = []
    low: List[int] = []
    for v in verdicts:
        try:
            i = int(v.get("subquestion") or 0)
            conf = float(v.get("confidence") if v.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            continue
        res = state.resolutions.get(i)
        model_low = conf < threshold or v.get("enough_evidence") is False
        code_low = bool(res) and res.confidence < threshold
        flagged = model_low or code_low or bool(v.get("sources_disagree"))
        if res is not None and v.get("primary_source_opened") is False and res.status != STATUS_UNKNOWN:
            flagged = True
        if flagged:
            low.append(i)
            for q in v.get("verification_queries") or []:
                if isinstance(q, str) and q.strip() and q not in state.queries_run and q not in queries:
                    queries.append(q.strip())
        _rlog(
            state,
            "verify [%d]: model confidence %.2f, code confidence %.2f, enough=%s, primary=%s, "
            "newer_likely=%s, disagree=%s, changed_over_time=%s → %s",
            i, conf, res.confidence if res else 0.0, v.get("enough_evidence"),
            v.get("primary_source_opened"), v.get("newer_source_likely"),
            v.get("sources_disagree"), v.get("changed_over_time"),
            "VERIFY" if flagged else "ok",
        )
    # Unresolved subquestions always deserve one more look, whatever the model said.
    for i in state.unresolved():
        if i not in low:
            low.append(i)
    if low and not queries:
        queries = _synthetic_followups(state, [subqs[i - 1] for i in low if 1 <= i <= len(subqs)])
    state.verification = {
        "verdicts": verdicts,
        "overall_confidence": data.get("overall_confidence"),
        "low_confidence": low,
    }
    return data, queries[: settings.deep_research_max_queries_per_iteration]


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


def validate_citations(report: str, source_count: int) -> Tuple[str, List[int]]:
    """Remove any [n] that does not resolve to a real gathered source.

    The search engine's only defence against a fabricated citation is a
    sentence in the prompt, and the frontend STRIPS [n] markers before
    rendering — so an invented [99] is invisible rather than caught. A report
    is a document people quote, so here the marker is checked against the
    registry. Returns the cleaned report and the invalid numbers found.

    TWO THINGS IT MUST NOT DO, both found by review before this shipped:

    * touch CODE. `arr[0]` inside a fenced block or an inline span is a
      subscript, not a citation, and deleting it silently corrupts a snippet
      the user will copy. Fenced blocks and inline spans are held out.
    * reflow the document. An earlier version collapsed every run of two or
      more spaces in the WHOLE report, which flattened YAML indentation,
      four-space code blocks and nested bullets — and it did so even when
      nothing had been removed. Only the gap left by a removed marker is
      tidied, and only there.
    """
    invalid: List[int] = []
    # Split on fenced blocks and inline code, keeping the delimiters, so the
    # substitution below only ever runs on prose segments.
    segments = re.split(r"(```.*?```|`[^`\n]*`)", report, flags=re.S)

    def _clean(prose: str) -> str:
        def _sub(m: re.Match) -> str:
            n = int(m.group(1))
            if 1 <= n <= source_count:
                return m.group(0)
            invalid.append(n)
            # The ONE space before the marker is part of the match, so
            # removing "[99]" from "a [99] b" yields "a b" without touching
            # any other whitespace in the document.
            return ""

        return _CITE_WITH_SPACE_RE.sub(_sub, prose)

    cleaned = "".join(
        seg if i % 2 else _clean(seg) for i, seg in enumerate(segments)
    )
    return cleaned, invalid


def cited_numbers(report: str, source_count: int) -> List[int]:
    """The valid citations a report actually uses, ignoring code."""
    segments = re.split(r"(```.*?```|`[^`\n]*`)", report, flags=re.S)
    prose = "".join(seg for i, seg in enumerate(segments) if i % 2 == 0)
    return sorted(
        {n for n in (int(m) for m in _CITE_RE.findall(prose)) if 1 <= n <= source_count}
    )


def _trim_evidence(sources: List[SourceRecord], question: str = "") -> None:
    """Full budget for the best sources, a query-centred excerpt for the tail.

    The same two-tier shape the search engine uses, so a 24-source run does
    not spend its whole prompt on the pages that ranked last. Mutates in
    place, like `search._apply_char_tiers`.

    THIS IS FINDING C1, one function further down the pipe. The tail cut used
    `extract.truncate_chars` — a pure head slice — so on a 24-source run
    sources 11-24 reached the report writer with their first 2,500 characters
    and nothing else. On `leaderboard_long.html` the answer row is at
    character 19,831 of 20,136: the page is fetched, cited in the panel, and
    handed to the model with the answer removed, and the report then says the
    fact is absent with a citation behind it.

    Worse after the search path was fixed, not better. `_fetch_sources` now
    returns 8,000 QUERY-CENTRED characters, and because passages are kept in
    document order the passage the question actually points at ends up LAST —
    measured at character 7,829 of 8,000 on that fixture. A head slice of a
    query-centred selection is therefore near-guaranteed to drop the one
    passage the selection existed to keep.

    `_select_text` is the search path's own selector: with a question it
    spends the same budget on the parts the question points at, and with no
    question it is the head slice, so a caller that passes only the list (the
    engine suite's own trim test) behaves exactly as before.
    """
    for s in sources:
        if s.n > _TIER_A_SOURCES:
            s.text = _select_text(s.text, question, _TIER_B_CHARS)


def _evidence_block(sources: Sequence[SourceRecord]) -> str:
    return "\n\n".join(
        f"[{s.n}] {s.title} ({s.url}; {s.label()})\n{s.text}" for s in sources
    )


def _report_messages(state: ResearchState, history: Sequence[dict]) -> List[dict]:
    recency = (
        "\nThis question is time-sensitive: prefer the most recent authoritative "
        "evidence, give dates where the sources give them, and say plainly when "
        "the sources may be out of date."
        if state.time_sensitive or _RECENCY_RE.search(state.question)
        else ""
    )
    gaps = (
        "\n\nEVIDENCE GAPS the auditor could not close — state these honestly "
        "in a short 'What this report could not establish' section:\n"
        + "\n".join(f"- {m}" for m in state.missing[:6])
        if state.missing
        else ""
    )
    conflicts = (
        "\n\nCONTRADICTIONS between sources — surface them rather than "
        "silently picking one side:\n"
        + "\n".join(f"- {c}" for c in state.contradictions[:4])
        if state.contradictions
        else ""
    )
    # A run whose search provider died mid-way read less of the web than it
    # planned to. Saying so is the difference between a thin report and a
    # thin report presented as a thorough one.
    outage = (
        "\n\nSEARCH COVERAGE: the search provider was unavailable for "
        f"{state.search_outages} of this run's query groups, so parts of the "
        "plan were never searched. Say so in the report's limitations rather "
        "than presenting the evidence base as complete."
        if state.search_outages
        else ""
    )
    # A run the clock cut short read less of the web than its plan called
    # for. Saying so is the difference between a thin report and a thin report
    # presented as a thorough one (R8, the same argument as the outage note).
    budget = (
        "\n\nTIME BUDGET: this run reached its wall-clock limit and stopped "
        "early — " + ", ".join(state.cut_short[:4]) + " did not finish. The "
        "evidence below is what could be gathered in the time available, not "
        "everything the plan called for: say so in the limitations rather "
        "than presenting the evidence base as complete."
        if state.cut_short
        else ""
    )
    audit = (
        "\n\nAUDIT: the evidence auditor could not be reached during this "
        "run, so nothing checked what is missing. Do not claim the coverage "
        "is complete; say the evidence was not audited."
        if state.auditor_failed
        else ""
    )
    system = (
        "You are writing a research report from numbered sources that were "
        "actually fetched and read. Rules:\n"
        "1. EVERY factual claim carries an inline citation like [1] or [3].\n"
        f"2. Only cite numbers 1 to {len(state.sources)} — these are the only "
        "sources that exist. Never invent a citation, a URL or a source.\n"
        "3. If the sources do not answer part of the question, say so "
        "explicitly instead of filling the gap from memory.\n"
        "4. Where sources disagree, present the disagreement and cite both.\n"
        "5. Structure the report with markdown headings, and lead with a "
        "short direct answer before the detail.\n"
        f"6. Current date: {state.today}. The sources were read from the live "
        "web and are newer than your training data: where a source "
        "contradicts what you remember, the source wins — write 'as of "
        "<date>' and cite it. Never let your own memory override newer web "
        "evidence.\n"
        "7. Use the EVIDENCE STATUS table. State CURRENT facts as current with "
        "their as-of date; present SUPERSEDED values as history ('previously "
        "X until Y', 'changed on Z'), not as errors; present CONFLICTING "
        "values as an open disagreement with both citations; for UNKNOWN say "
        "'not found in the sources consulted' and name what was searched. A "
        "value marked 'disputed by a weaker source' is an open disagreement "
        "whose other side is weaker — say that, never that it changed over "
        "time. A value marked 'NOT STATED VERBATIM' was not found in any "
        "source's own words: attribute it as what the sources imply, or leave "
        "it out; never state it as an established fact.\n"
        "8. A source marked 'same text as [n]' is a syndicated copy of [n]: "
        "it corroborates nothing on its own.\n"
        "9. Prefer primary sources (official, first-party, documentation, the "
        "paper itself) over reports about them when they disagree."
        + recency
    )
    user = (
        f"RESEARCH QUESTION:\n{state.question}\n\n"
        f"SUBQUESTIONS THE PLAN IDENTIFIED:\n"
        + "\n".join(f"- {s}" for s in state.subquestions)
        + f"\n\nEVIDENCE STATUS (resolved from the sources; dates are when each value held):\n{_resolution_table(state)}"
        + gaps
        + conflicts
        + outage
        + audit
        + budget
        + f"\n\nSOURCES:\n{_evidence_block(state.sources)}"
    )
    return [
        {"role": "system", "content": system},
        # `_conversation_turns`, NOT `recent_turns`: the latter keeps every
        # system message in the history — the cross-chat recall block, the
        # user's saved facts, a shared page's excerpt — and this report is
        # exported to PDF and shared. The planner learned this on its first
        # live run (it researched the signed-in user's own name); the report
        # writer was still being handed the same blocks until 2026-09-06.
        # Only what the user actually asked is research context.
        *_conversation_turns(history, 2),
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Persistence of what was learned
# ---------------------------------------------------------------------------

#: A stored quote is one sentence of a public page, clipped. 400 characters
#: holds the sentences the extractor works from (its own excerpts are 2,500
#: characters of a page, so a sentence longer than this is a run-on list, not
#: a statement) and keeps the Fast-mode claims block to one line per fact.
_QUOTE_MAX_CHARS = 400
#: Topic words stored beside the quote — the planner's entities that the
#: source itself mentions. A lexical hook for search_tsv, never a sentence.
_TOPIC_MAX_CHARS = 120
#: A value that is not stated verbatim may still match one sentence closely
#: (a diacritic the extractor dropped, a stray character). 0.85 is the
#: cut-off on difflib's ratio against the WHOLE sentence — "Francois Dupont"
#: against "François Dupont" scores 0.93; a different sentence never comes
#: close.
_FUZZY_MIN_RATIO = 0.85
#: The fuzzy path is closed to short values on purpose: one wrong character
#: in a short string is a different name or number, not a typo — "Person A"
#: against "Person B" scores 0.875, "version 3.2" against "version 3.3"
#: scores 0.91 — and a claim stored wrongly here is served to every user.
_FUZZY_MIN_CHARS = 24
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form of `text`, character
    for character: positions found in the folded string index the original
    (a precomposed accented letter folds to one letter; only ligatures and
    fractions drift by a character, which the quote clipping tolerates)."""
    import unicodedata  # local: the only user in this module, stdlib

    out: List[str] = []
    for ch in unicodedata.normalize("NFKD", text or ""):
        if unicodedata.combining(ch):
            continue
        out.append(ch.lower() if ch.isalnum() else " ")
    return "".join(out)


def _find_value(folded_text: str, value: str) -> Optional[Tuple[int, int]]:
    """Where `value` is stated in `folded_text` — as whole words, with any
    whitespace or punctuation between them — or None. Whole words, so that
    "Person A" is not found inside "Person Abbott"."""
    tokens = _fold(value).split()
    if not tokens:
        return None
    pattern = r"(?<!\w)" + r"\W+".join(re.escape(t) for t in tokens) + r"(?!\w)"
    m = re.search(pattern, folded_text)
    return (m.start(), m.end()) if m else None


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_BREAK_RE.finditer(text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _clip_quote(text: str, span: Tuple[int, int], hit: Tuple[int, int]) -> str:
    """The sentence at `span`, cut to _QUOTE_MAX_CHARS around `hit` when it
    is longer, whitespace collapsed. The value always stays inside the cut."""
    s, e = span
    if e - s > _QUOTE_MAX_CHARS:
        room = _QUOTE_MAX_CHARS - 2  # the two ellipsis marks
        a, b = hit
        start = max(s, min(a - (room - (b - a)) // 2, e - room))
        piece = text[start:start + room]
        piece = ("…" if start > s else "") + piece + ("…" if start + room < e else "")
    else:
        piece = text[s:e]
    return " ".join(piece.split())[:_QUOTE_MAX_CHARS]


#: (folded text, sentence spans) — what the matching needs from a page.
_Prepared = Tuple[str, List[Tuple[int, int]]]


def _prepare(text: str) -> _Prepared:
    """Fold a page once. `_fold` is a per-character Python loop (measured
    8 ms per 100k characters; a fuzzy miss then costs 85 ms across 1,300
    sentences), and one run checks every claim against every supporting
    source, so it is computed once per source, not once per pair."""
    return _fold(text), _sentence_spans(text)


def _quote_in(text: str, value: str, prepared: Optional[_Prepared] = None) -> str:
    """The sentence of `text` that states `value`, or "".

    Verbatim first (whole words, case/accent/punctuation folded); the match
    may straddle a sentence break ("Acme Inc. Ltd"), so the quote joins the
    sentences it touches. Then, for values long enough to make it safe, the
    one sentence that reads almost the same as the value."""
    value = " ".join((value or "").split())
    if not value or not text:
        return ""
    folded, spans = prepared or _prepare(text)
    if not spans:
        return ""
    hit = _find_value(folded, value)
    if hit is not None:
        a, b = hit
        covering = [sp for sp in spans if sp[0] < b and sp[1] > a] or [spans[0]]
        span = (covering[0][0], covering[-1][1])
        return _clip_quote(text, span, (max(a, span[0]), min(b, span[1])))
    folded_value = " ".join(_fold(value).split())
    if len(folded_value) < _FUZZY_MIN_CHARS:
        return ""
    import difflib  # local: the only user in this module, stdlib

    best, best_span = 0.0, None
    for sp in spans:
        candidate = " ".join(folded[sp[0]:sp[1]].split())
        if not candidate or abs(len(candidate) - len(folded_value)) > len(folded_value) // 2:
            continue  # a sentence half or twice the length cannot score 0.85
        ratio = difflib.SequenceMatcher(None, folded_value, candidate, autojunk=False).ratio()
        if ratio > best:
            best, best_span = ratio, sp
    if best_span is None or best < _FUZZY_MIN_RATIO:
        return ""
    return _clip_quote(text, best_span, best_span)


def _supporting_sources(state: ResearchState, numbers: Sequence[int]) -> List[SourceRecord]:
    """The canonical sources behind a value, best evidence first: a primary
    source, then authority, then the order they were opened in — so the
    quote stored is the one from the page a reader would trust most."""
    srcs = [s for s in (state.source(int(n)) for n in numbers if n) if s is not None]
    return sorted(srcs, key=lambda s: (not s.primary, -s.authority, s.n))


def _topic_words(state: ResearchState, folded_source: str) -> str:
    """The planner's entities that this source actually mentions (searched
    in its folded text), as a comma-separated hook for search_tsv. Never the
    question: an entity no public page names (a client, a colleague) has no
    business in a shared row, and the source's own words already carry the
    topic."""
    folded = folded_source
    picked: List[str] = []
    for entity in state.entities:
        words = " ".join(entity.split())
        if words and words not in picked and _find_value(folded, words) is not None:
            picked.append(words)
    out = ", ".join(picked)
    if len(out) > _TOPIC_MAX_CHARS:
        out = out[:_TOPIC_MAX_CHARS].rsplit(",", 1)[0].strip(", ")
    return out


async def _page_ids(urls: Sequence[str]) -> Dict[str, int]:
    """url_key → web_pages.id for the pages this run read. Best effort: the
    store write is a background task (`_persist_and_index`) that has usually
    landed by the time the report is out, and a claim with no page_id is
    still a claim — it just carries no domain/authority in the claims block."""
    keys = sorted({_normalize_url(u) for u in urls if u})
    if not keys:
        return {}
    try:
        pages = await db.run_in_thread(db.get_web_pages, keys)
    except Exception:  # noqa: BLE001 — the claim is worth keeping without the link
        log.debug("could not resolve research pages", exc_info=True)
        return {}
    return {str(p.get("url_key") or ""): int(p["id"]) for p in pages if p.get("id") is not None}


async def _persist_claims(state: ResearchState) -> None:
    """Resolved claims → web_claims, dated, for the knowledge layer.

    web_claims is SHARED: the Fast-mode grounding block shows a stored claim
    to every user who asks something with the same words. So a row may hold
    only what a public page states, never the asker's phrasing. Before
    2026-09-03 the stored claim was "<subquestion> <value>" — the planner
    writes the subquestion from the user's message and recent turns, so a
    question naming a client or a colleague would have been replayed to
    strangers — and page_id was always NULL. Now:

    * a row is written only when the value is found in a supporting source's
      text (`_quote_in`); the sentence that states it is the row's `quote`
      and its `claim`. A value no source states verbatim is dropped and
      logged — the report still cites it, the shared store does not repeat it;
    * `subquestion` holds only the planner's entity words the source
      mentions, never the sentence;
    * `page_id` is resolved through the same url_key `_persist_and_index`
      stores the page under;
    * `origin_user_id` / `origin_conversation_id` make the row attributable
      and purgeable with the conversation.
    """
    wanted: List[Tuple[str, str, Optional[date], float, List[int]]] = []
    for res in state.resolutions.values():
        if res.status not in (STATUS_CURRENT, STATUS_CONFLICTING):
            continue
        wanted.append((res.status, res.value, res.as_of, res.confidence, list(res.support)))
        for s in res.superseded[:3]:
            wanted.append(
                (
                    STATUS_SUPERSEDED,
                    str(s.get("value") or ""),
                    _parse_as_of(s.get("as_of")),
                    min(res.confidence, 0.6),
                    [int(n) for n in (s.get("sources") or []) if n],
                )
            )
    rows: List[dict] = []
    prepared: Dict[int, _Prepared] = {}
    for kind, value, as_of, confidence, support in wanted:
        value = " ".join((value or "").split())
        src, quote = None, ""
        for candidate in _supporting_sources(state, support):
            if candidate.n not in prepared:
                prepared[candidate.n] = _prepare(candidate.text)
            quote = _quote_in(candidate.text, value, prepared[candidate.n])
            if quote:
                src = candidate
                break
        if src is None:
            _rlog(
                state,
                "claim not persisted (%s): %r is not stated by %s",
                kind, value[:80], "".join(f"[{n}]" for n in support) or "any source",
            )
            continue
        rows.append(
            {
                "research_id": state.research_id,
                "page_id": None,
                "url": src.url,
                "subquestion": _topic_words(state, prepared[src.n][0]),
                "claim": quote,
                "quote": quote,
                "value": value,
                "as_of": as_of,
                "kind": kind,
                "confidence": confidence,
                "origin_user_id": state.user_id,
                "origin_conversation_id": state.conversation_id or None,
            }
        )
    if not rows:
        return
    ids = await _page_ids([r["url"] for r in rows])
    for r in rows:
        r["page_id"] = ids.get(_normalize_url(r["url"]))
    try:
        written = await db.run_in_thread(db.insert_web_claims, rows)
        _rlog(
            state, "persisted %d claim(s), %d linked to a stored page",
            written, sum(1 for r in rows if r["page_id"] is not None),
        )
    except Exception:  # noqa: BLE001 — the report already exists
        log.debug("could not persist research claims", exc_info=True)


async def _queue_primary_crawls(state: ResearchState) -> None:
    """The top primary domains this run found get a bounded background
    crawl, so the NEXT question about them answers from the corpus."""
    if not settings.deep_research_background_crawl:
        return
    try:
        from .crawl import enqueue_site_crawl
    except Exception:  # noqa: BLE001
        return
    seen: Set[str] = set()
    picked: List[SourceRecord] = []
    for s in sorted(state.primary_sources, key=lambda x: (-x.authority, x.n)):
        if s.domain_key in seen:
            continue
        seen.add(s.domain_key)
        picked.append(s)
        if len(picked) >= settings.deep_research_crawl_max_domains:
            break
    for s in picked:
        await enqueue_site_crawl(
            state.conversation_id,
            s.url,
            kind="research",
            max_pages=settings.deep_research_crawl_pages_per_domain,
            max_minutes=settings.web_share_crawl_max_minutes,
        )


# ---------------------------------------------------------------------------
# Engine entry point
# ---------------------------------------------------------------------------


async def _close_run(
    run_row: Optional[int],
    state: "ResearchState",
    status: str,
    report: str,
    sources_meta: List[dict],
    detail: str = "",
) -> None:
    """Close the research_runs row. Never raises — the report is what matters.

    Awaited rather than fire-and-forget: a spawned write can lose the race
    with process shutdown, and the row it leaves behind says 'running'.
    """
    if run_row is None:
        return
    try:
        await db.run_in_thread(
            db.finish_research_run,
            run_row,
            status,
            state.iterations,
            len(state.queries_run),
            len(state.sources),
            len(cited_numbers(report, len(state.sources))) if report else 0,
            report,
            sources_meta,
            detail,
        )
    except Exception:  # noqa: BLE001 — the answer already reached the user
        log.warning("could not close the research run", exc_info=True)


def _report_token_ceiling(effort: str) -> int:
    """How many completion tokens the report call may produce before the
    server cuts it off — mirroring the sizing `llm.stream_chat_events` does
    with the ceiling this module asks for.

    Deliberately reconstructed rather than assumed: the ledger's premise
    ("the report is capped at deep_research_report_max_tokens") only holds
    with thinking OFF. With thinking on and the default unbounded budget
    mode, `stream_chat_events` floors the request at MAX_OUTPUT_TOKENS
    (65,536), so 6,000 is not the wall — and comparing against 6,000 would
    report every long report as truncated.

    It is still only a lower bound on certainty: `context.fit_request` can
    LOWER the cap when the prompt crowds the window, and nothing in this
    process is told when it does. The honest fix is a `finish_reason` from
    the streaming layer; `llm.py` has no handling for one at all.
    """
    asked = int(settings.deep_research_report_max_tokens or 0)
    if not llm.wants_thinking("smart", effort):
        return asked
    budget = llm.thinking_budget(effort)
    if budget:
        return asked + int(budget)
    return max(asked, int(settings.max_output_tokens or 0))


# ---------------------------------------------------------------------------
# Admission control (R13) — who may run, how many at once, and what a refused
# request is told
# ---------------------------------------------------------------------------


class _Admission:
    """Which research runs may be in flight in this process right now.

    WHAT THE OLD GLOBAL LOCK GOT WRONG. `_RUN_LOCK` was one `asyncio.Lock` for
    the whole process, and a request that found it held was refused. So a
    single ten-minute run made every OTHER user's Deep Research request fail,
    org-wide, with nothing to do but retry blind. On a shared platform that is
    the wrong answer twice over: it is unfair (first arrival takes everything)
    and it is uninformative (the refusal could not say when to come back).

    WHAT IT WAS ACTUALLY PROTECTING — checked before removing it, because a
    lock is often load-bearing for something nobody wrote down:

      * NOT shared mutable state in this module. Everything at module level
        here is a constant, a compiled regex or a semaphore; all run state
        lives in the per-run `ResearchState`.
      * NOT the index writer. `web_index` serialises itself on its own
        `_index_lock`, and the write-behind path (`_persist_and_index`) is
        already re-entered concurrently by the ordinary Web Search path, which
        has never been serialised by anything.
      * NOT the politeness rules. `core/robots` keeps per-origin locks, the
        per-domain caps are per gather, and `core/net.safe_fetch` is untouched
        by any of this. What a second run DOES double is outbound volume —
        `_fetch_sources` bounds concurrency per call (16), so two runs can have
        32 fetches open — but that is the same pressure the ordinary Web Search
        path already puts on this box for two concurrent users, and it is the
        reason the ceiling is 2 rather than a number picked to look generous.
      * IT WAS, by accident, THE ONLY ADMISSION CONTROL ON THE PATH. Deep
        Research never passes through `engines.search.rate_ok` — `main.py`
        consults that for the web-search gate only — so nothing else caps how
        much of this box research may take. That property is deliberately KEPT
        below; what changes is that the cap is a budget, not a mutex.

    THE REAL CONSTRAINT IS MODEL TIME. `_LLM_SEM` already bounds concurrent
    generations at 2 for the whole process and every run shares it, so a
    second run does not add a third stream for interactive chat to queue
    behind — it interleaves inside the same two slots and mostly costs the
    first run some latency. That is why a ceiling above one is safe here while
    an unbounded one would not be.

    Concurrency safety: `try_admit` and `release` never await, so the check
    and the accounting cannot be interleaved by another task, and a released
    slot is HANDED to the first waiting request that may take it rather than
    waking every waiter to race for it.
    """

    def __init__(self) -> None:
        #: admission key -> the monotonic start time of each run it holds.
        self.running: Dict[str, List[float]] = {}
        #: Arrival-ordered queue of requests waiting for a slot.
        self.waiters: List[Tuple[str, "asyncio.Future"]] = []

    # -- limits, read per call so a settings change (or a test) takes effect
    @staticmethod
    def ceiling() -> int:
        return max(1, int(settings.deep_research_max_concurrent or 1))

    @classmethod
    def per_user(cls) -> int:
        # Never above the ceiling: a per-user allowance larger than the whole
        # machine is not an allowance, it is a rounding error waiting to be
        # read as one.
        return max(1, min(int(settings.deep_research_max_per_user or 1), cls.ceiling()))

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.running.values())

    def held_by(self, key: str) -> int:
        return len(self.running.get(key, ()))

    def try_admit(self, key: str) -> Optional[float]:
        """A slot's start time, or None when one is not available. Never
        awaits — see the class docstring."""
        if self.total >= self.ceiling():
            return None
        if self.held_by(key) >= self.per_user():
            return None
        started = time.monotonic()
        self.running.setdefault(key, []).append(started)
        return started

    def release(self, key: str, started: float) -> None:
        held = self.running.get(key)
        if held:
            try:
                held.remove(started)
            except ValueError:  # already released; releasing twice is not fatal
                pass
            if not held:
                self.running.pop(key, None)
        self._hand_over()

    def enqueue(self, key: str, fut: "asyncio.Future") -> None:
        self.waiters.append((key, fut))

    def dequeue(self, fut: "asyncio.Future") -> None:
        self.waiters = [w for w in self.waiters if w[1] is not fut]

    def _hand_over(self) -> None:
        """Give the freed slot(s) to the longest-waiting request that may have
        one. A waiter blocked by its OWN per-user limit is skipped rather than
        blocking the queue behind it — otherwise one user's second tab would
        stall everybody else's first."""
        i = 0
        while i < len(self.waiters) and self.total < self.ceiling():
            key, fut = self.waiters[i]
            if fut.done():  # timed out or cancelled between wake-ups
                self.waiters.pop(i)
                continue
            started = self.try_admit(key)
            if started is None:
                i += 1
                continue
            self.waiters.pop(i)
            fut.set_result(started)

    def frees_in_s(self, key: str = "") -> float:
        """How long until the earliest run in flight MUST be over, in seconds.

        Truthful only because R8 made `deep_research_timeout_s` a real bound
        rather than an advisory one: every expensive stage is now wrapped by
        what is left of it. The report still gets its floor after the gather
        budget is spent, so the ceiling on a whole run is the budget plus that
        floor. Returns 0.0 when nothing is running or no budget is configured
        — in which case the caller says nothing rather than guessing.
        """
        budget = float(settings.deep_research_timeout_s or 0.0)
        if budget <= 0:
            return 0.0
        starts = (
            list(self.running.get(key, ()))
            if key
            else [t for times in self.running.values() for t in times]
        )
        if not starts:
            return 0.0
        return max(0.0, min(starts) + budget + _REPORT_FLOOR_S - time.monotonic())


#: The admission state, and the loop it belongs to. `asyncio.Future`s are
#: loop-bound, and this module state outlives any individual loop (the app has
#: one for its lifetime; a test process has one per `asyncio.run`). Rebuilding
#: on a loop change is the same guard `compaction._lock_for` and `rerank._pool`
#: use, and it is correct rather than merely convenient: runs counted against a
#: dead loop are not running any more.
_ADMISSION: Optional[_Admission] = None
_ADMISSION_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _admission() -> _Admission:
    global _ADMISSION, _ADMISSION_LOOP

    loop = asyncio.get_running_loop()
    if _ADMISSION is None or _ADMISSION_LOOP is not loop:
        _ADMISSION = _Admission()
        _ADMISSION_LOOP = loop
    return _ADMISSION


def _admission_key(user_id: Optional[int]) -> str:
    """The fairness bucket. One per signed-in user.

    Everything reaching this engine carries a session (the enterprise auth
    retrofit put a cookie in front of every orchestrator route), so `None` is
    the CLI/test shape. It gets ONE shared bucket on purpose: a key the caller
    can mint for itself — a conversation id, say — is not a fairness key, it
    is a way around one.
    """
    return f"user:{user_id}" if user_id is not None else "anonymous"


def _about_minutes(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds < 90:
        return "under a minute"
    return f"about {round(seconds / 60)} minutes"


def _refusal_text(adm: _Admission, key: str, waited_s: float) -> str:
    """Why this request is not running, and when to come back.

    A bare "no" is what R13 is about. Everything in here is read from live
    accounting — never a guess, and never a promise the budget cannot keep.
    """
    mine = adm.held_by(key) >= adm.per_user()
    left = _about_minutes(adm.frees_in_s(key if mine else ""))
    if mine:
        allowed = adm.per_user()
        head = (
            f"Your own research run{'s are' if adm.held_by(key) != 1 else ' is'} "
            f"still going. Deep Research allows {allowed} "
            f"run{'s' if allowed != 1 else ''} at a time per person, so one long "
            f"run cannot take the whole machine."
        )
        when = f" It has {left} left." if left else ""
    else:
        total = adm.total
        head = (
            f"This machine is already running {total} research "
            f"run{'s' if total != 1 else ''}, which is its limit — Deep Research "
            f"reads and reasons for minutes at a time, and interactive chat "
            f"shares the same model."
        )
        if not left:
            when = ""
        elif total == 1:
            when = f" It finishes in {left}."
        else:
            when = f" The earliest finishes in {left}."
    waited = ""
    if waited_s >= 1:
        waited = f" I waited {round(waited_s)}s for a slot to come free."
    # "Try again then" needs a THEN. Without a configured budget there is no
    # honest one, and pointing at a time nobody promised is the bare refusal
    # this finding is about, dressed up.
    retry = "Try again then" if when else "Try again once it finishes"
    return (
        f"{head}{waited}{when} {retry}, or ask the same question with "
        f"Web Search for a quick cited answer now."
    )


def _handed_over(fut: "asyncio.Future") -> Optional[float]:
    """The slot this future was given, if it was given one. Never raises —
    `fut.exception()` on a cancelled future raises, which is exactly the state
    this is asked about."""
    if fut.done() and not fut.cancelled() and fut.exception() is None:
        return fut.result()
    return None


async def _acquire_slot(key: str, emit: Emit) -> Optional[float]:
    """A slot for this request, or None when it must be refused.

    Queues rather than refusing on sight, but only for
    `deep_research_queue_wait_s`: the user is watching a stream that has
    nothing to say while it waits, and a run holds its slot for minutes. When
    the wait runs out the caller gets a refusal that can quote real numbers.
    """
    adm = _admission()
    started = adm.try_admit(key)
    if started is not None:
        return started

    wait_s = max(0.0, float(settings.deep_research_queue_wait_s or 0.0))
    if wait_s <= 0:
        return None

    # Enqueued with NO await since `try_admit` failed, so a release that
    # happens in between cannot miss this request and leave it waiting out the
    # whole budget next to a free slot.
    fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
    adm.enqueue(key, fut)
    queued_at = time.monotonic()
    granted: Optional[float] = None
    try:
        await emit(
            "status",
            {"text": f"Deep Research is busy — waiting up to {round(wait_s)}s for a slot…"},
        )
        granted = await asyncio.wait_for(fut, wait_s)
    except (asyncio.TimeoutError, TimeoutError):
        # A slot handed over in the same tick the timeout fired is still a
        # slot; taking it here is what stops it leaking.
        granted = _handed_over(fut)
    except BaseException:
        # Cancelled (the client went away) — or the status emit itself failed,
        # which is an await and therefore a point where a slot can arrive.
        # Either way the slot must go back, or it is leaked until restart.
        handed = _handed_over(fut)
        if handed is not None:
            adm.release(key, handed)
        raise
    finally:
        adm.dequeue(fut)

    if granted is None:
        return None
    await emit(
        "status",
        {"text": f"A research slot came free after {round(time.monotonic() - queued_at)}s — starting."},
    )
    return granted


async def run_deep_research_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str = "think",
    conversation_id: str = "",
    user_id: Optional[int] = None,
) -> str:
    """Plan → search → open → extract → follow → assess → verify → report."""
    key = _admission_key(user_id)
    adm = _admission()
    queued_at = time.monotonic()
    started = await _acquire_slot(key, emit)
    if started is None:
        text = _refusal_text(adm, key, time.monotonic() - queued_at)
        log.info(
            "deep research refused: %d run(s) in flight, %d for this requester",
            adm.total, adm.held_by(key),
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "deep_research", "sources": []})
        return text

    try:
        return await _run(message, history, emit, effort, conversation_id, user_id)
    finally:
        adm.release(key, started)


def _sources_meta(state: ResearchState) -> List[dict]:
    return [
        {
            "n": s.n,
            "title": s.title,
            "url": s.url,
            "domain": s.domain,
            "published_at": s.published_at.date().isoformat() if s.published_at else "",
            "source_type": s.source_type,
            "primary": s.primary,
            "duplicate_of": s.dup_of,
            "found_via": s.discovered_via,
        }
        for s in state.sources
    ]


async def _run(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str,
    conversation_id: str,
    user_id: Optional[int],
) -> str:
    now = datetime.now(timezone.utc)
    state = ResearchState(
        research_id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        question=message,
        user_id=user_id,
        today=now.date().isoformat(),
        now_year=now.year,
    )
    # Offline on purpose: the router model is never consulted mid-run. The
    # level only weights recency and dates the prompts; the planner does
    # the real thinking about time with the main model.
    state.temporal = classify_offline(message, now_year=now.year)
    _rlog(
        state,
        "start: %r (temporal=%s via %s, today=%s, effort=%s)",
        message[:120], state.temporal.requirement.value, state.temporal.reason, state.today, effort,
    )
    run_row: Optional[int] = None
    # The INSERT that opens the run's record, held as a TASK rather than
    # awaited bare. `db.run_in_thread` is anyio's `to_thread.run_sync`:
    # cancelling the await raises CancelledError IMMEDIATELY and throws the
    # worker thread's result away, while the thread commits the row anyway.
    # So a Stop pressed in the first milliseconds of a run — which is exactly
    # when someone notices they picked the wrong mode — used to leave a
    # `research_runs` row at 'running' whose id nobody held. Nothing closes
    # it: the cancellation handler below needs `run_row`, and
    # `db.close_interrupted_research_runs` runs only at process START, so the
    # row claimed to be running until the next restart — inflating the admin
    # research analytics and making `frees_in_s` quote a dead run's budget to
    # the next person refused a slot. Keeping the task means the id survives
    # the cancellation and the row is closed like every other exit.
    creating: Optional["asyncio.Future"] = asyncio.ensure_future(
        db.run_in_thread(
            db.create_research_run, conversation_id, user_id, message, state.research_id
        )
    )

    async def run_row_id() -> Optional[int]:
        """The run's row id, waiting out the INSERT if it is still in flight.

        Shielded, so a cancellation arriving mid-INSERT does not abandon the
        row; idempotent, so the cancellation handler can ask again for an id
        the main path never got to see. Never raises anything but the
        cancellation itself.
        """
        nonlocal run_row, creating
        if run_row is None and creating is not None:
            try:
                run_row = int(await asyncio.shield(creating))
            except Exception:  # noqa: BLE001 — the report matters, the record does not
                log.warning("could not record the research run", exc_info=True)
                creating = None
        return run_row

    # The step currently in flight, so a failure can close it instead of
    # leaving a spinner running in the timeline forever.
    step_id = 0
    open_step: Optional[Tuple[int, str]] = None
    parts: List[str] = []

    async def step(title: str) -> int:
        nonlocal step_id, open_step
        step_id += 1
        open_step = (step_id, title)
        await emit("step", {"id": step_id, "title": title, "status": "running"})
        return step_id

    async def finish(sid: int, title: str, detail: str = "") -> None:
        nonlocal open_step
        open_step = None
        await emit(
            "step",
            {"id": sid, "title": title, "status": "done", "detail": detail},
        )

    def round_detail(added: List[SourceRecord]) -> str:
        stats = state.rounds[-1] if state.rounds else None
        bits = [f"{len(added)} new source(s); {len(state.sources)} total"]
        if stats:
            if stats.links_followed:
                bits.append(f"{stats.links_followed} link(s) followed")
            if stats.duplicates:
                bits.append(f"{stats.duplicates} duplicate(s)")
            if stats.new_claims:
                bits.append(f"{stats.new_claims} claim(s) extracted")
            if stats.search_outages:
                bits.append(f"{stats.search_outages} search outage(s) — some queries never ran")
        primaries = len(state.primary_sources)
        if primaries:
            bits.append(f"{primaries} primary")
        return "; ".join(bits)

    try:
        # Inside the guarded region on purpose: the id is what lets every exit
        # below — including the cancellation handler — CLOSE the row.
        run_row = await run_row_id()

        # --- Plan ------------------------------------------------------
        await emit("status", {"text": "Planning the research…"})
        sid = await step("Planning the research")
        ok, planned = await _bounded(
            state, "planning", _plan(message, history, effort, state)
        )
        if ok and planned:
            state.subquestions, queries = planned
        else:
            # A wedged planner must not eat the run: research the question as
            # it was asked rather than waiting out `llm.py`'s 1,800 s guard.
            state.subquestions, queries = [], [message]
        _rlog(state, "plan: %d subquestion(s), %d quer(y/ies), entities=%s",
              len(state.subquestions), len(queries), state.entities)
        await finish(
            sid,
            "Planned the research",
            "\n".join(f"- {s}" for s in state.subquestions) or "no subquestions",
        )

        # --- Iterate ---------------------------------------------------
        while True:
            state.iterations += 1
            label = (
                "Searching the web"
                if state.iterations == 1
                else f"Following up on gaps (round {state.iterations})"
            )
            await emit("status", {"text": f"{label} — {len(queries)} queries…"})
            sid = await step(label)
            added, cut = await _gather_bounded(
                state, queries, effort, emit,
                "search" if state.iterations == 1 else "follow-up",
            )
            detail = round_detail(added)
            if cut:
                detail += " · stopped by the run's time budget"
            await finish(sid, label, detail)

            if not state.budget_left():
                state.stop_reason = state.budget_reason() or "budget"
                break
            if len(state.sources) < settings.deep_research_min_sources and added:
                # Too thin to judge — search the plan's remaining angles first.
                queries = [s for s in state.subquestions if s not in state.queries_run][
                    : settings.deep_research_max_queries_per_iteration
                ]
                if queries:
                    continue

            await emit("status", {"text": "Checking what is still missing…"})
            sid = await step("Analyzing evidence")
            ok, verdict = await _bounded(state, "evidence audit", _assess(state, effort))
            if not ok:
                # The audit never finished, so this run was NOT audited — the
                # same honest state an auditor outage produces (R2), reached
                # by the clock instead of by an error.
                state.auditor_failed = True
                await finish(
                    sid,
                    "Analyzed evidence",
                    "the run reached its time budget before the audit "
                    "finished — the evidence was NOT audited",
                )
                state.stop_reason = "timeout"
                break
            # ACCUMULATED, not reassigned: what round 2 could not establish is
            # still unestablished in round 4 unless something found it.
            round_missing = [m for m in (verdict.get("missing") or []) if isinstance(m, str)]
            state.missing = _accumulate(state.missing, round_missing)
            state.contradictions = _accumulate(
                state.contradictions, verdict.get("contradictions")
            )
            followups = _followup_queries(state, verdict)
            unresolved = [state.resolutions[i].question for i in state.unresolved()]
            if verdict.get("auditor_failed"):
                detail = "the evidence auditor could not be reached — the evidence was NOT audited"
            elif round_missing:
                detail = "Gaps: " + "; ".join(round_missing[:4])
            else:
                detail = "no gaps found"
            if unresolved:
                detail += " · not found yet: " + "; ".join(unresolved[:3])
            await finish(sid, "Analyzed evidence", detail)

            stop = _should_stop(state, verdict, followups)
            _rlog(
                state,
                "assess: sufficient=%s missing=%d unresolved=%d followups=%d → %s",
                verdict.get("sufficient"), len(state.missing), len(unresolved), len(followups),
                stop or "continue",
            )
            if stop:
                state.stop_reason = stop
                break
            queries = followups

        if not state.sources:
            # Close the row. Every other exit does, and a run left at
            # 'running' forever is exactly the bug the crawler had before its
            # own review round — a dead SearXNG reaches here NORMALLY (the
            # search errors are swallowed per round), never via the exception
            # handler below.
            timed_out = bool(state.cut_short)
            await _close_run(
                run_row, state, "failed", "", [],
                "the time budget ended the run before any source was read"
                if timed_out
                else "no readable sources",
            )
            text = (
                "I could not gather any readable sources for this question — "
                + (
                    "the run reached its time budget before the first round "
                    "finished. Nothing was invented to fill the gap. Try "
                    "again, or give research a longer time budget."
                    if timed_out
                    else "the search provider returned nothing usable. Nothing "
                    "was invented to fill the gap. Try rephrasing, or ask with "
                    "Web Search for a single-pass answer."
                )
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "deep_research", "sources": []})
            return text

        # --- Verify (self-correction) ----------------------------------
        # NOT gated on budget_left(). It was, and that made the pass dead
        # exactly where it mattered: budget_left() is False precisely when the
        # loop ended on source_cap / timeout / iteration_cap — the thin,
        # over-run, low-confidence runs. Measured 2026-09-04, runs 24, 25 and
        # 26 all ended on source_cap and none produced a single verify line,
        # while the two runs that stopped `sufficient` — the ones that least
        # needed checking — were the only ones verified.
        #
        # The pass itself is cheap: one ~900-token call over claims already in
        # memory, no fetching. The FOLLOW-UP round it can request is the
        # expensive part, and that is still gated on budget below.
        if settings.deep_research_verify:
            await emit("status", {"text": "Cross-checking the important claims…"})
            sid = await step("Verifying claims")
            # Bounded, and SKIPPED outright when the budget is already spent.
            # The pass is cheap, but "cheap" is not "free" when the run is
            # already over its limit and holding a research slot somebody else
            # is queued for; an honest step detail beats another minute of
            # somebody else's turn.
            ok, vres = await _bounded(state, "verification", _verify(state, effort))
            if ok:
                _verdict, vqueries = vres
                low = list((state.verification or {}).get("low_confidence") or [])
            else:
                await finish(
                    sid, "Verifying claims", "not run: the run reached its time budget"
                )
                vqueries, low = [], []
            if ok and low and vqueries and state.budget_left():
                await finish(sid, "Verifying claims", f"{len(low)} claim(s) need more evidence — one more targeted round")
                state.iterations += 1
                state.verification_rounds += 1
                label = f"Verifying claims (round {state.iterations})"
                await emit("status", {"text": f"{label} — {len(vqueries)} queries…"})
                sid = await step(label)
                added, cut = await _gather_bounded(state, vqueries, effort, emit, "verify")
                detail = round_detail(added)
                if cut:
                    detail += " · stopped by the run's time budget"
                await finish(sid, label, detail)
                _rlog(state, "verification round: %d new source(s)", len(added))
            elif ok:
                await finish(
                    sid,
                    "Verified claims",
                    f"confidence {state.confidence:.2f}" + (f"; {len(low)} flagged, no further queries" if low else ""),
                )
        if not state.stop_reason:
            state.stop_reason = "sufficient"

        # --- Report ----------------------------------------------------
        await emit(
            "status",
            {
                "text": (
                    f"Time budget reached — writing the report from "
                    f"{len(state.sources)} sources…"
                    if state.cut_short
                    else f"Writing the report from {len(state.sources)} sources…"
                )
            },
        )
        sid = await step("Writing the report")
        # Trim the tail before building the prompt. _apply_char_tiers mutates
        # the objects it is handed, so it MUST be given the records the report
        # is actually built from — handing it throwaway copies quietly trimmed
        # nothing at all.
        # WITH the question: the tail cut is query-centred, not a head slice
        # that drops the answer row off the bottom of every page after the
        # tenth (C1). `_fetch_sources` already selected 8,000 characters
        # around the question, and those passages sit in DOCUMENT order, so a
        # head slice of them lands squarely on the introduction.
        _trim_evidence(state.sources, state.question)
        # The report STREAMS. Buffering it to validate citations first made a
        # measured 6,349-character report land in one lump after ~40 s of
        # nothing but a thinking indicator — the worst-looking part of an
        # otherwise good run. Validation still happens below, on the text that
        # is returned and stored; a stray marker that survives on the client
        # is already invisible there, because the frontend strips every [n]
        # before rendering and draws the source list from meta.sources.
        # Truncation was silent: a report cut off at its token ceiling reached
        # the user, the store and the PDF looking finished. There is no
        # finish_reason anywhere in `llm.py`, so this reads the usage the
        # streaming layer already captures (`llm.get_usage`, a per-request
        # ContextVar) and compares the completion this ONE call spent against
        # the ceiling it was given. No usage reported (a runtime that refuses
        # stream_options) means NOT MEASURED — never "fine".
        ceiling = _report_token_ceiling(effort)
        spent_before = int((llm.get_usage() or {}).get("completion_tokens", 0) or 0)
        async def _stream_report() -> None:
            async with _LLM_SEM:
                async for kind, delta in llm.stream_chat_events(
                    _report_messages(state, history),
                    effort=llm.normalize_effort(effort),
                    max_tokens=settings.deep_research_report_max_tokens,
                ):
                    await emit(kind, {"text": delta})
                    if kind == "token":
                        parts.append(delta)

        # The one stage the budget may not simply skip — it is the deliverable
        # — but it may not run unbounded either: the only guard underneath it
        # is `llm.py`'s GEN_WALL_CLOCK_S, 1,800 s by default, three times a
        # whole research run (R8). `parts` is appended as each delta arrives,
        # so a stream stopped here keeps every word the user has already seen.
        try:
            await asyncio.wait_for(_stream_report(), state.report_budget_s())
        except asyncio.TimeoutError:
            state.report_cut_short = True
            state.cut_short.append("report")
            log.warning(
                "research report stopped at the run's time budget after "
                "%.1fs (%d characters written)",
                state.elapsed, sum(len(p) for p in parts),
            )
        report = "".join(parts)
        if state.report_cut_short:
            note = (
                "\n\n[the run reached its time budget and the report stops "
                "here — the sources above are complete]"
            )
            await emit("token", {"text": note})
            report += note
        produced = int((llm.get_usage() or {}).get("completion_tokens", 0) or 0) - spent_before
        if ceiling > 0 and produced >= ceiling:
            state.report_truncated = True
            log.warning(
                "research report hit its token ceiling (%d of %d tokens, %d sources): "
                "the report is cut off",
                produced, ceiling, len(state.sources),
            )
            note = (
                "\n\n[the report reached its length limit and stops here — "
                "the sources above are complete; ask about one section for "
                "the rest]"
            )
            await emit("token", {"text": note})
            report += note

        # Citation integrity: a marker that resolves to nothing is removed.
        report, invalid = validate_citations(report, len(state.sources))
        if invalid:
            log.warning(
                "research report cited %d non-existent source(s): %s",
                len(invalid),
                sorted(set(invalid))[:10],
            )
        cited = cited_numbers(report, len(state.sources))
        await finish(
            sid,
            "Wrote the report",
            f"{len(cited)} of {len(state.sources)} sources cited · stopped: {state.stop_reason.replace('_', ' ')} · confidence {state.confidence:.2f}",
        )

        sources_meta = _sources_meta(state)
        resolutions_meta = [state.resolutions[i].as_meta() for i in sorted(state.resolutions)]
        await emit(
            "meta",
            {
                "route": "deep_research",
                "sources": sources_meta,
                "research_run": {
                    "research_id": state.research_id,
                    "iterations": state.iterations,
                    "queries": state.queries_run,
                    "subquestions": state.subquestions,
                    "sources_found": len(state.sources),
                    "sources_cited": len(cited),
                    "missing": state.missing,
                    "contradictions": state.contradictions,
                    "elapsed_s": round(state.elapsed, 1),
                    "invalid_citations_removed": len(invalid),
                    # 2026-09-03: why it stopped and what it established.
                    "stop_reason": state.stop_reason,
                    "today": state.today,
                    "temporal": state.temporal.requirement.value if state.temporal else "",
                    "rounds": [r.as_meta() for r in state.rounds],
                    "links_followed": state.links_followed,
                    "primary_sources": [s.n for s in state.primary_sources],
                    "duplicates_dropped": len(state.duplicates),
                    "stale_downranked": len(state.stale_downranked),
                    "claims": len(state.claims),
                    "resolutions": resolutions_meta,
                    "confidence": round(state.confidence, 2),
                    "verification_rounds": state.verification_rounds,
                    # What the run could NOT do, beside what it did.
                    "search_outages": state.search_outages,
                    "evidence_audited": not state.auditor_failed,
                    "report_truncated": state.report_truncated,
                    # R8: the wall-clock budget BOUNDS the run now instead of
                    # advising it, so what it cut belongs on the record.
                    "time_budget_s": round(float(settings.deep_research_timeout_s or 0.0), 1),
                    "stages_cut_short": list(state.cut_short),
                    "report_cut_short": state.report_cut_short,
                },
            },
        )
        await _close_run(run_row, state, "done", report, sources_meta)
        await _persist_claims(state)
        _spawn(_queue_primary_crawls(state))
        _rlog(
            state,
            "done: iterations=%d queries=%d sources=%d (%d distinct, %d primary, %d duplicates, "
            "%d via links) claims=%d cited=%d invalid_citations=%d confidence=%.2f stop=%s elapsed=%.1fs "
            "budget=%.0fs cut_short=%s",
            state.iterations, len(state.queries_run), len(state.sources),
            len(state.canonical_sources), len(state.primary_sources), len(state.duplicates),
            state.links_followed, len(state.claims), len(cited), len(invalid),
            state.confidence, state.stop_reason, state.elapsed,
            float(settings.deep_research_timeout_s or 0.0), state.cut_short or "none",
        )
        return report

    except asyncio.CancelledError:
        # A closed tab cancels this coroutine; shield the write so the row
        # does not stay 'running' forever with site-QA-style consequences.
        #
        # With the partial report and the sources, exactly like the failure
        # path below: cancelling at minute 9 of a 10-minute run used to store
        # report "" and sources [], so the record said 0 sources and no
        # report while 30 fetched pages and every resolved claim were sitting
        # in `state`. The pages themselves stay in the shared store either
        # way; the RECORD of the run is what was being destroyed.
        try:
            await asyncio.shield(
                _close_run(
                    await run_row_id(), state, "cancelled", "".join(parts),
                    _sources_meta(state), "cancelled mid-run",
                )
            )
        except Exception:  # noqa: BLE001 — cancellation still wins
            log.warning("could not mark the cancelled research run", exc_info=True)
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("deep research failed", exc_info=True)
        # Whatever was gathered is real and worth showing: a failure part-way
        # through the report used to emit sources: [] and store report: "",
        # discarding 20-odd pages the run had already read and paid for.
        sources_meta = _sources_meta(state)
        partial = "".join(parts)
        await _close_run(
            await run_row_id(), state, "failed", partial, sources_meta, str(exc)[:300]
        )
        # The step that was in flight must not be left spinning in the UI.
        if open_step is not None:
            await emit(
                "step",
                {
                    "id": open_step[0],
                    "title": open_step[1],
                    "status": "failed",
                    "detail": str(exc)[:200],
                },
            )
        text = (
            f"The research run failed ({exc}). "
            + (
                f"The {len(state.sources)} source(s) it had already read are "
                "listed below and stay in the web store, so asking again is "
                "cheaper than the first time."
                if state.sources
                else "Sources already gathered stay in the web store."
            )
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "deep_research", "sources": sources_meta})
        return text
