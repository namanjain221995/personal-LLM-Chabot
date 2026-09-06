"""Reprocessing: REMOTE freshness and PROCESSING freshness are different facts.

A `304 Not Modified` proves one thing only — the bytes at the far end have not
changed. It says nothing about whether OUR copy of the page is still usable,
and this phase created two ways for it not to be:

  * the CHUNKER changed (`web_index.CHUNKER_VERSION`, V24). The stored text is
    perfect; only the vectors are shaped wrong. The repair is entirely local —
    `web_pages.text` is retained — so it needs no request at all, and a 304
    must leave the page fully visible to `index_pending`.
  * the EXTRACTOR changed (`core.extract.EXTRACT_VERSION`, V21/V22). Now the
    stored text itself is what is wrong, and re-extraction needs the original
    HTML, which nothing keeps. A conditional request is actively harmful here:
    a 304 returns no body, so the page can never be repaired and comes back
    round for ever. Those rows go out UNCONDITIONALLY.

Everything else keeps the conditional request — that is what makes ~2,300
refreshes a day affordable and polite, and it is measured working against real
sites in production.

Also covered: `tools/reindex_web.py`'s safety properties — the Salesforce
corpus is unreachable from it, two writers cannot touch one LanceDB directory,
an interrupted build resumes without duplicating a row, and a swap cannot
revert a page the worker re-indexed while the build was running.

No network: `net.safe_fetch` and `llm.embed_texts` are stubbed. PostgreSQL is
the suite's test database; LanceDB writes go to tmp_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db, llm, web_index, web_worker
from app.config import settings
from app.core import net, robots
from app.core.extract import EXTRACT_VERSION
from app.engines import search as se
from tools import reindex_web


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

LONG = ("The office holder is named in this paragraph. " * 80).strip()
MEDIUM = ("A shorter page that still clears the chunk minimum. " * 12).strip()
THIN = "Too short to index."


def _page(url: str, text: str = MEDIUM, **kwargs) -> int:
    key = url.split("//", 1)[-1]
    return int(
        db.upsert_web_page(
            url_key=key, url=url, canonical_url=url, title="Page", text=text,
            content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            **kwargs,
        )["id"]
    )


def _row(page_id: int) -> dict:
    with db.connection() as con:
        return dict(
            con.execute("SELECT * FROM web_pages WHERE id = %s", (page_id,)).fetchone()
        )


@pytest.fixture()
def fake_embed(monkeypatch):
    calls = []

    async def embed(texts, **_kwargs):
        calls.append(len(texts))
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    return calls


@pytest.fixture(autouse=True)
def _robots_allowed(monkeypatch):
    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(robots, "allowed", yes)
    monkeypatch.setattr(robots, "reserve_slot", yes)
    yield
    robots.reset_cache()


def _fetch_result(url, body=b"<html><body><p>a body long enough to extract.</p></body></html>",
                  status=200, ctype="text/html", headers=None):
    return net.FetchResult(
        url=url, status=status, content_type=ctype, body=body, headers=headers or {}
    )


# ---------------------------------------------------------------------------
# 1. CHUNKER_VERSION means something
# ---------------------------------------------------------------------------


#: Chunker versions a shipped build has written into stored rows. 1 was the
#: original fixed window; 2 added the pipe-table header carry; 3 raised the
#: per-page cap 64 -> 256. Append only when a version has actually shipped.
_SHIPPED_CHUNKER_VERSIONS = (1, 2, 3)


def test_the_chunker_version_records_the_header_carry_shape():
    """The constant is a claim about `chunk_page`'s output, so prove the claim.

    Header carry repeats a pipe table's header row into every chunk that holds
    its rows (findings C1/K3: the header went to chunk 0 and the answer row to
    chunk 2, so retrieved evidence was bare digits with no columns). The shape
    changed on 2026-09-06 while the constant stayed at 1, which made every page
    indexed before that day indistinguishable from a repaired one.

    Asserted as a PROPERTY, not a literal. Pinning `== 2` made this test fail
    on its own subject matter — it broke the moment the chunker changed again
    (the cap raise to 256, version 3), which is exactly when it should pass.
    The behaviour below is the part worth protecting.
    """
    assert web_index.CHUNKER_VERSION >= 2, (
        "header carry shipped at version 2; the constant may only move forward"
    )
    assert web_index.CHUNKER_VERSION in _SHIPPED_CHUNKER_VERSIONS, (
        f"CHUNKER_VERSION {web_index.CHUNKER_VERSION} is not a version this "
        f"build has shipped {_SHIPPED_CHUNKER_VERSIONS} — add it deliberately"
    )

    header = "| model | score |\n|---|---|\n"
    rows = "".join(f"| model-{i} | {i}.5 |\n" for i in range(400))
    chunks = web_index.chunk_page("Leaderboard.\n\n" + header + rows)

    assert len(chunks) > 1, "the fixture must span a chunk boundary to prove anything"
    assert all("| model | score |" in c for c in chunks[1:]), (
        "a chunk that starts inside the table must carry its header — that is "
        "what CHUNKER_VERSION 2 records"
    )


def test_index_pending_repairs_stale_chunks_and_stamps_the_version(fake_embed, tmp_path, monkeypatch):
    """The V24 loop, end to end, with the spin the manager warned about.

    Passing CHUNKER_VERSION to both db calls is load-bearing in OPPOSITE
    directions: omit it from the select and the repair never happens; omit it
    from the stamp and every repaired page is re-selected for ever.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 1)
    page_id = _page("https://example.com/one")

    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    assert _row(page_id)["chunk_version"] == 1
    assert _row(page_id)["indexed_at"] is not None
    # Settled at chunker 1: nothing to do.
    assert asyncio.run(web_index.index_pending(limit=10)) == 0

    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)
    assert db.count_stale_chunk_pages(2) == 1
    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    assert _row(page_id)["chunk_version"] == 2
    assert db.count_stale_chunk_pages(2) == 0

    # THE SPIN: a repaired page must not be offered again, for ever.
    assert asyncio.run(web_index.index_pending(limit=10)) == 0
    assert asyncio.run(web_index.index_pending(limit=10)) == 0


