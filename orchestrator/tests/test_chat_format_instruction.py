"""How a chat answer is SHAPED, and what a Fast turn is allowed to spend.

Two halves of the same owner report (2026-09-17).

1. Answers came back as plain lines — no headings, no bold labels, no bullets
   — because the assistant prompt never said the interface renders Markdown.
   The same report's rewrite turn ("change my resume the same way") copied the
   pasted sample's plain text literally, invented a section the source said
   nothing about, and in one reproduction answered with the SAMPLE's content
   instead of the source's. `engines.FORMAT_INSTRUCTION` answers all of that,
   and only on the full assistant prompt: the Fast small-talk lane and the
   Salesforce chat prompt are short on purpose.

2. "Fast mode still thinks." A pasted job description contains "can",
   "fill" and a rate per hour, which `core/effort_policy.classify` reads as a
   measurement problem — so the chat engine opened a bounded thinking grant on
   a turn the person had asked to be fast. The engine no longer classifies
   anything; the request goes out thinking-off and the meta stays bare.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import effort_policy
from app.core import tracing as query_tracing
from app.engines import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, FORMAT_INSTRUCTION
from app.engines import chat as chat_engine

from tests.test_answer_quality_combined import (  # noqa: F401 — engine is a fixture
    FakeStream,
    _chunk,
    _collect,
    _thinking,
    engine,
)

MARKER = "FORMAT: answers are rendered as Markdown"

#: A pasted job description, of the shape the owner pasted: it holds "can",
#: "fill" and a rate per hour, which is exactly the trio that made
#: effort_policy.classify call it a measurement problem. Synthetic, with no
#: real company, person or contact detail in it.
PASTED_JOB_DESCRIPTION = """Here is the job description. Rewrite my resume in the same format.

Senior Exposure Management Analyst
Location: Remote (United States)
Rate: $160 per hour on a W2 contract

About the role
This is a backfill and we would like to fill the seat before the quarter ends.
We want an analyst who can own the external attack surface programme end to end.

Responsibilities
- Review the external attack surface every week and triage what is new.
- Drive remediation to a 45 day service level with the platform teams.
- Map every finding to MITRE ATT&CK before it reaches the risk register.

Requirements
- 5 years in security operations.
- Hands-on with Censys, Qualys or Tenable.
"""


def _assistant_system(mode: str = "assistant", **kwargs) -> str:
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    return chat_engine._messages("hello", history, mode, **kwargs)[0]["content"]


# ---------------------------------------------------------------------------
# 1. The instruction, and where it is allowed to be
# ---------------------------------------------------------------------------


def test_the_assistant_prompt_carries_the_format_instruction():
    system = _assistant_system()
    assert MARKER in system
    assert FORMAT_INSTRUCTION in system
    # Ahead of the two capability blocks, and never in place of them.
    assert system.index(FORMAT_INSTRUCTION) < system.index(DIAGRAM_INSTRUCTION)
    assert system.index(DIAGRAM_INSTRUCTION) < system.index(CODE_INSTRUCTION)
    assert system.startswith(chat_engine.ASSISTANT_SYSTEM + FORMAT_INSTRUCTION)


def test_the_lane_and_salesforce_prompts_do_not_carry_it():
    lane = _assistant_system(lane="greeting")
    assert MARKER not in lane and FORMAT_INSTRUCTION not in lane
    salesforce = _assistant_system("salesforce")
    assert MARKER not in salesforce and FORMAT_INSTRUCTION not in salesforce
    assert salesforce.startswith(chat_engine.SALESFORCE_CHAT_SYSTEM)


def test_the_instruction_names_sample_versus_source_and_not_specified():
    text = FORMAT_INSTRUCTION
    # The shape comes from the sample, the content from the source, and the
    # answer is never the sample handed back.
    assert "SAMPLE" in text and "SOURCE" in text
    assert "decides the shape" in text and "decides the " in text.split("SOURCE")[1]
    assert "never hand back the sample's own content" in text
    # A plain-text sample is still rendered.
    assert "plain-text sample still comes back as Markdown" in text
    assert "'Label: value'" in text
    # Nothing is invented for a section the source is silent about.
    assert "'Not specified'" in text
    assert "rather than inventing entries" in text
    # The structural vocabulary the answer is asked for.
    for token in ("## and ###", "- bullets", "**bold**", "table"):
        assert token in text
    # A short reply is still a short reply.
    assert "stays plain prose, with no headings" in text


# ---------------------------------------------------------------------------
# 2. A Fast turn does not think
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.events = []

    async def event(self, stage, **kwargs):
        self.events.append((stage, kwargs))


def _run_chat(message, *, effort="fast", mode="assistant", model_choice="smart"):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    recorder = _Recorder()

    async def main():
        token = query_tracing._current.set(recorder)
        try:
            return await chat_engine.run_chat_engine(
                message, [], emit, mode=mode, model_choice=model_choice, effort=effort,
            )
        finally:
            query_tracing._current.reset(token)

    return asyncio.run(main()), events, recorder


def test_the_pasted_job_description_is_what_the_classifier_misreads():
    """The premise of the test below: without this, it would prove nothing."""
    decision = effort_policy.classify(PASTED_JOB_DESCRIPTION)
    assert decision.think is True
    assert "measurement" in decision.signals


def test_a_fast_chat_turn_on_a_pasted_job_description_opens_no_grant(engine, monkeypatch):
    def must_not_grant(*args, **kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("the chat engine opened a thinking grant")

    # raising=False: Track F deletes the grant API outright, and this test
    # must keep proving the engine never reaches for one either way.
    monkeypatch.setattr(effort_policy, "grant", must_not_grant, raising=False)
    completions = engine(FakeStream([_chunk(content="Rewritten.", finish="stop")]))

    answer, events, recorder = _run_chat(PASTED_JOB_DESCRIPTION)

    request = completions.requests[0]
    assert _thinking(request) is False
    assert "thinking_token_budget" not in request["extra_body"]["chat_template_kwargs"]
    assert answer == "Rewritten."
    assert not [k for k, _ in events if k == "reasoning"]
    assert [d for k, d in events if k == "meta"] == [{"route": "chat"}]
    assert not [stage for stage, _ in recorder.events if stage == "ADAPTIVE_THINKING"]


@pytest.mark.parametrize("effort", ["think", "max"])
def test_the_efforts_that_ask_for_thinking_still_get_it(engine, monkeypatch, effort):
    """Removing the grant must not make Think stop thinking."""
    monkeypatch.setattr(chat_engine.settings, "extra_high_samples", 1)
    completions = engine(FakeStream([_chunk(reasoning="r"), _chunk(content="ok", finish="stop")]))
    _, events, _ = _run_chat(PASTED_JOB_DESCRIPTION, effort=effort)
    assert _thinking(completions.requests[0]) is True
    assert ("reasoning", {"text": "r"}) in events


def test_the_chat_engine_no_longer_classifies_a_turn_at_all(engine, monkeypatch):
    def must_not_classify(text):  # pragma: no cover - failing is the assertion
        raise AssertionError("the chat engine classified a turn")

    monkeypatch.setattr(effort_policy, "classify", must_not_classify)
    engine(FakeStream([_chunk(content="ok", finish="stop")]))
    assert not hasattr(chat_engine, "_adaptive_thinking")
    _run_chat(PASTED_JOB_DESCRIPTION)
