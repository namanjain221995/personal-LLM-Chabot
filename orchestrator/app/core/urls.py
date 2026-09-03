"""URL detection + relevance chunking (Phase 2, reused by Phase 3).

extract_urls finds http(s) links a user pasted. chunk_text / select_relevant
keep a large page from blowing the context budget: split into overlapping
chunks and keep the ones most relevant to the question (keyword overlap — the
same cheap, dependency-free approach as cross-chat recall).

shareable_url / check_shareable (2026-09-03) decide whether a link one member
pasted may be written into the SHARED web corpus at all — see the section
below for what a URL can carry that must never become everyone's knowledge.
"""
from __future__ import annotations

import ipaddress
import re
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

from ..memory_recall import keywords

# Stops before common trailing punctuation so "(see https://x.com)." works.
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_STRIP_TRAILING = ".,;:!?)]}\"'"


def extract_urls(text: str, limit: int = 5) -> List[str]:
    """Distinct http(s) URLs in `text`, order-preserving, capped at `limit`."""
    out: List[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_STRIP_TRAILING)
        if url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


#: How much OTHER text a genuine "read this link" message may carry. Enough for
#: "Please read these three and compare them against our pricing page:", and far
#: less than any real document.
MAX_SURROUNDING_CHARS = 500

#: …and how many lines. A share is a sentence; a pasted log is hundreds.
MAX_SURROUNDING_LINES = 15


def links_are_the_request(text: str, urls: List[str]) -> bool:
    """Is the user ASKING me to read these links, or do they merely appear?

    THE BUG (owner report, 2026-08-11). A 30,599-character paste — 831 lines of
    content to analyse — happened to contain some URLs. `extract_urls` found
    them, the request was routed to the URL engine, every fetch failed, and the
    answer was one sentence: "I couldn't read any of those links." The paste,
    which was the entire point of the message, was never looked at.

    The distinguishing signal is not what the links are, it is how much ELSE the
    message says. A link-sharing message is short because the links *are* the
    message. A document that mentions a URL is a document.

    Deliberately conservative in the direction that matters: when this returns
    False the URLs are treated as ordinary text and the message is answered
    normally, which is never catastrophic. Returning True wrongly means fetching
    the web and discarding what the user actually sent.
    """
    if not urls:
        return False
    remainder = text or ""
    for url in urls:
        remainder = remainder.replace(url, " ")
    if len(remainder.strip()) > MAX_SURROUNDING_CHARS:
        return False
    if remainder.count("\n") > MAX_SURROUNDING_LINES:
        return False
    return True