def test_a_thin_page_leaves_the_repair_queue_too(fake_embed, tmp_path, monkeypatch):
    """A page with no chunks cannot have stale ones — but it still has to be
    stamped, or the "nothing chunkable" branch re-reads it every cycle."""
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    thin = _page("https://example.com/thin", THIN)

    assert asyncio.run(web_index.index_pending(limit=10)) == 0
    assert _row(thin)["chunk_version"] == web_index.CHUNKER_VERSION
    assert not db.get_unindexed_web_pages(limit=10, chunk_version=web_index.CHUNKER_VERSION)


def test_the_fast_lookup_never_inherits_the_repair_backlog(fake_embed, tmp_path, monkeypatch):
    """`repair_stale_chunks=False` — the synchronous call in the request path.

    The Fast lookup indexes inline because it is about to read the corpus back.
    After a chunker bump the repair backlog is EVERY page, and draining it
    there would put a whole-corpus re-embed inside a user's deadline for work
    nobody is waiting on. It must still stamp what it does write.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 1)
    stale = _page("https://example.com/stale")
    assert asyncio.run(web_index.index_pending(limit=10)) == 1

    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)
    fresh = _page("https://example.com/fresh", LONG)

    fake_embed.clear()
    assert asyncio.run(web_index.index_pending(limit=40, repair_stale_chunks=False)) == 2
    assert sum(fake_embed) == 2, "only the new page's chunks may be embedded"
    assert _row(fresh)["chunk_version"] == 2
    assert _row(stale)["chunk_version"] == 1  # untouched, still owed to the worker
    assert db.count_stale_chunk_pages(2) == 1


def test_only_the_worker_drains_the_repair_backlog_from_the_search_engine():
    """Both `index_pending` calls in the search engine opt OUT of the repair.

    One is synchronous inside the Fast lookup; the other runs after the answer
    has streamed, but `embed_query` sheds load rather than queueing, so a
    whole-corpus re-embed started there degrades the NEXT question's recall.
    The worker is the caller with nobody waiting on it.
    """
    source = open(se.__file__, encoding="utf-8").read()
    calls = [
        line.strip() for line in source.splitlines()
        if "web_index.index_pending(" in line
    ]
    assert calls, "the search engine must still index what it fetches"
    assert all("repair_stale_chunks=False" in c for c in calls), calls


# ---------------------------------------------------------------------------
# 3. REMOTE freshness vs PROCESSING freshness
# ---------------------------------------------------------------------------


def _refresh_row(page_id: int, **extra):
    row = _row(page_id)
    out = {
        "id": row["id"], "url": row["url"], "url_key": row["url_key"],
        "title": row["title"], "content_hash": row["content_hash"],
        "etag": row["etag"], "last_modified": row["last_modified"],
        "retrieval_count": row["retrieval_count"],
        "refresh_failures": row["refresh_failures"], "fetched_at": row["fetched_at"],
    }
    out.update(extra)
    return out


def test_a_304_costs_no_reprocessing_when_the_page_is_current_on_both(monkeypatch, tmp_path, fake_embed):
    """ACCEPTANCE: current text, current chunks, unchanged upstream -> nothing.

    One conditional round trip, no body, no extraction, no re-chunk, no
    re-embed. This is the case that makes the whole refresh affordable, and it
    must survive everything else in this file.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    page_id = _page("https://cur.example/doc", etag='"v1"',
                    last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
                    extract_version=EXTRACT_VERSION)
    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    before = _row(page_id)

    sent = {}

    async def fake_fetch(url, **kwargs):
        sent.update(kwargs.get("headers") or {})
        return _fetch_result(url, b"", status=304, headers={"etag": '"v1"'})

    def never(*_a, **_k):
        raise AssertionError("a 304 must never reach the extractor")

    monkeypatch.setattr(se.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(se, "_call_extract", never)

    row = _refresh_row(page_id, extract_version=EXTRACT_VERSION)
    assert asyncio.run(web_worker._refresh_one(row)) == "not_modified"

    assert sent.get("If-None-Match") == '"v1"'
    assert sent.get("If-Modified-Since") == "Wed, 21 Oct 2015 07:28:00 GMT"
    after = _row(page_id)
    assert after["indexed_at"] == before["indexed_at"]
    assert after["chunk_version"] == before["chunk_version"]
    assert after["content_hash"] == before["content_hash"]
    assert after["fetched_at"] > before["fetched_at"]  # the freshness clock, only
    fake_embed.clear()
    assert asyncio.run(web_index.index_pending(limit=10)) == 0
    assert fake_embed == []


def test_a_304_still_leaves_a_chunk_stale_page_visible_to_the_indexer(monkeypatch, tmp_path, fake_embed):
    """ACCEPTANCE: current text, obsolete chunker -> re-chunked with NO request.

    The refresh worker confirming the remote body is unchanged must not be
    mistaken for "this page needs no work". `touch_web_page_unchanged` moves
    `fetched_at` and nothing else, so the page stays in `index_pending`'s
    queue, and the repair runs off the STORED TEXT with the network untouched.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 1)
    page_id = _page("https://cur.example/table", etag='"v1"',
                    extract_version=EXTRACT_VERSION)
    assert asyncio.run(web_index.index_pending(limit=10)) == 1

    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)

    async def fake_fetch(url, **_kwargs):
        return _fetch_result(url, b"", status=304)

    monkeypatch.setattr(se.net, "safe_fetch", fake_fetch)
    row = _refresh_row(page_id, extract_version=EXTRACT_VERSION)
    assert asyncio.run(web_worker._refresh_one(row)) == "not_modified"

    # The 304 changed nothing about the fact that the CHUNKS are obsolete.
    assert db.count_stale_chunk_pages(2) == 1
    assert [p["id"] for p in db.get_unindexed_web_pages(limit=10, chunk_version=2)] == [page_id]

    # And the repair itself makes no request whatsoever.
    def no_network(*_a, **_k):
        raise AssertionError("re-chunking must not fetch anything")

    monkeypatch.setattr(se.net, "safe_fetch", no_network)
    monkeypatch.setattr(net, "safe_fetch", no_network)
    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    assert _row(page_id)["chunk_version"] == 2


def test_a_page_below_the_extractor_version_is_fetched_unconditionally(monkeypatch):
    """ACCEPTANCE: obsolete extractor -> UNCONDITIONAL request, then re-extract.

    Re-extraction needs the original HTML, which is not stored anywhere. A
    conditional request answered 304 returns no body, so the page could never
    be repaired: it would sit below EXTRACT_VERSION for ever, re-offered every
    six hours, politely and pointlessly. The validators must be ABSENT.
    """
    page_id = _page("https://old.example/doc", etag='"v1"',
                    last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
                    extract_version=0)
    seen = {}

    async def fake_fetch(url, **kwargs):
        seen["headers"] = dict(kwargs.get("headers") or {})
        seen["url"] = url
        return _fetch_result(url)

    monkeypatch.setattr(se.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(
        se, "_call_extract",
        lambda *_a, **_k: (se.extract.Extracted(text="a much better extraction of the same page",
                                                title="Better"), []),
    )

    row = _refresh_row(page_id, extract_version=0)
    assert web_worker._stale_extractor(row) is True
    assert asyncio.run(web_worker._refresh_one(row)) == "read"

    assert "If-None-Match" not in seen["headers"]
    assert "If-Modified-Since" not in seen["headers"]
    assert seen["headers"] == {}
    after = _row(page_id)
    assert after["extract_version"] == EXTRACT_VERSION  # it can now leave the queue
    assert after["text"] == "a much better extraction of the same page"
    assert after["indexed_at"] is None  # the text moved, so the vectors must too


def test_the_ordinary_freshness_case_keeps_its_conditional_request(monkeypatch):
    """The politeness that is already deployed and measured working must stay.

    Only a row we can SEE is below the extractor earns an unconditional
    download; a page at the current version, and a row whose `extract_version`
    is missing entirely (a deployment before the V22 column), both keep the
    validators.
    """
    page_id = _page("https://cur.example/ok", etag='"v9"', extract_version=EXTRACT_VERSION)
    seen = []

    async def fake_fetch(url, **kwargs):
        seen.append(dict(kwargs.get("headers") or {}))
        return _fetch_result(url, b"", status=304)

    monkeypatch.setattr(se.net, "safe_fetch", fake_fetch)

    at_version = _refresh_row(page_id, extract_version=EXTRACT_VERSION)
    assert web_worker._stale_extractor(at_version) is False
    assert asyncio.run(web_worker._refresh_one(at_version)) == "not_modified"

    no_column = _refresh_row(page_id)          # pre-V22 deployment: key absent
    assert "extract_version" not in no_column
    assert web_worker._stale_extractor(no_column) is False
    assert asyncio.run(web_worker._refresh_one(no_column)) == "not_modified"

    assert all(h.get("If-None-Match") == '"v9"' for h in seen), seen
    assert len(seen) == 2

    # A NULL in the column is "unknown", not "0": still conditional.
    assert web_worker._stale_extractor({"extract_version": None}) is False


def test_chunk_staleness_never_enters_the_fetch_queue(fake_embed, tmp_path, monkeypatch):
    """The negative half of the separation, asserted rather than assumed.

    A re-chunk needs no bytes, so a chunk-stale page must NOT be added to the
    refresh worker's fetch queue — putting it there would spend a third-party
    request and a robots check on work that is purely local.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 1)
    page_id = _page("https://cur.example/local", extract_version=EXTRACT_VERSION)
    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    # No deadline, current extractor: the only thing wrong with it is its chunks.
    with db.connection() as con:
        con.execute("UPDATE web_pages SET next_refresh_at = NULL WHERE id = %s", (page_id,))

    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)
    assert db.count_stale_chunk_pages(2) == 1
    assert web_worker._due_pages(10) == []


