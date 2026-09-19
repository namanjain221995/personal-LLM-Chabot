"""A rewrite into a pasted sample's format comes back as Markdown (hotfix 1.2, P3).

Fast followed the prompt's plain-sample -> Markdown mapping in at most 1 of 3
live runs (harness live-out/pasted-format): section names came back as bold
lines or plain text, items as plain lines, and `Job Title: X` as a `##`
heading. The mapping is decided by the sample, so core/rewrite_shape.py applies
it to the stream. These drive the real chat engine with a stubbed model stream.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app.core import rewrite_shape
from app.engines import chat as chat_engine

from tests.test_answer_quality_combined import FakeStream, _chunk, engine  # noqa: F401

POSTING = "\n".join(
    [
        "Senior Data Engineer - Claims Platform",
        "Orbiton Mutual Insurance Private Limited",
        "Location Pune, hybrid, in office Monday and Wednesday at the Baner campus",
        "Experience 7 to 10 years",
        "requirements",
        "7+ years building batch and streaming data pipelines in Python and SQL",
        "hands on experience with Kafka, Flink or Spark Structured Streaming",
        "has run a dbt project with more than 400 models in production",
        "benefits",
        "family health insurance of 6 lakh rupees and a learning budget",
    ]
)
SAMPLE = "\n".join(
    [
        "Job Title: Data Engineer",
        "Company: Example Retail Labs",
        "Location: Surat (Hybrid)",
        "Key Skills",
        "Python, SQL",
        "Must Have",
        "5+ years of data engineering.",
        "Perks",
        "Medical insurance.",
    ]
)
ASK = "this is the requirement, use the sample format below and change it in the same way"
REWRITE = f"{POSTING}\n\n{ASK}\n\n{SAMPLE}"

#: What Fast actually streamed (shape of live run main~candidate2 run 1): bold
#: labels, section names as bold lines, items as plain lines.
PLAIN_ANSWER = "\n".join(
    [
        "## Job Title: Senior Data Engineer",
        "**Company:** Orbiton Mutual Insurance Private Limited",
        "Location: Pune, hybrid",
        "**Key Skills**",
        "Python, SQL, Kafka, Flink, Spark Structured Streaming, dbt",
        "",
        "Must Have",
        "7+ years building batch and streaming data pipelines in Python and SQL.",
        "Hands-on experience with Kafka, Flink or Spark Structured Streaming.",
        "Has run a dbt project with more than 400 models in production.",
        "",
        "**Perks**",
        "Family health insurance of 6 lakh rupees.",
        "A learning budget.",
    ]
)

HEAD = re.compile(r"^#{1,6}\s+\S", re.M)
BOLD_LABEL = re.compile(r"\*\*[^*\n]{1,60}?(?::\*\*|\*\*\s*:)")
BULLET = re.compile(r"^[-*+]\s+\S", re.M)


def _streamed(answer: str, size: int = 7):
    """The answer in small deltas that split lines mid-word, like vLLM's."""
    return FakeStream(
        [_chunk(content=answer[i : i + size]) for i in range(0, len(answer), size)]
        + [_chunk(finish="stop")]
    )


def _run(message: str):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    answer = asyncio.run(
        chat_engine.run_chat_engine(message, [], emit, mode="assistant", effort="fast")
    )
    streamed = "".join(d["text"] for k, d in events if k == "token")
    return answer, streamed


def test_a_rewrite_into_a_pasted_sample_streams_markdown(engine):
    engine(_streamed(PLAIN_ANSWER))
    answer, streamed = _run(REWRITE)
    # What is stored is what was streamed.
    assert answer == streamed
    assert len(HEAD.findall(answer)) >= 3
    assert len(BOLD_LABEL.findall(answer)) >= 3
    assert len(BULLET.findall(answer)) >= 3
    lines = answer.split("\n")
    # A Label: value line is never a heading.
    assert "**Job Title:** Senior Data Engineer" in lines
    assert "**Location:** Pune, hybrid" in lines
    # Section names are headings, whether bold or plain.
    assert {"## Key Skills", "## Must Have", "## Perks"} <= set(lines)
    # Two or more item lines are a list; one line alone stays prose.
    assert "- Has run a dbt project with more than 400 models in production." in lines
    assert "- A learning budget." in lines
    assert "Python, SQL, Kafka, Flink, Spark Structured Streaming, dbt" in lines


def test_any_other_turn_is_streamed_byte_for_byte(engine):
    engine(_streamed(PLAIN_ANSWER))
    answer, streamed = _run("Summarise the pros and cons of Kafka versus Flink.")
    assert answer == streamed == PLAIN_ANSWER


@pytest.mark.parametrize(
    "ask",
    [
        "rewrite this posting as a table like the sample below",
        "change it in the same way as the sample below but as plain text",
        "convert this to JSON using the template below",
    ],
)
def test_an_ask_for_a_shape_markdown_would_break_is_left_alone(ask):
    assert rewrite_shape.for_message(f"{POSTING}\n\n{ask}\n\n{SAMPLE}") is None


def test_a_rewrite_with_no_sample_of_its_own_is_left_to_the_prompt():
    assert rewrite_shape.for_message(f"{POSTING}\n\nreformat this posting nicely") is None


def test_markdown_and_code_pass_through_and_paragraphs_stay_prose():
    shaper = rewrite_shape.for_message(REWRITE)
    text = "\n".join(
        [
            "## Key Skills",
            "- Python",
            "1. First",
            "| a | b |",
            "```",
            "Location: inside a fence",
            "plain line one",
            "plain line two",
            "```",
            "A first short paragraph.",
            "",
            "A second short paragraph.",
        ]
    )
    out = shaper.feed(text) + shaper.finish()
    assert out.split("\n") == text.split("\n")


def test_a_skeleton_sample_with_empty_labels_is_still_a_sample():
    skeleton = "Job Title:\nCompany:\nLocation:\nKey Skills\nMust Have\nPerks"
    msg = f"{POSTING}\n\n{ASK}\n\n{skeleton}"
    assert rewrite_shape.sample_sections(msg) >= {"key skills", "must have", "perks"}
