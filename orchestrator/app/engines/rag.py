"""RAG engine (spec §8, vLLM design).

Embed the query via the vLLM embeddings endpoint (OpenAI-compatible) →
LanceDB top-30 → in-process or remote Qwen3 reranker down
to top-8 (or vector order when disabled/unavailable) → answer with
gpt-oss-120b citing record IDs → meta.citations =
[{record_id, object, url: https://techsara.lightning.force.com/<record_id>}].

transformers/torch and lancedb are imported ONLY inside functions; remote and
disabled reranking never import the in-process model framework.
"""
from __future__ import annotations

import asyncio
import math
import re
from typing import Awaitable, Callable, List, Optional, Sequence

import httpx

from . import DIAGRAM_INSTRUCTION, NO_DATA_MESSAGE, recent_turns
from .. import llm
from ..config import settings
from ..core.citations import build_citations
from ..embedding_index import open_compatible_table, validate_query_dimension
from ..model_capabilities import RerankerBackend

Emit = Callable[[str, dict], Awaitable[None]]

_RERANKER = None  # (tokenizer, model, torch) singleton, loaded lazily

_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements "
    'based on the Query and the Instruct provided. Note that the answer can '
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_INSTRUCT = "Given a question about Salesforce data, judge whether the record helps answer it."


async def retrieve(query: str, top_k: Optional[int] = None) -> List[dict]:
    """Embed via EMBED_BASE_URL and search LanceDB. Returns hit dicts (top-30)."""
    import lancedb  # lazy

    db = lancedb.connect(settings.lancedb_dir)
    table, metadata = open_compatible_table(
        db,
        settings.lancedb_dir,
        settings.lancedb_table,
        settings.embed_model,
    )
    vectors = await llm.embed_texts([query])
    if not vectors:
        return []
    validate_query_dimension(metadata, vectors[0], settings.lancedb_dir)
    return (
        table.search(vectors[0])
        .limit(top_k or settings.rag_top_k)
        .to_list()
    )


def _load_reranker():
    global _RERANKER
    if _RERANKER is None:
        # LAZY heavy imports — needs torch (base image); never triggered when
        # RERANK_ENABLED=false or in the offline test suite.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(settings.rerank_model, padding_side="left")
        model = AutoModelForCausalLM.from_pretrained(settings.rerank_model, torch_dtype="auto")
        if torch.cuda.is_available():
            model = model.cuda()
        model.eval()
        _RERANKER = (tokenizer, model, torch)
    return _RERANKER


def _rerank(query: str, hits: List[dict], top_n: int) -> List[dict]:
    """Score hits with Qwen3-Reranker-0.6B (yes/no logit) and keep top_n."""
    tokenizer, model, torch = _load_reranker()
    yes_id = tokenizer.convert_tokens_to_ids("yes")
    no_id = tokenizer.convert_tokens_to_ids("no")
    scores: List[float] = []
    with torch.no_grad():
        for hit in hits:
            doc = str(hit.get("text", ""))[:4000]
            prompt = (
                f"{_PREFIX}<Instruct>: {_INSTRUCT}\n<Query>: {query}\n"
                f"<Document>: {doc}{_SUFFIX}"
            )
            inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=8192)
            inputs = {k: v.to(model.device) for k, v in inputs.items()}
            logits = model(**inputs).logits[0, -1, :]
            pair = torch.stack([logits[no_id], logits[yes_id]])
            scores.append(torch.softmax(pair, dim=0)[1].item())
    order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
    return [hits[i] for i in order[:top_n]]