# ---------------------------------------------------------------------------
# 4. reindex_web — namespaces, concurrency, resume, the swap window
# ---------------------------------------------------------------------------


def test_the_reindex_tool_cannot_touch_the_salesforce_corpus(tmp_path, monkeypatch, fake_embed):
    """The blast-radius assertion. `/data/lancedb` is the CRM index and a
    DIFFERENT table; its sidecar pins one table per directory, so a web build
    there does not add a table, it breaks the CRM's.

    The "must be empty" check alone would have allowed it: on a box whose sync
    has not run, `/data/lancedb` is empty or absent.
    """
    salesforce = str(tmp_path / "lancedb")          # LANCEDB_DIR — CRM
    monkeypatch.setattr(settings, "lancedb_dir", salesforce)
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "lancedb-web"))
    _page("https://example.com/medium")

    for target in (
        salesforce,                                  # the corpus itself
        os.path.join(salesforce, "nested"),          # inside it
        salesforce + "/",                            # trailing slash
        str(tmp_path),                               # a parent that swallows it
    ):
        with pytest.raises(SystemExit, match="SALESFORCE"):
            asyncio.run(reindex_web.build(target, progress_every=0))
        with pytest.raises(SystemExit, match="SALESFORCE"):
            reindex_web.assert_not_salesforce(target)

    assert not os.path.exists(salesforce), "the CRM directory must not even be created"

    # And the tool names exactly one table, the web one, nowhere else. The
    # private conversation vectors live in PostgreSQL `conversation_chunks`
    # and no statement here goes anywhere near them.
    source = open(reindex_web.__file__, encoding="utf-8").read()
    body = source.split('"""', 2)[2]  # everything after the module docstring
    assert source.count("open_table(") == source.count("open_table(web_index.TABLE)")
    assert "create_table(" not in body
    assert "conversation_chunks" not in body
    # A build into a legitimate target still works, and opens only web_chunks.
    report = asyncio.run(reindex_web.build(str(tmp_path / "new"), progress_every=0))
    assert report.validated
    import lancedb

    assert lancedb.connect(str(tmp_path / "new")).table_names() == [web_index.TABLE]


