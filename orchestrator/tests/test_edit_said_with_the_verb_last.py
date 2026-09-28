"""An instruction with the verb LAST is still an instruction.

Hindi and Gujarati put the verb at the end, and `lexicon.normalize` already
carries that across:

    "colors edit karo."              -> "color update"
    "फील में कलर बदल दो"              -> "फील _in_ कलर change"
    "new column add karo excel me."  -> "new column add excel _in_"

`_IMPERATIVE_EDIT_RE` then missed every one of them, because it anchors the
verb at the START of the line. With a file already open, a person typing a
four-word instruction in their own word order was told there was no request
— `action=none`, `rule=no-request`, nothing made, nothing changed.

MEASURED on the 1,553-turn understanding corpus, before and after: gujlish
94% -> 95%, hi 97% -> 98%, `no_file` harms 9 -> 7, total 1,521 -> 1,523,
with English (921/935) and hinglish (211/214) unmoved. The 852-row intent
probe against dev is GAINED_FILE 0, LOST_FILE 0, RELABEL 0.
"""
from __future__ import annotations

import pytest

from app.artifacts import intent as I

#: A file the person is already looking at — the context every one of these
#: turns arrives in.
OPEN = dict(has_artifacts=True, has_assistant_answer=True)


@pytest.mark.parametrize("text", [
    "colors edit karo.",            # gujlish
    "फील में कलर बदल दो",             # hindi
    "font size change karo",        # hinglish
    "heading update karo",
])
def test_an_instruction_with_the_verb_last_edits_the_file(text):
    intent = I.decide(text, **OPEN)
    assert intent.action == "edit", (text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    "add a summary section",
    "change the heading colour",
    "remove the last row",
])
def test_the_verb_first_form_is_unchanged(text):
    assert I.decide(text, **OPEN).action == "edit", text


@pytest.mark.parametrize("text", [
    "the report is out of date",         # a remark: the verb is not final
    "i need to update my calendar",      # not about the file, and not final
])
def test_a_remark_is_still_not_an_edit(text):
    intent = I.decide(text, **OPEN)
    assert intent.action != "edit", (text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    "what should i change about it",
    "update",
])
def test_the_new_arm_claims_nothing_the_old_rules_already_decided(text):
    """BOTH OF THESE WERE ALREADY `edit` BEFORE THIS CHANGE, by other rules —
    checked against dev, character for character. They are here so that a
    later reading of this file does not credit (or blame) the verb-last arm
    for them: it does not fire on either."""
    from app.artifacts.intent import _SOV_EDIT_RE
    from app.artifacts import lexicon as LX

    assert _SOV_EDIT_RE.search(LX.normalize(text)) is None, (
        f"the verb-last arm should not be what decides {text!r}"
    )


def test_the_arm_needs_something_in_front_of_the_verb():
    """A bare verb names no object, so it is not an instruction ABOUT
    anything — whatever the older rules make of it, this arm stays out."""
    from app.artifacts.intent import _SOV_EDIT_RE

    assert _SOV_EDIT_RE.search("color update") is not None
    assert _SOV_EDIT_RE.search("update") is None


@pytest.mark.parametrize("text", [
    "give me bullet points on climate change",   # the AS3 verifier case CI caught
    "give me the latest software update",        # language_of calls this Hinglish ("me")
    "how to do a quick fix",                     # also "Hinglish" ("do", "to")
    "write a note on the policy change",
    "summarise the price change",
])
def test_an_english_sentence_ending_in_a_noun_is_not_a_verb_last_edit(text):
    """In English the word after the object is a NOUN: "climate change",
    "software update", "quick fix". The first of these was an edit of the
    open file on b010719e (CI shard 3, test_artifact_as3_verifier.py:141),
    because the verb-last arm read `climate change` as <object> <verb>. The
    arm now needs verb-last grammar in the person's own words."""
    intent = I.decide(text, artifact_hints=["Sales Report"], **OPEN)
    assert intent.action != "edit", (text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    "heading remove kar do",      # the light verb survives normalize here
    "title change karo",
    "chart ma color badlo",
])
def test_a_light_verb_is_the_evidence_of_verb_last_grammar(text):
    intent = I.decide(text, **OPEN)
    assert intent.action == "edit", (text, intent.action, intent.rule)


def test_the_rule_only_fires_on_a_short_message():
    """The caller bounds this at 12 words. A long sentence that happens to end
    in an edit verb is prose, not an instruction."""
    long_one = " ".join(["the quarterly report covers many regions and several product lines"] * 2) + " update"
    assert len(long_one.split()) > 12
    assert I.decide(long_one, **OPEN).action != "edit"
