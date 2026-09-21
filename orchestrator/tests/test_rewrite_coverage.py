"""A long rewrite must not call itself finished after dropping part of the
source (platform audit #13).

REPRODUCED LIVE, 2026-09-21, in-process against the real engine at Fast, one
call at a time. A synthetic rulebook rewrote perfectly at every size — 7 of 7,
68 of 68, 204 of 204 and 409 of 409 items at 2k/20k/60k/120k characters — and
a REAL 120,000-character document (docs/developer-platform/STANDARDS.md, 647
list items) came back as a 17,431-character answer carrying 44 of its 523
measurable items, 8.4%, with `stop_reason=complete`, `truncated=false` and not
one word saying so. The defect is about a heterogeneous source, not size.

After the two layers below, the same ask at 120,000 characters came back at
88.5% (463 of 523), and the sizes that still fall short say so.

Two layers, tested here:

  * `_messages` carries a completeness clause on THAT turn only, with the item
    count counted by `core/rewrite_coverage.py` rather than estimated by the
    model;
  * `run_chat_engine` measures the finished answer against the source and
    appends what it measurably failed to carry, into the stream, into the
    stored text and into the meta.

Everything here is offline: the model is a scripted fake.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import rewrite_coverage as rc
from app.engines import chat as chat_engine

from tests.test_answer_quality_combined import (  # noqa: F401 — engine is a fixture
    FakeStream,
    _chunk,
    engine,
)

REWRITE_ASK = "Rewrite the site rules below as a clean, well-structured document."
SUMMARISE_ASK = "Summarise the site rules below in a few bullet points."

#: One distinct subject per rule, so every item has a word of its own.
SUBJECTS = [
    "badge reader", "cold-store freezer", "delivery van", "dock gate", "fire panel",
    "water pump", "air handler", "server rack", "forklift", "paint booth",
    "label printer", "weighbridge", "conveyor belt", "packing line", "sprinkler valve",
    "diesel generator", "camera recorder", "airlock door", "steam boiler", "spray dryer",
    "pallet wrapper", "chilled display", "goods lift", "compressor", "dust extractor",
    "bottling head", "sorting arm", "seal tester", "metal detector", "parcel scanner",
    "glue station", "inkjet coder", "shrink tunnel", "case erector", "palletiser",
    "brine tank", "capping head", "vacuum former", "ribbon blender", "grit trap",
    "wash cabinet", "roller shutter", "eyewash station", "gantry crane", "salt silo",
]
assert len(SUBJECTS) == len(set(SUBJECTS)) == 45, "every item needs a word of its own"


def _rules(n: int) -> str:
    """A numbered rulebook of `n` items, each naming a different subject."""
    return "\n".join(
        f"{i + 1}. The {SUBJECTS[i % len(SUBJECTS)]} is checked every {7 + i} days "
        f"and the result written into the log by the duty officer."
        for i in range(n)
    )


def _message(n: int = 40, ask: str = REWRITE_ASK) -> str:
    return ask + "\n\n" + _rules(n)


def _answer(first: int, last: int) -> str:
    """An answer carrying the source's items `first`..`last` (1-based)."""
    return "Here is the rewritten document.\n\n" + "\n".join(
        f"- **{SUBJECTS[i % len(SUBJECTS)].title()}:** checked every {7 + i} days, "
        f"result logged by the duty officer."
        for i in range(first - 1, last)
    )


# ---------------------------------------------------------------- the gate --


def test_a_rewrite_over_a_listed_paste_is_counted():
    source = rc.source_of(_message(40))
    assert source is not None
    assert source.items == 40


def test_the_clause_carries_the_count_the_code_made():
    """The model is never asked to work out how many items it was given."""
    assert "40 listed items" in _system_for(_message(40))


def test_a_summarise_ask_is_never_a_coverage_turn():
    """Dropping detail IS the instruction there."""
    assert rc.source_of(_message(40, SUMMARISE_ASK)) is None


