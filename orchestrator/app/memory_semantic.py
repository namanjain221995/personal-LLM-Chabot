"""Semantic cross-chat recall (V10, 2026-08-21).

`memory_recall.py` finds past conversations by literal keyword overlap, so
"who runs the company?" never recalls an answer phrased as "the CEO is …".
This module adds meaning-based recall over the SAME stored messages: each
persisted chat message is embedded once (Qwen3-Embedding via EMBED_BASE_URL,
packed-float32 in PostgreSQL — see recall.py for why chat vectors never go
into the Salesforce LanceDB corpus) and each new question retrieves the
nearest messages from the user's OTHER conversations.

Embedding happens in arrears: `ensure_message_embeddings` drains a batch of
not-yet-embedded messages per request, so no persistence path had to change,
old messages backfill themselves, and a dead embedding service degrades to
keyword-only recall instead of failing the chat. Everything here is an
enhancement, never a precondition for answering.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

from . import db, llm
from .config import settings
from .memory_recall import format_recall_block, keywords
from .recall import cosine_many, pack_vector

log = logging.getLogger(__name__)

# Snippet length mirrors db._RECALL_SNIPPET_CHARS so both recall styles read
# the same in the prompt.
_SNIPPET_CHARS = 240
# How many not-yet-embedded messages one request may embed. One batch is a
# single /embeddings call (~tens of ms); the backlog drains across requests.
_EMBED_BATCH = 64
# How many candidate vectors to score per query. Brute-force cosine over
# packed float32 rows. This said "hundreds of vectors cost well under a
# millisecond"; measured 2026-09-06 the per-candidate shape cost ~40 ms for
# 500 rows at 1024 dimensions, on the event loop. `recall.cosine_many` scores
# the batch in one pass, which brings 500 rows back under ~2 ms.
_CANDIDATE_LIMIT = 500

#: A question about the person's OWN earlier conversations: "what did we
#: decide last time", "remind me what we agreed", "what did you tell me".
#: The freshness classifier marks these as needing evidence (reason
#: 'default'), which drops every assistant turn from recall — and the
#: assistant's answer is where a decision is usually stated (measured
#: 2026-09-18 with the real embedder, five synthetic decision chats: 0/5
#: blocks carried the decision). Every alternative names US or YOU as the
#: party, so "what did the central bank decide" and "the last time India won"
#: stay world-fact questions and keep the evidence gate.
_PAST_REFERENCE_RE = re.compile(
    r"\b(?:we|you and i|you and me)\s+(?:had\s+|have\s+|already\s+|finally\s+)?"
    r"(?:decided|agreed|settled|discussed|talked|chose|picked|concluded|went with)\b"
    r"|\bdid\s+(?:we|you|i)\s+(?:(?:finally|already|ever|ultimately)\s+)?"
    r"(?:decide|agree|settle|discuss|talk|choose|pick|conclude|go\s+with|end\s+up|"
    r"say|tell|recommend|suggest|advise|mention)\b"
    r"|\byou\s+(?:told|said|recommended|suggested|advised|mentioned|promised)\b"
    r"|(?<!if )\b(?:i|we)\s+(?:told|asked)\s+you\b"  # not "what if I asked you to …"
    # "last time we TALKED", not "the last time we landed on the moon" or
    # "last time I checked, bitcoin was 60k": the bare idiom opened the gate
    # for world-fact questions and recalled a stale answer (QA, 2026-09-18).
    r"|\blast\s+time\s+(?:we|you)\s+"
    r"(?:talked|spoke|chatted|discussed|asked|told|said|decided|agreed)\b"
    r"|\b(?:our|my)\s+(?:last|previous|earlier|other|old)\s+"
    r"(?:chat|conversation|session|discussion)\b"
    # "the last session" alone is also Parliament's; a CHAT is always ours.
    r"|\b(?:in|from)\s+(?:another\s+(?:chat|conversation)|"
    r"(?:the|that|an?)\s+(?:last|previous|earlier|other|old|different)\s+chat)\b"
    r"|\bour\s+(?:decision|agreement|conclusion)\b"
    # "Where did we land on the database?", "What database did we land on?",
    # "Which database are we going with?", "Remind me of the database
    # decision." — 4 of the 9 decision phrasings the verifier found missed
    # (2026-09-18). Not a bare "did we land": "Did we land on the moon in
    # 1969?" is a world question.
    r"|\bwhere\s+did\s+we\s+(?:finally\s+|ultimately\s+)?land\b"
    r"|\bdid\s+we\s+(?:finally\s+|ultimately\s+)?land\s+on\W*$"
    r"|\b(?:are|were)\s+we\s+(?:going|gonna\s+go)\s+with\b"
    r"|\bremind\s+me\s+(?:of|about)\s+(?:the|our|that)\s+(?:[\w-]+\s+){0,3}"
    r"(?:decision|choice|conclusion|plan)\b",
    re.I,
)

#: A courtesy typed after the question ("what did we decide?\nThanks!").
#: Read as the question, it hid the question above it.
_PLEASANTRY_LINE_RE = re.compile(
    r"\s*(?:thanks?(?:\s+(?:a\s+lot|so\s+much|again|in\s+advance))?|thank\s+you"
    r"(?:\s+(?:so\s+much|again|in\s+advance))?|thx|ty|tia|cheers|please|pls|"
    r"appreciate\s+it)[\s.!:)]*",
    re.I,
)


#: How much of the typed question `refers_to_past_conversation` reads.
_PAST_REFERENCE_MAX_CHARS = 2_000


def refers_to_past_conversation(query: str) -> bool:
    """True when `query` asks about what was said or decided in the person's
    own earlier conversations, not about the world.

    Only the typed question is read: the last non-empty line that is not a
    courtesy ("Thanks!"), at most 2,000 characters of it. The composer
    folds a paste into the message with no marker, so pasted meeting notes
    saying "we agreed to ship on Friday" opened the gate for the bitcoin
    question under them, and the whole-text scan cost 1.0-1.9 s of event
    loop on a 10 MB paste (QA, 2026-09-18)."""
    text = (query or "").rstrip()
    for _ in range(3):
        cut = text.rfind("\n")
        line = text[cut + 1 :][-_PAST_REFERENCE_MAX_CHARS:]
        if cut < 0 or not _PLEASANTRY_LINE_RE.fullmatch(line):
            break
        text = text[:cut].rstrip()
    return bool(_PAST_REFERENCE_RE.search(line))


def _env_float(name: str, default: float) -> float:
    """config._float semantics (unset or blank -> default; otherwise float()),
    read here until the integration lead moves the tunable into config.py."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


