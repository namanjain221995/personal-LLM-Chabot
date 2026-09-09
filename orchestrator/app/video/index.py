"""The evidence index: one LanceDB table, its own directory, keyed by analysis.

WHY ITS OWN DIRECTORY. `embedding_index` pins exactly one table per
directory in its sidecar, and both existing directories are taken: the
Salesforce corpus (written by the sync-worker; every hit there renders as a
CRM citation) and the public web corpus (searched for EVERY user on EVERY
turn with no identity filter). A video's transcript in either would be
either mislabelled or public. `LANCEDB_VIDEO_DIR` is neither.

WHY KEYED BY ANALYSIS, NOT CONVERSATION. Analyses are content-addressed:
the same file attached twice, by anyone, is one analysis. Rows carry the
`analysis_id`; WHICH conversations may see an analysis is the
`video_attachments` table in PostgreSQL, and every query here takes an
explicit list of analysis ids that the caller resolved from it. Nothing in
this table can be reached without first passing that join, so two videos in
one session never bleed and one user's video never appears in another's
chat.

MODALITIES. `speech` chunks are groups of transcript segments (~45 s);
`screen` chunks are OCR spans (what was written); `visual` chunks are frame
captions (what was shown). A question about "the code on the slide" ranks
screen chunks; "what did she say about pricing" ranks speech; the reranker
decides with the question in hand.

Writes take `web_index.write_lock` on THIS directory: Lance has no commit
lock on a plain filesystem and a concurrent writer is a corrupt table.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional, Sequence

from ..config import settings
from .artifacts import fmt_ts
from .types import OcrSpan, Segment

log = logging.getLogger(__name__)

TABLE = "video_chunks"
CHUNKER_VERSION = 1

#: L2 distance above which a hit is noise. Transcript chunks are shorter and
#: more uniform than web pages; measured on the first live videos the good
#: hits sat at 0.5-0.9 for a same-language question, 1.29 for an English
#: question over a Hindi transcript, and unrelated chunks at 1.6+. Loose on
#: purpose — the reranker does the fine ordering, and a cross-lingual hit
#: dropped here is an answer the person never sees.
MAX_DISTANCE = 1.5

_SPEECH_CHUNK_S = 45.0
_SPEECH_CHUNK_CHARS = 700
_SCREEN_CHUNK_CHARS = 1500
_EMBED_BATCH = 64

_write_lock = asyncio.Lock()


class VideoIndexUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------- chunking --


def _speech_chunks(segments: Sequence[Segment]) -> List[dict]:
    out: List[dict] = []
    cur: List[Segment] = []
    for s in segments:
        if not s.text.strip():
            continue
        if cur and (
            s.end_s - cur[0].start_s > _SPEECH_CHUNK_S
            or sum(len(c.text) + 1 for c in cur) + len(s.text) > _SPEECH_CHUNK_CHARS
        ):
            out.append(_speech_row(cur))
            cur = []
        cur.append(s)
    if cur:
        out.append(_speech_row(cur))
    return out


def _speech_row(segs: List[Segment]) -> dict:
    return {
        "modality": "speech",
        "start_s": float(segs[0].start_s),
        "end_s": float(segs[-1].end_s),
        "text": " ".join(s.text.strip() for s in segs),
    }


def _screen_chunks(spans: Sequence[OcrSpan]) -> List[dict]:
    out: List[dict] = []
    for span in spans:
        text = span.text.strip()
        if text:
            body = text if len(text) <= _SCREEN_CHUNK_CHARS else text[:_SCREEN_CHUNK_CHARS]
            out.append({
                "modality": "screen",
                "start_s": float(span.start_s),
                "end_s": float(span.end_s),
                "text": (f"[{span.kind}] " if span.kind else "") + body,
            })
        if span.caption:
            out.append({
                "modality": "visual",
                "start_s": float(span.start_s),
                "end_s": float(span.end_s),
                "text": (f"[{span.kind}] " if span.kind else "") + span.caption.strip(),
            })
    return out


def build_chunks(segments: Sequence[Segment], spans: Sequence[OcrSpan]) -> List[dict]:
    chunks = _speech_chunks(segments) + _screen_chunks(spans)
    chunks.sort(key=lambda c: (c["start_s"], c["modality"]))
    for i, c in enumerate(chunks):
        c["chunk_ix"] = i
    return chunks


# ---------------------------------------------------------------- storage --


def video_dir() -> str:
    directory = settings.lancedb_video_dir
    from ..web_index import _within  # the same overlap guard the web index uses

    for other, label in ((settings.lancedb_dir, "the Salesforce corpus"), (settings.lancedb_web_dir, "the web index")):
        if other and (_within(directory, other) or _within(other, directory)):
            raise VideoIndexUnavailable(
                f"LANCEDB_VIDEO_DIR {directory!r} overlaps {label} at {other!r}; each index needs its own directory"
            )
    return directory


def _write_sidecar(directory: str, dimension: int) -> None:
    from ..embedding_index import metadata_path
    from ..web_index import _atomic_write_json

    path = metadata_path(directory)
    if os.path.exists(path):
        return
    _atomic_write_json(
        path,
        {
            "table": TABLE,
            "model_id": settings.embed_model,
            "dimension": int(dimension),
            "schema_version": 1,
            "chunker_version": CHUNKER_VERSION,
            "query_instruction": "qwen3-web-v1",
        },
    )


def _schema(dim: int):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("analysis_id", pa.int64()),
            pa.field("chunk_ix", pa.int64()),
            pa.field("modality", pa.string()),
            pa.field("start_s", pa.float32()),
            pa.field("end_s", pa.float32()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )


def _open(create_dim: Optional[int] = None):
    import lancedb  # lazy

    from ..embedding_index import open_compatible_table

    directory = video_dir()
    os.makedirs(directory, exist_ok=True)
    conn = lancedb.connect(directory)
    if TABLE not in conn.table_names():
        if create_dim is None:
            return conn, None
        _write_sidecar(directory, int(create_dim))
        table = conn.create_table(TABLE, schema=_schema(int(create_dim)))
        return conn, table
    table, _meta = open_compatible_table(conn, directory, TABLE, settings.embed_model)
    return conn, table


async def _embed(texts: List[str]) -> List[List[float]]:
    from .. import llm

    out: List[List[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        out.extend(await llm.embed_texts(texts[start : start + _EMBED_BATCH], kind="index"))
        await asyncio.sleep(0)
    return out


async def index_analysis(analysis_id: int, chunks: Sequence[dict]) -> int:
    """Replace this analysis's rows with `chunks`. Returns rows written."""
    if not chunks:
        await delete_analysis(analysis_id)
        return 0
    vectors = await _embed([c["text"] for c in chunks])
    rows = [
        {
            "analysis_id": int(analysis_id),
            "chunk_ix": int(c["chunk_ix"]),
            "modality": str(c["modality"]),
            "start_s": float(c["start_s"]),
            "end_s": float(c["end_s"]),
            "text": str(c["text"]),
            "vector": [float(v) for v in vec],
        }
        for c, vec in zip(chunks, vectors)
    ]

    def _write() -> None:
        from ..web_index import write_lock

        with write_lock(video_dir(), wait_s=20.0):
            _conn, table = _open(create_dim=len(rows[0]["vector"]))
            assert table is not None
            table.delete(f"analysis_id = {int(analysis_id)}")
            table.add(rows)  # the second sanctioned LanceDB writer; see tests/test_exclusion_invariants

    async with _write_lock:
        await asyncio.to_thread(_write)
    return len(rows)


