"""Files as model input — chunks and the per-file vector index (2026-09-13).

Engines are stubs: `HashEmbedder` is a deterministic feature-hashing bag of
words (1,024 dims, like Qwen3-Embedding-0.6B), so ranking is real arithmetic
over real text without loading a model. The helpers here are imported by the
other file-input suites.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import zlib
from typing import Dict, List, Optional, Sequence

import numpy as np
import pytest

from app.apifiles import chunks, retrieval, vectors

# ------------------------------------------------------------------ helpers --

VOCAB = (
    "market revenue quarter growth customer supply chain logistics warehouse forecast budget "
    "margin pricing product launch region sales team hiring policy compliance audit risk vendor "
    "inventory shipment demand capacity plant energy cost report board strategy partner digital "
    "platform service support quality training safety incident review target metric pipeline "
    "operations finance legal procurement travel office lease equipment software license "
    "schedule milestone project delivery design research analysis survey feedback retention churn"
).split()

NEEDLES = {
    137: "The Ostrava facility's 2025 water usage was 48,213 cubic metres.",
    842: "Contract TS-7741 renews on 3 March 2027 with a 4.5% uplift.",
    505: "Escrow agent: Halden Trust.",
}


def prose(rng: random.Random, chars: int) -> str:
    out: List[str] = []
    size = 0
    while size < chars:
        sentence = " ".join(rng.choice(VOCAB) for _ in range(rng.randint(8, 16))).capitalize() + ". "
        out.append(sentence)
        size += len(sentence)
    return "".join(out)[:chars]


def thousand_page_rows(seed: int = 7) -> List[dict]:
    """1,000 pages of seeded prose at ~1,700 chars/page (the measured real
    report averaged 1,756), needles on pages 137 and 842, and pages 500-509
    as OCR'd image-only pages with the escrow needle on 505 — the A-3
    acceptance fixture's shape, as the extraction stage writes it."""
    rng = random.Random(seed)
    rows = []
    for page in range(1, 1001):
        text = prose(rng, 1700)
        source = "ocr" if 500 <= page <= 509 else "text"
        if page == 505:
            text = NEEDLES[505] + " " + text[:600]
        elif page in NEEDLES:
            text = text[:640] + " " + NEEDLES[page] + " " + text[640:]
        rows.append({"page": page, "text": text, "source": source})
    return rows


