"""Embedded machine-readable records: JSON-LD and microdata (2026-09-07).

The last unclosed piece of the C2/K4 extraction work. `core/extract` stripped
`<script>` before anything looked at it, so `<script type="application/ld+json">`
— frequently the ONLY machine-readable place a page states a price, a date, a
rating or a spec — was discarded, and microdata attributes were never read.

Every fixture here is written inline and by hand, and every expectation is
derived from the fixture text, never captured from a run. Three properties are
under test, in this order of importance:

  1. THE ASSOCIATION SURVIVES. A price must arrive attached to the thing it
     prices. `H100`, `2.90` and `USD` as loose tokens is worse than nothing,
     because a query-centred window can then put a number beside the wrong
     entity.
  2. NOTHING IS EXECUTED AND NOTHING IS FETCHED. This is third-party data on
     the request path: json.loads only, and no `@context` (or any other URL in
     the record) is ever resolved.
  3. THE BOUNDS HOLD AND NOTHING PRE-EXISTING MOVED. Malformed, enormous and
     deeply nested input is skipped without raising, and a page with no
     embedded records extracts byte-identically to what it did before.

No database, no network, no LLM — extraction is a pure function over bytes.
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import time

import pytest

from app.core import extract, structured

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "web_eval")


def _text(html: str) -> str:
    return extract.extract_readable(
        "text/html", html.encode(), "https://fixtures.invalid/p"
    ).text


def _lines(html: str, text: str = "", stats=None) -> list:
    return structured.embedded_records(html, text, stats)


def _script(payload: str, quote: str = '"') -> str:
    return (
        f"<script type={quote}application/ld+json{quote}>{payload}</script>"
    )


#: A page whose prose says nothing about the numbers, so anything numeric in
#: the extracted text can only have come from the embedded record.
def _page(*blocks: str, prose: str = "") -> str:
    body = prose or (
        "<h1>Accelerator rentals</h1><p>Nimbus Cloud rents accelerators by "
        "the hour. Capacity varies by region and is billed per second, with "
        "no minimum term and no reservation required.</p>"
    )
    return (
        "<!doctype html><html><head><title>Nimbus</title>"
        + "".join(blocks)
        + f"</head><body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# 1. The association survives
# ---------------------------------------------------------------------------

PRODUCT = _script(json.dumps({
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "H100 80GB SXM",
    "brand": {"@type": "Brand", "name": "NVIDIA"},
    "offers": {
        "@type": "Offer",
        "price": "2.90",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
        "priceValidUntil": "2026-12-31",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5", "bestRating": "5", "ratingCount": "128",
    },
}))


def test_product_offer_reaches_the_text_with_its_entity():
    """The headline case: `{"name": "H100", "offers": {"price": "2.90",
    "priceCurrency": "USD"}}` must not degrade into scattered tokens."""
    text = _text(_page(PRODUCT))
    line = next(l for l in text.split("\n") if "H100 80GB SXM" in l)
    assert "price: 2.90 USD" in line          # value and unit, one field
    assert "availability: InStock" in line
    assert "priceValidUntil: 2026-12-31" in line
    assert "brand: NVIDIA" in line
    assert "ratingValue: 4.5/5" in line       # rating and its scale
    assert "ratingCount: 128" in line
    # …and the prose the page also carries is untouched, above it.
    assert "Nimbus Cloud rents accelerators" in text
    assert text.index("Nimbus Cloud rents") < text.index("H100 80GB SXM")


def test_a_number_never_arrives_without_the_entity_that_owns_it():
    """Two products in one block: no line may carry one product's name and
    the other's price."""
    block = _script(json.dumps([
        {"@type": "Product", "name": "A100 80GB",
         "offers": {"price": "1.70", "priceCurrency": "USD"}},
        {"@type": "Product", "name": "B200 192GB",
         "offers": {"price": "7.20", "priceCurrency": "USD"}},
    ]))
    for line in _lines(_page(block)):
        if "A100" in line:
            assert "1.70" in line and "7.20" not in line
        if "B200" in line:
            assert "7.20" in line and "1.70" not in line


def test_graph_members_each_become_their_own_record():
    block = _script(json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": "Nimbus Cloud",
             "foundingDate": "2019-04-01"},
            {"@type": "Article", "headline": "Q3 price update",
             "datePublished": "2026-08-19",
             "author": {"@type": "Person", "name": "R. Vega"}},
        ],
    }))
    lines = _lines(_page(block))
    assert any("Organization: Nimbus Cloud" in l and "2019-04-01" in l
               for l in lines)
    assert any("Article: Q3 price update" in l and "author: R. Vega" in l
               and "datePublished: 2026-08-19" in l for l in lines)


