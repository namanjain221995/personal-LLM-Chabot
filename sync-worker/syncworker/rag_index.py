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

log = logging.getLogger("syncworker.rag_index")

TABLE_NAME = "chunks"
EMBED_BATCH_SIZE = 32
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")


class OpenAIEmbedder:
    """Embeds text batches via an OpenAI-compatible /embeddings endpoint.

    base_url is an OpenAI-compatible base like http://vllm-embed:30003/v1;
    the response's data[i].embedding entries are order-preserving.
    """

    def __init__(
        self, base_url: str, model: str, http: httpx.Client | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = http or httpx.Client(timeout=300.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            resp = self._http.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": batch},
            )
            resp.raise_for_status()
            vectors.extend(item["embedding"] for item in resp.json()["data"])
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors


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
        import pyarrow as pa

        db = self._connect()
        if TABLE_NAME in db.table_names():
            return db.open_table(TABLE_NAME)
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
        return db.create_table(TABLE_NAME, schema=schema)

    def _open_table_if_exists(self):
        db = self._connect()
        if TABLE_NAME in db.table_names():
            return db.open_table(TABLE_NAME)
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

        if rows:
            vectors = self._embedder.embed([r["text"] for r in rows])
            for row, vec in zip(rows, vectors):
                row["vector"] = [float(x) for x in vec]
            table = self._open_or_create_table(dim=len(rows[0]["vector"]))
        else:
            # Nothing to insert, but changed records may still have stale
            # chunks (long text cleared) that must be removed.
            table = self._open_table_if_exists()
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