def chunk_text(text: str, chunk_chars: int = 1600, overlap: int = 200) -> List[str]:
    """Split text into overlapping character chunks on whitespace boundaries."""
    text = text.strip()
    if len(text) <= chunk_chars:
        return [text] if text else []
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            sp = text.rfind(" ", start + int(chunk_chars * 0.6), end)
            if sp != -1:
                end = sp
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def select_relevant(text: str, query: str, max_chars: int) -> str:
    """Return up to `max_chars` of `text` most relevant to `query`.

    Small texts pass through. Larger ones are chunked and scored by keyword
    overlap with the query; the top chunks (in original order) are joined until
    the budget is filled — so the model sees the pertinent parts of a long page.
    """
    if len(text) <= max_chars:
        return text
    kws = set(keywords(query, max_keywords=12))
    if not kws:
        return text[:max_chars]
    # Chunk smaller than the budget so the relevant chunk is kept whole rather
    # than sliced through the middle.
    chunk_size = min(1600, max(300, max_chars // 2))
    chunks = chunk_text(text, chunk_chars=chunk_size, overlap=min(150, chunk_size // 4))

    scored = []
    for i, c in enumerate(chunks):
        low = c.lower()
        score = sum(low.count(k) for k in kws)
        scored.append((score, i, c))
    # keep the highest-scoring chunks, then restore reading order
    scored.sort(key=lambda t: t[0], reverse=True)
    picked: List[tuple] = []
    total = 0
    for score, i, c in scored:
        if total + len(c) > max_chars and picked:
            break
        picked.append((i, c))
        total += len(c)
    picked.sort(key=lambda t: t[0])
    joined = "\n…\n".join(c for _i, c in picked)
    return joined[:max_chars]


# ---------------------------------------------------------------------------
# Sharing a link with the whole workspace (2026-09-03, ADR-0001 D7)
# ---------------------------------------------------------------------------
#
# Since 2026-09-03 a pasted link is not only read into the sharer's own
# conversation: the page joins the GLOBAL web corpus that every member's Fast
# answer and search draws on, and its site is queued for a background crawl.
# That turns the URL itself into a security boundary. Some URLs are
# capabilities, not addresses: a pre-signed S3 object, an Azure SAS blob, a
# "share" link, an OAuth callback carrying a code — whoever holds the URL can
# read the resource, and the BODY we fetched through it is private to the
# sharer. Storing that body in the shared corpus would hand it to every other
# member, and crawling around it would go looking for more.
#
# The decision is made from the URL alone, before any write, and it errs on
# the side of keeping the link private to the sharer's conversation (the
# per-conversation path is unchanged, so the sharer still gets their answer).
# Nothing here is a fetch guard — core/net.safe_fetch still decides what we
# connect to; this decides what we are allowed to REMEMBER for everyone.

#: Closed set of refusal classes — they become a metric label, so they must
#: never carry anything from the URL.
SHARE_REFUSAL_REASONS = (
    "unparseable",       # not a URL we can take apart
    "scheme",            # not http(s)
    "userinfo",          # user:pass@host — credentials in the URL itself
    "ip_literal",        # a bare address has no domain identity, and is how
                         # an internal service is reached
    "internal_host",     # localhost / single-label / .local-style names
    "port",              # a non-default port is an internal or dev service
    "credential_query",  # a signed / tokenised / share-link parameter
)

#: Query (or fragment) parameter NAMES that mark a URL as a capability. Matched
#: case-insensitively after `-` → `_` folding, so `access-token`, `Access_Token`
#: and `ACCESS_TOKEN` are the same name. Exact names …
_CREDENTIAL_PARAM_NAMES = frozenset({
    "signature", "sig", "sv", "se", "sp", "st",         # AWS SigV2 / Azure SAS
    "token", "access_token", "id_token", "auth",
    "apikey", "api_key", "key",
    "sas", "share", "sharetoken",
    "secret", "password",
    "session", "sessionid",
    "code", "state",                                    # OAuth callbacks
})
#: … and prefixes (`X-Amz-Signature`, `X-Amz-Credential`, `oauth_token`, …).
_CREDENTIAL_PARAM_PREFIXES = ("x_amz_", "oauth_")

#: Tracking / click-id parameters dropped from the stored URL. They carry no
#: content, they make the same page look like ten (the corpus dedups on
#: url_key), and a click id ties the stored row to the sharer's browser
#: session. `ref` is deliberately NOT here: on many sites it selects content
#: (a git ref, a catalogue reference) and stripping it would change the page.
_TRACKING_PARAM_NAMES = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid",
    "twclid", "ttclid", "igshid", "mkt_tok", "_ga", "_gl", "s_kwcid", "spm",
    "ref_src", "srsltid",
})
_TRACKING_PARAM_PREFIXES = ("utm_", "_hs", "mc_", "vero_", "oly_", "pk_", "piwik_", "matomo_")

#: Names a private network uses for itself. `.example` / `.test` / `.invalid`
#: are NOT listed: they are reserved documentation names, appear throughout
#: this suite, and resolve nowhere — nothing to protect.
_INTERNAL_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".intranet", ".corp", ".home.arpa",
)


class ShareDecision(NamedTuple):
    """`url` is the version safe to store for everyone, or None; `reason` is
    one of SHARE_REFUSAL_REASONS (empty when accepted); `stripped` names the
    tracking parameters that were removed, for the log line."""
    url: Optional[str]
    reason: str
    stripped: Tuple[str, ...]


def _param_name(part: str) -> str:
    """Fold a raw `name=value` query part to a comparable name: percent-
    decoded, lower-cased, `-` folded to `_`, a trailing `[]` (PHP arrays)
    dropped. `token[]=…`, `Access-Token=…` and `%74oken=…` all read `token`."""
    name = part.split("=", 1)[0]
    try:
        name = unquote(name)
    except Exception:  # noqa: BLE001 — a malformed escape is still a name
        pass
    name = name.strip().lower().replace("-", "_")
    if name.endswith("[]"):
        name = name[:-2]
    return name


def _is_credential_param(name: str) -> bool:
    return name in _CREDENTIAL_PARAM_NAMES or name.startswith(_CREDENTIAL_PARAM_PREFIXES)


def _is_tracking_param(name: str) -> bool:
    return name in _TRACKING_PARAM_NAMES or name.startswith(_TRACKING_PARAM_PREFIXES)


def _looks_like_ip_literal(host: str) -> bool:
    """`ipaddress` recognises dotted-quad and bracket-stripped IPv6; the
    all-digits / hex forms (`2130706433`, `0x7f000001`, `0177.0.0.1`) are the
    classic SSRF spellings that `ipaddress` refuses but a resolver accepts."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host.startswith("0x"):
        return True
    return bool(host) and all(ch.isdigit() or ch == "." for ch in host)


def check_shareable(url: str) -> ShareDecision:
    """Decide whether `url` may enter the shared corpus, and in what form.

    Refuses (url=None) a URL with userinfo, an IP-literal or internal host, a
    non-default port, a non-http(s) scheme, or a credential-shaped query or
    fragment parameter. Otherwise returns the URL with tracking parameters
    removed and everything else — path, ordinary query (`?id=123`), fragment,
    original percent-encoding, parameter order — exactly as pasted.

    Pure: no I/O, no settings, never raises.
    """
    if not url or not isinstance(url, str):
        return ShareDecision(None, "unparseable", ())
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # raises for a non-numeric or out-of-range port
    except ValueError:
        return ShareDecision(None, "unparseable", ())
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return ShareDecision(None, "scheme", ())
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return ShareDecision(None, "unparseable", ())
    # `username`/`password` are None only when no `@` precedes the host; an
    # empty username (`https://@host/`) is still an authority we do not store.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ShareDecision(None, "userinfo", ())
    if _looks_like_ip_literal(host):
        return ShareDecision(None, "ip_literal", ())
    if host == "localhost" or "." not in host or host.endswith(_INTERNAL_HOST_SUFFIXES):
        return ShareDecision(None, "internal_host", ())
    if port is not None and port != (443 if scheme == "https" else 80):
        return ShareDecision(None, "port", ())

    kept: List[str] = []
    stripped: List[str] = []
    for raw in (parts.query or "").split("&"):
        if not raw:
            continue
        name = _param_name(raw)
        if _is_credential_param(name):
            return ShareDecision(None, "credential_query", ())
        if _is_tracking_param(name):
            stripped.append(name)
            continue
        kept.append(raw)
    # A fragment never reaches the server, but the OAuth implicit grant and
    # Firebase-style links put `#access_token=…` there — the sharer's browser
    # would hand it to the page's script, and we would hand it to everyone.
    fragment = parts.fragment or ""
    if "=" in fragment:
        # Both shapes: `#access_token=…` and the hash-router callback
        # `#/callback?access_token=…` (the verifier found the second slipping
        # through when only `&` was split on).
        for piece in fragment.replace("?", "&").split("&"):
            if piece and "=" in piece and _is_credential_param(_param_name(piece)):
                return ShareDecision(None, "credential_query", ())

    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), fragment))
    return ShareDecision(cleaned, "", tuple(stripped))


def shareable_url(url: str) -> Optional[str]:
    """The form of `url` that may be stored for every member, or None when the
    link must stay private to the conversation it was pasted in. See
    check_shareable for the rules and the reason classes."""
    return check_shareable(url).url