#: How long one user's candidate rows are reused (2026-09-13, plan item 3e).
#: Every assistant turn re-read the newest 500 message_embeddings rows WITH
#: their full message content — 754 kB of content plus 2 MB of vectors for
#: the owner (1,303 rows, read-only SELECT 2026-09-13) — and then lower()ed
#: and split() every one on the event loop. The rows only change when the
#: backfill writes new vectors (which invalidates below) or a conversation is
#: deleted (the fingerprint check below sees the count drop). 0 disables.
#: Named as it will be in config.py.
CROSS_CHAT_EMBEDDINGS_CACHE_S = _env_float("CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
#: Entries are per (user, model, excluded conversation, limit) because the
#: exclusion changes WHICH 500 rows come back. Worst case ~3 MB each.
_CANDIDATE_CACHE_MAX = 16

#: key -> (monotonic stamp, fingerprint, rows). Filled and read from worker
#: threads, hence the lock.
_candidate_cache: "OrderedDict[tuple, Tuple[float, tuple, List[dict]]]" = OrderedDict()
_candidate_lock = threading.Lock()
#: Bumped (under the lock) by every invalidation: per user, and for everyone.
#: A fetch that STARTED before an invalidation must not store its rows after
#: it (second prover pass, 2026-09-13, recall_cache_race.py: a rename that
#: landed while a fetch was in flight left the old title served for 60 s).
_user_generations: "dict[int, int]" = {}
_all_generation = [0]


def _generation(user_id: int) -> tuple:
    return (_all_generation[0], _user_generations.get(user_id, 0))


def invalidate_message_embeddings(user_id: Optional[int] = None) -> None:
    """Forget cached recall candidates for one user (or everyone).

    For writes the fingerprint cannot see: a conversation renamed or titled,
    a stored answer overwritten in place (main.py). New vectors and deletions
    need not call it — the fingerprint of the rows a key reads sees both."""
    with _candidate_lock:
        if user_id is None:
            _all_generation[0] += 1
            _candidate_cache.clear()
            return
        _user_generations[user_id] = _user_generations.get(user_id, 0) + 1
        for key in [k for k in _candidate_cache if k[0] == user_id]:
            _candidate_cache.pop(key, None)


def _embeddings_fingerprint(user_id: int, model_id: str, exclude_conversation_id: str = "") -> tuple:
    """A cheap summary of exactly the rows one cache key reads — this user's
    vectors OUTSIDE the excluded conversation — over idx_message_embeddings_user.

    Scoped to the key since the second prover pass (2026-09-13). It used to
    summarise ALL the user's vectors, and every turn's background backfill
    stores the previous turn's vectors — in the conversation the turn
    excludes — so the fingerprint moved every turn and the cache never hit
    for someone chatting (instrumented fast_path_bench followup: 0 hits, 17
    misses, fingerprint (1,1,...) .. (15,15,...)). Rows added or deleted
    anywhere the key reads move the count or the id sum. Blocking."""
    with db.read_connection() as con:
        row = con.execute(
            "SELECT count(*) AS n, max(message_id) AS top, coalesce(sum(message_id), 0) AS total"
            "  FROM message_embeddings"
            " WHERE user_id = %s AND model_id = %s AND conversation_id <> %s",
            (user_id, model_id, exclude_conversation_id or ""),
        ).fetchone()
    return (int(row["n"] or 0), row["top"], int(row["total"] or 0))


def _normalised(content: Optional[str]) -> str:
    return " ".join((content or "").lower().split())


def _load_candidates(
    user_id: int, model_id: str, exclude_conversation_id: Optional[str], limit: int
) -> List[dict]:
    """`db.fetch_message_embeddings`, cached, with each row's content already
    normalised (`_norm`) for the echo filter in `_rank_candidates`. Blocking: run in a thread."""
    # A Settings attribute of the same name wins (config.py, 2026-09-13).
    ttl = float(getattr(settings, "cross_chat_embeddings_cache_s", CROSS_CHAT_EMBEDDINGS_CACHE_S))

    def fetch() -> List[dict]:
        rows = db.fetch_message_embeddings(user_id, model_id, exclude_conversation_id, limit)
        for r in rows:
            r["_norm"] = _normalised(r.get("content"))
        return rows

    if ttl <= 0:
        return fetch()
    key = (user_id, model_id, exclude_conversation_id or "", int(limit))
    with _candidate_lock:
        generation = _generation(user_id)
    fingerprint = _embeddings_fingerprint(user_id, model_id, exclude_conversation_id or "")
    now = time.monotonic()
    with _candidate_lock:
        hit = _candidate_cache.get(key)
        if hit is not None and now - hit[0] < ttl and hit[1] == fingerprint:
            _candidate_cache.move_to_end(key)
            return hit[2]
    rows = fetch()
    with _candidate_lock:
        if _generation(user_id) != generation:
            return rows  # invalidated while this fetch ran: serve, never store
        _candidate_cache[key] = (now, fingerprint, rows)
        _candidate_cache.move_to_end(key)
        for stale in [k for k, v in _candidate_cache.items() if now - v[0] >= ttl]:
            _candidate_cache.pop(stale, None)
        while len(_candidate_cache) > _CANDIDATE_CACHE_MAX:
            _candidate_cache.popitem(last=False)
    return rows


def _rank_candidates(query: str, query_vec: List[float], candidates: List[dict]) -> List[tuple]:
    """(score, row) best first, echoes of the question dropped. CPU work over
    up to 500 rows, so it runs in a thread, not on the event loop."""
    norm_query = " ".join((query or "").lower().split())
    # Another conversation asking the same question carries no
    # information — this is exactly the failure mode keyword recall
    # had before its snippet fix; don't reintroduce it semantically.
    kept = [c for c in candidates if c["_norm"] != norm_query]
    scores = cosine_many(query_vec, [c["embedding"] for c in kept])
    scored = list(zip(scores, kept))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


#: An assistant turn this short that ends in "?" is a clarifying question,
#: not the answer: "Postgres or MySQL for the event store?" -> "What query
#: patterns do you expect?" -> "Mostly big aggregates." -> "Then go with
#: ClickHouse." Paired with the clarifying question alone, the decision
#: was recalled 0/3 live (QA, 2026-09-18). A full answer that ends in an
#: offer ("…Want me to sketch the schema?") is longer than this — and a
#: short one ("Go with Postgres. Want me to sketch the schema?") is two
#: sentences: taken for a question, it dragged the NEXT exchange, a stale
#: bitcoin price, into the block (verifier, 2026-09-18).
_CLARIFYING_MAX_CHARS = 300
_SENTENCE_END_RE = re.compile(r"[.!]\s")


def _is_role(row: dict, role: str) -> bool:
    return (row.get("role") or "").lower() == role


def _answer_index(candidates: List[dict]) -> dict:
    """user message_id -> the rows that answered it: the next embedded row of
    the same conversation, when that row is the assistant's — and, when that
    row is a clarifying question, the person's reply and the assistant turn
    after it (one round).

    Built from the candidate rows only (no extra query). A reply shorter than
    the backfill's 15-character minimum has no row, so the next row may be a
    later answer in the same conversation; a user row next means no pair."""
    by_conversation: dict = {}
    for row in candidates:
        by_conversation.setdefault(row["conversation_id"], []).append(row)
    answers: dict = {}
    for rows in by_conversation.values():
        rows.sort(key=lambda r: r["message_id"])
        for i, asked in enumerate(rows[:-1]):
            following = rows[i + 1]
            if not (_is_role(asked, "user") and _is_role(following, "assistant")):
                continue
            chain = [following]
            reply = (following.get("content") or "").strip()
            if (
                reply.endswith("?")
                and len(reply) <= _CLARIFYING_MAX_CHARS
                and not _SENTENCE_END_RE.search(reply)
                and i + 3 < len(rows)
                and _is_role(rows[i + 2], "user")
                and _is_role(rows[i + 3], "assistant")
            ):
                chain += [rows[i + 2], rows[i + 3]]
            answers[asked["message_id"]] = chain
    return answers


def _embedding_available() -> bool:
    """False on profiles with no embedding service (cpu, external-without-
    embeddings), where the launcher sets EMBED_MODEL=disabled and points
    EMBED_BASE_URL at disabled.invalid. Without this gate every assistant
    turn would spend an SDK-retried, doomed /embeddings call."""
    if (settings.embed_model or "").strip().lower() == "disabled":
        return False
    return "disabled.invalid" not in (settings.embed_base_url or "")


def _snippet(text: str) -> str:
    clean = " ".join((text or "").split())
    if len(clean) > _SNIPPET_CHARS:
        clean = clean[:_SNIPPET_CHARS] + "…"
    return clean


#: Where a long answer states its choice. The main model writes decision
#: answers in markdown, and in all six it wrote live (2026-09-19, 6,770-9,664
#: characters, the decision 5,434-9,413 characters in) the choice sat in one
#: of two shapes: a heading labelled Recommendation / Verdict / Conclusion,
#: with the choice in it or on the next line, or a line that opens with a
#: bold imperative ("**Adopt the Hybrid Model.**"). Plain prose falls back
#: to a sentence with a decision word in it.
_DECISION_LABEL_RE = re.compile(
    r"\b(?:recommend(?:ation|ed)?|verdict|decision|conclusion|bottom\s+line|tl;?dr|"
    r"in\s+short|final\s+(?:answer|call|choice|pick))\b",
    re.I,
)
_DECISION_LEAD_RE = re.compile(
    r"^\*\*\s*(?:go\s+with|use|choose|pick|adopt|take|build|ship|"
    r"hire|stick\s+with|switch\s+to|start\s+with|move\s+to|stay\s+with|sign|lease|keep|"
    r"launch|buy|migrate\s+to|standardi[sz]e\s+on)\b",
    re.I,
)
_DECISIVE_RE = re.compile(
    r"\b(?:decid(?:e|es|ed|ing)|decision|recommend(?:s|ed|ation)?|verdict|conclusion|"
    r"bottom\s+line|in\s+short|go(?:ing)?\s+with|went\s+with|choose|chose|pick(?:ed)?|"
    r"settled?\s+on|final\s+(?:answer|call|choice|pick)|best\s+(?:choice|option|fit|bet))\b",
    re.I,
)
_CONDITIONAL_RE = re.compile(r"\b(?:if|when|whenever|unless|in\s+case|only\s+for)\b", re.I)
#: A sentence ends at . ! ? before a space and a capital: not "Next.js",
#: not "(e.g. a shuttle)".
_ANSWER_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\"'(\[])")
_ANSWER_RULE_RE = re.compile(r"[-*_=#\s]+")
_LIST_MARKER_RE = re.compile(r"^(?:[-+]\s+|\*\s+(?!\*)|\d+[.)]\s+)")


