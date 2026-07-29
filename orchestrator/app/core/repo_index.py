"""Repo code indexing (Phase 3) — chunk source files with line numbers so the
Q&A engine can cite `path:Lstart-Lend`. Keyword retrieval matches the cheap,
dependency-free approach used elsewhere; the interface leaves room to swap in
embeddings later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from .repo import iter_source_files, read_text

CHUNK_LINES = 60
OVERLAP_LINES = 10


@dataclass
class CodeChunk:
    path: str
    start_line: int  # 1-indexed, inclusive
    end_line: int
    text: str


def chunk_file(path: str, text: str) -> List[CodeChunk]:
    """Split a file into overlapping line-windows, tracking line ranges."""
    lines = text.splitlines()
    if not lines:
        return []
    chunks: List[CodeChunk] = []
    start = 0
    n = len(lines)
    while start < n:
        end = min(n, start + CHUNK_LINES)
        body = "\n".join(lines[start:end]).strip()
        if body:
            chunks.append(
                CodeChunk(path=path, start_line=start + 1, end_line=end, text=body)
            )
        if end >= n:
            break
        start = max(end - OVERLAP_LINES, start + 1)
    return chunks


def index_repo(repo_dir: str, max_chunks: int = 6000) -> List[CodeChunk]:
    """Chunk every source file in the repo, capped at `max_chunks`."""
    out: List[CodeChunk] = []
    for rel, ap in iter_source_files(repo_dir):
        for ch in chunk_file(rel, read_text(ap)):
            out.append(ch)
            if len(out) >= max_chunks:
                return out
    return out
