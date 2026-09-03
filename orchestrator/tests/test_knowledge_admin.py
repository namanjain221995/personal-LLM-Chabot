"""Operator tooling for the knowledge stores (ADR-0001 D7/D8, migration V16).

Covers three pieces that share one fixture set:

- tools.knowledge_admin — list / quarantine / purge pages of the shared web
  corpus by domain, origin and introducer; every mutation prints counts
  first and refuses without --yes.
- tools.reindex_web — build a NEW web index directory from PostgreSQL,
  validate it (distinct page_id == indexable pages), print the swap and the
  rollback; reset-watermark for an in-place rebuild.
- health._check_web_index — the additive /health entry that reports the
  index's rows and distinct pages without ever failing /health.

The embedding service is replaced by a deterministic fake (4-dim vectors);
PostgreSQL is the test database from conftest; LanceDB writes go to tmp_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import List

import pytest

from app import db, health, llm
from app.config import settings
from app.embedding_index import metadata_path
from tools import knowledge_admin, reindex_web

LONG = ("The office holder is named in this paragraph. " * 80).strip()  # ~3,700 chars -> 2 chunks
MEDIUM = ("A shorter page that still clears the chunk minimum. " * 12).strip()  # ~600 chars -> 1 chunk
THIN = "Too short to index."  # < 200 chars -> 0 chunks


def _page(url: str, text: str, *, origin: str = "search", introducer=None, conv=None, title="Page") -> int:
    key = url.replace("https://", "").replace("http://", "")
    return int(
        db.upsert_web_page(
            url_key=key, url=url, canonical_url=url, title=title, text=text,
            content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha1(text.encode()).hexdigest(),
            origin=origin, introduced_by_user_id=introducer, introduced_in_conversation_id=conv,
        )["id"]
    )


def _claim(page_id: int, text: str, *, origin_user=None) -> None:
    assert db.insert_web_claims(
        [{"research_id": "run-1", "page_id": page_id, "url": "u", "claim": text,
          "quote": text, "kind": "fact", "confidence": 0.9, "origin_user_id": origin_user}]
    ) == 1


def _pages_row(page_id: int) -> dict | None:
    with db.connection() as con:
        row = con.execute(
            "SELECT id, quarantined_at, indexed_at FROM web_pages WHERE id = %s", (page_id,)
        ).fetchone()
    return dict(row) if row else None


def _count(sql: str, *params) -> int:
    with db.connection() as con:
        row = con.execute(sql, params).fetchone()  # dict_row: one column, any name
        return int(next(iter(row.values())))


def _run(main, argv: List[str], capsys):
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.fixture()
def fake_embed(monkeypatch):
    calls: List[int] = []

    async def embed(texts, **_kwargs):
        calls.append(len(texts))
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    return calls


@pytest.fixture()
def live_dir(tmp_path, monkeypatch):
    """A LIVE web index directory that does not exist yet (fresh deployment)."""
    live = str(tmp_path / "lancedb-web")
    monkeypatch.setattr(settings, "lancedb_web_dir", live)
    return live


# ---------------------------------------------------------------------------
# knowledge_admin
# ---------------------------------------------------------------------------


def test_pages_filters_by_domain_origin_and_introducer(capsys):
    a = _page("https://docs.example.com/a", MEDIUM, origin="share", introducer=7, conv="c1")
    b = _page("https://example.com/b", MEDIUM, origin="search")
    c = _page("https://other.org/c", MEDIUM, origin="crawl", introducer=9)

    rc, out, _ = _run(knowledge_admin.main, ["pages", "--domain", "example.com"], capsys)
    assert rc == 0
    assert "web_pages: 2" in out and "search=1" in out and "share=1" in out
    assert f"{a:>7}" in out and f"{b:>7}" in out and f"{c:>7}" not in out

    rc, out, _ = _run(knowledge_admin.main, ["pages", "--origin", "share"], capsys)
    assert "web_pages: 1" in out and "docs.example.com" in out

    rc, out, _ = _run(knowledge_admin.main, ["pages", "--introducer", "9"], capsys)
    assert "web_pages: 1" in out and "other.org" in out


def test_quarantine_prints_counts_and_refuses_without_yes(capsys):
    a = _page("https://docs.example.com/a", MEDIUM, origin="share", introducer=7)
    _page("https://example.com/b", MEDIUM)

    rc, out, _ = _run(knowledge_admin.main, ["quarantine", "--introducer", "7"], capsys)
    assert rc == 0
    assert "web_pages: 1" in out and "pages this would change: 1" in out
    assert "nothing changed" in out
    assert _pages_row(a)["quarantined_at"] is None

    rc, out, _ = _run(knowledge_admin.main, ["quarantine", "--introducer", "7", "--yes"], capsys)
    assert rc == 0 and "quarantined 1 page(s)" in out
    assert _pages_row(a)["quarantined_at"] is not None

    # Idempotent: already-quarantined pages are not counted as changes.
    rc, out, _ = _run(knowledge_admin.main, ["quarantine", "--introducer", "7", "--yes"], capsys)
    assert "pages this would change: 0" in out

    rc, out, _ = _run(knowledge_admin.main, ["pages", "--quarantined"], capsys)
    assert "web_pages: 1" in out and " Q " in out

    rc, out, _ = _run(knowledge_admin.main, ["unquarantine", "--id", str(a), "--yes"], capsys)
    assert rc == 0 and "unquarantined 1 page(s)" in out
    assert _pages_row(a)["quarantined_at"] is None


def test_quarantine_by_domain_covers_subdomains_and_needs_a_selector(capsys):
    a = _page("https://docs.example.com/a", MEDIUM)
    b = _page("https://www.example.com/b", MEDIUM)
    c = _page("https://notexample.com/c", MEDIUM)

    rc, _, err = _run(knowledge_admin.main, ["quarantine", "--yes"], capsys)
    assert rc == 2 and "--id, --domain, --introducer" in err

    rc, out, _ = _run(knowledge_admin.main, ["quarantine", "--domain", "example.com", "--yes"], capsys)
    assert "quarantined 2 page(s)" in out
    assert _pages_row(a)["quarantined_at"] is not None
    assert _pages_row(b)["quarantined_at"] is not None
    assert _pages_row(c)["quarantined_at"] is None


def test_claims_lists_by_page_and_introducer(capsys):
    a = _page("https://docs.example.com/a", MEDIUM, origin="share", introducer=7)
    b = _page("https://other.org/c", MEDIUM, introducer=9)
    _claim(a, "The founder is named on the page.", origin_user=7)
    _claim(b, "An unrelated claim.", origin_user=9)

    rc, out, _ = _run(knowledge_admin.main, ["claims", "--introducer", "7"], capsys)
    assert rc == 0 and "1 claim(s)" in out and "founder is named" in out and "unrelated" not in out

    rc, out, _ = _run(knowledge_admin.main, ["claims", "--page-id", str(b)], capsys)
    assert "1 claim(s)" in out and "unrelated" in out

    rc, out, _ = _run(knowledge_admin.main, ["claims", "--origin-user", "9"], capsys)
    assert "1 claim(s)" in out and "unrelated" in out


def test_purge_refuses_without_yes_and_deletes_only_the_introducers_pages(capsys):
    shared = _page("https://docs.example.com/a", MEDIUM, origin="share", introducer=7)
    # A second store of the same URL with different text writes a version row.
    assert _page("https://docs.example.com/a", MEDIUM + " Updated.", origin="share", introducer=7) == shared
    found = _page("https://example.com/found", MEDIUM, origin="search", introducer=7)
    other = _page("https://other.org/c", MEDIUM, introducer=9)
    _claim(shared, "A claim from the shared page.", origin_user=7)
    _claim(other, "A claim from someone else's page.", origin_user=9)
    assert _count("SELECT count(*) FROM web_page_versions WHERE page_id = %s", shared) == 1

    rc, out, err = _run(knowledge_admin.main, ["purge", "--introducer", "7"], capsys)
    assert rc == 2 and "REFUSED" in err
    assert "web_pages: 2" in out and "share=1" in out and "search=1" in out
    assert "web_claims on those pages: 1" in out and "web_page_versions of those pages: 1" in out
    assert _pages_row(shared) is not None and _pages_row(found) is not None

    # Narrowed to the origin that was never independently confirmed.
    rc, out, _ = _run(knowledge_admin.main, ["purge", "--introducer", "7", "--origin", "share", "--yes"], capsys)
    assert rc == 0
    assert "deleted web_pages=1 web_claims=1 web_page_versions=1" in out
    assert "still have chunk rows" in out
    assert _pages_row(shared) is None
    assert _pages_row(found) is not None and _pages_row(other) is not None
    # The orphan-claim trap: the FK is SET NULL, so the explicit delete matters.
    assert _count("SELECT count(*) FROM web_claims") == 1
    assert _count("SELECT count(*) FROM web_claims WHERE page_id IS NULL") == 0
    assert _count("SELECT count(*) FROM web_page_versions") == 0

    rc, out, _ = _run(knowledge_admin.main, ["purge", "--introducer", "7", "--yes"], capsys)
    assert rc == 0 and "deleted web_pages=1" in out
    assert _pages_row(found) is None and _pages_row(other) is not None

    rc, out, _ = _run(knowledge_admin.main, ["purge", "--introducer", "7", "--yes"], capsys)
    assert rc == 0 and "nothing to purge" in out


def test_purge_drop_vectors_removes_the_pages_chunks(capsys, fake_embed, live_dir, tmp_path):
    a = _page("https://docs.example.com/a", LONG, origin="share", introducer=7)
    b = _page("https://other.org/c", MEDIUM, introducer=9)
    out_dir = str(tmp_path / "built")
    report = asyncio.run(reindex_web.build(out_dir, progress_every=0))
    assert report.validated and report.rows == 3
    # Point the LIVE index at the build so the purge's vector cleanup hits it.
    settings.lancedb_web_dir = out_dir

    rc, out, _ = _run(knowledge_admin.main, ["purge", "--introducer", "7", "--drop-vectors", "--yes"], capsys)
    assert rc == 0 and "web index: removed 2 chunk row(s); 1 remain" in out
    assert _pages_row(a) is None and _pages_row(b) is not None
    settings.lancedb_web_dir = out_dir
    status = health._check_web_index()
    assert status["rows"] == 1 and status["distinct_pages"] == 1


# ---------------------------------------------------------------------------
# reindex_web
# ---------------------------------------------------------------------------


def test_build_writes_a_validated_index_with_the_live_sidecar_shape(fake_embed, live_dir, tmp_path):
    _page("https://docs.example.com/long", LONG)
    _page("https://example.com/medium", MEDIUM)
    _page("https://example.com/thin", THIN)
    out_dir = str(tmp_path / "lancedb-web.new")

    report = asyncio.run(reindex_web.build(out_dir, progress_every=0))

    assert report.validated, report.problems
    assert (report.pages_total, report.pages_indexed, report.pages_thin) == (3, 2, 1)
    assert report.chunks == 3 and report.rows == 3 and report.distinct_pages == 2
    assert report.dimension == 4
    assert fake_embed == [3]  # one batch — well under 64
    # The live setting is restored after the build; the new dir is separate.
    assert settings.lancedb_web_dir == live_dir
    assert not os.path.exists(live_dir)
    with open(metadata_path(out_dir), encoding="utf-8") as fh:
        sidecar = json.load(fh)
    assert sidecar["table"] == "web_chunks" and sidecar["dimension"] == 4
    assert sidecar["model_id"] == settings.embed_model
    assert sidecar["chunker_version"] == 1 and sidecar["schema_version"] == 1


def test_build_embeds_in_batches_of_64(fake_embed, live_dir, tmp_path):
    # 70 chunks: 35 pages x 2 chunks each -> batches of 64 + 6.
    for i in range(35):
        _page(f"https://example.com/p{i}", LONG + f" page {i}")
    report = asyncio.run(reindex_web.build(str(tmp_path / "new"), progress_every=0))
    assert report.validated and report.chunks == 70 and report.distinct_pages == 35
    assert fake_embed == [64, 6]


def test_build_refuses_the_live_directory_and_non_empty_targets(fake_embed, live_dir, tmp_path):
    _page("https://example.com/medium", MEDIUM)
    with pytest.raises(SystemExit, match="LIVE index directory"):
        asyncio.run(reindex_web.build(live_dir))
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "something").write_text("x")
    with pytest.raises(SystemExit, match="never deletes"):
        asyncio.run(reindex_web.build(str(busy)))
    assert not os.path.exists(live_dir)


def test_validate_catches_a_page_missing_from_the_table(fake_embed, live_dir, tmp_path):
    import lancedb

    a = _page("https://docs.example.com/long", LONG)
    _page("https://example.com/medium", MEDIUM)
    out_dir = str(tmp_path / "new")
    report = asyncio.run(reindex_web.build(out_dir, progress_every=0))
    assert report.validated

    lancedb.connect(out_dir).open_table("web_chunks").delete(f"page_id = {a}")
    report.problems.clear()
    reindex_web.validate(report)
    assert any("distinct page_id 1 != indexable pages 2" in p for p in report.problems)
    assert any("rows 1 != chunks embedded 3" in p for p in report.problems)


def test_main_prints_the_swap_and_the_rollback(fake_embed, live_dir, tmp_path, capsys):
    _page("https://example.com/medium", MEDIUM)
    out_dir = str(tmp_path / "new")
    rc, out, _ = _run(reindex_web.main, ["build", "--out", out_dir, "--progress-every", "0"], capsys)
    assert rc == 0
    assert "validated: distinct page_id == indexable pages" in out
    assert f"LANCEDB_WEB_DIR={out_dir}" in out
    assert f"set LANCEDB_WEB_DIR={live_dir} in .env" in out  # rollback
    assert "reset-watermark --since" in out

    # A --limit build is a smoke test and must never offer the swap.
    rc, out, _ = _run(reindex_web.main, ["build", "--out", str(tmp_path / "smoke"), "--limit", "1", "--progress-every", "0"], capsys)
    assert rc == 0 and "smoke test" in out and "LANCEDB_WEB_DIR=" not in out


def test_main_reports_validation_failure_without_swap_instructions(live_dir, tmp_path, capsys, monkeypatch):
    _page("https://example.com/medium", MEDIUM)
    _page("https://example.com/long", LONG)

    async def short_embed(texts, **_kwargs):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts[:-1]]  # one vector short

    monkeypatch.setattr(llm, "embed_texts", short_embed)
    with pytest.raises(RuntimeError, match="returned 2 vectors for 3 texts"):
        asyncio.run(reindex_web.build(str(tmp_path / "new"), progress_every=0))
    assert settings.lancedb_web_dir == live_dir  # restored on the error path too


def test_reset_watermark_counts_first_and_supports_since(capsys):
    a = _page("https://example.com/a", MEDIUM)
    b = _page("https://example.com/b", MEDIUM)
    db.mark_web_pages_indexed([a, b])
    assert _pages_row(a)["indexed_at"] is not None

    rc, out, _ = _run(reindex_web.main, ["reset-watermark"], capsys)
    assert rc == 0 and "would be queued for re-indexing (every page): 2" in out
    assert "nothing changed" in out and _pages_row(a)["indexed_at"] is not None

    rc, out, _ = _run(reindex_web.main, ["--reset-watermark", "--since", "2999-01-01T00:00:00Z", "--yes"], capsys)
    assert rc == 0 and "queued 0 page(s)" in out
    assert _pages_row(a)["indexed_at"] is not None

    rc, out, _ = _run(reindex_web.main, ["reset-watermark", "--since", "2000-01-01T00:00:00+00:00", "--yes"], capsys)
    assert rc == 0 and "queued 2 page(s)" in out
    assert _pages_row(a)["indexed_at"] is None and _pages_row(b)["indexed_at"] is None

    db.mark_web_pages_indexed([a, b])
    rc, out, _ = _run(reindex_web.main, ["reset-watermark", "--yes"], capsys)
    assert "queued 2 page(s)" in out
    assert db.count_unindexed_web_pages() == 2


# ---------------------------------------------------------------------------
# /health: web_index
# ---------------------------------------------------------------------------


def test_check_web_index_is_empty_before_any_index_exists(live_dir):
    status = health._check_web_index()
    assert status["status"] == "empty"
    assert status["directory"] == live_dir and status["table"] == "web_chunks"
    assert status["rows"] == 0 and status["distinct_pages"] == 0
    assert status["pending_pages"] == 0


def test_check_web_index_reports_rows_distinct_pages_and_backlog(fake_embed, live_dir, tmp_path):
    _page("https://docs.example.com/long", LONG)
    _page("https://example.com/medium", MEDIUM)
    out_dir = str(tmp_path / "new")
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated
    _page("https://example.com/later", MEDIUM)  # stored after the build: backlog of 1 for the worker
    settings.lancedb_web_dir = out_dir

    status = health._check_web_index()
    assert status["status"] == "ok"
    assert status["rows"] == 3 and status["distinct_pages"] == 2
    assert status["model_id"] == settings.embed_model and status["dimension"] == 4
    assert status["chunker_version"] == 1
    assert status["pending_pages"] == 3  # nothing has been marked indexed in the test store


def test_check_web_index_degrades_on_model_mismatch_instead_of_failing(fake_embed, live_dir, tmp_path, monkeypatch):
    _page("https://example.com/medium", MEDIUM)
    out_dir = str(tmp_path / "new")
    assert asyncio.run(reindex_web.build(out_dir, progress_every=0)).validated
    settings.lancedb_web_dir = out_dir
    monkeypatch.setattr(settings, "embed_model", "some/other-model")

    status = health._check_web_index()
    assert status["status"] == "degraded"
    assert "metadata model is" in status["detail"]
    assert status["rows"] == 0  # never opened an incompatible table


def test_check_dependencies_adds_web_index_without_touching_the_required_checks(monkeypatch, live_dir):
    async def endpoint_ok(client, base_url):
        return {"status": "ok"}

    async def optional_disabled(client):
        return {"status": "disabled", "detail": "disabled by configuration"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(health, "_probe_ocr", optional_disabled)
    monkeypatch.setattr(health, "_probe_reranker", optional_disabled)
    monkeypatch.setattr(health, "_check_duckdb", lambda path: {"status": "ok"})
    monkeypatch.setattr(health, "_check_app_db", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "_check_embedding_index", lambda: {"status": "empty", "detail": "not indexed yet"})
    monkeypatch.setattr(health, "_check_web_index", lambda: {"status": "degraded", "detail": "sidecar mismatch", "rows": 0, "distinct_pages": 0})

    result = asyncio.run(health.check_dependencies())

    assert result["status"] == "ok"
    assert set(result["checks"]) == {"vllm", "vllm-router", "vllm-embed", "duckdb", "app_db"}
    assert result["web_index"]["status"] == "degraded"
    assert result["capabilities"]["embed"]["status"] == "ok"
