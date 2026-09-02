"""Provenance of a fetched page: WHEN it says what it says, WHAT KIND of
source it is, and whether two pages are the SAME report wearing two URLs.

Three families of pure helpers, no I/O, no model calls:

  DATES        `page_dates()` reads publication / modification dates out of
               the page's own metadata (OpenGraph, JSON-LD, <time>, the
               Last-Modified header). It never invents a date: a page that
               does not say when it was written gets None, and the ranking
               layers treat None as "unknown", not as "today".

  SOURCE TYPE  `source_type()` classifies a URL structurally — official,
               academic, documentation, news/press, community, social, blog,
               PDF — from its domain suffix and path shape. Structural on
               purpose: it recognises the CLASS of a site (a government
               suffix, a /press-release/ path, a Q&A forum), never a
               particular site or a particular answer.

  DUPLICATES   `shingles()` / `jaccard()` / `near_duplicate()` catch the
               syndicated copy: the same wire story on ten domains is one
               source, not ten independent confirmations. Word 6-gram
               fingerprints over the opening of the text; a few hundred
               microseconds per page.

Why this lives in core/ and not inside the research engine: the search
engine, the crawler, the refresh worker and the living-knowledge layer all
store and rank pages, and they all need the same notion of "when" and
"how trustworthy". One implementation, one set of tests.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import FrozenSet, Iterable, Mapping, Optional
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?)?$"
)
_EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)


def parse_date(value: object) -> Optional[datetime]:
    """A timezone-aware UTC datetime from the shapes pages actually emit:
    ISO-8601 (with or without time/offset), RFC 2822 (HTTP headers), or a
    datetime/date object. None for anything else — never a guess."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        try:
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    text = str(value).strip()
    if not text:
        return None
    m = _ISO_RE.match(text)
    if m:
        y, mo, d, hh, mm, ss, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
        except ValueError:
            return None
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt - sign * offset
        return dt.replace(tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plausible(dt: Optional[datetime], now: Optional[datetime] = None) -> Optional[datetime]:
    """Drop dates no page can honestly carry: before the web, or in the
    future (a scheduler's placeholder, a typo'd year)."""
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if dt < _EARLIEST or dt > now + timedelta(days=2):
        return None
    return dt


@dataclass
class PageDates:
    published: Optional[datetime] = None
    modified: Optional[datetime] = None
    #: Which signal produced `published` — surfaced in logs so a wrong date
    #: is diagnosable ("meta", "jsonld", "header", "text", ...).
    source: str = ""


def page_dates(
    html: str,
    url: str = "",
    headers: Optional[Mapping[str, str]] = None,
    *,
    now: Optional[datetime] = None,
) -> PageDates:
    """Publication and modification dates from a page's own metadata.

    htmldate (a trafilatura dependency, already in the image) reads
    OpenGraph/article meta tags, JSON-LD, <time> elements and, as a last
    resort, dates written in the visible text. `original_date=True` asks for
    the FIRST publication date; a second pass asks for the latest update.
    The HTTP Last-Modified header fills the modification date when the page
    itself is silent. Everything is bounds-checked; nothing is invented.
    """
    out = PageDates()
    if not html:
        return out
    try:
        from htmldate import find_date  # lazy: heavy import, optional in tests

        published = find_date(html, url=url or None, original_date=True, outputformat="%Y-%m-%d")
        if published:
            out.published = _plausible(parse_date(published), now)
            out.source = "page"
        modified = find_date(html, url=url or None, original_date=False, outputformat="%Y-%m-%d")
        if modified:
            out.modified = _plausible(parse_date(modified), now)
    except Exception:  # noqa: BLE001 — dates are an enhancement, never a gate
        pass
    if headers:
        header_mod = _plausible(parse_date(headers.get("last-modified")), now)
        if header_mod and out.modified is None:
            out.modified = header_mod
        if header_mod and out.published is None and out.modified is None:
            out.modified = header_mod
    # A modification can never precede the publication it modifies.
    if out.published and out.modified and out.modified < out.published:
        out.modified = out.published
    return out


def effective_time(
    published: Optional[datetime],
    modified: Optional[datetime],
    fetched: Optional[datetime],
) -> Optional[datetime]:
    """When the page's content is FROM, for recency ranking.

    Publication wins: a 2019 article fetched this morning is still a 2019
    article. A modification date counts when the page was updated after it
    was written. Only a page with no dates of its own falls back to the
    fetch time — the weakest possible evidence, which is why the ranking
    layers treat it as a floor, not a claim.
    """
    if published and modified:
        return max(published, modified)
    return published or modified or fetched


# ---------------------------------------------------------------------------
# Source type
# ---------------------------------------------------------------------------

#: The classes are structural: what kind of site publishes at this shape of
#: address. None of these name a site the platform prefers for an ANSWER.
_OFFICIAL_SUFFIX = (
    ".gov", ".gov.in", ".gov.uk", ".gov.au", ".go.jp", ".govt.nz", ".gc.ca",
    ".europa.eu", ".int", ".mil", ".nic.in", ".gov.sg", ".gov.br", ".gouv.fr",
    ".bund.de", ".gov.za", ".gov.ie", ".admin.ch",
)
_ACADEMIC_SUFFIX = (".edu", ".ac.uk", ".edu.au", ".ac.in", ".edu.cn", ".ac.jp", ".ac.nz")
_DOCS_HOST_RE = re.compile(r"^(docs?|documentation|developer|developers|dev|api|help|support|wiki)\.", re.I)
_DOCS_PATH_RE = re.compile(r"/(docs?|documentation|reference|manual|guide|api-reference|readthedocs)(/|$)", re.I)
_PRESS_PATH_RE = re.compile(
    r"/(press|press-?releases?|newsroom|news-?room|media|announcements?|releases?|"
    r"changelog|release-?notes|whats-?new|blog/announc)", re.I,
)
_NEWS_PATH_RE = re.compile(r"/(news|article|articles|story|stories|202\d/\d{2}|20\d\d/[a-z]{3})(/|$)", re.I)
_COMMUNITY_HOST_RE = re.compile(
    r"(^|\.)(reddit\.com|stackoverflow\.com|stackexchange\.com|quora\.com|"
    r"news\.ycombinator\.com|discourse\.|forum\.|forums\.|community\.)", re.I,
)
_COMMUNITY_PATH_RE = re.compile(r"/(forum|forums|questions?|discussion|discussions|thread|threads|t)/", re.I)
_SOCIAL_HOST_RE = re.compile(
    r"(^|\.)(twitter\.com|x\.com|facebook\.com|instagram\.com|linkedin\.com|"
    r"threads\.net|tiktok\.com|youtube\.com|youtu\.be|t\.me|pinterest\.)", re.I,
)
_BLOG_RE = re.compile(r"(^|\.)(blogspot|wordpress\.com|medium\.com|substack\.com|tumblr\.com)|/blog(/|$)", re.I)
_WIKI_RE = re.compile(r"(^|\.)(wikipedia\.org|wikimedia\.org|wikidata\.org|britannica\.com)$", re.I)
_CODE_HOST_RE = re.compile(r"(^|\.)(github\.com|gitlab\.com|bitbucket\.org|pypi\.org|npmjs\.com|huggingface\.co)$", re.I)

SOURCE_TYPES = (
    "official", "academic", "reference", "docs", "press", "news", "code",
    "community", "social", "blog", "pdf", "unknown",
)


def domain_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def source_type(url: str, content_type: str = "", sitename: str = "") -> str:
    """The structural class of a source. See SOURCE_TYPES."""
    host = domain_of(url)
    if not host:
        return "unknown"
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        path = "/"
    lowered = url.lower()
    ct = (content_type or "").lower()
    if "pdf" in ct or path.lower().endswith(".pdf"):
        # A PDF on an official or academic host stays official/academic —
        # the document class outranks the container format.
        if host.endswith(_OFFICIAL_SUFFIX) or ".gov." in host:
            return "official"
        if host.endswith(_ACADEMIC_SUFFIX):
            return "academic"
        return "pdf"
    if host.endswith(_OFFICIAL_SUFFIX) or ".gov." in host:
        return "official"
    if host.endswith(_ACADEMIC_SUFFIX) or host in ("arxiv.org", "pubmed.ncbi.nlm.nih.gov"):
        return "academic"
    if _SOCIAL_HOST_RE.search(host):
        return "social"
    if _COMMUNITY_HOST_RE.search(host) or _COMMUNITY_PATH_RE.search(path):
        return "community"
    if _WIKI_RE.search(host):
        return "reference"
    if _DOCS_HOST_RE.search(host) or _DOCS_PATH_RE.search(path):
        return "docs"
    if _PRESS_PATH_RE.search(path):
        return "press"
    if _CODE_HOST_RE.search(host):
        return "code"
    if _BLOG_RE.search(lowered):
        return "blog"
    if _NEWS_PATH_RE.search(path) or host.startswith("news."):
        return "news"
    return "unknown"


#: Types that publish FIRST-HAND: the organisation, the standard, the paper,
#: the product's own documentation or announcement. Everything else reports
#: on those.
PRIMARY_TYPES = frozenset({"official", "academic", "docs", "press"})


def is_primary(url: str, kind: str, authority: int) -> bool:
    """Is this a first-hand source rather than a report about one?

    Structural again: an official or academic host, first-party
    documentation, or a press/announcement path. High cached authority
    (reference sites, first-party corporate domains) also counts — the
    living-knowledge layer's authority scale already encodes that."""
    if kind in PRIMARY_TYPES:
        return True
    return authority >= 70 and kind not in ("community", "social", "blog")


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
#: Fingerprint the OPENING of a page: syndicated copies share their lead, and
#: a 6-gram over the first ~20k characters is cheap and decisive.
_SHINGLE_CHARS = 20_000
_SHINGLE_K = 6
_MAX_SHINGLES = 4000


def shingles(text: str, k: int = _SHINGLE_K) -> FrozenSet[int]:
    """Hashed word k-grams of the text's opening. Empty for short texts."""
    words = _WORD_RE.findall((text or "")[:_SHINGLE_CHARS].lower())
    if len(words) < k:
        return frozenset()
    out = set()
    for i in range(len(words) - k + 1):
        gram = " ".join(words[i : i + k])
        out.add(int(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).hexdigest(), 16))
        if len(out) >= _MAX_SHINGLES:
            break
    return frozenset(out)


def jaccard(a: Iterable[int], b: Iterable[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return inter / float(len(sa | sb))


def containment(a: Iterable[int], b: Iterable[int]) -> float:
    """|a ∩ b| / |smaller| — catches the copy that trimmed the original.

    Jaccard alone misses a syndicated article that cut the last paragraphs:
    the shared opening is nearly all of the shorter text, but only half of
    the longer one's shingles, so the union-based score sags below the
    threshold while the two are plainly the same report."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


def near_duplicate(a: Iterable[int], b: Iterable[int], threshold: float = 0.6) -> bool:
    """Same report? True when the fingerprints overlap heavily either way."""
    if threshold <= 0:
        return False
    return jaccard(a, b) >= threshold or containment(a, b) >= min(0.95, threshold + 0.25)


_TITLE_NOISE_RE = re.compile(r"\s*[|\-–—:·]\s*[^|\-–—:·]{1,40}$")


def title_key(title: str) -> str:
    """A headline with its site suffix stripped, for cheap same-story hints
    ("Acme names new CEO | Reuters" ~ "Acme names new CEO - AP News")."""
    t = (title or "").strip().lower()
    t = _TITLE_NOISE_RE.sub("", t)
    return " ".join(_WORD_RE.findall(t))[:120]
