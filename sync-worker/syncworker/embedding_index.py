"""Persistent compatibility metadata for the Salesforce embedding index.

The embedding model identity is part of a vector's meaning, not merely a
deployment setting.  A dimension match alone cannot prove that two models
share a vector space, so legacy or mismatched indexes are never relabelled or
rebuilt automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import tempfile


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
    """The configured embedding space cannot safely use an existing index."""


@dataclass(frozen=True)
class EmbeddingIndexMetadata:
    table: str
    model_id: str
    dimension: int
    schema_version: int = METADATA_SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "table": self.table,
            "model_id": self.model_id,
            "dimension": self.dimension,
        }


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


def write_metadata_once(
    lancedb_dir: str, metadata: EmbeddingIndexMetadata
) -> None:
    """Persist metadata atomically, refusing to overwrite any existing file."""
    os.makedirs(lancedb_dir, exist_ok=True)
    path = metadata_path(lancedb_dir)
    if os.path.exists(path):
        raise _error(f"metadata file {path!r} already exists", lancedb_dir)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{METADATA_FILENAME}.", suffix=".tmp", dir=lancedb_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(metadata.as_dict(), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            raise _error(f"metadata file {path!r} appeared concurrently", lancedb_dir)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def vector_dimension(table) -> int:
    try:
        vector_type = table.schema.field("vector").type
        dimension = int(vector_type.list_size)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("LanceDB table has no fixed-size `vector` column") from exc
    if dimension <= 0:
        raise ValueError("LanceDB vector dimension must be positive")
    return dimension


def validate_metadata(
    metadata: EmbeddingIndexMetadata,
    *,
    lancedb_dir: str,
    table: str,
    model_id: str,
    dimension: int,
) -> None:
    if metadata.table != table:
        raise _error(
            f"metadata names table {metadata.table!r}, configured table is {table!r}",
            lancedb_dir,
        )
    if metadata.model_id != model_id:
        raise _error(
            f"metadata model is {metadata.model_id!r}, configured model is {model_id!r}",
            lancedb_dir,
        )
    if metadata.dimension != dimension:
        raise _error(
            f"metadata dimension is {metadata.dimension}, table/request dimension is "
            f"{dimension}",
            lancedb_dir,
        )
