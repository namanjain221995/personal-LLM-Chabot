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
from ..core import extract, provenance
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
_LLM_SEM = asyncio.Semaphore(2)

#: One research run at a time per orchestrator process. A second run would
#: double every budget below against the same SearXNG and the same GPU; the
#: user gets a plain answer that says so rather than a starved one.
_RUN_LOCK = asyncio.Lock()

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
    #: [{value, as_of, sources}] — comparable-date disagreements.
    conflicts: List[dict] = field(default_factory=list)
    confidence: float = 0.0

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
            tail += (
                f' · conflicting: "{c["value"]}" ('
                f"{('as of ' + c['as_of']) if c.get('as_of') else 'date not stated'}; "
                + "".join(f"[{n}]" for n in c.get("sources", []))
                + ")"
            )
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

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def budget_left(self) -> bool:
        return (
            self.iterations < settings.deep_research_max_iterations
            and len(self.sources) < settings.deep_research_max_sources
            and self.elapsed < settings.deep_research_timeout_s
        )

    def budget_reason(self) -> str:
        if self.iterations >= settings.deep_research_max_iterations:
            return "iteration_cap"
        if len(self.sources) >= settings.deep_research_max_sources:
            return "source_cap"
        if self.elapsed >= settings.deep_research_timeout_s:
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
        excerpt = extract.truncate_chars(" ".join((s.text or "").split()), 2500)
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
        groups: Dict[str, List[Claim]] = {}
        for c in claims:
            key = _norm_value(c.value) or _norm_value(c.text)[:80]
            groups.setdefault(key, []).append(c)

        scored: List[dict] = []
        for key, cs in groups.items():
            srcs = [state.source(c.source_n) for c in cs]
            srcs = [s for s in srcs if s is not None]
            canonical = sorted({s.canonical_n for s in srcs})
            domains = {s.domain_key for s in srcs if s.dup_of is None}
            auth = max((s.authority for s in srcs), default=40)
            primary = any(s.primary for s in srcs)
            times = [t for t in (_claim_time(state, c) for c in cs) if t]
            when = max(times) if times else None
            historical = bool(cs) and all(c.hint == "historical" for c in cs)
            scored.append(
                {
                    "key": key, "claims": cs, "support": canonical,
                    "independent": max(1, len(domains)) if srcs else 0,
                    "authority": auth, "primary": primary, "when": when,
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
                superseded.append(entry)  # a weak, undated outlier

        status = STATUS_CONFLICTING if conflicts else STATUS_CURRENT
        confidence = 0.3
        confidence += 0.25 if winner["independent"] >= 2 else 0.0
        confidence += 0.2 if winner["authority"] >= 70 else (0.05 if winner["authority"] >= 40 else 0.0)
        confidence += 0.1 if winner["primary"] else 0.0
        if winner["when"] and state.time_sensitive:
            age_days = (today - winner["when"]).days
            confidence += 0.1 if age_days * 86400 <= max_age else -0.05
        elif not state.time_sensitive:
            confidence += 0.05
        confidence -= 0.25 if conflicts else 0.0
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
        f"[{s.n}] {s.title} ({s.domain_key}; {s.label()}) — {extract.truncate_chars(' '.join((s.text or '').split()), 1000)}"
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
        log.warning("gap analysis failed; treating evidence as sufficient", exc_info=True)
        return {"sufficient": True, "missing": [], "followup_queries": []}
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
        f"[{s.n}] {s.title} ({s.domain_key}; {s.label()}) — {extract.truncate_chars(' '.join((s.text or '').split()), 700)}"
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


def _trim_evidence(sources: List[SourceRecord]) -> None:
    """Full budget for the best sources, a summary excerpt for the long tail.

    The same two-tier shape the search engine uses, so a 24-source run does
    not spend its whole prompt on the pages that ranked last. Mutates in
    place, like `search._apply_char_tiers`.
    """
    for s in sources:
        if s.n > _TIER_A_SOURCES:
            s.text = extract.truncate_chars(s.text, _TIER_B_CHARS)


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
        "'not found in the sources consulted' and name what was searched.\n"
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
        + f"\n\nSOURCES:\n{_evidence_block(state.sources)}"
    )
    return [
        {"role": "system", "content": system},
        *recent_turns(history, 2),
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Persistence of what was learned
# ---------------------------------------------------------------------------


async def _persist_claims(state: ResearchState) -> None:
    """Resolved claims → web_claims, dated, for the knowledge layer."""
    rows: List[dict] = []
    for res in state.resolutions.values():
        if res.status not in (STATUS_CURRENT, STATUS_CONFLICTING):
            continue
        src = state.source(res.support[0]) if res.support else None
        rows.append(
            {
                "research_id": state.research_id,
                "page_id": None,
                "url": src.url if src else "",
                "subquestion": res.question,
                "claim": f"{res.question} {res.value}".strip() if res.value else res.question,
                "value": res.value,
                "as_of": res.as_of,
                "kind": res.status,
                "confidence": res.confidence,
            }
        )
        for s in res.superseded[:3]:
            rows.append(
                {
                    "research_id": state.research_id,
                    "page_id": None,
                    "url": "",
                    "subquestion": res.question,
                    "claim": f"{res.question} {s.get('value', '')}".strip(),
                    "value": s.get("value", ""),
                    "as_of": _parse_as_of(s.get("as_of")),
                    "kind": STATUS_SUPERSEDED,
                    "confidence": min(res.confidence, 0.6),
                }
            )
    if not rows:
        return
    try:
        written = await db.run_in_thread(db.insert_web_claims, rows)
        _rlog(state, "persisted %d claim(s)", written)
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


async def run_deep_research_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str = "think",
    conversation_id: str = "",
    user_id: Optional[int] = None,
) -> str:
    """Plan → search → open → extract → follow → assess → verify → report."""
    if _RUN_LOCK.locked():
        text = (
            "A research run is already in progress on this machine. Deep "
            "Research uses the whole search and GPU budget, so it runs one at "
            "a time — try again when the current one finishes, or ask with "
            "Web Search for a quick cited answer instead."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "deep_research", "sources": []})
        return text

    async with _RUN_LOCK:
        return await _run(message, history, emit, effort, conversation_id, user_id)


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
    try:
        run_row = await db.run_in_thread(
            db.create_research_run, conversation_id, user_id, message, state.research_id
        )
    except Exception:  # noqa: BLE001 — the report matters, the record does not
        log.warning("could not record the research run", exc_info=True)

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
        primaries = len(state.primary_sources)
        if primaries:
            bits.append(f"{primaries} primary")
        return "; ".join(bits)

    try:
        # --- Plan ------------------------------------------------------
        await emit("status", {"text": "Planning the research…"})
        sid = await step("Planning the research")
        state.subquestions, queries = await _plan(message, history, effort, state)
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
            added = await _gather(state, queries, effort, emit, label="search" if state.iterations == 1 else "follow-up")
            await finish(sid, label, round_detail(added))

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
            verdict = await _assess(state, effort)
            state.missing = [m for m in (verdict.get("missing") or []) if isinstance(m, str)]
            state.contradictions = [
                c for c in (verdict.get("contradictions") or []) if isinstance(c, str)
            ]
            followups = _followup_queries(state, verdict)
            unresolved = [state.resolutions[i].question for i in state.unresolved()]
            detail = ("Gaps: " + "; ".join(state.missing[:4])) if state.missing else "no gaps found"
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
            await _close_run(run_row, state, "failed", "", [], "no readable sources")
            text = (
                "I could not gather any readable sources for this question — "
                "the search provider returned nothing usable. Nothing was "
                "invented to fill the gap. Try rephrasing, or ask with Web "
                "Search for a single-pass answer."
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "deep_research", "sources": []})
            return text

        # --- Verify (self-correction) ----------------------------------
        if settings.deep_research_verify and state.budget_left():
            await emit("status", {"text": "Cross-checking the important claims…"})
            sid = await step("Verifying claims")
            _verdict, vqueries = await _verify(state, effort)
            low = list((state.verification or {}).get("low_confidence") or [])
            if low and vqueries and state.budget_left():
                await finish(sid, "Verifying claims", f"{len(low)} claim(s) need more evidence — one more targeted round")
                state.iterations += 1
                state.verification_rounds += 1
                label = f"Verifying claims (round {state.iterations})"
                await emit("status", {"text": f"{label} — {len(vqueries)} queries…"})
                sid = await step(label)
                added = await _gather(state, vqueries, effort, emit, label="verify")
                await finish(sid, label, round_detail(added))
                _rlog(state, "verification round: %d new source(s)", len(added))
            else:
                await finish(
                    sid,
                    "Verified claims",
                    f"confidence {state.confidence:.2f}" + (f"; {len(low)} flagged, no further queries" if low else ""),
                )
        if not state.stop_reason:
            state.stop_reason = "sufficient"

        # --- Report ----------------------------------------------------
        await emit("status", {"text": f"Writing the report from {len(state.sources)} sources…"})
        sid = await step("Writing the report")
        # Trim the tail before building the prompt. _apply_char_tiers mutates
        # the objects it is handed, so it MUST be given the records the report
        # is actually built from — handing it throwaway copies quietly trimmed
        # nothing at all.
        _trim_evidence(state.sources)
        # The report STREAMS. Buffering it to validate citations first made a
        # measured 6,349-character report land in one lump after ~40 s of
        # nothing but a thinking indicator — the worst-looking part of an
        # otherwise good run. Validation still happens below, on the text that
        # is returned and stored; a stray marker that survives on the client
        # is already invisible there, because the frontend strips every [n]
        # before rendering and draws the source list from meta.sources.
        async with _LLM_SEM:
            async for kind, delta in llm.stream_chat_events(
                _report_messages(state, history),
                effort=llm.normalize_effort(effort),
                max_tokens=settings.deep_research_report_max_tokens,
            ):
                await emit(kind, {"text": delta})
                if kind == "token":
                    parts.append(delta)
        report = "".join(parts)

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
                },
            },
        )
        await _close_run(run_row, state, "done", report, sources_meta)
        await _persist_claims(state)
        _spawn(_queue_primary_crawls(state))
        _rlog(
            state,
            "done: iterations=%d queries=%d sources=%d (%d distinct, %d primary, %d duplicates, "
            "%d via links) claims=%d cited=%d invalid_citations=%d confidence=%.2f stop=%s elapsed=%.1fs",
            state.iterations, len(state.queries_run), len(state.sources),
            len(state.canonical_sources), len(state.primary_sources), len(state.duplicates),
            state.links_followed, len(state.claims), len(cited), len(invalid),
            state.confidence, state.stop_reason, state.elapsed,
        )
        return report

    except asyncio.CancelledError:
        # A closed tab cancels this coroutine; shield the write so the row
        # does not stay 'running' forever with site-QA-style consequences.
        try:
            await asyncio.shield(
                _close_run(run_row, state, "cancelled", "", [], "cancelled mid-run")
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
            run_row, state, "failed", partial, sources_meta, str(exc)[:300]
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
