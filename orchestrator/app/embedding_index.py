"""Read-only compatibility checks for the Salesforce LanceDB index."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os


METADATA_FILENAME = "_techsara_embedding_index.json"
METADATA_SCHEMA_VERSION = 1


def safe_reindex_guidance(lancedb_dir: str) -> str:
    return (
        "No vectors were changed. Keep the existing index as a backup, set "
        f"LANCEDB_DIR to a new empty directory (not {lancedb_dir!r}), clear each "
        "indexed object's watermark with `python -m syncworker.objects resync "
        "<Object>`, and run a full sync."
    )


class EmbeddingCompatibilityError(RuntimeError):
    """The query model cannot safely search the persisted vector space."""


@dataclass(frozen=True)
class EmbeddingIndexMetadata:
    table: str
    model_id: str
    dimension: int
    schema_version: int


def metadata_path(lancedb_dir: str) -> str:
    return os.path.join(lancedb_dir, METADATA_FILENAME)


def _error(reason: str, lancedb_dir: str) -> EmbeddingCompatibilityError:
    return EmbeddingCompatibilityError(
        f"Embedding index compatibility check failed: {reason}. "
        + safe_reindex_guidance(lancedb_dir)
    )


def load_metadata(lancedb_dir: str) -> EmbeddingIndexMetadata | None:
    path = metadata_path(lancedb_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        metadata = EmbeddingIndexMetadata(
            schema_version=int(raw["schema_version"]),
            table=str(raw["table"]),
            model_id=str(raw["model_id"]),
            dimension=int(raw["dimension"]),
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise _error(f"metadata file {path!r} is invalid ({exc})", lancedb_dir) from exc
    if metadata.schema_version != METADATA_SCHEMA_VERSION:
        raise _error(
            f"metadata schema version is {metadata.schema_version}, expected "
            f"{METADATA_SCHEMA_VERSION}",
            lancedb_dir,
        )
    if not metadata.table or not metadata.model_id or metadata.dimension <= 0:
        raise _error("metadata contains empty or non-positive values", lancedb_dir)
    return metadata


def vector_dimension(table) -> int:
    try:
        dimension = int(table.schema.field("vector").type.list_size)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("LanceDB table has no fixed-size `vector` column") from exc
    if dimension <= 0:
        raise ValueError("LanceDB vector dimension must be positive")
    return dimension


def open_compatible_table(db, lancedb_dir: str, table_name: str, model_id: str):
    """Open an existing table only after model and schema identity agree."""
    if table_name not in db.table_names():
        raise FileNotFoundError(f"LanceDB table {table_name!r} does not exist")
    table = db.open_table(table_name)
    try:
        table_dim = vector_dimension(table)
    except ValueError as exc:
        raise _error(str(exc), lancedb_dir) from exc
    metadata = load_metadata(lancedb_dir)
    if metadata is None:
        if table.count_rows() == 0:
            raise FileNotFoundError(
                "embedding index does not exist yet (empty table has no metadata)"
            )
        raise _error("a non-empty LanceDB table has no embedding metadata", lancedb_dir)
    if metadata.table != table_name:
        raise _error(
            f"metadata names table {metadata.table!r}, configured table is "
            f"{table_name!r}",
            lancedb_dir,
        )
    if metadata.model_id != model_id:
        raise _error(
            f"metadata model is {metadata.model_id!r}, configured model is "
            f"{model_id!r}",
            lancedb_dir,
        )
    if metadata.dimension != table_dim:
        raise _error(
            f"metadata dimension is {metadata.dimension}, table dimension is "
            f"{table_dim}",
            lancedb_dir,
        )
    return table, metadata


def validate_query_dimension(
    metadata: EmbeddingIndexMetadata, query_vector: list[float], lancedb_dir: str
) -> None:
    actual = len(query_vector)
    if actual != metadata.dimension:
        raise _error(
            f"embedding endpoint returned dimension {actual}, index uses "
            f"{metadata.dimension}",
            lancedb_dir,
        )


def inspect_embedding_index(
    lancedb_dir: str, table_name: str, model_id: str
) -> dict:
    """Health-friendly inspection that never creates, writes, or repairs data."""
    if not os.path.isdir(lancedb_dir):
        return {"status": "empty", "detail": "embedding index has not been created"}
    try:
        import lancedb  # lazy

        db = lancedb.connect(lancedb_dir)
        _, metadata = open_compatible_table(db, lancedb_dir, table_name, model_id)
    except FileNotFoundError as exc:
        return {"status": "empty", "detail": str(exc)}
    except EmbeddingCompatibilityError as exc:
        return {"status": "error", "detail": str(exc)}
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "model_id": metadata.model_id,
        "dimension": metadata.dimension,
        "table": metadata.table,
    }
