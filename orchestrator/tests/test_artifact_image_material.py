"""An image attached to a file request is the file's material (B8b).

WHAT WAS WRONG (4810da0). The artifact branch in main.py runs above the image
route, built the intent's upload names and formats from `pdf_uploads` and
`pdf_filename` only, and `material_in.gather` had no image input at all. So
"make this table into an excel file" with a photo of the table attached was
read as an EXPORT of the previous answer: the workbook was built from
whatever the assistant said last, and nothing said the photo was ignored.

WHAT IS PINNED HERE, through the real /chat route with the composer and the
renderer stubbed and the model call that reads the image stubbed at
`llm.chat_completion`:

1. the image is part of the intent's inputs (its format and a name);
2. an image turn with a create intent reads the image and hands its text to
   `gather`, whose tables become the upload tables the workbook is built
   from — the turn is a create from the upload, not an export;
3. an image nothing could be read from never yields a file built from the
   previous answer: the turn says so in one sentence and opens no job;
4. words that name the earlier answer keep the export (the image is not
   read), and a question about an image still goes to the vision engine.

ROUND 2 (QA, 2026-09-18, against 5a6c6c5), pinned at the end of this file:
an honest repetitive table is not a model loop; "above", "the answer" and
"this chat" inside words about the photo do not hand the turn to the previous
answer; a brief in the words makes its file when the photo has no text; the
refusal is decided on the documents that RESOLVED; the transcript is never
stored and at most two reads run at once.

REPAIR ROUND 1 (2026-09-19), at the end: a source noun located IN the photo
("the last table in this photo") names the photo; a brief keeps the
conversation even when its photo is read; the paper the words name, or a
spreadsheet request, is never a brief; a "|" printed inside a cell stays in
its cell, and extra cells the model added are not joined into one.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import llm, metrics
from app import main as app_main
from app.artifacts import material_in
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.main import _live_generations, app

HEADER = ["SKU", "Item", "On hand", "Counted"]
ROWS = [
    ["AB-1021", "Hex bolt M8", "240", "236"],
    ["CR-2255", "Cable reel 50m", "18", "53"],
    ["DL-3107", "Door lock set", "64", "64"],
    ["EL-5533", "LED panel 60x60", "97", "76"],
    ["FT-4410", "Floor tile grey", "1,250", "1,244"],
    ["GS-7002", "Glass shelf 80cm", "32", "30"],
    ["HP-6118", "Hinge pack (10)", "415", "420"],
    ["PV-8841", "PVC pipe 3m", "128", "128"],
]
#: What the main model returned for the synthetic stock-table photo, both
#: live runs (2026-09-18, Fast, thinking off): 36 of 36 cells exact.
TABLE_MD = "Warehouse B - stock count, 14 Sep\n\n" + "\n".join(
    ["| " + " | ".join(HEADER) + " |", "|---|---|---|---|", *("| " + " | ".join(r) + " |" for r in ROWS)]
)

#: The earlier answer the old code exported instead of the photo.
PREVIOUS_ANSWER = (
    "## Regional sales, Q2\n\n| Region | Sales |\n|---|---|\n| North | 120 |\n| South | 95 |\n| East | 143 |\n\n"
    "North and East grew; South fell for the second quarter in a row, mostly on lower repeat orders."
)
HISTORY = [
    {"role": "user", "content": "give me the regional sales for Q2"},
    {"role": "assistant", "content": PREVIOUS_ANSWER},
]


def _jpeg_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (240, 240, 240)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


IMG = _jpeg_b64()


# --------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    monkeypatch.setattr(settings, "artifact_intent_llm_enabled", False)
    monkeypatch.setattr(app_main, "_shutting_down", False)
    _live_generations.clear()
    pipeline.reset_for_tests()
    metrics.reset()

    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} bytes".encode() * 50
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1, "warnings": [],
                "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")

    async def vision_answer(messages, **kwargs):
        yield ("token", "The image shows a stock table.")

    monkeypatch.setattr(llm, "stream_chat_events", vision_answer)
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    pipeline.set_visual_reviewer(None)
    _live_generations.clear()
    metrics.reset()


@pytest.fixture
def capture():
    """A stub composer recording what the job was handed."""
    seen: dict = {}

    async def composer(ctx):
        seen["material"] = dict(ctx.material)
        seen["operation"] = ctx.operation
        seen["formats"] = list(ctx.formats)
        await ctx.progress_stage("intent", "done", "")
        return S.parse_body("workbook", {"title": "Stock count", "sheets": [
            {"name": "Data", "columns": [{"name": "A"}], "rows": [["x"]]}]})

    return composer, seen


@pytest.fixture
def reader(monkeypatch):
    """`llm.chat_completion` answering the image-read call only."""
    calls: list = []
    state = {"reply": TABLE_MD}

    async def completion(messages, **kwargs):
        has_image = any(
            isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages
        )
        calls.append({"has_image": has_image, "kwargs": dict(kwargs)})
        return state["reply"] if has_image else ""

    monkeypatch.setattr(llm, "chat_completion", completion)
    return calls, state


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _post(client, message, *, conv, history=HISTORY, images=(IMG,), **extra):
    body = {"messages": [*history, {"role": "user", "content": message}], "mode": "assistant", "conversation_id": conv,
            "intent_id": f"int-{conv}", "effort": "fast", **extra}
    if images:
        body["images"] = list(images)
    return client.post("/chat", json=body)


def _turn(client, composer, message, *, conv, **kw):
    pipeline.set_composer(composer)
    resp = _post(client, message, conv=conv, **kw)
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    tokens = "".join(d["text"] for k, d in events if k == "token")
    return final, tokens


def _table(material: dict, header):
    for t in material.get("tables") or []:
        if list(t.get("columns") or []) == list(header):
            return t
    return None


# ---------------------------------------------------- 1. the intent inputs --


def test_the_intent_gate_is_told_that_an_image_is_attached(capture, reader, monkeypatch):
    composer, _seen = capture
    from app.artifacts import intent as intent_rules

    got: dict = {}
    real = intent_rules.decide_with_hook

    async def spy(text, hook=None, **kw):
        got.update(kw)
        return await real(text, hook, **kw)

    monkeypatch.setattr(intent_rules, "decide_with_hook", spy)
    with TestClient(app) as client:
        _turn(client, composer, "make this table into an excel file", conv="img-formats")
    assert got.get("upload_formats") == ["jpg"], "the attached JPEG is one of the turn's uploads"


# --------------------------------------------- 2. the image is the material --


def test_an_image_turn_hands_the_image_text_to_gather_and_the_file_is_made_from_it(capture, reader):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this table into an excel file", conv="img-create")
    assert final["route"] == "artifact"
    assert [f["format"] for f in final["artifacts"][0]["files"]] == ["xlsx"]
    assert [c["has_image"] for c in calls] == [True], "the image was read once, by the main model"
    assert seen["operation"] == "create", "a file made FROM the photo, not an export of the last answer"
    table = _table(seen["material"], HEADER)
    assert table is not None, "the photo's table reached the job's material"
    assert table["rows"] == ROWS
    assert table["source_id"] == "upload_image"
    # The photo's table is the first table the composer is offered, and the
    # export instruction ("turn the previous answer into the file") is gone.
    assert list(seen["material"]["tables"][0]["columns"]) == HEADER
    assert not any("previous answer into the file" in n for n in seen["material"].get("notes") or [])
    # Live, with the answer's table offered as well, the workbook grew a
    # second sheet of it in both runs; the photo is the whole material.
    assert _table(seen["material"], ["Region", "Sales"]) is None
    assert "CR-2255" in seen["material"]["uploads_text"]


def test_the_image_read_is_fast_and_never_thinks(capture, reader):
    """Measured on the synthetic stock photo: thinking off read 36/36 cells in
    4.8-7.1 s; a transcription has nothing to reason about."""
    composer, _seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        _turn(client, composer, "make this table into an excel file", conv="img-fast")
    assert calls and calls[0]["kwargs"].get("thinking") is False
    assert calls[0]["kwargs"].get("temperature") == 0.0


# ------------------------------------------- 3. nothing readable, no file --


@pytest.mark.parametrize("reply", ["NO_READABLE_TEXT", "", "| ? | ? |\n|---|---|\n| ? | ? |", "nije " * 200],
                         ids=["sentinel", "empty", "only-question-marks", "loop"])
def test_an_unreadable_image_never_yields_a_file_built_from_the_previous_answer(capture, reader, reply):
    composer, seen = capture
    _calls, state = reader
    state["reply"] = reply
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this table into an excel file", conv="img-unreadable")
    assert "artifacts" not in final, "no file at all"
    assert seen == {}, "no job was composed"
    from app import db
    from app.artifacts import db as adb

    viewer = db.get_user_by_username("local")
    assert adb.list_conversation_jobs(int(viewer["id"]), "img-unreadable") == [], "no job row either"
    assert tokens == material_in.UNREADABLE_IMAGE_LINE
    assert tokens.count(". ") == 0 and tokens.endswith("."), "one sentence"
    assert "photo" in tokens and "couldn't read" in tokens


def test_one_unreadable_image_of_two_is_a_note_not_a_refusal(capture, reader, monkeypatch):
    composer, seen = capture
    answers = iter([TABLE_MD, "NO_READABLE_TEXT"])

    async def completion(messages, **kwargs):
        return next(answers)

    monkeypatch.setattr(llm, "chat_completion", completion)
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "make this table into an excel file", conv="img-partial", images=(IMG, IMG))
    assert final["route"] == "artifact" and final.get("artifacts")
    assert _table(seen["material"], HEADER) is not None
    assert any("image-2.jpg" in n and "no text" in n for n in seen["material"]["notes"])


def test_an_unreadable_image_beside_a_document_is_a_note_and_the_document_is_the_material(capture, reader, monkeypatch):
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"

    csv_text = "\n".join(",".join(f'"{c}"' for c in row) for row in [HEADER, *ROWS])

    async def resolved(request, conv):
        return [("stock.csv", base64.b64encode(csv_text.encode()).decode())], [], None

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "make this table into an excel file", conv="img-with-doc",
                               pdf_uploads=[{"upload_id": "0" * 32, "name": "stock.csv"}])
    assert final["route"] == "artifact" and final.get("artifacts"), "the document is still material"
    assert _table(seen["material"], HEADER) is not None
    assert any(n.startswith("image.jpg: no text") for n in seen["material"]["notes"])


def test_only_a_create_from_the_image_alone_is_refused():
    from app.artifacts import intent as I

    nothing = material_in.ImageReading(notes=["image.jpg: no text in it could be read, so it was not used."], total=1)
    create = I.ArtifactIntent("create", target="upload")
    assert material_in.image_refusal(create, nothing, has_documents=False) == material_in.UNREADABLE_IMAGE_LINE
    assert material_in.image_refusal(create, nothing, has_documents=True) == ""
    assert material_in.image_refusal(I.ArtifactIntent("edit", target="artifact"), nothing, has_documents=False) == ""
    read = material_in.ImageReading(texts=[("image.jpg", TABLE_MD)], total=1)
    assert material_in.image_refusal(create, read, has_documents=False) == ""
    assert material_in.image_refusal(create, None, has_documents=False) == ""


# ------------------------------------------------------ 4. what is kept --


def test_words_that_name_the_earlier_answer_keep_the_export(capture, reader):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "put your previous answer into an excel file", conv="img-export")
    assert final["route"] == "artifact"
    assert calls == [], "the image is not the material, so it is not read"
    assert _table(seen["material"], ["Region", "Sales"]) is not None


def test_a_question_about_an_image_still_goes_to_the_vision_engine(capture, reader):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "what does this table show?", conv="img-question", history=[])
    assert final["route"] == "vision"
    assert tokens == "The image shows a stock table."
    assert calls == [] and seen == {}


# ------------------------------------------------------------ the parts --


def test_gather_turns_image_text_into_material_like_a_document():
    got = asyncio.run(material_in.gather(history=HISTORY, image_texts=[("image.jpg", TABLE_MD)], save_documents=False))
    assert got.upload_names == ["image.jpg"]
    assert [t.columns for t in got.upload_tables] == [HEADER]
    assert got.upload_tables[0].rows == ROWS
    assert got.upload_tables[0].id == "upload1"
    assert got.upload_tables[0].source_id == "upload_image"
    assert got.uploads_text.startswith("Document: image.jpg\n")
    assert [d["kind"] for d in got.upload_docs] == ["image"]
    # The answer's own table is still there, after the upload's.
    assert [list(t.columns) for t in got.tables] == [HEADER, ["Region", "Sales"]]


def test_a_file_made_from_an_image_is_not_offered_the_previous_answers_tables():
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("create", target="upload")
    got = asyncio.run(material_in.gather(history=HISTORY, image_texts=[("image.jpg", TABLE_MD)], intent=intent,
                                         save_documents=False))
    assert [list(t.columns) for t in got.tables] == [HEADER]
    assert got.answer_tables == []


def test_gather_with_an_image_does_not_reach_for_the_conversations_datasets(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the conversation's datasets are not this turn's material")

    monkeypatch.setattr(material_in, "_workspace_tables", boom)
    monkeypatch.setattr(material_in, "_earlier_documents", boom)
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("create", target="upload")
    got = asyncio.run(material_in.gather(history=[], image_texts=[("image.jpg", TABLE_MD)], conversation_id="c1",
                                         workspace="/nonexistent", intent=intent, save_documents=False))
    assert len(got.upload_tables) == 1


def test_image_names_and_formats_come_from_the_bytes():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()
    assert material_in.image_names([IMG]) == ["image.jpg"]
    assert material_in.image_names([IMG, "data:image/png;base64," + png, png]) == ["image-1.jpg", "image-2.png", "image-3.png"]


@pytest.mark.parametrize("words,action,target,material", [
    ("make this table into an excel file", "export", "previous_answer", True),
    ("make an excel file from this photo", "create", "conversation", True),
    ("put your previous answer into an excel file", "export", "previous_answer", False),
])
def test_image_turn_retargets_a_create_or_export_to_the_upload(words, action, target, material):
    from app.artifacts import intent as I

    intent = I.ArtifactIntent(action, formats=["xlsx"], target=target, rule="r")
    out, reads = material_in.image_turn(intent, words, ["jpg"])
    assert reads is material
    if material:
        assert (out.action, out.target, out.new_artifact, out.upload_refs) == ("create", "upload", True, ["jpg"])
        assert out.formats == ["xlsx"] and out.rule == "r+image"
    else:
        assert out is intent


def test_image_turn_leaves_a_convert_alone_and_keeps_an_edit_an_edit():
    from app.artifacts import intent as I

    convert = I.ArtifactIntent("convert", formats=["pdf"], target="artifact")
    assert material_in.image_turn(convert, "also as pdf", ["jpg"]) == (convert, False)
    edit = I.ArtifactIntent("edit", target="artifact")
    assert material_in.image_turn(edit, "add the rows in this photo to the sheet", ["jpg"]) == (edit, True)


def test_read_images_text_strips_fences_and_drops_what_it_could_not_read(monkeypatch):
    replies = iter(["```markdown\n" + TABLE_MD + "\n```", "NO_READABLE_TEXT"])

    async def completion(messages, **kwargs):
        return next(replies)

    monkeypatch.setattr(llm, "chat_completion", completion)
    got = asyncio.run(material_in.read_images_text([IMG, IMG], ["image-1.jpg", "image-2.jpg"]))
    assert got.texts == [("image-1.jpg", TABLE_MD)]
    assert got.notes == ["image-2.jpg: no text in it could be read, so it was not used."]
    assert (got.failed, got.total) == (0, 2)


def test_a_read_that_failed_is_not_reported_as_an_illegible_photo(monkeypatch):
    """"Take a sharper photo" is the wrong advice when nobody read it."""
    async def completion(messages, **kwargs):
        raise RuntimeError("engine down")

    monkeypatch.setattr(llm, "chat_completion", completion)
    got = asyncio.run(material_in.read_images_text([IMG], ["image.jpg"]))
    assert got.texts == [] and (got.failed, got.total) == (1, 1)
    assert got.notes == ["image.jpg: reading it failed (RuntimeError), so it was not used."]
    assert got.refusal() == material_in.unreadable_image_line(1, failed=True)
    assert "again" in got.refusal() and "sharper" not in got.refusal()


def test_a_read_past_the_deadline_is_cancelled_and_counted_as_failed(monkeypatch):
    async def completion(messages, **kwargs):
        await asyncio.sleep(5)
        return TABLE_MD

    monkeypatch.setattr(llm, "chat_completion", completion)
    got = asyncio.run(material_in.read_images_text([IMG], ["image.jpg"], deadline_s=0.05))
    assert got.texts == [] and got.failed == 1
    assert "took longer" in got.notes[0]


def test_an_illegible_photo_and_several_photos_get_their_own_sentence():
    one = material_in.unreadable_image_line(1)
    assert one == material_in.UNREADABLE_IMAGE_LINE and "sharper" in one
    many = material_in.unreadable_image_line(3)
    assert "any of the attached photos" in many and "from them" in many
    for line in (one, many, material_in.unreadable_image_line(2, failed=True)):
        assert line.count(". ") == 0 and line.endswith(".")


def test_an_engine_error_on_the_image_read_makes_no_file_either(capture, monkeypatch):
    composer, seen = capture

    async def completion(messages, **kwargs):
        raise RuntimeError("engine down")

    monkeypatch.setattr(llm, "chat_completion", completion)
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this table into an excel file", conv="img-engine-down")
    assert "artifacts" not in final and seen == {}
    assert tokens == material_in.unreadable_image_line(1, failed=True)


# ------------------------------------------ round 2: QA's mutation guards --


def test_an_image_transcript_is_never_written_to_the_document_store(capture, reader, monkeypatch):
    """The store is pinned into every later turn as "documents the user
    uploaded"; a model's reading of a photo is not one. No builder test
    pins this: dropping "image" from the skip list stays green there."""
    composer, _seen = capture
    from app import db

    saved: list = []
    real = db.save_document

    def spy(conversation_id, filename, text, total_pages=0):
        saved.append(filename)
        return real(conversation_id, filename, text, total_pages)

    monkeypatch.setattr(db, "save_document", spy)
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "make this table into an excel file", conv="qa-docstore")
    assert final.get("artifacts")
    assert not [n for n in saved if n.startswith("image")], saved


def test_five_images_one_hanging_keeps_the_four_and_honours_the_deadline(monkeypatch):
    from app.artifacts import material_in

    seen = {"n": 0, "peak": 0, "live": 0}

    async def completion(messages, **kwargs):
        seen["n"] += 1
        seen["live"] += 1
        seen["peak"] = max(seen["peak"], seen["live"])
        try:
            if seen["n"] == 3:
                await asyncio.sleep(30)
            await asyncio.sleep(0.01)
            return TABLE_MD
        finally:
            seen["live"] -= 1

    monkeypatch.setattr(llm, "chat_completion", completion)
    t0 = time.monotonic()
    got = asyncio.run(material_in.read_images_text([IMG] * 5, deadline_s=0.5))
    took = time.monotonic() - t0
    assert took < 2.0, took
    assert len(got.texts) == 4 and got.failed == 1 and got.total == 5
    assert seen["peak"] <= 2, "at most two reads at a time on the shared engine"


# ------------------------------------ round 2: an honest table is no loop --


#: A 12-pupil P/A register: the shape the QA fixture was, rows mostly "P".
_REGISTER = "\n".join(
    ["| No | Name | " + " | ".join(str(d) for d in range(1, 32)) + " |", "|" + "---|" * 33]
    + [f"| {i} | Pupil {chr(64 + i)} | " + " | ".join("A" if (i * d) % 11 == 0 else "P" for d in range(1, 32)) + " |"
       for i in range(1, 13)]
)
_HDR = "| Code | Item | Qty |\n|---|---|---|\n"


@pytest.mark.parametrize("text", [
    _REGISTER,
    # A printed form's empty lines are blank rows back to back, not a loop.
    "Visitor log\n\n| Name | Time | Sign |\n|---|---|---|\n| Asha | 9:10 | AS |\n| Ravi | 9:40 | RK |\n" + "|  |  |  |\n" * 20,
], ids=["register", "form-with-blank-rows"])
def test_an_honest_repetitive_table_is_read_not_refused_as_a_loop(text):
    assert material_in._readable_text(text) == text.strip()


@pytest.mark.parametrize("text", [
    _HDR + "| A1 | bolt | 1 |\n| A2 | nut | 2 |\n" + "| A3 | washer | 3 |\n" * 30,
    _HDR + "| A1 | bolt | 1 |\n| A2 |" + " P |" * 3000,
    _HDR + "| A1 | " + "M8 " * 400 + "| 1 |\n| A2 | nut | 2 |",
    "Stock count\n" + "nije " * 200 + "\n\n" + _HDR + "| A1 | bolt | 1 |",
], ids=["one-row-repeated", "a-row-far-wider-than-its-header", "a-looping-cell", "looping-prose-beside-a-table"])
def test_a_table_that_loops_is_still_unreadable(text):
    assert material_in._readable_text(text) == ""


# ------------------------- round 2: words that name the photo or the answer --


@pytest.mark.parametrize("words", [
    "make an excel from this photo and your previous answer",
    "put your previous answer into excel, and add the table in the attached image",
])
def test_the_photo_named_beside_the_answer_is_read_and_the_export_is_kept(words):
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("export", formats=["xlsx"], target="previous_answer", rule="export-followup")
    out, reads = material_in.image_turn(intent, words, ["jpg"])
    assert out is intent and reads is True


@pytest.mark.parametrize("words", [
    "put your previous answer into excel, not this photo",
    "put your previous answer in excel and ignore the photo",
])
def test_a_photo_the_words_turn_down_is_not_read(words):
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("export", formats=["xlsx"], target="previous_answer", rule="export-followup")
    assert material_in.image_turn(intent, words, ["jpg"]) == (intent, False)


@pytest.mark.parametrize("words", [
    "make an excel of this, don't use the previous answer",
    "keep the totals table above the details and make this an excel",
    "make the answer key in this scan into a word doc",
])
def test_a_negated_answer_or_a_layout_above_leaves_the_photo_the_source(words):
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("export", formats=["xlsx"], target="previous_answer", rule="export-followup")
    out, reads = material_in.image_turn(intent, words, ["jpg"])
    assert reads is True and (out.action, out.target) == ("create", "upload")


def test_an_upload_the_rules_chose_as_the_source_is_read_whatever_else_is_named():
    """QA: 'convert the attached image to excel with the totals row above the
    details' was already target=upload at the gate, the image was not read,
    and gather pulled the conversation's earlier documents in instead."""
    from app.artifacts import intent as I

    intent = I.ArtifactIntent("create", formats=["xlsx"], target="upload", rule="create")
    out, reads = material_in.image_turn(intent, "convert the attached image to excel like the table above", ["jpg"])
    assert reads is True and (out.action, out.target) == ("create", "upload")


