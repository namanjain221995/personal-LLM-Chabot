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

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import db, metrics, rerank, web_index
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
    # Authority is per HOST, never inherited from the registrable domain:
    # sites.google.com, gist.github.com and answers.microsoft.com are places
    # anyone can publish, and a member could plant a "reference" page there
    # (security critique, 2026-09-03). core/provenance knows the shapes.
    cap: Optional[int] = None
    try:
        from .core.provenance import authority_cap

        cap = authority_cap(url)
    except Exception:  # noqa: BLE001 — helper absent in an older tree
        cap = None
    if host.endswith(_OFFICIAL_SUFFIX) or ".gov." in host:
        score = AUTHORITY_OFFICIAL
    elif host.endswith(_ACADEMIC_SUFFIX):
        score = AUTHORITY_ACADEMIC
    else:
        base = ".".join(host.split(".")[-2:])
        if host in _REFERENCE or base in _REFERENCE:
            score = AUTHORITY_REFERENCE
        elif _LOW_QUALITY.search(url):
            score = AUTHORITY_LOW
        else:
            score = AUTHORITY_NEUTRAL
    return min(score, cap) if cap is not None else score


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
    #: V14 provenance: when the page was last updated, and its structural
    #: class (official / docs / news / community …) from core/provenance.
    modified_at: Optional[datetime] = None
    source_type: str = ""
    #: Component scores, kept for the debug view and the metrics — an opaque
    #: single number makes a bad ranking impossible to argue with.
    dense: float = 0.0
    lexical: float = 0.0
    recency: float = 0.0
    score: float = 0.0
    page_id: Optional[int] = None
    #: Probability that this passage ANSWERS the question, from the templated
    #: cross-encoder (app/rerank.py, ADR-0001 D4). -1.0 = not judged.
    answer: float = -1.0
    #: Where this evidence may be shown. Everything this module reads is the
    #: PUBLIC web corpus (no user data in `web_pages`); the evidence cache
    #: refuses anything else, so a future private source cannot leak through
    #: a shared cache by accident.
    scope: str = "public"
    #: How the page entered the corpus (V16): search | refresh | crawl |
    #: share | research. A member-SHARED page may be cited but never retires
    #: other evidence and never gains authority (ADR-0001 D7).
    origin: str = "search"

    @property
    def age_seconds(self) -> float:
        """Seconds since this platform READ the page — the freshness of the
        copy, which decides whether the network is worth spending."""
        if self.fetched_at is None:
            return float("inf")
        return max(0.0, (_now() - self.fetched_at).total_seconds())

    @property
    def effective_age_seconds(self) -> float:
        """Seconds since the page's content is FROM — its publication or
        last update when the page states one, else the read time.

        The distinction is the whole fix for a stale copy outranking a
        current one: a 2019 article fetched this morning is fresh as a COPY
        and five years old as EVIDENCE. Ranking and supersession use this;
        the network decision keeps `age_seconds`."""
        from .core.provenance import effective_time

        when = effective_time(self.published_at, self.modified_at, self.fetched_at)
        if when is None:
            return float("inf")
        return max(0.0, (_now() - when).total_seconds())

    @property
    def scored(self) -> bool:
        """True when the cross-encoder judged this passage (ADR-0001 D4)."""
        return self.answer >= 0.0

    @property
    def content_date(self) -> Optional[datetime]:
        """The page's own date, when precise enough to order pages by.

        A year-only date (what a page that states just "2026" becomes) says
        nothing about which of two pages is newer inside that year, and it
        must never make a page look eight months stale next to one that
        states no date at all — that is exactly how the org chart naming an
        office holder lost to an undated company profile."""
        from .core.provenance import effective_time

        when = effective_time(self.published_at, self.modified_at, None)
        return when if date_precision(when) == "day" else None

    @property
    def dated(self) -> bool:
        return self.content_date is not None

    @property
    def relevant(self) -> bool:
        """Does this passage bear on the question — may it be cited, may it
        retire an older passage?

        When the cross-encoder ran, its answer probability decides: a page
        ABOUT the entity that never states the fact asked about is not
        relevant, however many of the question's words it repeats. Without
        that judgement the older heuristic stands: the question's own words
        on the page, or a strong vector match (recency and authority alone
        once pushed five unrelated documentation pages past the bar)."""
        if self.scored:
            return self.answer >= _relevant_threshold()
        return self.lexical >= 0.34 or self.dense >= 0.62

    def as_source(self) -> Dict[str, Any]:
        """The shape `meta.sources` already uses, so citations render as-is."""
        return {
            "url": self.url,
            "title": self.title or self.url,
            "snippet": self.text[:400],
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else "",
            "origin": self.origin,
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
    #: Why the cross-encoder's judgement is missing, when it is: rerank_busy
    #: | rerank_error | rerank_disabled | rerank_canary | embed_busy … A
    #: degraded retrieval is never cached and never escalates under load.
    degraded: str = ""

    @property
    def found(self) -> bool:
        return bool(self.evidence)

    @property
    def stale_answer(self) -> bool:
        """The best ANSWERING passage states a date older than the level's
        stale cutoff. A yes/no judge says "yes" to "<name> was appointed in
        2023" — supersession cannot fire without a newer rival, the copy is
        re-read daily, and the platform would answer with the former office
        holder, cited. The passage's own date is the only signal left."""
        cutoff = _stale_after(self.freshness)
        if cutoff is None:
            return False
        best = next((e for e in self.evidence if e.relevant), None)
        if best is None or best.content_date is None:
            return False
        return (_now() - best.content_date).total_seconds() > cutoff

    @property
    def confidence(self) -> float:
        """How sure the corpus is that it ANSWERS: the best answer
        probability among relevant passages (0 when nothing was scored)."""
        best = 0.0
        for e in self.evidence:
            if e.scored and e.relevant:
                best = max(best, e.answer)
        return best

    def sufficient(self, max_age_seconds: int) -> bool:
        """Enough, and new enough, to answer WITHOUT going to the network."""
        if not self.evidence:
            return False
        if self.newest_age > max_age_seconds:
            return False
        # Only passages that are actually about the question count. Without
        # this gate, fresh + authoritative + unrelated read as "answered" and
        # the live lookup that would have found the real answer never ran.
        relevant = [e for e in self.evidence if e.relevant]
        if not relevant:
            return False
        best = relevant[0]
        if best.scored:
            # The cross-encoder's verdict: one passage that clearly answers,
            # or two that probably do. Two profile pages that merely name the
            # entity score near zero here and send the question onward —
            # which is what the audit found the entity-overlap rule failing.
            # Freshness here is COPY age (the page was re-read recently and
            # still says this); the passage's own date is checked separately
            # by `stale_answer`.
            if self.stale_answer:
                return False
            if best.answer >= _answer_threshold():
                return True
            return len(relevant) >= 2 and all(
                e.answer >= _CORROBORATION_ANSWER for e in relevant[:2]
            )
        if self.degraded and self.degraded != "rerank_disabled":
            # Unjudged because the reranker was busy, broken or tripped:
            # only the strict topical gate may declare sufficiency — vector
            # agreement AND the question's words AND a strong blend. The
            # looser rule below is exactly what the audit found declaring
            # two non-answering profile pages "enough" (critique 2026-09-03),
            # and a busy reranker must not silently reinstate it.
            return best.dense >= 0.35 and best.lexical >= 0.34 and best.score >= 0.62
        # No cross-encoder on this deployment at all (a profile without the
        # reranker service): the hybrid rule stands as it always did. One
        # lone weak passage is not a basis for contradicting the model's own
        # prior; require either a decent score or corroboration.
        if best.score >= 0.62:
            return True
        return len(relevant) >= 2 and best.score >= 0.5


def _stale_after(level: Freshness) -> Optional[float]:
    """Seconds after which an answering passage's own date makes it stale."""
    if level is Freshness.STATIC:
        return None
    if level is Freshness.REALTIME:
        from .freshness import _MAX_AGE

        return float(_MAX_AGE[Freshness.REALTIME])
    try:
        return float(settings.knowledge_stale_after_recent_s)
    except Exception:  # noqa: BLE001
        return 120.0 * 86400


def _relevant_threshold() -> float:
    try:
        return float(settings.knowledge_relevant_threshold)
    except Exception:  # noqa: BLE001
        return 0.30


def _answer_threshold() -> float:
    try:
        return float(settings.knowledge_answer_threshold)
    except Exception:  # noqa: BLE001
        return 0.70


#: Two passages at this answer probability corroborate each other enough
#: to stand in for one that clears the full bar.
_CORROBORATION_ANSWER = 0.5


def date_precision(dt: Optional[datetime]) -> str:
    """'none' | 'year' | 'day'. htmldate returns a full date even when a page
    states only a year; that arrives as January 1st, midnight."""
    if dt is None:
        return "none"
    if dt.month == 1 and dt.day == 1 and dt.hour == 0 and dt.minute == 0:
        return "year"
    return "day"


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
    # Question shapes, not topics (2026-09-03): "explain" matched a page
    # titled "Explaining AI…" and made an unrelated page look relevant.
    "explain", "describe", "summarize", "summarise", "list", "show", "give",
    "how", "why", "when", "where", "whether", "can", "could", "would", "should",
    "will", "need", "want", "know", "help", "info", "information", "details",
    "detail", "question", "answer", "with", "from", "into", "this", "that",
    "these", "those", "there", "here", "its", "it", "be", "been", "being",
}


