"""The web index read path (2026-09-13): one open per retrieval, no vector
column on the wire, a scalar index on page_id, and an ANN detector that a
scalar index cannot fool."""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from app import db, llm, web_index
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


@pytest.fixture()
def web_table(tmp_path, monkeypatch):
    """A real web_chunks table: two indexed pages plus 400 synthetic rows,
    enough for an IVF build to train."""
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "lancedb-web"))

    async def embed(texts, **_kwargs):
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    _page("https://example.com/one")
    _page("https://example.com/two")
    assert asyncio.run(web_index.index_pending(limit=10)) >= 2
    _conn, table, _meta = web_index._open()
    rng = np.random.default_rng(0)
    table.add([
        {"page_id": 10_000 + i, "url": f"https://synthetic.example/{i}", "title": "s",
         "text": "synthetic", "fetched_at": "2026-09-13",
         "vector": rng.standard_normal(4).astype(np.float32).tolist()}
        for i in range(400)
    ])
    monkeypatch.setattr(web_index, "_has_index", None)
    return table


def test_a_scalar_page_id_index_is_not_mistaken_for_the_ann_index(web_table, monkeypatch):
    web_table.create_scalar_index("page_id", index_type="BTREE", replace=True)
    assert web_index._page_id_index_present(web_table)
    monkeypatch.setattr(web_index, "_has_index", None)
    assert web_index._index_present(web_table) is False

    web_table.create_index(
        metric="l2", vector_column_name="vector", index_type="IVF_FLAT",
        num_partitions=2, replace=True,
    )
    monkeypatch.setattr(web_index, "_has_index", None)
    assert web_index._index_present(web_table) is True


def test_maintain_adds_the_page_id_index_and_still_builds_the_ann_index(web_table, monkeypatch):
    # The trap this closes: with the scalar index in place, the old name test
    # ("idx" in the name) reported an ANN index and the build never ran.
    web_table.create_scalar_index("page_id", index_type="BTREE", replace=True)
    monkeypatch.setattr(settings, "web_index_ann_min_rows", 1)
    monkeypatch.setattr(settings, "web_index_optimize_every", 10_000)
    upkeep = asyncio.run(web_index.maintain())
    assert upkeep["indexed"] is True
    _conn, table, _meta = web_index._open()
    columns = [list(i.columns) for i in table.list_indices()]
    assert ["page_id"] in columns and ["vector"] in columns


def test_maintain_builds_the_page_id_index_when_missing(web_table, monkeypatch):
    monkeypatch.setattr(settings, "web_index_ann_min_rows", 10**9)
    upkeep = asyncio.run(web_index.maintain())
    assert upkeep["page_id_indexed"] is True
    _conn, table, _meta = web_index._open()
    assert web_index._page_id_index_present(table)
    # Rollback safety: the orchestrator before 2026-09-14 took ANY index name
    # containing "idx" or "vector" for the ANN index, so the name the worker
    # gives the scalar index must contain neither (lancedb's default
    # `page_id_idx` would stop a rolled-back worker from ever building IVF).
    names = [str(i.name).lower() for i in table.list_indices() if list(i.columns) == ["page_id"]]
    assert names and all("idx" not in n and "vector" not in n for n in names), names
    # Idempotent: the next cycle does not rebuild it.
    assert asyncio.run(web_index.maintain())["page_id_indexed"] is False


def test_prefiltered_rounds_return_the_same_rows_with_the_scalar_index(web_table):
    vector = [1.0, 1.0, 0.5, 0.25]
    excluded = "page_id NOT IN (10000, 10001, 10002)"

    def run():
        return [
            (r["page_id"], round(r["_distance"], 6))
            for r in web_table.search(vector).limit(50).where(excluded).select(web_index._HIT_COLUMNS).to_list()
        ]

    before = run()
    web_table.create_scalar_index("page_id", index_type="BTREE", replace=True)
    after = run()
    assert before == after and len(after) == 50
    assert not {10000, 10001, 10002} & {pid for pid, _d in after}


class _CountingTable:
    def __init__(self, hits):
        self.hits = hits
        self.searches = 0
        self.selected = []

    def search(self, vector):
        self.searches += 1
        return self

    def limit(self, n):
        return self

    def where(self, clause):
        return self

    def select(self, columns):
        self.selected.append(list(columns))
        return self

    def list_indices(self):
        return []

    def to_list(self):
        return self.hits


def test_retrieve_opens_the_table_once_and_never_asks_for_the_vector(monkeypatch):
    monkeypatch.setattr(settings, "web_memory_enabled", True)

    async def fake_embed(texts, **kw):
        return [[0.0] * 4]

    # A full round (>= the 36-row budget of top_k=3), all one page, all near:
    # the only shape in which a second round can still find something.
    hits = [
        {"page_id": 7, "url": "https://a.example/p", "title": "A", "text": f"c{i}",
         "fetched_at": "d", "_distance": 0.4 + i / 1000}
        for i in range(40)
    ]
    table = _CountingTable(hits)
    opens = []

    def fake_open(create_dim=None):
        opens.append(1)
        return None, table, SimpleNamespace(dimension=4)

    monkeypatch.setattr(web_index.llm, "embed_texts", fake_embed)
    monkeypatch.setattr(web_index, "_open", fake_open)
    monkeypatch.setattr(web_index, "validate_query_dimension", lambda *a, **k: None)
    monkeypatch.setattr(web_index, "_servable_page_ids", lambda ids: {int(i) for i in ids if i})
    out = asyncio.run(web_index.retrieve("q", top_k=3))
    assert [o["url"] for o in out] == ["https://a.example/p"]
    assert table.searches == 2, "one page short of top_k must run a second round"
    assert len(opens) == 1
    assert all("vector" not in cols and "_distance" in cols for cols in table.selected)
