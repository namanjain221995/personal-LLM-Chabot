"""Files as model input — retrieval over a request's files (design §5.4).

Stub engines only: `HashEmbedder` (tests/test_apifiles_vectors.py) and
`TermReranker` below. Nothing here reaches a production engine.
"""
from __future__ import annotations

import asyncio
import random
from typing import List, Optional, Sequence

from app.apifiles import chunks, retrieval
from tests.test_apifiles_vectors import (
    NEEDLES,
    HashEmbedder,
    indexed_document,
    prose,
    thousand_page_rows,
    write_pages,
)


class TermReranker:
    """Stub reranker: score = question terms present in the passage. `refuse`
    answers None, as the engine wrapper does when the rerank gate refuses."""

    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: List[List[str]] = []

    async def __call__(self, question: str, documents: Sequence[str]) -> Optional[List[float]]:
        self.calls.append(list(documents))
        if self.refuse:
            return None
        wanted = set(retrieval.terms(question))
        return [float(len(wanted & set(retrieval.terms(d)))) for d in documents]


def test_a_thousand_page_document_retrieves_each_needle_page_inside_the_budget(tmp_path):
    async def scenario():
        derived = str(tmp_path / "big")
        embedder = HashEmbedder()
        info = await indexed_document(derived, thousand_page_rows(), embedder)
        assert info.rows > 1000 and not info.truncated
        source = [retrieval.Source("file-big", derived, "pdf")]
        for page, question in (
            (842, "When does contract TS-7741 renew and with what uplift?"),
            (137, "What was the Ostrava facility's 2025 water usage?"),
            (505, "Who is the escrow agent?"),
        ):
            got = await retrieval.retrieve(question, source, budget_tokens=32_000, embed_query=embedder.query, rerank=TermReranker())
            assert page in got.pages("file-big"), question
            assert got.tokens <= 32_000
            assert got.meta["retrieval"] == "vector" and got.meta["rerank"] == "done"
            texts = [e.chunk.text for e in got.by_file["file-big"]]
            assert any(NEEDLES[page][:24] in t for t in texts)
            numbers = [e.chunk.chunk_no for e in got.by_file["file-big"]]
            assert numbers == sorted(numbers), "excerpts come back in reading order"
        assert len(embedder.query_calls) == 3, "one query embedding per question"

    asyncio.run(scenario())


def test_candidates_from_several_files_are_merged_and_each_answering_file_is_kept(tmp_path):
    async def scenario():
        embedder = HashEmbedder()
        sources = []
        for n in range(3):
            rng = random.Random(40 + n)
            rows = [{"page": p, "text": prose(rng, 1400)} for p in range(1, 60)]
            rows[20 + n]["text"] += f" The zeppelin hangar code for site {n} is QX-{n}{n}{n}9."
            derived = str(tmp_path / f"f{n}")
            await indexed_document(derived, rows, embedder)
            sources.append(retrieval.Source(f"file-{n}", derived, "pdf"))
        got = await retrieval.retrieve(
            "zeppelin hangar code", sources, budget_tokens=6000, embed_query=embedder.query, rerank=None
        )
        assert got.meta["retrieval_candidates"] <= retrieval.CANDIDATES_MANY_FILES
        for n in range(3):
            pages = got.pages(f"file-{n}")
            assert 21 + n in pages, f"file {n}'s answering page survived the merge"

    asyncio.run(scenario())