def test_several_blocks_on_one_page_are_all_read():
    """An array block, a single-object block, and a single-quoted type
    attribute — all three shapes occur in the wild."""
    lines = _lines(_page(
        _script(json.dumps([
            {"@type": "Product", "name": "A100 80GB",
             "offers": {"price": "1.70", "priceCurrency": "USD"}},
            {"@type": "Product", "name": "B200 192GB",
             "offers": {"price": "7.20", "priceCurrency": "USD"}},
        ])),
        _script(json.dumps({"@type": "Dataset", "name": "BenchLM v4",
                            "variableMeasured": "reasoning score",
                            "temporalCoverage": "2026-03"}), quote="'"),
    ))
    joined = "\n".join(lines)
    assert "A100 80GB" in joined and "1.70" in joined
    assert "B200 192GB" in joined and "7.20" in joined
    assert "Dataset: BenchLM v4" in joined and "reasoning score" in joined


def test_a_ranked_list_keeps_each_rank_on_its_entity_line():
    """`ItemList`/`ListItem` is how every leaderboard states its ranking.
    The wrapper's `position` belongs to the item it wraps — emitting them as
    two lines is the rank/name separation finding C1 is about."""
    block = _script(json.dumps({
        "@type": "ItemList", "name": "BenchLM Reasoning",
        "itemListElement": [
            {"@type": "ListItem", "position": 1,
             "item": {"@type": "Product", "name": "Aurora-Max",
                      "ratingValue": "93.4"}},
            {"@type": "ListItem", "position": 12,
             "item": {"@type": "Product", "name": "GPT-5.2",
                      "ratingValue": "82.7"}},
        ],
    }))
    lines = _lines(_page(block))
    gpt = next(l for l in lines if "GPT-5.2" in l)
    assert "position: 12" in gpt and "ratingValue: 82.7" in gpt
    assert "93.4" not in gpt                      # not the adjacent entry
    assert any("BenchLM Reasoning" in l and "Aurora-Max" in l for l in lines)


def test_a_named_child_keeps_its_parent_on_the_line():
    """When a nested entity has a name of its own it gets its own line —
    carrying the parent's name, so a chunk boundary cannot orphan it."""
    block = _script(json.dumps({
        "@type": "Product", "name": "Orbital OC-H1",
        "isRelatedTo": {"@type": "Product", "name": "Orbital OC-H2",
                        "offers": {"price": "4.20", "priceCurrency": "USD"}},
    }))
    child = next(l for l in _lines(_page(block)) if "OC-H2" in l)
    assert "Orbital OC-H1 ›" in child
    assert "price: 4.20 USD" in child


def test_microdata_is_read_with_its_units_and_its_stated_date():
    html = _page(prose=(
        "<h1>Nimbus H200</h1>"
        "<div itemscope itemtype='https://schema.org/Product'>"
        "<span itemprop='name'>H200 141GB</span>"
        "<div itemprop='offers' itemscope itemtype='https://schema.org/Offer'>"
        "<meta itemprop='priceCurrency' content='USD'>"
        "<span itemprop='price'>4.60</span>"
        "<link itemprop='availability' href='https://schema.org/InStock'>"
        "</div>"
        "<time itemprop='releaseDate' datetime='2024-11-18'>Nov 2024</time>"
        "</div>"
    ))
    line = next(l for l in _lines(html) if "H200 141GB" in l)
    assert line.startswith("[microdata] ")
    assert "price: 4.60 USD" in line
    assert "availability: InStock" in line
    # <time datetime> is the machine-readable form the PAGE stated.
    assert "releaseDate: 2024-11-18" in line


def test_dates_are_emitted_exactly_as_the_page_wrote_them():
    """Nothing here parses or normalises a date — provenance owns that."""
    block = _script(json.dumps({
        "@type": "Article", "headline": "Pricing changes",
        "datePublished": "March 4, 2026",
        "dateModified": "2026-03-18T11:30:00Z",
    }))
    line = next(l for l in _lines(_page(block)) if "Pricing changes" in l)
    assert "datePublished: March 4, 2026" in line
    assert "dateModified: 2026-03-18T11:30:00Z" in line


