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


# ===================================================================
# QA rounds 1 and 2 (2026-09-18) against b13f07a: every reproduction.
# The intent cases are verdicts 4810da0 gave; b13f07a lost them.
# ===================================================================

import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import test_artifact_intent_labelled as L  # noqa: E402  (the production gate harness)

EDIT_CTX = dict(has_artifacts=True, last_turn_is_artifact=True, has_assistant_answer=True)


@pytest.mark.parametrize("text, hints", [
    # An EDIT that says "don't make a new file" is the commonest reason to say it.
    ("Don't create a new file, just update the existing report with the Q3 numbers.", ["report"]),
    ("Without creating a separate document, add a risks section to the deck.", ["deck"]),
    ("Don't make a new file. Change the title of the deck to Growth Plan.", ["deck"]),
    ("Update the deck - don't create a new file, just edit slide 3.", ["deck"]),
    ("Add a conclusion to the report, do not generate a new document.", ["report"]),
    # No "new": with a file in the room the edit rules still read it first.
    ("Update the report with the Q3 numbers, don't create a file.", ["report"]),
])
def test_an_edit_that_rules_out_a_new_file_is_still_an_edit(text, hints):
    intent = I.decide(text, artifact_hints=hints, **EDIT_CTX)
    assert intent.action == "edit", (text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    # A file request, then a negation of ANOTHER file.
    "Create a PDF of the plan. Don't create a separate file for the appendix.",
    "Make a deck on our hiring plan, and don't create a new file for the speaker notes.",
    "Build me a pitch deck. Don't generate a document.",
    "I need a PDF of this. Do not create a new file each time I ask for changes.",
    # The FILE delivered here: send/post/give/paste/show are not text verbs.
    "Make a PDF of this and send it here in this chat.",
    "Create a docx and post it in this chat.",
    "Generate the Excel sheet and give it here in this chat.",
    "Create a Word document and show the text inline too.",
    "Write it out in this chat, then make a PDF of it.",
    "Create the report as a PDF and paste the answer inline as well.",
    # "2 word documents" is two Word files.
    "Make me 2 word documents: one for the leave policy and one for travel.",
    # A first-person negation describes the person: the request before it stands.
    "Make me a report on our leave policy - I don't create files myself.",
    # A negation inside material after "<request>:" is the material's.
    "Turn this policy into a report: Staff don't create files on the shared drive.",
    # The count's `word` is not the format, but the doc it sizes is a file.
    "put it in a 2000 word doc",
    # A request AFTER the answer is placed here still makes its file.
    "Keep the answer inline for now, then make a report from it.",
    "Write it out in this chat, then turn it into a report.",
])
def test_a_file_request_survives_the_chat_only_forms(text):
    intent = I.decide(text)
    assert intent.action == "create", (text, intent.action, intent.rule)


#: The gate as main.py wires it (Fast lane, rules, recorded classifier).
#: chat-only is FINAL, so a wrong one is never looked at again.
@pytest.mark.parametrize("text, code, want", [
    ("Update the report, don't create a new file", "PF", "edit"),
    ("Edit the audit report: change the title to Q4 Audit. Do not make a new file.", "PF", "edit"),
    ("Fix the typo in the audit report without creating a new document", "PF", "edit"),
    ("Add a column for owner to the vendor tracker, don't create a new file", "PF", "edit"),
    ("Keep it inline and add a summary section to the audit report", "PF", "edit"),
    ("Convert this to a docx without creating a new document", "PA", "export"),
    ("Write it out in this chat and also save it as a PDF", "PA", "export"),
    ("answer inline, then also create a PDF of it", "PA", "export"),
    ("Make a PDF of the plan. Don't create a separate file for the appendix.", "P0", "create"),
    ("Create a PowerPoint deck on Q3 results. Do not make a new file each time I ask for a change.", "P0", "create"),
    ("Create a PDF. Don't give me a download link, attach the file", "P0", "create"),
    ("Make a deck. No need to create a separate document for notes.", "P0", "create"),
    ("Create a report on churn as a PDF, and do not create any attachment besides it", "P0", "create"),
    ("Create a report on churn as a PDF without producing extra files", "P0", "create"),
    ("Can you make a docx? My laptop can't create files with Word installed.", "P0", "create"),
    ("Turn this policy into a PDF: Employees must not create files on the shared drive. Managers review access monthly.",
     "P0", "create"),
    # A chart IS shown in the chat.
    ("Draw a bar chart of sales by month and show it inline: Jan 10, Feb 20, Mar 30", "P0", "create"),
    ("Plot this data as a pie chart and show it here in this chat: A 10, B 20, C 30", "P0", "create"),
    ("Make a pie chart of the table above and show it inline", "PA", "create"),
    ("Create a PDF report on churn and send it here in this chat", "P0", "create"),
    ("Create an Excel tracker and post it in this chat", "P0", "create"),
    ("Make the deck and show it inline", "P0", "create"),
    # A chart with no format named: shown here IS where a chart goes.
    ("Plot sales by month as a line chart and put it here in this chat: Jan 10, Feb 20, Mar 30", "P0", "create"),
])
def test_the_gate_keeps_every_file_the_chat_only_forms_took(text, code, want):
    intent, _calls, _ = L._decide("long-asks-qa", text, code)
    assert intent.action == want, (text, intent.action, intent.rule)