def test_the_photo_and_the_named_answer_both_reach_the_material(capture, reader):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "make an excel from this photo and your previous answer", conv="img-both")
    assert final.get("artifacts")
    assert [c["has_image"] for c in calls] == [True]
    assert _table(seen["material"], HEADER) is not None, "the photo's table"
    assert _table(seen["material"], ["Region", "Sales"]) is not None, "and the answer's"


# ----------------------------- round 2: a brief in the words, a photo with no text --


@pytest.mark.parametrize("words", [
    "make a one-page PDF flyer for the grand opening of Rosa's Bakery on 5 October, 8am to 2pm, 20% off all pastries, using this photo",
    "create a presentation about coral reef conservation using this image",
    "make a pdf report on our Q3 sales with this photo as the cover",
    "make a birthday invitation for Maya's 6th birthday party on Saturday at 4pm with this photo",
    "make a flyer for this Saturday's grand opening using the attached photo",
    "make an invitation for Maya's party. Put this photo on the front",
])
def test_a_brief_in_the_words_carries_its_own_subject(words):
    assert material_in.words_carry_a_subject(words) is True


@pytest.mark.parametrize("words", [
    "make this table into an excel file",
    "make this attendance register into an excel file",
    "make an excel file from this photo",
    "turn this receipt from Sharma Hardware Store in Pune into a spreadsheet with a total",
    # Six content words, and all of them describe the photo.
    "convert this bank statement to excel with one row per transaction and a running balance",
    "make an excel for the class 7B marks in this photo",
    "make this register for 5 October into an excel file",
    "make an excel for my accountant from this photo",
    "put this in an excel for me",
])
def test_words_about_the_photo_carry_no_subject_of_their_own(words):
    assert material_in.words_carry_a_subject(words) is False


