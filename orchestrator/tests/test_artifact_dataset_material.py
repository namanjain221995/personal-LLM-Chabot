"""A dataset uploaded in an EARLIER turn is material for this turn's file.

THE FAILURE THIS PINS (owner report, 2026-09-17). A CSV was uploaded, then
"Big report" was asked for in the next turn. `material_in.gather` read the
conversation's dataset workspace only when `intent.target == "upload"`, and
that target is set only when a file is attached to THIS turn — so the report
was composed with `tables=[]`, every chart the model bound to the filename
became a "the table … is not available" callout, and the PNG-only follow-up
failed outright. Nothing in the pipeline had the rows the person had given it.

The uploads row and the extracted file are authored here exactly as
`app/uploads._finalise_dataset` writes them (status "ready", notes empty, the
bytes under `<workspace>/uploads/<conv>/<upload id>/extracted/<name>`), so
what these tests drive is the production shape, not a stub.
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from app import db
from app.artifacts import intent as I
from app.artifacts import material_in as M

FIXTURE = Path(__file__).parent / "fixtures" / "artifacts" / "customers_100.csv"
UPLOAD_ID = "0123456789abcdef0123456789abcdef"
SECOND_UPLOAD_ID = "fedcba9876543210fedcba9876543210"


def _fixture_rows() -> list:
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _dataset_upload(workspace: Path, conv: str, *, upload_id: str = UPLOAD_ID, filename: str = "customers-100.csv",
                    status: str = "ready", notes=None, profile=None, write_file: bool = True,
                    body: bytes = None) -> None:
    """One upload as the dataset rail leaves it: a row plus the extracted file."""
    if write_file:
        root = workspace / "uploads" / conv / upload_id / "extracted"
        root.mkdir(parents=True, exist_ok=True)
        (root / filename).write_bytes(body if body is not None else FIXTURE.read_bytes())
    db.save_upload(upload_id, conv, filename, 4096, status,
                   json.dumps(profile) if profile is not None else None, notes)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    return tmp_path


def _gather(conv, workspace, intent, text="Big report"):
    return asyncio.run(M.gather(history=[], conversation_id=conv, workspace=str(workspace), intent=intent,
                                text=text, save_documents=False))


def test_a_dataset_uploaded_in_an_earlier_turn_is_material_for_a_create(workspace):
    conv = "conv-dataset-create"
    _dataset_upload(workspace, conv)
    # What verdict_to_intent returns for "Big report" with nothing attached:
    # a create made from the CONVERSATION, never from an "upload".
    intent = I.ArtifactIntent("create", target="conversation", instruction="Big report")
    g = _gather(conv, workspace, intent)
    assert [t.id for t in g.upload_tables] == ["upload1"], "the conversation's CSV is the turn's material"
    table = g.upload_tables[0]
    assert table.title == "customers-100.csv"
    assert len(table.rows) == 100 and table.columns == _fixture_rows()[0]
    assert table.rows[0][6] == "Aurelia", "the real cells, not a profile summary"


def test_an_edit_turn_gathers_the_conversation_dataset(workspace):
    conv = "conv-dataset-edit"
    _dataset_upload(workspace, conv)
    intent = I.ArtifactIntent("edit", target="artifact", instruction="also i want Plots on this docs")
    g = _gather(conv, workspace, intent, text="also i want Plots on this docs")
    assert [t.id for t in g.upload_tables] == ["upload1"] and len(g.upload_tables[0].rows) == 100


def test_a_missing_file_uses_profile_full_rows_and_an_expired_upload_is_skipped_with_a_note(workspace):
    conv = "conv-dataset-gone"
    profile = [{"file": "small.csv", "kind": "table", "rows": 2, "full_content": True,
                "columns": [{"name": "Team"}, {"name": "Wins"}],
                "full_rows": [{"Team": "Red", "Wins": "3"}, {"Team": "Blue", "Wins": ""}]}]
    # The bytes were swept by the TTL; the profile kept the whole file.
    _dataset_upload(workspace, conv, filename="small.csv", profile=profile, write_file=False)
    # …and a second upload that never became a dataset at all.
    _dataset_upload(workspace, conv, upload_id=SECOND_UPLOAD_ID, filename="broken.csv", status="failed",
                    notes="UnicodeDecodeError", write_file=False)
    intent = I.ArtifactIntent("create", target="conversation", instruction="chart it")
    g = _gather(conv, workspace, intent, text="chart it")
    assert [t.id for t in g.upload_tables] == ["upload1"]
    assert g.upload_tables[0].rows == [["Red", "3"], ["Blue", None]], "the rows kept with the upload stand in"
    assert any("broken.csv could not be used" in n and "failed" in n for n in g.notes), g.notes
    assert any("small.csv is no longer stored" in n for n in g.notes), g.notes


def test_a_sampled_profile_is_not_a_dataset(workspace):
    """GUARD (kept from the deleted `engine._upload_tables`). A profile that
    only SAMPLED a big file must never stand in for it: a report written over
    5 of 5,000 rows is wrong in a way nobody can see."""
    conv = "conv-dataset-sampled"
    profile = [{"file": "big.csv", "kind": "table", "rows": 5000, "columns": [{"name": "k"}],
                "sample_rows": [{"k": "x"}]}]
    _dataset_upload(workspace, conv, filename="big.csv", profile=profile, write_file=False)
    intent = I.ArtifactIntent("create", target="conversation", instruction="Big report")
    g = _gather(conv, workspace, intent)
    assert g.upload_tables == []
    assert any("big.csv is no longer stored" in n for n in g.notes), g.notes


def test_export_and_previous_answer_targets_do_not_load_datasets(workspace):
    """GUARD. An export is the previous ANSWER, converted as written, and a
    previous_answer target is the same thing said another way: pulling a CSV
    into either would put rows in a file the person asked to be a copy."""
    conv = "conv-dataset-guard"
    _dataset_upload(workspace, conv)
    export = I.ArtifactIntent("export", target="conversation", instruction="give it in docs")
    assert _gather(conv, workspace, export, text="give it in docs").upload_tables == []
    prev = I.ArtifactIntent("create", target="previous_answer", instruction="put that in a pdf")
    assert _gather(conv, workspace, prev, text="put that in a pdf").upload_tables == []
    convert = I.ArtifactIntent("convert", target="artifact", instruction="convert it to pdf")
    assert _gather(conv, workspace, convert, text="convert it to pdf").upload_tables == []


def test_a_document_or_video_upload_is_never_read_as_a_dataset(workspace):
    """GUARD. The document rail writes notes "document" and video/api writes
    "video" on rows whose filename may still end in .csv."""
    conv = "conv-dataset-notes"
    _dataset_upload(workspace, conv, filename="handbook.csv", notes="document")
    _dataset_upload(workspace, conv, upload_id=SECOND_UPLOAD_ID, filename="clip.csv", notes="video")
    intent = I.ArtifactIntent("create", target="conversation", instruction="Big report")
    assert _gather(conv, workspace, intent).upload_tables == []


def test_the_dataset_the_words_name_is_read_first(workspace):
    conv = "conv-dataset-named"
    _dataset_upload(workspace, conv, filename="customers-100.csv")
    _dataset_upload(workspace, conv, upload_id=SECOND_UPLOAD_ID, filename="orders.csv",
                    body=b"Order,Amount\nA-1,10\nA-2,20\n")
    intent = I.ArtifactIntent("create", target="conversation", instruction="chart the customers-100 file")
    g = _gather(conv, workspace, intent, text="chart the customers-100 file")
    assert [t.title for t in g.upload_tables] == ["customers-100.csv", "orders.csv"]
    # With no name in the words the most recent upload is read first.
    plain = _gather(conv, workspace, intent, text="Big report")
    assert [t.title for t in plain.upload_tables] == ["orders.csv", "customers-100.csv"]


def test_an_attachment_on_this_turn_still_wins(workspace):
    """GUARD. The conversation's workspace is read only when nothing is
    attached: a file the person just sent is the material, not an older one."""
    import base64

    conv = "conv-dataset-attached"
    _dataset_upload(workspace, conv)
    b64 = base64.b64encode(b"Region,Sales\nNorth,120\nSouth,95\n").decode()
    intent = I.ArtifactIntent("create", target="upload", instruction="chart this")
    g = asyncio.run(M.gather(history=[], pdf_uploads=[("sales.csv", b64)], conversation_id=conv,
                             workspace=str(workspace), intent=intent, text="chart this", save_documents=False))
    assert [t.title for t in g.upload_tables] == ["sales.csv"]


def test_the_row_budget_and_the_file_cap_still_hold(workspace):
    conv = "conv-dataset-budget"
    body = b"A,B\n" + b"".join(f"{i},{i}\n".encode() for i in range(50))
    for n in range(7):
        _dataset_upload(workspace, conv, upload_id=f"{n:032x}", filename=f"part{n}.csv", body=body)
    intent = I.ArtifactIntent("create", target="conversation", instruction="Big report")
    g = _gather(conv, workspace, intent)
    assert len(g.upload_tables) == M._MAX_UPLOADS == 5
    assert [t.id for t in g.upload_tables] == [f"upload{i + 1}" for i in range(5)]


def test_the_reader_runs_off_the_event_loop(workspace):
    """The 200,000-row budget is CPU work; it must not block the loop."""
    import threading
    import time

    conv = "conv-dataset-thread"
    seen = {}
    real = M.read_csv_bytes

    def watched(raw, **kw):
        seen["thread"] = threading.current_thread().name
        time.sleep(0.05)
        return real(raw, **kw)

    async def drive():
        gaps = [0.0]

        async def beat():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        pulse = asyncio.create_task(beat())
        try:
            g = await M.gather(history=[], conversation_id=conv, workspace=str(workspace),
                               intent=I.ArtifactIntent("create", target="conversation"), text="x", save_documents=False)
        finally:
            pulse.cancel()
        return g, max(gaps)

    _dataset_upload(workspace, conv)
    M.read_csv_bytes = watched  # noqa: SLF001 — the module's own reader, restored below
    try:
        g, worst = asyncio.run(drive())
    finally:
        M.read_csv_bytes = real
    assert len(g.upload_tables[0].rows) == 100
    assert seen["thread"] != "MainThread", "the reader ran in a worker thread"
    assert worst < 0.25, f"the loop stalled for {worst:.3f}s while the CSV was read"


# ----------------------------------- the owner's second sentence (A-F1) --


def test_a_convert_that_asks_for_a_chart_loads_the_conversations_dataset(workspace):
    """THE OWNER'S SECOND REPORTED SENTENCE. "also i want Plots on this docs",
    sent in the state he sent it (the report's card was the last turn), is read
    by the rules as a FORMAT CONVERSION — measured on the integrated tree
    (e543bef, 2026-09-18): action='convert', rule='convert-artifact-turn',
    formats=['docx'], chart_request=True. A conversion re-renders the stored
    spec, and this gate refused the data to every convert, so no CSV was read
    and no plot could be drawn. Nothing else could rescue it either:
    `intent._should_consult` is False for that verdict, so the LLM classifier
    is never asked.
    """
    conv = "conv-dataset-plots"
    _dataset_upload(workspace, conv)
    intent = I.decide("also i want Plots on this docs", has_artifacts=True, artifact_hints=["Customer Report"],
                      has_assistant_answer=True, last_turn_is_artifact=True)
    assert (intent.action, intent.chart_request) == ("convert", True), (intent.action, intent.rule)
    assert M.wants_conversation_datasets(intent) is True
    g = _gather(conv, workspace, intent, text="also i want Plots on this docs")
    assert [t.id for t in g.upload_tables] == ["upload1"], "the dataset the plot must be drawn from"
    assert len(g.upload_tables[0].rows) == 100


def test_a_conversion_that_says_nothing_about_charts_still_loads_nothing(workspace):
    """GUARD for the rule above. "also give me it as a PDF" is a real
    conversion (chart_request False): it re-renders the stored spec and needs
    no material, so the CSVs stay unread."""
    conv = "conv-dataset-pdf"
    _dataset_upload(workspace, conv)
    intent = I.decide("also give me it as a PDF", has_artifacts=True, artifact_hints=["Customer Report"],
                      has_assistant_answer=True, last_turn_is_artifact=True)
    assert (intent.action, intent.chart_request) == ("convert", False), (intent.action, intent.rule)
    assert M.wants_conversation_datasets(intent) is False
    assert _gather(conv, workspace, intent, text="also give me it as a PDF").upload_tables == []


def test_an_ordinary_english_word_does_not_name_a_file(workspace):
    """A-F8. The stem match is a >= 4-character substring, so the ordinary
    word "report" in "please give Big report" read report.csv as the file the
    person NAMED and read it before the newest upload. Both files are still
    read, but the first one becomes upload1 — the table the composer and
    `add_chart` bind to first, and the one that wins the shared row budget."""
    assert M._names_file("I want proper this data understand ?? please give Big report ??", "report.csv") is False
    assert M._names_file("chart the sales.csv", "sales.csv") is True
    assert M._names_file("chart the customers-100 file", "customers-100.csv") is True

    conv = "conv-dataset-word"
    _dataset_upload(workspace, conv, filename="report.csv", body=b"A,B\n1,2\n")
    _dataset_upload(workspace, conv, upload_id=SECOND_UPLOAD_ID, filename="customers-100.csv")
    g = _gather(conv, workspace, I.ArtifactIntent("create", target="conversation", instruction="Big report"),
                text="please give Big report")
    assert [t.title for t in g.upload_tables] == ["customers-100.csv", "report.csv"], \
        "the newest upload is upload1; an English word does not promote report.csv"
