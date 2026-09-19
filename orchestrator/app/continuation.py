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
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

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

#: THE LENGTH TARGET (`target_words`, 2026-09-18). "10,000 words" came back
#: as 24,364 words in one run and 5,340 in another: nothing here knew a
#: length had been asked for. Below `_TARGET_LOW` of it at a normal stop the
#: run gets ONE more segment; past `_TARGET_HIGH` it stops at the next line
#: break and says how long the answer is. That stop is reported as
#: STOP_BUDGET, not a new reason: the length asked for IS this run's length
#: budget, and every reader of a stop reason (the UI's notice, deep
#: research's report note) already has words for it. `LongResult.note` and
#: the `target_words`/`words` meta say which budget it was.
#: 70%, not the 85% first built: live 2026-09-19 the extension fired on two
#: of three 3,000-word articles that stopped at 79-83% after writing their
#: own closing paragraph under a heading `_has_ended` cannot know ("Legacy
#: and Modern Resonance", "Decline and Legacy"), and both times broke the
#: piece: a fragment ("of the ancient caravan routes.") glued after the
#: ending, and new body sections plus a second "Conclusion". That is the
#: fourth to sixth such break measured. A piece that ends at 80% is short;
#: one that ends under 70% missed the length.
_TARGET_LOW = 0.70
_TARGET_HIGH = 1.30
#: ...and never below THIS share of it. A reply that stopped under a quarter
#: of the length asked for was not written as that piece: a clarifying
#: question, a refusal, an outline. Live (QA r1, 2026-09-18) an 80-93-word
#: "which topic do you mean?" (3% of 3,000) was told "It is not finished:
#: continue it with about 2,900 more words" and the extension appended "I
#: cannot fulfill this request..."; an outline of 1,248-1,397 words (12-14%
#: of 10,000) got 3,800-4,200 more words under it. Genuine shortfalls
#: measured live sat at 82-89%.
_TARGET_FLOOR = 0.25
#: Below this a target changes nothing. It is shape_for's long-form
#: threshold: a short piece that ends early is finished, and a segment
#: appended after its ending does more harm than the shortfall.
_TARGET_MIN_WORDS = 800
#: Past the high mark with no line break in sight, stop anyway after this
#: many characters (a paragraph-free wall of text has no better place).
_TARGET_OVERRUN_CHARS = 2000
#: THE FIRST CALL IS TOLD THE LENGTH TOO (2026-09-19). With the count only
#: in the person's words, "Write a 3,000-word article" came back at 2,435-
#: 3,474 words over 15 live Fast runs, 4 of them under 2,700; the 2,435-word
#: one had already written its conclusion, where the one extra segment may
#: not follow (`_has_ended`). The plan's numbers are computed here, never by
#: the model. The model keeps the section COUNT it is given but not the
#: words per section: told 7 sections of about 430 words, four live runs
#: wrote 7-8 sections of 309-379 words (2,358-2,653 words for 3,000). So the
#: count is sized by the words per section it actually writes.
_SECTION_WORDS = 340
_MAX_SECTIONS = 30

