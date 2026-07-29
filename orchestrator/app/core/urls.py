"""URL detection + relevance chunking (Phase 2, reused by Phase 3).

extract_urls finds http(s) links a user pasted. chunk_text / select_relevant
keep a large page from blowing the context budget: split into overlapping
chunks and keep the ones most relevant to the question (keyword overlap — the
same cheap, dependency-free approach as cross-chat recall).
"""
from __future__ import annotations

import re
from typing import List

from ..memory_recall import keywords

# Stops before common trailing punctuation so "(see https://x.com)." works.
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_STRIP_TRAILING = ".,;:!?)]}\"'"


def extract_urls(text: str, limit: int = 5) -> List[str]:
    """Distinct http(s) URLs in `text`, order-preserving, capped at `limit`."""
    out: List[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_STRIP_TRAILING)
        if url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def chunk_text(text: str, chunk_chars: int = 1600, overlap: int = 200) -> List[str]:
    """Split text into overlapping character chunks on whitespace boundaries."""
    text = text.strip()
    if len(text) <= chunk_chars:
        return [text] if text else []
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            sp = text.rfind(" ", start + int(chunk_chars * 0.6), end)
            if sp != -1:
                end = sp
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def select_relevant(text: str, query: str, max_chars: int) -> str:
    """Return up to `max_chars` of `text` most relevant to `query`.

    Small texts pass through. Larger ones are chunked and scored by keyword
    overlap with the query; the top chunks (in original order) are joined until
    the budget is filled — so the model sees the pertinent parts of a long page.
    """
    if len(text) <= max_chars:
        return text
    kws = set(keywords(query, max_keywords=12))
    if not kws:
        return text[:max_chars]
    # Chunk smaller than the budget so the relevant chunk is kept whole rather
    # than sliced through the middle.
    chunk_size = min(1600, max(300, max_chars // 2))
    chunks = chunk_text(text, chunk_chars=chunk_size, overlap=min(150, chunk_size // 4))

    scored = []
    for i, c in enumerate(chunks):
        low = c.lower()
        score = sum(low.count(k) for k in kws)
        scored.append((score, i, c))
    # keep the highest-scoring chunks, then restore reading order
    scored.sort(key=lambda t: t[0], reverse=True)
    picked: List[tuple] = []
    total = 0
    for score, i, c in scored:
        if total + len(c) > max_chars and picked:
            break
        picked.append((i, c))
        total += len(c)
    picked.sort(key=lambda t: t[0])
    joined = "\n…\n".join(c for _i, c in picked)
    return joined[:max_chars]