def test_a_sidecar_written_before_the_key_existed_still_catches_up(tmp_path, monkeypatch, fake_embed):
    """The LIVE production sidecar, byte for byte.

    `/data/lancedb-web/_techsara_embedding_index.json` was written on
    2026-08-30, before `chunker_version` was a sidecar key, and reads exactly
    the four fields below — no chunker_version at all. Treating "key absent"
    as "unknown, leave it alone" would have made the one deployment that
    matters permanently unable to report its chunk shape.
    """
    directory = str(tmp_path / "legacy")
    monkeypatch.setattr(settings, "lancedb_web_dir", directory)
    os.makedirs(directory)
    from app.embedding_index import metadata_path

    with open(metadata_path(directory), "w", encoding="utf-8") as fh:
        json.dump(
            {"table": "web_chunks", "model_id": settings.embed_model,
             "dimension": 4, "schema_version": 1}, fh,
        )
    assert web_index.sidecar_chunker_version(directory) == 0
    assert web_index.sidecar_chunker_version(str(tmp_path / "nothing")) is None

    _page("https://example.com/one")
    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    upkeep = asyncio.run(web_index.maintain())
    assert upkeep["sidecar_advanced"] is True
    assert upkeep["stale_chunk_pages"] == 0
    assert web_index.sidecar_chunker_version(directory) == web_index.CHUNKER_VERSION
    # The keys the typed loader needs are untouched by the rewrite.
    with open(metadata_path(directory), encoding="utf-8") as fh:
        after = json.load(fh)
    assert after["model_id"] == settings.embed_model and after["dimension"] == 4
    assert after["table"] == "web_chunks" and after["schema_version"] == 1