_TOKEN = re.compile(r"\S+")
_ALNUM = re.compile(r"[^\W_]")
#: A run of three or more backticks opens or closes code (a fence, or an
#: inline span that crosses lines); an odd count so far means code is open.
_TICKS_OR_NEWLINE = re.compile(r"`{3,}|\n")
#: A piece that has ENDED: its last heading closes it, or its last paragraph
#: opens with a closing phrase. The one extra segment is never granted after
#: one — live (2026-09-18) it fired twice at 82-83% of the target, both times
#: after "## Conclusion", and appended new body sections under the ending and
#: a second conclusion that repeated the first one's sentence verbatim.
_CLOSING_HEADING = re.compile(
    r"^\W*(?:\d+[.)]?\s*)?(?:conclusions?|concluding\b|final\s+(?:thoughts|words|reflections?|remarks|notes)|summary\b|"
    r"in\s+summary|in\s+closing|closing\b|wrap(?:ping)?[- ]?up|(?:key\s+)?takeaways?|epilogue|afterword|the\s+end\b|"
    r"looking\s+(?:ahead|forward)|recap\b|final\s+word)",
    re.IGNORECASE,
)
_HEADING_LINE = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]+(.+?)[ \t#]*|\*\*([^*\n]{1,120})\*\*[ \t:]*)$", re.MULTILINE)
_CLOSING_PARAGRAPH = re.compile(
    r"^\W*(?:in\s+conclusion|to\s+conclude|in\s+summary|in\s+closing|to\s+sum\s+up|to\s+summari[sz]e|all\s+in\s+all|"
    r"the\s+end\b)",
    re.IGNORECASE,
)
#: How far back the ending is looked for: a conclusion section is rarely
#: longer, and a heading further back than this is not the last section.
_ENDING_SCAN_CHARS = 20_000


#: The extension answering the length note instead of writing: "I cannot
#: continue the previous response because the previous response was a
#: complete..." and "I cannot fulfill the request to generate 19,356 words"
#: (QA r1 live, 4 runs). Read on the held opening of that segment only.
_DECLINES = re.compile(
    r"^\W*(?:i\s+(?:cannot|can\s*not|can['’]t|am\s+unable|am\s+not\s+able|won['’]t|will\s+not|apologi[sz]e)\b"
    r"|i['’]m\s+(?:sorry|unable|not\s+able)\b|sorry\b|unfortunately\b|as\s+an\s+ai\b"
    r"|the\s+(?:previous|above|preceding)\s+(?:text|response|piece|article|essay|answer|reply)\s+(?:is|was)\s+(?:already\s+)?complete)",
    re.IGNORECASE,
)


def _has_ended(text: str) -> bool:
    """Does `text` end with its own conclusion (see `_CLOSING_HEADING`)?"""
    tail = text[-_ENDING_SCAN_CHARS:].rstrip()
    if not tail:
        return False
    last = None
    for last in _HEADING_LINE.finditer(tail):
        pass
    if last is not None and _CLOSING_HEADING.match(last.group(1) or last.group(2) or ""):
        return True
    paragraph = tail[tail.rfind("\n\n") + 2:] if "\n\n" in tail else tail
    return bool(_CLOSING_PARAGRAPH.match(paragraph))

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


class StopGeneration(Exception):
    """Raised by an `on_delta` consumer to end the run ON PURPOSE.

    Not a failure: the run ends with `reason` as its stop reason, nothing is
    logged as an error, and the upstream stream is closed by the same
    `finally` that closes it when a user presses Stop. Whatever the consumer
    was handed before raising stays in `LongResult.text`; what the person
    actually saw is the consumer's business (engines/chat.py's loop guard
    keeps its own copy).
    """

    def __init__(self, reason: str = STOP_REPETITION) -> None:
        super().__init__(reason)
        self.reason = reason


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
    #: The length target this run was held to, and the words it wrote (a
    #: token with a letter or digit in it). Both None when there was none.
    target_words: Optional[int] = None
    words: Optional[int] = None

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def note(self) -> Optional[str]:
        """The sentence for a run with a length target that stopped on a
        budget: how long the answer is against what was asked for."""
        if self.stop_reason != STOP_BUDGET or self.target_words is None or self.words is None:
            return None
        return f"This answer stops at {self.words:,} words; about {self.target_words:,} were asked for."

    def as_meta(self) -> Dict[str, Any]:
        """What the UI and the stored message are told. Small and closed.
        The length keys appear only when a target was set, so a run without
        one reports exactly the four keys it always did."""
        meta: Dict[str, Any] = {
            "segments": self.segment_count,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
            "truncated": self.truncated,
        }
        if self.target_words is not None:
            meta["target_words"] = self.target_words
            meta["words"] = self.words
            if self.note:
                meta["note"] = self.note
        return meta


class _WordGauge:
    """Words emitted so far, counted as they go out — never by re-reading the
    text, which is millions of characters on a long run. A word is a token
    with a letter or digit in it, so a table pipe or a `---` rule is not one;
    a word split across two deltas is counted once."""

    def __init__(self, target: int) -> None:
        self.target = target
        self.high = math.ceil(target * _TARGET_HIGH)
        self.words = 0
        self.over = False
        #: Characters emitted since `over` was set.
        self.since_over = 0
        self._in_word = False
        self._counted = False
        #: Inside code opened by a backtick run (the overshoot cut must not
        #: end in it), and trailing backticks a split delta may continue.
        self.fence_open = False
        self._ticks = ""

    @staticmethod
    def _fence_after(text: str, fence_open: bool, ticks: str) -> Tuple[bool, str, int]:
        """(code open, backticks carried) after `text`, and the index in
        `text` just past its first newline at which no code is open (-1 if
        none). Backticks at the end are carried, not counted, until the run
        is known to be over."""
        joined = ticks + text
        body = joined.rstrip("`")
        cut = -1
        for m in _TICKS_OR_NEWLINE.finditer(body):
            if m.group(0) != "\n":
                fence_open = not fence_open
            elif cut < 0 and not fence_open:
                cut = m.end() - len(ticks)
        return fence_open, joined[len(body):], cut

    def cut_point(self, text: str) -> int:
        """Where the overshoot may end inside `text`: just past the first line
        break outside code, or -1."""
        return self._fence_after(text, self.fence_open, self._ticks)[2]

    def feed(self, text: str) -> None:
        if not text:
            return
        if self.over:
            self.since_over += len(text)
        self.fence_open, self._ticks, _ = self._fence_after(text, self.fence_open, self._ticks)
        for m in _TOKEN.finditer(text):
            has = _ALNUM.search(m.group(0)) is not None
            if m.start() == 0 and self._in_word:
                if has and not self._counted:
                    self.words += 1
                    self._counted = True
            else:
                self._counted = has
                self.words += int(has)
        self._in_word = not text[-1].isspace()
        if not self.over and self.words >= self.high:
            self.over = True

    def note(self, *, extending: bool) -> str:
        """What the next continuation is told, in numbers this code computed."""
        words, target = self.words, self.target
        if extending:
            return (f"Length: the piece above stops at {words:,} words, but about {target:,} were asked for. It is "
                    f"not finished: continue it with about {target - words:,} more words of new material that "
                    "deepens what is there, then end it.")
        if words >= target:
            return (f"Length: {words:,} words are written, and about {target:,} were asked for. Finish the part "
                    "in progress and end the piece.")
        return (f"Length: {words:,} of the {target:,} words asked for are written, so about {target - words:,} "
                f"remain. Pace what is left to end near {target:,} words.")


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

    BOTH the first 160 characters and the next 160 must already appear. The
    opening alone stopped a table of 880 near-identical rules at 320 codes
    (2026-09-18): a continuation that begins mid-row opens with the rule text
    every row shares, while the next row's code is new. A real cycle repeats
    past its first line; a candidate with nothing after its opening is judged
    on the opening, as before.
    """
    probe = " ".join(candidate.split())
    first, second = probe[:160], probe[160:320]
    if len(first) < 80:
        return False
    hay = " ".join(produced.split())
    return first in hay and (not second or second in hay)


def _length_plan(target: int) -> str:
    """What the FIRST call is told about a length target, in numbers this
    code computed (see `_SECTION_WORDS`)."""
    sections = max(3, min(_MAX_SECTIONS, round(target / _SECTION_WORDS)))
    per = int(round(target / sections, -1))
    return (f"Length: about {target:,} words were asked for. Plan about {sections} sections of about {per:,} words "
            f"each, counting the introduction and the conclusion, and give every section that depth: the piece "
            f"should reach about {target:,} words before it ends.")


def _first_messages(base: Sequence[dict], target: Optional[int]) -> Sequence[dict]:
    """The first call's prompt: `base` unchanged without a target (the same
    object, so a caller without one sends what it always sent), else the
    length plan appended to the person's own message. Only a plain-text last
    user turn carries it; any other shape is sent as it is."""
    if target is None or not base:
        return base
    last = base[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        return base
    return [*base[:-1], {**last, "content": last["content"] + "\n\n" + _length_plan(target)}]


def _continuation_messages(
    base: Sequence[dict], produced: str, tail_chars: int, note: str = ""
) -> List[dict]:
    """The next call's prompt: the original request, a tail, an instruction.

    The tail is passed as an ASSISTANT turn rather than pasted into a user
    message, so the model reads it as its own writing to be continued instead
    of as material to comment on. `note` (the length target's numbers) is
    appended only when a target was set.
    """
    tail = produced[-tail_chars:]
    instruction = CONTINUE_INSTRUCTION
    covered = _outline(produced)
    if covered:
        instruction += (
            "\n\nSections already written (do not write any of these again):\n"
            + "\n".join(covered)
        )
    if note:
        instruction += "\n\n" + note
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
    answer_plan: Optional[Any] = None,
    target_words: Optional[int] = None,
) -> LongResult:
    """Produce one text across as many calls as the budget allows.

    `on_delta(kind, text)` receives the same (kind, delta) pairs
    `llm.stream_chat_events` yields, so a caller streams a long answer exactly
    as it streams a short one — the seams are not visible to the reader and no
    marker is emitted between segments.

    `on_segment` is called after every completed segment with the result so
    far. That is the checkpoint hook: it is where a caller persists state so a
    six-hour run survives a restart.

    `answer_plan` (chat's per-call sampling, see llm.stream_chat_events) is
    handed to every segment's call unchanged, and not passed at all when None,
    so a caller without one sends exactly what it always sent.

    `target_words` (answer_sampling.requested_words) is the length the person
    asked for. A normal stop at 25-70% of it, on a piece that has not reached
    its own conclusion, gets ONE more segment (dropped if it declines); past 130%
    the run stops at the next line break with `STOP_BUDGET` and a `note`
    saying how long the answer is; the first call is told a section plan
    (`_length_plan`) and every continuation the counts.
    None, or a target under `_TARGET_MIN_WORDS`, sends exactly what a call
    without it sends.
    """
    plan_kwargs = {} if answer_plan is None else {"answer_plan": answer_plan}
    target = int(target_words) if target_words and target_words >= _TARGET_MIN_WORDS else None
    gauge = _WordGauge(target) if target is not None else None
    #: The one extra segment a short normal stop may get has been used, and
    #: whether the NEXT segment is that one (its prompt says so).
    extended = False
    extending = False
    #: The index of that segment. The answer before it was COMPLETE, so if
    #: it adds nothing (it repeats, or the call fails before a token) the run
    #: is reported complete: QA 2026-09-18 saw a finished 1,800-word answer
    #: reported as "it had begun repeating itself" because the optional
    #: segment reopened the piece.
    ext_index = -1
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
            target_words=target,
            words=None if gauge is None else gauge.words,
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

        if index == 0:
            prompt = _first_messages(base, target)
        elif gauge is None:
            prompt = _continuation_messages(base, produced, tail)
        else:
            prompt = _continuation_messages(base, produced, tail, gauge.note(extending=extending))
            extending = False
        seg_started = time.monotonic()
        seg_chars_before = len(produced)
        previous_tail = produced[-tail:]
        seg_tokens_before = _completion_tokens()
        #: Set when the consumer raised StopGeneration during this segment.
        halted: Optional[str] = None

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
        #: The optional extra segment declined instead of writing (`_DECLINES`).
        declined = False
        # Text produced but NOT yet emitted: everything after the last
        # whitespace. See 2b in the module docstring — the trailing partial
        # word is held so a continuable segment can drop it.
        pending = ""

        async def _emit(text: str) -> None:
            """Hand `text` to the reader and count it. Past the length
            target's high mark the run ends at the next line break (or after
            `_TARGET_OVERRUN_CHARS` with none), via StopGeneration, exactly as
            the loop guard ends it. With no target this is the two lines every
            emission site always ran."""
            nonlocal produced
            if gauge is not None and gauge.over:
                # A line break outside a code fence: a cut inside one left
                # the fence open and the rest of the page rendered as code
                # (QA 2026-09-18). Past the overrun bound, the last line break
                # in sight, and the fence is closed by hand.
                cut = gauge.cut_point(text)
                if cut < 0 and gauge.since_over + len(text) >= _TARGET_OVERRUN_CHARS:
                    cut = text.rfind("\n") + 1 or len(text)
                if cut >= 0:
                    text = text[:cut]
                    produced += text
                    gauge.feed(text)
                    await on_delta("token", text)
                    if gauge.fence_open:
                        closer = ("" if produced.endswith("\n") else "\n") + "```\n"
                        produced += closer
                        gauge.feed(closer)
                        await on_delta("token", closer)
                    raise StopGeneration(STOP_BUDGET)
            produced += text
            await on_delta("token", text)
            if gauge is not None:
                gauge.feed(text)

        async def _push(text: str) -> None:
            """Emit up to the last word boundary; hold the rest.

            Only when a continuation is POSSIBLE. When the budget allows one
            call and no more there is no seam to protect, so every delta goes
            straight out and the ordinary path streams exactly as it did
            before this file existed — which is what the Fast effort is.
            """
            nonlocal pending
            if not text:
                return
            if not may_continue:
                await _emit(text)
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
                await _emit(emit_now)

        async def _release() -> None:
            """Decide the seam on the held opening, then let it go."""
            nonlocal holding, head, head_len, stripped, repeated, declined
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
            if index == ext_index:
                # The extra segment follows a piece that ENDED, usually on a
                # full stop with no whitespace after it: glued on, it read
                # "connection.The psychological impact" and "Infrastructure.###
                # Chapter 9", a heading that no longer renders (QA r1 live, 4
                # of 4 extensions). It is new material, so a paragraph break.
                body = text.lstrip()
                if not body:
                    return
                if _DECLINES.match(body):
                    declined = True
                    return
                newlines = len(produced) - len(produced.rstrip("\n"))
                text = "\n" * max(0, 2 - newlines) + body
            elif produced.endswith((" ", "\n", "\t")):
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
            **plan_kwargs,
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
                    if repeated or declined:
                        break
                    continue
                await _push(delta)
        except StopGeneration as halt:
            halted = halt.reason
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            errors.append(f"segment {index}: {type(exc).__name__}: {exc}")
            log.exception("long generation failed in segment %d", index)
            # Everything already written stays. A partial answer beats none —
            # but NONE is not a partial answer. When the first segment dies
            # before a single token (the model was unreachable for the whole
            # recovery window, say) the failure must reach the worker, whose
            # _failure_sentence tells the person MODEL_UNAVAILABLE; swallowing
            # it here produced an empty "successful" answer with no error
            # event at all (seen live 2026-09-11 during a router restart).
            if index == 0:
                if not produced and not pending:
                    raise
                return _result(STOP_ERROR)
            # The optional extra segment failed before adding a word: the
            # answer is the complete one it followed (the error stays in
            # `errors`).
            stop = STOP_COMPLETE if index == ext_index and len(produced) == seg_chars_before else STOP_ERROR
            break
        finally:
            # Breaking out of an `async for` does NOT close the generator, and
            # an unclosed one holds its HTTP response until it is collected.
            with contextlib.suppress(Exception):
                await stream.aclose()

        reason = llm.get_finish_reason()
        if halted is None:
            try:
                # A segment shorter than the hold never reached the release above.
                if holding and not repeated and not declined:
                    await _release()

                # The held fragment is a real ending only when nothing follows it. If
                # this segment is going to be continued, the fragment is an
                # interrupted word and the next call rewrites it from a clean
                # boundary — so it is dropped rather than shown.
                if pending and reason not in _CONTINUABLE:
                    await _emit(pending)
                    pending = ""
            except StopGeneration as halt:
                halted = halt.reason
        produced_here = produced[seg_chars_before:]

        if halted is not None:
            # The consumer ended the run (e.g. the answer loop guard). The
            # segment is recorded as far as it got; no further call is made.
            segs.append(
                Segment(index, len(produced_here), None, reason, stripped, time.monotonic() - seg_started)
            )
            stop = halted
            break

        if declined:
            # Nothing of it was shown: the answer is the complete one before it.
            log.info("long generation: the length extension (segment %d) declined; kept the complete answer", index)
            segs.append(Segment(index, 0, None, reason, stripped, time.monotonic() - seg_started))
            stop = STOP_COMPLETE
            break

        if repeated:
            log.warning(
                "long generation stopped: segment %d repeats text already written",
                index,
            )
            segs.append(
                Segment(index, 0, None, reason, stripped, time.monotonic() - seg_started)
            )
            # A repeat is caught before anything of its segment is shown, so
            # an optional extra segment that repeats leaves the complete answer.
            stop = STOP_COMPLETE if index == ext_index and len(produced) == seg_chars_before else STOP_REPETITION
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
            # "stop", "tool_calls", or nothing reported. The model is done —
            # unless it stopped well short of the length asked for, which
            # earns ONE more segment, told the numbers (`_WordGauge.note`).
            if (gauge is not None and not extended and may_continue and index + 1 < segments_cap
                    and _TARGET_FLOOR * gauge.target <= gauge.words < _TARGET_LOW * gauge.target
                    and total_cap - _used_tokens(measured_start, produced) > settings.continuation_min_segment_tokens
                    and not (deadline_s is not None and time.monotonic() - started >= deadline_s)
                    and not _has_ended(produced)):
                extended = extending = True
                ext_index = index + 1
                continue
            stop = STOP_COMPLETE
            break
        if gauge is not None and gauge.over:
            # Past the high mark at a seam: no further call.
            stop = STOP_BUDGET
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
