"""Long-form output: many bounded calls, one text.

THE TESTS ARE ABOUT STOPPING, not about generating. Continuing is three lines
of loop; a loop that will not stop is six hours of the only GPU in the
building. So most of what follows drives the engine into a failure mode and
asserts it ends, ends for the right recorded reason, and keeps every word it
had already produced.

The model is faked throughout. What is real is the contract with `llm.py`:
`stream_chat_events` yields (kind, delta) and `get_finish_reason()` says why
the LAST call ended. `FakeModel` implements exactly that pair, so a change to
either side of it breaks these tests rather than production.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import pytest

from app import continuation, llm
from app.continuation import (
    STOP_BUDGET,
    STOP_COMPLETE,
    STOP_DEADLINE,
    STOP_ERROR,
    STOP_NO_PROGRESS,
    STOP_REPETITION,
    STOP_SEGMENTS,
    STOP_WALL_CLOCK,
    _strip_overlap,
    stream_long_completion,
)


class FakeModel:
    """A scripted stand-in for `llm.stream_chat_events` + `get_finish_reason`.

    `script` is a list of (text, finish_reason). Each call consumes one entry;
    past the end it repeats the last, which is what makes runaway-loop tests
    possible. `text` may be a callable of the call index — a model that never
    stops still writes something NEW each time, and feeding identical text
    would trip the overlap and repetition guards instead of the one under
    test. It may also be an Exception, which is raised.
    """

    def __init__(
        self,
        script: List[Tuple[str, Optional[str]]],
        *,
        usage: bool = True,
        reasoning: Optional[str] = None,
    ):
        self.script = script
        self.reasoning = reasoning
        self.calls = 0
        self.prompts: List[List[dict]] = []
        self.asked_max_tokens: List[Optional[int]] = []
        self.reason: Optional[str] = None
        self._usage = usage
        self._completion = 0

    async def stream(self, messages, **kwargs):
        entry = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        self.prompts.append([dict(m) for m in messages])
        self.asked_max_tokens.append(kwargs.get("max_tokens"))
        text, reason = entry
        if isinstance(text, Exception):
            raise text
        if callable(text):
            text = text(self.calls - 1)
        if self.reasoning and self.calls == 1:
            yield "reasoning", self.reasoning
        # Deltas arrive in pieces, as they really do.
        for i in range(0, len(text), 7):
            yield "token", text[i : i + 7]
        self.reason = reason
        self._completion += max(1, len(text) // 4)

    def finish_reason(self) -> Optional[str]:
        return self.reason

    def usage(self):
        return {"completion_tokens": self._completion, "prompt_tokens": 0, "calls": self.calls} if self._usage else None


@pytest.fixture()
def model(monkeypatch):
    """Install a FakeModel; the test fills in the script."""

    def _install(script, *, usage: bool = True, reasoning: Optional[str] = None) -> FakeModel:
        fake = FakeModel(script, usage=usage, reasoning=reasoning)
        monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
        monkeypatch.setattr(llm, "get_finish_reason", fake.finish_reason)
        monkeypatch.setattr(llm, "get_usage", fake.usage)
        return fake

    return _install


def collect(**kwargs):
    """Run a generation, returning (result, streamed_text).

    Sync, wrapping asyncio.run — the convention this suite already uses, so
    no async plugin is needed for one module.
    """
    seen: List[str] = []

    async def on_delta(kind: str, text: str) -> None:
        if kind == "token":
            seen.append(text)

    kwargs.setdefault("messages", [{"role": "user", "content": "write something"}])
    messages = kwargs.pop("messages")
    result = asyncio.run(stream_long_completion(messages, on_delta=on_delta, **kwargs))
    return result, "".join(seen)


def endless(index: int) -> str:
    """Novel prose every round — a model with plenty left to say."""
    return f"Paragraph {index} explores another distinct facet of the subject. " * 4


# ---------------------------------------------------------------------------
# When to continue, and when not to
# ---------------------------------------------------------------------------


def test_a_finished_answer_is_not_continued(model):
    fake = model([("All done.", "stop")])
    result, streamed = collect(total_max_tokens=1_000_000)
    assert fake.calls == 1, "the model said it was finished"
    assert result.stop_reason == STOP_COMPLETE
    assert result.truncated is False
    assert result.text == streamed == "All done."


def test_a_truncated_answer_is_continued_into_one_text(model):
    fake = model([("First half. ", "length"), ("Second half.", "stop")])
    result, streamed = collect(total_max_tokens=1_000_000)
    assert fake.calls == 2
    assert result.text == "First half. Second half."
    # The seam is invisible: the caller streamed one continuous answer and no
    # marker was emitted between segments.
    assert streamed == result.text
    assert result.stop_reason == STOP_COMPLETE
    assert result.truncated is False
    assert result.segment_count == 2


def test_an_unreported_finish_reason_is_treated_as_finished(model):
    """None means NOT REPORTED. Guessing "length" there loops forever, so the
    safe reading is "done" — a short answer beats an unbounded one."""
    fake = model([("Something.", None)])
    result, _ = collect(total_max_tokens=1_000_000)
    assert fake.calls == 1
    assert result.stop_reason == STOP_COMPLETE


def test_our_own_wall_clock_is_not_a_reason_to_continue(model):
    """`length` means the model had more to say. `wall_clock` means we pulled
    the plug — going round again just hits the same guard."""
    fake = model([("Cut off. ", llm.WALL_CLOCK_FINISH)])
    result, _ = collect(total_max_tokens=1_000_000)
    assert fake.calls == 1
    assert result.stop_reason == STOP_WALL_CLOCK
    assert result.truncated is True


# ---------------------------------------------------------------------------
# Streaming granularity
#
# The first version of this engine buffered a whole segment so it could strip
# the seam, which delivered the answer in one lump per call — the exact thing
# streaming exists to prevent. It reached the chat tests as "one token event
# instead of two". These pin the shape so it cannot come back.
# ---------------------------------------------------------------------------


def _deltas(**kwargs):
    """Every (kind, text) the caller received, in order."""
    seen = []

    async def on_delta(kind, text):
        seen.append((kind, text))

    kwargs.setdefault("messages", [{"role": "user", "content": "go"}])
    messages = kwargs.pop("messages")
    asyncio.run(stream_long_completion(messages, on_delta=on_delta, **kwargs))
    return seen


def test_an_answer_that_cannot_be_continued_streams_delta_by_delta(model):
    """The Fast path — budget equal to one segment — must behave EXACTLY as it
    did before this engine existed: every delta forwarded as it arrives,
    nothing held back for a seam that cannot occur."""
    model([("abcdefghijklmn", "stop")])          # 14 chars, 7 per delta
    seen = _deltas(total_max_tokens=8_192, segment_max_tokens=8_192)
    assert seen == [("token", "abcdefg"), ("token", "hijklmn")]


def test_a_continuable_answer_streams_in_words_and_loses_nothing(model):
    """Where a seam IS possible the stream is held to word boundaries, which
    costs one word of latency and buys a clean join. Nothing is dropped: the
    final fragment is released once the model says it has finished."""
    model([("the quick brown fox jumps", "stop")])
    seen = _deltas(total_max_tokens=1_000_000, segment_max_tokens=8_192)
    assert "".join(t for _, t in seen) == "the quick brown fox jumps"
    assert len(seen) > 1, "still streamed, not delivered in one lump"
    # Every piece but the last ends on a boundary.
    assert all(t.endswith((" ", "\n", "\t")) for _, t in seen[:-1])


def test_the_interrupted_word_at_a_seam_is_never_shown(model):
    """The artifact this exists for, measured against the real model:
    "...to narrow" + "the results" rendered as "narrowthe results". The
    partial word is dropped and the next call rewrites it from a boundary."""
    fake = model([("A sentence that was cut off while trying to narrow", "length"),
                  ("narrow the results down to what matters most here.", "stop")])
    result, streamed = collect(total_max_tokens=1_000_000)
    assert "narrowthe" not in result.text
    assert "narrow the results" in result.text
    assert streamed == result.text
    # What the model was shown ends on a boundary, not mid-word.
    assert fake.prompts[1][-2]["content"].endswith(" ")


def test_reasoning_is_forwarded_immediately_and_is_never_text(model):
    model([("answer", "stop")], reasoning="thinking...")
    seen = _deltas(total_max_tokens=1_000)
    assert seen == [("reasoning", "thinking..."), ("token", "answer")]


def test_a_continuation_holds_back_only_its_opening(model):
    """The seam needs deciding before it is shown; the rest of the segment
    must not wait for it."""
    long_second = "X" * 200 + "".join(f" word{i}" for i in range(300))
    model([("An opening paragraph with real substance to it. ", "length"),
           (long_second, "stop")])
    seen = _deltas(total_max_tokens=1_000_000)
    tokens = [t for k, t in seen if k == "token"]
    # The first segment streamed in 7-char pieces...
    assert len([t for t in tokens if len(t) <= 7]) > 5
    # ...and the second did too, after one larger opening chunk.
    assert len(tokens) > 20, f"the continuation arrived in {len(tokens)} pieces"


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_strip_overlap_removes_a_re_emitted_opening():
    assert _strip_overlap("...the quick brown fox jumps", "the quick brown fox jumps over") == " over"


def test_strip_overlap_leaves_an_honest_continuation_alone():
    assert _strip_overlap("...ending here.", " And then something new entirely.") == (
        " And then something new entirely."
    )


def test_strip_overlap_ignores_coincidental_short_matches():
    # "The " is not a repeat; stripping it would eat real text.
    assert _strip_overlap("something. The ", "The next section begins") == "The next section begins"


def test_a_repeated_seam_never_reaches_the_reader(model):
    tail = "This sentence is long enough to be matched at the seam by the stripper."
    fake = model([(tail, "length"), (tail + " Then new material.", "stop")])
    result, streamed = collect(total_max_tokens=1_000_000)
    assert result.text == tail + " Then new material."
    assert result.text.count(tail) == 1, "the duplicated opening was stripped"
    assert streamed == result.text
    assert result.segments[1].overlap_stripped > 0


# ---------------------------------------------------------------------------
# Every way a run must be able to end
# ---------------------------------------------------------------------------


def test_the_budget_stops_the_run(model):
    # Never says "stop"; only the budget can end this.
    fake = model([(endless, "length")])
    result, _ = collect(total_max_tokens=2_000, segment_max_tokens=500, max_segments=200)
    assert result.stop_reason == STOP_BUDGET
    assert result.truncated is True
    assert result.text, "everything already written is kept"
    assert fake.calls < 200, "the budget stopped it, not the segment cap"


def test_the_budget_still_binds_when_the_server_reports_no_usage(model):
    """A budget that stops applying whenever telemetry is missing is not a
    budget — `stream_options` is an extension a runtime may refuse."""
    fake = model([(endless, "length")], usage=False)
    result, _ = collect(total_max_tokens=2_000, segment_max_tokens=500, max_segments=200)
    assert result.stop_reason == STOP_BUDGET
    # Estimated for the guard, but never REPORTED as a count.
    assert result.output_tokens is None


def test_the_segment_cap_is_a_backstop(model):
    fake = model([(endless, "length")])
    result, _ = collect(total_max_tokens=10_000_000, max_segments=5)
    assert result.stop_reason == STOP_SEGMENTS
    assert fake.calls == 5


def test_a_model_that_stops_producing_ends_the_run(model):
    """Two content-free continuations in a row is a model with nothing left,
    however cheerfully it keeps saying `length`."""
    opening = "A real opening paragraph with enough substance to count as progress. "
    fake = model([(opening, "length"), (".", "length")])
    result, _ = collect(total_max_tokens=1_000_000)
    assert result.stop_reason == STOP_NO_PROGRESS
    assert fake.calls == 3, "one real segment, then two that produced nothing"
    assert result.text.startswith(opening), "the real paragraph is kept"


def test_a_model_that_loops_back_is_caught(model):
    """The failure `_strip_overlap` cannot catch: not a repeated seam but a
    jump backwards to text written earlier."""
    body = (
        "Section one covers the architecture of the system in enough detail "
        "that this sentence is comfortably longer than the probe. "
    )
    fake = model([(body + "And more. ", "length"), (body, "length")])
    result, _ = collect(total_max_tokens=1_000_000)
    assert result.stop_reason == STOP_REPETITION
    assert result.text.count("Section one covers") == 1


def test_the_deadline_stops_the_run(model, monkeypatch):
    fake = model([(endless, "length")])
    result, _ = collect(total_max_tokens=1_000_000, deadline_s=-1)
    assert result.stop_reason == STOP_DEADLINE
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def test_a_failure_in_the_first_segment_produces_nothing_but_says_so(model):
    fake = model([(RuntimeError("engine died"), None)])
    result, _ = collect(total_max_tokens=1_000_000)
    assert result.stop_reason == STOP_ERROR
    assert result.text == ""
    assert result.errors and "engine died" in result.errors[0]


def test_a_failure_midway_keeps_everything_already_written(model):
    """A partial answer beats none. The user has already SEEN this text."""
    fake = model([("A solid opening paragraph. ", "length"), (RuntimeError("boom"), None)])
    result, streamed = collect(total_max_tokens=1_000_000)
    assert result.stop_reason == STOP_ERROR
    assert result.text == "A solid opening paragraph. "
    assert streamed == result.text
    assert result.truncated is True
    assert any("boom" in e for e in result.errors)


# ---------------------------------------------------------------------------
# What the continuation actually asks for
# ---------------------------------------------------------------------------


def test_a_continuation_carries_the_tail_and_the_instruction(model):
    fake = model([("The opening of a long piece. ", "length"), ("The end.", "stop")])
    collect(total_max_tokens=1_000_000)

    second = fake.prompts[1]
    assert second[0] == {"role": "user", "content": "write something"}, (
        "the original request is still there"
    )
    assert second[-2]["role"] == "assistant", "the tail is the model's own writing"
    assert "The opening of a long piece." in second[-2]["content"]
    assert second[-1]["role"] == "user"
    assert "exactly where it stops" in second[-1]["content"]
    assert "Do not repeat" in second[-1]["content"]


def test_the_prompt_does_not_grow_with_the_text(model):
    """The whole reason a tail exists. If every segment fed back everything,
    segment 50 would be a 400,000-token prefill."""
    fake = model([("word " * 400, "length")])
    collect(total_max_tokens=1_000_000, max_segments=6, tail_chars=500)
    sizes = [sum(len(m["content"]) for m in p) for p in fake.prompts[1:]]
    assert max(sizes) - min(sizes) < 400, f"prompt grew across segments: {sizes}"


def test_headings_already_written_are_named_so_they_are_not_rewritten(model):
    text = "# Overview\nSome prose.\n\n## Architecture\nMore prose. "
    fake = model([(text, "length"), ("done", "stop")])
    collect(total_max_tokens=1_000_000, tail_chars=20)
    instruction = fake.prompts[1][-1]["content"]
    # The tail is far too short to carry them, which is exactly the case the
    # outline exists for.
    assert "# Overview" in instruction
    assert "## Architecture" in instruction
    assert "do not write any of these again" in instruction


def test_a_segment_never_asks_for_more_than_the_budget_has_left(model):
    fake = model([("x" * 100, "length")])
    collect(total_max_tokens=3_000, segment_max_tokens=2_000, max_segments=10)
    assert max(fake.asked_max_tokens) <= 2_000
    assert all(a is not None and a > 0 for a in fake.asked_max_tokens)


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------


def test_every_segment_offers_a_checkpoint(model):
    a = "The first substantial paragraph of the piece, long enough to count. "
    b = "The second substantial paragraph, continuing the same argument well. "
    c = "The third and final paragraph, which concludes the whole thing here."
    fake = model([(a, "length"), (b, "length"), (c, "stop")])
    seen: List[str] = []

    async def on_delta(kind, text):
        pass

    async def on_segment(result):
        seen.append(result.text)

    asyncio.run(
        stream_long_completion(
            [{"role": "user", "content": "go"}],
            on_delta=on_delta,
            on_segment=on_segment,
            total_max_tokens=1_000_000,
        )
    )
    # A checkpoint after each segment, each carrying everything so far — that
    # is what makes a six-hour run survivable.
    assert seen == [a, a + b, a + b + c]


# ---------------------------------------------------------------------------
# Budgets are a product decision, not a mechanism
# ---------------------------------------------------------------------------


def test_effort_decides_how_long_a_run_may_be():
    assert continuation.budget_for("fast") == continuation.settings.model_max_output
    assert continuation.budget_for("think") > continuation.budget_for("fast")
    assert continuation.budget_for("max") == continuation.settings.max_logical_output_tokens
    # Wire aliases resolve the same way the rest of the app resolves them.
    assert continuation.budget_for("extra_high") == continuation.budget_for("max")
    assert continuation.budget_for("low") == continuation.budget_for("fast")


def test_turning_the_feature_off_returns_every_effort_to_one_call(monkeypatch):
    monkeypatch.setattr(continuation.settings, "continuation_enabled", False)
    for effort in ("fast", "think", "max"):
        assert continuation.budget_for(effort) == continuation.settings.model_max_output


# ---------------------------------------------------------------------------
# The foundation: why the last call stopped
#
# Everything above rests on `llm.get_finish_reason()` being the reason the
# MOST RECENT call ended. These test that contract directly, because a stale
# value there does not fail loudly — it loops.
# ---------------------------------------------------------------------------


class _Chunk:
    """The shape the OpenAI SDK yields: choices[0].finish_reason, or none."""

    class _Choice:
        def __init__(self, reason):
            self.finish_reason = reason
            self.delta = None

    def __init__(self, reason=None, *, choices=True):
        self.choices = [self._Choice(reason)] if choices else []
        self.usage = None


def test_the_finish_reason_starts_unknown_and_is_captured():
    llm.reset_finish_reason()
    assert llm.get_finish_reason() is None
    llm._capture_finish(_Chunk("length"))
    assert llm.get_finish_reason() == "length"


def test_the_usage_chunk_carries_no_choices_and_must_not_clear_it():
    """vLLM sends usage LAST, in a chunk with no choices. Reading choices[0]
    there would raise; clearing on it would lose the reason entirely."""
    llm.reset_finish_reason()
    llm._capture_finish(_Chunk("length"))
    llm._capture_finish(_Chunk(choices=False))
    assert llm.get_finish_reason() == "length"


def test_intermediate_chunks_report_nothing_and_leave_it_alone():
    llm.reset_finish_reason()
    llm._capture_finish(_Chunk("length"))
    llm._capture_finish(_Chunk(None))  # every chunk before the last
    assert llm.get_finish_reason() == "length"


def test_it_does_not_accumulate_the_way_usage_does():
    """`_usage` sums across a turn because an operator asks what the TURN
    cost. This is the opposite: a continuation loop asks about one call."""
    llm.reset_finish_reason()
    llm._capture_finish(_Chunk("length"))
    llm._capture_finish(_Chunk("stop"))
    assert llm.get_finish_reason() == "stop"


def test_a_call_that_reports_nothing_cannot_inherit_the_last_one(model):
    """The loop-forever bug this guards: segment 2 dies before the server says
    anything, `get_finish_reason()` still reads segment 1's "length", and the
    engine continues on a call that produced nothing."""
    llm.reset_finish_reason()
    llm._capture_finish(_Chunk("length"))
    llm.reset_finish_reason()
    assert llm.get_finish_reason() is None


def test_our_guards_are_distinguishable_from_the_servers_reasons():
    assert llm.WALL_CLOCK_FINISH not in ("stop", "length", "tool_calls")
    assert llm.WALL_CLOCK_FINISH not in continuation._CONTINUABLE
    assert "length" in continuation._CONTINUABLE


def test_a_checkpoint_never_claims_the_run_finished(model):
    """A snapshot taken mid-run is persisted state. If it said "complete", a
    resume would skip work that was never done."""
    a = "The first substantial paragraph, long enough to count as progress. "
    fake = model([(a, "length"), ("The second and final paragraph here.", "stop")])
    reasons = []

    async def on_delta(kind, text):
        pass

    async def on_segment(result):
        reasons.append(result.stop_reason)

    final = asyncio.run(
        stream_long_completion(
            [{"role": "user", "content": "go"}],
            on_delta=on_delta,
            on_segment=on_segment,
            total_max_tokens=1_000_000,
        )
    )
    assert reasons == [continuation.STOP_RUNNING, continuation.STOP_RUNNING]
    assert final.stop_reason == STOP_COMPLETE, "only the RETURN value is a verdict"


def test_each_segment_reports_what_it_cost(model):
    a = "The first substantial paragraph, long enough to count as progress. "
    b = "The second paragraph, also long enough to be counted as real work. "
    model([(a, "length"), (b, "stop")])
    result, _ = collect(total_max_tokens=1_000_000)
    assert len(result.segments) == 2
    assert all(s.tokens is not None and s.tokens > 0 for s in result.segments)
    assert sum(s.tokens for s in result.segments) == result.output_tokens


def test_an_unmeasured_segment_reports_no_count_rather_than_zero(model):
    model([("A single complete answer.", "stop")], usage=False)
    result, _ = collect(total_max_tokens=1_000)
    assert result.segments[0].tokens is None
    assert result.output_tokens is None


def test_the_stream_is_closed_when_the_engine_breaks_out_of_it(model):
    """Breaking an `async for` leaves the generator suspended, holding its
    HTTP response until it is collected."""
    closed = {"n": 0}

    class Tracking(FakeModel):
        async def stream(self, messages, **kwargs):
            try:
                async for pair in super().stream(messages, **kwargs):
                    yield pair
            finally:
                closed["n"] += 1

    # To reach the REPETITION break the model must jump BACK, not repeat the
    # seam — an exact seam repeat is stripped before the check ever sees it,
    # and the run then ends through the ordinary no-progress path instead.
    opening = "The opening section sets out the architecture in considerable detail. " * 2
    later = "The closing section draws the practical consequences together. " * 2
    fake = Tracking([(opening + later, "length"), (opening, "length")])
    import app.llm as _llm

    seen = []

    async def on_delta(kind, text):
        seen.append(text)

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(_llm, "stream_chat_events", fake.stream)
    mp.setattr(_llm, "get_finish_reason", fake.finish_reason)
    mp.setattr(_llm, "get_usage", fake.usage)
    try:
        result = asyncio.run(
            stream_long_completion(
                [{"role": "user", "content": "go"}],
                on_delta=on_delta,
                total_max_tokens=1_000_000,
            )
        )
    finally:
        mp.undo()
    assert result.stop_reason == STOP_REPETITION, "the run broke out mid-stream"
    assert closed["n"] == fake.calls, "every stream opened was also closed"


def test_a_run_that_starts_with_no_usage_recorded_still_reports_its_tokens(model):
    """`reset_usage()` clears the counter to None, not zero — the normal state
    at the start of a turn. Reading that as "unmeasured" made every run report
    no token count at all while each segment reported one."""
    fake = model([("A first paragraph with genuine substance in it here. ", "length"),
                  ("A second paragraph that finishes the piece off.", "stop")])
    fake._completion = 0
    llm.reset_usage()
    result, _ = collect(total_max_tokens=1_000_000)
    assert result.output_tokens is not None and result.output_tokens > 0
    assert result.output_tokens == sum(s.tokens for s in result.segments)


def test_the_seam_does_not_double_the_space_between_two_words(model):
    """Both sides supply it: the text ends on a boundary, and the model is
    told to begin a new word."""
    fake = model([("A sentence ending on a clean word boundary here and", "length"),
                  (" then continuing straight on from that point.", "stop")])
    result, _ = collect(total_max_tokens=1_000_000)
    assert "  " not in result.text


def test_a_deliberate_paragraph_break_at_a_seam_survives(model):
    """A leading newline is structure, not a stray space."""
    fake = model([("The end of one section, cut off mid", "length"),
                  ("\n\n## The next section starts here properly.", "stop")])
    result, _ = collect(total_max_tokens=1_000_000)
    assert "\n\n## The next section" in result.text


def test_a_stale_finish_reason_cannot_make_the_loop_continue(monkeypatch):
    """The signal is a ContextVar. Anything that supplies the stream without
    going through `stream_chat_events` — a stub, a wrapper, a different
    generation function — leaves the previous value standing, and a stale
    "length" would continue a call that reported nothing at all.

    Found by a real test doing exactly that: it stubbed the generator, the
    engine read another test's "length", continued, and dropped the held word
    at a seam that never existed.
    """
    async def bare_stream(*a, **k):
        async def gen():
            yield "token", "Answered without it."

        return gen()

    calls = {"n": 0}

    async def counting(*a, **k):
        calls["n"] += 1
        async for pair in await bare_stream(*a, **k):
            yield pair

    monkeypatch.setattr(llm, "stream_chat_events", counting)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    llm._finish_reason.set("length")          # left over from an earlier call

    result, streamed = collect(total_max_tokens=1_000_000)
    assert calls["n"] == 1, "it must not continue on a reason nobody reported"
    assert result.text == "Answered without it.", "and the last word is not dropped"
    assert streamed == result.text
    assert result.stop_reason == STOP_COMPLETE