def _decision_statements(text: str) -> List[str]:
    """The statements of a long answer that state its choice, in order."""
    lines = [
        # "2. **Use …**": the list number is not a sentence of its own
        _LIST_MARKER_RE.sub("", line.strip())
        for line in (text or "").splitlines()
        if line.strip()
        and not line.strip().startswith("|")  # a table row compares, it does not decide
        and not _ANSWER_RULE_RE.fullmatch(line.strip())
    ]
    marked: List[str] = []
    for i, line in enumerate(lines):
        if line.startswith("#") and _DECISION_LABEL_RE.search(line):
            heading = line.lstrip("#").strip()
            following = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("#") else ""
            first = _ANSWER_SENTENCE_END_RE.split(following, maxsplit=1)[0] if following else ""
            marked.append(f"{heading} {first}".strip())
        elif _DECISION_LEAD_RE.match(line):
            first = _ANSWER_SENTENCE_END_RE.split(line, maxsplit=1)[0]
            # "- **Choose GitLab CI** if you need…" is the alternative, not
            # the choice; two of them crowded out "### Recommendation:
            # **GitHub Actions**" in a held-out live answer
            if not _CONDITIONAL_RE.search(first):
                marked.append(first)
    if marked:
        return marked
    return [
        sentence
        for line in lines
        for sentence in _ANSWER_SENTENCE_END_RE.split(line)
        if _DECISIVE_RE.search(sentence)
    ]


