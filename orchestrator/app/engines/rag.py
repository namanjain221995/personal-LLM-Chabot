"""RAG engine (spec §8, vLLM design).

Embed the query via the vLLM embeddings endpoint (OpenAI-compatible) →
LanceDB top-30 → LAZY Qwen3-Reranker-0.6B down
to top-8 (skipped entirely when RERANK_ENABLED=false) → answer with
gpt-oss-120b citing record IDs → meta.citations =
[{record_id, object, url: https://techsara.lightning.force.com/<record_id>}].

transformers/torch and lancedb are imported ONLY inside functions, so the app
and the offline test suite never load them.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, List, Optional, Sequence

from . import DIAGRAM_INSTRUCTION, NO_DATA_MESSAGE, recent_turns
from .. import llm
from ..config import settings
from ..core.citations import build_citations

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
    vectors = await llm.embed_texts([query])
    if not vectors:
        return []
    import lancedb  # lazy

    db = lancedb.connect(settings.lancedb_dir)
    table = db.open_table(settings.lancedb_table)
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


async def select_context(query: str) -> List[dict]:
    """top-30 retrieve → rerank to top-8 (or plain cut when disabled/failed)."""
    hits = await retrieve(query, settings.rag_top_k)
    if not hits:
        return []
    if settings.rerank_enabled:
        try:
            return await asyncio.to_thread(_rerank, query, hits, settings.rag_final_k)
        except Exception:
            # Reranker unavailable (e.g. fallback base image) → vector order.
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
