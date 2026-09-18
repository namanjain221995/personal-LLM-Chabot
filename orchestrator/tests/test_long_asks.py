"""Long asks: a word-count ask reaches chat, the count is read right, the
length is a target, the seam guard stops only real loops, and an over-window
paste leaves room to answer.

Reproduced at 4810da0 (2026-09-18), in-process:

  * "Write a 5,000-word article about the Roman road network." was a file:
    the bare `word` noun in intent.py matched the UNIT of a word count.
  * "Produce a 12,000 word manual" was prose while "12000 word" was longform:
    the size regexes in answer_sampling.py could not cross a comma.
  * "10,000 words" came back as 24,364 words in one run and 5,340 in another:
    stream_long_completion had no idea a length had been asked for.
  * A 35,143-character table was stopped at 320 of 880 codes by the seam
    repeat guard, which fired on the 160-character opening alone.
  * A 5.2 MB paste left 403 tokens for the answer: fit_request stopped
    trimming as soon as 256 tokens of room existed.

Everything here is offline: the model is a scripted fake and /tokenize is a
character counter.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import pytest

from app import context, continuation, llm
from app.artifacts import intent as I
from app.config import settings
from app.core import answer_sampling as S

# ---------------------------------------------------------------- intent --

#: An article, blog post, essay or story WITH a word count is text for the
#: chat, not a file.
ARTICLE_ASKS = [
    "Write a 5,000-word article about the Roman road network.",
    "Write a 5000 word article about the Roman road network.",
    "Write a 1,500 word blog post about sourdough.",
]

#: The person said, in words, that they do not want a file.
NEGATED_CREATION = [
    "Write a 5,000-word article about the Roman road network. Write the whole thing out here in this chat "
    "as your reply - do not create a file.",
    "Write a 5,000-word article about Rome. Don't create a file, just answer inline.",
    # `document` IS a file noun (the decision below), so only the negated
    # creation can take this one to chat.
    "Write a 10,000-word document on the Silk Road. Do not create a file.",
]


@pytest.mark.parametrize("text", ARTICLE_ASKS)
def test_a_word_count_article_is_answered_in_chat(text):
    intent = I.decide(text)
    assert intent.action == "none", (text, intent.rule)


@pytest.mark.parametrize("text", NEGATED_CREATION)
def test_a_negated_file_creation_is_answered_in_chat(text):
    intent = I.decide(text)
    assert (intent.action, intent.rule) == ("none", "chat-only"), text


@pytest.mark.parametrize("text, formats", [
    ("make me a word document about our leave policy", ["docx"]),
    ("create a word file of the onboarding checklist", ["docx"]),
    ("i need a word version of the above", ["docx"]),
    # The aiq big_report case R05: a count AND a named format is still a file.
    ("Write a long-form whitepaper, around 5,000 words, on zero-trust architecture for mid-size companies, as a PDF.",
     ["pdf"]),
])
def test_a_named_file_still_creates(text, formats):
    intent = I.decide(text)
    assert intent.action == "create", (text, intent.rule)
    assert intent.formats == formats


def test_a_word_count_document_stays_a_file():
    """DECISION (2026-09-18): `document` is a file noun, and long documents
    are made as files (5cba009). Only the count's unit stopped being one."""
    assert I.decide("Write a 10,000-word document on the history of Rome.").action == "create"


@pytest.mark.parametrize("text", [
    "Write a 10,000-word document on the Silk Road. Answer in this chat.",
    "Write a 10,000-word document on the Silk Road. Keep it inline.",
])
def test_the_answer_placed_here_is_chat(text):
    assert (I.decide(text).action, I.decide(text).rule) == ("none", "chat-only")


@pytest.mark.parametrize("text", [
    # `inline` and `in this chat` are places for the ANSWER only.
    "Create a PDF report on our Q3 results with inline citations.",
    "Put it inline in the report and make a PDF of it.",
    "Make a PDF of everything we discussed in this chat.",
    "Make a PDF of everything here in this chat.",
    "Put the answer in this chat into a docx.",
    # A negation followed by the request it is replaced with.
    "Don't create a document, make a deck on our hiring plan.",
    # A first-person negation describes the person, not the instruction.
    "I can't create a file myself, could you create a PDF of the plan?",
])
def test_the_chat_only_forms_do_not_swallow_a_request(text):
    assert I.decide(text).action == "create", text


# ----------------------------------------------------------------- shape --

