"""Adversarial QA r1 for B8b (an image attached to a file request is the file's material).

Route tests run through the real /chat route with the composer and renderer
stubbed and the image-read model call stubbed at `llm.chat_completion`, the
same harness as test_artifact_image_material.py. Every reply the stub gives is
one the main model gave live on a QA fixture (see the QA report): the
attendance register was transcribed at 89-90% of cells, 3 of 3 runs, and a
text-free product photo answered NO_READABLE_TEXT 3 of 3.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import random
import time

import pytest
from fastapi.testclient import TestClient

from app import llm, metrics
from app import main as app_main
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
TABLE_MD = "Warehouse B - stock count, 14 Sep\n\n" + "\n".join(
    ["| " + " | ".join(HEADER) + " |", "|---|---|---|---|", *("| " + " | ".join(r) + " |" for r in ROWS)]
)
PREVIOUS_ANSWER = (
    "## Regional sales, Q2\n\n| Region | Sales |\n|---|---|\n| North | 120 |\n| South | 95 |\n| East | 143 |\n\n"
    "North and East grew; South fell for the second quarter in a row, mostly on lower repeat orders."
)
HISTORY = [
    {"role": "user", "content": "give me the regional sales for Q2"},
    {"role": "assistant", "content": PREVIOUS_ANSWER},
]

# A class attendance register, 10 pupils x 31 days of P/A: the main model's
# own transcript of the QA fixture photo (live 2026-09-18, Fast, thinking off,
# run 1 of 3; all 10 rows, 324 of 363 cells exact, finish=stop). Synthetic
# names. The other two runs were the same shape (327 and 326 of 363).
REGISTER_LIVE_MD = 'Class 7B - attendance register, August\n\n| No | Name | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 31 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n| 1 | Aarav Shah | P | P | P | P | P | A | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | P | P | A | P | P | P | P | P |\n| 2 | Diya Patel | P | P | P | P | P | P | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P |\n| 3 | Kabir Mehta | P | P | P | A | P | P | A | P | P | P | P | P | P | P | A | P | A | P | P | P | P | P | P | P | P | A | P | A | P | P | P | P | A |\n| 4 | Isha Rao | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | P | A | P | P | A | P | P | P | P |\n| 5 | Vivaan Joshi | P | P | A | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P |\n| 6 | Anaya Desai | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P |\n| 7 | Arjun Nair | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | P | P | A | P | P | P | P | P | P | P | P | P | P | P |\n| 8 | Myra Iyer | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | P | A | P | P | P | P | P | P | P | A | A | P | P | A |\n| 9 | Reyansh Gupta | P | P | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P |\n| 10 | Sara Khan | P | P | P | A | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | P | A | P | A | P | P | A | A | A | P | P |'
REG_HEADER = ["No", "Name"] + [str(d) for d in range(1, 32)]
_NAMES = ["Aarav Shah", "Diya Patel", "Kabir Mehta", "Isha Rao", "Vivaan Joshi", "Anaya Desai", "Arjun Nair", "Myra Iyer",
          "Reyansh Gupta", "Sara Khan"]
_rnd = random.Random(3)
#: The same register for a class of 20: a flawless transcript, no model error.
REGISTER_20_MD = "Class 7B - attendance register, August\n\n" + "\n".join(
    ["| " + " | ".join(REG_HEADER) + " |", "|" + "---|" * len(REG_HEADER)]
    + ["| " + " | ".join([str(i + 1), f"{_NAMES[i % 10]} {i // 10 + 1}"] + [("A" if _rnd.random() < 0.12 else "P") for _ in range(31)]) + " |"
       for i in range(20)]
)


def _jpeg_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (240, 240, 240)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


IMG = _jpeg_b64()


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
    seen: dict = {}

    async def composer(ctx):
        seen["material"] = dict(ctx.material)
        seen["operation"] = ctx.operation
        seen["formats"] = list(ctx.formats)
        await ctx.progress_stage("intent", "done", "")
        seen["kind"] = str(ctx.job.get("kind") or "")
        if seen["kind"] == "document":
            return S.parse_body("document", {"title": "Flyer", "blocks": [{"type": "paragraph", "text": "Grand opening"}]})
        return S.parse_body("workbook", {"title": "Sheet", "sheets": [
            {"name": "Data", "columns": [{"name": "A"}], "rows": [["x"]]}]})

    return composer, seen


@pytest.fixture
def reader(monkeypatch):
    calls: list = []
    state = {"reply": TABLE_MD}

    async def completion(messages, **kwargs):
        has_image = any(
            isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages
        )
        calls.append({"has_image": has_image})
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


def _turn(client, composer, message, *, conv, history=HISTORY, images=(IMG,), **extra):
    pipeline.set_composer(composer)
    # A unique session per turn: with no earlier messages the route falls back
    # to the in-process session memory, which earlier tests have written to.
    body = {"messages": [*history, {"role": "user", "content": message}], "mode": "assistant", "conversation_id": conv,
            "session_id": f"s-{conv}-{time.monotonic_ns()}", "intent_id": f"int-{conv}", "effort": "fast", **extra}
    if images:
        body["images"] = list(images)
    resp = client.post("/chat", json=body)
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


# ------------------------------------------------ Q1. a legible table refused --


@pytest.mark.parametrize("transcript", [REGISTER_LIVE_MD, REGISTER_20_MD], ids=["live-10-pupils", "20-pupils"])
def test_a_legible_attendance_register_is_made_into_the_file_not_refused(capture, reader, transcript):
    """A 10 x 31 P/A register is the most repetitive honest table there is.
    ocr.is_degenerate's unique-token floor (0.08) reads it as a model loop
    (the live transcripts scored 0.077-0.078), so the turn said "I couldn't
    read any text in the attached photo" about a photo the model had read."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = transcript
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this attendance register into an excel file", conv="qa-register")
    assert "couldn't read" not in tokens, tokens
    assert final.get("artifacts"), "a legible register makes a file"
    table = _table(seen["material"], REG_HEADER)
    assert table is not None and len(table["rows"]) == transcript.count("\n| ") - 1


