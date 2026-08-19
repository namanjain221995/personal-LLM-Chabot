"""Chat engine (V2-DESIGN §3a): plain streamed completions, no data engines.

Two uses:
- mode="assistant": the router and data engines are bypassed entirely; the
  selected model answers as a helpful local assistant (general knowledge OK,
  never claims to have consulted Salesforce data).
- mode="salesforce" + router class "chat": greetings/small talk get a brief
  friendly reply that mentions toggling Salesforce mode off for general
  questions.

Streams vLLM reasoning deltas as `reasoning` events and answer deltas as
`token` events; emits the single final meta {route: "chat"} (mode/model/
effort are merged in centrally by the /chat endpoint).
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Sequence

from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, recent_turns
from .. import llm
from ..config import settings
from ..core import best_of

Emit = Callable[[str, dict], Awaitable[None]]

ASSISTANT_SYSTEM = (
    "You are the TechSara local AI assistant, running entirely on this "
    "machine. Be helpful, clear, and concise, and use general knowledge "
    "freely. You are NOT connected to Salesforce data in this mode — never "
    "claim to have looked something up in Salesforce or invent CRM numbers; "
    "if asked about the user's Salesforce data, suggest switching Salesforce "
    "mode on."
)

SALESFORCE_CHAT_SYSTEM = (
    "You are the TechSara Local AI Analysis Platform for Salesforce data. "
    "The user sent a greeting, small talk, thanks, or a question about you "
    "rather than a data question. Reply briefly and warmly (a couple of "
    "sentences) and offer to answer questions about their Salesforce data. "
    "Never invent Salesforce numbers.\n"
    # This prompt used to say the platform had no Salesforce access, and the
    # model repeated it verbatim — "I don't have direct access to your live
    # Salesforce org" — to a user whose org it had been querying all session.
    "IMPORTANT: you DO have Salesforce access. This platform holds a synced "
    "copy of the org and can also query Salesforce live over the API. Never "
    "tell the user you cannot see their Salesforce data, and never suggest "
    "they check it themselves or run a script. If their message is actually a "
    "data question, say you will look it up and ask them to send it as a "
    "direct question (for example \"does the interview record for X exist?\")."
)


def _messages(message: str, history: Sequence[dict], mode: str) -> List[dict]:
    # Salesforce-mode "chat" is greetings/small talk — a diagram would never
    # belong there, so only assistant mode carries the diagram capability.
    system = (
        ASSISTANT_SYSTEM + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION
        if mode == "assistant"
        else SALESFORCE_CHAT_SYSTEM
    )
    return (
        [{"role": "system", "content": system}]
        + recent_turns(history, 6)
        + [{"role": "user", "content": message}]
    )


async def run_chat_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    mode: str = "salesforce",
    model_choice: str = "smart",
    effort: str = "medium",
) -> str:
    """Stream a plain completion from the selected model; meta route=chat."""
    effort = llm.normalize_effort(effort)
    # The thinking model spends a large, variable share of its budget on
    # reasoning before emitting a single answer token — a small ceiling makes
    # longer asks (e.g. "draw a flowchart of X") come back EMPTY. max_tokens is
    # only a cap, so a generous value costs nothing on short replies.
    max_tokens = 8000 if mode == "assistant" else 6000
    # High is the level for hard questions — long code, real derivations — so
    # it gets room to finish. A ceiling that cuts the answer mid-function is
    # worse than a slow answer.
    if effort in ("think", "max") and mode == "assistant":
        max_tokens = 16000
    # Thinking levels are used for code and analysis, where 0.6 invents API
    # names and drifts. Fast/Low stay conversational.
    temperature = 0.3 if effort in ("think", "max") else 0.6

    # extra_high = best-of-N: EXTRA_HIGH_SAMPLES candidates generated
    # CONCURRENTLY, a thinking-off guided-JSON judge picks the winner, and
    # the winner's thinking + answer stream to the UI (core/best_of.py).
    # Zero usable candidates falls through to the ordinary single stream —
    # best-of-N must never make extra_high worse than high.
    if (
        effort == "max"
        and model_choice == "smart"
        and settings.extra_high_samples > 1
    ):
        prompt = _messages(message, history, mode)
        candidates = await best_of.generate_candidates(
            prompt,
            n=settings.extra_high_samples,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if any(c.usable for c in candidates):
            winner, reason = await best_of.select_best(message, candidates)
            best_of.log_losers(candidates, winner)
            for start in range(0, len(winner.reasoning), 1000):
                await emit(
                    "reasoning", {"text": winner.reasoning[start : start + 1000]}
                )
            for start in range(0, len(winner.answer), 200):
                await emit("token", {"text": winner.answer[start : start + 200]})
            await emit(
                "meta",
                {
                    "route": "chat",
                    "best_of": settings.extra_high_samples,
                    "best_of_winner": winner.index,
                    "best_of_reason": reason,
                },
            )
            return winner.answer

    parts: List[str] = []
    async for kind, text in llm.stream_chat_events(
        _messages(message, history, mode),
        model_choice=model_choice,
        effort=effort,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
        else:
            parts.append(text)
            await emit("token", {"text": text})

    # §10/V2 §2: the SINGLE final meta — no citations/sql keys on this route.
    await emit("meta", {"route": "chat"})
    return "".join(parts)
