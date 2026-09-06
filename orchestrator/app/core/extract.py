"""Readable main-text extraction from fetched pages (Phase 1/2).

Turns a fetched HTML/PDF/plain-text body into clean text for the model,
dropping nav/ads/boilerplate. HTML uses trafilatura (pure-python; lxml has
arm64 wheels); PDF reuses pypdfium2's text layer; other types are refused.

Heavy libs (trafilatura, pypdfium2) import lazily so the app/tests load without
them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


#: WHICH EXTRACTOR PRODUCED A PAGE'S STORED TEXT (V21, 2026-09-05).
#:
#: Improving extraction is not free: every page already in ``web_pages`` holds
#: the text the OLD extractor produced, and the vector chunks in LanceDB were
#: built from that text. A mass re-crawl was rejected — instead each row
#: records the extractor version that filled it, and the refresh worker
#: re-reads anything below the current version most-retrieved first, inside
#: its ordinary per-cycle budget (``web_worker._due_pages``). 0 is
#: unknown/legacy.
#:
#: BUMP THIS WHENEVER EXTRACTION CHANGES WHAT A PAGE YIELDS. A change that
#: alters stored text without a bump leaves the corpus permanently split
#: between two extractors with nothing able to tell them apart.
#:
#: 1 → the original trafilatura pass.
#: 2 → the structured-append pass (tables, card lists, embedded records).
#: 3 → the bounded lxml augmentation below (C2/K4, 2026-09-06): definition
#:     lists, headings and repeated card blocks that trafilatura's precision
#:     filter discards, plus the label/value separator repair. This is a NEW
#:     number on purpose: pages stored by the version-2 build already carry
#:     2, so reusing it would mean they were never re-read.
#: 4 → embedded machine-readable records (2026-09-07): JSON-LD
#:     (`<script type="application/ld+json">`) and microdata, recovered
#:     BEFORE `<script>` is stripped and folded into the text as one line per
#:     record (`core/structured`). A page whose price/date/rating is stated
#:     only in schema.org previously extracted to prose with none of its
#:     data. The bump is required, not cosmetic: every page already stored
#:     was read by an extractor that discarded those blocks, and this is the
#:     only mechanism that gets them re-read.
EXTRACT_VERSION = 4


class UnsupportedContentError(ValueError):
    """The fetched content type can't be read as text (image, binary, …)."""


@dataclass
class Extracted:
    title: str
    text: str
    #: Provenance (2026-09-03), read from the page's own metadata by
    #: core/provenance.page_dates — ISO dates or None, never invented. Every
    #: caller and test that builds Extracted(title=, text=) keeps working.
    published_at: Optional[str] = None
    modified_at: Optional[str] = None
    #: The site's own name (og:site_name / JSON-LD publisher) when it says.
    sitename: str = ""


def _title_from_url(url: str) -> str:
    host = urlparse(url).hostname or url
    return host