def test_a_json_number_keeps_the_form_the_page_wrote():
    """`"price": 2.90` is a JSON float; str(2.90) is "2.9". Losing the cent
    changes the fact, so numbers are parsed as their source text."""
    block = _script('{"@type":"Product","name":"Kestrel-XL",'
                    '"offers":{"price":2.90,"priceCurrency":"USD"}}')
    assert "price: 2.90 USD" in "\n".join(_lines(_page(block)))


def test_provenance_is_recorded_on_every_line():
    """A value from an embedded record must not read as page prose, and the
    marker is per line because chunking splits a page long before it splits
    a block."""
    text = _text(_page(PRODUCT))
    assert "Embedded structured data" in text
    appended = [l for l in text.split("\n") if "H100 80GB SXM" in l]
    assert appended and all(l.startswith("[jsonld] ") for l in appended)


# ---------------------------------------------------------------------------
# 2. Nothing is executed, nothing is fetched
# ---------------------------------------------------------------------------


def test_no_socket_is_opened_while_parsing(monkeypatch):
    """`@context`, `@id` and `sameAs` are identifiers, not resources."""
    import lxml.html  # noqa: F401  — warm the import before the guard

    def boom(*a, **k):  # pragma: no cover - the assertion is that it is unused
        raise AssertionError("structured data parsing opened a socket")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    block = _script(json.dumps({
        "@context": "https://schema.org",
        "@id": "https://nimbus.invalid/gpu/h100#product",
        "@type": "Product", "name": "H100 80GB",
        "sameAs": "https://en.wikipedia.org/wiki/Hopper",
        "offers": {"price": "3.10", "priceCurrency": "USD"},
    }))
    html = _page(block, "<span itemscope itemtype='https://schema.org/Thing'>"
                        "<span itemprop='name'>X</span></span>")
    assert "price: 3.10 USD" in "\n".join(_lines(html))


def test_active_content_in_a_record_is_data_not_behaviour():
    """A record can say anything. It is rendered as text and nothing else.

    The `</script>` is written the way the HTML spec requires a script's own
    content to write it (`<\\/script>`); the unescaped form is covered by the
    test below, because a browser ends the element there too.
    """
    block = (
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Widget",'
        '"checkout":"javascript:alert(1)",'
        '"note":"<\\/script><script>alert(2)<\\/script>"}'
        "</script>"
    )
    lines = _lines(_page(block))
    assert len(lines) == 1                    # one record, one inert line
    assert "\n" not in lines[0]
    assert "Widget" in lines[0]
    assert "javascript:alert(1)" in lines[0]  # carried as text, never run


def test_an_unescaped_closing_tag_ends_the_block_and_counts_as_malformed():
    """A browser ends the script element at a literal `</script>` too, so the
    remainder is not JSON. It must be counted, not raised — and the block
    must not silently yield half a record."""
    stats: dict = {}
    block = ('<script type="application/ld+json">'
             '{"@type":"Product","name":"Widget","note":"</script>"}'
             "</script>")
    assert _lines(_page(block), stats=stats) == []
    assert stats["malformed"] == 1


def test_a_value_cannot_forge_an_extra_line():
    """Newlines and control characters in a value are stripped, so a hostile
    record cannot inject a line that looks like a separate fact."""
    block = _script(json.dumps({
        "@type": "Product",
        "name": "Widget\n[jsonld] Product: Forged — price: 0.01 USD",
        "offers": {"price": "9.99", "priceCurrency": "USD"},
    }))
    lines = _lines(_page(block))
    assert len(lines) == 1
    assert "\n" not in lines[0] and "\x00" not in lines[0]
    # Exactly one provenance marker: the real one. The value's copy is
    # defanged, so a record cannot attribute an invented fact to itself.
    assert lines[0].count("[jsonld]") == 1
    assert "(jsonld) Product: Forged" in lines[0]
    # The real field is still the record's own, at the end of its own line.
    assert lines[0].endswith("price: 9.99 USD")
    # RESIDUAL, recorded rather than hidden: a value may still contain the
    # " — " and "; " separators and so LOOK like extra fields. It cannot
    # forge a new line or a new attribution, and whatever it says stays
    # attached to this entity, which is what the association guarantee is.


