"""Reorder retrieved candidates with a cross-encoder.

Retrieval is cheap and approximate: the lexicon matches words, the embeddings
match a compressed summary of meaning. Neither reads the question and the card
together. A cross-encoder does exactly that, one pair at a time, which is why
it ranks better and why it is only ever used on a shortlist -- scoring all
4,484 cards this way would cost minutes.

One rule the reranker is not allowed to break: a curated lexicon entry and a
choice the user already made are human decisions, and a model does not get to
overturn them. Those stay pinned; everything below them is reordered.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

DEFAULT_ENDPOINT = "http://127.0.0.1:8005/v1"
DEFAULT_MODEL = "Qwen/Qwen3-Reranker-0.6B"

# Only an answer the user gave in THIS conversation is pinned. A curated entry
# is authoritative about what a TERM means -- "mock interview" is
# Internal_Interview__c -- and that mapping is preserved in the resolution. It
# is not a claim that the question is ABOUT that component: "who can conduct
# mock interviews" is answered by Recruiter__c.Active_For_Mock_Interview__c,
# which the cross-encoder scores 0.99 and the object 0.14. Pinning the object
# there buried the answer.
PINNED_TIERS = {"user_choice"}

# A curated component still must not vanish, so its reranked score cannot fall
# below this. It can be outranked; it cannot be dropped.
CURATED_FLOOR = 0.55

# Scoring the whole retrieval pool costs latency for candidates that were never
# going to win; this many is enough for the top slots to settle.
DEFAULT_TOP_N = 25


class RerankError(RuntimeError):
    """Raised when the rerank endpoint is unreachable or answers unexpectedly."""


@dataclass
class Scored:
    index: int
    score: float


def rerank(query: str, documents: Sequence[str], *,
           endpoint: str = DEFAULT_ENDPOINT,
           model: str = DEFAULT_MODEL,
           timeout: float = 30.0) -> list[Scored]:
    """Relevance of each document to the query, highest first."""
    if not documents:
        return []
    try:
        import httpx
    except ImportError as exc:
        raise RerankError("reranking needs httpx: python -m pip install httpx") from exc

    with httpx.Client() as client:
        response = client.post(
            f"{endpoint.rstrip('/')}/rerank",
            json={"model": model, "query": query, "documents": list(documents)},
            timeout=timeout)
    if response.status_code != 200:
        raise RerankError(
            f"{endpoint}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
        scored = [Scored(index=item["index"], score=float(item["relevance_score"]))
                  for item in payload["results"]]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RerankError(f"{endpoint}: unexpected response shape: {exc}") from exc
    scored.sort(key=lambda s: -s.score)
    return scored