def _html_title(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = _TAG_RE.sub("", m.group(1))
    title = _WS_RE.sub(" ", title).strip()
    return title or None


def _strip_tags(html: str) -> str:
    """Last-resort text: drop script/style, strip tags, collapse whitespace."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


def _extract_pdf_text(body: bytes) -> str:
    import base64

    from .pdf import render_pdf  # reuse the pypdfium2 text layer

    _images, text, _total = render_pdf(base64.b64encode(body).decode(), max_pages=10)
    return text


def _page_provenance(html: str, url: str, headers: Optional[dict]) -> tuple:
    """(published_iso, modified_iso, sitename) from the page's metadata.

    Bounded and fail-soft: a page that carries no dates yields None, and any
    parser failure yields None — the text extraction above it is never at
    risk. Measured at ~0.02 ms per page for the date pass on this image."""
    published = modified = None
    sitename = ""
    try:
        from . import provenance

        dates = provenance.page_dates(html, url, headers)
        published = dates.published.date().isoformat() if dates.published else None
        modified = dates.modified.date().isoformat() if dates.modified else None
    except Exception:  # noqa: BLE001
        pass
    try:
        import trafilatura

        meta = trafilatura.extract_metadata(html, default_url=url or None)
        sitename = (getattr(meta, "sitename", None) or "").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return published, modified, sitename


# ---------------------------------------------------------------------------
# Structural augmentation (C2/K4, 2026-09-06) — recover what trafilatura drops
# ---------------------------------------------------------------------------
# Measured on tests/fixtures/web_eval/hosting_costs.html: a <dl>/<dt>/<dd> GPU
# price list extracts to 165 chars of the SURROUNDING PROSE with all four
# prices missing — identically under include_tables, favor_recall,
# no_fallback, output_format="markdown", include_formatting and include_links.
# No kwarg reaches it: it is trafilatura's precision filter discarding the
# block, so the recovery has to be a second pass over the DOM.
#
# Why this is the dangerous class of loss rather than a cosmetic one: the
# prose survives, so the page reads as successfully fetched and gets cited,
# while carrying none of its data. The model then reports "the source does not
# mention it" about a page that states it plainly, with a citation behind it.
#
# THE RULES OF THIS PASS, deliberately timid:
#   * It only ADDS lines, or re-spaces ONE existing line in place (keeping its
#     indentation) when a label and its value came out concatenated.
#   * A candidate already present in the extracted text is dropped. "Present"
#     is compared with ALL whitespace removed, so a line break or a missing
#     separator does not make old text look new — this is what stops a page
#     whose cards trafilatura DID keep from being stored twice.
#   * Nothing inside a <table> is read or rewritten: trafilatura already
#     renders those as pipe rows, and that output must stay byte-identical.
#   * Chrome (nav/aside/footer, and nav-ish class/id names) is left out: this
#     recovers data, not menus.
#   * Every loop is bounded — nodes walked, lines added, characters added — so
#     a pathological page cannot blow up the store or the index.
#   * Any failure returns the trafilatura text unchanged. Extraction is never
#     put at risk by the augmentation.

#: Module-level so a test (or an operator patching one file) can turn the pass
#: off without a settings round-trip. `core/extract` deliberately imports no
#: config; a real setting is a follow-up for the config owner.
AUGMENT_STRUCTURED = True

_AUG_WS_RE = re.compile(r"\s+")
#: Never content, whatever the DOM says.
_AUG_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "iframe", "form",
    "button", "select", "option",
)
_AUG_CHROME_TAGS = frozenset({"nav", "aside", "footer"})
_AUG_CHROME_ATTR_RE = re.compile(
    r"(?:^|[\s_-])(?:nav|menu|breadcrumb|pagination|pager|sidebar|footer|"
    r"cookie|consent|banner|social|share|related|promo|advert|ads|subscribe|"
    r"newsletter|comment|toolbar)(?:$|[\s_-])",
    re.I,
)
_AUG_INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i",
    "kbd", "label", "mark", "output", "q", "s", "samp", "small", "span",
    "strong", "sub", "sup", "time", "u", "var",
})
_AUG_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
#: Children that make a parent look like a repeated card/list group.
_AUG_GROUP_TAGS = frozenset({"div", "li", "article", "section"})
#: Blocks whose children may be pure inline runs (the label/value card cell).
_AUG_LEAF_TAGS = frozenset({
    "div", "p", "li", "dd", "dt", "figcaption", "summary", "blockquote",
})

#: An XHTML page decodes to a str that still carries its XML declaration, and
#: lxml refuses those from a str. Dropping the declaration is enough — the
#: bytes were already decoded upstream.
_AUG_XMLDECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)

_AUG_MAX_HTML_CHARS = 3_000_000
_AUG_MAX_NODES = 20_000
_AUG_MIN_GROUP = 3
#: A "repeated card group" with more members than this is a generated grid,
#: not a data block worth reading item by item.
_AUG_MAX_GROUP_MEMBERS = 500
_AUG_MIN_KEY_CHARS = 2
_AUG_MAX_LINE_CHARS = 1_000
_AUG_MAX_ADD_LINES = 400
_AUG_MAX_ADD_CHARS = 20_000


def _aug_norm(value: str) -> str:
    return _AUG_WS_RE.sub(" ", value or "").strip()


def _aug_key(value: str) -> str:
    """Whitespace-blind, case-blind identity of a run of text.

    Dropping ALL whitespace is the point: `Reasoning93.4` (what trafilatura
    emitted) and `Reasoning 93.4` (what the DOM says) are the same fact, and
    a card split over four output lines is still the same card.
    """
    return _AUG_WS_RE.sub("", value or "").casefold()


def _aug_block_text(el) -> str:
    return _aug_norm(" ".join(el.itertext()))


def _aug_inline_text(el) -> str:
    """Text of an inline-only block, separating siblings that abut.

    `<span>Reasoning</span><span>93.4</span>` has no whitespace between the
    two spans, so every faithful extractor emits `Reasoning93.4` — which is
    one token to an embedder and unsearchable for `93.4`. A separator goes in
    ONLY between two element siblings with nothing at all between them;
    `See <a>x</a> and <a>y</a>.` keeps its own spacing untouched.
    """
    parts: list = [el.text or ""]
    abutting = False
    for child in el:
        piece = "".join(child.itertext())
        if abutting and piece:
            left = "".join(parts)
            if left and not left[-1].isspace() and not piece[0].isspace():
                parts.append(" ")
        parts.append(piece)
        tail = child.tail or ""
        parts.append(tail)
        abutting = tail == ""
    return _aug_norm("".join(parts))


def _aug_skip(el) -> bool:
    """True for anything inside a table or inside page chrome."""
    node = el
    depth = 0
    while node is not None and depth < 64:
        tag = node.tag
        if isinstance(tag, str):
            lowered = tag.lower()
            if lowered == "table":
                return True
            if lowered in _AUG_CHROME_TAGS:
                return True
            attrs = (node.get("class") or "") + " " + (node.get("id") or "")
            if attrs.strip() and _AUG_CHROME_ATTR_RE.search(attrs):
                return True
        node = node.getparent()
        depth += 1
    return False


def _aug_covered(el, covered_ids: set) -> bool:
    if not covered_ids:
        return False
    node = el.getparent()
    depth = 0
    while node is not None and depth < 64:
        if id(node) in covered_ids:
            return True
        node = node.getparent()
        depth += 1
    return False


def _augment_structured(html: str, text: str) -> str:
    """Append structured blocks trafilatura dropped; repair lost separators."""
    if not AUGMENT_STRUCTURED or not html or len(html) > _AUG_MAX_HTML_CHARS:
        return text
    try:
        import lxml.html
        from lxml import etree

        try:
            root = lxml.html.fromstring(html)
        except ValueError:  # "Unicode strings with encoding declaration…"
            root = lxml.html.fromstring(_AUG_XMLDECL_RE.sub("", html, count=1))
        etree.strip_elements(root, etree.Comment, with_tail=False)
        etree.strip_elements(root, *_AUG_DROP_TAGS, with_tail=False)
        scope = root.find(".//body")
        if scope is None:
            scope = root
    except Exception:  # noqa: BLE001 — the extracted text is never at risk
        log.debug("structural augmentation: parse failed", exc_info=True)
        return text

    lines = (text or "").split("\n")
    #: Existing lines by whitespace-blind key, for the separator repair. Pipe
    #: rows are excluded so a table can never be rewritten.
    by_key: dict = {}
    for i, line in enumerate(lines):
        if "|" in line:
            continue
        key = _aug_key(line)
        if len(key) >= 4:
            by_key.setdefault(key, []).append(i)
    whole = _aug_key(text)

    additions: list = []
    added_keys: set = set()
    state = {"chars": 0, "repairs": 0}

    def consider(line: str, key: str = "") -> bool:
        """→ True when the line was APPENDED (its subtree is then covered)."""
        line = _aug_norm(line)
        if not line or len(line) > _AUG_MAX_LINE_CHARS:
            return False
        key = key or _aug_key(line)
        if len(key) < _AUG_MIN_KEY_CHARS:
            return False
        slots = by_key.get(key)
        if slots:  # already extracted — at most re-space it, in place
            index = slots.pop(0)
            if not slots:
                by_key.pop(key, None)
            existing = lines[index]
            if existing.strip() != line and "|" not in existing:
                indent = existing[: len(existing) - len(existing.lstrip())]
                lines[index] = indent + line
                state["repairs"] += 1
            return False
        if key in whole or key in added_keys:
            return False
        if (
            len(additions) >= _AUG_MAX_ADD_LINES
            or state["chars"] + len(line) > _AUG_MAX_ADD_CHARS
        ):
            return False
        additions.append(line)
        added_keys.add(key)
        state["chars"] += len(line) + 1
        return True

    covered_ids: set = set()
    nodes = 0
    try:
        for el in scope.iter():
            nodes += 1
            if nodes > _AUG_MAX_NODES:
                break
            tag = el.tag
            if not isinstance(tag, str):
                continue
            tag = tag.lower()
            if _aug_skip(el) or _aug_covered(el, covered_ids):
                continue

            if tag in _AUG_HEADING_TAGS:
                consider(_aug_block_text(el))
                continue

            if tag == "dl":
                # A definition list is term → value(s); emitting the two
                # separately would leave orphan numbers, so they are joined.
                # The KEY is built without the ": " so a page whose <dl>
                # trafilatura DID keep is still recognised as already present.
                covered_ids.add(id(el))
                term = ""
                values: list = []

                def flush() -> None:
                    if not term:
                        return
                    joined = "; ".join(v for v in values if v)
                    line = (term + ": " + joined) if joined else term
                    consider(line, _aug_key(term + " " + joined))

                for child in el:
                    child_tag = child.tag
                    if not isinstance(child_tag, str):
                        continue
                    child_tag = child_tag.lower()
                    if child_tag == "dt":
                        flush()
                        term = _aug_block_text(child)
                        values = []
                    elif child_tag == "dd" and term:
                        values.append(_aug_block_text(child))
                flush()
                continue

            kids = [c for c in el if isinstance(c.tag, str)]
            kid_tags = {c.tag.lower() for c in kids}
            if (
                len(kids) >= _AUG_MIN_GROUP
                and len(kid_tags) == 1
                and next(iter(kid_tags)) in _AUG_GROUP_TAGS
                and len({(c.get("class") or "").strip() for c in kids}) == 1
            ):
                # Repeated sibling cards/list items: one line each, so a card
                # keeps its label with its number instead of shedding it.
                for kid in kids[:_AUG_MAX_GROUP_MEMBERS]:
                    if consider(_aug_block_text(kid)):
                        covered_ids.add(id(kid))
                continue

            if tag in _AUG_LEAF_TAGS and len(kids) >= 2:
                if all(c.tag.lower() in _AUG_INLINE_TAGS for c in kids) and (
                    sum(1 for c in kids if "".join(c.itertext()).strip()) >= 2
                ):
                    consider(_aug_inline_text(el))
    except Exception:  # noqa: BLE001
        log.debug("structural augmentation: walk failed", exc_info=True)
        return text

    if state["repairs"] or additions:
        log.debug(
            "structural augmentation: +%d line(s), %d separator repair(s)",
            len(additions), state["repairs"],
        )
    out = "\n".join(lines)
    if additions:
        out = out.rstrip() + "\n\n" + "\n".join(additions)
    return out


# ---------------------------------------------------------------------------
# Embedded machine-readable records (2026-09-07) — the piece C2 left open
# ---------------------------------------------------------------------------
# `_strip_tags` above drops <script> wholesale and `script` heads the
# augmentation pass's drop-list, so `<script type="application/ld+json">` was
# discarded before anything looked at it — and that block is frequently the
# ONLY machine-readable place a page states a price, a date, a rating or a
# spec. Microdata (itemscope/itemprop/itemtype) was never read either.
#
# The parsing, and every bound on it, lives in `core/structured`: it is
# untrusted third-party data (json.loads only, nothing executed, no @context
# or other external reference ever fetched), and the caps on blocks, bytes,
# nesting depth, records and appended characters belong next to the parser
# that has to honour them.
#
# This runs AFTER `_augment_structured` on purpose: the augmentation may have
# just recovered the very price the JSON-LD also states, and the dedupe
# compares against the text as it will actually be stored.

#: Same reasoning as AUGMENT_STRUCTURED: module-level so a test or an
#: operator can switch the pass off without a settings round-trip.
RECOVER_EMBEDDED_RECORDS = True

#: One heading above the block. The per-line `[jsonld]`/`[microdata]` marker
#: is what actually carries provenance through chunking; this only explains
#: it to the model in the one chunk that contains it.
_EMBEDDED_HEADING = (
    "Embedded structured data (records the page declares about itself):"
)


def _augment_embedded_records(html: str, text: str) -> str:
    """Append JSON-LD / microdata records the page states, deduped."""
    if not RECOVER_EMBEDDED_RECORDS or not html:
        return text
    try:
        from . import structured

        stats: dict = {}
        lines = structured.embedded_records(html, text or "", stats)
    except Exception:  # noqa: BLE001 — the extracted text is never at risk
        log.debug("embedded records: pass failed", exc_info=True)
        return text
    if not lines:
        return text
    log.debug(
        "embedded records: +%d line(s) from %d block(s), %d malformed",
        len(lines), stats.get("blocks", 0), stats.get("malformed", 0),
    )
    return (
        (text or "").rstrip() + "\n\n" + _EMBEDDED_HEADING + "\n"
        + "\n".join(lines)
    )


def extract_readable(
    content_type: str, body: bytes, url: str, headers: Optional[dict] = None
) -> Extracted:
    """Extract (title, readable text) from a fetched body, dispatched by type.

    `headers` (optional) supplies the response's Last-Modified for pages that
    carry no date of their own. Raises UnsupportedContentError for types we
    can't read as text.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    lowered_url = url.lower()

    if "pdf" in ct or lowered_url.endswith(".pdf"):
        modified = None
        try:
            from . import provenance

            dt = provenance.parse_date((headers or {}).get("last-modified"))
            modified = dt.date().isoformat() if dt else None
        except Exception:  # noqa: BLE001
            modified = None
        return Extracted(
            title=_title_from_url(url),
            text=_extract_pdf_text(body),
            modified_at=modified,
        )

    if ct in ("text/plain", "text/markdown"):
        return Extracted(
            title=_title_from_url(url), text=body.decode("utf-8", "replace")
        )

    if "html" in ct or "xml" in ct or ct == "":
        html = body.decode("utf-8", "replace")
        text: Optional[str] = None
        try:
            import trafilatura

            text = trafilatura.extract(
                html, include_comments=False, include_tables=True, favor_recall=True
            )
        except Exception:
            text = None
        if not text:
            text = _strip_tags(html)
        else:
            # C2/K4: trafilatura's precision filter drops definition lists,
            # card blocks and headings outright. The prose it keeps makes the
            # page look successfully read, so the loss is invisible.
            text = _augment_structured(html, text)
        # Both branches: `_strip_tags` drops <script> too, so a JS-rendered
        # page whose only readable content is its JSON-LD needs this pass
        # exactly as much as one trafilatura read successfully.
        text = _augment_embedded_records(html, text)
        published, modified, sitename = _page_provenance(html, url, headers)
        return Extracted(
            title=_html_title(html) or _title_from_url(url),
            text=text or "",
            published_at=published,
            modified_at=modified,
            sitename=sitename,
        )

    raise UnsupportedContentError(ct or "unknown")


#: K9 (2026-09-06): 263 live web_pages rows hold under 400 characters of
#: extracted text and were stored with fetch_status=200 — including
#: https://qwen.ai/home, whose entire stored body is the four characters
#: "Qwen". Those rows are retrieved, cited, and counted as "the page was
#: read", so the model answers a question from a page that says nothing and
#: the citation panel behind it looks perfectly healthy.
#:
#: INTERACTION with the embedded-record pass (2026-09-07), deliberate and
#: covered by a test: a JavaScript shell whose JSON-LD states a hundred priced
#: products now extracts to thousands of characters and PASSES this gate. That
#: is the intent — the page carries the data it was fetched for, in the only
#: machine-readable form it has. A shell with no embedded records is refused
#: exactly as before.
#:
#: The gate is deliberately conservative: it refuses only what CANNOT be an
#: answer, never what is merely brief. "Nimbus does not offer an L40S
#: instance. Spot capacity, where available, is billed at 40% of the
#: on-demand price." (111 chars) passes. A bare site name and a
#: "please enable JavaScript" shell do not.
STORE_MIN_CHARS = 100
STORE_MIN_WORDS = 8

_JS_SHELL_RE = re.compile(
    r"enable\s+javascript|javascript\s+is\s+(?:required|disabled|turned\s+off)|"
    r"requires\s+javascript|turn\s+on\s+javascript|enable\s+js\b|"
    r"your\s+browser\s+does\s+not\s+support",
    re.I,
)
#: A shell is short by definition. Above this, "enable JavaScript" is a
#: sentence ON a real page (a tutorial, a support article), not the page.
_JS_SHELL_MAX_CHARS = 1500


def page_quality(text: str, url: str = "") -> "tuple[bool, str]":
    """(worth_storing, reason). `reason` is "" when the page is worth storing.

    Shared deliberately: the crawler, the search-result read and the
    pasted-link read all persist into the same global corpus, so they should
    all apply the same floor. Placed here rather than in the crawler so no
    caller has to import an engine to use it.
    """
    body = (text or "").strip()
    if not body:
        return False, "empty"
    if len(body) <= _JS_SHELL_MAX_CHARS and _JS_SHELL_RE.search(body):
        return False, "js_shell"
    if len(body) < STORE_MIN_CHARS or len(body.split()) < STORE_MIN_WORDS:
        return False, "thin"
    return True, ""


def truncate_chars(text: str, max_chars: int) -> str:
    """Trim to a character budget on a whitespace boundary, with an ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sp = cut.rfind(" ")
    if sp > max_chars * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + " …"

def extract_readable_and_links(
    content_type: str, body: bytes, url: str, headers: Optional[dict] = None
) -> "tuple[Extracted, list[str]]":
    """One parse pass for the crawler: readable text PLUS harvested links.

    MUST run inside the same single-worker pool as extract_readable —
    trafilatura shares non-thread-safe compiled lxml objects, and a parallel
    parse on another thread can abort the interpreter (see the pool note in
    engines/search.py). Combining the two here means the crawler pays one
    pool submission per page, not two.

    Links come only from HTML (a PDF has none worth walking), are
    absolute-ized against the FINAL post-redirect URL with <base href>
    honoured, and are de-fragmented — fragments multiply a crawl frontier
    with self-links. Measured: 41 ms for ~2,500 links on a 727 KB doc page.
    """
    extracted = extract_readable(content_type, body, url, headers)
    links: list[str] = []
    if "html" in (content_type or "").lower():
        try:
            import lxml.html  # already a trafilatura dependency

            doc = lxml.html.document_fromstring(body)
            doc.make_links_absolute(url, resolve_base_href=True)
            from urllib.parse import urldefrag

            seen: set[str] = set()
            for a in doc.iter("a"):
                href = (a.get("href") or "").strip()
                if not href.startswith(("http://", "https://")):
                    continue
                clean, _frag = urldefrag(href)
                if clean and clean not in seen:
                    seen.add(clean)
                    links.append(clean)
        except Exception:  # noqa: BLE001 — links are an extra, never a gate
            # Loud on purpose: a systematically broken harvest (lxml missing,
            # API change) otherwise looks exactly like "this site has no
            # links", and the crawler quietly explores nothing.
            log.warning("link harvest failed for %s", url, exc_info=True)
            links = []
    return extracted, links

