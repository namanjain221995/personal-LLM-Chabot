"""One LanceDB session per process (embedding_index.connect, 2026-09-13).

The session only changes what is cached. These tests pin the two properties
that make it safe to share: every read still sees what another writer
committed (rows, deletes, a rebuilt index, a deleted-and-recreated directory
at the same version numbers), and no orchestrator code path connects around
the shared session.
"""
from __future__ import annotations

import ast
import pathlib
import shutil

import numpy as np
import pytest

from app import embedding_index

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
_DIM = 16


def _rows(tag: str, n: int, offset: int = 0, seed: int = 0):
    rng = np.random.default_rng(seed)
    return [
        {"id": offset + i, "tag": tag, "vector": rng.standard_normal(_DIM).astype(np.float32)}
        for i in range(n)
    ]


def _read(directory: str):
    table = embedding_index.connect(directory).open_table("t")
    hits = (
        table.search(np.zeros(_DIM, dtype=np.float32))
        .limit(10_000)
        .select(["tag", "_distance"])
        .to_list()
    )
    return table.count_rows(), sorted({h["tag"] for h in hits})


def test_connect_hands_every_caller_the_same_session(monkeypatch):
    import lancedb

    seen = []
    monkeypatch.setattr(lancedb, "connect", lambda uri, **kw: seen.append(kw.get("session")) or object())
    embedding_index.connect("/nowhere/a")
    embedding_index.connect("/nowhere/b")
    assert seen[0] is not None
    assert seen[0] is seen[1] is embedding_index.lance_session()


def test_a_zero_index_cache_restores_a_private_session_per_connect(monkeypatch):
    import lancedb

    seen = []
    monkeypatch.setattr(embedding_index, "LANCEDB_INDEX_CACHE_MB", 0)
    monkeypatch.setattr(lancedb, "connect", lambda uri, **kw: seen.append(kw) or object())
    embedding_index.connect("/nowhere")
    assert seen == [{}]


def test_the_shared_session_is_bounded_by_the_configured_caps(monkeypatch):
    import lancedb

    built = []
    monkeypatch.setattr(lancedb, "Session", lambda **kw: built.append(kw) or object())
    monkeypatch.setattr(embedding_index, "_session", None)
    monkeypatch.setattr(embedding_index, "LANCEDB_INDEX_CACHE_MB", 512)
    monkeypatch.setattr(embedding_index, "LANCEDB_METADATA_CACHE_MB", 64)
    first = embedding_index.lance_session()
    assert embedding_index.lance_session() is first  # built once
    # Bounded, unlike lancedb's per-connection default of 6 GiB + 1 GiB.
    assert built == [{"index_cache_size_bytes": 512 << 20, "metadata_cache_size_bytes": 64 << 20}]


def test_a_fresh_open_sees_other_writers_rebuilds_and_a_recreated_directory(tmp_path):
    import lancedb

    directory = str(tmp_path / "db")
    writer = lancedb.connect(directory).create_table("t", data=_rows("A", 600))
    assert _read(directory) == (600, ["A"])

    writer.create_index(
        metric="l2", vector_column_name="vector", index_type="IVF_FLAT",
        num_partitions=4, replace=True,
    )
    other = lancedb.connect(directory).open_table("t")  # a different process's handle
    other.add(_rows("A2", 7, offset=10_000, seed=1))
    assert _read(directory) == (607, ["A", "A2"])
    other.delete("tag = 'A2'")
    assert _read(directory) == (600, ["A"])

    # "Deleting the whole directory is always safe" (web_index): the rebuild
    # reuses the SAME version numbers, which is what a cache keyed carelessly
    # on (path, version) would get wrong.
    shutil.rmtree(directory)
    rebuilt = lancedb.connect(directory).create_table("t", data=_rows("B", 600, seed=2))
    assert _read(directory) == (600, ["B"])
    rebuilt.create_index(
        metric="l2", vector_column_name="vector", index_type="IVF_FLAT",
        num_partitions=4, replace=True,
    )
    table = embedding_index.connect(directory).open_table("t")
    hits = table.search(np.zeros(_DIM, dtype=np.float32)).limit(20).nprobes(4).select(["tag", "_distance"]).to_list()
    assert {h["tag"] for h in hits} == {"B"}


def test_no_orchestrator_code_connects_around_the_shared_session():
    offenders = []
    for path in _APP.rglob("*.py"):
        if path.name == "embedding_index.py" and path.parent == _APP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "lancedb"
            ):
                offenders.append(f"{path.relative_to(_APP)}:{node.lineno}")
    assert offenders == [], "use embedding_index.connect: " + ", ".join(offenders)
