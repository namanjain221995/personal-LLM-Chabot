"""RAG indexing of long-text Salesforce fields into LanceDB.

Pipeline per changed record: delete all existing chunks for that record_id,
re-chunk each configured long-text field (~800 tokens, 100 overlap), embed
the chunks via the OpenAI-compatible /embeddings endpoint served by vLLM
(EMBED_MODEL at the EMBED_VIA base URL, e.g. http://vllm-embed:30003/v1),
and insert rows into the LanceDB table chunks(vector, text, object,
record_id, field, system_modstamp).

lancedb is imported lazily so offline unit tests never need it installed.
"""

from __future__ import annotations

import logging
import re

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
    def __init__(self, lancedb_dir: str, embedder: OpenAIEmbedder) -> None:
        self._dir = lancedb_dir
        self._embedder = embedder
        self._db = None

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
        return len(rows)