def test_two_processes_cannot_write_one_index_directory(tmp_path, monkeypatch, fake_embed):
    """`_index_lock` is an asyncio.Lock and cannot see another interpreter —
    and `docker exec … python -m tools.reindex_web` is another interpreter.

    Lance commits its manifest optimistically with no filesystem commit lock,
    so two writers is a corrupt table. The flock is taken here from a second
    THREAD holding it the way a second process would.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    out_dir = str(tmp_path / "new")
    _page("https://example.com/medium")

    held = threading.Event()
    release = threading.Event()

    def hold():
        with web_index.write_lock(out_dir, wait_s=0.0):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    try:
        with pytest.raises(web_index.IndexBusy, match="another process is writing"):
            asyncio.run(reindex_web.build(out_dir, progress_every=0))
    finally:
        release.set()
        holder.join(10)

    # Released, so the same build now succeeds into the same directory.
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated


def test_the_lock_file_never_makes_a_fresh_target_look_non_empty(tmp_path, monkeypatch, fake_embed):
    """It lives BESIDE the directory on purpose: inside, it would trip the
    "this tool never deletes" guard on a perfectly fresh --out."""
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    out_dir = str(tmp_path / "fresh")
    _page("https://example.com/medium")
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated
    assert not os.path.exists(os.path.join(out_dir, ".fresh.write.lock"))
    assert os.path.exists(web_index._lock_path(out_dir))


def test_an_interrupted_build_resumes_without_duplicating_or_losing_a_row(
    tmp_path, monkeypatch, fake_embed
):
    """ACCEPTANCE for resumability, with a real interruption mid-build.

    The embedding service is made to fail part-way, exactly as a killed
    container or an OOM would. What survives must be a consistent PREFIX, and
    the resumed run must produce the same index a single uninterrupted run
    would — same rows, same distinct pages, no duplicates.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    ids = [_page(f"https://example.com/p{i}", LONG + f" page {i}") for i in range(20)]
    assert len(ids) == 20

    # A reference: what an uninterrupted build produces.
    reference = asyncio.run(reindex_web.build(str(tmp_path / "reference"), progress_every=0))
    assert reference.validated

    out_dir = str(tmp_path / "resumable")
    calls = {"n": 0}
    real_embed = llm.embed_texts

    async def flaky(texts, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("embedding service went away mid-build")
        return await real_embed(texts, **kwargs)

    monkeypatch.setattr(llm, "embed_texts", flaky)
    # Small batches so the interruption lands in the middle of the store.
    monkeypatch.setattr(reindex_web, "EMBED_BATCH", 8)
    monkeypatch.setattr(reindex_web, "ADD_BATCH", 1)
    with pytest.raises(RuntimeError, match="went away mid-build"):
        asyncio.run(reindex_web.build(out_dir, progress_every=0))

    partial = reindex_web.read_manifest(out_dir)
    assert partial is not None and partial["complete"] is False
    import lancedb

    partial_rows = lancedb.connect(out_dir).open_table(web_index.TABLE).count_rows()
    assert 0 < partial_rows < reference.rows, partial_rows

    # Without --resume the tool still refuses, and says what to do about it.
    with pytest.raises(SystemExit, match="--resume"):
        asyncio.run(reindex_web.build(out_dir, progress_every=0))

    monkeypatch.setattr(llm, "embed_texts", real_embed)
    resumed = asyncio.run(reindex_web.build(out_dir, progress_every=0, resume=True))

    assert resumed.validated, resumed.problems
    assert (resumed.rows, resumed.distinct_pages) == (reference.rows, reference.distinct_pages)
    assert resumed.pages_indexed == reference.pages_indexed
    assert reindex_web.read_manifest(out_dir)["complete"] is True
    # The invariant a bad resume breaks: one page, one set of chunks.
    assert reindex_web._table_page_ids(out_dir) == sorted(ids)
    table = lancedb.connect(out_dir).open_table(web_index.TABLE)
    per_page = {}
    for row in table.search().select(["page_id"]).limit(resumed.rows).to_list():
        per_page[row["page_id"]] = per_page.get(row["page_id"], 0) + 1
    assert set(per_page.values()) == {2}, per_page

    # The original build's start instant survives the resume: `adopt`'s window
    # has to cover the attempt that was interrupted, not just the retry.
    assert reindex_web.read_manifest(out_dir)["started_at"] == partial["started_at"]


def test_resume_refuses_a_directory_it_did_not_write_or_a_different_chunker(tmp_path, monkeypatch, fake_embed):
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    _page("https://example.com/medium")

    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "something").write_text("x")
    with pytest.raises(SystemExit, match="needs a build manifest"):
        asyncio.run(reindex_web.build(str(stranger), progress_every=0, resume=True))

    done = str(tmp_path / "done")
    assert asyncio.run(reindex_web.build(done, progress_every=0)).validated
    with pytest.raises(SystemExit, match="already completed"):
        asyncio.run(reindex_web.build(done, progress_every=0, resume=True))

    manifest = reindex_web.read_manifest(done)
    manifest.update({"complete": False, "chunker_version": 99})
    reindex_web._write_manifest(done, manifest)
    with pytest.raises(SystemExit, match="chunker 99"):
        asyncio.run(reindex_web.build(done, progress_every=0, resume=True))