def test_settle_goes_on_from_the_conversation_only_on_a_brief():
    from app.artifacts import intent as I

    nothing = material_in.ImageReading(notes=["image.jpg: no text in it could be read, so it was not used."], total=1)
    create = I.ArtifactIntent("create", formats=["pdf"], target="upload", rule="create+image")
    brief = "make a flyer for the grand opening of Rosa's Bakery using this photo"
    out, refusal = material_in.settle_image_turn(create, nothing, text=brief, has_documents=False)
    assert refusal == "" and (out.action, out.target, out.upload_refs) == ("create", "conversation", [])
    out, refusal = material_in.settle_image_turn(create, nothing, text="make this table into an excel file", has_documents=False)
    assert out is create and refusal == material_in.UNREADABLE_IMAGE_LINE
    assert material_in.settle_image_turn(create, nothing, text="make this table into an excel file", has_documents=True) == (create, "")
    # A photo that WAS read is still only an ingredient of the brief: the
    # file is made from the conversation, the photo's text beside it.
    read = material_in.ImageReading(texts=[("image.jpg", TABLE_MD)], total=1)
    create_read = I.ArtifactIntent("create", formats=["pdf"], target="upload", upload_refs=["jpg"], rule="create+image")
    out, refusal = material_in.settle_image_turn(create_read, read, text=brief, has_documents=False)
    assert refusal == "" and (out.action, out.target, out.upload_refs) == ("create", "conversation", ["jpg"])
    source = I.ArtifactIntent("create", formats=["xlsx"], target="upload", rule="create+image")
    assert material_in.settle_image_turn(source, read, text="make this table into an excel file", has_documents=False) == (source, "")


