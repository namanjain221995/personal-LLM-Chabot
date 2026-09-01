"""The living knowledge layer: source-backed evidence, ranked by more than similarity.

WHAT THIS FIXES. On 2026-08-31 the platform answered "who's vice president of
india" from a web search correctly (C. P. Radhakrishnan), stored 19 pages
saying so — and then, in a new chat with search off, answered "Jagdeep
Dhankhar" from frozen weights. The evidence was on disk, indexed, and
retrievable the whole time; nothing ever asked for it, and nothing could have
told the two answers apart if it had: `web_index.retrieve` ranks by embedding
distance alone, and the corpus holds 10 pages naming the previous holder.

So this module adds the two things distance cannot give:

  REACH      one call any engine can make, not something buried in the search
             engine and reachable only when a live search runs.
  JUDGEMENT  a score built from lexical overlap, source authority, and age as
             well as similarity — plus explicit detection of the case where
             stored sources disagree because one of them is simply old.

It is READ-MOSTLY and never raises: retrieval is an enhancement to an answer,
never a precondition for one. Every failure path returns "no evidence" and the
caller proceeds exactly as it does today.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import db, web_index
from .config import settings
from .freshness import Freshness

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source authority
#
# Deliberately a handful of general heuristics, not a curated domain list: a
# hand-maintained allowlist of "good websites" is a maintenance burden that is
# always out of date and always wrong for somebody's country. Suffix rules
# generalise — .gov.in, .gov.uk and .go.jp are all official without anyone
# adding them — and the small explicit set below covers reference sites whose
# suffix says nothing useful about them.
# ---------------------------------------------------------------------------

_OFFICIAL_SUFFIX = (".gov", ".gov.in", ".gov.uk", ".gov.au", ".go.jp", ".govt.nz",
                    ".gc.ca", ".europa.eu", ".int", ".mil", ".nic.in")
_ACADEMIC_SUFFIX = (".edu", ".ac.uk", ".edu.au", ".ac.in", ".edu.cn")

#: Reference and first-party documentation. Mid-high, below official primary
#: sources: excellent for stable facts, occasionally behind on breaking ones.
_REFERENCE = {
    "wikipedia.org", "britannica.com", "reuters.com", "apnews.com",
    "bbc.co.uk", "bbc.com", "nature.com", "science.org", "arxiv.org",
    "python.org", "docs.python.org", "developer.mozilla.org", "kernel.org",
    "ubuntu.com", "debian.org", "postgresql.org", "docker.com",
    "nvidia.com", "salesforce.com", "microsoft.com", "apple.com", "google.com",
}

#: Shapes that correlate with rewritten, undated, second-hand content.
_LOW_QUALITY = re.compile(
    r"(blogspot|wordpress\.com|medium\.com|quora|answers\.|"
    r"examhub|examsdaily|study ?iq|affairscloud|jagranjosh|adda247|"
    r"\.blog$|/blog/|seo|listicle)",
    re.I,
)

AUTHORITY_OFFICIAL = 100
AUTHORITY_ACADEMIC = 80
AUTHORITY_REFERENCE = 70
AUTHORITY_NEUTRAL = 40
AUTHORITY_LOW = 15


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def authority_of(url: str) -> int:
    """0-100. Higher is a source we would rather believe about a live fact."""
    host = domain_of(url)
    if not host:
        return AUTHORITY_NEUTRAL
    if host.endswith(_OFFICIAL_SUFFIX) or ".gov." in host:
        return AUTHORITY_OFFICIAL
    if host.endswith(_ACADEMIC_SUFFIX):
        return AUTHORITY_ACADEMIC
    base = ".".join(host.split(".")[-2:])
    if host in _REFERENCE or base in _REFERENCE:
        return AUTHORITY_REFERENCE
    if _LOW_QUALITY.search(url):
        return AUTHORITY_LOW
    return AUTHORITY_NEUTRAL


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """One passage, with everything needed to rank it and to cite it."""

    url: str
    title: str
    text: str
    domain: str
    authority: int
    fetched_at: Optional[datetime]
    published_at: Optional[datetime] = None
    #: Component scores, kept for the debug view and the metrics — an opaque
    #: single number makes a bad ranking impossible to argue with.
    dense: float = 0.0
    lexical: float = 0.0
    recency: float = 0.0
    score: float = 0.0
    page_id: Optional[int] = None

    @property
    def age_seconds(self) -> float:
        if self.fetched_at is None:
            return float("inf")
        return max(0.0, (_now() - self.fetched_at).total_seconds())

    def as_source(self) -> Dict[str, Any]:
        """The shape `meta.sources` already uses, so citations render as-is."""
        return {
            "url": self.url,
            "title": self.title or self.url,
            "snippet": self.text[:400],
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else "",
        }


@dataclass
class Retrieval:
    """What the layer found, and how much it should be trusted."""

    query: str
    freshness: Freshness
    evidence: List[Evidence] = field(default_factory=list)
    #: Sources that were dropped for being clearly superseded. Kept so the
    #: debug view can explain a decision rather than silently discarding.
    superseded: List[Evidence] = field(default_factory=list)
    #: True when comparably-authoritative sources of comparable age disagree.
    conflict: bool = False
    #: Age of the freshest evidence, in seconds.
    newest_age: float = float("inf")

    @property
    def found(self) -> bool:
        return bool(self.evidence)

    def sufficient(self, max_age_seconds: int) -> bool:
        """Enough, and new enough, to answer WITHOUT going to the network."""
        if not self.evidence:
            return False
        if self.newest_age > max_age_seconds:
            return False
        # One lone weak passage is not a basis for contradicting the model's
        # own prior; require either a decent score or corroboration.
        best = self.evidence[0]
        if best.score >= 0.62:
            return True
        return len(self.evidence) >= 2 and best.score >= 0.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

#: Half-life in days per freshness level: how fast a page's recency score
#: decays. A REALTIME question discounts yesterday heavily; a STATIC one barely
#: cares that a page is a year old.
_HALF_LIFE_DAYS = {
    Freshness.REALTIME: 0.5,
    Freshness.RECENT: 30.0,
    Freshness.STATIC: 900.0,
}

#: How much each signal contributes, per freshness level. For a live fact,
#: WHEN and WHO says it matter nearly as much as WHETHER it matches; for a
#: timeless one, relevance is almost the whole story.
_WEIGHTS = {
    Freshness.REALTIME: {"dense": 0.30, "lexical": 0.22, "recency": 0.33, "authority": 0.15},
    Freshness.RECENT:   {"dense": 0.34, "lexical": 0.26, "recency": 0.24, "authority": 0.16},
    Freshness.STATIC:   {"dense": 0.52, "lexical": 0.30, "recency": 0.04, "authority": 0.14},
}


def _recency_score(age_seconds: float, level: Freshness) -> float:
    """1.0 for something read moments ago, decaying by half-life."""
    if age_seconds == float("inf"):
        return 0.0
    half_life = _HALF_LIFE_DAYS[level] * 86400.0
    return float(0.5 ** (age_seconds / half_life))


def _dense_score(distance: float) -> float:
    """LanceDB gives L2 DISTANCE (lower is better); ranking wants 0..1 up."""
    return max(0.0, min(1.0, 1.0 - (distance / max(web_index.MAX_DISTANCE, 1e-6))))


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "is", "are", "was", "were", "who", "what", "which",
    "in", "on", "at", "to", "for", "and", "or", "s", "current", "currently",
    "now", "latest", "please", "tell", "me", "about", "do", "does", "did",
}


def _terms(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


def _lexical_score(query: str, ev: Evidence) -> float:
    """Surface-form overlap — the signal embeddings lose.

    "Vice President of India" and "Vice President of the United States" sit
    almost on top of each other in embedding space; the words "india" and
    "united states" are what tell them apart, and this is what sees them.
    Title matches count double: a page TITLED for the entity is about it.
    """
    q = _terms(query)
    if not q:
        return 0.0
    title = set(_terms(ev.title))
    body = set(_terms(ev.text[:4000]))
    hits = 0.0
    for term in set(q):
        if term in title:
            hits += 1.0
        elif term in body:
            hits += 0.5
    return min(1.0, hits / max(len(set(q)), 1))


def _score(query: str, ev: Evidence, level: Freshness) -> Evidence:
    w = _WEIGHTS[level]
    ev.lexical = _lexical_score(query, ev)
    ev.recency = _recency_score(ev.age_seconds, level)
    ev.score = (
        w["dense"] * ev.dense
        + w["lexical"] * ev.lexical
        + w["recency"] * ev.recency
        + w["authority"] * (ev.authority / 100.0)
    )
    return ev


# ---------------------------------------------------------------------------
# Conflict / supersession
# ---------------------------------------------------------------------------

#: Two pieces of evidence about the same volatile fact whose fetch times are
#: this far apart are not "two opinions" — the older one is probably just out
#: of date. 45 days: long enough that ordinary crawl jitter does not trigger
#: it, short enough to catch an office changing hands.
_SUPERSEDE_GAP_DAYS = 45


def _partition(evidence: List[Evidence], level: Freshness) -> Tuple[List[Evidence], List[Evidence], bool]:
    """(kept, superseded, conflict).

    For a live fact, evidence far older than the best evidence is DROPPED
    rather than handed to the model alongside it. Sending both and hoping the
    model prefers the newer one is how "here are two names, one from 2025 and
    one from 2026" becomes a confidently wrong answer.
    """
    if not evidence or level is Freshness.STATIC:
        return evidence, [], False

    newest = min((e.age_seconds for e in evidence if e.age_seconds != float("inf")), default=None)
    if newest is None:
        return evidence, [], False

    cutoff = newest + _SUPERSEDE_GAP_DAYS * 86400
    kept, superseded = [], []
    for ev in evidence:
        # Only drop a stale source when a BETTER-SOURCED fresh one exists;
        # an old official page still beats a fresh content farm.
        if ev.age_seconds > cutoff and any(
            k.age_seconds <= cutoff and k.authority >= ev.authority for k in evidence
        ):
            superseded.append(ev)
        else:
            kept.append(ev)

    # A genuine conflict needs INDEPENDENT sources: two comparably fresh,
    # comparably authoritative pages from DIFFERENT domains.
    #
    # Requiring different domains is not a detail. Measured against the live
    # corpus, five pages of vicepresidentofindia.nic.in — all saying the same
    # thing — tripped a same-domain check and told the model its sources
    # disagreed. Corroboration is the opposite of conflict, and hedging on a
    # unanimous official answer is exactly the timidity this layer exists to
    # remove.
    top = kept[:4]
    conflict = False
    if len(top) >= 2:
        authoritative = [e for e in top if e.authority >= AUTHORITY_REFERENCE]
        domains = {e.domain for e in authoritative if e.domain}
        if len(authoritative) >= 2 and len(domains) >= 2:
            spread = max(e.age_seconds for e in authoritative) - min(
                e.age_seconds for e in authoritative
            )
            conflict = spread < _SUPERSEDE_GAP_DAYS * 86400 / 2
    return kept, superseded, conflict


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _lexical_candidates(query: str, limit: int) -> List[Dict[str, Any]]:
    """PostgreSQL full-text candidates (V13 `search_tsv`).

    The other half of hybrid retrieval. `websearch_to_tsquery` accepts what a
    person actually types (no operator syntax to get wrong) and never raises on
    punctuation — `to_tsquery` would.
    """
    terms = " ".join(_terms(query)[:12])
    if not terms:
        return []
    try:
        with db.connection() as con:
            return con.execute(
                """SELECT id, url, title, text, domain, authority, fetched_at,
                          published_at,
                          ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s)) AS rank
                     FROM web_pages
                    WHERE search_tsv @@ websearch_to_tsquery('english', %s)
                      AND text <> ''
                    ORDER BY rank DESC
                    LIMIT %s""",
                (terms, terms, limit),
            ).fetchall()
    except Exception:  # noqa: BLE001 — lexical is one half; dense still works
        log.debug("lexical web candidates unavailable", exc_info=True)
        return []


def _page_meta(urls: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Freshness/authority columns for pages the vector index returned.

    The LanceDB row carries a `fetched_at` frozen at INDEX time, which drifts
    behind reality every time a page is re-fetched with unchanged content.
    PostgreSQL is the source of truth for when we last actually saw a page.
    """
    if not urls:
        return {}
    try:
        with db.connection() as con:
            rows = con.execute(
                """SELECT id, url, title, domain, authority, fetched_at, published_at,
                          last_changed_at
                     FROM web_pages WHERE url = ANY(%s)""",
                (list(urls),),
            ).fetchall()
        return {r["url"]: dict(r) for r in rows}
    except Exception:  # noqa: BLE001
        log.debug("page metadata unavailable", exc_info=True)
        return {}


