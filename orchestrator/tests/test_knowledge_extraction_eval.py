"""Extraction, chunking and store-gate evaluation over the web_eval fixtures.

Every expectation here is derived from the fixture HTML itself (see
``fixtures/web_eval/cases.json``, whose values were transcribed by hand from
the literal text nodes) — never from what the code currently returns. Where a
case was known-failing at baseline the measured baseline is recorded in the
test, so a regression reads as a regression rather than as "the number moved".

No database, no network, no LLM: extraction and chunking are pure functions
over bytes, and the two store-time gates are pure functions over text.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from app.core import extract
from app import web_index

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "web_eval")


def _html(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def _extract(name: str) -> extract.Extracted:
    return extract.extract_readable(
        "text/html", _html(name), f"https://fixtures.invalid/{name}"
    )


def _cases() -> dict:
    with open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# C2 / K4 — structured data trafilatura discards
# ---------------------------------------------------------------------------


def test_definition_list_values_survive_extraction():
    """`definition-list-values`, known-failing at baseline.

    Measured 2026-09-06 before the fix: `hosting_costs.html` extracted to 165
    characters — the two prose paragraphs around the price list and NOT ONE of
    its four prices, identically under include_tables / favor_recall /
    no_fallback / output_format="markdown" / include_formatting /
    include_links. The page therefore read as successfully fetched and was
    citable while carrying none of its data.
    """
    text = _extract("hosting_costs.html").text
    prices = _cases()["facts"]["nimbus_pricing"]
    for accelerator, price in prices.items():
        model = accelerator.split()[0]  # "H100 80GB" -> "H100"
        assert model in text, f"{model} missing from the extracted text"
        if price is None:
            continue  # L40S: the page states its ABSENCE, there is no price
        assert f"{price:.2f}" in text, f"{model}'s price {price} was dropped"

    # And the prose the baseline did keep is still there — this is an
    # augmentation, not a replacement.
    assert "Nimbus does not offer an L40S instance." in text
    assert len(text) > 165


def test_augmentation_does_not_duplicate_what_was_already_extracted():
    """The dedupe is whitespace-blind on purpose.

    `leaderboard_cards.html` renders its 14 entries as card <div>s that
    trafilatura DOES keep. A naive "append every card" pass would store the
    whole leaderboard twice, doubling the page and its chunks.
    """
    text = _extract("leaderboard_cards.html").text
    assert text.count("Aurora-Max") == 1
    assert text.count("93.4") == 1
    assert text.count("GPT-5.2") == 1


def test_pipe_table_output_is_untouched():
    """Acceptance: `leaderboard.html`'s pipe table is byte-identical.

    The expected rows are transcribed from the fixture's <table id=ranking>,
    in document order, not captured from a previous run.
    """
    text = _extract("leaderboard.html").text
    rows = [line for line in text.split("\n") if "|" in line]
    assert rows == [
        "| Rank | Model | Reasoning score | Cost (USD / 1M tok) | Evaluated | ",
        "|---|---|---|---|---|",
        "| 1 | Aurora-Max | 93.4 | 12.00 | 2026-03-02 | ",
        "| 2 | Meridian-Pro | 92.1 | 9.50 | 2026-03-02 | ",
        "| 3 | Solaris-9 | 91.7 | 8.00 | 2026-03-03 | ",
        "| 4 | Kestrel-XL | 90.2 | 11.25 | 2026-03-03 | ",
        "| 5 | Vantage-4 | 89.9 | 7.40 | 2026-03-03 | ",
        "| 6 | Lumen-Ultra | 88.6 | 10.10 | 2026-03-04 | ",
        "| 7 | Cobalt-7B | 87.3 | 2.20 | 2026-03-04 | ",
        "| 8 | Nimbus-Turbo | 86.8 | 3.75 | 2026-03-04 | ",
        "| 9 | Perigee-2 | 85.4 | 5.60 | 2026-03-05 | ",
        "| 10 | Halcyon-Mini | 84.0 | 1.10 | 2026-03-05 | ",
        "| 11 | GPT-5 | 83.2 | 6.00 | 2026-03-05 | ",
        "| 12 | GPT-5.2 | 82.7 | 6.50 | 2026-03-06 | ",
        "| 13 | Zephyr-Compact | 79.5 | 0.90 | 2026-03-06 | ",
        "| 14 | Tessera-Lite | 76.1 | 0.55 | 2026-03-06 | ",
    ]


def test_no_answer_page_gains_no_model_and_no_score():
    """`missing-evidence` / `coverage-gap-not-absence`, the negative control.

    `no_answer.html` genuinely contains no model name and no number. A
    recovery pass that invents either would turn a correct "the evidence does
    not cover this" into a fabricated answer, which is worse than the loss it
    is fixing.
    """
    text = _extract("no_answer.html").text
    facts = _cases()["facts"]["leaderboard"]
    for model in (k for k in facts if k != "_source" and k != "model_count"):
        assert model not in text, f"invented {model} on the negative fixture"
    assert re.search(r"\d", text) is None, f"invented a number: {text!r}"


def test_card_label_and_value_are_separated():
    """`exact-score-cards`.

    The fixture writes `<span class=label>Reasoning</span><span
    class=value>93.4</span>` with nothing between the two spans, so a faithful
    extractor emits `Reasoning93.4` — one token to an embedder, and
    unsearchable for the score on its own.
    """
    text = _extract("leaderboard_cards.html").text
    assert "Reasoning 82.7" in text  # GPT-5.2's card
    assert "Cost / 1M tok $6.50" in text
    assert re.search(r"Reasoning\d", text) is None
    assert re.search(r"tok\$", text) is None


def test_long_page_extraction_is_unchanged_by_the_augmentation():
    """`exact-score-long-page` — the C1 fixture must not shift underneath it.

    Measured 2026-09-06: the model name on GPT-5.2's row sits at char 19,831
    of 20,136. The augmentation appends nothing here (every heading is already
    in the prose), so the offsets the truncation findings were measured
    against still hold exactly.
    """
    text = _extract("leaderboard_long.html").text
    assert len(text) == 20136
    assert text.index("GPT-5.2 | 82.7") == 19831


def test_provenance_dates_come_from_the_page():
    """`provenance-dates` — a regression guard, not a fix.

    These already pass; they are asserted here because the augmentation pass
    runs between the trafilatura call and the metadata read.
    """
    case = next(c for c in _cases()["cases"] if c["id"] == "provenance-dates")
    got = _extract("leaderboard.html")
    assert got.published_at == case["expect_published_at"]
    assert got.modified_at == case["expect_modified_at"]
    assert got.sitename == case["expect_sitename"]


def test_augmentation_is_fail_soft_and_bounded():
    ugly = (
        b"<html><body><h1>Shell</h1><dl><dt>A</dt><dd>1.00</dd></dl>"
        b"<p>" + b"prose that survives the precision filter. " * 20 + b"</p>"
        b"</body></html>"
    )
    assert "A: 1.00" in extract.extract_readable("text/html", ugly, "u").text

    # Broken markup must never cost the caller its text.
    broken = b"<html><body><p>" + b"a real sentence here. " * 30 + b"<dl><dt>x"
    assert extract.extract_readable("text/html", broken, "u").text

    # The pass adds at most _AUG_MAX_ADD_CHARS, whatever the page does.
    cards = "".join(
        f'<div class="c"><span>Item {i}</span><span>{i}</span></div>'
        for i in range(5000)
    )
    grid = f"<html><body><div class=g>{cards}</div></body></html>"
    grown = extract._augment_structured(grid, "unrelated prose. " * 30)
    assert len(grown) < 30 * len("unrelated prose. ") + extract._AUG_MAX_ADD_CHARS + 64


def test_augmentation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(extract, "AUGMENT_STRUCTURED", False)
    assert "7.20" not in _extract("hosting_costs.html").text


# ---------------------------------------------------------------------------
# K3 — the table header must reach every chunk of the table
# ---------------------------------------------------------------------------


def _long_pipe_table(rows: int = 400) -> str:
    header = "| Rank | Model | Reasoning score | Evaluated |"
    rule = "|---|---|---|---|"
    body = "\n".join(
        f"| {i} | Model-{i:03d} | {90 - i * 0.05:.2f} | 2026-03-{(i % 27) + 1:02d} |"
        for i in range(1, rows + 1)
    )
    return "Intro prose about the benchmark. " * 20 + "\n" + header + "\n" + rule + "\n" + body


def test_every_chunk_of_a_table_carries_its_header():
    text = _long_pipe_table()
    chunks = web_index.chunk_page(text)
    assert len(chunks) > 3, "fixture must span several chunks to be meaningful"

    data_row = re.compile(r"^\| \d+ \| Model-\d+ \|", re.M)
    with_rows = [c for c in chunks if data_row.search(c)]
    assert len(with_rows) == len(chunks)  # the table dominates the page
    for i, chunk in enumerate(with_rows):
        assert "| Rank | Model | Reasoning score | Evaluated |" in chunk, (
            f"chunk {i} holds data rows with no column names"
        )
        assert "|---|---|---|---|" in chunk


def test_the_header_is_carried_once_not_stacked():
    chunks = web_index.chunk_page(_long_pipe_table())
    for chunk in chunks:
        assert chunk.count("| Rank | Model | Reasoning score | Evaluated |") == 1


def test_prose_pages_are_chunked_exactly_as_before():
    """No table, no carry: the chunker's existing shape is untouched."""
    text = "sentence number %d. " % 0 + "some ordinary prose. " * 2000
    chunks = web_index.chunk_page(text)
    clean = text.strip()
    assert chunks[0] == clean[: web_index._CHUNK_CHARS]
    stride = web_index._CHUNK_CHARS - web_index._OVERLAP_CHARS
    assert chunks[1] == clean[stride : stride + web_index._CHUNK_CHARS]


