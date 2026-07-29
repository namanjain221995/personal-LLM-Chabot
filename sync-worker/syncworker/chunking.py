"""Long-text chunking for the RAG index.

Chunks are ~800 tokens with a 100-token overlap between consecutive chunks.
Tokens are approximated by whitespace-separated words, which avoids pulling
a tokenizer dependency and is close enough for embedding-window sizing.
"""

from __future__ import annotations

DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100


def chunk_text(
    text: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """Split text into overlapping chunks of ~chunk_tokens tokens.

    Consecutive chunks share exactly overlap_tokens tokens (except a shorter
    final chunk, which still starts overlap_tokens before the previous chunk
    ended). Empty/whitespace-only text yields no chunks.
    """
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must satisfy 0 <= overlap < chunk_tokens")

    tokens = text.split()
    if not tokens:
        return []

    step = chunk_tokens - overlap_tokens
    chunks: list[str] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + chunk_tokens]
        chunks.append(" ".join(window))
        if start + chunk_tokens >= len(tokens):
            break
    return chunks
