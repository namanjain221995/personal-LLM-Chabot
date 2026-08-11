"""Read-only safeguards around the persisted Salesforce vector space."""
from __future__ import annotations

import json

import lancedb
import pyarrow as pa
import pytest

from app.embedding_index import (
    EmbeddingCompatibilityError,
    METADATA_FILENAME,
    inspect_embedding_index,
    open_compatible_table,
    validate_query_dimension,
)


MODEL = "Qwen/Qwen3-Embedding-0.6B"


def _table(tmp_path, *, dimension=3, with_row=True):
    directory = tmp_path / "lancedb"
    db = lancedb.connect(str(directory))
    schema = pa.schema(
        [
            pa.field("vector", pa.list_(pa.float32(), dimension)),
            pa.field("text", pa.string()),
            pa.field("object", pa.string()),
            pa.field("record_id", pa.string()),
            pa.field("field", pa.string()),
            pa.field("system_modstamp", pa.string()),
        ]
    )
    table = db.create_table("chunks", schema=schema)
    if with_row:
        table.add(
            [
                {
                    "vector": [0.1] * dimension,
                    "text": "account notes",
                    "object": "Account",
                    "record_id": "001000000000001AAA",
                    "field": "Description",
                    "system_modstamp": "t1",
                }
            ]
        )
    return directory, db, table


def _metadata(directory, *, model=MODEL, dimension=3):
    (directory / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "table": "chunks",
                "model_id": model,
                "dimension": dimension,
            }
        )
    )


def test_matching_model_table_and_query_dimensions_are_accepted(tmp_path):
    directory, db, table = _table(tmp_path)
    _metadata(directory)

    opened, metadata = open_compatible_table(
        db, str(directory), "chunks", MODEL
    )
    validate_query_dimension(metadata, [0.2, 0.3, 0.4], str(directory))

    assert opened.count_rows() == table.count_rows() == 1
    assert inspect_embedding_index(str(directory), "chunks", MODEL)["status"] == "ok"


def test_query_model_mismatch_is_actionable_and_does_not_touch_rows(tmp_path):
    directory, db, table = _table(tmp_path)
    _metadata(directory, model="old/model")
    before = table.to_arrow().to_pylist()

    with pytest.raises(EmbeddingCompatibilityError, match="configured model") as exc:
        open_compatible_table(db, str(directory), "chunks", MODEL)

    assert "new empty directory" in str(exc.value)
    assert table.to_arrow().to_pylist() == before
    status = inspect_embedding_index(str(directory), "chunks", MODEL)
    assert status["status"] == "error"
    assert "No vectors were changed" in status["detail"]


def test_query_vector_dimension_mismatch_fails_before_search(tmp_path):
    directory, db, _ = _table(tmp_path)
    _metadata(directory)
    _, metadata = open_compatible_table(db, str(directory), "chunks", MODEL)

    with pytest.raises(EmbeddingCompatibilityError, match="returned dimension 2"):
        validate_query_dimension(metadata, [0.1, 0.2], str(directory))


def test_nonempty_legacy_index_without_metadata_is_degraded_not_claimed(tmp_path):
    directory, db, table = _table(tmp_path)
    before = table.to_arrow().to_pylist()

    with pytest.raises(EmbeddingCompatibilityError, match="has no embedding metadata"):
        open_compatible_table(db, str(directory), "chunks", MODEL)

    assert table.to_arrow().to_pylist() == before
    assert not (directory / METADATA_FILENAME).exists()