def _answer_snippet(text: str) -> str:
    """A paired answer as its opening, the last two statements of its choice,
    and its close.

    Cut to the 240-character snippet, a decision stated later in the answer
    never reached the block: 72.7% of production assistant messages are
    longer than that (median 744 characters), and the main model's own
    answers to a decision question put the choice thousands of characters
    in (verifier, 2026-09-18: 0/3 recalled; 0/6 in my live set). At most
    four 240-character pieces, so a long answer still costs the block a
    bounded amount."""
    clean = " ".join((text or "").split())
    if len(clean) <= 2 * _SNIPPET_CHARS:
        return clean
    head, tail = clean[:_SNIPPET_CHARS], clean[-_SNIPPET_CHARS:]
    statements = [" ".join(s.split())[:_SNIPPET_CHARS] for s in _decision_statements(text)]
    statements = [s for s in statements if s and s not in head and s not in tail][-2:]
    return " … ".join([head, *statements, tail])


async def ensure_message_embeddings(user_id: int) -> int:
    """Embed a batch of stored messages that have no vector yet.

    Returns how many were embedded; 0 on any failure (recall then simply
    sees fewer candidates).
    """
    if not settings.cross_chat_semantic_enabled or not _embedding_available():
        return 0
    try:
        pending = await db.run_in_thread(
            db.messages_missing_embeddings,
            user_id,
            settings.embed_model,
            _EMBED_BATCH,
        )
        if not pending:
            return 0
        vectors = await llm.embed_texts([m["content"] for m in pending])
        if len(vectors) != len(pending) or not vectors[0]:
            return 0
        rows = [
            {
                "message_id": m["id"],
                "conversation_id": m["conversation_id"],
                "embedding": pack_vector(v),
            }
            for m, v in zip(pending, vectors)
        ]
        stored = await db.run_in_thread(
            db.store_message_embeddings,
            user_id,
            settings.embed_model,
            len(vectors[0]),
            rows,
        )
        # No invalidation here (2026-09-13): the key-scoped fingerprint sees
        # new vectors where a key reads them, and invalidating the whole user
        # threw away every entry on every turn.
        return stored
    except Exception:
        log.warning("message embedding backfill failed", exc_info=True)
        return 0