def test_a_brief_with_a_text_free_photo_is_made_from_the_words_not_exported(capture, reader):
    """With a previous answer in the conversation the rules read the flyer
    brief as an export of it ('export-followup-handover', because of 'this
    photo'). A photo with no text must not hand the file back to that answer."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"
    with TestClient(app) as client:
        final, tokens = _turn(
            client, composer,
            "make a one-page PDF flyer for the grand opening of Rosa's Bakery on 5 October, 8am to 2pm, "
            "20% off all pastries, using this photo",
            conv="img-brief-history",
        )
    assert "couldn't read" not in tokens
    assert final.get("artifacts") and seen["operation"] == "create"
    notes = seen["material"].get("notes") or []
    assert not any("previous answer into the file" in n for n in notes), "not an export of the previous answer"
    assert any(n.startswith("image.jpg: no text") for n in notes), "the unread photo is said"


# --------------------------- round 2: what resolved, not what was referenced --


def test_an_unreadable_photo_beside_a_swept_upload_gets_the_sentence_and_no_job(capture, reader, monkeypatch):
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"

    async def resolved(request, conv):
        return [], [], "stock.csv is no longer available on the server — please re-attach it."

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this table into an excel file", conv="img-swept",
                              pdf_uploads=[{"upload_id": "0" * 32, "name": "stock.csv"}])
    assert "artifacts" not in final and seen == {}
    assert tokens == material_in.UNREADABLE_IMAGE_LINE


# ------------------------------------------------- round 2: a "|" in a cell --


def test_the_read_asks_for_a_printed_pipe_escaped_and_the_cell_stays_whole():
    """QA: the cell "+CMD|' /C calc'!A0" was stored as '+CMD' — the prompt
    never asked for the escape the table reader already honours."""
    assert "\\|" in material_in.IMAGE_READ_PROMPT
    md = "| Code | Formula |\n|---|---|\n| X1 | +CMD\\|' /C calc'!A0 |"
    got = asyncio.run(material_in.gather(history=[], image_texts=[("image.jpg", md)], save_documents=False))
    assert got.upload_tables[0].rows == [["X1", "+CMD|' /C calc'!A0"]]


def test_the_read_deadline_outlasts_a_dense_table_on_a_loaded_engine():
    """Live 2026-09-18, 12-14 requests running: 975 output tokens in 145.4 s
    (6.7 tokens/s per stream). A 60 s bound failed both live register turns
    with "send it again". The 20-pupil register is 1,681 tokens."""
    assert material_in.IMAGE_READ_DEADLINE_S * 6.7 >= 1681


# ------------------------------------- repair round 1 (2026-09-19) --


@pytest.mark.parametrize("words", [
    "create a spreadsheet from the last table in this photo",
    "make an excel of your table in the attached image",
])
def test_a_source_noun_located_in_the_photo_names_the_photo_not_the_answer(capture, reader, words):
    """QA r1c: "the last table in this photo" matched "last ... table", so
    the export of the previous answer was kept and its Region/Sales table
    sat in the material beside the photo's."""
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, words, conv="img-last-table")
    assert [c["has_image"] for c in calls] == [True]
    assert final.get("artifacts")
    assert _table(seen["material"], HEADER) is not None, "the photo's table is the material"
    assert _table(seen["material"], ["Region", "Sales"]) is None, "not the previous answer's table"


