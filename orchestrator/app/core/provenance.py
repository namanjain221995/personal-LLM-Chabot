"""Provenance of a fetched page: WHEN it says what it says, WHAT KIND of
source it is, and whether two pages are the SAME report wearing two URLs.

Four families of pure helpers, no I/O, no model calls:

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

  UGC          `is_ugc_host()` recognises the shapes that mean "anyone can
               publish here" — a tenant subdomain, a forum label, a personal
               page, a wiki talk page — so that authority and "primary" are
               never inherited from a trusted registrable domain by a page
               its owner did not write. `authority_cap()` is what the store
               applies on top of its own score.

  DUPLICATES   `shingles()` / `jaccard()` / `near_duplicate()` catch the
               syndicated copy: the same wire story on ten domains is one
               source, not ten independent confirmations. Word 6-gram
               fingerprints over the text; a couple of milliseconds per page.

  SELF-INTEREST `self_published()` / `primary_weight()` answer a question the
               "primary" flag cannot: is the domain publishing this page the
               same entity the claim is ABOUT? A vendor grading its own
               product is first-hand and interested at the same time.

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
from urllib.parse import unquote, urlsplit

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
#: Path shapes that can only be a forum: nobody publishes first-party content
#: under /forum/ or /thread/. Counted on every host, government included.
_FORUM_PATH_RE = re.compile(r"/(forum|forums|discussion|discussions|thread|threads)/", re.I)
#: Q&A-shaped paths that are a forum on most hosts but an FAQ on an agency
#: site (/questions/ on a .gov is the agency's own FAQ) and a topic listing
#: on others (/t/ is Discourse's topic prefix). Not counted on official
#: hosts, where the public cannot write anyway.
_QA_PATH_RE = re.compile(r"/(question|questions|t)/", re.I)
_SOCIAL_HOST_RE = re.compile(
    r"(^|\.)(twitter\.com|x\.com|facebook\.com|instagram\.com|linkedin\.com|"
    r"threads\.net|tiktok\.com|youtube\.com|youtu\.be|t\.me|pinterest\.)", re.I,
)
#: Hosted-blog platforms (blogspot, wordpress.com, medium, substack, tumblr)
#: used to live here. They are tenant platforms — anyone gets a subdomain —
#: so they belong to the UGC class below; only the path shape is left.
_BLOG_RE = re.compile(r"/blog(/|$)", re.I)
_WIKI_RE = re.compile(r"(^|\.)(wikipedia\.org|wikimedia\.org|wikidata\.org|britannica\.com)$", re.I)
_CODE_HOST_RE = re.compile(r"(^|\.)(github\.com|gitlab\.com|bitbucket\.org|pypi\.org|npmjs\.com|huggingface\.co)$", re.I)

# ---------------------------------------------------------------------------
# User-generated content — "anyone can publish here"
#
# Security critique (2026-09-03): authority and "primary" were inherited from
# the registrable domain, so a page anyone can create under a trusted domain
# scored as a reference or first-hand source. Measured before the fix:
# sites.google.com and techcommunity.microsoft.com both scored 70 (the
# google.com / microsoft.com reference entries), and `someone.github.io/docs/`
# was a `docs` class and therefore primary. A member could plant a
# "reference" page on any of them and the store would believe it over a
# newsroom. The class below is STRUCTURAL: shapes of host and path that mean
# an individual, not the domain's owner, wrote the page. It never names a
# site the platform prefers for an answer, and it needs no allowlist, so a
# platform that did not exist when this was written is still caught by its
# shape.
# ---------------------------------------------------------------------------

#: A leftmost label that names a space handed to individuals on somebody
#: else's domain (a tenant): personal sites, pastes, user directories,
#: "answers" Q&A portals. Needs a registrable domain to its right, so
#: `people.com` (two labels: a magazine) is not `people.csail.mit.edu`.
#: Skipped on government hosts, where no individual can obtain a hostname
#: and `answers.` is the agency's own FAQ.
_UGC_TENANT_LABEL_RE = re.compile(r"^(sites|users?|people|gist|pastes?|pastebin|answers)\.", re.I)
#: A leftmost label that names a place where the public writes: mailing
#: lists, groups, discussion boards, "techcommunity"-style forums. Counted
#: everywhere — an agency-run forum is still a forum.
_UGC_FORUM_LABEL_RE = re.compile(
    r"^(discussions?|discuss|[a-z0-9-]*communit(y|ies)|groups|lists|bbs|boards|talk)\.", re.I
)
#: Platforms whose subdomains are tenants (the shape the Public Suffix List
#: files under PRIVATE DOMAINS): `<anyone>.github.io`, `<anyone>.medium.com`,
#: an S3 bucket, a tunnel, a dynamic-DNS name. The apex counts too — Medium
#: and Substack publish members at `medium.com/@name`. readthedocs.io is
#: deliberately absent: its tenant IS the project whose manual it hosts, the
#: /readthedocs path is already a docs shape, and the primary rule's
#: authority floor keeps a neutral-authority tenant out of "primary" anyway.
_UGC_PLATFORM_SUFFIXES = (
    # static-site and page hosts
    "github.io", "gitlab.io", "bitbucket.io", "pages.dev", "netlify.app", "vercel.app",
    "web.app", "firebaseapp.com", "herokuapp.com", "onrender.com", "fly.dev", "glitch.me",
    "repl.co", "surge.sh", "neocities.org", "notion.site", "super.site", "carrd.co",
    "webflow.io", "wixsite.com", "weebly.com", "squarespace.com", "godaddysites.com",
    "jimdosite.com", "strikingly.com", "yolasite.com", "altervista.org", "000webhostapp.com",
    "tripod.com", "angelfire.com",
    # hosted blogs and newsletters
    "medium.com", "substack.com", "hashnode.dev", "ghost.io", "wordpress.com", "tumblr.com",
    "livejournal.com", "over-blog.com",
    # hosted forums and lists
    "groups.io", "proboards.com", "freeforums.net", "boards.net", "forumotion.com",
    # object storage, CDN and app hosting — a bucket is anyone's web page
    "amazonaws.com", "storage.googleapis.com", "blob.core.windows.net", "web.core.windows.net",
    "azurewebsites.net", "cloudfront.net", "r2.dev", "digitaloceanspaces.com", "appspot.com",
    "workers.dev",
    # tunnels and dynamic DNS — a laptop with a public name
    "ngrok.io", "ngrok.app", "ngrok-free.app", "trycloudflare.com", "loca.lt", "duckdns.org",
    "no-ip.org", "ddns.net", "nip.io", "sslip.io",
)
_UGC_PLATFORM_RE = re.compile(
    r"(^|\.)(" + "|".join(re.escape(s) for s in _UGC_PLATFORM_SUFFIXES) + r")$", re.I
)
#: Blogger hands out `<name>.blogspot.<cc>` under dozens of country TLDs.
_UGC_BLOGSPOT_RE = re.compile(r"(^|\.)blogspot\.[a-z]{2,}(\.[a-z]{2,})?$", re.I)
#: Path shapes that mean an individual's space, on any host:
#:   /~name/                 the classic personal page, on a university host too
#:   /@name/                 a handle-namespaced author (Medium, Substack, Fediverse)
#:   /document/d/<id>        a Drive-style shared document (Docs, Sheets, Slides, a file)
#:   /pipermail/, /mailman/  a mailing-list archive
#: Deliberately NOT here: /user/ and /users/ (`docs.example/user/guide/` is
#: a manual section) and /profile/ (a company profile on a finance site is
#: editorial). The host classes above already catch profile pages.
_UGC_PATH_RE = re.compile(
    r"^/(~[^/]+|@[^/]+)(/|$)|"
    r"^/(document|spreadsheets|presentation|forms|file|drawings|folders)/d/|"
    r"/(pipermail|mailman|hyperkitty|archives/list)/", re.I
)
#: MediaWiki namespaces that are a person's page or a discussion, not an
#: article: User:, Talk:, any *_talk:, Draft:, and the project namespace
#: (Wikipedia:/Project: — village pumps, deletion debates). Matched on the
#: decoded path and on the ?title= form MediaWiki also serves.
_WIKI_NAMESPACE_RE = re.compile(r"(^|/)(user|talk|draft|wikipedia|project|[a-z]+_talk):", re.I)
_WIKI_TITLE_QUERY_RE = re.compile(r"(?:^|&)title=([^&]*)")

#: The authority a UGC page may not exceed: web_memory.AUTHORITY_LOW. Defined
#: here rather than imported because core/ must not depend on app/ (web_memory
#: imports this module); tests pin the two equal.
UGC_AUTHORITY_CAP = 15


def _is_official_host(host: str) -> bool:
    return host.endswith(_OFFICIAL_SUFFIX) or ".gov." in host


def _wiki_namespace(path: str, query: str) -> bool:
    candidates = [path]
    m = _WIKI_TITLE_QUERY_RE.search(query)
    if m:
        candidates.append("/" + m.group(1))
    for c in candidates:
        # `User%3AJane`, `User%20talk:Jane` and `User+talk:Jane` are the same
        # page as `User_talk:Jane`; compare the canonical form.
        decoded = unquote(c).replace(" ", "_").replace("+", "_")
        if _WIKI_NAMESPACE_RE.search(decoded):
            return True
    return False


def is_ugc_host(url: str) -> bool:
    """Could anyone — not the domain's owner — have written this page?

    True for the structural shapes of user-generated content: a social or
    forum host, a tenant subdomain of a hosting platform, a host label that
    names a public writing space, a personal-page / handle / talk-page /
    shared-document path. False for a page only the domain's owner could
    have published. Social hosts are included (they are the purest case);
    `source_type` keeps their finer label.
    """
    host = domain_of(url)
    if not host:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    path = parts.path or "/"
    if _SOCIAL_HOST_RE.search(host) or _COMMUNITY_HOST_RE.search(host):
        return True
    if _UGC_PLATFORM_RE.search(host) or _UGC_BLOGSPOT_RE.search(host):
        return True
    # A label rule needs a registrable domain to its right — three labels or
    # more. `talk.example` is a site called talk; `talk.example.org` is a
    # forum on example.org.
    subdomain = host.count(".") >= 2
    if subdomain and _UGC_FORUM_LABEL_RE.match(host):
        return True
    if _FORUM_PATH_RE.search(path) or _UGC_PATH_RE.search(path) or _wiki_namespace(path, parts.query or ""):
        return True
    # Government hosts: nobody outside the agency can obtain a hostname, and
    # a Q&A-shaped path is the agency's own FAQ. Only the unmistakable shapes
    # above count there.
    if _is_official_host(host):
        return False
    if subdomain and _UGC_TENANT_LABEL_RE.match(host):
        return True
    return bool(_QA_PATH_RE.search(path))


def authority_cap(url: str) -> Optional[int]:
    """The most authority a page at this URL may carry, or None for no cap.

    web_memory.authority_of scores a host by its suffix and a small reference
    set, then applies this: a page anyone could have written is capped at
    LOW (15) however trusted its registrable domain is."""
    return UGC_AUTHORITY_CAP if is_ugc_host(url) else None


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
    ct = (content_type or "").lower()
    # WHO could have written it outranks WHERE it sits and what format it is
    # in: a forum thread on a government host is a forum, a talk page on an
    # encyclopedia is a conversation, a PDF on a tenant site is self-published.
    # Social keeps its finer label; it is user-generated too (is_ugc_host).
    if _SOCIAL_HOST_RE.search(host):
        return "social"
    if is_ugc_host(url):
        return "community"
    if "pdf" in ct or path.lower().endswith(".pdf"):
        # A PDF on an official or academic host stays official/academic —
        # the document class outranks the container format.
        if _is_official_host(host):
            return "official"
        if host.endswith(_ACADEMIC_SUFFIX):
            return "academic"
        return "pdf"
    if _is_official_host(host):
        return "official"
    if host.endswith(_ACADEMIC_SUFFIX) or host in ("arxiv.org", "pubmed.ncbi.nlm.nih.gov"):
        return "academic"
    if _WIKI_RE.search(host):
        return "reference"
    if _DOCS_HOST_RE.search(host) or _DOCS_PATH_RE.search(path):
        return "docs"
    if _PRESS_PATH_RE.search(path):
        return "press"
    if _CODE_HOST_RE.search(host):
        return "code"
    if _BLOG_RE.search(path):
        return "blog"
    if _NEWS_PATH_RE.search(path) or host.startswith("news."):
        return "news"
    return "unknown"


#: Types that publish FIRST-HAND: the organisation, the standard, the paper,
#: the product's own documentation or announcement. Everything else reports
#: on those.
PRIMARY_TYPES = frozenset({"official", "academic", "docs", "press"})

#: The authority below which a documentation or press path is not first-hand
#: on its own: web_memory.AUTHORITY_REFERENCE. A /docs/ path on a neutral
#: (40) host is anyone's project site; on a reference-grade host it is the
#: product's own manual. Official and academic suffixes need no floor — the
#: suffix is the credential.
PRIMARY_AUTHORITY_MIN = 70


def is_primary(url: str, kind: str, authority: int) -> bool:
    """Is this a first-hand source rather than a report about one?

    BOTH conditions, structurally: the class must publish first-hand
    (PRIMARY_TYPES) AND either the suffix is the credential (official,
    academic) or the host carries reference-grade authority. Authority alone
    no longer makes a page of unknown class primary — a high score says the
    domain is trusted, not that this page is the first-hand account.

    And never a page anyone could have written. That is checked on the URL
    itself, not on the class or the authority passed in, because rows stored
    before this rule carry a `docs` class for `sites.google.com/.../docs/`
    and an authority inherited from google.com (security critique,
    2026-09-03)."""
    if kind not in PRIMARY_TYPES:
        return False
    if is_ugc_host(url):
        return False
    return kind in ("official", "academic") or authority >= PRIMARY_AUTHORITY_MIN


# ---------------------------------------------------------------------------
# Self-interest — when the publisher IS the subject
#
# R12 (2026-09-06): `is_primary` asks WHO WROTE THIS, and answers it well. It
# has no way to ask the other half of the question — WHO IS IT ABOUT. A
# vendor's own leaderboard is `docs` or `press` on a reference-grade host, so
# it is primary, and every bonus that hangs off primary is paid in full for a
# claim about the vendor itself. The publisher of a page and the subject of a
# claim are two different things, and when they are the same thing the page
# is first-hand AND interested at once.
#
# The signal is structural and needs no maintained list: the registrable name
# in the URL's host, compared against the entity the claim is about. What it
# cannot see is written down in `self_published`; read that before relying on
# it for anything stronger than withholding a bonus.
# ---------------------------------------------------------------------------

#: Words that name a KIND of organisation, not a particular one. An entity
#: that reduces to nothing but these has no distinctive name to match, and
#: matching on them would tie "Acme Systems" to every host called systems.*.
_GENERIC_ENTITY_WORDS = frozenset({
    "the", "and", "for", "inc", "incorporated", "corp", "corporation", "company",
    "companies", "ltd", "limited", "llc", "llp", "plc", "gmbh", "sarl", "spa", "bhd",
    "pte", "pty", "group", "holdings", "holding", "partners", "ventures",
    "technologies", "technology", "tech", "labs", "laboratory", "laboratories",
    "systems", "solutions", "software", "services", "service", "platform", "products",
    "international", "global", "worldwide", "national", "european", "american",
    "foundation", "institute", "institution", "university", "college", "school",
    "department", "ministry", "agency", "office", "bureau", "board", "commission",
    "committee", "council", "association", "organization", "organisation", "society",
    "project", "team", "research", "studios", "studio", "media", "digital", "online",
    "app", "apps", "web", "net", "com", "org", "www",
})

#: Second-level labels that belong to a public suffix rather than to a name:
#: `acme.co.uk`, `acme.com.au`, `agency.gov.in`, `lab.ac.jp`. Consulted ONLY
#: when the final label is a two-letter country code, so `acme.co` (Colombia,
#: sold as a vanity TLD) still reduces to `acme`.
_PUBLIC_SECOND_LEVEL = frozenset({
    "co", "com", "net", "org", "edu", "ac", "gov", "govt", "go", "ne", "or", "mil",
    "int", "gob", "gouv", "gc", "asn", "id", "sch", "res", "web", "in", "firm",
    "nom", "gen", "k12", "lg", "police", "nhs", "plc", "ltd",
})


def registrable_label(url_or_host: str) -> str:
    """The NAME part of a host: `docs.acme.co.uk` and `www.acme.com` -> "acme".

    A deliberately small public-suffix heuristic rather than a PSL dependency:
    drop the TLD, drop a second-level public label when the TLD is a country
    code, and take what is left. Subdomains are dropped too, on purpose —
    `openai.microsoft.com` is Microsoft publishing about OpenAI, not OpenAI.
    """
    raw = (url_or_host or "").strip().lower()
    host = domain_of(raw) if "//" in raw or raw.startswith(("http:", "https:")) else raw
    host = host.split("/")[0].split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return ""
    tld = labels[-1]
    rest = labels[:-1]
    if len(rest) >= 2 and len(tld) == 2 and rest[-1] in _PUBLIC_SECOND_LEVEL:
        rest = rest[:-1]
    return rest[-1] if rest else ""


def _significant(tokens: Iterable[str]) -> list:
    return [t for t in tokens if len(t) >= 3 and t not in _GENERIC_ENTITY_WORDS]


def entity_keys(subject: object) -> FrozenSet[str]:
    """Host-shaped names for the entity (or entities) a claim is about.

    `subject` is a string or an iterable of strings — the research plan's
    ENTITY list, not the free-text question: a question tokenises into
    ordinary words, and an ordinary word is a domain name somewhere.
    "Acme Rocket Corp" yields {"acme", "rocket", "acmerocket"}, so it matches
    `acme.com`, `acmerocket.io` and `acme-rocket.com` alike.
    """
    if subject is None:
        return frozenset()
    if isinstance(subject, str):
        items = [subject]
    else:
        try:
            items = list(subject)
        except TypeError:  # not a string and not iterable: nothing to compare
            return frozenset()
    out = set()
    for item in items:
        if not isinstance(item, str):
            continue
        words = _significant(_WORD_RE.findall(item.lower()))
        if not words:
            continue
        out.update(words)
        if len(words) > 1:
            out.add("".join(words))
    return frozenset(out)


def self_published(url: str, subject: object) -> bool:
    """Is the domain publishing this page the entity the claim is ABOUT?

    True when the host's registrable name IS one of the subject's names.
    `https://acme.com/benchmarks` is self-published for "Acme"; the same page
    on `reviews.example` is not.

    WHAT THIS CANNOT SEE, and what therefore must not be built on top of it:

    * OWNERSHIP. A brand whose domain does not carry the parent's name
      (`youtube.com` for Google, `instagram.com` for Meta), a subsidiary, an
      agency, a wire service, sponsored content, an "independent" review site
      the vendor owns. There is no ownership graph here and there is no
      structural signal for one.
    * PRODUCTS. A claim about "GPT-5.2" on `openai.com` only registers if the
      plan also named OpenAI as an entity. Product names are not domains.
    * INTENT. Self-publication is not dishonesty. A vendor's API reference is
      the right source for its own API, and this returns True for it. Use the
      result to withhold a TRUST bonus, never to discard a source.
    """
    keys = entity_keys(subject)
    if not keys:
        return False
    label = registrable_label(url)
    if not label:
        return False
    parts = [p for p in label.split("-") if p]
    label_keys = {label.replace("-", "")}
    label_keys.update(_significant(parts))
    return bool(label_keys & keys)


#: What is left of the first-hand bonus when the publisher is the subject.
#: A HALF, not zero: the page really is first-hand, and for a factual claim
#: about the publisher's own product it is the best source there is. What it
#: is not is disinterested, and "most trustworthy possible" is what the full
#: bonus says. Reducing an unearned bonus, rather than inventing a penalty.
SELF_PUBLISHED_PRIMARY_WEIGHT = 0.5


def primary_weight(url: str, kind: str, authority: int, subject: object = ()) -> float:
    """How much of the first-hand bonus this source has earned, in [0, 1].

    0.0 when the page is not a primary source at all; 1.0 when it is primary
    and not published by the entity it is about; SELF_PUBLISHED_PRIMARY_WEIGHT
    when it is both. Pass no `subject` and this is exactly `is_primary` as a
    float — every caller keeps its current behaviour until it has an entity
    to compare against.

    THE BOUND. `official` and `academic` keep the full weight even when they
    are self-published, because for those two classes the suffix IS the
    credential and self-publication is the normal, correct case: a statistics
    office publishes its own statistics, a university publishes its own
    papers, a ministry publishes its own regulations. Demoting them would
    turn the best sources on the web into interested ones.

    THE MISFIRE THIS STILL HAS, stated so it can be judged: a standards body
    or a language project on a `.org` — `python.org`, `w3.org` — is demoted
    for a claim about ITSELF (a question naming Python, answered from
    python.org), because the suffix carries no credential there. The cost is
    bounded to half of one bonus term: the source keeps `is_primary`, its
    authority, its rank and its citation, and only a caller's first-hand
    bonus is halved. It does not fire for the ordinary case, where the
    question is about the product and the entity list names the product.
    """
    if not is_primary(url, kind, authority):
        return 0.0
    if kind in ("official", "academic"):
        return 1.0
    if self_published(url, subject):
        return SELF_PUBLISHED_PRIMARY_WEIGHT
    return 1.0


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
#: How much of a page is fingerprinted. This was 20,000 characters — the
#: page's OPENING — on the reasoning that syndicated copies share their lead.
#: They do, but the copies that matter most do not: an aggregator that
#: prepends its own lede pushes the wire body past a short window, and a page
#: whose first 20k characters are boilerplate fingerprints its boilerplate.
#: 60,000 characters covers essentially every extracted page in the store
#: (the chunk cap at web_index.INDEXED_CHARS_PER_PAGE is 179,600, and only
#: ~2% of live rows are anywhere near it). Measured 2026-09-06: 1.7 ms for
#: 20k chars, 5.0 ms for 60k — paid once per page at registration, and the
#: retrieval caller (web_memory._collapse_duplicates) fingerprints 3,200-char
#: windows, so its cost does not move at all.
_SHINGLE_CHARS = 60_000
_SHINGLE_K = 6
#: Upper bound on the fingerprint's size, so one enormous page cannot dominate
#: a run's memory. ~10k distinct 6-grams is ~10k words, which is roughly what
#: _SHINGLE_CHARS admits anyway.
_MAX_SHINGLES = 10_000

#: THE REWRITE RULE (R11). Jaccard and containment both compare a shared
#: fingerprint against a WHOLE document, so a copy that replaces the opening
#: and keeps the body scores below both thresholds while being, plainly, the
#: same document. Measured on the fixtures in tests/test_corpus_integrity.py
#: (a wire story, an aggregator that replaced the lede and the first third
#: with its own prose, and an independent article on the same event):
#:
#:     pair                     shared  jaccard  containment
#:     wire ~ aggregator-copy      250    0.391        0.568   <- was MISSED
#:     wire ~ independent           31    0.052        0.166
#:     wire ~ unrelated              0    0.000        0.000
#:
#: 250 shared 6-grams is ~250 words of identical text. Two independent
#: reports of one event shared 31, and two unrelated pages shared none, so a
#: floor of 200 shared grams is not something coincidence produces. The
#: fraction is what keeps the floor honest: it must also be at least half of
#: the shorter document, so a long page that happens to embed a long shared
#: block is not collapsed into it.
_DUP_MIN_SHARED = 200
_DUP_SHARED_FRACTION = 0.50


def shingles(text: str, k: int = _SHINGLE_K) -> FrozenSet[int]:
    """Hashed word k-grams of the text. Empty for short texts."""
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


def shared_grams(a: Iterable[int], b: Iterable[int]) -> int:
    """How many k-grams the two fingerprints have in common — the ABSOLUTE
    volume of identical text, independent of how long either page is."""
    return len(set(a) & set(b))


def near_duplicate(a: Iterable[int], b: Iterable[int], threshold: float = 0.6) -> bool:
    """Same report? True when the fingerprints overlap heavily either way.

    Three rules, in order of how much of the page they need to agree:

    1. JACCARD — the pages are largely the same text. The verbatim
       syndication case.
    2. CONTAINMENT — one page is nearly all inside the other. The copy that
       trimmed the tail, or the aggregator that prepended a short lede.
    3. THE REWRITE RULE — a large absolute volume of identical text that is
       also at least half the shorter page. This is the one that catches a
       copy whose OPENING was rewritten, which the first two miss because
       both divide by a whole document (see _DUP_MIN_SHARED).

    Rule 3 does not scale with `threshold`. `threshold` is a Jaccard cut and
    rule 3 is not a ratio over a union, so there is no honest way to derive one
    from the other; `threshold = 0` still disables everything, but no value
    between 0 and 1 tightens rule 3. Its floor is a measurement, not a taste
    setting — if it needs to become one, it needs its own knob.

    Rule 3 is deliberately biased. Two genuinely independent articles that
    each reproduce the same long statement, where that statement is more than
    half of each of them, are also called duplicates: structurally that is
    indistinguishable from a partial rewrite, and no signal in the text
    separates them. The error is taken in this direction on purpose — calling
    a copy independent INFLATES corroboration (the defect this module exists
    to prevent), while calling a heavy quoter a duplicate only withholds a
    corroboration bonus. The source is still read, still cited, still counted
    for authority; it just stops being a second voice.
    """
    if threshold <= 0:
        return False
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return False
    shared = len(sa & sb)
    if shared == 0:
        return False
    if shared / float(len(sa | sb)) >= threshold:
        return True
    fraction_of_smaller = shared / float(min(len(sa), len(sb)))
    if fraction_of_smaller >= min(0.95, threshold + 0.25):
        return True
    return shared >= _DUP_MIN_SHARED and fraction_of_smaller >= _DUP_SHARED_FRACTION


_TITLE_NOISE_RE = re.compile(r"\s*[|\-–—:·]\s*[^|\-–—:·]{1,40}$")


def title_key(title: str) -> str:
    """A headline with its site suffix stripped, for cheap same-story hints
    ("Acme names new CEO | Reuters" ~ "Acme names new CEO - AP News")."""
    t = (title or "").strip().lower()
    t = _TITLE_NOISE_RE.sub("", t)
    return " ".join(_WORD_RE.findall(t))[:120]