_backfills: dict = {}


def message_backfill_delay_s() -> float:
    """MESSAGE_BACKFILL_DELAY_S (default 3.0 s)."""
    return max(0.0, float(getattr(settings, "message_backfill_delay_s", 3.0) or 0.0))


async def _delayed_backfill(user_id: int) -> int:
    """The backfill, after a short pause (2026-09-14).

    It is started by the turn's own cross-chat read, so its batch embed (up
    to 64 texts) used to land on the embedding sidecar in the same second as
    that turn's query embed and rerank. The pause moves it behind them. The
    task is still created (and registered in `_backfills`) at once, so the
    one-in-flight rule holds and a later turn does not start a second one;
    there is no per-user minimum interval, so a conversation that just ended
    is recallable a few seconds later, as before.
    """
    delay = message_backfill_delay_s()
    if delay > 0:
        await asyncio.sleep(delay)
    return await ensure_message_embeddings(user_id)


def _backfill_in_background(user_id: int) -> None:
    """At most one backfill in flight per user; the task holds its own
    reference so it cannot be garbage-collected mid-flight."""
    task = _backfills.get(user_id)
    if task is not None and not task.done():
        return
    try:
        task = asyncio.get_running_loop().create_task(_delayed_backfill(user_id))
    except RuntimeError:  # pragma: no cover — no running loop
        return
    _backfills[user_id] = task
    task.add_done_callback(lambda t: _backfills.pop(user_id, None) if _backfills.get(user_id) is t else None)


