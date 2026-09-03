"""Table upkeep the writer owes the readers (ADR-0001 D8): compaction, version
pruning and the IVF_FLAT index — on a small temporary LanceDB table.

Real lancedb, real files; the clock is injected so "N cycles" and "once a
day" are asserted without sleeping. Nothing here talks to an embedding
service: the embedder is a deterministic stand-in.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

import pytest

from syncworker.rag_index import (
    TABLE_NAME,
    MaintenancePolicy,
    RagIndexer,
    ann_partitions,
)

MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIM = 8


class HashEmbedder:
    """Distinct, repeatable vectors per text — k-means needs spread, and a
    re-run must embed a record to the same point."""

    model_id = MODEL

    def embed(self, texts):
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([digest[i] / 255.0 for i in range(DIM)])
        return out


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _records(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "Id": f"001{i:015d}",
            "Description": f"Account {i} plans a renewable-energy expansion",
            "SystemModstamp": "2026-09-03T00:00:00Z",
        }
        for i in range(start, start + n)
    ]


def _indexer(tmp_path, policy: MaintenancePolicy, clock: Clock) -> RagIndexer:
    return RagIndexer(
        str(tmp_path / "lancedb"), HashEmbedder(), policy=policy, clock=clock
    )


#: Compaction every 2 "cycles" of 60 s; the index never (threshold out of reach).
NO_ANN = MaintenancePolicy(
    optimize_every_cycles=2,
    cycle_seconds=60.0,
    keep_versions_for=timedelta(0),
    ann_min_rows=10**9,
)


def _events(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _fragments(table) -> int:
    return int(table.stats()["fragment_stats"]["num_fragments"])


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def test_ann_partitions_is_sqrt_rows_clamped_to_32_and_1024():
    assert ann_partitions(0) == 32
    assert ann_partitions(10) == 32
    assert ann_partitions(1_024) == 32
    assert ann_partitions(90_000) == 300
    assert ann_partitions(2_000_000) == 1024


def test_policy_defaults_and_env_overrides(monkeypatch):
    for name in (
        "RAG_OPTIMIZE_EVERY_CYCLES",
        "SYNC_INTERVAL_MINUTES",
        "RAG_OPTIMIZE_KEEP_DAYS",
        "RAG_ANN_MIN_ROWS",
    ):
        monkeypatch.delenv(name, raising=False)
    default = MaintenancePolicy.from_env()
    assert default.optimize_every_cycles == 12
    assert default.optimize_interval_seconds == 12 * 30 * 60
    assert default.keep_versions_for == timedelta(days=7)
    assert default.ann_min_rows == 50_000
    assert default.ann_retry_seconds == 86_400

    monkeypatch.setenv("RAG_OPTIMIZE_EVERY_CYCLES", "3")
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("RAG_OPTIMIZE_KEEP_DAYS", "1")
    monkeypatch.setenv("RAG_ANN_MIN_ROWS", "500")
    custom = MaintenancePolicy.from_env()
    assert custom.optimize_interval_seconds == 3 * 5 * 60
    assert custom.keep_versions_for == timedelta(days=1)
    assert custom.ann_min_rows == 500


# ---------------------------------------------------------------------------
# compaction cadence
# ---------------------------------------------------------------------------


def test_first_write_compacts_then_waits_n_cycles(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    clock = Clock()
    indexer = _indexer(tmp_path, NO_ANN, clock)

    # A fresh process compacts on its first write: the table it inherits may
    # carry months of uncollected versions.
    assert indexer.index_records("Account", _records(1), ("Description",)) == 1
    assert len(_events(caplog, "rag_optimized")) == 1

    # Every changed record is a delete + add: fragments and versions pile up,
    # and inside the N-cycle window nothing collects them.
    for offset in range(1, 6):
        clock.now += 10
        indexer.index_records("Account", _records(1, start=offset), ("Description",))
    table = indexer._open_table_if_exists()
    assert table.count_rows() == 6
    assert _fragments(table) > 1
    assert len(_events(caplog, "rag_optimized")) == 1

    # Past N x the cycle interval the next write compacts, and the log
    # carries the before/after shape so the effect is visible in production.
    clock.now = 1_000.0 + NO_ANN.optimize_interval_seconds
    indexer.index_records("Account", _records(1, start=6), ("Description",))
    events = _events(caplog, "rag_optimized")
    assert len(events) == 2
    second = events[1]
    assert second.fragments_before > 1
    assert second.fragments_after == 1
    assert second.versions_before > second.versions_after
    assert second.bytes_before is not None and second.bytes_after is not None
    assert second.seconds >= 0
    assert _fragments(indexer._open_table_if_exists()) == 1
    assert indexer._open_table_if_exists().count_rows() == 7


def test_purges_count_as_writes_but_defer_the_work_out_of_the_session(
    tmp_path, caplog
):
    """main.py purges from inside a DuckDB session; compaction must not run
    there (it would hold the warehouse write lock), only be owed."""
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    clock = Clock()
    indexer = _indexer(tmp_path, NO_ANN, clock)
    indexer.index_records("Account", _records(3), ("Description",))
    assert len(_events(caplog, "rag_optimized")) == 1

    clock.now += NO_ANN.optimize_interval_seconds
    assert indexer.delete_records([r["Id"] for r in _records(1)]) == 1
    assert indexer._open_table_if_exists().count_rows() == 2
    assert len(_events(caplog, "rag_optimized")) == 1

    # The owed compaction happens on the next batch, outside the session.
    indexer.index_records("Account", _records(1, start=3), ("Description",))
    assert len(_events(caplog, "rag_optimized")) == 2


def test_a_batch_with_no_valid_ids_is_not_a_write(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    indexer = _indexer(tmp_path, NO_ANN, Clock())
    indexer.index_records("Account", _records(1), ("Description",))
    assert len(_events(caplog, "rag_optimized")) == 1

    indexer.maintain(force=True)  # settle the cadence
    assert indexer.index_records("Account", [{"Id": "not-a-salesforce-id"}], ("Description",)) == 0
    assert indexer._wrote_since_optimize is False


def test_maintain_is_a_no_op_without_a_table_or_without_writes(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    clock = Clock()
    indexer = _indexer(tmp_path, NO_ANN, clock)

    # No table yet: nothing to compact, nothing created.
    assert indexer.maintain() == {"optimized": False, "indexed": False, "rows": 0}
    assert not (tmp_path / "lancedb" / f"{TABLE_NAME}.lance").exists()

    indexer.index_records("Account", _records(2), ("Description",))
    assert len(_events(caplog, "rag_optimized")) == 1

    # Time alone does not trigger a compaction — only a write does.
    clock.now += 10 * NO_ANN.optimize_interval_seconds
    assert indexer.maintain()["optimized"] is False
    assert len(_events(caplog, "rag_optimized")) == 1

    # ... unless an operator forces it.
    assert indexer.maintain(force=True)["optimized"] is True
    assert len(_events(caplog, "rag_optimized")) == 2


# ---------------------------------------------------------------------------
# the vector index
# ---------------------------------------------------------------------------

WITH_ANN = MaintenancePolicy(
    optimize_every_cycles=2,
    cycle_seconds=60.0,
    keep_versions_for=timedelta(0),
    ann_min_rows=50,
)


def _vector_indices(table) -> list:
    return [i for i in table.list_indices() if "vector" in list(i.columns)]


def test_vector_index_is_built_once_rows_cross_the_threshold(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    clock = Clock()
    indexer = _indexer(tmp_path, WITH_ANN, clock)

    indexer.index_records("Account", _records(40), ("Description",))
    assert _vector_indices(indexer._open_table_if_exists()) == []
    assert _events(caplog, "rag_ann_built") == []

    indexer.index_records("Account", _records(20, start=40), ("Description",))
    table = indexer._open_table_if_exists()
    (index,) = _vector_indices(table)
    assert index.index_type.lower().replace("_", "") == "ivfflat"
    stats = table.index_stats(index.name)
    assert stats.distance_type == "l2"
    assert stats.num_indexed_rows == 60
    (built,) = _events(caplog, "rag_ann_built")
    assert built.rows == 60
    assert built.partitions == ann_partitions(60) == 32

    # Rows written after the build are searched flat and merged, so a fresh
    # record is findable at once; the next compaction folds it into the
    # index (that is what keeps nprobes honest as the table grows).
    indexer.index_records("Account", _records(1, start=60), ("Description",))
    table = indexer._open_table_if_exists()
    assert table.index_stats(index.name).num_unindexed_rows == 1
    needle = HashEmbedder().embed([_records(1, start=60)[0]["Description"]])[0]
    top = table.search(needle).limit(1).nprobes(4).refine_factor(2).to_list()[0]
    assert top["record_id"] == _records(1, start=60)[0]["Id"]

    clock.now += WITH_ANN.optimize_interval_seconds
    indexer.index_records("Account", _records(1, start=61), ("Description",))
    table = indexer._open_table_if_exists()
    assert table.index_stats(index.name).num_unindexed_rows == 0
    assert len(_vector_indices(table)) == 1
    # Once built, the index is not rebuilt on every write.
    assert len(_events(caplog, "rag_ann_built")) == 1


def test_failed_index_build_never_breaks_indexing_and_retries_daily(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="syncworker.rag_index")
    from lancedb.table import LanceTable

    attempts: list[int] = []

    def broken_create_index(self, *args, **kwargs):
        attempts.append(1)
        raise RuntimeError("k-means ran out of memory")

    monkeypatch.setattr(LanceTable, "create_index", broken_create_index)
    clock = Clock()
    indexer = _indexer(tmp_path, WITH_ANN, clock)

    # The chunks land even though the build blew up, and the failure is loud.
    assert indexer.index_records("Account", _records(60), ("Description",)) == 60
    assert indexer._open_table_if_exists().count_rows() == 60
    assert len(attempts) == 1
    assert len(_events(caplog, "rag_maintenance_error")) == 1

    # Not retried on the next batch...
    clock.now += 3600
    indexer.index_records("Account", _records(1, start=60), ("Description",))
    assert len(attempts) == 1

    # ...but it is a day later.
    clock.now += WITH_ANN.ann_retry_seconds
    indexer.index_records("Account", _records(1, start=61), ("Description",))
    assert len(attempts) == 2


def test_force_rebuilds_the_index_in_place(tmp_path, monkeypatch):
    from lancedb.table import LanceTable

    original = LanceTable.create_index
    seen: list[dict] = []

    def recording_create_index(self, *args, **kwargs):
        seen.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LanceTable, "create_index", recording_create_index)
    indexer = _indexer(tmp_path, WITH_ANN, Clock())
    indexer.index_records("Account", _records(60), ("Description",))
    assert len(seen) == 1

    out = indexer.maintain(force=True)
    assert out == {"optimized": True, "indexed": True, "rows": 60}
    assert len(seen) == 2
    # replace=True: the old index is swapped, never left beside a new one.
    assert all(call.get("replace") is True for call in seen)
    assert len(_vector_indices(indexer._open_table_if_exists())) == 1