_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(word: str) -> str:
    """A deliberately tiny stemmer: 'configured' ~ 'configure' ~ 'configures'.

    Exact-token overlap missed the obvious inflections — a page that says
    "configure" scored zero for a question that says "configured". Suffix
    stripping on words long enough to survive it fixes the common cases
    without a stemming dependency; it is a matching aid, never shown."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    # configure/configured, release/released, engine/engines all meet here.
    if len(word) >= 6 and word.endswith("e"):
        word = word[:-1]
    return word


def _terms(text: str) -> List[str]:
    return [
        _stem(w)
        for w in _WORD.findall((text or "").lower())
        if w not in _STOP and len(w) > 1
    ]


def _content_words(text: str) -> List[str]:
    """The question's content words, UNSTEMMED — what goes to PostgreSQL.

    The tiny Python stemmer and PostgreSQL's snowball disagree on ordinary
    words ('business' → 'busines' here, 'busi' there), and a term stemmed
    twice matches nothing. Under AND one such word excluded the page that
    matched every other term (critique, 2026-09-03). PostgreSQL stems once;
    `_stem` is for the Python-side overlap score only.
    """
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


#: Same window shape as the vector index, so the passage the cross-encoder
#: judges and the passage the model reads are the same size.
_WINDOW_CHARS = 3200
_WINDOW_STEP = 2800


def _best_window(text: str, query: str) -> str:
    """The ~3,200-char window of a page that carries the most of the
    question's terms. A lexical hit used to hand the cross-encoder the page
    HEAD; an answer at character 6,000 was judged "no" and the same page
    was fetched again to no effect (critique, 2026-09-03)."""
    clean = " ".join((text or "").split())
    if len(clean) <= _WINDOW_CHARS:
        return clean
    wanted = set(_terms(query))
    if not wanted:
        return clean[:_WINDOW_CHARS]
    best, best_hits = clean[:_WINDOW_CHARS], -1
    for start in range(0, len(clean), _WINDOW_STEP):
        piece = clean[start : start + _WINDOW_CHARS]
        hits = len(wanted & set(_terms(piece)))
        if hits > best_hits:
            best, best_hits = piece, hits
        if start + _WINDOW_CHARS >= len(clean):
            break
    return best


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
    # Recency is about the CONTENT's date, not the copy's (see Evidence).
    ev.recency = _recency_score(ev.effective_age_seconds, level)
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


def _site_of(url: str) -> str:
    """eTLD+1, so `in.linkedin.com` and `www.linkedin.com` are one site for
    the corroboration and conflict rules."""
    try:
        from .engines.search import _registrable_domain

        return _registrable_domain(url) or domain_of(url)
    except Exception:  # noqa: BLE001
        return domain_of(url)


def supersession_allowed(level: Freshness, verdict: Optional[Any]) -> bool:
    """May an older answering passage be retired by a newer one AT ALL?

    Only for questions whose answer is the kind that gets REPLACED: an office
    holder, a live value, an explicitly current/latest/volatile ask, one the
    router judged time-sensitive, or one anchored to a present-day year. For
    the ambiguous default ("what did release X change?", "how many flights
    did Y fly?") the newest page is not "newer information about the same
    fact" — measured on the eval set (2026-09-03), that rule retired 13 of
    60 gold pages, each judged as answering with probability 1.0, in favour
    of newer pages that were merely relevant.
    """
    if level is Freshness.STATIC:
        return False
    if verdict is None:
        return True  # direct callers and tests: the level alone decides
    if getattr(verdict, "requirement", None) is Freshness.REALTIME or getattr(verdict, "volatile", False):
        return True
    reason = str(getattr(verdict, "reason", "") or "")
    return reason.startswith(("lexical:office", "lexical:realtime", "lexical:recent", "router", "year:"))


def _partition(
    evidence: List[Evidence], level: Freshness, verdict: Optional[Any] = None
) -> Tuple[List[Evidence], List[Evidence], bool]:
    """(kept, superseded, conflict).

    For a live fact, evidence far older than the best evidence is DROPPED
    rather than handed to the model alongside it. Sending both and hoping the
    model prefers the newer one is how "here are two names, one from 2025 and
    one from 2026" becomes a confidently wrong answer.
    """
    if not evidence or not supersession_allowed(level, verdict):
        return evidence, [], False

    ages = {id(e): e.effective_age_seconds for e in evidence}
    gap = _SUPERSEDE_GAP_DAYS * 86400
    strong = _answer_threshold()

    def _retires(newer: Evidence, older: Evidence) -> bool:
        """May `newer` retire `older` as out of date?

        Three conditions, each learned from a measured failure:
        - both must be RELEVANT — a page about the entity that never states
          the fact cannot be "newer information" about it (an undated
          company profile retired the dated org chart that was the only
          page naming the office holder);
        - a page whose age is only its fetch time cannot retire one that
          states its date: we know nothing about when its content was
          written (the twin copy of one page, with and without a date, kept
          the undated twin);
        - only a BETTER-SOURCED fresh page retires an old one — an old
          official page still beats a fresh content farm.
        """
        if not (newer.relevant and older.relevant):
            return False
        if newer.scored or older.scored:
            # Judged passages: both must ANSWER, not merely relate. "Newer
            # information about the same fact" needs a newer page that states
            # the fact — a page about the same entity that scores 0.3 is not
            # a reason to discard one that scores 1.0.
            if not (newer.scored and older.scored):
                return False
            if newer.answer < strong or older.answer < strong:
                return False
        if older.dated and not newer.dated:
            return False
        if newer.origin == "share":
            # A page a member pasted is cited on its merits but can never be
            # the reason another source is discarded (D7).
            return False
        if newer.authority < older.authority:
            return False
        a_new, a_old = ages[id(newer)], ages[id(older)]
        if a_new == float("inf") or a_old == float("inf"):
            return False
        return a_old - a_new > gap

    kept, superseded = [], []
    for ev in evidence:
        if any(k is not ev and _retires(k, ev) for k in evidence):
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
    top = [e for e in kept if e.relevant][:4]
    conflict = False
    if len(top) >= 2:
        authoritative = [e for e in top if e.authority >= AUTHORITY_REFERENCE]
        domains = {_site_of(e.url) for e in authoritative if e.url}
        if len(authoritative) >= 2 and len(domains) >= 2:
            spread = max(ages[id(e)] for e in authoritative) - min(
                ages[id(e)] for e in authoritative
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
    words = _content_words(query)[:12]
    if not words:
        return []
    # AND first, OR to fill. websearch_to_tsquery ANDs plain words, which
    # puts the pages that carry EVERY question term first — for an entity
    # question those are the pages about the entity. OR alone, ranked by
    # ts_rank_cd, measurably rewarded raw term frequency instead: a
    # Microsoft "Solution Architects" page outranked every page about a
    # small company named "… Solutions", and encyclopaedia articles on the
    # word "CEO" filled the rest. OR still runs, after, so ONE question word
    # absent from a page ("explain", "how") cannot exclude the page that
    # matches every other term. Normalisation 32 (rank/(rank+1)) bounds the
    # rank without penalising a long authoritative page against a stub (1
    # and 2 divide by length and did exactly that). At most three pages per
    # domain: a crawled site whose footer carries the entity's name must
    # not fill the whole candidate set with boilerplate. Quarantined pages
    # (V16) never leave the store.
    sql = """WITH ranked AS (
               SELECT id, url, title, text, domain, authority, fetched_at,
                      published_at, modified_at, source_type, origin,
                      ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 32) AS rank,
                      row_number() OVER (
                        PARTITION BY domain
                        ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 32) DESC
                      ) AS dn
                 FROM web_pages
                WHERE search_tsv @@ websearch_to_tsquery('english', %s)
                  AND text <> ''
                  AND quarantined_at IS NULL
             )
             SELECT * FROM ranked WHERE dn <= 3 ORDER BY rank DESC LIMIT %s"""
    plain = " ".join(words)
    any_of = " OR ".join(words)
    try:
        with db.connection() as con:
            rows = list(con.execute(sql, (plain, plain, plain, limit)).fetchall())
            if len(rows) < limit:
                seen = {r["id"] for r in rows}
                for r in con.execute(sql, (any_of, any_of, any_of, limit)).fetchall():
                    if r["id"] not in seen:
                        rows.append(r)
                        seen.add(r["id"])
            return rows[:limit]
    except Exception:  # noqa: BLE001 — lexical is one half; dense still works
        log.debug("lexical web candidates unavailable", exc_info=True)
        return []


def _page_meta(urls: Sequence[str], ids: Sequence[int] = ()) -> Dict[str, Dict[str, Any]]:
    """Freshness/authority columns for pages the vector index returned.

    The LanceDB row carries a `fetched_at` frozen at INDEX time, which drifts
    behind reality every time a page is re-fetched with unchanged content.
    PostgreSQL is the source of truth for when we last actually saw a page.
    Joined by page id where the index row has one (PostgreSQL rewrites `url`
    on refetch, so a URL join could miss and leave a hit with no dates —
    which made it "never fresh"); by url otherwise. Keys: url and "id:<id>".
    """
    urls = [u for u in urls if u]
    ids = [int(i) for i in ids if i]
    if not urls and not ids:
        return {}
    try:
        with db.connection() as con:
            rows = con.execute(
                """SELECT id, url, title, domain, authority, fetched_at, published_at,
                          last_changed_at, modified_at, source_type, origin, quarantined_at
                     FROM web_pages WHERE url = ANY(%s) OR id = ANY(%s)""",
                (urls, ids),
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            out[d["url"]] = d
            out[f"id:{d['id']}"] = d
        return out
    except Exception:  # noqa: BLE001
        log.debug("page metadata unavailable", exc_info=True)
        return {}


async def retrieve(
    query: str,
    *,
    level: Freshness = Freshness.RECENT,
    top_k: int = 5,
    use_cache: bool = True,
    effort: str = "fast",
    verdict: Optional[Any] = None,
) -> Retrieval:
    """Best local evidence for `query`, ranked for the freshness it needs.

    Hybrid by construction: the dense index supplies semantic candidates, the
    PostgreSQL text index supplies exact-surface-form ones, and both are scored
    on the same scale so they can be merged instead of concatenated. Then the
    cross-encoder judges whether each candidate ANSWERS (ADR-0001 D4), and
    supersession runs only among passages that do (D5).

    `use_cache=False` is for a caller that just wrote to the store and is
    reading back (the Fast lookup). `effort` only sets how long stage 1 may
    wait for a reranker slot.
    """
    out = Retrieval(query=query, freshness=level)
    if not settings.web_memory_enabled or not (query or "").strip():
        return out

    cache_key = _cache_key(query, level, top_k)
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    # Dense and lexical halves run CONCURRENTLY (they touch different
    # services); both over-fetch because the ranking below reorders a lot.
    want = max(top_k * 3, 12)

    async def _dense() -> List[dict]:
        try:
            return await web_index.retrieve(query, top_k=want)
        except Exception:  # noqa: BLE001
            log.debug("dense web recall unavailable", exc_info=True)
            return []

    async def _lexical() -> List[Dict[str, Any]]:
        started = time.perf_counter()
        try:
            return await db.run_in_thread(_lexical_candidates, query, max(want, 24))
        finally:
            metrics.observe("knowledge_stage_seconds", time.perf_counter() - started, stage="lexical")

    dense_hits, lexical_rows = await asyncio.gather(_dense(), _lexical())

    # Merge by PAGE, not by URL string: PostgreSQL rewrites `url` on refetch
    # and dedupes on url_key, so a dense hit and a lexical row for the same
    # page can spell the URL differently.
    by_key: Dict[str, Evidence] = {}
    for hit in dense_hits:
        url = hit.get("url") or ""
        if not url:
            continue
        key = f"id:{hit['page_id']}" if hit.get("page_id") else url
        by_key[key] = Evidence(
            url=url,
            title=hit.get("title") or "",
            text=hit.get("text") or "",
            domain=domain_of(url),
            authority=authority_of(url),
            fetched_at=None,
            dense=_dense_score(float(hit.get("score", web_index.MAX_DISTANCE))),
            page_id=hit.get("page_id"),
        )

    for row in lexical_rows:
        url = row.get("url") or ""
        if not url:
            continue
        key = f"id:{row['id']}" if row.get("id") else url
        existing = by_key.get(key) or by_key.get(url)
        window = _best_window(row.get("text") or "", query)
        if existing is not None:
            # Found by both halves. The dense CHUNK is the nearest by meaning;
            # the lexical WINDOW carries the most question words. Keep the
            # one the question's words point at when it beats the chunk —
            # for an entity question that is the leadership section, not the
            # "about" paragraph the embedding sat closest to.
            if len(set(_terms(window)) & set(_terms(query))) > len(
                set(_terms(existing.text)) & set(_terms(query))
            ):
                existing.text = window
            continue
        by_key[key] = Evidence(
            url=url,
            title=row.get("title") or "",
            text=window,
            domain=row.get("domain") or domain_of(url),
            authority=int(row.get("authority") or 0) or authority_of(url),
            fetched_at=row.get("fetched_at"),
            published_at=row.get("published_at"),
            modified_at=row.get("modified_at"),
            source_type=row.get("source_type") or "",
            origin=row.get("origin") or "search",
            page_id=row.get("id"),
        )

    if not by_key:
        return out

    # One trip to PostgreSQL fills in truthful timestamps, authority and
    # provenance for everything the vector index contributed — and drops
    # anything an operator quarantined.
    started = time.perf_counter()
    meta = await db.run_in_thread(
        _page_meta,
        [e.url for e in by_key.values()],
        [e.page_id for e in by_key.values() if e.page_id],
    )
    metrics.observe("knowledge_stage_seconds", time.perf_counter() - started, stage="meta")
    candidates: List[Evidence] = []
    for ev in by_key.values():
        m = meta.get(f"id:{ev.page_id}") if ev.page_id else None
        m = m or meta.get(ev.url)
        if m:
            if m.get("quarantined_at"):
                continue
            ev.page_id = ev.page_id or m.get("id")
            ev.fetched_at = m.get("fetched_at") or ev.fetched_at
            ev.published_at = m.get("published_at") or ev.published_at
            ev.modified_at = m.get("modified_at") or ev.modified_at
            ev.source_type = ev.source_type or (m.get("source_type") or "")
            ev.title = ev.title or (m.get("title") or "")
            ev.domain = ev.domain or (m.get("domain") or "")
            ev.origin = m.get("origin") or ev.origin
            stored_authority = int(m.get("authority") or 0)
            if stored_authority:
                ev.authority = min(stored_authority, ev.authority) if ev.origin == "share" else stored_authority
        if ev.origin == "share":
            # A member-shared page may be cited, never trusted above neutral.
            ev.authority = min(ev.authority, AUTHORITY_NEUTRAL)
        candidates.append(ev)

    ranked = sorted(
        (_score(query, ev, level) for ev in candidates),
        key=lambda e: e.score,
        reverse=True,
    )
    ranked = _collapse_duplicates(ranked, query)
    if settings.knowledge_rerank:
        ranked, out.degraded = await _answerability(query, ranked, level=level, effort=effort)
    if verdict is None:
        # The freshness verdict's REASON decides whether supersession may run
        # at all (see supersession_allowed); a caller that has one passes it,
        # every other caller gets the same deterministic classification.
        from .freshness import classify_offline

        verdict = classify_offline(query, now_year=_now().year)
    kept, superseded, conflict = _partition(ranked, level, verdict=verdict)

    out.evidence = kept[:top_k]
    out.superseded = superseded[:3]
    out.conflict = conflict
    out.newest_age = min((e.age_seconds for e in out.evidence), default=float("inf"))
    # Cache only a judged, non-empty result: a miss is cheap to repeat and
    # would otherwise hide pages the indexer adds within the TTL; a degraded
    # verdict must never be served as a judged one.
    if out.evidence and not out.degraded:
        _cache_put(cache_key, out)

    # Demand signal for the refresh scheduler. Fire-and-forget: a counter must
    # never be on the latency path of an answer.
    ids = [e.page_id for e in out.evidence if e.page_id]
    if ids:
        try:
            asyncio.create_task(db.run_in_thread(_bump_retrieval, ids))
        except RuntimeError:  # pragma: no cover — no running loop (tests)
            pass
    return out


def _rerank_text(ev: Evidence) -> str:
    """What the cross-encoder judges: the title (the entity, usually) and the
    passage. The title matters — a chunk from the middle of a page often
    never repeats the name the question asks about."""
    title = " ".join((ev.title or "").split())
    body = " ".join((ev.text or "").split())[:2800]
    return f"{title}\n{body}" if title else body


def _collapse_duplicates(ranked: List[Evidence], query: str = "") -> List[Evidence]:
    """One evidence item per distinct text: mirrors (www./in.), syndicated
    copies and crawl duplicates otherwise count as corroboration and as
    "two domains" for the conflict rule.

    Two passages that are near-duplicates by fingerprint but carry DIFFERENT
    question terms are not copies of each other: sibling release notes
    (3.14.4 vs 3.14.5) share almost every sentence and differ in exactly the
    term the question asks about. Measured (2026-09-03): the fingerprint alone
    collapsed the asked-for release into its sibling."""
    from .core.provenance import near_duplicate, shingles

    # Raw tokens, not stems: the distinguishing term is often a single digit
    # ("3.14.4" vs "3.14.5"), which the stemmer's length filter drops.
    wanted = {w for w in _WORD.findall((query or "").lower()) if w not in _STOP}
    kept: List[Evidence] = []
    prints: List[Tuple[frozenset, frozenset]] = []
    for ev in ranked:
        fp = shingles(ev.text)
        hits = frozenset(wanted & set(_WORD.findall((ev.text or "").lower()))) if wanted else frozenset()
        if fp and any(near_duplicate(fp, other) and hits == other_hits for other, other_hits in prints):
            continue
        kept.append(ev)
        prints.append((fp, hits))
    return kept


def _degraded_reason(exc: Exception) -> str:
    text = str(exc).lower()
    if "busy" in text:
        return "rerank_busy"
    if "disabled" in text:
        return "rerank_disabled"
    if "tripped" in text or "canary" in text:
        return "rerank_canary"
    if "degenerate" in text:
        return "rerank_degenerate"
    return "rerank_error"


async def _answerability(
    query: str, ranked: List[Evidence], *, level: Freshness, effort: str
) -> Tuple[List[Evidence], str]:
    """Judge the top hybrid candidates: does each one ANSWER the question?

    The hybrid score says "about the same thing"; the cross-encoder says
    "answers this". Measured on the live corpus the two disagree exactly where
    it hurts — the passage naming the office holder ranked below profile
    pages that never mention the office. The answer probability leads the
    final order; the hybrid score breaks ties, so authority and recency still
    decide between passages that answer equally well.

    Returns (ranked, degraded_reason). Unavailable (disabled, busy, down,
    degenerate) → the hybrid order stands, the reason is recorded, and the
    caller neither caches the result nor escalates on the strength of it.

    Cost control: a STATIC question is judged only when the strict topical
    pre-gate finds a candidate at all (most timeless questions have none and
    pay nothing), and then at most 8 passages; a time-sensitive one at most
    `knowledge_rerank_candidates` (12 ≈ 95 ms measured).
    """
    if level is Freshness.STATIC:
        pre = [e for e in ranked if e.dense >= 0.35 and e.lexical >= 0.34]
        if not pre:
            return ranked, ""
        n = min(8, max(1, int(settings.knowledge_rerank_candidates)))
    else:
        n = max(1, int(settings.knowledge_rerank_candidates))
    # The judged set is the top of the BLEND plus the top of EACH HALF. The
    # blend weights recency for a time-sensitive question, which pushed a
    # ten-year-old page that dense retrieval ranked first below the cut —
    # unjudged, unranked, lost (measured on the eval set, 2026-09-03).
    head = list(ranked[:n])
    seen = {id(e) for e in head}
    for key in ("dense", "lexical"):
        for e in sorted(ranked, key=lambda x: getattr(x, key), reverse=True)[:4]:
            if id(e) not in seen and getattr(e, key) > 0:
                head.append(e)
                seen.add(id(e))
    tail = [e for e in ranked if id(e) not in seen]
    if not head:
        return ranked, ""
    wait = (
        float(settings.rerank_wait_fast_s)
        if (effort or "fast") == "fast"
        else float(settings.rerank_wait_think_s)
    )
    started = time.perf_counter()
    try:
        scores = await rerank.score(
            query,
            [_rerank_text(e) for e in head],
            kind=rerank.LOCAL,
            wait=wait,
            timeout=float(settings.rerank_stage_timeout_s),
        )
    except rerank.RerankUnavailable as exc:
        reason = _degraded_reason(exc)
        metrics.inc("knowledge_rerank_total", outcome="unavailable")
        metrics.inc("knowledge_degraded_total", reason=reason)
        log.debug("answerability unavailable: %s", exc)
        return ranked, reason
    metrics.inc("knowledge_rerank_total", outcome="ok")
    metrics.observe("knowledge_stage_seconds", time.perf_counter() - started, stage="rerank")
    for ev, prob in zip(head, scores):
        ev.answer = max(0.0, min(1.0, float(prob)))
        ev.score = 0.7 * ev.answer + 0.3 * ev.score
    head.sort(key=lambda e: e.score, reverse=True)
    return head + tail, ""


# ---------------------------------------------------------------------------
# Public-scope evidence cache (ADR-0001 D10)
# ---------------------------------------------------------------------------

#: normalised question → (monotonic stamp, Retrieval). Everything in here is
#: PUBLIC web evidence — this module reads only `web_pages` — which is why
#: the cache may be shared between users at all. The TTL is seconds, far
#: below every freshness window, so a cached result is never "stale" in the
#: sense the freshness layer cares about.
_cache: "OrderedDict[str, Tuple[float, Retrieval]]" = OrderedDict()


def _cache_key(query: str, level: Freshness, top_k: int) -> str:
    # The corpus generation (bumped on every page upsert / index write) and
    # the reranker's state are part of the key: a cached retrieval can never
    # outlive the corpus it came from, and a degraded one never masquerades
    # as a judged one.
    gen = db.web_corpus_generation()
    judged = "r" if (settings.knowledge_rerank and rerank.enabled()) else "h"
    return f"{gen}:{judged}:{level.value}:{int(top_k)}:{' '.join((query or '').lower().split())}"


def _cache_get(key: str) -> Optional[Retrieval]:
    ttl = float(getattr(settings, "knowledge_evidence_cache_ttl_s", 0) or 0)
    if ttl <= 0:
        return None
    hit = _cache.get(key)
    if hit is None or time.monotonic() - hit[0] > ttl:
        if hit is not None:
            _cache.pop(key, None)
        metrics.inc("knowledge_evidence_cache_total", outcome="miss")
        return None
    _cache.move_to_end(key)
    metrics.inc("knowledge_evidence_cache_total", outcome="hit")
    value = hit[1]
    # Fresh containers AND fresh items: callers filter, reassign and (in the
    # search path) trim `text`; none of that may edit the next caller's copy.
    return replace(
        value,
        evidence=[replace(e) for e in value.evidence],
        superseded=[replace(e) for e in value.superseded],
    )


def _cache_put(key: str, value: Retrieval) -> None:
    ttl = float(getattr(settings, "knowledge_evidence_cache_ttl_s", 0) or 0)
    if ttl <= 0:
        return
    if any(e.scope != "public" for e in value.evidence):
        # Not a code path today (this module is public-only); the assertion
        # is the contract, so a future private source cannot leak through.
        log.warning("evidence cache refused non-public evidence")
        return
    _cache[key] = (time.monotonic(), replace(value, evidence=list(value.evidence), superseded=list(value.superseded)))
    _cache.move_to_end(key)
    limit = max(8, int(getattr(settings, "knowledge_evidence_cache_size", 256) or 256))
    while len(_cache) > limit:
        _cache.popitem(last=False)


def cache_clear() -> None:
    _cache.clear()


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

#: Fallback character budget for the whole evidence block when settings are
#: unavailable. The live value is settings.living_knowledge_evidence_chars
#: (3600 by default since 2026-09-03; it was 900 — one paragraph, which is
#: not "a large amount of information from the site"). ~1k tokens of prefill
#: costs a Fast answer well under half a second on this deployment.
_EVIDENCE_CHARS = 3600


def _evidence_budget() -> int:
    try:
        return max(600, int(settings.living_knowledge_evidence_chars))
    except Exception:  # noqa: BLE001
        return _EVIDENCE_CHARS


def _dated_label(ev: Evidence) -> str:
    """'(domain, published 2026-03-12, read 2026-09-02)' — both dates, so
    the model can say 'as of' truthfully and weigh an old article read
    today for what it is."""
    read = ev.fetched_at.date().isoformat() if ev.fetched_at else "unknown date"
    stamp = ""
    if ev.published_at:
        stamp = f"published {ev.published_at.date().isoformat()}, "
    elif ev.modified_at:
        stamp = f"updated {ev.modified_at.date().isoformat()}, "
    kind = f"{ev.source_type}, " if ev.source_type and ev.source_type != "unknown" else ""
    shared = "shared by a workspace member, " if ev.origin == "share" else ""
    return f"({kind}{shared}{ev.domain}, {stamp}read {read})"


def _passages(result: Retrieval, budget: int) -> List[str]:
    """Numbered passages within the budget — several focused ones rather
    than one long one, because the answer needs coverage, not a single page."""
    lines: List[str] = []
    n = max(1, min(len(result.evidence), 4))
    per = max(300, min(1400, budget // n))
    remaining = budget
    for i, ev in enumerate(result.evidence, 1):
        if remaining <= 0:
            break
        body = " ".join((ev.text or "").split())[: min(per, max(180, remaining))]
        remaining -= len(body)
        lines.append(f"[{i}] {ev.title or ev.domain} {_dated_label(ev)}: {body}")
    return lines


def grounding_block(result: Retrieval, today: str) -> str:
    """The system-prompt fragment that makes the model prefer evidence.

    Explicit about three things the model cannot infer: what today's date is,
    that these passages postdate its training, and WHEN each one was read and
    published — so it can say "as of <date>" instead of implying timeless
    certainty.
    """
    if not result.evidence:
        return ""

    lines = [
        f"Current date: {today}.",
        "The sources below were read from the public web by this platform and "
        "are NEWER than your training data. Prefer them over your own recollection "
        "for anything time-sensitive, and do not contradict them unless the user "
        "supplies better evidence. Each source shows when it was published (where "
        "the page says) and when it was read; a newer authoritative source "
        "outranks an older one.",
    ]
    if result.conflict:
        lines.append(
            "NOTE: these sources disagree and are of comparable age and quality. "
            "Say so plainly rather than picking one and sounding certain."
        )
    lines.extend(_passages(result, _evidence_budget()))
    lines.append(
        "Answer from these sources. If they do not actually contain the answer, "
        "say what you do not know rather than filling the gap from memory. "
        + CITATION_RULE
    )
    return "\n".join(lines)


#: Shared by every grounded prompt. The audit found an earlier answer of the
#: assistant's, recalled from another chat, being restated and cited against
#: sources that did not contain it.
CITATION_RULE = (
    "Context from the user's earlier conversations is not a source: cite [n] "
    "only for statements that appear in source n, and never present something "
    "you said in an earlier conversation as if a source here confirmed it."
)


def topical_block(result: Retrieval, today: str) -> str:
    """Grounding for a TIMELESS question that a strongly matching stored
    passage can answer — a site the user indexed, a document a research run
    read. The framing differs from the time-sensitive block: the passages are
    reference material to draw on and cite, not a correction of the model's
    knowledge, and the model may still answer the parts they do not cover."""
    if not result.evidence:
        return ""
    lines = [
        f"Current date: {today}.",
        "Reference material this platform has already read from the web — pages "
        "from sites indexed here or read during earlier research — that closely "
        "matches the question. Draw on it where it answers the question and cite "
        "it inline as [n]; say plainly when it does not cover a part of the "
        "question, and answer that part from your own knowledge. " + CITATION_RULE,
    ]
    lines.extend(_passages(result, _evidence_budget()))
    return "\n".join(lines)


def claims_for(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Resolved claims from earlier Deep Research runs that match the
    question's words. Blocking (run via db.run_in_thread). Never raises."""
    # Unstemmed words: PostgreSQL stems them itself (see _content_words).
    terms = " ".join(_content_words(query)[:12])
    if not terms:
        return []
    try:
        return db.search_web_claims(terms, limit=limit, kinds=["current"])
    except Exception:  # noqa: BLE001
        log.debug("claim lookup unavailable", exc_info=True)
        return []


