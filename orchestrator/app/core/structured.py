"""Machine-readable records a page embeds about itself: JSON-LD + microdata.

WHY THIS EXISTS. ``core/extract`` drops every ``<script>`` before it looks at
the DOM (``_strip_tags``, and ``script`` heads the augmentation pass's
drop-list), so ``<script type="application/ld+json">`` never reaches the text
the model sees. That block is frequently the ONLY machine-readable place a
page states a price, a date, a rating or a spec: schema.org ``Product`` /
``Offer`` / ``Dataset`` / ``Article`` is written for crawlers, and a
JavaScript-rendered storefront often has nothing else. Microdata
(``itemscope``/``itemprop``/``itemtype``) is the same data in attributes and
was likewise never read. The structural augmentation pass in ``extract``
recovered definition lists, headings and card blocks and explicitly left this
piece open.

WHAT COMES OUT. Lines, not a schema — text the rest of the pipeline already
knows how to store, chunk, embed and window. One record per line, the entity
first and its fields attached to it::

    [jsonld] Product: H100 80GB SXM — price: 2.90 USD; availability: InStock

The pairing IS the information: ``H100``, ``2.90`` and ``USD`` scattered as
loose tokens is worse than nothing, because a query-centred window can then
put a number next to the wrong entity. Nested objects fold into the entity
that owns them (an ``Offer`` has no name of its own, so its price belongs on
the product's line); a nested object that HAS a name gets its own line
carrying its parent's name, so the association survives a chunk boundary.

PROVENANCE IS PER LINE, not per block. ``[jsonld]``/``[microdata]`` says the
value came from an embedded record rather than from the page's prose, and it
is repeated on every line on purpose: chunking splits a page every few
thousand characters and a single heading above the block would be separated
from most of its rows (the same failure the table-header repair exists for).

THIS IS UNTRUSTED DATA, PARSED AS DATA.
  * ``json.loads`` only. No ``eval``, no ``exec``, no JS engine, no
    ``object_hook``. ``parse_float``/``parse_int``/``parse_constant`` are set
    to ``str`` for a second reason beyond safety: it keeps a number in the
    form the page wrote it, so ``"price": 2.90`` stays ``2.90`` instead of
    becoming ``2.9``.
  * NOTHING IS FETCHED. ``@context``, ``@id``, ``sameAs`` and ``itemtype``
    are identifiers, not resources — a schema.org URL in ``@context`` is
    never resolved, and this module opens no socket at all.
  * Dates are copied through verbatim. Whatever the page stated ("2026-03-04",
    "March 4, 2026", "2026-03-04T09:00:00Z") is what is emitted; nothing here
    parses, normalises or invents a date. ``core/provenance`` owns dates.
  * Control characters are stripped and every value is length-clipped, so a
    hostile record cannot inject a newline and forge an extra line.

EVERYTHING IS BOUNDED — see the constants below, each with the reason for its
value. This runs on the request path, inside the single-worker extract pool,
and a page can legitimately carry megabytes of JSON-LD (product feeds do) or
a pathologically nested structure. A pre-existing audit found
``trafilatura.extract_metadata`` failing to return within 280 s on a 3.6 MB
page, so unbounded work here is a demonstrated hazard, not a theoretical one.
Malformed JSON is skipped silently-but-counted (``stats``), never raised.

MEASURED on this image, 2026-09-07 (aarch64, python 3.12): 0.9 ms on a 100 KB
page with no embedded records at all (two regex probes and nothing else),
6.1 ms on a 700 KB one, and 16 ms on a deliberately hostile 2.6 MB page
carrying 300 ld+json blocks and 5,000 ``itemscope`` elements — the caps bite
long before the work does. A page over MAX_HTML_CHARS is declined in 0 ms.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BOUNDS. Every loop below is capped by one of these.
# ---------------------------------------------------------------------------
#: Whole-page ceiling. Matches ``extract._AUG_MAX_HTML_CHARS`` deliberately:
#: above it the augmentation pass already declines, and the fetcher's own
#: byte cap normally keeps pages far below it.
MAX_HTML_CHARS = 3_000_000
#: ``<script>`` elements examined at all (most are JS we skip on the type
#: attribute alone). A big SPA page carries a few dozen.
MAX_SCRIPT_ELEMENTS = 400
#: ld+json blocks actually parsed. Real pages carry 1-6; 20 is generous.
MAX_BLOCKS = 20
#: Bytes of ONE block handed to json.loads. Real blocks are single-digit KB;
#: 256 KB covers even a bloated product feed, and a block above it is skipped
#: and counted rather than parsed — that is the megabyte-feed defence.
MAX_BLOCK_CHARS = 262_144
#: …and a page cannot get around that with 20 blocks of 256 KB each.
MAX_TOTAL_BLOCK_CHARS = 1_048_576
#: Nesting descended. schema.org data is 2-4 deep in practice; past 6 the
#: content is plumbing (``isPartOf`` chains) and a recursive/self-referential
#: shape stops here instead of unwinding forever.
MAX_DEPTH = 6
#: Total dict/list nodes visited per page, across every block. The single
#: hard stop that makes a wide-and-shallow structure as safe as a deep one.
MAX_NODES = 20_000
#: Keys read from one object.
MAX_KEYS = 60
#: Records rendered per page.
MAX_RECORDS = 100
#: Fields kept on one record's line.
MAX_FIELDS = 24
#: Items read from one array — and hence entities from one ``ItemList``.
#: Deliberately as large as MAX_RECORDS rather than a small number: a ranked
#: list is precisely the data a head-slice must not lose (finding C1, where
#: the answer row sat at char 19,831 of 20,136), so cutting a 39-model
#: leaderboard at 20 would reintroduce the failure in a new place. The record
#: and character caps below still bound the total.
MAX_LIST_ITEMS = 100
#: One value, clipped with an ellipsis. Long enough for a spec string, short
#: enough that a 2 MB ``description`` cannot become the page.
MAX_VALUE_CHARS = 200
#: One rendered line.
MAX_LINE_CHARS = 600
#: Total characters appended to a page's text. ~3k tokens: small next to the
#: 200k-char tsvector window and the per-page vector budget, and it bounds
#: what a hostile page can push into the shared corpus.
MAX_TOTAL_CHARS = 12_000
#: Top-level ``itemscope`` elements read.
MAX_MICRODATA_SCOPES = 60
#: DOM nodes walked for microdata.
MAX_MICRODATA_NODES = 20_000

#: Dedupe: how far from a mention of the entity a value has to appear in the
#: already-extracted text before we call it "the page already said this".
#: Measured in whitespace-stripped characters, so ~400 is a long paragraph.
_DEDUPE_WINDOW = 400
#: Occurrences of an entity name probed before giving up (a name can repeat).
_DEDUPE_PROBES = 8
#: Values shorter than this are too collision-prone to dedupe on ("1", "USD").
_DEDUPE_MIN_VALUE = 3

_WS_RE = re.compile(r"\s+")
_FLAT_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_TYPE_ATTR_RE = re.compile(
    r"""\btype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I
)
_ITEMSCOPE_RE = re.compile(r"\bitemscope\b", re.I)
#: The per-line provenance marker, as it must NOT appear inside a value.
_MARKER_RE = re.compile(r"\[(jsonld|microdata)\]", re.I)
_XMLDECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)

