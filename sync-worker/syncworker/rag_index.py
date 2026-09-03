"""RAG indexing of long-text Salesforce fields into LanceDB.

Pipeline per changed record: delete all existing chunks for that record_id,
re-chunk each configured long-text field (~800 tokens, 100 overlap), embed
the chunks via the OpenAI-compatible /embeddings endpoint served by vLLM
(EMBED_MODEL at the EMBED_VIA base URL, e.g. http://vllm-embed:30003/v1),
and insert rows into the LanceDB table chunks(vector, text, object,
record_id, field, system_modstamp).

Upkeep (ADR-0001 D8) lives here too, because this process is the table's
only writer and the orchestrator must never pay for it on a request:
periodic compaction + old-version pruning, and the IVF_FLAT vector index
once the table is big enough for a flat scan to hurt. See `maintain`.

lancedb is imported lazily so offline unit tests never need it installed.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx

from .chunking import chunk_text
from .embedding_index import (
    EmbeddingCompatibilityError,
    EmbeddingIndexMetadata,
    load_metadata,
    safe_reindex_guidance,
    validate_metadata,
    vector_dimension,
    write_metadata_once,
)

log = logging.getLogger("syncworker.rag_index")

TABLE_NAME = "chunks"
EMBED_BATCH_SIZE = 32
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")

#: The column the ANN index is built over; the orchestrator's reader looks
#: for an index on this column (engines/rag.retrieve) to decide nprobes.
VECTOR_COLUMN = "vector"


@dataclass(frozen=True)
class MaintenancePolicy:
    """When the writer compacts the table and (re)builds the vector index.

    Every delete+add cycle leaves a fragment and a version behind and nothing
    ever collected them. Measured on the deployment on 2026-09-03: 89,954 live
    rows in 257 fragments, 444.8 MB of live data, and 137,362 retained
    versions holding 3.1 GB on disk; the flat scan took 150 ms per question.
    """

    #: Compaction cadence, in sync cycles. main.py owns the cycle loop and
    #: this module cannot see its boundaries, so "N cycles" is enforced as
    #: N x the cycle interval on the wall clock: cycles are never closer than
    #: SYNC_INTERVAL_MINUTES apart, so this can never run MORE often than
    #: once per N cycles, and it runs only after something was written.
    optimize_every_cycles: int = 12
    cycle_seconds: float = 30 * 60.0
    #: Versions younger than this survive a prune. Readers open the latest
    #: version per request, so a week is generous; it exists so a snapshot
    #: someone is inspecting by hand does not vanish under them.
    keep_versions_for: timedelta = timedelta(days=7)
    #: Below this many rows the flat scan is the index (measured 19 ms at 9k
    #: rows on the web table); above it IVF_FLAT is built — measured 9 ms at
    #: recall@10 = 0.995 with 50 probes on a 90k-row copy, versus 150 ms flat.
    ann_min_rows: int = 50_000
    #: A build (or a failed build) is not attempted again sooner than this.
    ann_retry_seconds: float = 86_400.0

    @classmethod
    def from_env(cls) -> "MaintenancePolicy":
        return cls(
            optimize_every_cycles=int(os.getenv("RAG_OPTIMIZE_EVERY_CYCLES", "12")),
            cycle_seconds=int(os.getenv("SYNC_INTERVAL_MINUTES", "30")) * 60.0,
            keep_versions_for=timedelta(
                days=int(os.getenv("RAG_OPTIMIZE_KEEP_DAYS", "7"))
            ),
            ann_min_rows=int(os.getenv("RAG_ANN_MIN_ROWS", "50000")),
        )

    @property
    def optimize_interval_seconds(self) -> float:
        return max(1, self.optimize_every_cycles) * max(1.0, self.cycle_seconds)


def ann_partitions(rows: int) -> int:
    """IVF partition count: clamp(sqrt(rows), 32, 1024).

    sqrt(N) is the usual lists-per-vectors rule of thumb (300 partitions at
    90k rows, ~300 vectors each); the floor keeps a small table from
    degenerating into a handful of huge lists and the ceiling bounds the
    k-means training cost.
    """
    return max(32, min(1024, int(math.sqrt(max(0, int(rows))))))


class OpenAIEmbedder:
    """Embeds text batches via an OpenAI-compatible /embeddings endpoint.

    base_url is an OpenAI-compatible base like http://vllm-embed:30003/v1;
    explicit response indices are validated and restored to input order.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        http: httpx.Client | None = None,
        *,
        api_key: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = http or httpx.Client(timeout=300.0)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        dimension: int | None = None
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            resp = self._http.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": batch},
                headers=self._headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                returned = len(data) if isinstance(data, list) else 0
                raise RuntimeError(
                    f"embedder returned {returned} vectors for {len(batch)} texts"
                )
            indexed: dict[int, list[float]] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise RuntimeError("embedding response data item is not an object")
                try:
                    index = int(item["index"])
                    vector = item["embedding"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "embedding response item needs an index and vector"
                    ) from exc
                if (
                    index < 0
                    or index >= len(batch)
                    or index in indexed
                    or not isinstance(vector, list)
                    or not vector
                ):
                    raise RuntimeError("embedding response indices or vectors are invalid")
                if dimension is None:
                    dimension = len(vector)
                if len(vector) != dimension:
                    raise RuntimeError("embedding response changed vector dimension")
                indexed[index] = vector
            if set(indexed) != set(range(len(batch))):
                raise RuntimeError("embedding response indices are incomplete")
            vectors.extend(indexed[index] for index in range(len(batch)))
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors

    @property
    def model_id(self) -> str:
        return self._model


