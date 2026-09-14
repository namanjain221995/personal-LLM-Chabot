"""Per-file vector index: embeddings through the embed engine, stored as a
flat float32 matrix beside the chunks, searched brute force (2026-09-13).

LAYOUT (inside one blob's `derived/` directory, which is itself inside
`<PUBLIC_API_FILES_DIR>/<project_id>/<sha256>/` — the vector store IS per
project, because the directory is):

    chunks.jsonl / chunks.idx   the rows (chunks.py)
    vectors.f32                 little-endian float32, `dim` per row, L2-normalised
    vectors.dim                 the dimension, written with the first batch
    index.json                  {dim, rows, chunks_total, truncated, model,
                                 embed_input_tokens, created_at} — written LAST,
                                 atomically; its presence means "index done"

WHY NOT LANCEDB (design §1.3). A LanceDB delete leaves the bytes in older
versions until an `optimize(cleanup_older_than=…)` runs — `/data/lancedb`'s
119 G `chunks.lance` is what accumulated versions look like. `DELETE
/v1/files/{id}` must remove a tenant's vectors physically and at once; here
that is the blob directory's `rmtree`. Search cost, measured for the design:
numpy `m @ q` + top-24 over 50,000 × 1,024 rows (195 MiB) = 4.1 ms/query.

RESUMABLE. Embedding 50,000 chunks is hours under chat load (design R9,
unmeasured). Each packed engine call's vectors are appended and fsynced
before the next call, so a lost lease or a restart resumes at the first row
without a vector instead of starting over. A torn final row (crash mid-write)
is truncated away before resuming.

DOCUMENTS VS QUERIES. Qwen3-Embedding is asymmetric: documents are embedded
bare, queries with an `Instruct:` prefix (`llm.QUERY_INSTRUCTION` is the web
variant; this one names documents — design §5.4).

ENGINE ACCESS. Every call goes through `publicapi.capacity.hold('embed', …)`,
the public side's FIFO gate, so file indexing never queues the chat app's
query embeddings behind it. The functions that touch the engine are
injectable (`DocumentEmbedder`, `QueryEmbedder`) so tests run against stubs
and never load a production engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

import numpy as np

from . import chunks as chunk_store

log = logging.getLogger(__name__)

VECTORS_NAME = "vectors.f32"
DIM_NAME = "vectors.dim"
INDEX_NAME = "index.json"
MODEL_LABEL = "techsara-embed"

QUERY_INSTRUCTION = (
    "Instruct: Given a question, retrieve passages of the document that answer it\nQuery: "
)

#: Inputs per engine call. Sixteen is `publicapi.sidecars.EMBED_CALL_MAX_INPUTS`:
#: a sub-second pooling pass on the 0.6B model, so chat query embeddings
#: interleave between public calls instead of queueing behind a long batch.
CALL_MAX_INPUTS = 16

#: (vectors, prompt_tokens or None) for a list of document texts.
DocumentEmbedder = Callable[[Sequence[str]], Awaitable[Tuple[Sequence[Sequence[float]], Optional[int]]]]
#: A query vector, or None when the engine could not be reached in time.
QueryEmbedder = Callable[[str], Awaitable[Optional[Sequence[float]]]]


def index_max_chunks() -> int:
    """PUBLIC_API_FILES_INDEX_MAX_CHUNKS (50,000): 195 MiB of 1,024-dim
    float32 and 4.1 ms per brute-force search, measured (design §8)."""
    from . import limits

    return max(1, limits.index_max_chunks())


@dataclass(frozen=True)
class IndexInfo:
    dim: int
    rows: int
    chunks_total: int
    truncated: bool
    model: str
    embed_input_tokens: Optional[int]
    created_at: int

    def to_json(self) -> dict:
        return {
            "dim": self.dim,
            "rows": self.rows,
            "chunks_total": self.chunks_total,
            "truncated": self.truncated,
            "model": self.model,
            "embed_input_tokens": self.embed_input_tokens,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "IndexInfo":
        tokens = data.get("embed_input_tokens")
        return cls(
            dim=int(data["dim"]),
            rows=int(data["rows"]),
            chunks_total=int(data.get("chunks_total", data["rows"])),
            truncated=bool(data.get("truncated", False)),
            model=str(data.get("model") or MODEL_LABEL),
            embed_input_tokens=(int(tokens) if tokens is not None else None),
            created_at=int(data.get("created_at") or 0),
        )


@dataclass(frozen=True)
class Hit:
    """One search result: the chunk number (== vector row) and its cosine."""

    row: int
    score: float


class IndexNotReady(RuntimeError):
    """No complete index.json in the directory."""


def query_text(question: str) -> str:
    return QUERY_INSTRUCTION + " ".join((question or "").split())


def normalise(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation, float32; an all-zero row stays zero."""
    data = np.asarray(matrix, dtype=np.float32)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    norms = np.linalg.norm(data, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (data / norms).astype("<f4", copy=False)


def read_index(derived_dir: str) -> Optional[IndexInfo]:
    try:
        with open(os.path.join(derived_dir, INDEX_NAME), "r", encoding="utf-8") as fh:
            return IndexInfo.from_json(json.load(fh))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- building --


def _read_dim(derived_dir: str) -> Optional[int]:
    try:
        with open(os.path.join(derived_dir, DIM_NAME), "r", encoding="ascii") as fh:
            value = int(fh.read().strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _resume_rows(derived_dir: str, dim: Optional[int]) -> int:
    """Whole rows already on disk; a torn tail is truncated away."""
    path = os.path.join(derived_dir, VECTORS_NAME)
    if dim is None:
        try:
            os.unlink(path)
        except OSError:
            pass
        return 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    whole = size // (dim * 4)
    if whole * dim * 4 != size:
        with open(path, "r+b") as fh:
            fh.truncate(whole * dim * 4)
            fh.flush()
            os.fsync(fh.fileno())
    return whole


class IndexWriteConflict(RuntimeError):
    """vectors.f32 is not the size this builder left it: another builder
    (a runner that lost its lease while waiting at the gate, and a new one
    that resumed) wrote in between."""


def _write_rows_at(derived_dir: str, rows: np.ndarray, *, first_row: int, dim: int) -> None:
    """Write `rows` as rows `first_row…`, only if the file holds exactly
    `first_row` rows now (2026-09-13 review: an `ab` append from an old
    builder after a new one resumed misaligned vectors and chunk numbers,
    while index.json still said `rows = len(texts)` — silently wrong excerpts
    and citations). `pwrite` at the computed offset, then fsync."""
    path = os.path.join(derived_dir, VECTORS_NAME)
    payload = np.ascontiguousarray(rows, dtype="<f4").tobytes()
    offset = int(first_row) * int(dim) * 4
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o640)
    try:
        if os.fstat(fd).st_size != offset:
            raise IndexWriteConflict("the vector file changed under this builder")
        written = os.pwrite(fd, payload, offset)
        if written != len(payload):
            raise OSError("short write to the vector file")
        os.fsync(fd)
    finally:
        os.close(fd)


def _pack(texts: Sequence[str], max_items: int, budget: int) -> List[List[int]]:
    from ..publicapi import sidecars

    weights = [min(4096, len(t.encode("utf-8", "surrogatepass")) + 2) for t in texts]
    return sidecars.pack(weights, max_items=max_items, budget=budget)


def _embed_budget() -> int:
    try:
        from ..publicapi import sidecars

        return int(sidecars.kv_budget_tokens("embed"))
    except Exception:  # noqa: BLE001 - a missing setting must not stop indexing
        return 8192


async def build_index(
    derived_dir: str,
    *,
    embed_documents: Optional[DocumentEmbedder] = None,
    max_chunks: Optional[int] = None,
    should_continue: Optional[Callable[[], Awaitable[bool]]] = None,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> IndexInfo:
    """Embed chunks.jsonl into vectors.f32 and write index.json.

    Idempotent: an existing complete index is returned untouched. Resumable:
    see the module docstring. `should_continue` is consulted between engine
    calls (the processing runner's unit boundary: lease held, blob not
    `deleting`, process not shutting down); False raises `asyncio.
    CancelledError` with every finished row kept.
    """
    existing = read_index(derived_dir)
    if existing is not None:
        return existing
    embed = embed_documents or engine_embed_documents
    cap = index_max_chunks() if max_chunks is None else max(1, int(max_chunks))

    def load() -> Tuple[List[str], int]:
        texts: List[str] = []
        with chunk_store.ChunkTable(derived_dir) as table:
            total = len(table)
            for chunk in table:
                if len(texts) >= cap:
                    break
                texts.append(chunk.text)
        return texts, total

    texts, total = await asyncio.to_thread(load)
    dim = _read_dim(derived_dir)
    done = await asyncio.to_thread(_resume_rows, derived_dir, dim)
    resumed_from = done
    # A resumed build only saw THIS run's calls: None rather than an undercount.
    tokens: Optional[int] = 0 if resumed_from == 0 else None
    pending = list(range(done, len(texts)))
    batches = [[pending[i] for i in batch] for batch in _pack([texts[i] for i in pending], CALL_MAX_INPUTS, _embed_budget())] if pending else []
    for batch in batches:
        if should_continue is not None and not await should_continue():
            raise asyncio.CancelledError()
        vectors, used = await embed([texts[i] for i in batch])
        # The embed may have waited at the gate for up to an hour: the lease
        # can be lost in that time. Ask again before writing anything.
        if should_continue is not None and not await should_continue():
            raise asyncio.CancelledError()
        matrix = normalise(np.asarray(vectors, dtype=np.float32))
        if matrix.shape[0] != len(batch):
            raise RuntimeError("the embedding engine returned the wrong number of vectors")
        if dim is None:
            dim = int(matrix.shape[1])
            await asyncio.to_thread(
                chunk_store.atomic_write_bytes, os.path.join(derived_dir, DIM_NAME), str(dim).encode("ascii")
            )
        elif int(matrix.shape[1]) != dim:
            raise RuntimeError("the embedding engine changed dimension mid-index")
        await asyncio.to_thread(_write_rows_at, derived_dir, matrix, first_row=done, dim=int(dim))
        done += len(batch)
        if used is None:
            tokens = None
        elif tokens is not None:
            tokens += int(used)
        if on_progress is not None:
            await on_progress(done, len(texts))
    if dim is None:
        dim = 0  # a document with no text: an empty, complete index
    info = IndexInfo(
        dim=int(dim),
        rows=len(texts),
        chunks_total=int(total),
        truncated=total > len(texts),
        model=MODEL_LABEL,
        embed_input_tokens=tokens,
        created_at=int(time.time()),
    )
    await asyncio.to_thread(
        chunk_store.atomic_write_bytes,
        os.path.join(derived_dir, INDEX_NAME),
        json.dumps(info.to_json()).encode("utf-8"),
    )
    return info


# ---------------------------------------------------------------- searching --


def _search_sync(derived_dir: str, query: np.ndarray, top_k: int) -> List[Hit]:
    info = read_index(derived_dir)
    if info is None:
        raise IndexNotReady("no complete index")
    if info.rows == 0 or info.dim == 0:
        return []
    if query.shape[-1] != info.dim:
        raise ValueError("query dimension does not match the index")
    matrix = np.memmap(
        os.path.join(derived_dir, VECTORS_NAME), dtype="<f4", mode="r", shape=(info.rows, info.dim)
    )
    scores = matrix @ query.reshape(-1)
    k = max(0, min(int(top_k), info.rows))
    if k == 0:
        return []
    if k < info.rows:
        picked = np.argpartition(-scores, k - 1)[:k]
    else:
        picked = np.arange(info.rows)
    # Highest score first; ties in document order, so results are stable.
    order = sorted(picked.tolist(), key=lambda row: (-float(scores[row]), row))
    return [Hit(row=int(row), score=float(scores[row])) for row in order]


async def search(derived_dir: str, qvec: Sequence[float], *, top_k: int) -> List[Hit]:
    """Top-k rows by cosine similarity, off the event loop."""
    query = normalise(np.asarray(qvec, dtype=np.float32))[0]
    return await asyncio.to_thread(_search_sync, derived_dir, query, int(top_k))


# ------------------------------------------------------ engine (production) --


async def engine_embed_documents(texts: Sequence[str]) -> Tuple[List[List[float]], Optional[int]]:
    """One packed call to the embed engine under the public `embed` gate,
    waiting as long as a background job may (`capacity.background_wait_s`).
    A refusal or engine failure raises: the processing runner defers the blob
    and resumes from the rows already written."""
    from ..publicapi import capacity, sidecars

    weight = sum(min(4096, len(t.encode("utf-8", "surrogatepass")) + 2) for t in texts)
    async with capacity.hold("embed", weight_tokens=weight, wait_s=capacity.background_wait_s()):
        return await sidecars._embed_call(list(texts))


def make_engine_query_embedder(wait_s: float) -> QueryEmbedder:
    """A query embedder for one request: the gate waited at most `wait_s`.

    None — never an exception — when the gate refuses or the engine fails:
    retrieval is an upgrade, never a gate (the `video/index.retrieve` rule),
    and the caller falls back to lexical ranking."""

    async def embed_query(question: str) -> Optional[Sequence[float]]:
        from ..publicapi import capacity, errors, sidecars

        text = query_text(question)
        try:
            async with capacity.hold(
                "embed", weight_tokens=min(4096, len(text.encode("utf-8")) + 2), wait_s=float(wait_s)
            ):
                vectors, _tokens = await sidecars._embed_call([text])
        except errors.ApiError:
            log.info("file retrieval: embed gate refused; falling back to lexical ranking")
            return None
        except Exception:  # noqa: BLE001
            log.warning("file retrieval: embed engine failed; falling back to lexical ranking", exc_info=True)
            return None
        return vectors[0] if vectors else None

    return embed_query