#: The keys that name a record, in preference order. The first one present
#: leads the line, so the entity is the first thing on it.
_NAME_KEYS = (
    "name", "headline", "title", "legalName", "alternateName", "model",
    "sku", "productID", "identifier",
)
#: Never content: markup, media, styling, the JSON-LD plumbing keys, and the
#: two keys (``articleBody``, ``text``) whose value is the article prose that
#: was already extracted. ``description`` is deliberately NOT here — on a
#: JavaScript-rendered page it is sometimes the only sentence that exists,
#: and it is clipped to MAX_VALUE_CHARS like any other value and deduped
#: against the prose when the page states it twice.
_SKIP_KEY_RE = re.compile(
    r"(^@|image|logo|thumbnail|photo|avatar|icon|html|css|style|colou?r|"
    r"breadcrumb|potentialaction|mainentityofpage|speakable|"
    r"articlebody|^text$|contenturl|embedurl|sameas|^url$)",
    re.I,
)


class _Rec:
    """One embedded record, ready to render. Not part of the public API."""

    __slots__ = ("src", "type", "name", "parent", "fields")

    def __init__(self, src: str, type_: str, name: str, parent: str, fields):
        self.src = src
        self.type = type_
        self.name = name
        self.parent = parent
        self.fields = fields


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _clean(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    value = _CTRL_RE.sub("", value or "")
    # A record is written by the page, so it can contain anything — including
    # the provenance marker this module puts at the head of every line. Left
    # alone, `"name": "Widget\n[jsonld] Product: Forged"` would render a
    # second, invented attribution inside a real line. The newline itself is
    # collapsed by the whitespace pass just below (and `_line` collapses the
    # rendered line again), so a value cannot forge a LINE; this defangs the
    # marker so it cannot forge an ATTRIBUTION either.
    value = _MARKER_RE.sub(r"(\1)", value)
    value = _WS_RE.sub(" ", value).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


#: schema.org enum values are written as URLs (``.../InStock``). The URL is an
#: identifier, not a resource — it is NEVER fetched — but the last segment is
#: the readable, searchable form of the same value, so it is what we emit.
_ENUM_RE = re.compile(r"^https?://(?:www\.)?schema\.org/([A-Za-z0-9_]+)$")


def _scalar(value: Any) -> Optional[str]:
    """A JSON/microdata leaf as text, or None when it is a container.

    Booleans are checked before ints because ``bool`` IS an ``int``.
    """
    if isinstance(value, str):
        enum = _ENUM_RE.match(value.strip())
        if enum:
            return enum.group(1)
        return _clean(value) or None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _clean(str(value))
    return None


def _skip_key(key: Any) -> bool:
    return not isinstance(key, str) or not key or bool(_SKIP_KEY_RE.search(key))


def _short_type(value: Any) -> str:
    """``https://schema.org/Product`` → ``Product``; a list → its first item."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if not isinstance(value, str):
        return ""
    value = value.strip().rstrip("/")
    value = value.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return _clean(value, 60)


def _type_of(node: dict) -> str:
    return _short_type(node.get("@type") or node.get("@Type") or "")


def _name_of(node: dict) -> "tuple[str, str]":
    """(key, value) of the record's name, or ("", "")."""
    if not isinstance(node, dict):
        return "", ""
    for key in _NAME_KEYS:
        got = _scalar(node.get(key))
        if got:
            return key, _clean(got, 120)
    return "", ""


def _unwrap(node: dict) -> "tuple[dict, dict]":
    """Collapse a one-property wrapper onto the named thing it wraps.

    ``ListItem{position: 1, item: Product{name: …}}`` is how every ranked
    page writes a leaderboard. Emitting the wrapper and the product as two
    lines would put the rank on one line and the name on another — exactly
    the separation this module exists to prevent — so the wrapper's own
    scalars are handed to the inner record instead.
    """
    if _name_of(node)[1]:
        return node, {}
    inner = [
        (k, v) for k, v in node.items()
        if isinstance(v, dict) and _name_of(v)[1] and not _skip_key(k)
    ]
    if len(inner) != 1:
        return node, {}
    extras = {}
    for key, value in node.items():
        if _skip_key(key):
            continue
        got = _scalar(value)
        if got is not None:
            extras[key] = got
    return inner[0][1], extras


def _pair_units(scalars: dict) -> list:
    """Re-attach a value to its unit so the pair reads as one fact.

    schema.org splits ``2.90`` from ``USD`` and ``4.5`` from ``5``; kept apart
    they are two numbers with no relationship. The unit key is consumed, so
    it is not also emitted on its own.
    """
    lowered = {k.lower(): k for k in scalars}
    consumed: set = set()
    out: list = []
    for key, value in scalars.items():
        low = key.lower()
        if low in ("price", "lowprice", "highprice", "baseprice"):
            unit = lowered.get("pricecurrency") or lowered.get("currency")
            if unit and unit in scalars and unit != key:
                value = f"{value} {scalars[unit]}"
                consumed.add(unit)
        elif low == "value":
            unit = lowered.get("unittext") or lowered.get("unitcode")
            if unit and unit in scalars and unit != key:
                value = f"{value} {scalars[unit]}"
                consumed.add(unit)
        elif low == "ratingvalue":
            unit = lowered.get("bestrating")
            if unit and unit in scalars and unit != key:
                value = f"{value}/{scalars[unit]}"
                consumed.add(unit)
        out.append((key, value))
    return [(k, v) for k, v in out if k not in consumed]


# ---------------------------------------------------------------------------
# Object graph → records
# ---------------------------------------------------------------------------


def _collect(node: Any, parent: str, depth: int, out: list, budget: dict,
             src: str) -> None:
    """Walk a parsed JSON-LD (or microdata-shaped) object into ``out``.

    Bounded three ways at once: ``MAX_DEPTH`` (nesting), ``MAX_NODES``
    (total containers visited, shared across every block on the page) and
    ``MAX_RECORDS`` (lines). A cyclic structure cannot occur — ``json.loads``
    builds a tree — but a self-referential *shape* (``isPartOf`` repeating the
    same object) still terminates on depth.
    """
    if depth > MAX_DEPTH or len(out) >= MAX_RECORDS:
        return
    budget["nodes"] = budget.get("nodes", 0) + 1
    if budget["nodes"] > MAX_NODES:
        return
    if isinstance(node, list):
        for item in node[:MAX_LIST_ITEMS]:
            _collect(item, parent, depth + 1, out, budget, src)
        return
    if not isinstance(node, dict):
        return

    graph = node.get("@graph")
    if isinstance(graph, (list, dict)):
        # A @graph wrapper carries only @context/@graph; its members are the
        # records.
        _collect(graph, parent, depth + 1, out, budget, src)
        return

    node, extras = _unwrap(node)
    type_ = _type_of(node)
    name_key, name = _name_of(node)
    scalars: dict = dict(extras)
    children: list = []

    for index, (key, value) in enumerate(node.items()):
        if index >= MAX_KEYS:
            break
        budget["nodes"] = budget.get("nodes", 0) + 1
        if budget["nodes"] > MAX_NODES:
            break
        if key == name_key or _skip_key(key):
            continue
        got = _scalar(value)
        if got is not None:
            scalars.setdefault(key, got)
            continue
        if isinstance(value, list):
            items = value[:MAX_LIST_ITEMS]
            flat = [s for s in (_scalar(v) for v in items) if s]
            if flat:
                scalars.setdefault(key, _clean("; ".join(flat)))
            children.extend(v for v in items if isinstance(v, dict))
            continue
        if isinstance(value, dict):
            inner_name = _name_of(value)[1]
            inner_scalars = [
                k for k, v in value.items()
                if _scalar(v) is not None and not _skip_key(k)
            ]
            inner_nested = any(
                isinstance(v, (dict, list)) for v in value.values()
            )
            if inner_name and len(inner_scalars) <= 1 and not inner_nested:
                # brand: {"@type": "Brand", "name": "NVIDIA"} → brand: NVIDIA
                scalars.setdefault(key, inner_name)
            elif not inner_name and _unwrap(value)[0] is value:
                # An ANONYMOUS wrapper — offers, aggregateRating,
                # priceSpecification. It has no identity of its own, so its
                # fields belong to the entity that owns it. This is what
                # turns {"name": "H100", "offers": {"price": "2.90",
                # "priceCurrency": "USD"}} into "H100 — price: 2.90 USD"
                # rather than two disconnected lines.
                for inner_index, (k2, v2) in enumerate(value.items()):
                    if inner_index >= MAX_KEYS:
                        break
                    if _skip_key(k2):
                        continue
                    s2 = _scalar(v2)
                    if s2 is not None:
                        scalars.setdefault(k2, s2)
                for v2 in value.values():
                    if isinstance(v2, dict):
                        children.append(v2)
                    elif isinstance(v2, list):
                        children.extend(
                            x for x in v2[:MAX_LIST_ITEMS]
                            if isinstance(x, dict)
                        )
            else:
                children.append(value)

    fields = _pair_units(scalars)[:MAX_FIELDS]
    # A NESTED record with a name but no fields of its own says nothing the
    # parent's line does not already carry (an `isPartOf` chain repeating the
    # same title, say), so only a top-level entity is worth a bare name.
    if fields or (name and depth == 0):
        out.append(_Rec(src, type_, name, "" if parent == name else parent,
                        fields))
    label = name or parent
    for child in children:
        if len(out) >= MAX_RECORDS:
            break
        _collect(child, label, depth + 1, out, budget, src)


# ---------------------------------------------------------------------------
# 1: JSON-LD
# ---------------------------------------------------------------------------


def _unwrap_script_text(raw: str) -> str:
    """Strip the CDATA / HTML-comment wrappers some CMSes emit around JSON."""
    raw = raw.strip()
    if raw.startswith("<!--"):
        raw = raw[4:]
        if raw.rstrip().endswith("-->"):
            raw = raw.rstrip()[:-3]
        raw = raw.strip()
    if raw.startswith("//"):  # `//<![CDATA[`
        first, _, rest = raw.partition("\n")
        if "CDATA" in first:
            raw = rest.strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
        if raw.rstrip().endswith("]]>"):
            raw = raw.rstrip()[:-3]
        raw = raw.strip()
    if raw.endswith("]]>"):
        raw = raw[:-3].rstrip()
        if raw.endswith("//"):
            raw = raw[:-2].rstrip()
    return raw


def _jsonld_records(html: str, stats: dict, budget: dict) -> list:
    """Every ``<script type="application/ld+json">`` block, bounded.

    Read from the raw HTML with a regex rather than from a DOM on purpose:
    the augmentation pass already pays for one lxml parse and this avoids a
    second one on the common page. The HTML spec requires a script's own
    ``</script>`` to be escaped, so the non-greedy match is correct; a page
    that violates it yields invalid JSON and is counted as malformed.
    """
    out: list = []
    scripts = blocks = total = 0
    for match in _SCRIPT_RE.finditer(html):
        scripts += 1
        if scripts > MAX_SCRIPT_ELEMENTS:
            break
        attrs = match.group(1) or ""
        found = _TYPE_ATTR_RE.search(attrs)
        if not found:
            continue
        ctype = found.group(1) or found.group(2) or found.group(3) or ""
        if "ld+json" not in ctype.lower():
            continue
        blocks += 1
        if blocks > MAX_BLOCKS:
            stats["blocks_skipped"] = stats.get("blocks_skipped", 0) + 1
            break
        raw = match.group(2) or ""
        if len(raw) > MAX_BLOCK_CHARS:
            stats["oversized"] = stats.get("oversized", 0) + 1
            continue
        total += len(raw)
        if total > MAX_TOTAL_BLOCK_CHARS:
            stats["blocks_skipped"] = stats.get("blocks_skipped", 0) + 1
            break
        try:
            data = json.loads(
                _unwrap_script_text(raw),
                parse_float=str, parse_int=str, parse_constant=str,
            )
        except Exception:  # noqa: BLE001 — malformed input is expected input
            # ValueError for bad JSON, RecursionError for a block nested past
            # the interpreter's limit. Counted, never raised: a broken record
            # must not cost the caller the page's text.
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        stats["parsed"] = stats.get("parsed", 0) + 1
        _collect(data, "", 0, out, budget, "jsonld")
        if len(out) >= MAX_RECORDS:
            break
    stats["blocks"] = blocks
    return out


# ---------------------------------------------------------------------------
# 2: microdata
# ---------------------------------------------------------------------------

_MD_ATTR_VALUE = {
    "meta": "content",
    "audio": "src", "embed": "src", "iframe": "src", "img": "src",
    "source": "src", "track": "src", "video": "src",
    "a": "href", "area": "href", "link": "href",
    "object": "data",
    "data": "value", "meter": "value",
    "time": "datetime",
}


def _md_value(el) -> str:
    """A microdata property's value, per the attribute rules of the spec.

    ``<time datetime="2026-03-04">March 4</time>`` yields ``2026-03-04`` —
    the machine-readable form the page itself stated. Nothing is reformatted.
    """
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    attr = _MD_ATTR_VALUE.get(tag)
    if attr:
        got = el.get(attr)
        if got:
            return _clean(got)
    return _clean(" ".join(el.itertext()))


def _md_props(el, acc: list, budget: dict, depth: int) -> None:
    """Collect this scope's own itemprops, not a nested scope's."""
    if depth > 32:
        return
    for child in el:
        if not isinstance(child.tag, str):
            continue
        budget["md_nodes"] = budget.get("md_nodes", 0) + 1
        if budget["md_nodes"] > MAX_MICRODATA_NODES:
            return
        prop = child.get("itemprop")
        if child.get("itemscope") is not None:
            if prop:
                acc.append((prop, child))  # a nested record, as a value
            continue  # its properties belong to IT, not to us
        if prop:
            acc.append((prop, _md_value(child)))
        _md_props(child, acc, budget, depth + 1)


def _md_object(el, depth: int, budget: dict) -> dict:
    """One ``itemscope`` element as the same dict shape JSON-LD produces.

    Rendering, unit pairing, folding and every bound are then shared with the
    JSON-LD path — one code path, one set of limits.
    """
    obj: dict = {}
    itemtype = _short_type(el.get("itemtype") or "")
    if itemtype:
        obj["@type"] = itemtype
    if depth > MAX_DEPTH:
        return obj
    props: list = []
    _md_props(el, props, budget, 0)
    for prop, value in props[:MAX_KEYS]:
        if not isinstance(prop, str) or not prop.strip():
            continue
        prop = _clean(prop, 60)
        if not isinstance(value, str):
            value = _md_object(value, depth + 1, budget)
            if not value:
                continue
        elif not value:
            continue
        if prop in obj:
            existing = obj[prop]
            if isinstance(existing, list):
                if len(existing) < MAX_LIST_ITEMS:
                    existing.append(value)
            else:
                obj[prop] = [existing, value]
        else:
            obj[prop] = value
    return obj


def _microdata_records(html: str, stats: dict, budget: dict) -> list:
    """Top-level ``itemscope`` subtrees, bounded.

    The lxml parse is paid for ONLY when the page actually carries the
    attribute — a substring probe first, so a page without microdata (the
    overwhelming majority) costs one regex scan and nothing else.
    """
    if not _ITEMSCOPE_RE.search(html):
        return []
    import lxml.html  # a trafilatura dependency; present wherever it is

    try:
        root = lxml.html.fromstring(html)
    except ValueError:  # "Unicode strings with encoding declaration…"
        root = lxml.html.fromstring(_XMLDECL_RE.sub("", html, count=1))

    out: list = []
    scopes = 0
    for el in root.iter():
        budget["md_nodes"] = budget.get("md_nodes", 0) + 1
        if budget["md_nodes"] > MAX_MICRODATA_NODES:
            break
        if not isinstance(el.tag, str) or el.get("itemscope") is None:
            continue
        parent = el.getparent()
        nested = False
        depth = 0
        while parent is not None and depth < 64:
            if isinstance(parent.tag, str) and parent.get("itemscope") is not None:
                nested = True  # reached as a property of its own scope
                break
            parent = parent.getparent()
            depth += 1
        if nested:
            continue
        scopes += 1
        if scopes > MAX_MICRODATA_SCOPES:
            break
        obj = _md_object(el, 0, budget)
        if obj:
            _collect(obj, "", 0, out, budget, "microdata")
        if len(out) >= MAX_RECORDS:
            break
    stats["microdata_scopes"] = scopes
    return out


# ---------------------------------------------------------------------------
# Rendering + dedupe against the text already extracted
# ---------------------------------------------------------------------------


def _flat(value: str) -> str:
    """Whitespace-blind, case-blind identity — the same trick the structural
    augmentation uses, so a line break or a missing separator in the prose
    does not make an already-stated fact look new."""
    return _FLAT_RE.sub("", value or "").casefold()


def _value_keys(value: str) -> list:
    """What to look for in the prose: the whole value, and its bare number.

    Unit pairing has already turned ``2.90`` + ``USD`` into ``2.90 USD``,
    but the prose writes ``$2.90 per GPU-hour`` — so the leading token is
    probed as well, or the pairing would defeat the dedupe it precedes.
    """
    keys = []
    whole = _flat(value)
    if len(whole) >= _DEDUPE_MIN_VALUE:
        keys.append(whole)
    head = re.split(r"[\s/]+", (value or "").strip(), 1)[0]
    head_key = _flat(head)
    if len(head_key) >= _DEDUPE_MIN_VALUE and head_key != whole:
        keys.append(head_key)
    return keys


def _already_stated(flat_text: str, anchors: list, value: str) -> bool:
    """True when ``value`` appears near a mention of the entity in the prose.

    Proximity, not bare containment: "2.90" appearing SOMEWHERE on a page is
    not evidence that the page stated this product's price, and dropping the
    field on that basis would destroy the association this module exists to
    preserve. It has to appear within ``_DEDUPE_WINDOW`` of the entity's name.
    """
    keys = _value_keys(value)
    if not keys or not flat_text:
        return False
    for anchor in anchors:
        start = 0
        for _ in range(_DEDUPE_PROBES):
            at = flat_text.find(anchor, start)
            if at < 0:
                break
            window = flat_text[
                max(0, at - _DEDUPE_WINDOW): at + len(anchor) + _DEDUPE_WINDOW
            ]
            if any(key in window for key in keys):
                return True
            start = at + len(anchor)
    return False


def _anchors(name: str) -> list:
    """Where in the prose to look for this entity: its whole name, plus its
    longest distinctive words (a page writes "H100 80GB" where the record
    says "H100 80GB SXM")."""
    whole = _flat(name)
    if not whole:
        return []
    out = [whole]
    words = sorted(
        (w for w in re.split(r"[\s,/]+", name) if len(w) >= 4),
        key=len, reverse=True,
    )
    for word in words[:3]:
        key = _flat(word)
        if key and key != whole:
            out.append(key)
    return out


def _line(rec: _Rec, fields: list) -> str:
    head = f"{rec.type}: {rec.name}" if rec.type and rec.name else (
        rec.type or rec.name
    )
    if rec.parent and rec.parent != rec.name:
        head = f"{rec.parent} › {head}"
    body = "; ".join(f"{k}: {v}" for k, v in fields)
    line = f"[{rec.src}] {head} — {body}" if body else f"[{rec.src}] {head}"
    line = _CTRL_RE.sub("", line)
    line = _WS_RE.sub(" ", line).strip()
    if len(line) > MAX_LINE_CHARS:
        line = line[: MAX_LINE_CHARS - 1].rstrip() + "…"
    return line


def _render(records: list, text: str, stats: dict) -> list:
    flat_text = _flat(text)
    lines: list = []
    seen: set = set()
    total = 0
    for rec in records[:MAX_RECORDS]:
        anchors = _anchors(rec.name)
        kept: list = []
        for key, value in rec.fields:
            if anchors and _already_stated(flat_text, anchors, value):
                stats["fields_deduped"] = stats.get("fields_deduped", 0) + 1
                continue
            if not anchors and _flat(f"{key}{value}") in flat_text:
                stats["fields_deduped"] = stats.get("fields_deduped", 0) + 1
                continue
            kept.append((key, value))
        if not kept:
            # Nothing new. Emit the bare entity only when the page's prose
            # never names it at all.
            if not rec.name or _flat(rec.name) in flat_text:
                stats["records_deduped"] = stats.get("records_deduped", 0) + 1
                continue
        line = _line(rec, kept)
        key = _flat(line)
        if key in seen:
            stats["records_deduped"] = stats.get("records_deduped", 0) + 1
            continue
        if total + len(line) + 1 > MAX_TOTAL_CHARS:
            stats["truncated"] = stats.get("truncated", 0) + 1
            break
        seen.add(key)
        lines.append(line)
        total += len(line) + 1
    stats["lines"] = len(lines)
    return lines


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def embedded_records(
    html: str, text: str = "", stats: Optional[dict] = None
) -> list:
    """JSON-LD + microdata records on ``html``, as lines to append to ``text``.

    Never raises and never returns anything the page did not state. ``stats``
    (optional, filled in place) carries the counters — blocks seen, blocks
    parsed, malformed, oversized, fields and records deduped — so a page that
    silently yields nothing is still diagnosable.
    """
    st = stats if stats is not None else {}
    if not html or len(html) > MAX_HTML_CHARS:
        st["skipped_page"] = 1
        return []
    budget: dict = {"nodes": 0, "md_nodes": 0}
    records: list = []
    try:
        records.extend(_jsonld_records(html, st, budget))
    except Exception:  # noqa: BLE001 — recovery is an extra, never a gate
        log.debug("embedded records: json-ld pass failed", exc_info=True)
    try:
        if len(records) < MAX_RECORDS:
            records.extend(_microdata_records(html, st, budget))
    except Exception:  # noqa: BLE001
        log.debug("embedded records: microdata pass failed", exc_info=True)
    if not records:
        return []
    try:
        return _render(records, text or "", st)
    except Exception:  # noqa: BLE001
        log.debug("embedded records: render failed", exc_info=True)
        return []
