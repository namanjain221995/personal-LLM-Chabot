"""Crash damage in the web index (2026-10-29): a power cut left the newest
Lance manifests empty, every open failed, and each pass embedded its pages on
the GPU before finding that out. The table repairs itself, a table it cannot
repair costs no GPU work, and neither failure stays silent."""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app import db, llm, metrics, web_index
from app.config import settings


def _page(url: str) -> int:
    text = ("A shorter page that still clears the chunk minimum. " * 12).strip()
    return int(
        db.upsert_web_page(
            url_key=url.split("//", 1)[-1], url=url, canonical_url=url, title="Page",
            text=text, content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )["id"]
    )


def _manifests() -> list:
    versions = os.path.join(settings.lancedb_web_dir, f"{web_index.TABLE}.lance", "_versions")
    return sorted(os.path.join(versions, n) for n in os.listdir(versions))


def _failures(path: str) -> float:
    return metrics._counters.get("web_index_failures_total", {}).get((("path", path),), 0.0)


def _indexed_page_ids() -> set:
    _conn, table, _meta = web_index._open()
    return set(table.to_arrow().column("page_id").to_pylist())


@pytest.fixture()
def embed_calls(tmp_path, monkeypatch):
    """A real table holding one indexed page; returns the embed call log."""
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "lancedb-web"))
    calls: list = []

    async def embed(texts, **_kwargs):
        calls.append(len(texts))
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    _page("https://example.com/first")
    assert asyncio.run(web_index.index_pending(limit=10)) >= 1
    calls.clear()
    return calls


def _crash(count: int = 2) -> list:
    """Truncate the newest manifests, as a power cut after a commit does."""
    tip = _manifests()[:count]
    for path in tip:
        open(path, "w").close()
    return tip


def test_empty_manifests_are_repaired_and_the_pass_completes(embed_calls):
    tip = _crash()
    with pytest.raises(RuntimeError):
        web_index._open()
    new_page = _page("https://example.com/second")

    assert asyncio.run(web_index.index_pending(limit=10)) >= 1

    assert not any(os.path.exists(p) and os.path.getsize(p) == 0 for p in tip)
    assert new_page in _indexed_page_ids()
    assert embed_calls


def test_a_page_stamped_after_the_last_durable_version_is_requeued(embed_calls):
    page_id = _page("https://example.com/lost")
    assert asyncio.run(web_index.index_pending(limit=10)) >= 1
    durable = _manifests()[2]  # delete + add are the two newest versions
    os.utime(durable, (0, 0))  # stamped long after this version reached the disk
    _crash()

    assert web_index._repair_crash_damage() is True

    assert page_id in {p["id"] for p in db.get_unindexed_web_pages(10, [page_id], 0)}
    assert page_id not in _indexed_page_ids()  # the commit really was lost


def test_a_page_stamped_before_the_last_durable_version_stays_indexed(embed_calls):
    page_id = _page("https://example.com/kept")
    assert asyncio.run(web_index.index_pending(limit=10)) >= 1
    other = _page("https://example.com/later")
    assert asyncio.run(web_index.index_pending(limit=10)) >= 1
    _crash()
    durable = _manifests()[2]
    future = 4_000_000_000
    os.utime(durable, (future, future))  # everything was stamped before it

    assert web_index._repair_crash_damage() is True

    assert db.get_unindexed_web_pages(10, [page_id, other], 0) == []


def test_other_damage_costs_no_gpu_and_counts_as_a_failure(embed_calls):
    tip = _manifests()[0]
    with open(tip, "wb") as handle:
        handle.write(b"not a manifest")
    page_id = _page("https://example.com/queued")
    before = _failures("index")

    assert asyncio.run(web_index.index_pending(limit=10)) == 0

    assert embed_calls == []
    assert _failures("index") == before + 1
    assert os.path.getsize(tip) == len(b"not a manifest")
    assert db.get_unindexed_web_pages(10, [page_id], 0)  # still queued


def test_a_table_with_no_surviving_manifest_is_left_alone(embed_calls):
    manifests = _manifests()
    for path in manifests:
        open(path, "w").close()

    assert web_index._repair_crash_damage() is False
    assert all(os.path.exists(p) for p in manifests)


def test_a_healthy_table_is_not_touched(embed_calls):
    manifests = _manifests()
    assert web_index._repair_crash_damage() is False
    assert _manifests() == manifests


def test_recall_on_a_broken_table_counts_as_a_failure(embed_calls):
    _crash()
    before = _failures("recall")

    assert asyncio.run(web_index.retrieve("first page", top_k=3)) == []

    assert _failures("recall") == before + 1