async def delete_analysis(analysis_id: int) -> None:
    def _delete() -> None:
        from ..web_index import write_lock

        try:
            with write_lock(video_dir(), wait_s=20.0):
                _conn, table = _open()
                if table is not None:
                    table.delete(f"analysis_id = {int(analysis_id)}")
        except FileNotFoundError:
            pass

    async with _write_lock:
        await asyncio.to_thread(_delete)


async def optimize() -> None:
    def _opt() -> None:
        from datetime import timedelta

        from ..web_index import write_lock

        with write_lock(video_dir(), wait_s=10.0):
            _conn, table = _open()
            if table is not None:
                table.optimize(cleanup_older_than=timedelta(hours=1))

    async with _write_lock:
        await asyncio.to_thread(_opt)


# ---------------------------------------------------------------- queries --

_RERANK_INSTRUCTION = (
    "Given a question about a video, retrieve the transcript passages, on-screen "
    "text or frame descriptions that answer it"
)


async def retrieve(
    question: str,
    analysis_ids: Sequence[int],
    *,
    top_k: int,
    modality: Optional[str] = None,
) -> List[dict]:
    """Best evidence for `question` inside these analyses, reranked.

    Returns [] on any failure: retrieval is an upgrade to the summary and
    chapters that always ride along, never a gate.
    """
    ids = [int(i) for i in analysis_ids if i]
    if not ids or not question.strip():
        return []
    from .. import llm, rerank

    try:
        vector = await llm.embed_query(question, instruction=llm.QUERY_INSTRUCTION)
    except llm.EmbedUnavailable:
        return []
    except Exception:  # noqa: BLE001
        log.warning("video retrieve: embedding failed", exc_info=True)
        return []

    def _search() -> List[dict]:
        _conn, table = _open()
        if table is None:
            return []
        query = table.search(vector).limit(max(top_k * 4, 24))
        query = query.where("analysis_id IN (" + ", ".join(str(i) for i in ids) + ")")
        if modality in ("speech", "screen", "visual"):
            query = query.where(f"modality = '{modality}'")
        return query.to_list()

    try:
        hits = await asyncio.to_thread(_search)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        log.warning("video retrieve: search failed", exc_info=True)
        return []
    candidates = [
        {
            "analysis_id": int(h["analysis_id"]),
            "modality": str(h["modality"]),
            "start_s": float(h["start_s"]),
            "end_s": float(h["end_s"]),
            "text": str(h["text"]),
            "distance": float(h.get("_distance", 0.0)),
        }
        for h in hits
        if float(h.get("_distance", 0.0)) <= MAX_DISTANCE
    ]
    if not candidates:
        return []
    ordered = await rerank.order(question, candidates, text_key="text", top_n=top_k, instruction=_RERANK_INSTRUCTION, kind=rerank.BULK)
    return list(ordered[:top_k])


