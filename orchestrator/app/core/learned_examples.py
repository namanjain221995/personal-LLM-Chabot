"""Learn from chat: thumbs-up answers become few-shot SQL examples.

Every rated assistant message already stores the SQL that produced it
(messages.meta->>'sql') next to the thumb (messages.feedback). That is a
growing corpus of org-specific question→SQL pairs each verified by the one
judge who matters — the user who asked. This module replays the best of them
into SQL generation, so the model reuses a join or filter a person has
already confirmed instead of re-deriving it and getting it subtly wrong.

Why few-shots and not fine-tuning: the pairs arrive one at a time, must take
effect immediately, must be revocable (a thumb can be cleared or contested),
and the serving model is a shared vLLM instance. Retrieval gives all four for
free; a LoRA gives none of them without an ops pipeline this platform does
not need yet.

Deliberately conservative:
  * thumbs-UP examples only — a down-vote excludes that SQL text globally
    (see db.list_confirmed_sql_examples);
  * lexical similarity, same stemmed-token approach as sf_dictionary — cheap,
    offline, predictable;
  * capped at settings.learned_examples_k per prompt so examples never crowd
    out the schema and the org brief;
  * never fatal — any failure means "no examples", not a broken request.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings
from .brain import _tokens  # one tokenizer for all lexical retrieval

log = logging.getLogger("learned_examples")

#: The corpus changes only when someone rates an answer; re-reading it every
#: prompt would tax the messages table for nothing. Five minutes of staleness
#: on a NEW thumb is invisible; a CLEARED thumb lingering five minutes is the
#: acceptable cost.
_CACHE_TTL = 300.0

#: Keep prompts honest: a question longer than this is truncated in the shot,
#: and an SQL body longer than this means the example is skipped outright —
#: a 4,000-char query as a "shot" is noise.
_MAX_QUESTION_CHARS = 400
_MAX_SQL_CHARS = 2000

_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None


def invalidate() -> None:
    """Drop the cache (tests, and anywhere freshness genuinely matters)."""
    global _cache
    _cache = None


def _corpus() -> List[Dict[str, Any]]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    from .. import db

    examples = db.list_confirmed_sql_examples()
    _cache = (now, examples)
    return examples


def block_for(question: str) -> str:
    """A few-shot block of confirmed (question, SQL) pairs, or "".

    Empty when learning is disabled, nothing is rated yet, or nothing rated
    resembles this question — the prompt is unchanged in all three cases.
    """
    if not settings.learned_examples_enabled:
        return ""
    try:
        corpus = _corpus()
    except Exception as exc:  # noqa: BLE001 — the DB being down must not stop SQL
        log.info("learned examples unavailable: %s", str(exc)[:160])
        return ""
    if not corpus:
        return ""
    tokens = _tokens(question)
    if not tokens:
        return ""
    # A one-meaningful-word question ("how many invoices?") can never overlap
    # on two tokens, so the floor adapts to the question, not the corpus.
    floor = min(2, len(tokens))
    scored = []
    for example in corpus:
        if len(example["sql"]) > _MAX_SQL_CHARS:
            continue
        overlap = len(tokens & _tokens(example["question"]))
        if overlap >= floor:
            scored.append((overlap, example))
    if not scored:
        return ""
    scored.sort(key=lambda pair: -pair[0])
    picked: List[Dict[str, Any]] = []
    seen_questions = set()
    for _score, example in scored:
        key = " ".join(sorted(_tokens(example["question"])))
        if key in seen_questions:
            continue
        seen_questions.add(key)
        picked.append(example)
        if len(picked) >= settings.learned_examples_k:
            break
    shots = "\n\n".join(
        f"Q: {ex['question'][:_MAX_QUESTION_CHARS]}\nSQL: {ex['sql']}" for ex in picked
    )
    return (
        "SQL that answered similar questions in this org before, and that the "
        "asker confirmed was CORRECT (thumbs-up). Reuse their joins, filters "
        "and casts wherever this question matches; change only what this "
        "question changes:\n" + shots
    )
