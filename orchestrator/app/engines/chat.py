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

import contextlib
import logging
from typing import Awaitable, Callable, List, Optional, Sequence

from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, recent_turns
from .. import continuation, llm, metrics
from ..config import settings
from ..core import answer_sampling, best_of, effort_policy
from ..core import tracing as query_tracing

log = logging.getLogger(__name__)

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


def _lane_messages(message: str, history: Sequence[dict]) -> List[dict]:
    """The Fast small-talk lane's prompt (app/fast_lane.py): the persona, who
    is being assisted, the saved facts when main.py found them in time, and
    the last two exchanges clipped — no diagram or code rules, no grounding.
    Only THIS prompt is short; the stored conversation is untouched."""
    from .. import fast_lane
    from ..facts import FACTS_HEADER
    from ..identity import identity_line

    system = ASSISTANT_SYSTEM + identity_line()
    # main.py pins the saved-facts block as a system message; it is the one
    # system block the lane keeps. Recall and document blocks are never
    # assembled for a lane turn, and any other system message is dropped.
    for m in history:
        content = m.get("content")
        if m.get("role") == "system" and isinstance(content, str) and content.startswith(FACTS_HEADER):
            system = system + "\n\n" + content
            break
    turns = [
        {"role": m.get("role"), "content": str(m.get("content") or "")[: fast_lane.FAST_LANE_TURN_CHARS]}
        for m in recent_turns(
            [m for m in history if m.get("role") != "system"],
            fast_lane.FAST_LANE_HISTORY_TURNS * 2,
        )
    ]
    return [{"role": "system", "content": system}, *turns, {"role": "user", "content": message}]


def _messages(
    message: str, history: Sequence[dict], mode: str, grounding: str = "", lane: str = ""
) -> List[dict]:
    if lane:
        return _lane_messages(message, history)
    # Salesforce-mode "chat" is greetings/small talk — a diagram would never
    # belong there, so only assistant mode carries the diagram capability.
    from ..identity import identity_line

    system = (
        ASSISTANT_SYSTEM + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION
        if mode == "assistant"
        else SALESFORCE_CHAT_SYSTEM
    ) + identity_line()
    # Evidence goes AFTER the persona and the identity line, so the last thing
    # the model reads before the conversation is what today's date is and what
    # the sources actually say. Empty for a timeless question, which keeps an
    # ordinary chat exactly as cheap as it was.
    if grounding:
        system = system + "\n\n" + grounding
    return (
        [{"role": "system", "content": system}]
        # THE CONVERSATION, not a three-exchange slice. This was 6 — the
        # reason a 60-message French lesson answered "how to translate" with
        # a Python tutorial: the last six turns were a goodnight exchange and
        # the lesson itself was outside the window. Bounding history is
        # compaction's job (a rolling summary, on an absolute token budget)
        # and fit_request's (the physical window); an engine cutting on top
        # of both only throws away what they chose to keep.
        + recent_turns(history, settings.chat_history_turns)
        + [{"role": "user", "content": message}]
    )


async def _adaptive_thinking(
    message: str, model_choice: str, effort: str
) -> Optional[effort_policy.ThinkingDecision]:
    """Decide whether this thinking-off turn should think after all.

    None when the policy does not apply: it is switched off, or the turn
    already thinks (Think / Max). Every decision it does make — think or not —
    is counted and recorded on the query trace, by reason and signal names
    only, never the prompt.
    """
    if not settings.fast_adaptive_thinking or llm.wants_thinking(model_choice, effort):
        return None
    decision = effort_policy.classify(message)
    budget = settings.fast_thinking_budget if decision.think else 0
    metrics.inc(
        "fast_adaptive_thinking_total",
        "Fast turns by adaptive-thinking decision (think = bounded thinking on)",
        decision="think" if decision.think else "direct",
        reason=decision.reason,
    )
    await query_tracing.event(
        "ADAPTIVE_THINKING",
        component="orchestrator.app.engines.chat",
        details={**decision.as_trace(), "effort": effort, "budget_tokens": budget},
    )
    if decision.think:
        log.info(
            "adaptive thinking: Fast turn thinks (reason=%s signals=%s budget=%d classify=%.2fms)",
            decision.reason, ",".join(decision.signals), budget, decision.elapsed_ms,
        )
    return decision