async def best_distance(question: str, analysis_ids: Sequence[int]) -> Optional[float]:
    """How close the closest chunk is — the cheap 'is this about the video?' test."""
    ids = [int(i) for i in analysis_ids if i]
    if not ids or not question.strip():
        return None
    from .. import llm

    try:
        vector = await llm.embed_query(question, instruction=llm.QUERY_INSTRUCTION)
    except Exception:  # noqa: BLE001
        return None

    def _search() -> Optional[float]:
        _conn, table = _open()
        if table is None:
            return None
        rows = table.search(vector).limit(1).where("analysis_id IN (" + ", ".join(str(i) for i in ids) + ")").to_list()
        return float(rows[0]["_distance"]) if rows else None

    try:
        return await asyncio.to_thread(_search)
    except Exception:  # noqa: BLE001
        return None


def format_hits(hits: Sequence[dict], names: Dict[int, str]) -> str:
    """Evidence block for a prompt, one hit per line, timestamps first."""
    lines = []
    for h in hits:
        who = names.get(int(h["analysis_id"]), "")
        label = {"speech": "SPEECH", "screen": "SCREEN", "visual": "SCREEN"}.get(h["modality"], h["modality"].upper())
        prefix = f"[{fmt_ts(h['start_s'])}-{fmt_ts(h['end_s'])}]"
        if who:
            prefix += f" ({who})"
        lines.append(f"{prefix} {label}: {h['text']}")
    return "\n".join(lines)