def test_adopt_preserves_the_newest_revision_and_stops_the_rechunk_spin(
    tmp_path, monkeypatch, fake_embed
):
    """ACCEPTANCE for the swap: two failures that both look like success.

    1. A page the worker re-indexed DURING the build has newer chunks in the
       OLD directory only. The new one holds the build-time snapshot, so the
       swap reverts it — silently, because every count still matches.
    2. After a rebuild, PostgreSQL still says every page's chunk_version is
       behind, so the worker immediately re-chunks the whole corpus to produce
       byte-identical vectors.

    `adopt` fixes both, and the ORDER matters: queue the window first, then
    stamp only what is left. Stamping first would mark the reverted page done.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)
    quiet = _page("https://example.com/quiet")
    moved = _page("https://example.com/moved")
    asyncio.run(web_index.index_pending(limit=10))
    assert _row(quiet)["chunk_version"] == 2

    out_dir = str(tmp_path / "new")
    report = asyncio.run(reindex_web.build(out_dir, progress_every=0))
    assert report.validated and report.distinct_pages == 2

    # ... and WHILE that ran, the worker re-indexed `moved` into the old dir.
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET indexed_at = now(), chunk_version = 2 WHERE id = %s",
            (moved,),
        )
    # Simulate the corpus as it looks straight after a rebuild: the vectors are
    # chunker 2, the database has never been told.
    with db.connection() as con:
        con.execute("UPDATE web_pages SET chunk_version = 0")
    assert db.count_stale_chunk_pages(2) == 2

    # Before the swap, adopt refuses: the stamp is a claim about what is SERVED.
    with pytest.raises(SystemExit, match="live index directory"):
        reindex_web.adopt(out_dir, apply=True)

    monkeypatch.setattr(settings, "lancedb_web_dir", out_dir)
    preview = reindex_web.adopt(out_dir, apply=False)
    assert preview["table_pages"] == 2 and preview["window_pages"] == 1
    assert _row(moved)["indexed_at"] is not None  # a preview changes nothing

    result = reindex_web.adopt(out_dir, apply=True)
    assert result == {"window_pages": 1, "table_pages": 2, "queued": 1, "stamped": 1}

    # 1. the page that moved is queued, so the worker rebuilds it here...
    assert _row(moved)["indexed_at"] is None
    assert _row(moved)["chunk_version"] == 0
    # 2. ...and the untouched page is recorded, so it is NOT re-chunked.
    assert _row(quiet)["chunk_version"] == 2
    assert [p["id"] for p in db.get_unindexed_web_pages(limit=10, chunk_version=2)] == [moved]

    assert asyncio.run(web_index.index_pending(limit=10)) == 1
    assert db.count_stale_chunk_pages(2) == 0
    assert asyncio.run(web_index.index_pending(limit=10)) == 0  # settled


def test_adopt_never_stamps_a_page_the_index_does_not_hold(tmp_path, monkeypatch, fake_embed):
    """"Remove only demonstrably obsolete vectors" has a mirror: record only
    demonstrably present ones. The page ids come from the TABLE, never from a
    counter the build kept."""
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    present = _page("https://example.com/present")
    absent = _page("https://example.com/absent")
    # Both were indexed by the worker before the rebuild started.
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET indexed_at = now() - interval '2 days', "
            "fetched_at = now() - interval '2 days', chunk_version = 0"
        )
    out_dir = str(tmp_path / "new")
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated

    # `absent` lost its rows — a failed batch, a purge, anything. The COUNTERS
    # the build kept still say it was written; the table is the only evidence
    # `adopt` accepts, and it disagrees.
    import lancedb

    lancedb.connect(out_dir).open_table(web_index.TABLE).delete(f"page_id = {absent}")
    assert reindex_web._table_page_ids(out_dir) == [present]

    monkeypatch.setattr(settings, "lancedb_web_dir", out_dir)
    result = reindex_web.adopt(out_dir, apply=True)
    assert result["stamped"] == 1 and result["table_pages"] == 1
    assert _row(present)["chunk_version"] == web_index.CHUNKER_VERSION
    assert _row(absent)["chunk_version"] == 0, "no chunks here; claiming it would hide it"


def test_adopt_refuses_a_partial_or_foreign_or_wrong_chunker_index(tmp_path, monkeypatch, fake_embed):
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    _page("https://example.com/medium")

    stranger = str(tmp_path / "stranger")
    os.makedirs(stranger)
    monkeypatch.setattr(settings, "lancedb_web_dir", stranger)
    with pytest.raises(SystemExit, match="no build manifest"):
        reindex_web.adopt(stranger, apply=True)

    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    out_dir = str(tmp_path / "new")
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated
    monkeypatch.setattr(settings, "lancedb_web_dir", out_dir)

    manifest = reindex_web.read_manifest(out_dir)
    reindex_web._write_manifest(out_dir, {**manifest, "complete": False})
    with pytest.raises(SystemExit, match="never completed"):
        reindex_web.adopt(out_dir, apply=True)

    reindex_web._write_manifest(out_dir, {**manifest, "chunker_version": 1})
    with pytest.raises(SystemExit, match="built by chunker 1"):
        reindex_web.adopt(out_dir, apply=True)


def test_the_build_window_covers_a_rechunk_that_never_refetched_anything(tmp_path, monkeypatch, fake_embed):
    """The `--since` column that V24 made wrong.

    A stale-chunker repair moves `indexed_at` and NOT `fetched_at`, so a
    fetched_at-only window misses exactly the repairs this phase introduced —
    and the swap silently reverts them.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    page_id = _page("https://example.com/rechunked")
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET fetched_at = %s, indexed_at = now() WHERE id = %s",
            (started - timedelta(days=3), page_id),
        )

    assert reindex_web.count_watermark_reset(started) == 1
    assert reindex_web.reset_watermark(started) == 1
    assert _row(page_id)["indexed_at"] is None