def test_a_source_below_the_item_floor_is_not_a_coverage_turn():
    assert rc.source_of(_message(rc.MIN_ITEMS - 1)) is None
    assert rc.source_of(_message(rc.MIN_ITEMS)) is not None


@pytest.mark.parametrize(
    "ask",
    [
        "Rewrite the site rules below in Spanish.",
        "Translate the site rules below and keep the numbering.",
        "Convert the site rules below to CSV.",
    ],
)
def test_an_ask_this_cannot_measure_is_excluded_outright(ask):
    """The anchors are the SOURCE's own words. A faithful Spanish rewrite
    would measure as every item missing, and telling someone a complete
    answer is incomplete is worse than saying nothing."""
    assert rc.source_of(_message(40, ask)) is None
    assert rc.shortfall(_message(40, ask), "Una reescritura completa.") is None


def test_a_message_with_no_paste_is_not_a_coverage_turn():
    assert rc.source_of("Rewrite my CV to sound more senior.") is None


def test_a_question_about_a_pasted_list_is_not_a_rewrite():
    assert rc.source_of("What does rule 12 below actually require?\n\n" + _rules(40)) is None


# ------------------------------------------------------------- the anchors --


def test_items_that_share_every_word_are_still_measurable():
    """A rulebook that names assets by place and type ("the Ashfield pump",
    "the Ashfield chiller", "the Barrowden pump") has NO word unique to any
    item. Measured on the live source shape: 0 of 421 items measurable on
    single words, 421 of 421 once an adjacent pair may be the anchor."""
    places = ["Ashfield", "Barrowden", "Caldergate", "Denholme", "Elmsworth", "Farnley",
              "Garrowby", "Haverton", "Ilkeston", "Kelbrook", "Langrove", "Marsden",
              "Netherby", "Oakhanger"]
    kinds = ["pump", "chiller", "hoist"]
    rules = "\n".join(
        f"{i + 1}. The {places[i % len(places)]} {kinds[i // len(places)]} is checked "
        "every week and the result written into the log by the duty officer."
        for i in range(len(places) * len(kinds))
    )
    source = rc.source_of(REWRITE_ASK + "\n\n" + rules)
    assert source is not None
    assert source.items == 42
    assert len(source.anchors) == 42


# ------------------------------------------------------- what is left unsaid --


def test_a_complete_answer_says_nothing():
    message = _message(40)
    assert rc.shortfall(message, _answer(1, 40)) is None


def test_rewording_below_the_noise_bar_says_nothing():
    """THE FALSE ALARM THIS BAR EXISTS FOR. Measured 2026-09-21 on a live
    rewrite that carried all 210 of its items (verified by a marker planted in
    each one): 16 of 210 anchors, 7.6%, had been reworded away. A scattered
    shortfall must be far above that before anything is said."""
    message = _message(40)
    reworded = _answer(1, 40)
    for i in (5, 12, 27):  # 3 of 40 = 7.5%, the measured noise
        reworded = reworded.replace(SUBJECTS[i % len(SUBJECTS)].title(), "Unit")
    assert rc.shortfall(message, reworded) is None


def test_a_truncated_rewrite_names_the_item_it_reached():
    message = _message(40)
    short = rc.shortfall(message, _answer(1, 28))
    assert short is not None
    assert short.tail is True
    assert short.measurable == 40  # every item of this source can be checked
    assert short.covered_through == 28
    assert short.missing == 12
    assert "Nothing after item 28 of the 40 items you pasted" in short.note
    assert "carry on from item 29" in short.note


def test_a_tail_smaller_than_the_bar_is_not_worth_a_notice():
    """39 of 40: one item short is a rewording, not a truncation."""
    assert rc.shortfall(_message(40), _answer(1, 39)) is None