async def run_chat_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    mode: str = "salesforce",
    model_choice: str = "smart",
    effort: str = "medium",
    grounding: str = "",
    lane: str = "",
) -> str:
    """Stream a plain completion from the selected model; meta route=chat.

    `lane` is the Fast small-talk lane's category (app/fast_lane.py) when
    main.py sent the turn down it: a short prompt, one call of at most
    FAST_LANE_MAX_TOKENS, thinking off (Fast), no grounding.

    `grounding` is the living-knowledge block (app/web_memory.py): source-backed
    passages this platform already read from the public web, plus today's date.
    It arrives ALREADY BUILT so this engine stays a plain completion — the
    decision about whether evidence was needed, and the cost of finding it,
    belong to the caller.
    """
    effort = llm.normalize_effort(effort)
    if lane:
        return await _run_lane(message, history, emit, model_choice=model_choice, effort=effort, lane=lane)
    # --- effort_policy (answer quality) begin ---
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
    total_max_tokens = continuation.budget_for(effort)
    # FAST (assistant and Salesforce): the sampling and length slice of the
    # answer plan (core/answer_sampling.py). Sampling defaults to exactly the
    # temperature above; the caps are one 8,000-token call for prose and up
    # to 64,000 across segments for long-form and structured asks. None —
    # Think, Max and every other mode — keeps the values above unchanged.
    answer_plan = answer_sampling.fast_sampling_for(
        message, history, mode=mode, effort=effort, model_choice=model_choice
    )
    if answer_plan is not None:
        max_tokens = answer_plan.segment_max_tokens
        temperature = answer_plan.sampling.get("temperature", temperature)
        # An operator who set CONTINUATION_BUDGET_FAST lower still wins.
        total_max_tokens = min(answer_plan.total_max_tokens, total_max_tokens)
    # --- effort_policy (answer quality) end ---

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
        prompt = _messages(message, history, mode, grounding)
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

    # LONG ANSWERS ARE MANY CALLS. `max_tokens` above is the ceiling on ONE
    # call and stays exactly that; the total an answer may run to is decided
    # by the effort the person chose (continuation.budget_for) — except at
    # Fast, where the answer plan sets it: equal to the call for prose, so a
    # Fast reply is one call, and larger only for long-form and structured
    # asks.
    #
    # The seams are invisible: deltas arrive here through `_out` in order,
    # with re-emitted openings already stripped, so the UI streams one answer.
    #
    # LOOP GUARD (core/answer_guard.py, 2026-09-15): every ANSWER delta passes
    # through the guard, which shows ordinary text at once and holds back only
    # text that re-treads what was already written. When the answer has begun
    # looping it raises StopGeneration, which ends the upstream stream exactly
    # as Stop does; the repeated tail is never shown, and what this returns —
    # the text that is stored — is precisely the text that was streamed.
    from ..core import answer_guard

    # A repetition the person asked for ("write it 50 times") is not a loop.
    guard = answer_guard.AnswerGuard(answer_guard.repetition_allowance(message))

    async def _out(kind: str, text: str) -> None:
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
            return
        for piece in guard.feed(text):
            await emit("token", {"text": piece})
        if guard.verdict is not None:
            raise continuation.StopGeneration(continuation.STOP_REPETITION)

    # ADAPTIVE THINKING (core/effort_policy.py). A Fast turn whose prompt is
    # a multi-step reasoning task thinks — bounded by FAST_THINKING_BUDGET on
    # top of the answer ceiling — instead of reasoning, and looping, inside
    # the answer. The reasoning streams through `_out` as `reasoning` events
    # like any Think turn's. The grant covers exactly this generation.
    decision = await _adaptive_thinking(message, model_choice, effort)
    thinks = decision is not None and decision.think
    scope = (
        effort_policy.grant(settings.fast_thinking_budget, decision.reason)
        if thinks
        else contextlib.nullcontext()
    )
    with scope:
        long = await continuation.stream_long_completion(
            _messages(message, history, mode, grounding),
            on_delta=_out,
            model_choice=model_choice,
            effort=effort,
            temperature=temperature,
            segment_max_tokens=max_tokens,
            total_max_tokens=total_max_tokens,
            deadline_s=settings.continuation_deadline_s or None,
            **({} if answer_plan is None else {"answer_plan": answer_plan}),
        )
    for piece in guard.finish():
        await emit("token", {"text": piece})

    # §10/V2 §2: the SINGLE final meta — no citations/sql keys on this route.
    # `continuation` is added only when there was something to say: a
    # one-segment answer looks exactly as it did before. A stopped loop says
    # so through it (stop_reason "repetition": "This answer stops here: it had
    # begun repeating itself.").
    meta = {"route": "chat"}
    if long.segment_count > 1 or long.truncated:
        meta["continuation"] = long.as_meta()
    if guard.verdict is not None:
        meta["loop_guard"] = guard.verdict.as_meta()
        await answer_guard.record(guard.verdict, effort=effort, route="chat")
    if thinks:
        meta["adaptive_thinking"] = {
            "reason": decision.reason,
            "budget_tokens": settings.fast_thinking_budget,
        }
    await emit("meta", meta)
    return guard.shown


async def _run_lane(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    model_choice: str,
    effort: str,
    lane: str,
) -> str:
    """One short streamed completion for a small-talk lane turn."""
    from .. import fast_lane

    async def _out(kind: str, text: str) -> None:
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
        else:
            await emit("token", {"text": text})

    # Segment cap == total cap: one call, no continuation seam to hold back,
    # so every delta streams straight through. Thinking follows the effort,
    # and the lane only admits Fast, where llm.wants_thinking is False.
    long = await continuation.stream_long_completion(
        _messages(message, history, "assistant", lane=lane),
        on_delta=_out,
        model_choice=model_choice,
        effort=effort,
        temperature=0.6,
        segment_max_tokens=fast_lane.FAST_LANE_MAX_TOKENS,
        total_max_tokens=fast_lane.FAST_LANE_MAX_TOKENS,
        deadline_s=settings.continuation_deadline_s or None,
    )
    meta = {"route": "chat"}
    if long.segment_count > 1 or long.truncated:
        meta["continuation"] = long.as_meta()
    await emit("meta", meta)
    return long.text