def test_with_a_file_in_the_room_a_ruled_out_file_is_not_created_but_the_classifier_may_look():
    """No edit rule takes it, so no rule may create; the rule is not
    "chat-only" (final) because "In the audit report, answer inline each
    reviewer question" reached the classifier before 2026-09-18."""
    intent = I.decide("Write a 10,000-word document on the Silk Road. Do not create a file.",
                      has_artifacts=True, artifact_hints=["Quarterly Audit Report"])
    assert (intent.action, intent.rule) == ("none", "no-file-asked")
    assert I._should_consult(intent, "Write a 10,000-word document on the Silk Road. Do not create a file.")


@pytest.mark.parametrize("text", [
    # A word-count piece with a format (or a file cue) named anywhere is a
    # file by the RULES, not only when the classifier is up (+1.4-2.2 s).
    "Write a 2,000-word article on leave. PDF please.",
    "Write a 2,000-word article on leave, PDF format please",
    "Write a 2,000 word article on leave (docx)",
    "Write a 2,000 word article on leave - pdf",
    "Write a 2,000-word article on leave. Word file please.",
    "Create 3 word documents, one per team",
    "Make 5 word docs for each department",
    "Write a 1,500 word essay on climate, downloadable",
    "Write a 3,000-word story I can download",
    "Write a 3,000-word article I can print",
])
def test_a_count_with_a_named_format_is_created_by_the_rules(text):
    intent = I.decide(text)
    assert intent.action == "create", (text, intent.rule)


@pytest.mark.parametrize("text", [
    "Write a 3,000-word story about a lighthouse keeper.",
    "Write a 3,000-word essay on the fall of Rome.",
    "Never create files for me. Write a 2,000-word essay on tea.",
    "Write a 2,000\u00a0word article about tea.",
    "Write a 10k-word article about Rome.",
    "Write a 5,000\u2013word article about Rome.",
    "Write a 5,000 - word article about Rome.",
])
def test_intended_chat_forms(text):
    assert I.decide(text).action == "none", text


@pytest.mark.parametrize("text, fmt", [
    ("Write a 2,000-word essay and save it as a Word document.", "docx"),
    ("Give me a 500 word doc on our leave policy", "docx"),
    ("Write a 10,000-word document on the history of Rome.", "docx"),
])
def test_count_and_a_named_file_still_create(text, fmt):
    intent = I.decide(text)
    assert intent.action == "create" and fmt in intent.formats, (text, intent.action, intent.formats)


@pytest.mark.parametrize("text", [
    "اكتب مقالاً من 3000 كلمة عن طريق الحرير",
    "Write a 3,000-word article about ‮Rome‬.",
    "‏" * 50 + "Write a 3,000-word article.",
])
def test_rtl_and_bidi_controls_do_not_crash(text):
    I.decide(text)
    S.shape_for(text)
    S.requested_words(text)


@pytest.mark.parametrize("unit", ["don't create a file ", "i don't create a file ", "put it ", "answer ",
                                  "show it inline ", "in this chat ", "5,000-word ", "5 word ", "answer inline ",
                                  "make a pdf: don't create a file ", "write it here in this chat, "])
