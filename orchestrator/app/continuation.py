"""One long answer out of many bounded model calls.

THE PROBLEM. A single completion is capped three times over: by the config
ceiling, by whatever context room `fit_request` can spare, and by
GEN_WALL_CLOCK_S. On this deployment the model decodes at ~46 tokens/second,
so even an unbounded call cannot exceed roughly 190,000 tokens inside the
70-minute guard. A million-token answer is therefore not a number to raise
anywhere — it is a sequence of calls that must be stitched into one text.

THE MECHANISM. Generate. Ask the streaming layer why it stopped. If it stopped
because it ran out of room — and only then — build the next call from what has
already been written and keep going. Stop the moment the model says it is
finished, the budget is spent, the deadline passes, or the text stops moving
forward.

WHAT MAKES THIS HARD IS NOT THE LOOP.

  1. THE PROMPT GROWS. Feeding every previous token back turns segment 50 into
     a 400,000-token prefill. So each continuation sees a fixed TAIL of the
     text verbatim — enough for local coherence, to finish the sentence it was
     cut off in — plus an OUTLINE of the headings already written, which is
     what stops it re-covering ground it cannot see any more.

  2. MODELS REPEAT THEMSELVES AT THE SEAM. Told to continue, a model very
     often re-emits the last line or two first. `_strip_overlap` removes the
     duplicated prefix by matching against the tail we just sent, so the seam
     is invisible in the output rather than merely rare.

  2b. AND THEY DO NOT RESUME MID-WORD. A completion is cut wherever the token
     budget runs out — routinely in the middle of a word, or on the space
     between two. Continuation goes through the chat API, so the model sees a
     finished assistant turn rather than a raw prefix, and it reliably starts
     a fresh word. Measured against the real model: two of four seams came out
     jammed ("narrowthe", "50-100candidates"). Strengthening the instruction
     did not fix it, because the model is not doing anything unreasonable.
     So the last partial word of a continuable segment is never emitted at
     all: the text is cut back to the last whitespace, the fragment is
     dropped, and the model rewrites that word from a clean boundary. It costs
     one word of latency and removes the entire class of artifact.

  3. A LOOP THAT CANNOT STOP IS WORSE THAN A SHORT ANSWER. Degenerate
     repetition is the failure mode of exactly this technique, and at six
     hours per million tokens an unattended loop is expensive. Three
     independent guards stop it: no measurable progress twice running,
     a segment whose opening already appears in the text, and a hard
     segment count. Every stop reason is recorded and reported.

WHAT THIS DELIBERATELY DOES NOT DO. It does not retry failures — that is
`llm.py`'s business and LLM_MAX_RETRIES is 0 by policy. It does not decide how
big a budget a request deserves; the caller passes one, because "how long may
this run" is a product decision and this file is a mechanism.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from . import llm
from .config import settings

log = logging.getLogger(__name__)

#: Reasons a run ended. `complete` is the only one that means the model
#: decided it was done; every other value means WE stopped it, and the caller
#: is expected to say so rather than present the text as finished.
STOP_COMPLETE = "complete"
#: Not an ending. What a mid-run checkpoint carries, so a persisted snapshot
#: never claims the model finished when it is still writing.
STOP_RUNNING = "running"
STOP_BUDGET = "budget"
STOP_SEGMENTS = "segments"
STOP_DEADLINE = "deadline"
STOP_NO_PROGRESS = "no_progress"
STOP_REPETITION = "repetition"
STOP_WALL_CLOCK = "wall_clock"
STOP_CANCELLED = "cancelled"
STOP_ERROR = "error"

#: Only this one means "there was more to write". A server that reports
#: nothing leaves finish_reason None, and None is treated as "finished" —
#: the safe direction, since guessing the other way loops.
_CONTINUABLE = {"length"}

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

#: Below this, a "continuation" is the model saying something polite rather
#: than writing. Two in a row ends the run.
_MIN_PROGRESS_CHARS = 40

#: How much of the seam to search for a repeated opening. Long enough to catch
#: a re-emitted paragraph, short enough to stay cheap at every segment.
_MAX_OVERLAP_CHARS = 800

#: A coincidental short match is not a repeat. "The " must not be stripped.
_MIN_OVERLAP_CHARS = 24

#: Longest run without any whitespace that will be held back waiting for a
#: word boundary. A base64 blob or a long URL has none, and stalling the
#: stream for it would be worse than a jammed seam.
_MAX_PENDING_CHARS = 400

CONTINUE_INSTRUCTION = (
    "Continue the text above from exactly where it stops.\n"
    "\n"
    "YOUR REPLY IS APPENDED DIRECTLY TO IT, character for character, with no "
    "separator inserted between them. So begin with the exact characters that "
    "come next — including a leading space if the next thing is a new word, "
    "and including the rest of the word if it stopped part-way through one.\n"
    "\n"
    "- Do not repeat any sentence, clause or phrase you have already written.\n"
    "- Do not summarise what came before, and do not add a preamble such as "
    "\"Continuing\" or \"Here is the rest\".\n"
    "- Keep the same voice, formatting and heading levels.\n"
    "- Write until the piece is genuinely finished, then stop."
)


@dataclass
class Segment:
    """One model call inside a long generation."""

    index: int
    chars: int
    tokens: Optional[int]
    finish_reason: Optional[str]
    overlap_stripped: int
    seconds: float


@dataclass
class LongResult:
    text: str
    stop_reason: str
    segments: List[Segment] = field(default_factory=list)
    #: None when no server in the chain reported usage — NOT MEASURED, never 0.
    output_tokens: Optional[int] = None
    #: True when the text stops before the model said it was finished.
    truncated: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    def as_meta(self) -> Dict[str, Any]:
        """What the UI and the stored message are told. Small and closed."""
        return {
            "segments": self.segment_count,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
            "truncated": self.truncated,
        }


def _outline(text: str, limit: int = 60) -> List[str]:
    """The headings written so far.

    This is the only memory a continuation has of the part of the text that no
    longer fits in its prompt. Without it, a model handed the last 6,000
    characters of a long report will happily start section 2 again.
    """
    found = [f"{'#' * len(h)} {t}" for h, t in _HEADING.findall(text)]
    return found[-limit:]


def _strip_overlap(previous_tail: str, new_text: str) -> str:
    """Remove a re-emitted opening.

    Told to continue, models frequently repeat the last line first. The fix is
    mechanical: find the longest suffix of what we showed the model that the
    new text begins with, and drop it.
    """
    if not previous_tail or not new_text:
        return new_text
    window = previous_tail[-_MAX_OVERLAP_CHARS:]
    for size in range(min(len(window), len(new_text)), _MIN_OVERLAP_CHARS - 1, -1):
        if new_text.startswith(window[-size:]):
            return new_text[size:]
    return new_text


def _repeats_existing(produced: str, candidate: str) -> bool:
    """True when a segment's opening already appears in the text.

    Catches the loop that overlap-stripping cannot: a model that jumps back a
    paragraph or two rather than repeating verbatim at the seam.
    """
    probe = " ".join(candidate.split())[:160]
    if len(probe) < 80:
        return False
    return probe in " ".join(produced.split())


def _continuation_messages(
    base: Sequence[dict], produced: str, tail_chars: int
) -> List[dict]:
    """The next call's prompt: the original request, a tail, an instruction.

    The tail is passed as an ASSISTANT turn rather than pasted into a user
    message, so the model reads it as its own writing to be continued instead
    of as material to comment on.
    """
    tail = produced[-tail_chars:]
    instruction = CONTINUE_INSTRUCTION
    covered = _outline(produced)
    if covered:
        instruction += (
            "\n\nSections already written (do not write any of these again):\n"
            + "\n".join(covered)
        )
    return [*base, {"role": "assistant", "content": tail}, {"role": "user", "content": instruction}]


#: Characters per token, used for the budget guard ONLY when no server in the
#: chain reported usage. Deliberately low: over-counting tokens makes the
#: budget bind early, which is the safe direction for a guard whose job is to
#: stop a six-hour loop. It is never shown to anyone — an unmeasured run
#: reports `output_tokens: None`, because a guess presented as a count is the
#: thing the analytics console exists not to do.
_EST_CHARS_PER_TOKEN = 3.5


def _completion_tokens() -> Optional[int]:
    usage = llm.get_usage()
    if not usage:
        return None
    return int(usage.get("completion_tokens") or 0)


def _used_tokens(measured_start: Optional[int], produced: str) -> int:
    """Output spent so far — measured if the server said, estimated if not.

    A budget that silently stops applying whenever telemetry is missing is not
    a budget. `stream_options` is an extension a runtime may refuse (see
    `llm._ASK_FOR_USAGE`), and on that runtime the loop would otherwise run to
    the segment cap regardless of what the caller asked for.
    """
    now = _completion_tokens()
    if now is not None:
        return max(0, now - (measured_start or 0))
    return int(len(produced) / _EST_CHARS_PER_TOKEN)


async def stream_long_completion(
    messages: Sequence[dict],
    *,
    on_delta: Callable[[str, str], Awaitable[None]],
    model_choice: str = "smart",
    effort: str = "think",
    temperature: float = 0.2,
    segment_max_tokens: Optional[int] = None,
    total_max_tokens: Optional[int] = None,
    max_segments: Optional[int] = None,
    tail_chars: Optional[int] = None,
    deadline_s: Optional[float] = None,
    on_segment: Optional[Callable[[LongResult], Awaitable[None]]] = None,
) -> LongResult:
    """Produce one text across as many calls as the budget allows.

    `on_delta(kind, text)` receives the same (kind, delta) pairs
    `llm.stream_chat_events` yields, so a caller streams a long answer exactly
    as it streams a short one — the seams are not visible to the reader and no
    marker is emitted between segments.

    `on_segment` is called after every completed segment with the result so
    far. That is the checkpoint hook: it is where a caller persists state so a
    six-hour run survives a restart.
    """
    segment_cap = segment_max_tokens or settings.model_max_output
    total_cap = total_max_tokens or settings.model_max_output
    segments_cap = max_segments or settings.continuation_max_segments
    tail = tail_chars or settings.continuation_tail_chars
    started = time.monotonic()

    base = list(messages)
    #: Whether a second call can happen at all. When it cannot, several
    #: seam defences are pure cost and are switched off.
    may_continue = total_cap > segment_cap and segments_cap > 1
    produced = ""
    segs: List[Segment] = []
    errors: List[str] = []
    empty_runs = 0
    stop = STOP_COMPLETE
    measured_start = _completion_tokens()

    def _result(reason: str) -> LongResult:
        # `measured_start` is None when nothing had reported usage YET — which
        # is the normal state at the start of a turn, since `reset_usage()`
        # clears the ContextVar to None rather than to zero. Treating that as
        # "unmeasured" made every run report `output_tokens: None` even when
        # every segment had been measured. Only a None NOW means unmeasured.
        total = _completion_tokens()
        tokens = None if total is None else max(0, total - (measured_start or 0))
        return LongResult(
            text=produced,
            stop_reason=reason,
            segments=list(segs),
            output_tokens=tokens,
            truncated=reason != STOP_COMPLETE,
            errors=list(errors),
        )

    for index in range(segments_cap):
        # A continuation may not exceed what is left of the total budget, and
        # never asks for less than a floor — a 40-token segment is all seam
        # and no text.
        remaining = total_cap - _used_tokens(measured_start, produced)
        if index and remaining <= settings.continuation_min_segment_tokens:
            stop = STOP_BUDGET
            break
        ask = min(segment_cap, max(remaining, settings.continuation_min_segment_tokens))

        prompt = base if index == 0 else _continuation_messages(base, produced, tail)
        seg_started = time.monotonic()
        seg_chars_before = len(produced)
        previous_tail = produced[-tail:]
        seg_tokens_before = _completion_tokens()

        # STREAMING GRANULARITY IS NOT NEGOTIABLE. Buffering a whole segment to
        # strip its seam would deliver the answer in one lump per call, which
        # is the entire thing streaming exists to avoid. So only the OPENING of
        # a continuation is held back — just enough to decide the overlap — and
        # everything after it goes straight through, delta by delta. The first
        # segment holds nothing at all, so ordinary answers stream exactly as
        # they did before this file existed.
        holding = index > 0
        head: List[str] = []
        head_len = 0
        stripped = 0
        repeated = False
        # Text produced but NOT yet emitted: everything after the last
        # whitespace. See 2b in the module docstring — the trailing partial
        # word is held so a continuable segment can drop it.
        pending = ""

        async def _push(text: str) -> None:
            """Emit up to the last word boundary; hold the rest.

            Only when a continuation is POSSIBLE. When the budget allows one
            call and no more there is no seam to protect, so every delta goes
            straight out and the ordinary path streams exactly as it did
            before this file existed — which is what the Fast effort is.
            """
            nonlocal pending, produced
            if not text:
                return
            if not may_continue:
                produced += text
                await on_delta("token", text)
                return
            pending += text
            cut = max(pending.rfind(" "), pending.rfind("\n"), pending.rfind("\t"))
            if cut < 0:
                # No boundary anywhere. Holding for ever would stall the
                # stream, so a run this long is released as-is.
                if len(pending) < _MAX_PENDING_CHARS:
                    return
                cut = len(pending) - 1
            emit_now, pending = pending[: cut + 1], pending[cut + 1 :]
            if emit_now:
                produced += emit_now
                await on_delta("token", emit_now)

        async def _release() -> None:
            """Decide the seam on the held opening, then let it go."""
            nonlocal holding, head, head_len, stripped, repeated
            raw_head = "".join(head)
            head = []
            head_len = 0
            holding = False
            text = _strip_overlap(previous_tail, raw_head)
            stripped = len(raw_head) - len(text)
            # The text now always ends on a boundary and the model is told to
            # start a new word, so both sides often supply the space. A
            # leading NEWLINE is left alone — that is a deliberate paragraph
            # break, not an accident of the seam.
            if produced.endswith((" ", "\n", "\t")):
                text = text.lstrip(" ")
            if text.strip() and _repeats_existing(produced, text):
                repeated = True
                return
            await _push(text)

        # Clear it OURSELVES before the call we are about to ask about.
        # `stream_chat_events` clears it too, but this loop must not depend on
        # that: anything that supplies the stream by another route — a stub, a
        # future wrapper, a different generation function — would otherwise
        # leave the previous segment's "length" standing and the loop would
        # continue on a call that reported nothing. A consumer of "why did the
        # LAST call stop" resets before the call, not after.
        llm.reset_finish_reason()
        stream = llm.stream_chat_events(
            prompt,
            model_choice=model_choice,
            effort=effort,
            temperature=temperature,
            max_tokens=ask,
        )
        try:
            async for kind, delta in stream:
                if kind != "token":
                    # Reasoning belongs to the segment that produced it and is
                    # passed straight through; it is never part of the text.
                    await on_delta(kind, delta)
                    continue
                if holding:
                    head.append(delta)
                    head_len += len(delta)
                    if head_len < _MAX_OVERLAP_CHARS:
                        continue
                    await _release()
                    if repeated:
                        break
                    continue
                await _push(delta)
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            errors.append(f"segment {index}: {type(exc).__name__}: {exc}")
            log.exception("long generation failed in segment %d", index)
            # Everything already written stays. A partial answer beats none.
            if index == 0:
                return _result(STOP_ERROR)
            stop = STOP_ERROR
            break
        finally:
            # Breaking out of an `async for` does NOT close the generator, and
            # an unclosed one holds its HTTP response until it is collected.
            with contextlib.suppress(Exception):
                await stream.aclose()

        # A segment shorter than the hold never reached the release above.
        if holding and not repeated:
            await _release()

        reason = llm.get_finish_reason()
        # The held fragment is a real ending only when nothing follows it. If
        # this segment is going to be continued, the fragment is an
        # interrupted word and the next call rewrites it from a clean
        # boundary — so it is dropped rather than shown.
        if pending and reason not in _CONTINUABLE:
            produced += pending
            await on_delta("token", pending)
            pending = ""
        produced_here = produced[seg_chars_before:]

        if repeated:
            log.warning(
                "long generation stopped: segment %d repeats text already written",
                index,
            )
            segs.append(
                Segment(index, 0, None, reason, stripped, time.monotonic() - seg_started)
            )
            stop = STOP_REPETITION
            break

        seg_tokens_after = _completion_tokens()
        segs.append(
            Segment(
                index=index,
                chars=len(produced_here),
                tokens=(
                    None
                    if seg_tokens_after is None
                    else max(0, seg_tokens_after - (seg_tokens_before or 0))
                ),
                finish_reason=reason,
                overlap_stripped=stripped,
                seconds=time.monotonic() - seg_started,
            )
        )
        if on_segment is not None:
            await on_segment(_result(STOP_RUNNING))

        # --- why we might go round again, in order of authority ---

        if reason == llm.WALL_CLOCK_FINISH:
            # Our own guard fired inside one call. Continuing would just hit
            # it again; something is wrong upstream.
            stop = STOP_WALL_CLOCK
            break
        if reason not in _CONTINUABLE:
            # "stop", "tool_calls", or nothing reported. The model is done.
            stop = STOP_COMPLETE
            break
        if len(produced_here.strip()) < _MIN_PROGRESS_CHARS:
            empty_runs += 1
            if empty_runs >= 2:
                stop = STOP_NO_PROGRESS
                break
        else:
            empty_runs = 0
        if deadline_s is not None and time.monotonic() - started >= deadline_s:
            stop = STOP_DEADLINE
            break
    else:
        stop = STOP_SEGMENTS

    result = _result(stop)
    if result.segment_count > 1 or result.truncated:
        log.info(
            "long generation: %d segment(s), %s tokens, %d chars, stopped=%s",
            result.segment_count,
            result.output_tokens if result.output_tokens is not None else "unmeasured",
            len(result.text),
            result.stop_reason,
        )
    return result


def budget_for(effort: str) -> int:
    """How many output tokens one request of this effort may spend.

    Every effort gets the configured ceiling, so an answer runs until the
    MODEL decides it is finished. That is the point: the budget was never a
    quality control, and the things that ARE — the model stopping, repetition,
    no forward progress — do not depend on it and are always on.

    The per-effort settings remain, so the tiering can be restored by setting
    any of them lower. `continuation_enabled=False` returns every effort to a
    single call.
    """
    if not settings.continuation_enabled:
        return int(settings.model_max_output)
    return {
        "fast": int(settings.continuation_budget_fast),
        "think": int(settings.continuation_budget_think),
        "max": int(settings.continuation_budget_max),
    }.get(llm.normalize_effort(effort), int(settings.model_max_output))