#: The same ask with and without a group separator must get the same shape.
COUNT_PAIRS = [
    ("Produce a 12,000 word manual on onboarding.", "Produce a 12000 word manual on onboarding."),
    ("Give me 1,000 interview questions", "Give me 1000 interview questions"),
    ("I need 5,000 words on the Silk Road", "I need 5000 words on the Silk Road"),
    ("Give me 2,500 facts about space", "Give me 2500 facts about space"),
    ("Share 1,200 tips for new managers", "Share 1200 tips for new managers"),
    ("A 1,500-word explainer on tides, please", "A 1500-word explainer on tides, please"),
    ("Give me 10,000 words on Roman roads", "Give me 10000 words on Roman roads"),
    ("Produce 1,000 prompts for image models", "Produce 1000 prompts for image models"),
    ("Send 3,000 quotes about courage", "Send 3000 quotes about courage"),
    ("Give me 1,000,000 words on tea", "Give me 1000000 words on tea"),
    ("Give me 5 000 words on tea", "Give me 5000 words on tea"),
    ("Give me 5 000 words on tea", "Give me 5000 words on tea"),
]


@pytest.mark.parametrize("grouped, plain", COUNT_PAIRS)
def test_a_grouped_count_reads_like_a_plain_one(grouped, plain):
    assert S.shape_for(grouped) == S.shape_for(plain) == S.SHAPE_LONGFORM


@pytest.mark.parametrize("text, shape", [
    # English-only rule (2026-09-16): Indic digits behave exactly as before —
    # a plain Devanagari count is read, a grouped one is not.
    ("३००० शब्द", S.SHAPE_LONGFORM),
    ("३,००० शब्द", S.SHAPE_PROSE),
    ("૨૦૦૦ શબ્દ", S.SHAPE_LONGFORM),
])
def test_indic_digits_are_unchanged(text, shape):
    assert S.shape_for(text) == shape


@pytest.mark.parametrize("text, words", [
    ("Write a 10,000-word essay", 10000),
    ("Write a 5,000-word article about the Roman road network.", 5000),
    ("Write a 3000 word story about a lighthouse keeper", 3000),
    ("Write an article of about 3,000 words on tea.", 3000),
    ("Can you write at least 2,000 words on the history of tea?", 2000),
    ("Explain the Silk Road in 1500 words.", 1500),
    ("Rewrite this 5,000-word essay into 1,000 words.", 1000),
    ("Write a long-form whitepaper, around 5,000 words, on zero-trust architecture.", 5000),
])
def test_requested_words_reads_the_target(text, words):
    assert S.requested_words(text) == words


@pytest.mark.parametrize("text", [
    "hello",
    "Write an article about tea.",
    # A limit is not a target: a short answer is what was asked for.
    "Summarise this in under 300 words.",
    "Explain it in no more than 500 words.",
    "Keep it to 200 words max.",
    # A size per item is not the size of the answer.
    "Write 5 essays of 500 words each.",
    # A question ABOUT a length asks for no text of that length.
    "How long is a 5,000-word essay?",
    "What is a good structure for a 10,000-word dissertation?",
])
def test_requested_words_is_none_without_a_target(text):
    assert S.requested_words(text) is None


# --------------------------------------------------- the length target ----


