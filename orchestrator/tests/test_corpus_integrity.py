"""Corpus integrity: a purge that finishes, and corroboration that is earned.

Three ledger items, one file, because they are one property: what the store
says it holds must be what it can actually serve, and what it counts as
confirmation must actually be confirmation.

K7  A purge deletes rows in PostgreSQL and vectors in LanceDB. It used to
    delete only the first half unless an operator remembered `--drop-vectors`,
    leaving chunks that `web_index.retrieve` still answers from for pages that
    no longer exist. Dropping them is the default now; `--keep-vectors` is the
    deliberate exception, and a vector delete that FAILS fails the command.
    `web_index.retrieve` additionally asks PostgreSQL which pages may still be
    served, because `crawl.site_hits_for` reads the index with no round trip
    of its own and would otherwise quote a quarantined or deleted page.

R11 Near-duplicate detection fingerprinted the page OPENING, so an aggregator
    that rewrote the lede registered as a distinct source and lifted
    `independent` corroboration — worth +0.25 confidence in the resolver.

R12 `is_primary` asks who wrote a page and has no way to ask who it is ABOUT,
    so a vendor's own page about the vendor collected every first-hand bonus.

Everything here is offline: a fake embedder, a temporary LanceDB directory and
the conftest test database. Nothing reaches the live corpus, and the SALESFORCE
directory is asserted untouched rather than assumed so.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from typing import List

import pytest

from app import db, llm, web_index
from app.config import settings
from app.core import provenance as p
from app.engines import deep_research as dr
from app.engines.search import _Source
from app.freshness import Freshness, Verdict
from tools import knowledge_admin, reindex_web

LONG = ("The office holder is named in this paragraph. " * 80).strip()
OTHER = ("A different page about a different subject entirely. " * 80).strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_embed(monkeypatch):
    """A deterministic 4-dim embedder: no vLLM, no GPU, no network."""

    async def embed(texts, **_kwargs):
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)

    async def embed_query(text, **_kwargs):
        return [float(len(text) % 7), 1.0, 0.5, 0.25]

    monkeypatch.setattr(llm, "embed_query", embed_query)


@pytest.fixture()
def salesforce_dir(tmp_path, monkeypatch):
    """A stand-in for the CRM corpus that DOES NOT EXIST.

    `/data/lancedb` is the Salesforce index — a different directory, a
    different table, a different engine's citations. Every test in this file
    points `LANCEDB_DIR` at a path under tmp_path that was never created, so
    "the Salesforce corpus was not touched" is checkable: if any code path
    below opened or wrote it, the directory would exist afterwards. LanceDB
    creates on connect, so this is not a weak assertion.
    """
    crm = str(tmp_path / "lancedb-salesforce")
    monkeypatch.setattr(settings, "lancedb_dir", crm)
    assert not os.path.exists(crm)
    return crm


@pytest.fixture()
def web_dir(tmp_path, monkeypatch):
    """An isolated LIVE web index directory, nowhere near /data."""
    live = str(tmp_path / "lancedb-web")
    monkeypatch.setattr(settings, "lancedb_web_dir", live)
    return live


def _page(url: str, text: str, *, origin: str = "search", introducer=None, title="Page") -> int:
    key = url.replace("https://", "").replace("http://", "")
    return int(
        db.upsert_web_page(
            url_key=key, url=url, canonical_url=url, title=title, text=text,
            content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha1(text.encode()).hexdigest(),
            origin=origin, introduced_by_user_id=introducer,
        )["id"]
    )


def _run(argv: List[str], capsys):
    rc = knowledge_admin.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _index_rows() -> List[dict]:
    """Every row of the live web index, straight from LanceDB."""
    _conn, table, _meta = web_index._open()
    if table is None:
        return []
    return table.search().limit(10_000).to_list()


def _built_index(tmp_path, monkeypatch) -> str:
    """Build the web index from whatever is in PostgreSQL and make it live."""
    out = str(tmp_path / "built")
    report = asyncio.run(reindex_web.build(out, progress_every=0))
    assert report.validated, report.problems
    monkeypatch.setattr(settings, "lancedb_web_dir", out)
    return out


# ---------------------------------------------------------------------------
# K7 — a purge that finishes
# ---------------------------------------------------------------------------


def test_a_purge_drops_the_pages_vectors_without_being_asked(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    """The K7 acceptance: purge a page, then no chunk carries its page_id.

    `--drop-vectors` was opt-in, so this was the DEFAULT outcome of a purge:
    the row gone, the vectors still answering.
    """
    doomed = _page("https://shared.example/a", LONG, origin="share", introducer=7)
    kept = _page("https://other.example/c", OTHER, introducer=9)
    _built_index(tmp_path, monkeypatch)
    before = _index_rows()
    assert {r["page_id"] for r in before} == {doomed, kept}

    rc, out, _err = _run(["purge", "--introducer", "7", "--yes"], capsys)

    assert rc == 0, out
    assert "deleted web_pages=1" in out
    assert "web index: removed 2 chunk row(s); 2 remain" in out
    rows = _index_rows()
    assert doomed not in {r["page_id"] for r in rows}, "orphan vectors survived the purge"
    assert {r["page_id"] for r in rows} == {kept}
    assert not os.path.exists(salesforce_dir)


def test_keep_vectors_is_the_deliberate_exception_and_says_so(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    doomed = _page("https://shared.example/a", LONG, origin="share", introducer=7)
    kept = _page("https://other.example/c", OTHER, introducer=9)
    _built_index(tmp_path, monkeypatch)

    rc, out, _err = _run(["purge", "--introducer", "7", "--keep-vectors", "--yes"], capsys)

    assert rc == 0, out
    assert "WARNING: --keep-vectors" in out
    assert "reindex_web build" in out
    assert {r["page_id"] for r in _index_rows()} == {doomed, kept}
    assert not os.path.exists(salesforce_dir)


def test_drop_vectors_is_still_accepted_and_is_now_a_no_op(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    """An operator's runbook must not start failing on an argparse error."""
    doomed = _page("https://shared.example/a", LONG, origin="share", introducer=7)
    _built_index(tmp_path, monkeypatch)

    rc, out, _err = _run(["purge", "--introducer", "7", "--drop-vectors", "--yes"], capsys)

    assert rc == 0, out
    assert doomed not in {r["page_id"] for r in _index_rows()}