def write_pages(derived_dir: str, rows: Sequence[dict]) -> None:
    os.makedirs(derived_dir, exist_ok=True)
    with open(os.path.join(derived_dir, chunks.PAGES_NAME), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class HashEmbedder:
    """Deterministic stub for the embed engine. Records every call."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.document_calls: List[List[str]] = []
        self.query_calls: List[str] = []

    def vector(self, text: str) -> np.ndarray:
        if text.startswith(vectors.QUERY_INSTRUCTION):
            text = text[len(vectors.QUERY_INSTRUCTION):]
        v = np.zeros(self.dim, dtype=np.float32)
        for term in retrieval.terms(text):
            h = zlib.crc32(term.encode("utf-8"))
            v[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        return v

    async def documents(self, texts: Sequence[str]):
        self.document_calls.append(list(texts))
        return [self.vector(t).tolist() for t in texts], sum(len(t) // 3 + 1 for t in texts)

    async def query(self, question: str) -> Optional[List[float]]:
        text = vectors.query_text(question)
        self.query_calls.append(text)
        return self.vector(text).tolist()


async def indexed_document(derived_dir: str, rows: Sequence[dict], embedder: Optional[HashEmbedder] = None) -> vectors.IndexInfo:
    write_pages(derived_dir, rows)
    await asyncio.to_thread(chunks.build_text_chunks, derived_dir)
    return await vectors.build_index(derived_dir, embed_documents=(embedder or HashEmbedder()).documents)


# -------------------------------------------------------------------- chunks --


def test_a_page_shorter_than_the_chunk_size_is_one_chunk_and_no_chunk_spans_two_pages():
    pages = [chunks.Page(page=1, text="short page"), chunks.Page(page=2, text="x " * 2000), chunks.Page(page=3, text="   ")]
    out = list(chunks.chunk_pages(pages, max_chars=1500, overlap=150))
    assert [c.chunk_no for c in out] == list(range(len(out)))
    assert out[0].page_start == out[0].page_end == 1 and out[0].text == "short page"
    assert all(c.page_start == c.page_end for c in out)
    assert {c.page_start for c in out} == {1, 2}, "a whitespace-only page yields no chunk"
    assert all(len(c.text) <= 1500 for c in out)


def test_long_pages_are_cut_at_whitespace_with_bounded_overlap_and_every_character_is_covered():
    rng = random.Random(3)
    text = prose(rng, 7000)
    spans = chunks.split_page(text, max_chars=1500, overlap=150)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    covered = set()
    for (start, end), following in zip(spans, spans[1:] + [None]):
        assert end - start <= 1500
        covered.update(range(start, end))
        if following is not None:
            assert following[0] <= end, "no gap between chunks"
            assert end - following[0] <= 150, "overlap is bounded"
            assert text[end - 1] in " .\n" or end == len(text), "cut at whitespace"
            assert following[0] == 0 or text[following[0] - 1] == " ", "next chunk starts at a word"
    assert covered == set(range(len(text)))


def test_the_chunk_table_reads_any_row_by_number_and_survives_a_missing_offset_index(tmp_path):
    derived = str(tmp_path / "derived")
    write_pages(derived, [{"page": p, "text": prose(random.Random(p), 2600)} for p in range(1, 31)])
    count = chunks.build_text_chunks(derived)
    everything = list(chunks.read_chunks(derived))
    assert len(everything) == count > 30
    with chunks.ChunkTable(derived) as table:
        assert table.get(count - 1) == everything[-1]
        assert table.get(17) == everything[17]
        assert table.get(count) is None
    os.unlink(os.path.join(derived, chunks.CHUNKS_INDEX_NAME))
    with chunks.ChunkTable(derived) as table:
        assert len(table) == count and table.get(17) == everything[17]
    # A stale index (from a longer, older table) is not trusted either.
    with open(os.path.join(derived, chunks.CHUNKS_INDEX_NAME), "wb") as fh:
        fh.write(np.arange(count + 5, dtype="<u8").tobytes())
    with chunks.ChunkTable(derived) as table:
        assert table.get(3) == everything[3]


def test_media_chunks_come_from_the_video_pipeline_chunker_with_their_times():
    transcript = {"segments": [{"start": 0, "end": 20, "text": "hello there"}, {"start": 20, "end": 60, "text": "the deployment window moves"}]}
    screen = {"spans": [{"start": 30, "end": 40, "text": "CHECKPOINT BRAVO 9082", "kind": "slide"}]}
    out = chunks.chunk_media(transcript, screen)
    modalities = {c.modality for c in out}
    assert modalities == {"speech", "screen"}
    screen_chunk = next(c for c in out if c.modality == "screen")
    assert screen_chunk.start_s == 30.0 and "9082" in screen_chunk.text
    assert [c.chunk_no for c in out] == list(range(len(out)))


# -------------------------------------------------------------------- vectors --


def test_build_index_packs_engine_calls_and_stores_normalised_little_endian_rows(tmp_path):
    async def scenario():
        derived = str(tmp_path / "derived")
        embedder = HashEmbedder()
        info = await indexed_document(derived, [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 41)], embedder)
        assert info.rows == 40 and info.dim == 1024 and not info.truncated
        assert all(len(call) <= vectors.CALL_MAX_INPUTS for call in embedder.document_calls)
        budget = 8192
        for call in embedder.document_calls:
            weights = [len(t.encode()) + 2 for t in call]
            assert len(call) == 1 or sum(weights) <= budget, "a packed call fits the embed gate's budget"
        matrix = np.fromfile(os.path.join(derived, vectors.VECTORS_NAME), dtype="<f4").reshape(info.rows, info.dim)
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
        assert json.load(open(os.path.join(derived, vectors.INDEX_NAME)))["rows"] == 40
        assert not any(t.startswith("Instruct:") for call in embedder.document_calls for t in call), "documents embed bare"

    asyncio.run(scenario())


def test_search_ranks_exactly_like_brute_force_cosine(tmp_path):
    async def scenario():
        derived = str(tmp_path / "derived")
        embedder = HashEmbedder()
        info = await indexed_document(derived, [{"page": p, "text": prose(random.Random(100 + p), 1500)} for p in range(1, 61)], embedder)
        query = await embedder.query("forecast budget for the warehouse logistics team")
        hits = await vectors.search(derived, query, top_k=10)
        matrix = np.fromfile(os.path.join(derived, vectors.VECTORS_NAME), dtype="<f4").reshape(info.rows, info.dim)
        q = np.asarray(query, dtype=np.float32)
        q /= np.linalg.norm(q)
        scores = matrix @ q
        expected = sorted(range(info.rows), key=lambda r: (-float(scores[r]), r))[:10]
        assert [h.row for h in hits] == expected
        assert [round(h.score, 5) for h in hits] == [round(float(scores[r]), 5) for r in expected]
        assert len(await vectors.search(derived, query, top_k=10_000)) == info.rows

    asyncio.run(scenario())


def test_an_interrupted_index_resumes_at_the_first_row_without_a_vector(tmp_path):
    async def scenario():
        derived = str(tmp_path / "derived")
        write_pages(derived, [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 49)])
        chunks.build_text_chunks(derived)
        embedder = HashEmbedder()
        calls = {"n": 0}

        async def stop_after_two_calls() -> bool:
            calls["n"] += 1
            return calls["n"] <= 2

        with pytest.raises(asyncio.CancelledError):
            await vectors.build_index(derived, embed_documents=embedder.documents, should_continue=stop_after_two_calls)
        assert vectors.read_index(derived) is None, "no index.json until every row is written"
        embedded = sum(len(c) for c in embedder.document_calls)
        # A crash mid-write leaves a torn row; it must be cut away, not misread.
        with open(os.path.join(derived, vectors.VECTORS_NAME), "ab") as fh:
            fh.write(b"\x01\x02\x03")
        resumed = HashEmbedder()
        info = await vectors.build_index(derived, embed_documents=resumed.documents)
        assert sum(len(c) for c in resumed.document_calls) == 48 - embedded, "only the rows without a vector are embedded"
        assert info.rows == 48 and info.embed_input_tokens is None, "a resumed build does not claim a token count it did not see"
        fresh_dir = str(tmp_path / "fresh")
        fresh = await indexed_document(fresh_dir, [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 49)])
        a = np.fromfile(os.path.join(derived, vectors.VECTORS_NAME), dtype="<f4")
        b = np.fromfile(os.path.join(fresh_dir, vectors.VECTORS_NAME), dtype="<f4")
        assert fresh.rows == info.rows and np.array_equal(a, b), "a resumed index is byte-identical to an uninterrupted one"

    asyncio.run(scenario())


def test_the_chunk_cap_indexes_in_document_order_and_marks_the_index_truncated(tmp_path):
    async def scenario():
        derived = str(tmp_path / "derived")
        embedder = HashEmbedder()
        write_pages(derived, [{"page": p, "text": prose(random.Random(p), 900)} for p in range(1, 26)])
        chunks.build_text_chunks(derived)
        info = await vectors.build_index(derived, embed_documents=embedder.documents, max_chunks=10)
        assert info.rows == 10 and info.chunks_total == 25 and info.truncated
        assert [t for call in embedder.document_calls for t in call] == [c.text for c in list(chunks.read_chunks(derived))[:10]]

    asyncio.run(scenario())


def test_a_complete_index_is_returned_without_calling_the_engine_again(tmp_path):
    async def scenario():
        derived = str(tmp_path / "derived")
        await indexed_document(derived, [{"page": 1, "text": "one page"}])

        async def must_not_run(texts):
            raise AssertionError("the engine was called for a finished index")

        assert (await vectors.build_index(derived, embed_documents=must_not_run)).rows == 1

    asyncio.run(scenario())


def test_the_query_text_carries_the_document_instruction():
    text = vectors.query_text("  When does   it renew? ")
    assert text == vectors.QUERY_INSTRUCTION + "When does it renew?"
    assert "retrieve passages of the document" in vectors.QUERY_INSTRUCTION


def test_the_engine_query_embedder_answers_none_when_the_gate_refuses_or_the_engine_fails(monkeypatch):
    async def scenario():
        import contextlib

        from app.publicapi import capacity, errors, sidecars

        @contextlib.asynccontextmanager
        async def refused(*_args, **_kwargs):
            raise errors.model_at_capacity(retry_after=5)
            yield  # pragma: no cover

        monkeypatch.setattr(capacity, "hold", refused)
        assert await vectors.make_engine_query_embedder(0.01)("question") is None

        @contextlib.asynccontextmanager
        async def admitted(*_args, **_kwargs):
            yield

        async def broken(texts):
            raise RuntimeError("connection refused to http://embed:8000")

        sent: Dict[str, list] = {}

        async def fine(texts):
            sent["texts"] = list(texts)
            return [[0.5, 0.5]], 3

        monkeypatch.setattr(capacity, "hold", admitted)
        monkeypatch.setattr(sidecars, "_embed_call", broken)
        assert await vectors.make_engine_query_embedder(0.01)("question") is None
        monkeypatch.setattr(sidecars, "_embed_call", fine)
        assert await vectors.make_engine_query_embedder(0.01)("question") == [0.5, 0.5]
        assert sent["texts"] == [vectors.QUERY_INSTRUCTION + "question"]


    asyncio.run(scenario())


def test_a_builder_that_lost_its_lease_while_embedding_writes_nothing_and_an_overlapping_writer_is_detected(tmp_path):
    """2026-09-13 review: the lease is checked before each embed, but an embed
    may wait an hour at the gate. An old builder that then appended after a
    new one resumed misaligned vectors and chunk numbers under a complete
    index.json. Now: the lease is asked again after the embed, and a write
    happens only at the offset this builder expects."""
    async def scenario():
        derived = str(tmp_path / "derived")
        write_pages(derived, [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 49)])
        chunks.build_text_chunks(derived)
        embedder = HashEmbedder()
        answers = iter([True, False])

        async def lease_lost_during_the_embed() -> bool:
            return next(answers)

        with pytest.raises(asyncio.CancelledError):
            await vectors.build_index(derived, embed_documents=embedder.documents, should_continue=lease_lost_during_the_embed)
        assert len(embedder.document_calls) == 1
        assert not os.path.exists(os.path.join(derived, vectors.VECTORS_NAME)) or \
            os.path.getsize(os.path.join(derived, vectors.VECTORS_NAME)) == 0, "the embedded batch was not written"

        other = HashEmbedder()

        async def a_second_builder_writes_meanwhile(texts):
            result = await other.documents(texts)
            if len(other.document_calls) == 2:
                with open(os.path.join(derived, vectors.VECTORS_NAME), "ab") as fh:
                    fh.write(np.zeros(1024, dtype="<f4").tobytes())
            return result

        with pytest.raises(vectors.IndexWriteConflict):
            await vectors.build_index(derived, embed_documents=a_second_builder_writes_meanwhile)
        assert vectors.read_index(derived) is None, "no index.json over misaligned rows"

    asyncio.run(scenario())


def test_the_chunk_table_answers_nothing_for_a_row_whose_number_is_not_the_one_asked_for(tmp_path):
    derived = str(tmp_path / "derived")
    write_pages(derived, [{"page": p, "text": prose(random.Random(p), 900)} for p in range(1, 6)])
    count = chunks.build_text_chunks(derived)
    path = os.path.join(derived, chunks.CHUNKS_NAME)
    lines = open(path, encoding="utf-8").read().splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.unlink(os.path.join(derived, chunks.CHUNKS_INDEX_NAME))
    with chunks.ChunkTable(derived) as table:
        assert len(table) == count and table.get(0) is not None
        assert table.get(1) is None and table.get(2) is None