@pytest.mark.parametrize("words", [
    "make an excel for the expenses in the receipt",
    "make an excel for the expenses on the bill",
    "make a word document for the minutes on the whiteboard",
    # A spreadsheet request: the photo is its data, whatever the words say.
    "make an excel for the September expenses using this photo",
    "make a spreadsheet for tracking the September expenses using this photo",
])
def test_an_unreadable_photo_of_the_papers_the_words_name_is_refused_not_filled_from_the_answer(capture, reader, words):
    """The first cut read each of these as a brief of its own, so with an
    unreadable photo the file went on from the conversation, and the
    conversation's previous answer filled it: the B8b defect."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, words, conv="img-paper-unread")
    assert "artifacts" not in final and seen == {}, "no file from the previous answer"
    assert tokens == material_in.UNREADABLE_IMAGE_LINE


def test_a_brief_with_a_readable_photo_keeps_the_conversation_and_adds_the_photo(capture, reader):
    """QA r1b: every create became a create FROM the photo, so a brief whose
    photo had text lost the conversation's material (the previous answer's
    tables were dropped) and was made from the photo alone."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "Rosa's Bakery\nFresh bread since 1998"
    with TestClient(app) as client:
        final, tokens = _turn(
            client, composer,
            "make a one-page PDF flyer for the grand opening of Rosa's Bakery on 5 October, 8am to 2pm, "
            "20% off all pastries, using this photo",
            conv="img-brief-read",
        )
    assert final.get("artifacts") and "couldn't read" not in tokens
    material = seen["material"]
    assert "Fresh bread since 1998" in (material.get("uploads_text") or ""), "the photo's text joins the material"
    assert _table(material, ["Region", "Sales"]) is not None, "the conversation's material is kept for a brief"