def test_the_reranker_orders_the_pack_and_a_refused_rerank_keeps_vector_order_and_says_so(tmp_path):
    async def scenario():
        derived = str(tmp_path / "doc")
        embedder = HashEmbedder()
        rows = [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 80)]
        rows[64]["text"] = "Renewal terms: uplift of 4.5% applies at renewal of contract TS-7741. " + rows[64]["text"]
        await indexed_document(derived, rows, embedder)
        source = [retrieval.Source("file-doc", derived, "pdf")]
        # A budget with room for about one excerpt: what the ranking puts first wins.
        reranker = TermReranker()
        ranked = await retrieval.retrieve("contract TS-7741 uplift renewal", source, budget_tokens=560,
                                          embed_query=embedder.query, rerank=reranker)
        assert ranked.meta["rerank"] == "done" and reranker.calls
        assert ranked.pages("file-doc") == [65]
        refused = await retrieval.retrieve("contract TS-7741 uplift renewal", source, budget_tokens=560,
                                           embed_query=embedder.query, rerank=TermReranker(refuse=True))
        assert refused.meta["rerank"] == "skipped"
        assert refused.by_file, "a refused rerank still answers, in vector order"

        async def exploding(question, documents):
            raise RuntimeError("rerank engine at http://reranker:8000 is down")

        broken = await retrieval.retrieve("contract TS-7741 uplift renewal", source, budget_tokens=560,
                                          embed_query=embedder.query, rerank=exploding)
        assert broken.meta["rerank"] == "skipped" and broken.by_file

    asyncio.run(scenario())


def test_a_chosen_chunk_pulls_in_its_next_neighbour_and_the_pack_never_exceeds_the_budget(tmp_path):
    async def scenario():
        derived = str(tmp_path / "doc")
        embedder = HashEmbedder()
        rows = [{"page": p, "text": prose(random.Random(p), 3000)} for p in range(1, 40)]
        rows[30]["text"] = "Table 7 zircon allocations begin here. " + rows[30]["text"]
        await indexed_document(derived, rows, embedder)
        source = [retrieval.Source("file-doc", derived, "pdf")]
        for budget in (300, 1500, 4000, 12_000):
            got = await retrieval.retrieve("zircon allocations table", source, budget_tokens=budget,
                                           embed_query=embedder.query, rerank=None)
            assert got.tokens <= budget
            assert sum(retrieval.excerpt_tokens(e.chunk) for e in got.by_file.get("file-doc", [])) == got.tokens
        got = await retrieval.retrieve("zircon allocations table", source, budget_tokens=4000,
                                       embed_query=embedder.query, rerank=None)
        excerpts = got.by_file["file-doc"]
        hit = next(e for e in excerpts if "zircon" in e.chunk.text)
        assert any(e.chunk.chunk_no == hit.chunk.chunk_no + 1 and e.reason == "neighbour" for e in excerpts)

    asyncio.run(scenario())


def test_the_documents_first_chunk_is_kept_when_it_costs_at_most_an_eighth_of_the_budget(tmp_path):
    async def scenario():
        derived = str(tmp_path / "doc")
        embedder = HashEmbedder()
        rows = [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 50)]
        rows[0]["text"] = "Annual Report of the Northwind Cooperative. " + rows[0]["text"]
        rows[40]["text"] += " Quokka sightings rose sharply."
        await indexed_document(derived, rows, embedder)
        source = [retrieval.Source("file-doc", derived, "pdf")]
        roomy = await retrieval.retrieve("quokka sightings", source, budget_tokens=8000, embed_query=embedder.query, rerank=None)
        assert 1 in roomy.pages("file-doc") and any(e.reason == "first" for e in roomy.by_file["file-doc"])
        tight = await retrieval.retrieve("quokka sightings", source, budget_tokens=1000, embed_query=embedder.query, rerank=None)
        assert not any(e.reason == "first" for e in tight.by_file["file-doc"])

    asyncio.run(scenario())


def test_summary_mode_samples_evenly_across_the_whole_document_without_ranking(tmp_path):
    async def scenario():
        derived = str(tmp_path / "doc")
        write_pages(derived, [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 401)])
        chunks.build_text_chunks(derived)

        async def must_not_embed(question):
            raise AssertionError("summary mode ranks nothing")

        got = await retrieval.retrieve(
            "Describe the attached files.", [retrieval.Source("file-doc", derived, "pdf")],
            budget_tokens=8000, embed_query=must_not_embed, rerank=None, summary=True,
        )
        pages = got.pages("file-doc")
        assert got.tokens <= 8000 and len(pages) >= 10
        assert pages[0] <= 40 and pages[-1] >= 360, "the sample reaches both ends of the document"
        gaps = [b - a for a, b in zip(pages, pages[1:])]
        assert max(gaps) <= 2 * (sum(gaps) / len(gaps)) + 1, "evenly spaced, not clustered"

    asyncio.run(scenario())