async def retrieve(
    query: str,
    *,
    level: Freshness = Freshness.RECENT,
    top_k: int = 5,
) -> Retrieval:
    """Best local evidence for `query`, ranked for the freshness it needs.

    Hybrid by construction: the dense index supplies semantic candidates, the
    PostgreSQL text index supplies exact-surface-form ones, and both are scored
    on the same scale so they can be merged instead of concatenated.
    """
    out = Retrieval(query=query, freshness=level)
    if not settings.web_memory_enabled or not (query or "").strip():
        return out

    # Dense: over-fetch, because the ranking below reorders substantially.
    dense_hits: List[dict] = []
    try:
        dense_hits = await web_index.retrieve(query, top_k=max(top_k * 3, 12))
    except Exception:  # noqa: BLE001
        log.debug("dense web recall unavailable", exc_info=True)

    lexical_rows = await db.run_in_thread(_lexical_candidates, query, max(top_k * 3, 12))

    by_url: Dict[str, Evidence] = {}
    for hit in dense_hits:
        url = hit.get("url") or ""
        if not url:
            continue
        by_url[url] = Evidence(
            url=url,
            title=hit.get("title") or "",
            text=hit.get("text") or "",
            domain=domain_of(url),
            authority=authority_of(url),
            fetched_at=None,
            dense=_dense_score(float(hit.get("score", web_index.MAX_DISTANCE))),
        )

    for row in lexical_rows:
        url = row.get("url") or ""
        if not url:
            continue
        existing = by_url.get(url)
        if existing is not None:
            # Found by both halves — keep the dense passage (it is the matching
            # CHUNK, not the whole page) and let the lexical score stand on its
            # own merits during scoring.
            continue
        by_url[url] = Evidence(
            url=url,
            title=row.get("title") or "",
            # The page text, trimmed: a chunk-sized window is what the prompt
            # can afford, and the lexical match is near the top far more often
            # than not for entity questions.
            text=(row.get("text") or "")[:3200],
            domain=row.get("domain") or domain_of(url),
            authority=int(row.get("authority") or 0) or authority_of(url),
            fetched_at=row.get("fetched_at"),
            published_at=row.get("published_at"),
            page_id=row.get("id"),
        )

    if not by_url:
        return out

    # One trip to PostgreSQL fills in truthful timestamps and authority for
    # everything the vector index contributed.
    meta = await db.run_in_thread(_page_meta, list(by_url.keys()))
    for url, ev in by_url.items():
        m = meta.get(url)
        if m:
            ev.page_id = ev.page_id or m.get("id")
            ev.fetched_at = m.get("fetched_at") or ev.fetched_at
            ev.published_at = m.get("published_at") or ev.published_at
            ev.title = ev.title or (m.get("title") or "")
            ev.domain = ev.domain or (m.get("domain") or "")
            stored_authority = int(m.get("authority") or 0)
            if stored_authority:
                ev.authority = stored_authority

    ranked = sorted(
        (_score(query, ev, level) for ev in by_url.values()),
        key=lambda e: e.score,
        reverse=True,
    )
    kept, superseded, conflict = _partition(ranked, level)

    out.evidence = kept[:top_k]
    out.superseded = superseded[:3]
    out.conflict = conflict
    out.newest_age = min((e.age_seconds for e in out.evidence), default=float("inf"))

    # Demand signal for the refresh scheduler. Fire-and-forget: a counter must
    # never be on the latency path of an answer.
    ids = [e.page_id for e in out.evidence if e.page_id]
    if ids:
        try:
            import asyncio

            asyncio.create_task(db.run_in_thread(_bump_retrieval, ids))
        except RuntimeError:  # pragma: no cover — no running loop (tests)
            pass
    return out