def test_a_pipe_printed_inside_a_cell_stays_in_its_cell():
    """Live 2026-09-19, the invoice photo, 2 of 2 reads: the model wrote the
    cell "+CMD|' /C calc'!A0" as `| +CMD| /C calc!A0 |` although the prompt
    asks for "\\|", and the table kept "+CMD", the rest dropped unsaid."""
    md = ("Invoices due\n\n| Code | Supplier | Amount | Link |\n|---|---|---|---|\n"
          "| INV-303 | Cobalt Co | 455 | @SUM(1,2) |\n| INV-304 | Delta Inc | 990 | +CMD| /C calc!A0 |\n"
          "| INV-305 | Echo Ltd | 12 | x | |")
    got = asyncio.run(material_in.gather(history=[], image_texts=[("image.jpg", md)], save_documents=False))
    rows = got.upload_tables[0].rows
    assert rows[1] == ["INV-304", "Delta Inc", "990", "+CMD| /C calc!A0"]
    assert rows[0] == ["INV-303", "Cobalt Co", "455", "@SUM(1,2)"] and rows[2] == ["INV-305", "Echo Ltd", "12", "x"]


def test_extra_cells_the_model_added_are_not_joined_into_one():
    """The live register reads (2026-09-19) had 1-5 extra spaced cells in
    every row: the model's own extra columns, not a printed "|". A first
    cut joined them into the last cell ("P | P | A | P")."""
    md = "| No | Name | 1 | 2 |\n|---|---|---|---|\n| 1 | Asha | P | A | P | P |\n| 2 | Ravi | P | P |"
    got = asyncio.run(material_in.gather(history=[], image_texts=[("image.jpg", md)], save_documents=False))
    assert got.upload_tables[0].rows == [["1", "Asha", "P", "A"], ["2", "Ravi", "P", "P"]]


