"""GitHub repository engine (Phase 3).

Pasting a public repo URL clones it (shallow, capped, hooks off, code NEVER
run), indexes the source into per-conversation code chunks, and streams an
auto-overview. Follow-up questions search those chunks and answer with
`path:Lstart-Lend` citations that the UI can expand into snippets.
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, Sequence

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import db, llm
from ..config import settings
from ..core import repo as repolib
from ..core.repo import GithubRef, RepoError
from ..core.repo_index import chunk_file, index_repo
from ..memory_recall import keywords

Emit = Callable[[str, dict], Awaitable[None]]

_MAX_CONTEXT_CHARS = 60000


# --------------------------------------------------------------------------
# clone + index + overview
# --------------------------------------------------------------------------
async def _clone_and_index(
    ref: GithubRef, conversation_id: str, emit: Emit
) -> Optional[repolib.RepoOverview]:
    await emit("status", {"text": f"Cloning {ref.key}…"})
    repolib.enforce_quota_and_ttl()
    dest = repolib.workspace_path(conversation_id, ref)
    try:
        sha = repolib.shallow_clone(ref, dest)
    except RepoError as exc:
        await emit("status", {"text": str(exc)})
        return None

    await emit("status", {"text": "Indexing the code…"})
    overview = repolib.build_overview(dest)
    chunks = index_repo(dest)
    await db.run_in_thread(
        db.save_repo, conversation_id, ref.key, ref.clone_url, sha
    )
    await db.run_in_thread(
        db.replace_repo_chunks,
        conversation_id,
        ref.key,
        [
            {"path": c.path, "start_line": c.start_line, "end_line": c.end_line, "text": c.text}
            for c in chunks
        ],
    )
    return overview


def _overview_messages(ref: GithubRef, ov: repolib.RepoOverview) -> List[dict]:
    langs = ", ".join(f"{lang} ({n})" for lang, n in ov.languages[:8]) or "unknown"
    system = (
        "You are a senior engineer giving a concise onboarding overview of a "
        "code repository. Use the facts below. Cover: what the project is (from "
        "the README), the main languages, likely entry points, key config files, "
        "and how the code is organized. Be brief and structured (headings + "
        "bullets). Do not invent files that aren't listed."
    )
    facts = (
        f"Repository: {ref.key}\n"
        f"Files indexed: {ov.file_count}\n"
        f"Languages: {langs}\n"
        f"Entry points: {', '.join(ov.entry_points) or 'none obvious'}\n"
        f"Key configs: {', '.join(ov.key_configs) or 'none'}\n\n"
        f"File tree (partial):\n{ov.tree[:6000]}\n\n"
        f"README (excerpt):\n{ov.readme[:6000]}"
    )
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION},
            {"role": "user", "content": facts}]


# --------------------------------------------------------------------------
# code Q&A
# --------------------------------------------------------------------------
def _qa_context(chunks: List[dict]) -> str:
    parts = []
    total = 0
    for c in chunks:
        block = f"[{c['path']}:L{c['start_line']}-L{c['end_line']}]\n{c['text']}"
        if total + len(block) > _MAX_CONTEXT_CHARS and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def _qa_messages(question: str, chunks: List[dict], history: Sequence[dict]) -> List[dict]:
    system = (
        "You answer questions about a code repository using ONLY the code "
        "excerpts provided. Each excerpt is labelled with its file and line "
        "range like [path/to/file.py:L120-L160]. Cite the excerpts you rely on "
        "using that exact bracket form. If the excerpts don't answer the "
        "question, say what you'd need to look at."
    )
    user = f"Code excerpts:\n{_qa_context(chunks)}\n\nQuestion: {question}"
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION}, *recent_turns(history, 4),
            {"role": "user", "content": user}]


def _expand_for_code(kws: List[str]) -> List[str]:
    """Add short stems so a query word matches its code form: 'authentication'
    → also 'auth' (matches auth.py / AuthBase / HTTPBasicAuth). Pure keyword
    search otherwise misses code whose identifiers use a different suffix."""
    out = list(kws)
    for k in kws:
        if len(k) > 4:
            stem = k[:4]
            if stem not in out:
                out.append(stem)
    return out


async def _code_qa(
    message: str, conversation_id: str, history: Sequence[dict], emit: Emit
) -> str:
    kws = _expand_for_code(keywords(message, max_keywords=12))
    chunks = await db.run_in_thread(
        db.search_repo_chunks, conversation_id, kws, settings.repo_final_chunks
    )
    if not chunks:
        # fall back to a plain answer over the repo overview context
        chunks = await db.run_in_thread(
            db.search_repo_chunks, conversation_id, ["def", "class", "import"], 6
        )

    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _qa_messages(message, chunks, history), max_tokens=10000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)

    await emit(
        "meta",
        {
            "route": "repo",
            "code_sources": [
                {
                    "path": c["path"],
                    "start_line": c["start_line"],
                    "end_line": c["end_line"],
                    "snippet": c["text"][:1500],
                }
                for c in chunks
            ],
        },
    )
    return "".join(parts)


async def run_repo_engine(
    message: str,
    ref: Optional[GithubRef],
    conversation_id: str,
    history: Sequence[dict],
    emit: Emit,
) -> str:
    """New repo URL → clone + index + overview; otherwise answer code questions
    from the already-indexed repo."""
    if ref is not None and await db.run_in_thread(db.get_repo, conversation_id, ref.key) is None:
        overview = await _clone_and_index(ref, conversation_id, emit)
        if overview is None:
            note = f"I couldn't analyze {ref.key}."
            await emit("token", {"text": note})
            await emit("meta", {"route": "repo"})
            return note
        parts: List[str] = []
        async for kind, delta in llm.stream_chat_events(
            _overview_messages(ref, overview), max_tokens=8000
        ):
            await emit(kind, {"text": delta})
            if kind == "token":
                parts.append(delta)
        await emit(
            "meta",
            {"route": "repo", "repo": {"key": ref.key, "files": overview.file_count}},
        )
        return "".join(parts)

    # follow-up question about an already-indexed repo
    return await _code_qa(message, conversation_id, history, emit)
