"""Embedding-space metadata and retry/backfill integrity tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncworker.embedding_index import (
    EmbeddingCompatibilityError,
    METADATA_FILENAME,
)
from syncworker.main import sync_object
from syncworker.rag_index import RagIndexer
from syncworker.storage import Store


MODEL = "Qwen/Qwen3-Embedding-0.6B"
RECORD_ID = "001000000000001AAA"


class FixedEmbedder:
    def __init__(self, model_id=MODEL, dimension=3):
        self.model_id = model_id
        self.dimension = dimension
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [
            [float(index + 1)] * self.dimension
            for index, _ in enumerate(texts)
        ]


def _record(text="An account with a renewable-energy expansion plan"):
    return {
        "Id": RECORD_ID,
        "Description": text,
        "SystemModstamp": "2026-08-11T00:00:00Z",
    }


def _index(tmp_path, *, model=MODEL, dimension=3):
    directory = tmp_path / "lancedb"
    indexer = RagIndexer(str(directory), FixedEmbedder(model, dimension))
    indexer.index_records("Account", [_record()], ("Description",))
    return directory, indexer


def test_new_index_persists_model_and_dimension_metadata(tmp_path):
    directory, indexer = _index(tmp_path)

    metadata = json.loads((directory / METADATA_FILENAME).read_text())
    assert metadata == {
        "schema_version": 1,
        "table": "chunks",
        "model_id": MODEL,
        "dimension": 3,
    }
    assert indexer._open_table_if_exists().count_rows() == 1


def test_model_mismatch_fails_without_replacing_existing_vectors(tmp_path):
    directory, original = _index(tmp_path)
    before = original._open_table_if_exists().to_arrow().to_pylist()
    embedder = FixedEmbedder("different/embedding-model", 3)
    incompatible = RagIndexer(str(directory), embedder)

    with pytest.raises(EmbeddingCompatibilityError, match="configured model") as exc:
        incompatible.index_records(
            "Account", [_record("replacement text")], ("Description",)
        )

    assert "new empty directory" in str(exc.value)
    assert "No vectors were changed" in str(exc.value)
    assert embedder.calls == []  # fail before sending record content anywhere
    assert original._open_table_if_exists().to_arrow().to_pylist() == before


def test_dimension_mismatch_fails_without_replacing_existing_vectors(tmp_path):
    directory, original = _index(tmp_path)
    before = original._open_table_if_exists().to_arrow().to_pylist()
    incompatible = RagIndexer(str(directory), FixedEmbedder(MODEL, 4))

    with pytest.raises(EmbeddingCompatibilityError, match="returned dimension 4"):
        incompatible.index_records(
            "Account", [_record("replacement text")], ("Description",)
        )

    assert original._open_table_if_exists().to_arrow().to_pylist() == before


def test_nonempty_legacy_table_is_not_silently_claimed_by_current_model(tmp_path):
    directory, original = _index(tmp_path)
    metadata = directory / METADATA_FILENAME
    metadata.unlink()  # simulate the pre-metadata release
    legacy_rows = original._connect().open_table("chunks").count_rows()
    reopened = RagIndexer(str(directory), FixedEmbedder())

    with pytest.raises(EmbeddingCompatibilityError, match="has no embedding metadata"):
        reopened.index_records("Account", [_record()], ("Description",))

    assert reopened._connect().open_table("chunks").count_rows() == legacy_rows
    assert not metadata.exists()


def test_empty_legacy_table_is_not_claimed_when_endpoint_dimension_differs(tmp_path):
    directory, original = _index(tmp_path)
    table = original._connect().open_table("chunks")
    table.delete(f"record_id = '{RECORD_ID}'")
    metadata = directory / METADATA_FILENAME
    metadata.unlink()

    incompatible = RagIndexer(str(directory), FixedEmbedder(MODEL, 4))
    with pytest.raises(EmbeddingCompatibilityError, match="returned dimension 4"):
        incompatible.index_records("Account", [_record()], ("Description",))

    assert table.count_rows() == 0
    assert not metadata.exists()


class ObjectConfig:
    name = "Account"
    fields = ("Id", "Description", "SystemModstamp")
    rag_fields = ("Description",)
    watermark_field = "SystemModstamp"


class SyncSettings:
    sync_auto_fields = False
    sync_max_fields = 80

    def __init__(self, parquet_dir):
        self.parquet_dir = str(parquet_dir)


class TwoCycleClient:
    def describe_fields(self, name):
        return {"Id", "Description", "SystemModstamp"}

    def bulk_query(self, soql):
        yield [_record()]

    def soql_query(self, soql):
        return iter(())  # second cycle has no changed records


class ColdThenReadyIndexer:
    def __init__(self):
        self.calls = []

    def index_records(self, object_name, records, rag_fields):
        self.calls.append((object_name, list(records), tuple(rag_fields)))
        if len(self.calls) == 1:
            raise RuntimeError("embedding model is still loading")
        return len(records)

    def delete_records(self, record_ids):
        return len(record_ids)


def test_initial_embedding_failure_is_backfilled_with_no_later_record_change(tmp_path):
    store = Store(str(tmp_path / "warehouse.duckdb"))
    settings = SyncSettings(tmp_path / "parquet")
    client = TwoCycleClient()
    indexer = ColdThenReadyIndexer()

    assert sync_object(ObjectConfig(), client, store, indexer, settings) == 1
    assert store.pending_rag_ids("Account") == [RECORD_ID]
    assert store.get_watermark("Account") is not None

    # The incremental Salesforce query is empty, but persisted warehouse data
    # is retried before that query and clears the durable pending marker.
    assert sync_object(ObjectConfig(), client, store, indexer, settings) == 0
    assert store.pending_rag_ids("Account") == []
    assert len(indexer.calls) == 2
    assert indexer.calls[1][1][0]["Description"].startswith("An account")
