"""Read-only compatibility checks for the Salesforce LanceDB index."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import threading


METADATA_FILENAME = "_techsara_embedding_index.json"
METADATA_SCHEMA_VERSION = 1


def _env_int(name: str, default: int) -> int:
    """config._int semantics (unset or blank -> default), read here until the
    tunable moves into config.py."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


#: ONE LanceDB cache for the whole process (2026-09-13).
#:
#: Every read path connects per request (`lancedb.connect` + `open_table`) so
#: it always sees the version another writer — the sync-worker, the crawl, the
#: web worker — committed a moment ago. Each `connect()` without a `session`
#: builds a NEW cache (6 GiB index + 1 GiB metadata ceilings), which the next
#: request throws away. With an IVF index present the query then reloads the
#: index from disk every time. Measured on a synthetic 1024-dim web_chunks
#: table (dbperf 2026-09-13, lancedb 0.37.1, nprobes 50, refine 2, limit 90):
#:
#:     20k rows   per-request connect 42.5 ms p50 / 91.8 ms p95, RSS 2.06 GB
#:                shared 512 MB session 10.4 ms / 12.1 ms,         RSS 0.37 GB
#:     200k rows  per-request connect 42.0 ms / 116.9 ms,        RSS 5.11 GB
#:                shared 512 MB session 17.9 ms / 21.6 ms,         RSS 0.82 GB
#:
#: (300 sequential queries per variant, separate processes.) Sharing the
#: session changes WHAT IS CACHED, not what is read: `open_table` on a fresh
#: connection still resolves the latest manifest, so rows another process
#: added or deleted, a rebuilt index and a deleted-and-recreated directory are
#: all visible on the next request (tests/test_lancedb_shared_session.py).
#: The caps bound the process: 128 MB measured 34 ms at 200k rows (the index
#: no longer fits), 2 GB bought nothing over 512 MB. 0 for the index cap
#: restores a private session per connect.
LANCEDB_INDEX_CACHE_MB = _env_int("LANCEDB_INDEX_CACHE_MB", 512)
LANCEDB_METADATA_CACHE_MB = _env_int("LANCEDB_METADATA_CACHE_MB", 64)

_session = None
_session_lock = threading.Lock()


def lance_session():
    """The process-wide `lancedb.Session`, or None when sharing is disabled."""
    global _session
    if LANCEDB_INDEX_CACHE_MB <= 0:
        return None
    if _session is None:
        with _session_lock:
            if _session is None:
                import lancedb  # lazy

                _session = lancedb.Session(
                    index_cache_size_bytes=LANCEDB_INDEX_CACHE_MB << 20,
                    metadata_cache_size_bytes=max(1, LANCEDB_METADATA_CACHE_MB) << 20,
                )
    return _session


def connect(uri: str):
    """`lancedb.connect(uri)` on the shared session. Every orchestrator read
    and write path connects through here (tests pin that)."""
    import lancedb  # lazy

    session = lance_session()
    if session is None:
        return lancedb.connect(uri)
    return lancedb.connect(uri, session=session)


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
        db = connect(lancedb_dir)
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