def test_pipes_in_prose_are_not_mistaken_for_a_table():
    text = "a | b is not a table. " * 400
    chunks = web_index.chunk_page(text)
    assert chunks[0] == text.strip()[: web_index._CHUNK_CHARS]


# ---------------------------------------------------------------------------
# K9 — the store-time quality gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Qwen", "thin"),  # the live https://qwen.ai/home row, in full
        ("", "empty"),
        ("   \n  ", "empty"),
        ("You need to enable JavaScript to run this app.", "js_shell"),
        ("Please enable Javascript and reload the page.", "js_shell"),
    ],
)
def test_unusable_pages_are_refused_with_a_reason(text, reason):
    keep, got = extract.page_quality(text, "https://example.invalid/x")
    assert keep is False and got == reason


@pytest.mark.parametrize(
    "text",
    [
        # The `multi-source-negative` case: short, and the whole answer.
        "Nimbus does not offer an L40S instance. Spot capacity, where "
        "available, is billed at 40% of the on-demand price.",
        # A brief release note is a legitimate page.
        "Release 4.2.1 fixes the retry loop in the scheduler and raises the "
        "default timeout to 30 seconds. No configuration change is required.",
    ],
)
def test_short_but_real_pages_are_kept(text):
    assert extract.page_quality(text, "https://example.invalid/x") == (True, "")