def test_scattered_losses_above_the_bar_are_reported():
    message = _message(40)
    kept = _answer(1, 40)
    # Every second item removed, and the last item kept: not a tail.
    dropped = "\n".join(
        ln for i, ln in enumerate(kept.split("\n")) if i < 2 or i % 2 == 0
    )
    short = rc.shortfall(message, dropped)
    assert short is not None
    assert short.tail is False
    assert short.missing >= rc.SCATTERED_SHARE * short.measurable
    assert f"of the {short.items} items in the text you pasted" in short.note


def test_nothing_is_said_about_a_turn_that_is_not_a_countable_rewrite():
    assert rc.shortfall(_message(40, SUMMARISE_ASK), "A short summary.") is None
    assert rc.shortfall("Write me a poem about rain.", "Rain.") is None


def test_the_note_never_claims_more_than_was_measured():
    """`missing` counts items whose own word is absent — a floor, never an
    estimate of the rest."""
    short = rc.shortfall(_message(40), _answer(1, 20))
    assert short is not None
    assert short.missing <= short.measurable <= short.items
    assert short.as_meta() == {
        "items": 40,
        "measurable": short.measurable,
        "missing": short.missing,
        "covered_through": 20,
    }


# ------------------------------------------------------------- the prompt --


def _system_for(message: str) -> str:
    return chat_engine._messages(message, [], "assistant")[0]["content"]


def test_the_clause_is_in_the_prompt_for_a_countable_rewrite():
    assert "COVER THE WHOLE SOURCE" in _system_for(_message(40))


def test_the_clause_is_in_the_salesforce_prompt_too():
    system = chat_engine._messages(_message(40), [], "salesforce")[0]["content"]
    assert "COVER THE WHOLE SOURCE" in system


@pytest.mark.parametrize(
    "message",
    [
        "Hello there.",
        "Write me a poem about rain.",
        _message(40, SUMMARISE_ASK),
        _message(rc.MIN_ITEMS - 1),
    ],
)
def test_no_other_turn_pays_for_the_clause(message):
    assert "COVER THE WHOLE SOURCE" not in _system_for(message)


def test_the_small_talk_lane_is_untouched():
    lane = chat_engine._messages("hi", [], "assistant", lane="greeting")[0]["content"]
    assert "COVER THE WHOLE SOURCE" not in lane


# ------------------------------------------------------------- the engine --


def _run_chat(message: str, *, effort: str = "fast", mode: str = "assistant"):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    answer = asyncio.run(
        chat_engine.run_chat_engine(
            message, [], emit, mode=mode, model_choice="smart", effort=effort
        )
    )
    return answer, events


def _streamed(events) -> str:
    return "".join(data["text"] for kind, data in events if kind == "token")


def _meta(events) -> dict:
    return next(data for kind, data in events if kind == "meta")


def test_a_half_written_rewrite_says_so_in_the_answer_the_person_reads(engine):
    """The run ends stop_reason=complete, truncated=false — the model stopped
    of its own accord — so only a check against the source can say this."""
    engine(FakeStream([_chunk(content=_answer(1, 28), finish="stop")]))

    answer, events = _run_chat(_message(40))

    assert "Nothing after item 28 of the 40 items you pasted" in answer
    # Streamed, stored and reported: a reload must not show a finished rewrite.
    assert _streamed(events) == answer
    assert _meta(events)["coverage"] == {
        "items": 40,
        "measurable": 40,
        "missing": 12,
        "covered_through": 28,
    }


def test_a_complete_rewrite_keeps_the_meta_and_the_text_it_always_had(engine):
    engine(FakeStream([_chunk(content=_answer(1, 40), finish="stop")]))

    answer, events = _run_chat(_message(40))

    assert "not complete" not in answer
    assert answer == _answer(1, 40)
    assert _meta(events) == {"route": "chat"}


def test_an_ordinary_turn_is_measured_at_all(engine):
    engine(FakeStream([_chunk(content="Rain falls.", finish="stop")]))

    answer, events = _run_chat("Write me a poem about rain.")

    assert answer == "Rain falls."
    assert _meta(events) == {"route": "chat"}