def test_a_failed_vector_delete_fails_the_purge(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    """The rows are gone by then, so exit 0 would report a clean purge over
    exactly the orphaned state K7 describes."""
    _page("https://shared.example/a", LONG, origin="share", introducer=7)
    _built_index(tmp_path, monkeypatch)

    def boom(_ids):
        raise RuntimeError("lance table is locked")

    monkeypatch.setattr(web_index, "delete_pages", boom)
    rc, out, err = _run(["purge", "--introducer", "7", "--yes"], capsys)

    assert rc == 1
    assert "deleted web_pages=1" in out
    assert "vectors NOT removed" in err and "lance table is locked" in err
    assert "reindex_web build" in err


def test_the_web_index_refuses_a_directory_that_overlaps_the_crm_corpus(
    tmp_path, monkeypatch, salesforce_dir
):
    """LANCEDB_WEB_DIR is an environment variable, and a typo in it would
    point every write in web_index — deletes included — at Salesforce."""
    for bad in (salesforce_dir, os.path.join(salesforce_dir, "web"), os.path.dirname(salesforce_dir)):
        monkeypatch.setattr(settings, "lancedb_web_dir", bad)
        with pytest.raises(RuntimeError, match="SALESFORCE"):
            web_index._open()
        with pytest.raises(RuntimeError, match="SALESFORCE"):
            web_index.delete_pages([1])
    assert not os.path.exists(salesforce_dir)


def test_purging_never_opens_the_salesforce_directory(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    """LanceDB creates a directory on connect, so a path that still does not
    exist is proof that nothing connected to it."""
    _page("https://shared.example/a", LONG, origin="share", introducer=7)
    _page("https://other.example/c", OTHER, introducer=9)
    _built_index(tmp_path, monkeypatch)
    _run(["pages", "--domain", "shared.example"], capsys)
    _run(["quarantine", "--url", "https://shared.example/a", "--yes"], capsys)
    _run(["unquarantine", "--url", "https://shared.example/a", "--yes"], capsys)
    _run(["purge", "--introducer", "7", "--yes"], capsys)

    assert not os.path.exists(salesforce_dir)
    assert settings.lancedb_table != web_index.TABLE


# ---------------------------------------------------------------------------
# K7 — the other half: what retrieval will serve
# ---------------------------------------------------------------------------


def test_retrieve_will_not_serve_a_chunk_whose_page_is_gone(
    tmp_path, monkeypatch, fake_embed, web_dir, salesforce_dir
):
    """An orphan is not a stale statistic. `crawl.site_hits_for` renders the
    chunk text straight into an answer, so an orphan is a citation to a page
    that was deleted — and with no row left, nothing can even date it."""
    orphan = _page("https://shared.example/a", LONG, introducer=7)
    kept = _page("https://other.example/c", OTHER, introducer=9)
    _built_index(tmp_path, monkeypatch)
    assert {r["page_id"] for r in _index_rows()} == {orphan, kept}

    # Delete the row WITHOUT touching the vectors: `purge --keep-vectors`, or
    # any purge run before this change.
    with db.connection() as con:
        con.execute("DELETE FROM web_pages WHERE id = %s", (orphan,))

    hits = asyncio.run(web_index.retrieve("the office holder", top_k=6))
    assert [h["page_id"] for h in hits] == [kept]
    assert {r["page_id"] for r in _index_rows()} == {orphan, kept}, "vectors untouched"


def test_retrieve_will_not_serve_a_quarantined_page(
    tmp_path, monkeypatch, capsys, fake_embed, web_dir, salesforce_dir
):
    """Quarantine is reversible and leaves the vectors in place on purpose,
    which only works if every retrieval path honours it. `web_memory` does it
    in SQL; this path had no PostgreSQL round trip at all."""
    hidden = _page("https://shared.example/a", LONG, introducer=7)
    kept = _page("https://other.example/c", OTHER, introducer=9)
    _built_index(tmp_path, monkeypatch)

    rc, out, _err = _run(["quarantine", "--url", "https://shared.example/a", "--yes"], capsys)
    assert rc == 0 and "quarantined 1 page(s)" in out

    hits = asyncio.run(web_index.retrieve("the office holder", top_k=6))
    assert [h["page_id"] for h in hits] == [kept]

    rc, out, _err = _run(["unquarantine", "--url", "https://shared.example/a", "--yes"], capsys)
    assert rc == 0 and "unquarantined 1 page(s)" in out
    back = asyncio.run(web_index.retrieve("the office holder", top_k=6))
    assert hidden in {h["page_id"] for h in back}


def test_the_url_selector_finds_the_page_an_operator_is_holding(capsys):
    """An operator arrives with a URL out of a citation panel, not a page id."""
    a = _page("https://shared.example/a", LONG, introducer=7)
    _page("https://shared.example/b", OTHER, introducer=7)

    rc, out, _err = _run(["pages", "--url", "https://shared.example/a"], capsys)
    assert rc == 0 and "web_pages: 1" in out and f"{a:>7}" in out

    rc, _out, err = _run(["quarantine", "--yes"], capsys)
    assert rc == 2 and "--id, --url, --domain, --introducer" in err


# ---------------------------------------------------------------------------
# R11 — a rewrite is still the same document
#
# The fixtures are three real shapes of the same event: the wire story, an
# aggregator that put its own lede and its own first third in front of the
# wire body, and a genuinely independent report that quotes the same official
# statement. Measured with these exact strings on 2026-09-06:
#
#     pair                     shared  jaccard  containment   before  after
#     wire ~ aggregator-copy      250    0.391        0.568    MISSED  dup
#     wire ~ independent           31    0.052        0.166    ok      ok
#     wire ~ unrelated              0    0.000        0.000    ok      ok
# ---------------------------------------------------------------------------

_WIRE_BODY = """
The regulator said the new capital requirements would take effect in stages, with the
first tranche applying from the start of the next financial year and the remainder
phased in over the following twenty-four months. Institutions holding more than fifty
billion in assets will be required to maintain an additional buffer of one and a half
percentage points above the existing minimum, while smaller lenders face a reduced
supplement of half a percentage point.
Officials described the package as the most significant revision of the framework since
it was introduced, and said it responded to weaknesses exposed during the market
turbulence of the previous spring. "The evidence we gathered showed that liquidity
assumptions embedded in the old rules were optimistic, and that firms were able to
satisfy them while remaining exposed to a rapid outflow of deposits," the deputy
governor said in a statement accompanying the consultation response.
Industry groups had argued during the consultation that the proposed buffers would
constrain lending to small businesses at a moment when credit conditions were already
tightening. The regulator acknowledged the concern but said the analysis it published
alongside the rules found the effect on aggregate lending would be modest, amounting
to less than a tenth of a percentage point of annual growth over the transition period.
Several of the largest institutions have already begun retaining earnings in
anticipation of the change, according to filings reviewed for this report, and two of
them have suspended share buyback programmes that had been announced earlier in the
year. Analysts expect the sector to raise a combined figure in the low tens of billions
through retained earnings rather than through new equity issuance.
The rules also introduce a reporting requirement covering intraday liquidity positions,
which firms will have to submit on a monthly basis beginning eighteen months after the
effective date. Supervisors will be given discretion to impose firm-specific add-ons
where they judge that a business model concentrates funding risk, and the regulator
said it expected to use that power sparingly and only after a supervisory dialogue.
A separate consultation on the treatment of sovereign exposures remains open until the
end of the quarter, and officials indicated that any changes arising from it would be
implemented on a later timetable so that firms are not required to absorb two revisions
at once. The regulator will publish an impact assessment covering both packages once
the second consultation closes.
"""

_WIRE_LEDE = """
The banking regulator published its final rules on Tuesday, confirming a set of capital
buffers that lenders will be required to build over the next two years and closing a
consultation that drew more than two hundred responses from the industry.
"""

_AGGREGATOR_LEDE = """
Banks operating in the country are facing a fresh set of capital demands after the
supervisor signed off on a long-awaited rulebook this week, ending months of lobbying
by lenders who had warned that the changes would bite into their ability to extend
credit. Our analysis of the final text, published below in full, shows the regulator
gave ground on timing but almost none on the headline numbers. Readers following this
story since the spring will recognise several of the arguments rehearsed once more.
"""

#: The aggregator's own words in place of the wire's first third. Everything
#: from "Industry groups" onward is carried over verbatim.
_AGGREGATOR_OPENING = """
Lenders will have to hold more capital under rules confirmed this week, with the earliest
requirements landing at the beginning of the coming financial year and the rest arriving
across the two years that follow. Firms above the fifty billion asset mark must carry an
extra buffer worth one and a half points on top of today's floor; those below it get away
with half a point. The supervisor called the overhaul the largest since the framework
began, pointing to problems that surfaced when markets seized up last spring. Its deputy
governor said the old liquidity assumptions had proved optimistic and that firms could
meet them while still being vulnerable to deposits leaving quickly.
"""

_INDEPENDENT = """
Shares in the country's largest lenders slipped on Tuesday afternoon after the
supervisor confirmed a package of capital measures that analysts had largely expected
but which arrived with a shorter transition than several banks had lobbied for.
Traders pointed to the suspension of two buyback programmes as the immediate trigger
for the move, rather than the buffers themselves.
Fund managers who follow the sector said the practical effect would be a slower pace of
distributions rather than a change in the shape of loan books. "The evidence we gathered
showed that liquidity assumptions embedded in the old rules were optimistic, and that
firms were able to satisfy them while remaining exposed to a rapid outflow of deposits,"
the deputy governor said, a line that several notes to clients seized on as evidence
that supervisory tolerance had narrowed.
Bond desks reported light two-way flow in subordinated paper. One strategist argued that
the phased timetable removes the tail risk of a disorderly capital raise, which would
historically have been the market's main worry, and that spreads should grind tighter
once the second consultation on sovereign exposures is out of the way.
"""

_UNRELATED = """
The transport authority opened bidding on Monday for the second phase of the coastal
rail link, a project that has been delayed twice since it was first costed and which is
now expected to carry passengers no earlier than the end of the decade. Three consortia
have pre-qualified, and the authority said it would evaluate bids on a combination of
price, delivery schedule and local employment commitments.
Engineers working on the first phase have reported that ground conditions along the
southern approach were softer than the original survey suggested, which required
additional piling and pushed the completion date back by roughly seven months.
"""


def _flat(*parts: str) -> str:
    return " ".join(" ".join(part.split()) for part in parts)


WIRE = _flat(_WIRE_LEDE, _WIRE_BODY)
_WIRE_TAIL = "Industry groups" + _flat(_WIRE_BODY).split("Industry groups", 1)[1]
#: Same document, different opening: the aggregator's lede AND its own
#: rewritten first third, then the wire body verbatim.
AGGREGATOR_COPY = _flat(_AGGREGATOR_LEDE, _AGGREGATOR_OPENING) + " " + _WIRE_TAIL
INDEPENDENT_REPORT = _flat(_INDEPENDENT)
UNRELATED_REPORT = _flat(_UNRELATED)


def test_the_fixtures_are_the_shapes_the_thresholds_were_chosen_from():
    """Pin the measurement, not just the verdict: if the fixtures drift, the
    numbers in `_DUP_MIN_SHARED`'s comment stop describing them."""
    wire, copy = p.shingles(WIRE), p.shingles(AGGREGATOR_COPY)
    indep, unrel = p.shingles(INDEPENDENT_REPORT), p.shingles(UNRELATED_REPORT)

    assert p.shared_grams(wire, copy) == 250
    assert 0.38 < p.jaccard(wire, copy) < 0.40, "below the jaccard threshold"
    assert 0.56 < p.containment(wire, copy) < 0.58, "below the containment threshold"
    # Two independent reports of one event, both quoting the same statement.
    assert p.shared_grams(wire, indep) == 31
    assert p.shared_grams(wire, unrel) == 0


def test_a_rewritten_opening_is_recognised_as_the_same_document():
    wire, copy = p.shingles(WIRE), p.shingles(AGGREGATOR_COPY)
    assert p.near_duplicate(wire, copy), "the R11 case: neither ratio rule reaches it"
    assert p.near_duplicate(copy, wire), "and the test is symmetric"


def test_an_independent_report_of_the_same_event_is_not_a_duplicate():
    wire = p.shingles(WIRE)
    assert not p.near_duplicate(wire, p.shingles(INDEPENDENT_REPORT))
    assert not p.near_duplicate(wire, p.shingles(UNRELATED_REPORT))


def test_the_older_duplicate_shapes_still_hold():
    wire = p.shingles(WIRE)
    assert p.near_duplicate(wire, p.shingles("Breaking: " + WIRE)), "prepended lede"
    assert p.near_duplicate(wire, p.shingles(WIRE[: len(WIRE) // 2])), "trimmed tail"
    assert not p.near_duplicate(wire, p.shingles(WIRE), threshold=0)
    assert p.shingles("too short") == frozenset()
    assert not p.near_duplicate(frozenset(), wire)


def test_a_page_is_fingerprinted_past_its_opening():
    """The literal R11 headline. A page whose first 20,000 characters are one
    thing and whose body is another used to fingerprint only the first thing."""
    filler = "Cookie preferences and navigation boilerplate for this site. " * 400
    assert len(filler) > 20_000
    a = filler + " " + WIRE
    b = filler.replace("Cookie", "Privacy") + " " + WIRE
    assert p.shared_grams(p.shingles(a), p.shingles(b)) >= 400
    assert p.near_duplicate(p.shingles(a), p.shingles(b))


# --- and the consequence the ledger actually counts: corroboration ---------


def _state(question="what did the regulator decide", subqs=("what did the regulator decide",)):
    st = dr.ResearchState(research_id="dup0123456789", conversation_id="c1", question=question)
    st.subquestions = list(subqs)
    now = datetime.now(timezone.utc)
    st.today = now.date().isoformat()
    st.now_year = now.year
    st.temporal = Verdict(Freshness.RECENT, 14 * 86400, "lexical:recent")
    return st


def _register(st, url, text, *, authority=40, kind="news"):
    src = _Source(
        n=0, title=url, url=url, text=text, links=[],
        fetched_at=datetime.now(timezone.utc), authority=authority, source_type=kind,
    )
    return dr._register(st, src, "q")


def _claim(st, value, source_n):
    st.claims.append(
        dr.Claim(subq=1, text=f"the buffer is {value}", value=value,
                 source_n=source_n, as_of=None, hint="current", iteration=1)
    )


def test_a_rewrite_on_a_second_domain_does_not_lift_independent_corroboration():
    """The R11 acceptance, end to end through the resolver that pays for it.

    `_resolve` counts distinct domains among sources with `dup_of is None`,
    and awards +0.25 confidence at two. The aggregator IS a second domain; it
    is not a second source.
    """
    st = _state()
    a = _register(st, "https://wire.example/story", WIRE)
    b = _register(st, "https://aggregator.example/story", AGGREGATOR_COPY)
    _claim(st, "1.5 percentage points", a.n)
    _claim(st, "1.5 percentage points", b.n)
    dr._resolve(st)

    assert b.dup_of == a.n, "the rewrite must be registered as a copy"
    assert st.duplicates and st.duplicates[0]["dup_of"] == a.n
    res = st.resolutions[1]
    assert res.independent == 1, "two domains, one document"
    copied_confidence = res.confidence

    # The control: a genuinely independent second report of the same event,
    # saying the same thing, IS corroboration and does earn the bonus.
    st2 = _state()
    c = _register(st2, "https://wire.example/story", WIRE)
    d = _register(st2, "https://independent.example/story", INDEPENDENT_REPORT)
    _claim(st2, "1.5 percentage points", c.n)
    _claim(st2, "1.5 percentage points", d.n)
    dr._resolve(st2)

    assert d.dup_of is None
    assert st2.resolutions[1].independent == 2
    assert st2.resolutions[1].confidence == pytest.approx(copied_confidence + 0.25)


# ---------------------------------------------------------------------------
# R12 — a publisher grading its own work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://acme.com/x", "acme"),
        ("https://www.acme.com/x", "acme"),
        ("https://docs.acme.co.uk/x", "acme"),
        ("https://blog.acme.com.au/x", "acme"),
        ("https://agency.gov.in/x", "agency"),
        ("https://lab.ac.jp/x", "lab"),
        # A vanity two-letter TLD is a TLD, not a public second level.
        ("https://acme.co/x", "acme"),
        # A subdomain is NOT the publisher: this is Microsoft writing about
        # OpenAI, and treating it as OpenAI's own page would be exactly the
        # error the check exists to avoid.
        ("https://openai.microsoft.com/x", "microsoft"),
        ("https://acme-corp.com/x", "acme-corp"),
        ("not a url", ""),
        ("https://localhost/x", ""),
    ],
)
def test_registrable_label_reduces_a_host_to_its_name(url, expected):
    assert p.registrable_label(url) == expected


def test_entity_keys_ignore_words_that_name_a_kind_of_organisation():
    assert p.entity_keys("Acme Rocket Corp") == frozenset({"acme", "rocket", "acmerocket"})
    assert p.entity_keys(["OpenAI"]) == frozenset({"openai"})
    # Nothing distinctive left: no key, so nothing can match it.
    assert p.entity_keys("The Company Ltd") == frozenset()
    assert p.entity_keys("") == frozenset()
    assert p.entity_keys(None) == frozenset()


def test_self_published_sees_the_subjects_own_domain():
    subject = ["Acme Rocket Corp"]
    assert p.self_published("https://acme.com/benchmarks", subject)
    assert p.self_published("https://docs.acme.com/benchmarks", subject)
    assert p.self_published("https://acme-rocket.com/benchmarks", subject)
    assert p.self_published("https://acmerocket.io/benchmarks", subject)
    assert not p.self_published("https://reviews.example/acme", subject)
    assert not p.self_published("https://acme.com/x", [])
    # It is a comparison, not a reputation: with no subject there is no answer.
    assert not p.self_published("https://acme.com/x", "The Company")


def test_a_vendor_grading_itself_loses_half_the_first_hand_bonus():
    """The R12 acceptance. `docs.acme.com` is `docs` on a reference-grade
    host, so it is primary — and for a claim about Acme it is also the
    interested party."""
    about_acme = ["Acme Rocket Corp"]
    own = p.primary_weight("https://docs.acme.com/benchmarks", "docs", 70, about_acme)
    assert own == p.SELF_PUBLISHED_PRIMARY_WEIGHT < 1.0
    assert p.is_primary("https://docs.acme.com/benchmarks", "docs", 70), (
        "it is still a first-hand source; what it loses is the trust bonus"
    )
    # A genuine independent primary source keeps the whole bonus.
    assert p.primary_weight("https://docs.other.example/benchmarks", "docs", 70, about_acme) == 1.0
    assert p.primary_weight("https://acme.com/press/launch", "press", 70, ["Globex"]) == 1.0
    # And a page that was never primary earns nothing either way.
    assert p.primary_weight("https://acme.com/blog/post", "blog", 70, about_acme) == 0.0
    assert p.primary_weight("https://forum.acme.com/t/1", "community", 90, about_acme) == 0.0


def test_the_bound_a_credentialed_suffix_keeps_the_whole_bonus():
    """A statistics office publishing its own statistics, a university
    publishing its own paper, a ministry publishing its own regulation: for
    `official` and `academic` the suffix IS the credential and
    self-publication is the correct, normal case."""
    assert p.primary_weight("https://ons.gov.uk/releases/x", "official", 90, ["ONS"]) == 1.0
    assert p.primary_weight("https://ministry.gov.uk/x", "official", 100, ["Ministry of Health"]) == 1.0
    assert p.primary_weight("https://cs.stanford.edu/paper.pdf", "academic", 80, ["Stanford"]) == 1.0


def test_the_misfire_this_still_has_is_bounded_to_half_of_one_bonus():
    """Stated as a test so it is a known cost rather than a surprise: a
    project on a .org carries no suffix credential, so a question about the
    PROJECT answered from the project's own docs is demoted."""
    weight = p.primary_weight("https://docs.python.org/3/whatsnew/", "docs", 70, ["Python"])
    assert weight == p.SELF_PUBLISHED_PRIMARY_WEIGHT
    # What it does NOT lose: the class, the authority, the citation, or the
    # primary flag itself. Only a caller's first-hand bonus is halved.
    assert p.is_primary("https://docs.python.org/3/whatsnew/", "docs", 70)
    assert p.authority_cap("https://docs.python.org/3/whatsnew/") is None
    # And it does not fire for the ordinary shape, where the plan names the
    # product rather than the organisation behind the domain.
    assert p.primary_weight("https://docs.python.org/3/whatsnew/", "docs", 70, ["asyncio"]) == 1.0


@pytest.mark.parametrize(
    "url, kind, authority",
    [
        ("https://ministry.gov.uk/x", "official", 100),
        ("https://cs.stanford.edu/paper.pdf", "academic", 80),
        ("https://docs.vendor.example/x", "docs", 70),
        ("https://vendor.example/press/x", "press", 70),
        ("https://docs.vendor.example/x", "docs", 40),
        ("https://ref.example/x", "unknown", 70),
        ("https://someone.medium.com/x", "blog", 15),
        ("https://sites.google.com/view/anyone/docs/", "docs", 100),
    ],
)
def test_primary_weight_with_no_subject_is_exactly_is_primary(url, kind, authority):
    """Every existing caller keeps its behaviour until it has an entity to
    compare against — the new signal is opt-in, not a silent re-ranking."""
    expected = 1.0 if p.is_primary(url, kind, authority) else 0.0
    assert p.primary_weight(url, kind, authority) == expected