def _hit(row: dict, snippet: str, score: float) -> dict:
    return {
        "title": row["title"],
        "role": row["role"],
        "snippet": snippet,
        "conversation_id": row["conversation_id"],
        "score": score,
    }


async def semantic_hits(
    user_id: int,
    query: str,
    exclude_conversation_id: Optional[str],
    limit: int = 3,
    *,
    pair_answers: bool = False,
) -> List[dict]:
    """Nearest stored messages from the user's other conversations.

    Returns [{title, role, snippet, conversation_id, score}] sorted by
    similarity; [] when disabled, nothing qualifies, or embedding fails.

    `pair_answers=True` follows each user hit with the assistant turn that
    answered it, whatever that answer's own score: for "what did we decide"
    the QUESTION is what resembles the query, and the answer holding the
    decision scored under the relative floor (real embedder, 2026-09-18:
    0.599 against 0.75 x 0.804 = 0.603). A question and its answer (with a
    clarifying round, when there was one) count as one hit against `limit`.
    """
    if (
        not settings.cross_chat_semantic_enabled
        or not _embedding_available()
        or not (query or "").strip()
    ):
        return []
    try:
        candidates = await db.run_in_thread(
            _load_candidates,
            user_id,
            settings.embed_model,
            exclude_conversation_id,
            _CANDIDATE_LIMIT,
        )
        if not candidates:
            return []
        query_vec = await llm.embed_query(query)
        scored = await db.run_in_thread(_rank_candidates, query, query_vec, candidates)
        answers = await db.run_in_thread(_answer_index, candidates) if pair_answers else {}
        score_of = {c["message_id"]: s for s, c in scored} if pair_answers else {}
        hits: List[dict] = []
        units = 0
        seen_snippets: set = set()
        # A RELATIVE floor as well as the absolute one. The absolute floor
        # alone (0.30) behaves badly on a small corpus: with nothing genuinely
        # relevant to find, everything that clears it is returned, and recall
        # degenerates into "the three least-unrelated things this user ever
        # said". Measured consequence — a French lesson asked "how to
        # translate" and the memory block handed the model snippets about
        # CODE, priming exactly the wrong reading of the word.
        #
        # Keyed off the best hit, so it never removes the top result and never
        # fires when recall is genuinely confident; it only drops the tail
        # that is much weaker than what was actually found.
        best = scored[0][0] if scored else 0.0
        relative_floor = best * settings.semantic_recall_relative_floor
        for score, c in scored:
            if score < settings.semantic_recall_min_score or units >= limit:
                break
            if score < relative_floor:
                break
            snippet = _snippet(c["content"])
            if snippet in seen_snippets:
                continue  # the same text stored in several conversations
            seen_snippets.add(snippet)
            hits.append(_hit(c, snippet, score))
            units += 1
            for answer in answers.get(c["message_id"], ()):
                answer_snippet = _answer_snippet(answer["content"])
                if answer_snippet not in seen_snippets:
                    seen_snippets.add(answer_snippet)
                    hits.append(
                        _hit(answer, answer_snippet, score_of.get(answer["message_id"], 0.0))
                    )
        return hits
    except Exception:
        log.warning("semantic recall failed", exc_info=True)
        return []