# -------------------------------------------------- round 3 (2026-09-19) --
#
# Live after the dev merge, the owner's two phrasings with a whiteboard or a
# receipt photo: "put this image in a Word doc with a summary" was a
# read_source question (the vision route answered "Here is the Word document
# with the receipt image …" and made no file, 2 of 2), and "make a PDF report
# of this" after a file card was a convert of that file (the photo was never
# read; the composer wrote "no specific data was provided", 3 of 4).

#: The earlier turn is a file card, so the rules read a follow-up as a convert.
CARD_HISTORY = [
    {"role": "user", "content": "make a pdf of our sprint notes"},
    {"role": "assistant", "content": "Created **Sprint Notes** as PDF."},
]
WHITEBOARD_MD = ("Sprint 14 retro - 16 Sep\nWent well:\n- Login bug fixed in 2 days\nProblems:\n- CI took 45 min per run\n"
                 "Actions:\n1. Priya: split the CI job by 30 Sep")


@pytest.fixture
def doc_capture():
    """A stub composer that writes a document, recording what it was handed."""
    seen: dict = {}

    async def composer(ctx):
        seen["material"] = dict(ctx.material)
        seen["operation"] = ctx.operation
        seen["kind"] = ctx.kind
        await ctx.progress_stage("intent", "done", "")
        return S.parse_body("document", {"title": "Sprint 14 retro", "blocks": [{"type": "paragraph", "text": "x"}]})

    return composer, seen


