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
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os

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
