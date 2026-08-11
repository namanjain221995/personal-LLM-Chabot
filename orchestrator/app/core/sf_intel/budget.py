"""Build a prompt for a 262,144-token window without sending 262,144 tokens.

A big window is permission to include what matters, not an instruction to
include everything. Every token spent on a stale turn is latency the user pays
for on every request, and prefix caching only helps when the prefix is stable —
so blocks are added in a FIXED priority order and the tail is dropped, rather
than the whole thing being assembled and then trimmed from the middle.

Priority (highest first) mirrors §11A of the implementation directive:

     1  system + security instructions      never dropped
     2  the current user request            never dropped
     3  pending intent / clarification      never dropped
     4  tool definitions                    never dropped
     5  recent conversation turns
     6  the compact session summary
     7  previous Salesforce query state
     8  retrieved schema context
     9  retrieved Salesforce records
    10  older semantically relevant turns

Blocks 1-4 are PINNED: if they alone exceed the budget the request still goes
out (context.fit_request clips the largest message as a last resort) rather than
being silently answered without the question it was asked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from ...config import settings

#: Priorities, named so a call site reads as intent rather than as a number.
P_SYSTEM = 1
P_REQUEST = 2
P_PENDING = 3
P_TOOLS = 4
P_RECENT_TURNS = 5
P_SESSION_SUMMARY = 6
P_SALESFORCE_STATE = 7
P_SCHEMA = 8
P_RECORDS = 9
P_OLDER_TURNS = 10

#: Blocks that are never dropped, whatever the budget says.
PINNED = frozenset({P_SYSTEM, P_REQUEST, P_PENDING, P_TOOLS})


@dataclass(frozen=True)
class ContextBudget:
    """What one request may spend."""

    window: int
    reserved_output: int
    safety_margin: int

    @property
    def max_input_tokens(self) -> int:
        return max(0, self.window - self.reserved_output - self.safety_margin)

    def as_dict(self) -> dict:
        return {
            "window": self.window,
            "reserved_output": self.reserved_output,
            "safety_margin": self.safety_margin,
            "max_input_tokens": self.max_input_tokens,
        }


def budget_for(effort: str = "medium", *, window: Optional[int] = None) -> ContextBudget:
    """The documented budget for an effort level.

    High reserves more OUTPUT, not more input: a multi-step comparison that
    states its assumptions runs long, and truncating a conclusion is the worst
    place in an answer to run out of room.
    """
    reserved = (
        settings.model_high_max_output
        if effort == "high"
        else settings.model_max_output
    )
    return ContextBudget(
        window=int(window or settings.model_max_context),
        reserved_output=int(reserved),
        safety_margin=int(settings.main_model_context_safety_margin),
    )


@dataclass
class Block:
    """One candidate piece of prompt."""

    priority: int
    role: str
    content: str
    #: Ordering within a priority. Lower goes first, which is what keeps recent
    #: turns in chronological order once the set of them has been chosen.
    ordinal: int = 0
    label: str = ""


@dataclass
class BuiltContext:
    messages: List[dict] = field(default_factory=list)
    tokens: int = 0
    dropped: List[str] = field(default_factory=list)
    budget: Optional[ContextBudget] = None

    def as_dict(self) -> dict:
        out = {"tokens": self.tokens, "dropped_blocks": list(self.dropped)}
        if self.budget is not None:
            out.update(self.budget.as_dict())
        return out


def _estimate(text: str) -> int:
    from ... import context as ctx

    return ctx.estimate_tokens(text)


async def build(
    blocks: Sequence[Block],
    *,
    budget: ContextBudget,
    count: Optional[Callable] = None,
    base_url: str = "",
    model: str = "",
) -> BuiltContext:
    """Assemble the highest-priority blocks that fit.

    `count` is an async (messages) -> token_count. The default asks the SERVING
    vLLM through /tokenize, which is exact — the character estimate is a
    fallback for when that endpoint cannot be reached, and it is deliberately
    pessimistic so an estimate errs toward a smaller prompt.
    """
    ordered = sorted(blocks, key=lambda b: (b.priority, b.ordinal))
    kept: List[Block] = []
    dropped: List[str] = []

    running = 0
    for block in ordered:
        if not (block.content or "").strip():
            continue
        cost = _estimate(block.content) + 4
        if block.priority in PINNED or running + cost <= budget.max_input_tokens:
            kept.append(block)
            running += cost
        else:
            dropped.append(block.label or f"priority-{block.priority}")

    # Emit in priority order, which is also the order the model reads best:
    # instructions, then established context, then the request's own material.
    messages = [{"role": b.role, "content": b.content} for b in kept]

    tokens = running
    if count is not None and base_url and model:
        try:
            exact, _window = await count(base_url, model, messages)
            tokens = int(exact)
        except Exception:  # noqa: BLE001 — the estimate stands
            pass
    return BuiltContext(
        messages=messages, tokens=tokens, dropped=dropped, budget=budget
    )


async def build_default(
    blocks: Sequence[Block], *, effort: str = "medium"
) -> BuiltContext:
    """`build` wired to the main model's tokenizer and window."""
    from ... import context as ctx

    window = await ctx.model_window(settings.openai_base_url, settings.llm_model)
    return await build(
        blocks,
        budget=budget_for(effort, window=window),
        count=ctx.count_tokens,
        base_url=settings.openai_base_url,
        model=settings.llm_model,
    )