@pytest.mark.parametrize("words,history", [
    ("put this image in a Word doc with a summary", []),
    ("put this image in a Word doc with a summary", CARD_HISTORY),
    ("make a PDF report of this", CARD_HISTORY),
    ("convert this to pdf", CARD_HISTORY),
])
def test_the_owners_phrasings_make_the_file_from_the_photo(doc_capture, reader, words, history):
    composer, seen = doc_capture
    calls, state = reader
    state["reply"] = WHITEBOARD_MD
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, words, conv="img-owner-" + str(len(history)) + words[:8].replace(" ", ""),
                              history=history)
    assert final["route"] == "artifact", tokens
    assert [c["has_image"] for c in calls] == [True], "the attached photo was read"
    assert seen["operation"] == "create" and seen["kind"] == "document"
    assert "Priya: split the CI job by 30 Sep" in (seen["material"].get("uploads_text") or "")
    assert final.get("artifacts"), "a file was made"


@pytest.mark.parametrize("words,rule", [
    ("also as pdf", "convert-short"),
    ("convert the report you made to pdf", "convert-artifact-turn"),
    ("make the previous file a pdf", "convert-artifact-turn"),
    ("turn that spreadsheet into a pdf", "convert"),
    ("convert this to pdf", "ui-convert"),
])
def test_a_convert_of_a_named_earlier_file_stays_a_convert_and_reads_nothing(words, rule):
    from app.artifacts import intent as I

    convert = I.ArtifactIntent("convert", formats=["pdf"], target="artifact", reference="latest", rule=rule)
    assert material_in.image_turn(convert, words, ["jpg"]) == (convert, False)


@pytest.mark.parametrize("words", ["make a PDF report of this", "convert this receipt to excel", "put the attached photo in a pdf"])
def test_a_convert_whose_words_point_at_the_photo_is_made_from_the_photo(words):
    from app.artifacts import intent as I

    convert = I.ArtifactIntent("convert", formats=["pdf"], target="artifact", reference="latest", reference_hint="notes",
                               rule="convert-artifact-turn")
    out, reads = material_in.image_turn(convert, words, ["jpg"])
    assert reads is True
    assert (out.action, out.target, out.new_artifact, out.reference, out.reference_hint, out.upload_refs, out.rule) == (
        "create", "upload", True, "none", "", ["jpg"], "convert-artifact-turn+image")


def test_placing_the_attachment_in_a_new_file_is_a_request_not_a_question_about_it():
    from app.artifacts import intent as I
    from app.artifacts import lexicon as LX

    for words in ("put this image in a Word doc with a summary", "put a summary of this image in a word doc",
                  "put the key points of this pdf in a word document"):
        assert LX.negative_shape(words.lower(), ["jpg"]) is None, words
        assert I.decide(words, upload_formats=["jpg"]).wants_file, words
    # A file the words READ stays a question: the article is definite, or
    # there is no destination at all.
    for words in ("what does the summary in this pdf say", "summarize this pdf", "what is the total in this pdf",
                  "tell me what the word doc says"):
        assert not I.decide(words, upload_formats=["pdf"]).wants_file, words
