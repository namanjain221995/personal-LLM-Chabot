"""B8b adversarial QA (2026-09-18): phrasings where the words name the PHOTO
yet `image_turn` keeps the export of the previous answer. Measured live on
5a6c6c5 (Fast, in-process): "make this into an excel file with the totals row
above the data" + an unreadable photo built a "Regional Sales Q2" workbook
from the previous answer, and "make an excel file of this photo, not the
answer" + the readable stock photo built a "Regional Sales" workbook with 0 of
36 stock cells. A bare "above" (a layout word) and a NEGATED "the answer"
are not the person naming the earlier answer as the source."""
from __future__ import annotations

import pytest

from app.artifacts import intent as I
from app.artifacts import material_in


@pytest.mark.parametrize("words", [
    "make this into an excel file with the totals row above the data",
    "convert this receipt to excel, keep the date above the items",
    "make an excel file of this photo, not the answer",
    "make an excel file from this photo instead of the answer",
])
def test_words_that_name_the_photo_make_the_photo_the_material(words):
    intent = I.ArtifactIntent("export", formats=["xlsx"], target="previous_answer", rule="export-followup")
    out, reads = material_in.image_turn(intent, words, ["jpg"])
    assert reads is True, words
    assert (out.action, out.target) == ("create", "upload")


@pytest.mark.parametrize("words", [
    "put your previous answer into an excel file",
    "make the table above into an excel file",
    "export the answer above as excel",
    "turn your last reply into a pdf",
])
def test_words_that_name_the_earlier_answer_still_keep_the_export(words):
    """What must NOT change: the earlier answer, named, is still the source."""
    intent = I.ArtifactIntent("export", formats=["xlsx"], target="previous_answer", rule="export-followup")
    out, reads = material_in.image_turn(intent, words, ["jpg"])
    assert reads is False and out is intent, words
