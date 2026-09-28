"""Two ways the export rule failed a person writing romanised Gujarati.

Both measured on the 1,553-turn understanding corpus, in context PA — "a
substantial assistant ANSWER precedes this turn; no artifact exists" — where
the right decision is to export that answer to a file.

1. "aapo ye data export karo."  (give this data — export it)
   normalises to "_give_ _this_ data export _give_ ." — a reference and a
   keeping verb, exactly what `export-keep-verb` wants. It was vetoed by
   `_UPLOAD_SOURCE_RE`, which reads "this data" as "the upload named as the
   source". There was no upload. The veto now needs one.

2. "shu report che, export karo."  (what report is it — export it)
   has no pronoun at all, so `ref` was false and the turn became a brand-new
   file. That is not the person's mistake: Hindi and Gujarati DROP the object
   pronoun. "export karo" means "export it"; English has to say "export this".

MEASURED: gujlish 95% -> 98% (84 -> 86 of 88), TOTAL 1,523 -> 1,525, every
other language unmoved. The 852-row intent probe: GAINED_FILE 0, LOST_FILE 0,
RELABEL 0.
"""
from __future__ import annotations

import pytest

from app.artifacts import intent as I

#: The corpus's PA context: an answer in the room, no file yet.
PA = dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=True, artifact_hints=())


@pytest.mark.parametrize("text", [
    "aapo ye data export karo.",
    "shu report che, export karo.",
    "ye export karo",
    "isko save kar do",
    "download karo",
])
def test_an_indic_export_with_an_answer_in_the_room_exports_it(text):
    intent = I.decide(text, **PA)
    assert intent.action == "export", (text, intent.action, intent.rule)
    assert intent.reference == "previous_answer"


def test_the_upload_veto_still_holds_when_there_is_an_upload():
    """THE OTHER HALF. "this data" IS the upload when one is attached, and
    "download the data I attached" is a read of it, not an export of the
    answer. Only the no-upload case changed."""
    for text in ("aapo ye data export karo.", "download the data I attached"):
        intent = I.decide(text, **PA, upload_formats=("xlsx",))
        assert intent.action != "export", (text, intent.action, intent.rule)


def test_the_pro_drop_arm_never_fires_on_english():
    """`_give_` is emitted only by lexicon.normalize from Indic or romanised
    Indic input, so an English sentence cannot reach the new arm. English
    exports keep the rules they always had."""
    from app.artifacts import lexicon as LX

    for text in ("export this", "export it to excel", "please save that", "download it"):
        assert not I._PRO_DROP_KEEP_RE.search(LX.normalize(text)), text


@pytest.mark.parametrize("text,rule", [
    ("export this", "export-keep-verb"),
    ("export it to excel", "export-followup"),
    ("put that in a pdf", "export-followup"),
])
def test_english_exports_are_unchanged(text, rule):
    intent = I.decide(text, **PA)
    assert (intent.action, intent.rule) == ("export", rule), (text, intent.rule)


def test_with_no_answer_in_the_room_there_is_nothing_to_export():
    """The caller requires an answer; a bare "export karo" in an empty chat
    must not become an export of nothing."""
    empty = dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=False, artifact_hints=())
    assert I.decide("export karo", **empty).action != "export"


def test_a_caller_that_does_not_say_keeps_the_old_veto():
    """`has_upload` defaults to True, so any caller not updated keeps the
    stricter rule rather than silently losing it."""
    from app.artifacts import lexicon as LX

    low = LX.normalize("aapo ye data export karo.")
    assert I._export_shape(low, []) is None
    assert I._export_shape(low, [], has_upload=False) == "export-keep-verb"