def reranker_score_url(base_url: str) -> str:
    """Return the vLLM-compatible root ``/score`` endpoint."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return f"{root}/score"


def _rank_remote_response(hits: List[dict], payload: object, top_n: int) -> List[dict]:
    """Validate a remote score response and apply it without losing hit order.

    vLLM returns ``data: [{index, score}, ...]``.  Complete, unique indices are
    required so a malformed response can never associate one record's score
    with another record. Python's stable sort preserves vector order for ties.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("reranker /score response has no data list")
    data = payload["data"]
    if len(data) != len(hits):
        raise ValueError("reranker /score returned the wrong number of scores")

    scores: dict[int, float] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("reranker /score data item is not an object")
        try:
            index = int(item["index"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("reranker /score item needs numeric index and score") from exc
        if index < 0 or index >= len(hits) or index in scores or not math.isfinite(score):
            raise ValueError("reranker /score returned invalid indices or scores")
        scores[index] = score

    if set(scores) != set(range(len(hits))):
        raise ValueError("reranker /score response indices are incomplete")
    order = sorted(range(len(hits)), key=lambda index: scores[index], reverse=True)
    return [hits[index] for index in order[:top_n]]


async def _remote_rerank(query: str, hits: List[dict], top_n: int) -> List[dict]:
    """Score all query/document pairs through a remote vLLM ``/score`` API."""
    if not settings.rerank_base_url:
        raise ValueError("RERANK_BASE_URL is required for the remote reranker")
    if (
        settings.reranker_capabilities.requires_authentication
        and not settings.rerank_api_key
    ):
        raise ValueError("RERANK_API_KEY is required by the configured reranker")
    headers = {}
    if settings.rerank_api_key:
        headers["Authorization"] = f"Bearer {settings.rerank_api_key}"
    body = {
        "model": settings.rerank_model,
        # vLLM supports the 1→N form and returns one indexed score per document.
        "text_1": query,
        "text_2": [str(hit.get("text", ""))[:4000] for hit in hits],
    }
    async with httpx.AsyncClient(timeout=settings.rerank_timeout) as client:
        response = await client.post(
            reranker_score_url(settings.rerank_base_url),
            json=body,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    return _rank_remote_response(hits, payload, top_n)


async def select_context(query: str) -> List[dict]:
    """top-30 retrieve → rerank to top-8 (or plain cut when disabled/failed)."""
    hits = await retrieve(query, settings.rag_top_k)
    if not hits:
        return []
    try:
        backend = RerankerBackend.parse(settings.rerank_backend)
    except ValueError:
        # Settings validates this at startup; retain the historical safe
        # retrieval fallback if a test/runtime mutates it later.
        backend = RerankerBackend.DISABLED
    capabilities = settings.reranker_capabilities
    if (
        settings.rerank_enabled
        and capabilities.enabled
        and capabilities.supports_reranking
        and backend is not RerankerBackend.DISABLED
    ):
        try:
            if backend is RerankerBackend.REMOTE:
                return await _remote_rerank(query, hits, settings.rag_final_k)
            return await asyncio.to_thread(_rerank, query, hits, settings.rag_final_k)
        except Exception:
            # Reranker unavailable/malformed → preserve vector-search order.
            return hits[: settings.rag_final_k]
    return hits[: settings.rag_final_k]


def _context_block(hits: Sequence[dict]) -> str:
    lines = []
    for hit in hits:
        rid = hit.get("record_id", "unknown")
        obj = hit.get("object", "Record")
        text = str(hit.get("text", "")).strip()
        lines.append(f"[{rid}] ({obj}) {text}")
    return "\n\n".join(lines)


def _answer_messages(message: str, hits: Sequence[dict], history: Sequence[dict]) -> List[dict]:
    system = (
        "You answer questions about Salesforce data using ONLY the provided "
        "records. Cite the record IDs you rely on in square brackets, e.g. "
        "[001xx000003DGbY]. If the records do not contain the answer, say so."
    )
    # Brain knowledge: process questions ("how does the recurring plan
    # work?") route here, and no CRM record contains that answer — the
    # Salesforce team's documentation packs do (core/brain.py).
    from ..core import brain

    notes = brain.knowledge_for(message)
    if notes:
        system += (
            "\n\nInternal documentation is provided below. Use it to interpret "
            "the records and to answer questions about how this org's "
            "processes work, even when no record contains the answer. Never "
            "invent a record citation for something the documentation said.\n\n"
            + notes
        )
    user = f"Records:\n{_context_block(hits)}\n\nQuestion: {message}"
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION}] + recent_turns(history, 6) + [
        {"role": "user", "content": user}
    ]


async def run_rag_engine(message: str, history: Sequence[dict], emit: Emit) -> str:
    try:
        hits = await select_context(message)
    except Exception as exc:
        # First-run state: LanceDB dir/table absent until the first sync.
        if re.search(r"not found|no such|does not exist", str(exc), re.I):
            await emit("token", {"text": NO_DATA_MESSAGE})
            await emit("meta", {"route": "rag"})
            return NO_DATA_MESSAGE
        raise

    parts: List[str] = []
    async for token in llm.stream_chat_completion(
        _answer_messages(message, hits, history), temperature=0.2, max_tokens=5000
    ):
        parts.append(token)
        await emit("token", {"text": token})
    answer = "".join(parts)

    citations = build_citations(hits, base_url=settings.sf_lightning_base_url)
    # Prefer the records actually cited in the answer; fall back to all used.
    mentioned = [c for c in citations if c["record_id"] and re.search(re.escape(c["record_id"]), answer)]
    # §10: the single final meta carries the `route` key.
    await emit("meta", {"route": "rag", "citations": mentioned or citations})
    return answer