def _bump_retrieval(page_ids: Sequence[int]) -> None:
    try:
        with db.connection() as con:
            con.execute(
                """UPDATE web_pages
                      SET retrieval_count = retrieval_count + 1,
                          last_retrieved_at = now()
                    WHERE id = ANY(%s)""",
                (list(page_ids),),
            )
    except Exception:  # noqa: BLE001
        log.debug("could not record retrieval demand", exc_info=True)


# ---------------------------------------------------------------------------
# Prompt grounding
# ---------------------------------------------------------------------------

#: Character budget for the whole evidence block. The window is 1M tokens, but
#: spending it on web text is how a fast answer stops being fast — and a model
#: given six focused passages answers better than one given six full pages.
_EVIDENCE_CHARS = 900


def grounding_block(result: Retrieval, today: str) -> str:
    """The system-prompt fragment that makes the model prefer evidence.

    Explicit about three things the model cannot infer: what today's date is,
    that these passages postdate its training, and WHEN each one was read — so
    it can say "as of <date>" instead of implying timeless certainty.
    """
    if not result.evidence:
        return ""

    lines = [
        f"Current date: {today}.",
        "The sources below were read from the public web by this platform and "
        "are NEWER than your training data. Prefer them over your own recollection "
        "for anything time-sensitive, and do not contradict them unless the user "
        "supplies better evidence.",
    ]
    if result.conflict:
        lines.append(
            "NOTE: these sources disagree and are of comparable age and quality. "
            "Say so plainly rather than picking one and sounding certain."
        )

    budget = _EVIDENCE_CHARS
    for i, ev in enumerate(result.evidence, 1):
        if budget <= 0:
            break
        when = ev.fetched_at.date().isoformat() if ev.fetched_at else "unknown date"
        body = " ".join((ev.text or "").split())[: max(180, budget // 2)]
        budget -= len(body)
        lines.append(f"[{i}] {ev.title or ev.domain} ({ev.domain}, read {when}): {body}")

    lines.append(
        "Answer from these sources. If they do not actually contain the answer, "
        "say what you do not know rather than filling the gap from memory."
    )
    return "\n".join(lines)


def staleness_note(result: Retrieval, max_age_seconds: int) -> str:
    """What to add when the only evidence available is past its shelf life.

    Offline or rate-limited, the honest move is to answer from what we have and
    SAY how old it is — never to present a cached fact as the current one.
    """
    if not result.evidence:
        return ""
    if result.newest_age <= max_age_seconds:
        return ""
    newest = max((e.fetched_at for e in result.evidence if e.fetched_at), default=None)
    when = newest.date().isoformat() if newest else "an unknown date"
    days = int(result.newest_age // 86400)
    return (
        f"IMPORTANT: the newest source available locally was read on {when} "
        f"({days} days ago) and could not be refreshed. Answer from it, but state "
        f"clearly that it reflects the position as of {when} and may have changed."
    )
