"""Notes about how a file was made stay on the record; a reader gets the plain part.

THE FAILURE THIS PINS (hotfix 1.1 live replay, 2026-09-19). The owner's three
replies read out four notes written for operators, verbatim:

- "Not confirmed in the file: not met: the rest of the document unchanged —
  sections the request did not name changed or disappeared: ['Company and
  Contact Information Analysis', …]" (the self-check's evidence, a list repr)
- "no category or series is called '2020', '2021', '2022', so that colour
  was not used" (the chart colour map's missed keys)
- "a pie shows at most 7 slices, so 83 of the 89 values of Company would
  disappear into one Other wedge, so the data is drawn as a horizontal bar:
  …" (the chart chooser's type substitution)
- "written as a long document: “Big” was read as about 3,000 words, written
  in 8 sections over 9 model calls" (the sectioned writer's call count)

The version row and the log keep every one as written. The reply and the card
say at most one short plain line for each, and nothing for the two that do
not help the person.
"""
from __future__ import annotations

import logging

from app.artifacts import types as T
from app.engines import artifact as engine

UNMET = ("not met: the rest of the document unchanged — sections the request did not name changed or "
         "disappeared: ['Company and Contact Information Analysis', 'Website and Digital Presence Insights']")
COLOUR = "no category or series is called '2020', '2021', '2022', so that colour was not used"
PIE = ("a pie shows at most 7 slices, so 83 of the 89 values of Company would disappear into one Other wedge, so the "
       "data is drawn as a horizontal bar: Company has 89 values, too many for a pie or a vertical axis, so they are "
       "ranked by the row count")
LONG = ("written as a long document: “Big” was read as about 3,000 words, written in 8 sections over 9 model calls")
PLAIN = "no numeric column was named, so rows were counted"

OPERATOR_WORDING = ("not met:", "did not name changed or disappeared", "['", "so that colour was not used",
                    "Other wedge", "so the data is drawn as", "model calls")


def _ref(warnings):
    return T.ArtifactRef(artifact_id="a" * 32, version=2, job_id="j" * 32, title="Customer Subscription Analysis Report",
                         kind="document", status="completed_with_warnings",
                         files=[T.FileRef(format="docx", filename="r.docx", mime_type="application/vnd.openxmlformats-"
                                          "officedocument.wordprocessingml.document", size=10)], warnings=list(warnings))


def _clean(text: str) -> None:
    for bit in OPERATOR_WORDING:
        assert bit not in text, (bit, text)


def test_the_create_sentence_says_only_the_plain_parts():
    line = engine._sentence(_ref([LONG, PIE, COLOUR, PLAIN]), "create", [LONG, PIE, COLOUR, PLAIN])
    _clean(line)
    assert "“Big” was read as about 3,000 words" in line, "why it took minutes is worth one line"
    assert PLAIN in line


def test_the_edit_sentence_says_what_was_not_confirmed_in_one_plain_line():
    line = engine._edit_sentence("Customer Subscription Analysis Report", 2, ["3 charts added"], [], unmet=[UNMET],
                                 warnings=[PLAIN, COLOUR, UNMET])
    _clean(line)
    assert line.count("Not confirmed in the file") == 1, line
    assert "Not confirmed in the file: the rest of the document unchanged." in line, line
    assert "not confirmed in the file: the rest of the document unchanged" not in line, "said once, not again as a warning"


def test_the_card_reads_the_same_plain_parts():
    said = _ref([UNMET, COLOUR, PIE, LONG, PLAIN]).to_json()["warnings"]
    _clean(" ".join(said))
    assert "not confirmed in the file: the rest of the document unchanged" in said
    assert any(s.startswith("written as a long document: “Big” was read as about 3,000 words") for s in said), said
    assert PLAIN in said


def test_the_version_record_keeps_every_note_as_written():
    ref = _ref([UNMET, COLOUR, PIE, LONG])
    ref.to_json()
    assert ref.warnings == [UNMET, COLOUR, PIE, LONG], "the row keeps the operator wording"


def test_the_hidden_notes_are_logged_for_operators(caplog):
    ref = _ref([COLOUR, PIE, PLAIN])
    with caplog.at_level(logging.INFO, logger=engine.log.name):
        engine._log_operator_notes(ref, ref.warnings)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "so that colour was not used" in logged and "so the data is drawn as" in logged, logged
    assert PLAIN not in logged