async def cross_chat_block(
    user_id: int,
    query: str,
    exclude_conversation_id: Optional[str],
    *,
    semantic_limit: int = 3,
    keyword_limit: int = 3,
    include_assistant: bool = True,
) -> Optional[str]:
    """One combined recall block: semantic hits first, keyword hits after.

    Both retrievers run over the same stored messages, so overlapping hits
    are deduplicated by snippet. None when neither finds anything.

    `include_assistant=False` keeps only what the USER said in earlier
    chats. An answer the assistant gave before is only as good as it ever
    was, and for a fact that changes (who holds an office, what a thing
    costs) it is not evidence — recalled verbatim it becomes the answer, and
    the model attaches the current sources' citations to it (measured
    2026-09-03: 0/3 vs 3/3 answers driven by one recalled reply).

    …except when the question is ABOUT those earlier chats ("what did we
    decide last time?", `refers_to_past_conversation`). The evidence gate
    exists for world facts; the person's own history is the thing asked
    for, and what the assistant said in it is part of that history. Such a
    question keeps assistant turns whatever the caller passed, and each
    recalled question brings the answer that followed it.
    """
    # The backfill (embedding this user's messages that have no vector yet)
    # used to run INLINE before every answer: one synchronous batch of up to
    # 64 embeddings on the critical path, against the most contended sidecar.
    # It now runs behind the answer; recall sees the new vectors from the
    # next turn on, which is when they can matter.
    _backfill_in_background(user_id)
    past = refers_to_past_conversation(query)
    if past:
        include_assistant = True
    # Passed only when set, so every other question makes exactly the call
    # it made before (test_knowledge_unified fakes the old signature).
    pairing = {"pair_answers": True} if past else {}
    semantic = await semantic_hits(
        user_id, query, exclude_conversation_id, limit=semantic_limit, **pairing
    )
    if not include_assistant:
        semantic = [h for h in semantic if (h.get("role") or "").lower() != "assistant"]
    keyword: List[dict] = []
    if keywords(query):
        try:
            keyword = await db.run_in_thread(
                db.recall_conversations,
                user_id,
                keywords(query),
                exclude_conversation_id,
                keyword_limit,
            )
        except Exception:
            log.warning("keyword recall failed", exc_info=True)
    if not include_assistant:
        keyword = [h for h in keyword if (h.get("role") or "").lower() != "assistant"]
    merged: List[dict] = []
    seen: set = set()
    for hit in [*semantic, *keyword]:
        snippet = hit.get("snippet") or ""
        if not snippet or snippet in seen:
            continue
        seen.add(snippet)
        merged.append(hit)
    return format_recall_block(merged)
