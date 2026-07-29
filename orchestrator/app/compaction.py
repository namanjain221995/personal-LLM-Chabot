"""Per-session context management: budget maths + auto-compaction (Phase A).

The model is stateless and a session's stored history has no size limit, so
every request is assembled fresh from ONLY that session's data:

    system prompt → rolling summary → retrieved snippets → recent turns
    verbatim → current user message

and always reserves this request's own output budget plus the safety margin.

Compaction has two paths sharing ONE idempotent mechanism (`covers_through`,
a count of leading messages already folded):

- background, right after a turn completes, above BG threshold (0.70) — the
  normal path, so the user practically never waits;
- synchronous, before a request goes out, above the hard threshold (0.80) —
  the guarantee that covers a burst of very long turns.

Because both only ever fold messages beyond `covers_through` and write it back
under a per-conversation lock, they cannot double-fold or race each other.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from . import context, db, summarize
from .config import settings

Emit = Callable[[str, dict], Awaitable[None]]

# One lock per conversation: the background compact of turn N and the
# synchronous compact of turn N+1 must not interleave.
_locks: Dict[str, asyncio.Lock] = {}


# A background compaction finishes AFTER the stream's `done`, so its notice
# cannot ride on that answer's meta. It is held here and attached to the next
# reply instead — the user still never waited for it.
_pending_notice: Dict[str, dict] = {}


def take_pending_notice(conversation_id: str) -> Optional[dict]:
    return _pending_notice.pop(conversation_id, None)


def _lock_for(conversation_id: str) -> asyncio.Lock:
    lock = _locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conversation_id] = lock
    return lock


@dataclass
class Budget:
    """What one request may spend, and on what."""

    window: int
    output_reserved: int
    usable: int
    used: int
    breakdown: Dict[str, int] = field(default_factory=dict)

    @property
    def fraction(self) -> float:
        return (self.used / self.usable) if self.usable > 0 else 1.0


def usable_budget(window: int, output_reserved: int) -> int:
    """Room for the prompt once THIS request's output is reserved.

    The reservation is the actual budget of the request in hand, not a global
    constant: an engine asking for 12000 output tokens genuinely has less
    prompt room than one asking for 2000.
    """
    return max(1, window - output_reserved - settings.context_safety_margin)


def output_reservation(requested: Optional[int], window: Optional[int] = None) -> int:
    """Output tokens to reserve, never below the thinking model's floor.

    A thinking model spends most of its budget reasoning; squeezing output
    below the floor produces an EMPTY answer, which is worse than a shorter
    history. So the floor wins and the input gets trimmed instead.

    The reservation is also bounded by the WINDOW. Reserving the default 8192
    against an 8192-token model would leave nothing at all for the prompt —
    the small "fast" model would report a full context on an empty chat. Half
    the window is the cap, and the floor still wins under that.
    """
    ceiling = requested or settings.model_max_output
    reserved = max(settings.min_output_floor, ceiling)
    if window:
        reserved = min(reserved, max(settings.min_output_floor, window // 2))
    return reserved


async def measure(
    messages: Sequence[dict],
    *,
    base_url: str,
    model: str,
    requested_max_tokens: Optional[int] = None,
) -> Budget:
    """Token accounting for a candidate prompt."""
    window = await context.model_window(base_url, model)
    used, served = await context.count_tokens(base_url, model, list(messages))
    if served:
        window = served
    reserved = output_reservation(requested_max_tokens, window)
    return Budget(
        window=window,
        output_reserved=reserved,
        usable=usable_budget(window, reserved),
        used=used,
    )


def split_history(history: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """(pinned system blocks, conversational turns)."""
    system = [m for m in history if m.get("role") == "system"]
    turns = [m for m in history if m.get("role") != "system"]
    return system, turns


# Never keep fewer than this verbatim: the model still needs the immediate
# exchange it is replying to.
MIN_KEEP_RECENT = 2
# Bound on the adaptive shrink so a pathological chat cannot loop.
_MAX_ADAPTIVE_ROUNDS = 4


def fold_boundary(
    turn_count: int, covers_through: int, keep: Optional[int] = None
) -> int:
    """How many leading turns should be folded, keeping the recent tail.

    Returns the new `covers_through`; equal to the old one when there is
    nothing worth folding. `keep` overrides KEEP_RECENT_TURNS so the caller
    can keep fewer when the tail alone will not fit.
    """
    keep = max(1, settings.keep_recent_turns if keep is None else keep)
    boundary = max(0, turn_count - keep)
    return max(covers_through, boundary)


def assemble(
    history: Sequence[dict],
    summary: Optional[str],
    covers_through: int,
    retrieved: Optional[str] = None,
) -> List[dict]:
    """Build the prompt history: system → summary → retrieved → recent turns.

    `covers_through` counts TURNS (system blocks are pinned and never folded).
    """
    system, turns = split_history(history)
    covers_through = max(0, min(covers_through, len(turns)))
    out: List[dict] = list(system)
    if summary and summary.strip():
        out.append(summarize.summary_block(summary))
    if retrieved and retrieved.strip():
        out.append({"role": "system", "content": retrieved})
    out.extend(turns[covers_through:])
    return out


async def _fold(
    conversation_id: str,
    turns: Sequence[dict],
    covers_through: int,
    new_boundary: int,
    existing: str,
) -> Optional[dict]:
    """Summarize turns[covers_through:new_boundary] into the stored summary."""
    folded = list(turns[covers_through:new_boundary])
    if not folded:
        return None
    summary = await summarize.summarize(existing, folded)
    if len(summary) / 3 > settings.summary_max_tokens * 0.9:
        # The summary is approaching its own cap — condense it rather than
        # letting it crowd out the turns it exists to make room for.
        summary = await summarize.condense(summary)
    db.save_summary(
        conversation_id, summary, new_boundary, context.estimate_tokens(summary)
    )
    # Phase B indexes the same folded turns for retrieval; a failure there
    # must not undo a successful summary.
    try:
        from . import recall

        await recall.index_folded(conversation_id, folded, covers_through)
    except Exception:
        pass
    return {
        "summary": summary,
        "covers_through": new_boundary,
        "folded": len(folded),
    }


async def compact(
    conversation_id: str,
    history: Sequence[dict],
    *,
    force: bool = False,
    keep: Optional[int] = None,
) -> Optional[dict]:
    """Fold older turns into the rolling summary. Idempotent and locked.

    Returns a dict describing what happened, or None when nothing was folded.
    Never raises: compaction is best-effort, and a chat must not break because
    summarizing failed.
    """
    async with _lock_for(conversation_id):
        try:
            _, turns = split_history(history)
            row = db.get_summary(conversation_id)
            existing = row["summary"] if row else ""
            covers = min(row["covers_through"] if row else 0, len(turns))
            boundary = (
                len(turns) if force else fold_boundary(len(turns), covers, keep)
            )
            # Never fold the turn currently being answered.
            boundary = min(boundary, max(0, len(turns) - 1))
            if boundary <= covers:
                return None
            return await _fold(conversation_id, turns, covers, boundary, existing)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "compaction failed for %s; falling back to turn trimming",
                conversation_id,
                exc_info=True,
            )
            return None


async def prepare(
    conversation_id: str,
    history: Sequence[dict],
    current_text: str,
    *,
    base_url: str,
    model: str,
    requested_max_tokens: Optional[int] = None,
    emit: Optional[Emit] = None,
    retrieved: Optional[str] = None,
) -> Tuple[List[dict], dict]:
    """Assemble this request's history, compacting first if it would overflow.

    Returns (history, info) where info feeds the answer's meta: the context
    meter reads it, and a compaction is reported to the user.
    """
    row = db.get_summary(conversation_id)
    summary = row["summary"] if row else None
    covers = row["covers_through"] if row else 0

    candidate = assemble(history, summary, covers, retrieved)
    probe = list(candidate) + [{"role": "user", "content": current_text}]
    budget = await measure(
        probe,
        base_url=base_url,
        model=model,
        requested_max_tokens=requested_max_tokens,
    )

    compacted = None
    if budget.fraction > settings.context_compact_threshold:
        if emit is not None:
            await emit("status", {"text": "Compacting conversation…"})
        # Adaptive: KEEP_RECENT_TURNS verbatim turns can themselves exceed the
        # budget (a handful of huge pastes). Rather than fold once and fall
        # through to clipping — which silently drops part of what the user
        # wrote — keep fewer turns and fold more, halving until it fits.
        keep = settings.keep_recent_turns
        for _ in range(_MAX_ADAPTIVE_ROUNDS):
            result = await compact(conversation_id, history, keep=keep)
            if result:
                compacted = result
                summary, covers = result["summary"], result["covers_through"]
            else:
                # Nothing folded — either there was nothing to fold, or a
                # CONCURRENT compaction (the detached background task) got
                # there first. Re-read the stored state before judging: using
                # the stale pre-fold measurement here would shrink `keep` and
                # fold again for no reason.
                fresh = db.get_summary(conversation_id)
                if fresh is None:
                    break
                summary, covers = fresh["summary"], fresh["covers_through"]
            candidate = assemble(history, summary, covers, retrieved)
            probe = list(candidate) + [{"role": "user", "content": current_text}]
            budget = await measure(
                probe,
                base_url=base_url,
                model=model,
                requested_max_tokens=requested_max_tokens,
            )
            if budget.fraction <= settings.context_compact_threshold:
                break
            if keep <= MIN_KEEP_RECENT:
                break  # nothing left to give up; fit_request clips from here
            keep = max(MIN_KEEP_RECENT, keep // 2)

    info = {
        "tokens_used": budget.used,
        "usable_budget": budget.usable,
        "window": budget.window,
        "reserved_output": budget.output_reserved,
        "fraction": round(budget.fraction, 4),
        "summarized_turns": (
            compacted["covers_through"] if compacted else (covers if summary else 0)
        ),
    }
    if compacted:
        info["compacted"] = {"folded_turns": compacted["folded"], "background": False}
    else:
        # A compaction that ran in the background after the PREVIOUS turn
        # reports itself here, so the user still sees that it happened.
        pending = take_pending_notice(conversation_id)
        if pending:
            info["compacted"] = pending
    return candidate, info


async def maybe_background_compact(
    conversation_id: str,
    history: Sequence[dict],
    *,
    base_url: str,
    model: str,
    requested_max_tokens: Optional[int] = None,
) -> Optional[dict]:
    """After a turn completes: compact early so the NEXT one never waits."""
    try:
        row = db.get_summary(conversation_id)
        summary = row["summary"] if row else None
        covers = row["covers_through"] if row else 0
        candidate = assemble(history, summary, covers)
        budget = await measure(
            candidate,
            base_url=base_url,
            model=model,
            requested_max_tokens=requested_max_tokens,
        )
        if budget.fraction <= settings.context_bg_compact_threshold:
            return None
        result = await compact(conversation_id, history)
        if result:
            _pending_notice[conversation_id] = {
                "folded_turns": result["folded"],
                "background": True,
            }
        return result
    except Exception:
        return None