def test_a_long_page_that_mentions_javascript_is_not_a_shell():
    body = (
        "This tutorial explains how to enable JavaScript in three browsers. "
    ) * 40
    assert extract.page_quality(body)[0] is True


def test_every_web_eval_fixture_passes_the_gate():
    """The gate must not reject any page these findings depend on."""
    for name in sorted(os.listdir(FIXTURES)):
        if not name.endswith(".html"):
            continue
        keep, reason = extract.page_quality(_extract(name).text, name)
        assert keep, f"{name} refused as {reason}"


# ---------------------------------------------------------------------------
# K10 — the per-page chunk ceiling, now visible
# ---------------------------------------------------------------------------


def test_the_chunk_ceiling_is_reported_rather_than_silent():
    """The subject is that the shortfall is REPORTED, not what the cap happens
    to be today.

    The ceiling is (cap - 1) strides plus one full chunk — NOT cap * CHUNK,
    which is what it looks like until the overlap is accounted for. That
    relationship is the thing worth pinning; the number it evaluates to is not.
    Asserting the literal (179,600, i.e. cap 64) made this fail the moment the
    cap moved to 256, which is a change this test has no opinion about.
    """
    cap = web_index._MAX_CHUNKS_PER_PAGE
    stride = web_index._CHUNK_CHARS - web_index._OVERLAP_CHARS
    ceiling = web_index.INDEXED_CHARS_PER_PAGE

    assert ceiling == (cap - 1) * stride + web_index._CHUNK_CHARS
    assert ceiling != cap * web_index._CHUNK_CHARS, "the overlap must be accounted for"
    assert web_index.unindexed_chars("x" * (ceiling - 1)) == 0
    assert web_index.unindexed_chars("x" * (ceiling + 5_000)) == 5_000

    # Sized against the CURRENT ceiling so the cap binds whatever it is set to.
    over = "word " * ((ceiling // 5) + 20_000)
    assert len(over) > ceiling
    chunks = web_index.chunk_page(over)
    assert len(chunks) == cap
    assert web_index.unindexed_chars(over) > 0


# ---------------------------------------------------------------------------
# K11 — the sitemap's own <lastmod>
# ---------------------------------------------------------------------------

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.invalid/a</loc><lastmod>2026-08-19</lastmod></url>
  <url><loc>https://ex.invalid/b</loc></url>
  <url>
    <loc>https://ex.invalid/c</loc>
    <lastmod>2026-08-20T11:30:00+00:00</lastmod>
    <changefreq>daily</changefreq>
  </url>
</urlset>"""


def test_sitemap_lastmod_is_parsed_per_entry():
    from app.engines import crawl

    assert crawl.parse_sitemap(SITEMAP) == [
        ("https://ex.invalid/a", "2026-08-19"),
        ("https://ex.invalid/b", ""),
        ("https://ex.invalid/c", "2026-08-20T11:30:00+00:00"),
    ]


def test_a_sitemap_without_url_elements_still_yields_its_locs():
    from app.engines import crawl

    plain = "<sitemapindex><loc>https://ex.invalid/s1.xml</loc></sitemapindex>"
    assert crawl.parse_sitemap(plain) == [("https://ex.invalid/s1.xml", "")]


def test_lastmod_is_interpreted_conservatively():
    from app.engines import crawl

    assert crawl.sitemap_lastmod_at("2026-08-19").isoformat() == (
        "2026-08-19T00:00:00+00:00"
    )
    assert crawl.sitemap_lastmod_at("2026-08-20T11:30:00+00:00").hour == 11
    assert crawl.sitemap_lastmod_at("") is None
    assert crawl.sitemap_lastmod_at("last tuesday") is None
    assert crawl.sitemap_lastmod_at("1970-01-01") is None  # before the web
    assert crawl.sitemap_lastmod_at("2199-01-01") is None  # a placeholder


def test_lastmod_fills_modified_at_only_when_the_page_states_nothing(monkeypatch):
    from app.engines import crawl

    written: list = []
    monkeypatch.setattr(
        crawl.db, "upsert_web_page", lambda **kw: written.append(kw) or {"id": 1}
    )

    dateless = extract.Extracted(title="T", text="body " * 40)
    crawl._store(
        "https://ex.invalid/a", "https://ex.invalid/a", dateless, "text/html",
        sitemap_lastmod="2026-08-19",
    )
    assert written[-1]["modified_at"].date().isoformat() == "2026-08-19"

    dated = extract.Extracted(title="T", text="body " * 40, modified_at="2026-03-18")
    crawl._store(
        "https://ex.invalid/b", "https://ex.invalid/b", dated, "text/html",
        sitemap_lastmod="2026-08-19",
    )
    assert written[-1]["modified_at"].date().isoformat() == "2026-03-18"

    crawl._store(
        "https://ex.invalid/c", "https://ex.invalid/c", dateless, "text/html",
    )
    assert written[-1]["modified_at"] is None


# ---------------------------------------------------------------------------
# V21/V22 — the extractor version, and the refresh term it feeds
# ---------------------------------------------------------------------------


#: Extractor versions that a SHIPPED build has already written into stored
#: rows. 2 came from the candidate build that was rolled back (its migration
#: V21 index still hardcodes `< 2`); 3 from this phase's structural
#: augmentation pass. Append a number here only when it has actually shipped.
_SHIPPED_EXTRACT_VERSIONS = (2, 3)


def test_extract_version_is_a_new_number():
    """Improving extraction without bumping this leaves the corpus split.

    Asserted as a PROPERTY, not a literal. The invariant is "greater than every
    version already in the wild" — reusing a number means the pages carrying it
    are never re-read, so the improvement silently never reaches them. Pinning
    the literal instead made this test fail on its own subject matter: it broke
    the moment extraction improved, which is exactly when it should pass.
    """
    assert extract.EXTRACT_VERSION > max(_SHIPPED_EXTRACT_VERSIONS), (
        f"EXTRACT_VERSION {extract.EXTRACT_VERSION} does not exceed every shipped "
        f"version {_SHIPPED_EXTRACT_VERSIONS}; stored pages at that number would "
        "never be re-extracted"
    )


def _store_page(url: str, *, version: int, retrievals: int = 0):
    """One web_pages row, then its non-upsertable columns set directly."""
    from app import db

    row = db.upsert_web_page(
        url_key=url,
        url=url,
        canonical_url=url,
        title="T",
        text="stored body " * 40,
        content_type="text/html",
        fetch_status=200,
        content_hash=url,
        extract_version=version,
    )
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET retrieval_count = %s WHERE id = %s",
            (retrievals, int(row["id"])),
        )
    return int(row["id"])


def _set(page_id: int, **columns) -> None:
    from app import db

    assignments = ", ".join(f"{name} = %s" for name in columns)
    with db.connection() as con:
        con.execute(
            f"UPDATE web_pages SET {assignments} WHERE id = %s",
            (*columns.values(), page_id),
        )


def test_a_stale_extractor_page_joins_the_refresh_queue():
    """The V21 design: an extractor improvement makes stored text stale in a
    way no deadline knows about, so those rows are re-read in demand order
    instead of by a mass recrawl."""
    from app import web_worker

    old = _store_page("ex.invalid/old", version=1, retrievals=5)
    current = _store_page("ex.invalid/current", version=extract.EXTRACT_VERSION, retrievals=99)
    # Both are scheduled well into the future: only the extractor version can
    # put either of them in the queue.
    _set(old, next_refresh_at="2099-01-01", fetched_at="2026-01-01")
    _set(current, next_refresh_at="2099-01-01", fetched_at="2026-01-01")

    due = [r["url"] for r in web_worker._due_pages(10)]
    assert due == ["ex.invalid/old"]


def test_the_stale_extractor_term_cannot_spin_on_one_page():
    """A page re-read moments ago must not come straight back.

    The version is written by `upsert_web_page`'s callers; a caller that
    forgets it would otherwise re-offer the same eight rows every cycle
    forever, at a budget of 8 pages per cycle.
    """
    from app import web_worker

    page = _store_page("ex.invalid/just-read", version=0, retrievals=5)
    _set(page, next_refresh_at="2099-01-01")  # fetched_at is now()
    assert web_worker._due_pages(10) == []

    _set(page, fetched_at="2026-01-01")
    assert [r["url"] for r in web_worker._due_pages(10)] == ["ex.invalid/just-read"]


def test_a_failing_stale_page_backs_off_instead_of_holding_the_budget():
    from app import web_worker

    page = _store_page("ex.invalid/broken", version=0, retrievals=5)
    _set(page, next_refresh_at="2099-01-01", fetched_at="2026-01-01", refresh_failures=1)
    assert web_worker._due_pages(10) == []


def test_the_deadline_queue_still_works_and_leads_by_demand():
    from app import web_worker

    quiet = _store_page("ex.invalid/quiet", version=extract.EXTRACT_VERSION, retrievals=1)
    busy = _store_page("ex.invalid/busy", version=extract.EXTRACT_VERSION, retrievals=50)
    _set(quiet, next_refresh_at="2020-01-01")
    _set(busy, next_refresh_at="2020-01-01")
    assert [r["url"] for r in web_worker._due_pages(10)] == [
        "ex.invalid/busy",
        "ex.invalid/quiet",
    ]


def test_a_crawled_page_records_the_extractor_that_read_it(monkeypatch):
    from app.engines import crawl

    written: list = []
    monkeypatch.setattr(
        crawl.db, "upsert_web_page", lambda **kw: written.append(kw) or {"id": 1}
    )
    crawl._store(
        "https://ex.invalid/a",
        "https://ex.invalid/a",
        extract.Extracted(title="T", text="body " * 40),
        "text/html",
    )
    assert written[-1]["extract_version"] == extract.EXTRACT_VERSION


def test_the_queue_survives_a_database_without_the_v22_column(monkeypatch, caplog):
    """Before the migration lands, refreshing must not stop dead.

    The fallback is deliberately narrow — only PostgreSQL saying that exact
    column is missing — so a real database fault still surfaces instead of
    being downgraded to "run the old query".
    """
    from app import web_worker

    monkeypatch.setattr(web_worker, "_HAS_EXTRACT_VERSION", True)
    monkeypatch.setattr(
        web_worker,
        "_DUE_SQL_V21",
        web_worker._DUE_SQL_V21.replace("extract_version", "extract_version_absent"),
    )
    page = _store_page("ex.invalid/due", version=0, retrievals=3)
    _set(page, next_refresh_at="2020-01-01")

    with caplog.at_level("WARNING"):
        due = [r["url"] for r in web_worker._due_pages(10)]
    assert due == ["ex.invalid/due"]  # the deadline term still answers
    assert web_worker._HAS_EXTRACT_VERSION is False
    assert "extract_version" in caplog.text

    # A fault that is NOT the missing column must propagate.
    monkeypatch.setattr(web_worker, "_HAS_EXTRACT_VERSION", True)
    monkeypatch.setattr(
        web_worker, "_DUE_SQL_V21", "SELECT no_such_column FROM web_pages LIMIT %s %s %s"
    )
    with pytest.raises(Exception):
        web_worker._due_pages(10)