# --------------------- Q2. words that merely CONTAIN "answer"/"above"/"chat" --


@pytest.mark.parametrize("words", [
    "turn this chat screenshot into a word document",
    "make a word doc of the answer sheet in this photo",
    "make an excel from this photo, keep the header row above the data",
    "convert the attached image to excel with the totals row above the details",
])
def test_the_photo_stays_the_material_when_the_words_only_contain_answer_above_or_chat(capture, reader, words):
    """`_ANSWER_SOURCE_RE` matches a bare `above`, `the answer` and `this chat`
    anywhere, so each of these sends the turn back to the previous answer and
    the photo is never read: the B8b defect, through another door."""
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, words, conv="qa-answer-words")
    assert [c["has_image"] for c in calls] == [True], "the attached photo was read"
    assert _table(seen["material"], HEADER) is not None, "the photo's table is the material"
    assert _table(seen["material"], ["Region", "Sales"]) is None, "not the previous answer's table"


# ------------------------ Q3. a topic request with a text-free photo attached --


def test_a_topic_request_with_a_text_free_photo_still_makes_its_file(capture, reader):
    """The words carry the whole subject; the photo is decoration. Before the
    fix this made the flyer (create from the conversation). After it,
    image_turn makes every create a create-from-the-upload, the photo has no
    text (live: NO_READABLE_TEXT, 3 of 3), and the turn is refused."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"
    with TestClient(app) as client:
        final, tokens = _turn(
            client, composer,
            "make a one-page PDF flyer for the grand opening of Rosa's Bakery on 5 October, 8am to 2pm, "
            "20% off all pastries, using this photo",
            conv="qa-flyer", history=[],
        )
    assert "couldn't read" not in tokens, tokens
    assert final.get("artifacts"), "the flyer is made from the words"


# -------------------------------------------- Q4. what must NOT change --


@pytest.mark.parametrize("words", [
    "what's in this image",
    "can you read this bill and tell me the total",
    "extract the table from this photo",
    "what does this chart show?",
])
def test_questions_about_an_image_still_go_to_the_vision_engine(capture, reader, words):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, words, conv="qa-vision-q")
    assert final["route"] == "vision"
    assert tokens == "The image shows a stock table."
    assert calls == [] and seen == {}


def test_a_file_request_without_an_image_still_exports_the_previous_answer(capture, reader):
    composer, seen = capture
    calls, _state = reader
    with TestClient(app) as client:
        final, _tokens = _turn(client, composer, "make this table into an excel file", conv="qa-no-image", images=())
    assert final["route"] == "artifact" and final.get("artifacts")
    assert calls == []
    assert _table(seen["material"], ["Region", "Sales"]) is not None


def test_a_document_only_turn_names_only_the_document_to_the_gate(capture, reader, monkeypatch):
    composer, _seen = capture
    from app.artifacts import intent as intent_rules

    got: dict = {}
    real = intent_rules.decide_with_hook

    async def spy(text, hook=None, **kw):
        got.update(kw)
        return await real(text, hook, **kw)

    csv_text = "\n".join(",".join(f'"{c}"' for c in row) for row in [HEADER, *ROWS])

    async def resolved(request, conv):
        return [("stock.csv", base64.b64encode(csv_text.encode()).decode())], [], None

    monkeypatch.setattr(intent_rules, "decide_with_hook", spy)
    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    with TestClient(app) as client:
        _turn(client, composer, "make this into an excel file", conv="qa-doc-only", images=(),
              pdf_uploads=[{"upload_id": "0" * 32, "name": "stock.csv"}])
    assert got.get("upload_formats") == ["csv"]


# ---------------------------------------------------- Q5. the parts' seams --


@pytest.mark.parametrize("text", [
    "| الصنف | الكمية |\n|---|---|\n| تفاح | ١٢ |\n| موز | ٧ |",
    "| वस्तु | मात्रा |\n|---|---|\n| चावल | 12 |",
    "| 品目 | 数量 |\n|---|---|\n| りんご | 12 |",
])
def test_right_to_left_and_non_latin_transcripts_are_readable_and_keep_their_cells(text):
    from app.artifacts import material_in

    assert material_in._readable_text(text) == text
    got = asyncio.run(material_in.gather(history=[], image_texts=[("image.jpg", text)], save_documents=False))
    rows = [line.strip("| ").split(" | ") for line in text.splitlines()[2:]]
    assert got.upload_tables and got.upload_tables[0].rows == rows


def test_malformed_image_bytes_are_named_without_raising():
    from app.artifacts import material_in

    assert material_in.image_names(["!!not base64!!"]) == ["image.png"]
    assert material_in.image_names([""]) == ["image.png"]
    assert material_in.image_names(["data:image/heic;base64,AAAA"]) == ["image.heic"]


def test_an_unreadable_photo_beside_a_deleted_upload_is_not_replaced_by_the_previous_answer(capture, reader, monkeypatch):
    """`has_documents` is read from the REQUEST (a reference was sent), not
    from what resolved. When the referenced upload is gone and the photo is
    unreadable, the turn has no material at all, and the previous answer's
    table fills the file: the B8b defect through the deleted-upload door."""
    composer, seen = capture
    _calls, state = reader
    state["reply"] = "NO_READABLE_TEXT"

    async def resolved(request, conv):
        return [], [], "That file is no longer available; please attach it again."

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    with TestClient(app) as client:
        final, tokens = _turn(client, composer, "make this table into an excel file", conv="qa-deleted-upload",
                              pdf_uploads=[{"upload_id": "0" * 32, "name": "stock.csv"}])
    built_from_answer = bool(seen) and _table(seen.get("material") or {}, ["Region", "Sales"]) is not None
    assert not built_from_answer, "the file was built from the previous answer"