def test_a_limited_smoke_build_is_never_adoptable(tmp_path, monkeypatch, fake_embed):
    """A `--limit` build holds a fraction of the corpus. Adopting it would
    stamp those pages as current and leave the rest of the store looking like
    it has vectors in a directory that does not contain them."""
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    for i in range(3):
        _page(f"https://example.com/p{i}")
    out_dir = str(tmp_path / "smoke")
    report = asyncio.run(reindex_web.build(out_dir, limit=1, progress_every=0))
    assert report.validated and report.limited

    assert reindex_web.read_manifest(out_dir)["complete"] is False
    monkeypatch.setattr(settings, "lancedb_web_dir", out_dir)
    with pytest.raises(SystemExit, match="never completed"):
        reindex_web.adopt(out_dir, apply=True)


def test_adopt_through_the_cli_prints_counts_first_and_refuses_without_yes(
    tmp_path, monkeypatch, fake_embed, capsys
):
    """Same contract as every other mutation in this tool: counts, then --yes."""
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    page_id = _page("https://example.com/one")
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET indexed_at = now() - interval '2 days', "
            "fetched_at = now() - interval '2 days'"
        )
    out_dir = str(tmp_path / "new")
    assert reindex_web.main(["build", "--out", out_dir, "--progress-every", "0"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(settings, "lancedb_web_dir", out_dir)

    assert reindex_web.main(["adopt"]) == 0
    out = capsys.readouterr().out
    assert "pages the index holds: 1" in out and "nothing changed" in out
    assert _row(page_id)["chunk_version"] == 0

    assert reindex_web.main(["adopt", "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"chunk_version={web_index.CHUNKER_VERSION} on 1 page(s)" in out
    assert _row(page_id)["chunk_version"] == web_index.CHUNKER_VERSION


def test_resume_is_incompatible_with_a_limit(tmp_path, monkeypatch, fake_embed):
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    _page("https://example.com/medium")
    with pytest.raises(SystemExit, match="mutually exclusive"):
        asyncio.run(reindex_web.build(str(tmp_path / "x"), limit=1, resume=True, progress_every=0))


# --------------------------------------------------------------------------
# V27: raising the cap must not invalidate vectors it cannot change.
# --------------------------------------------------------------------------


def _chunks_at(monkeypatch, text: str, cap: int):
    from app import web_index

    monkeypatch.setattr(web_index, "_MAX_CHUNKS_PER_PAGE", cap)
    return web_index.chunk_page(text)


def test_a_page_under_the_old_ceiling_chunks_identically_at_either_cap(monkeypatch):
    """The claim V27's surgical stamp rests on.

    `chunk_page` stops on `start + CHUNK >= len(text)`, not on the cap, so the
    cap only binds for a page needing more than 64 chunks. A page that fits in
    the OLD ceiling therefore yields the same windows, the same header carry
    and the same text at cap 64 and at cap 256 — its vectors are already
    correct for chunker 3, and re-embedding ~2,000 such pages would produce
    identical numbers while costing users 1.65-4x TTFT for the duration.

    Verified against 120 real stored pages (largest 175,618 chars) before this
    was written: 0 differing.
    """
    from app import web_index

    old_ceiling = 179_600  # (64 - 1) * 2800 + 3200, the cap in force at V24
    text = ("Turbine efficiency rose to 41.2% in the third quarter. " * 60 + "\n") * 55
    text = text[: old_ceiling - 10]
    assert len(text) <= old_ceiling

    at_64 = _chunks_at(monkeypatch, text, 64)
    at_256 = _chunks_at(monkeypatch, text, 256)
    assert at_64 == at_256, "a page under the old ceiling must not re-chunk"
    assert len(at_64) <= 64


def test_a_page_over_the_old_ceiling_does_gain_chunks(monkeypatch):
    """The converse — without this the cap change would be a no-op."""
    old_ceiling = 179_600
    text = ("Rotor blade inspection log entry with distinct wording. " * 40 + "\n") * 200
    assert len(text) > old_ceiling

    at_64 = _chunks_at(monkeypatch, text, 64)
    at_256 = _chunks_at(monkeypatch, text, 256)
    assert len(at_64) == 64
    assert len(at_256) > 64, "raising the cap must actually reach further into the page"
    assert at_256[:64] == at_64, "the chunks it already had must not move"


#: (chunk cap, CHUNKER_VERSION) pairs a SHIPPED build has written into stored
#: rows. Append a pair here only once it has actually shipped. Recorded rather
#: than asserted as a literal for the same reason the extractor versions are:
#: pinning today's numbers makes the test fail on its own subject matter, the
#: moment the cap is deliberately moved.
_SHIPPED_CAP_AND_CHUNKER = ((64, 2), (256, 3))


def test_the_chunker_version_advanced_with_the_cap():
    """A cap change alters what a page yields, so the version must move with it
    or `chunk_version` stops identifying the shape it claims to.

    Asserted as a PROPERTY of the pair, not as two magic numbers: whatever the
    cap is set to, it must not be a cap some earlier build shipped under a
    DIFFERENT chunker version, and the version must exceed every version
    already in the wild — a reused number means the pages carrying it are
    never re-chunked.
    """
    from app import web_index

    cap = web_index._MAX_CHUNKS_PER_PAGE
    version = web_index.CHUNKER_VERSION

    assert version >= max(v for _c, v in _SHIPPED_CAP_AND_CHUNKER)
    for shipped_cap, shipped_version in _SHIPPED_CAP_AND_CHUNKER:
        if cap == shipped_cap:
            assert version == shipped_version, (
                f"cap {cap} shipped as chunker {shipped_version}; calling the "
                f"same shape {version} splits the corpus"
            )
        else:
            assert version > shipped_version, (
                f"cap moved {shipped_cap} -> {cap} without advancing "
                f"CHUNKER_VERSION past {shipped_version}; stored pages at that "
                "number would never be re-chunked"
            )

    # And the derived ceiling must follow the cap, not be edited beside it.
    stride = web_index._CHUNK_CHARS - web_index._OVERLAP_CHARS
    assert web_index.INDEXED_CHARS_PER_PAGE == (cap - 1) * stride + web_index._CHUNK_CHARS


# --------------------------------------------------------------------------
# K10: the cap is a CEILING, and a page that hits it must say so.
# --------------------------------------------------------------------------


def test_indexing_an_oversized_page_reports_the_tail_it_dropped(
    tmp_path, monkeypatch, fake_embed, caplog
):
    """The half of K10 that is not arithmetic.

    `unindexed_chars` being correct is worth nothing if nobody calls it: before
    this, a page too big for the cap was indexed to the ceiling and stamped
    exactly like a complete one, so a half-indexed page was indistinguishable
    from a whole one in the database, in the logs and on /metrics. What is
    under test here is that `index_pending` — the ONE caller — actually emits
    the counter and the line, with the size of the loss in it.

    Deliberately not run against patched constants: the real cap, the real
    ceiling and the real chunker, so the test cannot pass against arithmetic
    that no longer matches what `chunk_page` does.
    """
    from app import metrics

    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    ceiling = web_index.INDEXED_CHARS_PER_PAGE
    dropped = 5_000
    over = ("x" * 79 + " ") * ((ceiling + dropped) // 80)
    page_id = _page("https://example.com/huge", text=over)

    metrics.reset()
    with caplog.at_level("WARNING"):
        written = asyncio.run(web_index.index_pending(limit=5))

    assert written == web_index._MAX_CHUNKS_PER_PAGE, (
        "the cap should have bound; if it did not, this page is not oversized"
    )
    assert _row(page_id)["chunk_version"] == web_index.CHUNKER_VERSION

    rendered = metrics.render()
    assert "web_index_page_truncated_total" in rendered, (
        "an oversized page was indexed with no counter; a partial page is "
        "indistinguishable from a complete one on /metrics"
    )
    assert "https://example.com/huge" in caplog.text
    assert str(web_index.unindexed_chars(over)) in caplog.text, (
        "the warning must say HOW MUCH was dropped, not merely that something was"
    )


def test_a_page_inside_the_ceiling_is_reported_as_nothing(
    tmp_path, monkeypatch, fake_embed, caplog
):
    """The control: the counter must mean something when it does fire."""
    from app import metrics

    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "live"))
    _page("https://example.com/ordinary", text=LONG)

    metrics.reset()
    with caplog.at_level("WARNING"):
        assert asyncio.run(web_index.index_pending(limit=5)) > 0

    assert "web_index_page_truncated_total" not in metrics.render()
    assert "chunk ceiling" not in caplog.text