def test_decide_stays_fast_on_adversarial_repeats(unit):
    text = (unit * (4000 // len(unit) + 1))[:4000]
    I.decide(text)
    started = time.perf_counter()
    for _ in range(3):
        I.decide(text)
    assert (time.perf_counter() - started) / 3 < 0.1, unit


# ------------------------------------------------ requested_words (QA) --

@pytest.mark.parametrize("text", [
    # A count INSIDE pasted or quoted material is data, not the person's ask.
    "Summarise this email for me:\n\nHi all, please write a 5,000-word report on the migration by Friday. Thanks, Sam",
    "Translate this to French: Please write a 2,000 word article about our product launch.",
    "Is this sentence grammatical? 'Write a 1,500-word essay about your summer.'",
    "Fix the typos: pls writ a 4000 word storry about dragons",
    'Here is the assignment brief: "Write a 3,000-word essay on climate policy." Here is my draft: Climate '
    "policy matters because it shapes investment. Give me feedback on the draft.",
    # The size of the SOURCE.
    "Write a critique of a 10,000-word thesis",
    # More limits.
    "Write a summary; keep it to 1,000 words.",
    "Write an article of not over 1,500 words.",
    "Write an article, 1,500 words or shorter.",
    "Write a blog post, 1,200 words at the very most.",
    "Write a 2,000-word-max essay",
    # A size per item.
    "Write 10 blog posts, each 1,000 words.",
    "Write 10 blog posts about tea, each around 1,000 words long.",
    "Write three articles; make each one 1,500 words.",
    "Write a 10-chapter novel where each chapter is 3,000 words.",
    "Write a pair of 1,500-word essays",
    "Write a series of 1,500-word essays for each month of the year",
    "Draft 4 x 1,000-word articles",
    "Write 4 x 1,000 words on tea.",
    "Write two thoughtful 1,500-word essays on tea.",
    "Write 5 500-word essays",
    "Write 5 essays of 1,500 words.",
    # Two pieces are not one target.
    "Write a 1,000-word essay and a 1,000-word rebuttal.",
    "Write a 2,000-word short story and a 1,000-word analysis of it.",
    # A question about a length.
    "How do I write a 5,000-word essay?",
])
def test_requested_words_is_none_for_every_qa_case(text):
    assert S.requested_words(text) is None, (text, S.requested_words(text))


@pytest.mark.parametrize("text, words", [
    # The count describing OTHER text is skipped; the answer's own stays.
    ("Write a 2,000-word article that references a 10,000-word study", 2000),
    ("write a 900 word article, the entire book is 90,000 words", 900),
    ("Write a 3,000-word paper with a 200-word abstract.", 3000),
    ("Write a 200-word abstract for a 5,000-word paper on tides.", 200),
    ("Give me a 100-word pitch for my 90,000-word novel.", 100),
    # A correction: the count ruled out is not the target.
    ("Write a 2,000 word essay, not 5,000 words.", 2000),
    # The same target restated in another sentence.
    ("Write a 3,000-word essay on tea. Aim for 3,000 words.", 3000),
    # Material presented, then the person's own ask on its last line.
    ("Here is my outline:\n- origins\n- trade\n- decline\nNow write a 3,000-word article from it.", 3000),
    ("Here is my outline: origins, trade, decline. Now write a 3,000-word article from it.", 3000),
    ("Task: write a 3,000-word article on tea.", 3000),
])
def test_requested_words_reads_the_answers_own_count(text, words):
    assert S.requested_words(text) == words


def test_requested_words_is_bounded_on_a_huge_message():
    big = "Write a 3,000-word article about tea. " + ("filler text with 2,000 words inside. " * 150_000)
    t0 = time.perf_counter()
    S.requested_words(big)
    S.shape_for(big)
    assert time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize("text", ["Give me 5 100-word summaries", "Give me 2 500 word essays"])
def test_two_counts_side_by_side_are_not_one_grouped_count(text):
    """4810da0 read both as prose; a plain-space group made them 5,100 and 2,500."""
    assert S.shape_for(text) == S.SHAPE_PROSE


# -------------------------------------------- the length target (QA) ----


class RaisingFakeModel(FakeModel):
    """A script entry may be an exception, raised before any token."""

    async def stream(self, messages, **kwargs):
        text, _ = self.script[min(self.calls, len(self.script) - 1)]
        if isinstance(text, Exception):
            self.calls += 1
            self.prompts.append([dict(m) for m in messages])
            raise text
        async for item in super().stream(messages, **kwargs):
            yield item


@pytest.fixture()
def raising_model(monkeypatch):
    def _install(script) -> RaisingFakeModel:
        fake = RaisingFakeModel(script)
        monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
        monkeypatch.setattr(llm, "get_finish_reason", fake.finish_reason)
        monkeypatch.setattr(llm, "get_usage", fake.usage)
        return fake

    return _install


def test_the_extension_never_downgrades_a_complete_answer_to_repetition(model):
    """The optional segment reopens the piece; the guard drops it, and the
    answer the person got is the complete one."""
    first = prose(1800)
    model([(first, "stop"), (first[-900:], "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert streamed == first
    assert (result.stop_reason, result.truncated) == (continuation.STOP_COMPLETE, False), result.as_meta()


def test_an_extension_that_restarts_the_piece_reports_what_no_target_reports(model):
    first = prose(1800)
    model([(first, "stop"), (first[:4000], "stop")])
    no_target, text_a = run(total_max_tokens=1_000_000, segment_max_tokens=8000)
    model([(first, "stop"), (first[:4000], "stop")])
    with_target, text_b = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert text_a == text_b
    assert (with_target.stop_reason, with_target.truncated) == (no_target.stop_reason, no_target.truncated) \
        == (continuation.STOP_COMPLETE, False)


def test_the_extension_never_downgrades_a_complete_answer_to_error(raising_model):
    first = prose(1800)
    fake = raising_model([(first, "stop"), (RuntimeError("engine died"), None)])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2 and streamed == first
    assert (result.stop_reason, result.truncated) == (continuation.STOP_COMPLETE, False), result.as_meta()
    assert result.errors, "the failure is still recorded"


def test_an_extension_that_dies_mid_text_is_an_error(model, monkeypatch):
    """Once the extension has shown words the answer ends mid-extension: that
    IS a stopped answer."""
    first = prose(1800)
    calls = {"n": 0}

    async def stream(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "token", first
            return
        # Past the held seam opening (_MAX_OVERLAP_CHARS), so it is shown.
        yield "token", "\n\n" + prose(150, start=5000)
        raise RuntimeError("engine died")

    monkeypatch.setattr(llm, "stream_chat_events", stream)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    monkeypatch.setattr(llm, "get_usage", lambda: {"completion_tokens": 1, "prompt_tokens": 0, "calls": 1})
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert calls["n"] == 2 and "w5000" in streamed
    assert result.stop_reason == continuation.STOP_ERROR and result.truncated is True


@pytest.mark.parametrize("ending", [
    "\n\n## Conclusion\n\nRoman roads outlived the empire that built them.\n",
    "\n\n### Conclusion: The Inevitability of War?\n\nAs we look back, the lesson is plain.\n",
    "\n\n**Final Thoughts**\n\nThat is the whole story.\n",
    "\n\nIn conclusion, the network was the empire's nervous system.\n",
])
def test_no_extension_after_the_piece_has_ended(model, ending):
    """Live 2026-09-18: 2,462 of 3,000 and 4,167 of 5,000 words, both ending
    in a Conclusion, got new body sections appended UNDER that conclusion."""
    fake = model([(prose(1800) + ending, "stop"), (prose(600, start=1800), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 1
    assert result.stop_reason == continuation.STOP_COMPLETE
    assert streamed.endswith(ending)


def test_an_earlier_conclusion_heading_does_not_block_the_extension(model):
    """Only the LAST section decides: a piece whose conclusion heading is
    followed by more sections has not ended there."""
    body = "## Summary of sources\n\n" + prose(200) + "\n\n## The trade routes\n\n" + prose(1600, start=200)
    fake = model([(body, "stop"), (prose(600, start=1800), "stop")])
    run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2


def test_a_pasted_count_does_not_pad_a_translation(model):
    msg = "Translate this to French: Please write a 2,000 word article about our product launch."
    fake = model([("Veuillez écrire un article de 2 000 mots sur le lancement de notre produit.", "stop"),
                  (prose(1900), "stop")])
    run(messages=[{"role": "user", "content": msg}], total_max_tokens=1_000_000, segment_max_tokens=8000,
        target_words=S.requested_words(msg))
    assert fake.calls == 1


def test_the_overshoot_cut_never_leaves_a_code_fence_open(model):
    code = "```python\n" + "".join(f"x{i} = {i}  # step {i} of the pipeline\n" for i in range(3000)) + "```\n\nDone.\n"
    model([(prose(900) + code, "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=100_000, target_words=1000)
    assert result.stop_reason == continuation.STOP_BUDGET
    assert streamed.count("```") % 2 == 0, streamed[-80:]
    assert streamed.endswith("```\n")


def test_the_overshoot_waits_for_a_fence_that_closes_soon(model):
    """A short code block still open at the high mark closes on its own; the
    run stops at the first line break after it, and adds nothing."""
    code = "\n\n```python\n" + "".join(f"y{i} = {i}\n" for i in range(30)) + "```\n\nAfter the code.\n"
    model([(prose(1290) + code + prose(300, start=1290), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=100_000, target_words=1000)
    assert result.stop_reason == continuation.STOP_BUDGET
    assert streamed.count("```") == 2
    assert "y29 = 29\n```\n" in streamed and streamed.count("```\n```") == 0


def test_the_gauge_counts_words_split_across_deltas_without_a_seam(model):
    """total == segment: no continuation is possible (`may_continue` False),
    so every 11-character delta goes straight out and splits words."""
    text = " ".join(f"word{i}" for i in range(1500)) + "."
    model([(text, "stop")])
    result, streamed = run(total_max_tokens=8000, segment_max_tokens=8000, target_words=3000)
    assert streamed == text
    assert result.words == count(streamed) == 1500


def test_concurrent_runs_keep_their_own_targets(monkeypatch):
    scripts = {
        "A": [(prose(1000), "stop"), (prose(900, 1000), "stop")],
        "B": [(prose(1300), "stop")],
    }
    calls = {"A": 0, "B": 0}

    async def stream(messages, **kw):
        key = messages[0]["content"]
        text, _ = scripts[key][min(calls[key], len(scripts[key]) - 1)]
        calls[key] += 1
        for i in range(0, len(text), 13):
            await asyncio.sleep(0)
            yield "token", text[i: i + 13]

    monkeypatch.setattr(llm, "stream_chat_events", stream)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    monkeypatch.setattr(llm, "get_usage", lambda: {"completion_tokens": 1, "prompt_tokens": 0, "calls": 1})

    async def both():
        async def od(k, t):
            return None
        return await asyncio.gather(
            continuation.stream_long_completion([{"role": "user", "content": "A"}], on_delta=od,
                                                total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000),
            continuation.stream_long_completion([{"role": "user", "content": "B"}], on_delta=od,
                                                total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=1500),
        )

    a, b = asyncio.run(both())
    assert (a.words, a.target_words, calls["A"]) == (1900, 3000, 2)
    assert (b.words, b.target_words, calls["B"]) == (1300, 1500, 1)


def test_a_paragraph_loop_is_still_caught_at_the_seam(model):
    paras = [f"Paragraph {n}: " + " ".join(f"t{n}_{k}" for k in range(60)) + "\n\n" for n in range(12)]
    model([("".join(paras), "length"), ("".join(paras[8:]), "length")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000)
    assert result.stop_reason == continuation.STOP_REPETITION
    assert streamed.count("Paragraph 8:") == 1


def test_a_near_duplicate_restart_that_diverges_is_not_a_loop():
    produced = "Intro. " + "The same opening sentence of this section is long enough to fill the window. " * 3
    candidate = produced[7:7 + 170] + " and then a completely different continuation " * 6
    assert continuation._repeats_existing(produced, candidate) is False


# ------------------------------------------------------ fit_request (QA) --


def _counting(window: int, calls: List[int]):
    async def counter(base_url, model, messages):
        calls.append(1)
        return sum(len(m.get("content", "")) // 3 + 4 for m in messages if isinstance(m.get("content"), str)), window

    return counter


@pytest.mark.parametrize("history", [2, 24, 30, 60])
def test_an_over_window_paste_after_a_long_conversation_is_clipped_not_starved(monkeypatch, history):
    """4810da0 and b13f07a: with 24+ prior messages all 24 rounds went on
    dropping one turn each, and a 1.77M-token prompt went to a 1M window
    with max_tokens=1. Now the old turns go in one round and the paste is
    clipped in the same round."""
    window, requested = 1_000_000, 8000
    calls: List[int] = []
    monkeypatch.setattr(context, "count_tokens", _counting(window, calls))
    paste = "".join(f"line {i}: the quarterly ledger reconciles.\n" for i in range(118_000))
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(history):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " * 50})
    msgs.append({"role": "user", "content": paste + "\nSummarise the ledger."})
    sized, max_tokens = _fit(msgs, requested)
    assert max_tokens >= min(requested, context.OVERFLOW_ANSWER_ROOM)
    prompt = sum(len(m["content"]) // 3 + 4 for m in sized)
    assert prompt + max_tokens + settings.context_safety_margin <= window
    assert sized[-1]["content"].endswith("Summarise the ledger.")
    # Old turns go first, as they always did: the pinned system block and
    # the (clipped) paste are what is left.
    assert sized[0] == msgs[0] and len(sized) == 2
    assert len(calls) <= 4


def test_a_paste_just_over_a_small_window_keeps_its_text_and_loses_old_turns(monkeypatch):
    """The first repair clipped this paste by the WHOLE deficit to keep old
    turns: 15,689 characters became 2,000 while three old turns stayed
    (measured on the 400-case randomised fit comparison, case 22). Old turns
    go first; the paste loses only what they could not cover."""
    window = 8192
    calls: List[int] = []
    monkeypatch.setattr(context, "count_tokens", _counting(window, calls))
    msgs = [{"role": "system", "content": "s" * 2225}]
    for i, n in enumerate([444, 4884, 1492, 4279, 6009, 2737, 5828, 3651, 5402, 3655, 3198]):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "h" * n})
    msgs.append({"role": "user", "content": "q" * 15_689})
    sized, max_tokens = _fit(msgs, 100_000)
    assert max_tokens >= context._overflow_room(100_000, window)
    prompt = sum(len(m["content"]) // 3 + 4 for m in sized)
    assert prompt + max_tokens + settings.context_safety_margin <= window
    assert sized[0] == msgs[0]
    assert len(sized[-1]["content"]) >= 12_000, len(sized[-1]["content"])
    assert len(calls) <= 4


def test_overflow_room_never_exceeds_a_quarter_of_the_window(monkeypatch):
    """The room is capped at window // 4: a 16,000 ask in a 32k window trims
    for 8,192, not 16,000 (the lower-bound test above cannot see the cap)."""
    monkeypatch.setattr(context, "count_tokens", _char_counter(32_768))
    _, max_tokens = _fit([{"role": "user", "content": "x " * 200_000}], 16_000)
    assert 32_768 // 4 <= max_tokens < 9_000


def test_a_long_history_that_barely_overflows_is_trimmed_in_one_round(monkeypatch):
    """Dropping a turn per /tokenize round took 16 rounds here at b13f07a."""
    calls: List[int] = []
    window = 200_000
    monkeypatch.setattr(context, "count_tokens", _counting(window, calls))
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(80):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 7_500})
    msgs.append({"role": "user", "content": "and now?"})
    sized, max_tokens = _fit(msgs, 100_000)
    assert max_tokens >= context.OVERFLOW_ANSWER_ROOM
    assert len(calls) <= 3
    assert sized[-1]["content"] == "and now?" and sized[0]["role"] == "system"
    # Only as many of the oldest turns as the room needs, and the newest kept.
    assert sized[1:-1] == msgs[len(msgs) - len(sized) + 1:-1]
    assert len(msgs) - len(sized) <= 15



# ===================================================================
# QA r1, repair round 1 (2026-09-19): what the first repair left open,
# and every intent phrasing the four r1 reviews named.
# ===================================================================

FILE_CTX = dict(has_artifacts=True, last_turn_is_artifact=True, has_assistant_answer=True,
                artifact_hints=["Q3 report", "hiring deck", "vendor tracker", "sales spreadsheet"])
CONTEXTS = {"none": {}, "ans": dict(has_assistant_answer=True), "file": FILE_CTX}

#: Each phrasing whose verdict b13f07a changed, with the action 4810da0
#: gave it in each context where it changed (measured with intent.decide on
#: both commits). None of them was an intended change.
KEPT_VERDICTS = [
    ("Don't create a new file, just update the existing report with the Q3 numbers.", {'file': 'edit'}),
    ('Without creating a separate document, add a risks section to the deck.', {'file': 'edit'}),
    ("Don't make a new file. Change the title of the deck to Growth Plan.", {'file': 'edit'}),
    ("Update the deck - don't create a new file, just edit slide 3.", {'file': 'edit'}),
    ('Add a conclusion to the report, do not generate a new document.', {'file': 'edit'}),
    ("Create a PDF of the plan. Don't create a separate file for the appendix.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make a deck on our hiring plan, and don't create a new file for the speaker notes.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Build me a pitch deck. Don't generate a document.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('I need a PDF of this. Do not create a new file each time I ask for changes.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Make a PDF of this and send it here in this chat.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create a docx and post it in this chat.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Generate the Excel sheet and give it here in this chat.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Create a Word document and show the text inline too.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Write it out in this chat, then make a PDF of it.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create the report as a PDF and paste the answer inline as well.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Make me 2 word documents: one for the leave policy and one for travel.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Update the deck, don't create a new file.", {'file': 'edit'}),
    ('Add a slide on hiring to the deck - do not create a new file.', {'file': 'edit'}),
    ("Fix the typo in the report. Don't generate a new document.", {'file': 'edit'}),
    ("Please update the report with the new numbers and don't create a new file.", {'file': 'edit'}),
    ("Replace the chart in the deck with a pie chart. Don't make a new file.", {'file': 'edit'}),
    ("Revise the PDF in place - don't generate a new document.", {'file': 'edit'}),
    ('Edit the existing tracker, do not create a new file.', {'file': 'edit'}),
    ("Change the title of the spreadsheet; don't make a new file, edit the existing one.", {'file': 'edit'}),
    ('Create a PDF of the plan. Do not create a separate file for the appendix.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make a PDF report on Q3, and don't create a new file for the charts.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make one PDF with all chapters, don't create a file per chapter.", {'none': 'create', 'ans': 'create', 'file': 'convert'}),
    ("Create a single docx; don't make separate documents for each section.", {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Put all three tables in one Excel workbook; do not create separate files.', {'none': 'create', 'ans': 'create', 'file': 'convert'}),
    ('Export the answer above as a docx. Do not send the file to anyone.', {'none': 'create', 'ans': 'export', 'file': 'export'}),
    ('Put it in a docx, but do not attach the file to an email.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ("Convert this to PDF, don't create a separate file for each chapter.", {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Answer in this chat and also give me a PDF version.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Write it out here in this chat, then make a PDF of it.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create a report as a PDF. Answer in this chat when it is done.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Give me a PDF of the summary and answer in this chat too.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create a docx proposal, and answer in this chat what you assumed.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Write the report in a PDF; respond in this chat with a summary too.', {'none': 'create', 'ans': 'create', 'file': 'convert'}),
    ('Create a word document. Keep it inline with our brand guide.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create a PDF; keep it inline with the style of the last report.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create 3 word documents: one for sales, one for HR and one for finance.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Can you make 4 Word documents, one per quarter?', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 5,000-word article on Rome. Word document please.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Update the report, don't create a new file", {'file': 'edit'}),
    ('Edit the audit report: change the title to Q4 Audit. Do not make a new file.', {'file': 'edit'}),
    ('Fix the typo in the audit report without creating a new document', {'file': 'edit'}),
    ("Add a column for owner to the vendor tracker, don't create a new file", {'file': 'edit'}),
    ('Keep it inline and add a summary section to the audit report', {'file': 'edit'}),
    ('Convert this to a docx without creating a new document', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Write it out in this chat and also save it as a PDF', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('answer inline, then also create a PDF of it', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ("Make a PDF of the plan. Don't create a separate file for the appendix.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a PowerPoint deck on Q3 results. Do not make a new file each time I ask for a change.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Create a PDF. Don't give me a download link, attach the file", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Make a deck. No need to create a separate document for notes.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a report on churn as a PDF, and do not create any attachment besides it', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a report on churn as a PDF without producing extra files', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Can you make a docx? My laptop can't create files with Word installed.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Turn this policy into a PDF: Employees must not create files on the shared drive. ...', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Draw a bar chart of sales by month and show it inline: Jan 10, Feb 20, Mar 30', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Plot this data as a pie chart and show it here in this chat: A 10, B 20, C 30', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Make a pie chart of the table above and show it inline', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a PDF report on churn and send it here in this chat', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create an Excel tracker and post it in this chat', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Make the deck and show it inline', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 2,000-word article on leave. PDF please.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 2,000-word article on leave, PDF format please', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 2,000 word article on leave (docx)', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 2,000 word article on leave - pdf', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 2,000-word article on leave. Word file please.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create 3 word documents, one per team', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Make 5 word docs for each department', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 5,000-word article on leave, word format please', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 1,500 word essay on climate, downloadable', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Write a 3,000-word story I can download', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make me a PDF of the essay. Don't create a separate file for the appendix.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Make me a PDF of the essay. Do not create a separate file for the appendix, keep it in the same PDF.', {'none': 'create', 'ans': 'create', 'file': 'convert'}),
    ("Create a Word document of the plan. Don't make a new file for each section.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Create a 2-page PDF brochure for our bakery. Don't send me a file link by email, just attach it here.", {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ("Don't create a new file, just update the existing deck with the Q3 numbers.", {'file': 'edit'}),
    ("Add a summary slide to the deck. Don't make a new file.", {'file': 'edit'}),
    ('Update the report with the new figures, do not create a new document.', {'file': 'edit'}),
    ("Fix the typo in the title. Please don't generate a new file, edit the same one.", {'file': 'edit'}),
    ("Change the chart colours to blue - don't make a new document", {'file': 'edit'}),
    ('Turn this into a PDF:\nCompany policy: employees must not create files on the shared drive without approval. Managers review access monthly.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Convert this to a Word document:\nRule 4. Staff should not create documents containing client data on personal laptops.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ("Export the answer above as a PDF. Don't create a new document, reuse the formatting.", {'none': 'create', 'ans': 'export', 'file': 'export'}),
    ('Make me a PDF and show it inline', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create the excel file, then answer inline with the totals.', {'none': 'create', 'ans': 'create', 'file': 'convert'}),
    ('Give me 2 word documents: one for the CV and one for the cover letter.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a 12-word slogan and put it on a slide.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make a PDF report on our Q3 results. Don't create a separate file for the appendix.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Create an Excel tracker for our sales leads and don't generate any extra files.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Generate the report as a PDF; do not produce a separate document for the charts.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ("Make me a Word document of the policy, and don't create any attachments beyond that.", {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Build a pptx for the board. Do not send me a separate file with the notes.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Create a deck on our hiring plan. Do not create a separate document for the speaker notes.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ("Make a PDF of it. Don't create a file for each section.", {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ("Don't generate a new file; fix the typo on page 2 of the report.", {'file': 'edit'}),
    ('Do not create a new document, edit the deck and change the title to Growth Plan.', {'file': 'edit'}),
    ('Put everything in this chat in a PDF.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Answer in this chat, and also export it as a PDF.', {'none': 'create', 'ans': 'export', 'file': 'convert'}),
    ('Write it out in this chat, then also make a PDF of it.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Make a one-pager PDF on our product and keep it inline with our brand guide.', {'none': 'create', 'ans': 'export', 'file': 'create'}),
    ('Create 2 word documents, one for the leave policy and one for the FAQ.', {'none': 'create', 'ans': 'create', 'file': 'create'}),
    ('Convert these 2 PDFs into 2 word docs.', {'none': 'create'}),
    ('Give me the answer above as 2 word documents: summary and detail.', {'none': 'create', 'ans': 'export', 'file': 'export'}),
]
_KEPT = [(t, c, a) for t, v in KEPT_VERDICTS for c, a in v.items()]


@pytest.mark.parametrize("text, ctx, want", _KEPT, ids=[f"{c}-{i}" for i, (_, c, _) in enumerate(_KEPT)])
def test_every_reviewed_phrasing_keeps_its_4810da0_verdict(text, ctx, want):
    intent = I.decide(text, **CONTEXTS[ctx])
    assert intent.action == want, (ctx, text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    # The count sizes a piece the answer is ABOUT. Live (QA r1) the outline
    # for a 10,000-word dissertation came back at 1,248-1,397 words and the
    # extension appended 3,800-4,200 more under it.
    "Give me an outline for a 10,000-word dissertation on climate policy.",
    "Explain how to structure a 10,000-word dissertation.",
    "Explain the structure of a 10,000-word dissertation.",
    "Give me tips for writing a 3,000-word essay.",
    "Tell me how long it takes to write a 5,000-word essay.",
    "I need to write a 5,000-word essay on Gandhi. Can you suggest a title?",
    "I am writing a 10,000-word thesis on water policy. What are good sources?",
    "Create a checklist for a 20,000-word report.",
    "Draft a table of contents for a 20,000-word report.",
    "Write a title for a 3,000-word article on tea.",
    "Give me 5 title ideas for a 3,000-word blog post.",
    "Create a rubric for grading 1,500-word essays.",
    # A text worked ON.
    "Please proofread a 3,000-word essay I will paste next.",
    "Please proofread my essay (4,000 words).",
    "Please summarise this; the report is 12,000 words long.",
    # A reduction: its count is a ceiling (live: a 550-word summary was told
    # to continue with about 19,450 more words).
    "Summarize a 20,000-word report into 500 words",
    "Can you summarize a 10,000 word report in 5 bullet points?",
    "I need you to cut this down from 3,000 words to 1,000 words.",
    # A limit said as a negated verb.
    "Write an essay on climate change, don't exceed 1,500 words.",
    "Write a cover letter; don't go over 1,000 words.",
    "Write a blog post in a maximum of 1,200 words.",
    "Write a summary that doesn't exceed 900 words.",
    # Pasted material.
    "Here is the assignment brief:\n\nStudents will write a 5,000-word essay on the causes of WWI.\n\n"
    "Summarise the brief above in 3 bullet points.",
    "Translate this: 'Please write a 3,000-word report on sales.'",
    "Summarise this job ad: We need a writer who can produce 2,000 words a day.",
    # A second deliverable after the piece: past 130% of ONE piece the run is
    # cut, and the MCQs or the translation would be lost.
    "Write a 1,000-word essay on photosynthesis, then give me 10 MCQs with answers on it.",
    "Write a 2,000-word story, then translate it into Hindi.",
    "Write a 1,000-word essay on photosynthesis and a 1,000-word essay on respiration.",
    "Write 1,000 words on each of these 4 topics: tea, coffee, cocoa, mate.",
    "For each chapter, write a 1,000-word summary.",
    "Write 3 x 1,000-word articles about tea.",
    # A question that asks whether to write it is not the ask.
    "Should I write a 3,000-word essay on tea?",
    # A correction to another length, and a count no answer runs to.
    "Write me a 5,000 word essay. Actually make it 2,000 words.",
    "Write a 99,999,999-word novel.",
])
def test_requested_words_is_none_for_every_r1_case(text):
    assert S.requested_words(text) is None, (text, S.requested_words(text))


@pytest.mark.parametrize("text, words", [
    # The skips above must not take a plain ask.
    ("I need a 5,000-word essay on the causes of WWI.", 5000),
    ("I need you to write a 5,000-word essay on the causes of WWI.", 5000),
    ("Can you write a 3,000-word article about tea?", 3000),
    ("Write a 3,000-word essay on tea. Also, make it funny.", 3000),
    ("Write a 3,000-word story where the hero then builds a boat.", 3000),
    ("Explain photosynthesis in a 1,500-word essay.", 1500),
    ("Write a 3,000-word article about the Silk Road.", 3000),
])
def test_the_r1_skips_leave_a_plain_ask_alone(text, words):
    assert S.requested_words(text) == words


CLARIFY = ("I'd be glad to write that article, but I don't have any record of what you told me earlier. "
           "Could you tell me which topic you mean, and who the article is for?")


@pytest.mark.parametrize("first", [CLARIFY, prose(700)], ids=["clarifying-question", "23-percent"])
def test_a_reply_far_below_the_target_is_not_padded(model, first):
    """Live (QA r1): an 80-93-word clarifying question (3% of 3,000) was told
    "It is not finished: continue it with about 2,900 more words", and the
    extension appended "I cannot fulfill this request..."."""
    fake = model([(first, "stop"), (prose(900, start=5000), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 1
    assert streamed == first
    assert (result.stop_reason, result.truncated) == (continuation.STOP_COMPLETE, False)


def test_a_quarter_of_the_target_still_gets_the_extension(model):
    fake = model([(prose(760), "stop"), (prose(900, start=760), "stop")])
    run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2


@pytest.mark.parametrize("ending", [" The end.", " The end.\n", " The end.\n\n"])
@pytest.mark.parametrize("opening", ["### Part 2\n\n", " The next part opens here.\n\n", "\nThe next part.\n\n"])
def test_the_extension_starts_a_new_paragraph(model, ending, opening):
    """Live (QA r1), 4 of 4 extensions were glued to the last sentence:
    "connection.The psychological impact", "Infrastructure.### Chapter 9"
    (a heading that no longer renders)."""
    first = " ".join(f"w{i}" for i in range(1800)) + ending
    fake = model([(first, "stop"), (opening + " ".join(f"v{i}" for i in range(700)), "stop")])
    _, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2
    seam = streamed.index("The end.") + len("The end.")
    assert streamed[seam:seam + 2] == "\n\n" and streamed[seam + 2] not in " \n", repr(streamed[seam - 10:seam + 20])


@pytest.mark.parametrize("reply", [
    "I cannot continue the previous response because the previous response was a complete and logical answer.",
    "I cannot fulfill the request to generate 1,200 words of new material.",
    "I'm sorry, but the article above is already complete.",
    "Unfortunately, there is nothing more to add.",
])
def test_an_extension_that_declines_is_dropped(model, reply):
    """Live (QA r1): four extensions answered the length note instead of
    writing, and the refusal was appended to the answer."""
    first = prose(1800)
    fake = model([(first, "stop"), (reply + "\n\n" + prose(50, start=9000), "stop")])
    result, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2
    assert streamed == first
    assert (result.stop_reason, result.truncated) == (continuation.STOP_COMPLETE, False)


def test_a_first_segment_that_declines_is_not_touched(model):
    """The decline rule reads the EXTENSION only: a first answer that opens
    with "I cannot" is the answer."""
    first = "I cannot share that. " + prose(1800)
    fake = model([(first, "stop"), (prose(900, start=5000), "stop")])
    _, streamed = run(total_max_tokens=1_000_000, segment_max_tokens=8000, target_words=3000)
    assert fake.calls == 2 and streamed.startswith(first)


# ===================================================================
# B6 (live on production, 2026-09-19): "Write a 1,200-word essay ..." at
# Fast went to the artifact route and came back as a PDF and a DOCX after
# 72-336 s with nothing streamed. An essay or article with a word count is
# streamed chat unless the person names a file or format.
# ===================================================================


@pytest.mark.parametrize("text", [
    "Write a 1,200-word essay on the impact of social media on teenagers.",
    "Write a 1,200 word essay about the French Revolution",
    "Please write a 1,200-word essay on AI in healthcare",
    "Write a 3,000-word article about renewable energy.",
    # A topic that names reports, documents, decks or downloads is still the
    # TOPIC: these went to the classifier (or, through a bare "download" or
    # "printable" cue, straight to a file) instead of the chat.
    "Write a 1,200-word essay on the role of documents in history.",
    "Write a 1,200-word essay on why reports matter.",
    "Write a 3,000-word article about the deck of a ship.",
    "Write a 2,000-word essay on why people download pirated music.",
    "Write a 1,000-word story about a kid who wants to download a game.",
    "Write a 2,000-word blog post about printable planners.",
    "Write a 1,500-word article on how to download files safely.",
])
@pytest.mark.parametrize("ctx", ["none", "ans"])
def test_a_counted_essay_or_article_is_final_chat(text, ctx):
    intent = I.decide(text, **CONTEXTS[ctx])
    assert intent.action == "none", (text, intent.action, intent.rule)
    assert not I._should_consult(intent, text), (text, intent.rule)


@pytest.mark.parametrize("text, fmt", [
    ("Write a 5,000-word article as a Word file", "docx"),
    ("Write a 5,000-word article about the Silk Road as a Word file.", "docx"),
    ("Write a 1,200-word essay on Gandhi as a PDF.", "pdf"),
    ("Write a 1,200-word essay on Gandhi in a Word document.", "docx"),
])
def test_a_counted_essay_with_a_named_format_is_still_a_file(text, fmt):
    intent = I.decide(text)
    assert intent.action == "create" and fmt in intent.formats, (text, intent.action, intent.formats)


@pytest.mark.parametrize("text", [
    "Write a 3,000-word article about Rome. Make it downloadable.",
    "Write a downloadable 1,500-word essay on tea.",
    "Write a 2,000-word essay on tea, printable version please.",
    "Write a 1,500-word essay on the history of the printing press and put it on a slide.",
])
def test_a_file_cue_about_the_piece_still_makes_a_file(text):
    assert I.decide(text).action == "create", text