class FakeModel:
    """(text, finish_reason) per call, as in test_continuation.py."""

    def __init__(self, script: List[Tuple[str, Optional[str]]]):
        self.script = script
        self.calls = 0
        self.prompts: List[List[dict]] = []
        self.kwargs: List[dict] = []
        self.reason: Optional[str] = None
        self._completion = 0

    async def stream(self, messages, **kwargs):
        text, reason = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        self.prompts.append([dict(m) for m in messages])
        self.kwargs.append(dict(kwargs))
        for i in range(0, len(text), 11):
            yield "token", text[i: i + 11]
        self.reason = reason
        self._completion += max(1, len(text) // 4)

    def finish_reason(self):
        return self.reason

    def usage(self):
        return {"completion_tokens": self._completion, "prompt_tokens": 0, "calls": self.calls}


@pytest.fixture()
def model(monkeypatch):
    def _install(script) -> FakeModel:
        fake = FakeModel(script)
        monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
        monkeypatch.setattr(llm, "get_finish_reason", fake.finish_reason)
        monkeypatch.setattr(llm, "get_usage", fake.usage)
        return fake

    return _install


def run(**kwargs):
    seen: List[str] = []

    async def on_delta(kind, text):
        if kind == "token":
            seen.append(text)

    messages = kwargs.pop("messages", [{"role": "user", "content": "Write a 3,000-word article about tea."}])
    result = asyncio.run(continuation.stream_long_completion(messages, on_delta=on_delta, **kwargs))
    return result, "".join(seen)


def prose(words: int, start: int = 0) -> str:
    """`words` distinct words in paragraphs of 40, so no guard sees a repeat."""
    out = []
    for i in range(start, start + words):
        out.append(f"w{i}")
        out.append("\n\n" if (i + 1) % 40 == 0 else " ")
    return "".join(out)


def count(text: str) -> int:
    return sum(1 for t in text.split() if any(c.isalnum() for c in t))


def test_a_normal_stop_at_60_percent_gets_exactly_one_more_segment(model):
    fake = model([(prose(1800), "stop"), (prose(600, start=1800), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2
    assert result.stop_reason == continuation.STOP_COMPLETE
    assert count(streamed) == 2400
    # The extra segment is told the numbers the code computed.
    note = fake.prompts[1][-1]["content"]
    assert "3,000" in note and "1,800" in note


def test_one_more_segment_is_the_most_a_short_stop_gets(model):
    fake = model([(prose(1800), "stop"), (prose(100, start=1800), "stop"), (prose(100, start=1900), "stop")])
    result, _ = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2
    assert result.stop_reason == continuation.STOP_COMPLETE


def test_a_normal_stop_near_the_target_is_finished(model):
    fake = model([(prose(2700), "stop")])
    result, _ = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 1
    assert result.stop_reason == continuation.STOP_COMPLETE


def test_past_130_percent_the_run_stops_and_says_how_long_it_is(model):
    fake = model([(prose(4200), "length"), (prose(2000, start=4200), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 1, "no continuation past the target"
    assert result.stop_reason == continuation.STOP_BUDGET
    assert result.truncated is True
    # Stopped at the first paragraph break past 3,900 words, never mid-line.
    assert 3900 <= count(streamed) <= 3940
    assert streamed.endswith("\n")
    assert result.words == count(streamed)
    meta = result.as_meta()
    assert meta["target_words"] == 3000 and meta["words"] == result.words
    assert "3,000" in meta["note"] and f"{result.words:,}" in meta["note"]


def test_the_overrun_is_caught_in_a_later_segment_too(model):
    fake = model([(prose(2000), "length"), (prose(3000, start=2000), "length")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2
    assert result.stop_reason == continuation.STOP_BUDGET
    assert 3900 <= count(streamed) <= 3940


def test_a_short_target_changes_nothing(model):
    """Below 800 words (shape_for's long-form threshold) an early stop is a
    finished short piece, and appending after its ending does more harm than
    the shortfall."""
    fake = model([(prose(60), "stop")])
    result, _ = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=200)
    assert fake.calls == 1 and result.stop_reason == continuation.STOP_COMPLETE


def _requests(fake: FakeModel):
    return [(p, k) for p, k in zip(fake.prompts, fake.kwargs)]


def test_no_target_sends_byte_identical_requests(model):
    script = [(prose(300), "length"), (prose(300, start=300), "length"), (prose(50, start=600), "stop")]
    base = [{"role": "user", "content": "Write about tea."}]
    fake_a = model(list(script))
    result_a, text_a = run(messages=list(base), total_max_tokens=1_000_000, segment_max_tokens=500)
    fake_b = model(list(script))
    result_b, text_b = run(messages=list(base), total_max_tokens=1_000_000, segment_max_tokens=500, target_words=None)
    assert _requests(fake_a) == _requests(fake_b)
    assert (text_a, result_a.as_meta()) == (text_b, result_b.as_meta())
    # ...and they are the requests 4810da0 sent: the base prompt, then the
    # tail + the unchanged instruction, with no length note anywhere.
    assert fake_b.prompts[0] == base
    for prompt in fake_b.prompts[1:]:
        assert prompt[:-2] == base
        assert prompt[-1] == {"role": "user", "content": continuation.CONTINUE_INSTRUCTION}
    assert set(result_b.as_meta()) == {"segments", "output_tokens", "stop_reason", "truncated"}


# ------------------------------------------------------- the repeat guard --

_RULE = ("Access to the production database must be reviewed every quarter by the system owner, "
         "and the evidence of the review is stored in the governance tool with a sign-off")


def _rule_table_segments(rows: int = 880, per_segment: int = 40) -> List[Tuple[str, Optional[str]]]:
    """A markdown table of `rows` near-identical rules, cut mid-row at every
    segment boundary — the shape of the 880-code table stopped at 320."""
    table = "| Code | Rule | Status |\n|---|---|---|\n" + "".join(
        f"| C{n:04d} | {_RULE} | {'Active' if n % 3 else 'Draft'} |\n" for n in range(1, rows + 1)
    )
    # Each segment ends right after a row's code cell, so the continuation
    # opens with that row's rule text: the 160 characters every row shares.
    cuts = [table.index(f"| C{n:04d} | ") + len(f"| C{n:04d} | ") for n in range(per_segment + 1, rows + 1, per_segment)]
    bounds = [0, *cuts, len(table)]
    return [(table[a:b], "stop" if b == len(table) else "length") for a, b in zip(bounds, bounds[1:])]


def test_the_guard_lets_a_table_of_similar_rows_finish(model):
    fake = model(_rule_table_segments())
    result, streamed = run(total_max_tokens=10_000_000, segment_max_tokens=8000)
    assert result.stop_reason == continuation.STOP_COMPLETE, result.stop_reason
    for n in range(1, 881):
        assert streamed.count(f"| C{n:04d} |") == 1, n
    assert fake.calls == 22


def test_the_guard_still_stops_a_true_cycle(model):
    rows = "".join(f"| C{n:04d} | {_RULE} | Active |\n" for n in range(1, 21))
    fake = model([(rows, "length"), (rows, "length")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000)
    assert result.stop_reason == continuation.STOP_REPETITION
    assert streamed.count("| C0001 |") == 1


def test_the_guard_needs_both_windows():
    produced = " ".join(f"row {n} " + _RULE for n in range(50))
    seam_restart = _RULE + " and then a genuinely new paragraph about something else entirely, " * 5
    assert continuation._repeats_existing(produced, seam_restart) is False
    assert continuation._repeats_existing(produced, produced[-400:]) is True


# ----------------------------------------------------------- fit_request --


def _char_counter(window: int):
    async def counter(base_url, model, messages):
        return sum(len(m.get("content", "")) // 3 + 4 for m in messages if isinstance(m.get("content"), str)), window

    return counter


def _fit(messages, requested):
    return asyncio.run(context.fit_request(messages, base_url="http://x/v1", model="m", requested_max_tokens=requested))


@pytest.mark.parametrize("requested", [8000, 1_000_000])
def test_an_over_window_paste_leaves_room_to_answer(monkeypatch, requested):
    window = 1_000_000
    monkeypatch.setattr(context, "count_tokens", _char_counter(window))
    paste = "".join(f"line {i}: the quarterly ledger reconciles.\n" for i in range(118_000))  # ~5.3 MB
    assert len(paste) > 5_000_000
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": paste + "\nSummarise the ledger."}]
    sized, max_tokens = _fit(msgs, requested)
    assert max_tokens >= min(requested, context.OVERFLOW_ANSWER_ROOM)
    prompt = sum(len(m["content"]) // 3 + 4 for m in sized)
    assert prompt + max_tokens + settings.context_safety_margin <= window
    assert sized[-1]["content"].endswith("Summarise the ledger.")


def test_overflow_room_is_bounded_by_a_quarter_of_the_window(monkeypatch):
    monkeypatch.setattr(context, "count_tokens", _char_counter(32_768))
    msgs = [{"role": "user", "content": "x " * 200_000}]
    _, max_tokens = _fit(msgs, 16_000)
    assert max_tokens >= 32_768 // 4


def test_a_request_that_fits_is_untouched(monkeypatch):
    monkeypatch.setattr(context, "count_tokens", _char_counter(12_000))
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " * 400} for i in range(6)
    ]
    sized, max_tokens = _fit(msgs, 8000)
    prompt = sum(len(m["content"]) // 3 + 4 for m in msgs)
    # 4810da0's arithmetic, unchanged: the room left, below the ask.
    assert sized == msgs
    assert max_tokens == 12_000 - prompt - settings.context_safety_margin
    assert 256 <= max_tokens < 8000


# -------------------------------------------------------- Salesforce shape --


@pytest.mark.parametrize("text, shape", [
    ("hello", S.SHAPE_PROSE),
    ("thanks!", S.SHAPE_PROSE),
    ("explain what a lead conversion does", S.SHAPE_PROSE),
    ("write a detailed guide to our renewal process", S.SHAPE_LONGFORM),
    ("give me a table of accounts by region", S.SHAPE_STRUCTURED),
    ("show my open opportunities", S.SHAPE_STRUCTURED),
])
def test_salesforce_mode_uses_the_assistant_shapes(text, shape):
    assert S.shape_for(text, mode="salesforce") == shape