def test_the_module_contains_no_dynamic_evaluation():
    source = open(structured.__file__, encoding="utf-8").read()
    for forbidden in ("eval(", "exec(", "__import__(", "urlopen",
                      "requests.", "httpx.", "subprocess", "pickle",
                      "os.system"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 3. The bounds hold, and malformed input costs nothing
# ---------------------------------------------------------------------------


def test_malformed_json_is_skipped_counted_and_does_not_stop_the_page():
    stats: dict = {}
    html = _page(
        _script('{"@type":"Product", "name": }'),          # syntax error
        _script("not json at all"),
        _script(""),                                        # empty
        _script('{"@type":"Product","name":"Kestrel-XL",'
                '"offers":{"price":"11.25","priceCurrency":"USD"}}'),
    )
    lines = _lines(html, stats=stats)
    assert stats["malformed"] == 3
    assert stats["blocks"] == 4
    assert stats["parsed"] == 1
    assert any("Kestrel-XL" in l and "11.25" in l for l in lines)
    # And the page's own text is never at risk.
    assert "Nimbus Cloud rents accelerators" in _text(html)


def test_an_enormous_block_is_skipped_without_being_parsed():
    stats: dict = {}
    huge = ('{"@type":"Product","name":"Bloat","d":"'
            + "y" * (structured.MAX_BLOCK_CHARS + 10_000) + '"}')
    html = _page(_script(huge),
                 _script('{"@type":"Product","name":"Small","price":"1.00"}'))
    started = time.perf_counter()
    lines = _lines(html, stats=stats)
    elapsed = time.perf_counter() - started
    assert stats["oversized"] == 1
    assert not any("Bloat" in l for l in lines)
    assert any("Small" in l for l in lines)      # the next block still runs
    assert elapsed < 5.0, f"oversized skip took {elapsed:.1f}s"


def test_a_page_above_the_html_ceiling_is_declined_whole():
    stats: dict = {}
    html = "<html><body>" + "x" * (structured.MAX_HTML_CHARS + 1) + "</body>"
    assert _lines(html, stats=stats) == []
    assert stats["skipped_page"] == 1


def test_deeply_nested_input_terminates_and_never_raises():
    # (a) nesting past the interpreter's own limit: json.loads raises, we count
    stats: dict = {}
    started = time.perf_counter()
    assert _lines(_page(_script("[" * 60_000 + "]" * 60_000)),
                  stats=stats) == []
    assert stats["malformed"] == 1
    # (b) nesting inside the parser's reach: the depth cap stops the walk
    stats = {}
    chain = ('{"@type":"Thing","name":"root"'
             + ',"isPartOf":{"@type":"Thing","name":"n"' * 300
             + "}" * 300 + "}")
    lines = _lines(_page(_script(chain)), stats=stats)
    elapsed = time.perf_counter() - started
    assert stats.get("malformed", 0) == 0
    assert len(lines) <= structured.MAX_RECORDS
    assert elapsed < 5.0, f"deep input took {elapsed:.1f}s"


def test_the_appended_text_is_bounded_whatever_the_page_carries():
    products = [
        {"@type": "Product", "name": f"Model-{i}",
         "description": "d" * 400,
         "offers": {"price": f"{i}.00", "priceCurrency": "USD"}}
        for i in range(4_000)
    ]
    stats: dict = {}
    lines = _lines(_page(_script(json.dumps(products))), stats=stats)
    assert len(lines) <= structured.MAX_RECORDS
    assert all(len(l) <= structured.MAX_LINE_CHARS for l in lines)
    assert sum(len(l) + 1 for l in lines) <= structured.MAX_TOTAL_CHARS
    # …and the whole page grows by no more than that plus the one heading.
    grown = _text(_page(_script(json.dumps(products))))
    plain = _text(_page())
    assert len(grown) - len(plain) <= structured.MAX_TOTAL_CHARS + 200


def test_a_wide_microdata_page_is_bounded():
    scopes = "".join(
        f"<div itemscope itemtype='https://schema.org/Product'>"
        f"<span itemprop='name'>M{i}</span>"
        f"<span itemprop='price'>{i}.00</span></div>"
        for i in range(500)
    )
    stats: dict = {}
    lines = _lines(_page(prose=scopes), stats=stats)
    assert stats["microdata_scopes"] <= structured.MAX_MICRODATA_SCOPES + 1
    assert len(lines) <= structured.MAX_RECORDS
    assert sum(len(l) + 1 for l in lines) <= structured.MAX_TOTAL_CHARS


def test_broken_markup_never_costs_the_caller_its_text():
    broken = ("<html><body><p>" + "a real sentence here. " * 30
              + "<div itemscope itemtype='https://schema.org/Product'>"
                "<span itemprop='name'>Half")
    out = extract.extract_readable("text/html", broken.encode(), "u")
    assert "a real sentence here." in out.text
    assert _lines(broken) is not None


def test_an_unparseable_structured_pass_leaves_the_text_alone(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(structured, "embedded_records", boom)
    text = _text(_page(PRODUCT))
    assert "Nimbus Cloud rents accelerators" in text
    assert "Embedded structured data" not in text


# ---------------------------------------------------------------------------
# 4. Deduplication against the text already extracted
# ---------------------------------------------------------------------------


def test_a_price_stated_in_prose_is_not_appended_twice():
    prose = ("<h1>Nimbus price list</h1>"
             "<p>The H100 80GB rents for $3.10 per GPU-hour on demand, "
             "billed per second with no minimum term.</p>")
    block = _script(json.dumps({
        "@type": "Product", "name": "H100 80GB",
        "offers": {"price": "3.10", "priceCurrency": "USD"},
        "sku": "nb-h100",
    }))
    text = _text(_page(block, prose=prose))
    assert text.count("3.10") == 1
    assert "sku: nb-h100" in text        # the field that IS new still lands


def test_dedupe_requires_proximity_not_mere_presence():
    """"3.10" appearing somewhere on a long page is not evidence that the
    page stated THIS product's price. Dropping the field on that basis would
    destroy the association the pass exists to preserve."""
    prose = ("<h1>Nimbus</h1><p>The H100 80GB is available in three "
             "regions.</p><p>"
             + "Unrelated methodology prose. " * 60
             + "Our uptime SLA was 3.10 percent better last quarter.</p>")
    block = _script(json.dumps({
        "@type": "Product", "name": "H100 80GB",
        "offers": {"price": "3.10", "priceCurrency": "USD"},
    }))
    assert "price: 3.10 USD" in _text(_page(block, prose=prose))


def test_an_identical_record_repeated_on_the_page_is_emitted_once():
    one = {"@type": "Product", "name": "A100 80GB",
           "offers": {"price": "1.70", "priceCurrency": "USD"}}
    html = _page(_script(json.dumps(one)),
                 _script(json.dumps({"@graph": [one]})))
    lines = _lines(html)
    assert sum(1 for l in lines if "A100 80GB" in l) == 1


def test_a_record_the_prose_fully_covers_adds_nothing():
    prose = ("<h1>Nimbus</h1><p>The H100 80GB rents for $3.10 per GPU-hour, "
             "InStock in every region, and was released 2023-03-21.</p>")
    block = _script(json.dumps({
        "@type": "Product", "name": "H100 80GB",
        "releaseDate": "2023-03-21",
        "offers": {"price": "3.10", "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock"},
    }))
    text = _text(_page(block, prose=prose))
    assert "Embedded structured data" not in text


# ---------------------------------------------------------------------------
# 5. Nothing that already worked moved
# ---------------------------------------------------------------------------


def test_every_existing_web_eval_fixture_extracts_identically():
    """The acceptance criterion: a page with no embedded records must yield
    EXACTLY what it yielded before, byte for byte. None of the seven eval
    fixtures carries JSON-LD or microdata (verified below), so switching the
    pass off must change nothing at all."""
    paths = sorted(glob.glob(os.path.join(FIXTURES, "*.html")))
    assert len(paths) >= 7
    for path in paths:
        with open(path, "rb") as fh:
            body = fh.read()
        assert b"ld+json" not in body and b"itemscope" not in body, path
        url = "https://fixtures.invalid/" + os.path.basename(path)
        extract.RECOVER_EMBEDDED_RECORDS = True
        with_pass = extract.extract_readable("text/html", body, url).text
        extract.RECOVER_EMBEDDED_RECORDS = False
        try:
            without = extract.extract_readable("text/html", body, url).text
        finally:
            extract.RECOVER_EMBEDDED_RECORDS = True
        assert with_pass == without, f"{os.path.basename(path)} changed"


def test_the_negative_fixture_still_gains_no_model_and_no_score():
    """`no_answer.html` genuinely contains no model name and no number.
    Inventing either turns a correct "the evidence does not cover this" into
    a fabricated answer — worse than the loss being fixed."""
    with open(os.path.join(FIXTURES, "no_answer.html"), "rb") as fh:
        body = fh.read()
    text = extract.extract_readable(
        "text/html", body, "https://fixtures.invalid/no_answer.html"
    ).text
    with open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8") as fh:
        facts = json.load(fh)["facts"]["leaderboard"]
    for model in (k for k in facts if k not in ("_source", "model_count")):
        assert model not in text, f"invented {model} on the negative fixture"
    assert re.search(r"\d", text) is None, f"invented a number: {text!r}"


def test_the_pipe_table_rendering_is_untouched():
    with open(os.path.join(FIXTURES, "leaderboard.html"), "rb") as fh:
        body = fh.read()
    text = extract.extract_readable(
        "text/html", body, "https://fixtures.invalid/leaderboard.html"
    ).text
    rows = [line for line in text.split("\n") if "|" in line]
    assert rows[0] == (
        "| Rank | Model | Reasoning score | Cost (USD / 1M tok) | Evaluated | "
    )
    assert rows[-1] == "| 14 | Tessera-Lite | 76.1 | 0.55 | 2026-03-06 | "
    assert len(rows) == 16  # header, separator, 14 data rows


def test_the_definition_list_recovery_still_works_alongside():
    """The pass added for C2 and this one must not interfere: hosting_costs
    has a <dl> and no JSON-LD."""
    with open(os.path.join(FIXTURES, "hosting_costs.html"), "rb") as fh:
        body = fh.read()
    text = extract.extract_readable(
        "text/html", body, "https://fixtures.invalid/hosting_costs.html"
    ).text
    assert "B200 192GB: $7.20" in text
    assert "Embedded structured data" not in text


def test_the_pass_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(extract, "RECOVER_EMBEDDED_RECORDS", False)
    text = _text(_page(PRODUCT))
    assert "2.90" not in text
    assert "Embedded structured data" not in text


def test_records_are_recovered_even_when_there_is_no_readable_prose():
    """The case that matters most in the wild: a JavaScript-rendered page
    whose only machine-readable content IS its JSON-LD. `_strip_tags`, the
    fallback path, drops <script> too — so it needs this pass as much as the
    trafilatura path does."""
    shell = ("<!doctype html><html><head><title>Shop</title>"
             + PRODUCT
             + "</head><body><div id='root'></div></body></html>")
    text = extract.extract_readable(
        "text/html", shell.encode(), "https://fixtures.invalid/shell"
    ).text
    assert "H100 80GB SXM" in text and "price: 2.90 USD" in text


def test_an_xhtml_page_with_an_xml_declaration_still_yields_its_microdata():
    """An XHTML page decodes to a str that still carries its declaration, and
    lxml refuses those from a str — the same trap the augmentation pass hit."""
    html = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
        "<div itemscope itemtype='https://schema.org/Product'>"
        "<span itemprop='name'>OC-B1</span>"
        "<span itemprop='price'>6.75</span></div></body></html>"
    )
    assert any("OC-B1" in l and "6.75" in l for l in _lines(html))


def test_a_javascript_shell_that_carries_records_now_passes_the_store_gate():
    """A DELIBERATE interaction with the K9 store gate, asserted so it is a
    decision and not an accident.

    `page_quality` refuses a short "please enable JavaScript" shell because
    such a page says nothing. A shell whose JSON-LD states a hundred priced
    products is not that page — it now carries the data it was fetched for,
    and refusing it would throw away the only machine-readable copy. A shell
    with NO records is still refused, exactly as before.
    """
    empty_shell = (
        "<html><head><title>Shop</title></head><body>"
        "<noscript>Please enable JavaScript to view this store.</noscript>"
        "</body></html>"
    )
    text = _text(empty_shell)
    assert extract.page_quality(text) == (False, "js_shell")

    products = [
        {"@type": "Product", "name": f"Model-{i}",
         "offers": {"price": f"{i}.00", "priceCurrency": "USD"}}
        for i in range(200)
    ]
    with_records = empty_shell.replace(
        "</head>", _script(json.dumps(products)) + "</head>"
    )
    text = _text(with_records)
    assert "price: 7.00 USD" in text
    assert extract.page_quality(text) == (True, "")


def test_extract_version_was_bumped_for_this_change():
    """Extraction now yields something new, so every page already in
    `web_pages` was read by an extractor that discarded these records. The
    version is the ONLY mechanism that gets them re-read
    (`web_worker._due_pages` queues anything below it)."""
    assert extract.EXTRACT_VERSION == 4


@pytest.mark.parametrize("bound,value", [
    ("MAX_BLOCKS", 20), ("MAX_DEPTH", 6), ("MAX_RECORDS", 100),
    ("MAX_TOTAL_CHARS", 12_000), ("MAX_LINE_CHARS", 600),
])
def test_the_declared_bounds_are_what_the_module_uses(bound, value):
    """A bound that drifts silently is not a bound. These are the numbers the
    module's own comments justify."""
    assert getattr(structured, bound) == value