class RagIndexer:
    def __init__(
        self,
        lancedb_dir: str,
        embedder: OpenAIEmbedder,
        *,
        policy: MaintenancePolicy | None = None,
        clock=time.monotonic,
    ) -> None:
        self._dir = lancedb_dir
        self._embedder = embedder
        self._db = None
        self._policy = policy or MaintenancePolicy.from_env()
        self._clock = clock
        # Maintenance bookkeeping (see `maintain`). A fresh process compacts
        # on its first write: the table it inherits may carry months of
        # uncollected versions, and waiting N cycles buys nothing.
        self._wrote_since_optimize = False
        self._optimized_at: float | None = None
        self._ann_attempted_at: float | None = None

    def _connect(self):
        if self._db is None:
            import lancedb  # lazy: not needed by offline tests

            self._db = lancedb.connect(self._dir)
        return self._db

    def _open_or_create_table(self, dim: int):
        db = self._connect()
        if TABLE_NAME in db.table_names():
            return self._validate_existing_table(db.open_table(TABLE_NAME), dim)

        metadata = load_metadata(self._dir)
        if metadata is not None:
            validate_metadata(
                metadata,
                lancedb_dir=self._dir,
                table=TABLE_NAME,
                model_id=self._embedder.model_id,
                dimension=dim,
            )

        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("text", pa.string()),
                pa.field("object", pa.string()),
                pa.field("record_id", pa.string()),
                pa.field("field", pa.string()),
                pa.field("system_modstamp", pa.string()),
            ]
        )
        table = db.create_table(TABLE_NAME, schema=schema)
        if metadata is None:
            write_metadata_once(
                self._dir,
                EmbeddingIndexMetadata(
                    table=TABLE_NAME,
                    model_id=self._embedder.model_id,
                    dimension=dim,
                ),
            )
        return table

    def _compatibility_error(self, reason: str) -> EmbeddingCompatibilityError:
        return EmbeddingCompatibilityError(
            f"Embedding index compatibility check failed: {reason}. "
            + safe_reindex_guidance(self._dir)
        )

    def _validate_existing_table(self, table, incoming_dim: int | None = None):
        try:
            table_dim = vector_dimension(table)
        except ValueError as exc:
            raise self._compatibility_error(str(exc)) from exc
        metadata = load_metadata(self._dir)
        if metadata is None:
            # An empty legacy table contains no vector space to mislabel, so it
            # is safe to attach metadata. A non-empty one is deliberately left
            # untouched because its model identity cannot be inferred.
            if table.count_rows() != 0:
                raise self._compatibility_error(
                    "a non-empty LanceDB table has no embedding metadata"
                )
            # Do not claim even an empty legacy table until the endpoint has
            # demonstrated that its vector width matches the fixed schema.
            if incoming_dim is None:
                return table
            if incoming_dim != table_dim:
                raise self._compatibility_error(
                    f"embedding endpoint returned dimension {incoming_dim}, existing "
                    f"table uses {table_dim}"
                )
            metadata = EmbeddingIndexMetadata(
                table=TABLE_NAME,
                model_id=self._embedder.model_id,
                dimension=table_dim,
            )
            write_metadata_once(self._dir, metadata)
        validate_metadata(
            metadata,
            lancedb_dir=self._dir,
            table=TABLE_NAME,
            model_id=self._embedder.model_id,
            dimension=table_dim,
        )
        if incoming_dim is not None and incoming_dim != table_dim:
            raise self._compatibility_error(
                f"embedding endpoint returned dimension {incoming_dim}, existing table "
                f"uses {table_dim}"
            )
        return table

    def _open_table_if_exists(self):
        db = self._connect()
        if TABLE_NAME in db.table_names():
            return self._validate_existing_table(db.open_table(TABLE_NAME))
        # A sidecar may survive a manually moved/deleted table. Its model
        # identity is still authoritative, so reject a different configured
        # model before sending warehouse content to the endpoint.
        metadata = load_metadata(self._dir)
        if metadata is not None:
            validate_metadata(
                metadata,
                lancedb_dir=self._dir,
                table=TABLE_NAME,
                model_id=self._embedder.model_id,
                dimension=metadata.dimension,
            )
        return None

    def delete_records(self, record_ids: list[str]) -> int:
        """Drop every chunk belonging to records deleted in Salesforce.

        Ids are validated against the Salesforce Id shape before being spliced
        into the delete predicate — same rule that keeps index_records' delete
        safe. Returns the number of ids acted on (0 when the table is absent).
        """
        valid = [str(r) for r in record_ids if _SF_ID_RE.match(str(r))]
        if not valid:
            return 0
        table = self._open_table_if_exists()
        if table is None:
            return 0
        for start in range(0, len(valid), 100):
            batch = valid[start : start + 100]
            predicate = ", ".join(f"'{rid}'" for rid in batch)
            table.delete(f"record_id IN ({predicate})")
        # A purge counts as a write, but upkeep is deferred to the next
        # index_records: main.py purges from INSIDE a warehouse session, and
        # a compaction there (minutes, the first time) would hold the DuckDB
        # write lock against the orchestrator's readers for its duration.
        self._wrote_since_optimize = True
        return len(valid)

    def index_records(
        self, object_name: str, records: list[dict], rag_fields: tuple[str, ...]
    ) -> int:
        """Re-index the given (changed) records. Returns chunk count inserted."""
        if not rag_fields or not records:
            return 0

        rows: list[dict] = []
        record_ids: list[str] = []
        for rec in records:
            record_id = rec.get("Id")
            if not record_id or not _SF_ID_RE.match(str(record_id)):
                continue
            record_ids.append(str(record_id))
            modstamp = rec.get("SystemModstamp")
            for field_name in rag_fields:
                value = rec.get(field_name)
                if value is None:
                    continue
                for chunk in chunk_text(str(value)):
                    rows.append(
                        {
                            "text": chunk,
                            "object": object_name,
                            "record_id": str(record_id),
                            "field": field_name,
                            "system_modstamp": str(modstamp) if modstamp else "",
                        }
                    )

        # Validate any known model identity before sending record content to
        # the embedding endpoint. Dimension is checked again after embedding,
        # still before any existing chunk is deleted.
        table = self._open_table_if_exists()
        if rows:
            vectors = self._embedder.embed([r["text"] for r in rows])
            for row, vec in zip(rows, vectors):
                row["vector"] = [float(x) for x in vec]
            dimension = len(rows[0]["vector"])
            if table is None:
                table = self._open_or_create_table(dim=dimension)
            else:
                table = self._validate_existing_table(table, dimension)
        else:
            # Nothing to insert, but changed records may still have stale
            # chunks (long text cleared) that must be removed.
            if table is None:
                return 0

        # Record change => drop that record's old chunks, then re-insert.
        # record_ids are validated as alphanumeric Salesforce Ids above.
        for rid in record_ids:
            table.delete(f"record_id = '{rid}'")
        if rows:
            table.add(rows)
            log.info(
                "rag chunks indexed",
                extra={
                    "event": "rag_indexed",
                    "object": object_name,
                    "records": len(record_ids),
                    "chunks": len(rows),
                },
            )
        if record_ids:
            # Outside any warehouse session (see sync_object's lock
            # discipline), so upkeep here never blocks a reader.
            self._wrote_since_optimize = True
            self.maintain(table)
        return len(rows)

    # ------------------------------------------------------------------
    # Upkeep (ADR-0001 D8): compaction, version pruning, the ANN index
    # ------------------------------------------------------------------

    def maintain(self, table=None, *, force: bool = False) -> dict:
        """Compact the table and keep the vector index in step with its size.

        Runs from the write path — after a batch was indexed; a purge only
        marks the table dirty — never from a request: the orchestrator only
        reads this table. Two
        gated jobs, each recorded as attempted BEFORE it runs so a failing
        job is retried on its cadence rather than on every batch:

        - OPTIMIZE (compact fragments, prune versions older than
          `keep_versions_for`, fold unindexed rows into the index) when
          something was written since the last run and at least
          `optimize_every_cycles` cycles' worth of time has passed. Logs the
          fragment/version/byte counts before and after.
        - ANN INDEX (IVF_FLAT, l2, sqrt(rows) partitions) once the table
          holds `ann_min_rows` rows and no vector index exists, at most one
          attempt per `ann_retry_seconds`.

        `force` runs both now (an operator's "rebuild", or a test). Failures
        are logged and swallowed: the sync must never stall on upkeep.
        Returns what happened, for logs and tests.
        """
        out = {"optimized": False, "indexed": False, "rows": 0}
        try:
            if table is None:
                table = self._open_table_if_exists()
                if table is None:
                    return out
            now = float(self._clock())
            if force or self._optimize_due(now):
                self._optimized_at = now
                self._wrote_since_optimize = False
                out["optimized"] = self._optimize(table)

            rows = int(table.count_rows())
            out["rows"] = rows
            if rows >= self._policy.ann_min_rows and (force or self._ann_due(now)):
                if force or not self._vector_index_present(table):
                    self._ann_attempted_at = now
                    out["indexed"] = self._build_vector_index(table, rows)
        except Exception:
            log.error(
                "rag index maintenance failed; will retry on its cadence",
                exc_info=True,
                extra={"event": "rag_maintenance_error"},
            )
        return out

    def _optimize_due(self, now: float) -> bool:
        if not self._wrote_since_optimize:
            return False
        if self._optimized_at is None:
            return True
        return now - self._optimized_at >= self._policy.optimize_interval_seconds

    def _ann_due(self, now: float) -> bool:
        if self._ann_attempted_at is None:
            return True
        return now - self._ann_attempted_at >= self._policy.ann_retry_seconds

    def _optimize(self, table) -> bool:
        before = self._table_shape(table)
        started = time.perf_counter()
        try:
            table.optimize(cleanup_older_than=self._policy.keep_versions_for)
        except TypeError:
            # Older client without the keyword: compaction still happens,
            # old versions stay until an upgrade.
            table.optimize()
        after = self._table_shape(table)
        log.info(
            "rag table compacted",
            extra={
                "event": "rag_optimized",
                "fragments_before": before["fragments"],
                "fragments_after": after["fragments"],
                "versions_before": before["versions"],
                "versions_after": after["versions"],
                "bytes_before": before["bytes"],
                "bytes_after": after["bytes"],
                "seconds": round(time.perf_counter() - started, 2),
            },
        )
        return True

    def _table_shape(self, table) -> dict:
        """Fragment, retained-version and byte counts, each None if unknown."""
        shape: dict = {"fragments": None, "versions": None, "bytes": None}
        try:
            stats = table.stats()
            stats = dict(stats) if not isinstance(stats, dict) else stats
            fragments = stats.get("fragment_stats") or {}
            shape["fragments"] = fragments.get("num_fragments")
            shape["bytes"] = stats.get("total_bytes")
        except Exception:  # noqa: BLE001 — a stats gap must not block the prune
            pass
        shape["versions"] = self._version_count(table)
        return shape

    def _version_count(self, table) -> int | None:
        """Retained manifests, counted from the directory.

        `list_versions()` reads every manifest: measured 28.75 s for the
        137,362 versions the production table had accumulated — on the very
        run whose job is to delete them. One manifest file per version is
        the on-disk layout, so a directory listing gives the same figure in
        milliseconds; the API call stays as the fallback for any layout this
        does not recognise.
        """
        versions_dir = os.path.join(self._dir, f"{TABLE_NAME}.lance", "_versions")
        try:
            with os.scandir(versions_dir) as entries:
                return sum(1 for entry in entries if entry.name.endswith(".manifest"))
        except OSError:
            pass
        try:
            return len(table.list_versions())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _vector_index_present(table) -> bool:
        for index in table.list_indices():
            columns = [str(c) for c in (getattr(index, "columns", None) or [])]
            name = str(getattr(index, "name", index))
            if VECTOR_COLUMN in columns or VECTOR_COLUMN in name.lower():
                return True
        return False

    def _build_vector_index(self, table, rows: int) -> bool:
        partitions = ann_partitions(rows)
        started = time.perf_counter()
        try:
            from lancedb.index import IvfFlat

            # The unified API (column first, config object) is the one that is
            # not deprecated on 0.37/0.38; `replace=True` swaps the previous
            # index atomically, so a reader never sees the table without one.
            table.create_index(
                VECTOR_COLUMN,
                config=IvfFlat(distance_type="l2", num_partitions=partitions),
                replace=True,
            )
        except ImportError:
            # Older client: the keyword form builds the same index.
            table.create_index(
                metric="l2",
                vector_column_name=VECTOR_COLUMN,
                index_type="IVF_FLAT",
                num_partitions=partitions,
                replace=True,
            )
        log.info(
            "rag vector index built",
            extra={
                "event": "rag_ann_built",
                "index_type": "IVF_FLAT",
                "rows": rows,
                "partitions": partitions,
                "seconds": round(time.perf_counter() - started, 2),
            },
        )
        return True