def test_without_a_query_embedding_ranking_falls_back_to_lexical_and_still_finds_the_needle(tmp_path):
    async def scenario():
        derived = str(tmp_path / "big")
        await indexed_document(derived, thousand_page_rows(), HashEmbedder())

        async def gate_refused(question):
            return None

        got = await retrieval.retrieve(
            "When does contract TS-7741 renew?", [retrieval.Source("file-big", derived, "pdf")],
            budget_tokens=16_000, embed_query=gate_refused, rerank=None,
        )
        assert got.meta["retrieval"] == "lexical"
        assert 842 in got.pages("file-big")
        # A file whose index was never built is ranked lexically beside indexed ones.
        bare = str(tmp_path / "bare")
        rows = [{"page": p, "text": prose(random.Random(900 + p), 1400)} for p in range(1, 30)]
        rows[12]["text"] += " Contract TS-9001 renews in April."
        write_pages(bare, rows)
        chunks.build_text_chunks(bare)
        embedder = HashEmbedder()
        mixed = await retrieval.retrieve(
            "contract TS-9001 renews", [retrieval.Source("file-big", derived, "pdf"), retrieval.Source("file-bare", bare, "pdf")],
            budget_tokens=16_000, embed_query=embedder.query, rerank=None,
        )
        assert mixed.meta["retrieval"] == "vector+lexical"
        assert 13 in mixed.pages("file-bare")

    asyncio.run(scenario())


def test_an_empty_budget_or_no_sources_retrieves_nothing_without_touching_an_engine():
    async def scenario():
        async def must_not_run(*_args):
            raise AssertionError("no engine call expected")

        empty = await retrieval.retrieve("q", [], budget_tokens=1000, embed_query=must_not_run, rerank=must_not_run)
        assert empty.by_file == {} and empty.tokens == 0
        zero = await retrieval.retrieve("q", [retrieval.Source("f", "/nonexistent", "pdf")], budget_tokens=0,
                                        embed_query=must_not_run, rerank=must_not_run)
        assert zero.by_file == {}

    asyncio.run(scenario())


def test_a_long_question_reaches_the_embed_and_rerank_engines_as_its_last_two_thousand_characters(tmp_path):
    async def scenario():
        derived = str(tmp_path / "doc")
        rows = [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 20)]
        rows[6]["text"] += " Contract TS-7741 renews on 3 March 2027."
        embedder = HashEmbedder()
        await indexed_document(derived, rows, embedder)
        reranker = TermReranker()
        seen: List[str] = []

        async def rerank(question, documents):
            seen.append(question)
            return await reranker(question, documents)

        question = "background " * 6000 + "When does contract TS-7741 renew?"
        got = await retrieval.retrieve(question, [retrieval.Source("f", derived, "pdf")], budget_tokens=4000,
                                       embed_query=embedder.query, rerank=rerank)
        from app.apifiles import vectors

        sent = embedder.query_calls[0][len(vectors.QUERY_INSTRUCTION):]
        assert len(sent) <= retrieval.QUERY_MAX_CHARS and sent.endswith("When does contract TS-7741 renew?")
        assert seen and len(seen[0]) <= retrieval.QUERY_MAX_CHARS
        assert got.meta["retrieval"] == "vector" and 7 in got.pages("f")

    asyncio.run(scenario())


def test_the_lexical_fallback_reads_a_bounded_number_of_chunks_per_file(tmp_path):
    derived = str(tmp_path / "doc")
    rows = [{"page": p, "text": prose(random.Random(p), 1400)} for p in range(1, 40)]
    rows[35]["text"] += " Contract TS-9001 renews in April."
    write_pages(derived, rows)
    chunks.build_text_chunks(derived)
    source = [retrieval.Source("f", derived, "pdf")]
    assert any(n >= 35 for _s, _f, n in retrieval._lexical_rank("contract TS-9001", source, 5))
    capped = retrieval._lexical_rank("contract TS-9001", source, 5, max_chunks=10)
    assert capped == [] or all(n < 10 for _s, _f, n in capped)
