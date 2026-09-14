"""LanceDB read-path budget and index upkeep (dbperf track lancedb, 2026-09-14).

- web_index.retrieve: a 12-per-page round budget, no second round when the
  first already reached past MAX_DISTANCE or came back short of its limit,
  and no `refine_factor` on an IVF_FLAT index (exact distances already).
- health._check_web_index: rows / distinct_pages cached on the table state,
  never stale across a write or a deleted-and-recreated directory.
- video/index: `maintain()` compacts after writes, builds the analysis_id
  scalar index, stays idle without writes, and the video maintenance loop
  calls it; video queries never read the vector column.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from app import db, health, llm, web_index
from app.config import settings


# ---------------------------------------------------------------------------
# web_index.retrieve round budget
# ---------------------------------------------------------------------------


class _Query:
    def __init__(self, table):
        self.t = table

    def limit(self, n):
        self.t.limits.append(int(n))
        return self

    def where(self, clause, *a, **k):
        return self

    def nprobes(self, n):
        self.t.nprobes.append(int(n))
        return self

    def refine_factor(self, n):
        self.t.refines.append(int(n))
        return self

    def bypass_vector_index(self):
        return self

    def select(self, columns):
        return self

    def to_list(self):
        rounds = self.t.rounds
        return rounds[min(len(self.t.limits) - 1, len(rounds) - 1)]


class _Table:
    def __init__(self, rounds, indices=()):
        self.rounds = rounds
        self.indices = list(indices)
        self.limits, self.nprobes, self.refines = [], [], []

    def search(self, vector):
        return _Query(self)

    def list_indices(self):
        return self.indices


def _hits(page_ids, distance=0.5, step=0.0):
    return [
        {"page_id": pid, "url": f"https://p{pid}.example/", "title": "t", "text": f"c{pid}-{i}",
         "fetched_at": "d", "_distance": distance + i * step}
        for i, pid in enumerate(page_ids)
    ]


@pytest.fixture()
def fake_retrieval(monkeypatch):
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "knowledge_ann_bypass", False)
    monkeypatch.setattr(web_index, "_has_index", None)

    async def fake_embed(texts, **kw):
        return [[0.0] * 4]

    monkeypatch.setattr(web_index.llm, "embed_texts", fake_embed)
    monkeypatch.setattr(web_index, "validate_query_dimension", lambda *a, **k: None)
    monkeypatch.setattr(web_index, "_servable_page_ids", lambda ids: {int(i) for i in ids if i})

    def install(table):
        monkeypatch.setattr(web_index, "_open", lambda create_dim=None: (None, table, SimpleNamespace(dimension=4)))
        return table

    return install


def test_a_round_asks_for_twelve_chunks_per_requested_page(fake_retrieval):
    table = fake_retrieval(_Table([_hits(range(1, 16))]))
    asyncio.run(web_index.retrieve("q", top_k=15))
    assert table.limits[0] == 180


def test_no_second_round_once_the_first_reached_past_max_distance(fake_retrieval):
    # 179 near chunks of 2 pages plus one past the floor: every row the scan
    # did not return is farther still, so another round can only be dropped.
    near = _hits([1, 2] * 89 + [2], distance=0.3)
    far = _hits([3], distance=web_index.MAX_DISTANCE + 0.05)
    table = fake_retrieval(_Table([near + far, _hits([9], distance=0.2)]))
    out = asyncio.run(web_index.retrieve("q", top_k=15))
    assert len(table.limits) == 1
    assert {o["url"] for o in out} == {"https://p1.example/", "https://p2.example/"}


def test_no_second_round_when_the_first_came_back_short_of_its_limit(fake_retrieval):
    table = fake_retrieval(_Table([_hits([1, 2, 3]), _hits([9])]))
    out = asyncio.run(web_index.retrieve("q", top_k=15))
    assert len(table.limits) == 1 and len(out) == 3


def test_a_full_round_of_near_rows_still_runs_the_next_round(fake_retrieval):
    swamp = _hits([1] * 180, distance=0.3, step=0.001)
    table = fake_retrieval(_Table([swamp, _hits([2, 3], distance=0.5)]))
    out = asyncio.run(web_index.retrieve("q", top_k=3))
    assert len(table.limits) == 2
    assert [o["url"] for o in out] == ["https://p1.example/", "https://p2.example/", "https://p3.example/"]


def _vector_index(kind):
    return SimpleNamespace(name="vector_idx", columns=["vector"], index_type=kind)


def test_an_ivf_flat_index_is_searched_without_refine(fake_retrieval):
    table = fake_retrieval(_Table([_hits([1, 2, 3])], indices=[_vector_index("IvfFlat"), SimpleNamespace(name="page_id_idx", columns=["page_id"], index_type="BTree")]))
    asyncio.run(web_index.retrieve("q", top_k=3))
    assert table.nprobes == [int(settings.web_index_nprobes)]
    assert table.refines == []


@pytest.mark.parametrize("kind", ["IvfPq", "IvfHnswSq", ""])
def test_a_lossy_or_unknown_vector_index_keeps_refine(fake_retrieval, kind):
    table = fake_retrieval(_Table([_hits([1, 2, 3])], indices=[_vector_index(kind)]))
    asyncio.run(web_index.retrieve("q", top_k=3))
    assert table.refines == [2]


def test_the_budget_rules_return_what_exhaustive_rounds_return_on_a_real_table(tmp_path, monkeypatch):
    """Real LanceDB, real rounds: one page swamps the first round, other near
    pages sit behind it, far pages behind those. The result must be the flat
    best-chunk-per-page ranking whatever the round budget."""
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "lancedb-web"))
    monkeypatch.setattr(web_index, "_has_index", None)
    text = ("A shorter page that still clears the chunk minimum. " * 12).strip()
    ids = []
    for n in range(12):
        url = f"https://budget{n}.example/"
        ids.append(int(db.upsert_web_page(
            url_key=url.split("//", 1)[-1], url=url, canonical_url=url, title="P", text=text + str(n),
            content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha256((text + str(n)).encode()).hexdigest(),
        )["id"]))
    rng = np.random.default_rng(1)
    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    def at(distance):
        v = query + np.array([0.0, distance, 0.0, 0.0], dtype=np.float32)
        return (v + rng.standard_normal(4).astype(np.float32) * 1e-4).tolist()

    rows = [{"page_id": ids[0], "url": "https://budget0.example/", "title": "P", "text": f"s{i}",
             "fetched_at": "d", "vector": at(0.1 + i * 0.001)} for i in range(200)]
    for n in range(1, 6):   # near pages behind the swamp
        rows.append({"page_id": ids[n], "url": f"https://budget{n}.example/", "title": "P", "text": "n",
                     "fetched_at": "d", "vector": at(0.5 + n * 0.01)})
    for n in range(6, 12):  # beyond MAX_DISTANCE
        rows.append({"page_id": ids[n], "url": f"https://budget{n}.example/", "title": "P", "text": "f",
                     "fetched_at": "d", "vector": at(1.5 + n * 0.01)})
    _conn, table, _meta = web_index._open(create_dim=4)
    table.add(rows)

    async def embed_query(question, **_kw):
        return query.tolist()

    monkeypatch.setattr(llm, "embed_query", embed_query)
    expected = ["https://budget0.example/"] + [f"https://budget{n}.example/" for n in range(1, 6)]
    for per_page in (6, 12):
        monkeypatch.setattr(web_index, "_CANDIDATES_PER_PAGE", per_page)
        out = asyncio.run(web_index.retrieve("q", top_k=10))
        assert [o["url"] for o in out] == expected, per_page


# ---------------------------------------------------------------------------
# /health web_index counts cache
# ---------------------------------------------------------------------------


def _web_rows(page_ids, dim=4, seed=0):
    rng = np.random.default_rng(seed)
    return [{"page_id": int(p), "url": f"https://h{p}.example/", "title": "t", "text": "x",
             "fetched_at": "d", "vector": rng.standard_normal(dim).astype(np.float32).tolist()} for p in page_ids]


@pytest.fixture()
def health_web_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "lancedb-web"))
    health.reset_dependency_cache()
    yield settings.lancedb_web_dir
    health.reset_dependency_cache()


def _spy_unique(monkeypatch):
    import pyarrow.compute as pc

    calls = []
    real = pc.unique
    monkeypatch.setattr(pc, "unique", lambda arr, *a, **k: calls.append(1) or real(arr, *a, **k))
    return calls


def test_health_web_index_counts_are_cached_until_the_table_changes(health_web_dir, monkeypatch):
    _conn, table, _meta = web_index._open(create_dim=4)
    table.add(_web_rows([1, 1, 2]))
    scans = _spy_unique(monkeypatch)

    first = health._check_web_index()
    assert (first["status"], first["rows"], first["distinct_pages"]) == ("ok", 3, 2)
    second = health._check_web_index()
    assert (second["rows"], second["distinct_pages"]) == (3, 2)
    assert len(scans) == 1, "an unchanged table must not be rescanned"

    web_index._open()[1].add(_web_rows([3, 4]))
    third = health._check_web_index()
    assert (third["rows"], third["distinct_pages"]) == (5, 4)
    assert len(scans) == 2


def test_health_web_index_counts_survive_a_recreated_directory(health_web_dir, monkeypatch):
    _conn, table, _meta = web_index._open(create_dim=4)
    table.add(_web_rows([1, 2, 3]))
    assert health._check_web_index()["distinct_pages"] == 3

    # Deleted and rebuilt: the same version numbers, different content.
    shutil.rmtree(health_web_dir)
    _conn, table, _meta = web_index._open(create_dim=4)
    table.add(_web_rows([7, 7]))
    again = health._check_web_index()
    assert (again["rows"], again["distinct_pages"]) == (2, 1)


# ---------------------------------------------------------------------------
# video index upkeep
# ---------------------------------------------------------------------------


@pytest.fixture()
def video_index(tmp_path, monkeypatch):
    from app.video import index

    dim = 8
    monkeypatch.setattr(settings, "lancedb_video_dir", str(tmp_path / "vid" / "lancedb-video"))
    monkeypatch.setattr(settings, "embed_model", "fake-embed")

    async def fake_embed_texts(texts, **kw):
        rng = np.random.default_rng(len(texts))
        return rng.standard_normal((len(texts), dim)).astype(np.float32).tolist()

    async def fake_embed_query(text, **kw):
        return [1.0] + [0.0] * (dim - 1)

    monkeypatch.setattr(llm, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(llm, "embed_query", fake_embed_query)
    monkeypatch.setattr(index, "_writes_since_optimize", 0)
    monkeypatch.setattr(index, "_prune_due_at", None)
    return index


def _chunks(n):
    return [{"chunk_ix": i, "modality": "speech", "start_s": float(i), "end_s": float(i + 1), "text": f"line {i}"} for i in range(n)]


def test_video_maintain_compacts_after_writes_and_indexes_analysis_id(video_index):
    index = video_index

    async def scenario():
        for aid in (1, 2, 3):
            await index.index_analysis(aid, _chunks(20))
        await index.index_analysis(2, _chunks(10))  # a re-run replaces rows
        await index.delete_analysis(3)
        _conn, table = index._open()
        fragments_before = table.stats()["fragment_stats"]["num_fragments"]
        upkeep = await index.maintain()
        _conn, table = index._open()
        return fragments_before, upkeep, table

    fragments_before, upkeep, table = asyncio.run(scenario())
    assert fragments_before > 1
    assert upkeep["optimized"] is True and upkeep["analysis_id_indexed"] is True
    assert upkeep["rows"] == 30
    assert table.stats()["fragment_stats"]["num_fragments"] == 1
    assert ["analysis_id"] in [list(i.columns) for i in table.list_indices()]
    assert index._writes_since_optimize == 0


def test_video_maintain_is_idle_without_new_writes(video_index):
    index = video_index

    async def scenario():
        await index.index_analysis(1, _chunks(5))
        first = await index.maintain()
        second = await index.maintain()
        await index.delete_analysis(1)
        third = await index.maintain()
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first["optimized"] is True
    assert second["optimized"] is False and second["analysis_id_indexed"] is False
    assert third["optimized"] is True


def test_video_maintain_prunes_the_replaced_versions_once_the_grace_has_passed(video_index, monkeypatch):
    import time

    index = video_index

    async def scenario():
        for aid in (1, 2, 3, 4):
            await index.index_analysis(aid, _chunks(5))
        compacted = await index.maintain()
        versions = lambda: len(os.listdir(os.path.join(settings.lancedb_video_dir, "video_chunks.lance", "_versions")))
        after_compaction = versions()
        assert index._prune_due_at is not None, "a compaction owes one later prune pass"
        idle = await index.maintain()  # inside the grace: nothing to do
        # The grace has passed (made zero so the files written a moment ago qualify).
        monkeypatch.setattr(index, "_PRUNE_GRACE_S", 0.0)
        monkeypatch.setattr(index, "_prune_due_at", time.monotonic() - 1)
        pruned = await index.maintain()
        after_prune = versions()
        settled = await index.maintain()
        return compacted, after_compaction, idle, pruned, after_prune, settled

    compacted, after_compaction, idle, pruned, after_prune, settled = asyncio.run(scenario())
    assert compacted["optimized"] is True and idle["optimized"] is False
    assert pruned["optimized"] is True and after_prune < after_compaction
    assert index._prune_due_at is None
    assert settled["optimized"] is False, "a prune-only pass does not schedule another"
    assert pruned["rows"] == 20


def test_video_maintain_never_creates_the_directory(video_index):
    index = video_index
    index._writes_since_optimize = 1
    upkeep = asyncio.run(index.maintain())
    assert upkeep == {"rows": 0, "optimized": False, "analysis_id_indexed": False}
    assert not os.path.exists(settings.lancedb_video_dir)


def test_video_queries_are_scoped_and_correct_with_the_scalar_index(video_index, monkeypatch):
    index = video_index
    from app import rerank

    async def passthrough(query, items, **kw):
        return list(items[: kw.get("top_n") or len(items)])

    monkeypatch.setattr(rerank, "order", passthrough)
    monkeypatch.setattr(index, "MAX_DISTANCE", 100.0)

    async def scenario():
        for aid in (1, 2, 3):
            await index.index_analysis(aid, _chunks(12))
        before = await index.retrieve("q", [2], top_k=50)
        await index.maintain()
        after = await index.retrieve("q", [2], top_k=50)
        return before, after, await index.best_distance("q", [1, 3])

    before, after, best = asyncio.run(scenario())
    assert {h["analysis_id"] for h in after} == {2} and len(after) == 12
    assert [(h["start_s"], h["text"]) for h in before] == [(h["start_s"], h["text"]) for h in after]
    assert best is not None


def test_video_queries_never_read_the_vector(video_index, monkeypatch):
    index = video_index
    selected = []

    class Q:
        def limit(self, n):
            return self

        def where(self, clause):
            return self

        def select(self, cols):
            selected.append(list(cols))
            return self

        def to_list(self):
            return [{"analysis_id": 1, "modality": "speech", "start_s": 0.0, "end_s": 1.0, "text": "t", "_distance": 0.1}]

    table = SimpleNamespace(search=lambda v: Q())
    monkeypatch.setattr(index, "_open", lambda create_dim=None: (None, table))
    from app import rerank

    async def passthrough(query, items, **kw):
        return list(items)

    monkeypatch.setattr(rerank, "order", passthrough)
    assert asyncio.run(index.retrieve("q", [1], top_k=3))
    assert asyncio.run(index.best_distance("q", [1])) == pytest.approx(0.1)
    assert len(selected) == 2
    assert all("vector" not in cols and "_distance" in cols for cols in selected)


def test_the_video_maintenance_loop_runs_index_upkeep(monkeypatch):
    from app.video import index, pipeline

    calls = []

    async def nothing(*a, **k):
        return 0

    async def upkeep(*a, **k):
        calls.append(1)
        raise asyncio.CancelledError  # stop the loop after one pass

    monkeypatch.setattr(pipeline, "drain_queue", nothing)
    monkeypatch.setattr(pipeline, "reap_orphans", nothing)
    monkeypatch.setattr(index, "maintain", upkeep)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pipeline.asyncio, "sleep", lambda s: real_sleep(0))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline._maintenance_loop())
    assert calls == [1]