def claims_block(rows: Sequence[Dict[str, Any]]) -> str:
    """Lines for claims a research run resolved as CURRENT — each with the
    date the claim was true as of, and where it came from, so the model
    can state it as a dated fact rather than a timeless one."""
    lines: List[str] = []
    for r in rows:
        claim = " ".join(str(r.get("claim") or "").split())
        value = " ".join(str(r.get("value") or "").split())
        if not claim:
            continue
        as_of = r.get("as_of")
        when = f" (as of {as_of.isoformat() if hasattr(as_of, 'isoformat') else as_of})" if as_of else ""
        made = r.get("created_at")
        made_s = made.date().isoformat() if hasattr(made, "date") else "an earlier date"
        origin = r.get("domain") or domain_of(str(r.get("url") or "")) or "a web source"
        lines.append(
            f"- {claim}{(': ' + value) if value and value.lower() not in claim.lower() else ''}"
            f"{when} — established by a research run on {made_s} from {origin}"
        )
    if not lines:
        return ""
    return "Facts an earlier research run on this platform verified against web sources:\n" + "\n".join(lines)


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
    if result.newest_age == float("inf"):
        # Dense-only hits carry no fetch time until PostgreSQL fills it in;
        # `int(inf // 86400)` raised and threw away the whole grounding.
        return (
            "IMPORTANT: the sources available locally carry no read date and "
            "could not be refreshed. Answer from them, but say plainly that "
            "their currency is unknown."
        )
    days = int(result.newest_age // 86400)
    return (
        f"IMPORTANT: the newest source available locally was read on {when} "
        f"({days} days ago) and could not be refreshed. Answer from it, but state "
        f"clearly that it reflects the position as of {when} and may have changed."
    )
