"""Vision accuracy: no invented characters, the image survives the turn, and
the OCR pre-pass is a labelled hint instead of text in the prompt.

Three audit findings from 2026-09-17, all measured against the live engine
and the live OCR sidecar with six images of known ground truth:

1. CRITICAL — a dark, blurred door sign reading "Emergency contact: ext. 4471"
   was answered "**ext. 4472**", and the model's own reasoning shows where the
   digit came from: the OCR pre-pass had handed it the line "1. 2017年1月1日",
   which the picture does not contain, and the model wrote "this seems like a
   hallucination ... it looks like a phone number or extension" and then
   produced 4472 anyway. A wrong extension with a hedge is worse than no
   extension: the person dials it.
2. HIGH — the second question about the same image ("what was the invoice
   number again?") routed to `rag` and answered "I don't see the note you're
   referring to in our chat. Could you please paste it or upload it here?"
   about a photo sent one turn earlier, with both answers in it.
3. HIGH — the pre-pass ran the document prompt on chat images: every one of
   six reads came back with text the picture does not contain, and the
   screenshot (11.6 s) and the chart (25.4 s) blew the 10 s deadline, so the
   turn paid the full deadline and then threw the transcript away.

Offline, like the rest of the suite. The live half of the proof — the same
six images through the real engine before and after — is in the change's
report; what is asserted here is the mechanism that cannot regress silently.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import context, llm
from app.config import settings
from app.engines import image_memory
from app.engines import ocr as ocr_module
from app.engines import router as router_engine
from app.engines import vision
from app.main import app

IMG = "aGVsbG8="


async def _collect(_event, _data):
    return None


def _fake_stream(recorder, pairs=(("token", "ok"),)):
    async def fake(messages, *, model_choice="smart", effort="medium", **kwargs):
        recorder["messages"] = list(messages)
        for pair in pairs:
            yield pair

    return fake


def _image_text_parts(recorder):
    return [
        p["text"] for p in recorder["messages"][-1]["content"] if p["type"] == "text"
    ]


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setitem(
        context._window_cache, settings.openai_base_url, settings.model_max_context
    )
    image_memory.clear()
    yield
    image_memory.clear()


# ---------------------------------------------------------------------------
# 1. An unreadable character is reported unreadable, never completed
# ---------------------------------------------------------------------------


def test_the_prompt_forbids_completing_a_number_it_cannot_fully_read():
    """The generic honesty line ("Never invent values") was already there and
    did not stop 4472. The rule the answer needed is about characters."""
    system = vision._SYSTEM
    assert "NEVER COMPLETE A NUMBER YOU CANNOT READ" in system
    # the mark an unreadable character gets, measured live: with this rule
    # the same sign came back "ext. 447?" and "the last digit is not clearly
    # legible" instead of "ext. 4472".
    assert "put " in system and "question mark" in system
    # the exact escape routes the failing answer used
    assert "a similar document" in system
    assert "an OCR " in system
    assert "hedged guess" in system


def test_the_prompt_makes_a_superlative_check_itself_against_the_rows():
    """The table photo was read perfectly and then summarised "worst
    discrepancy (largest absolute delta): EL-5533 with a delta of -21" — the
    largest absolute delta in the rows it had just listed is CR-2255 at +35."""
    assert "check the claim against every value you read" in vision._SYSTEM
    assert "state the test you" in vision._SYSTEM


# ---------------------------------------------------------------------------
# 1b. The picture is measured, because the rule alone was not enough
# ---------------------------------------------------------------------------


def _png(background: int, ink: int, *, blur: float = 0.0) -> str:
    """A small greyscale PNG as base64: text at `ink` on `background`."""
    import base64
    import io

    from PIL import Image, ImageDraw, ImageFilter

    im = Image.new("L", (480, 320), background)
    draw = ImageDraw.Draw(im)
    for row in range(6):
        draw.text((20, 20 + row * 40), "Emergency contact: ext. 4471", fill=ink)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_a_dark_blurred_picture_is_measured_as_hard_to_read():
    from app.engines import image_quality

    quality = image_quality.measure(_png(12, 34, blur=2.5))
    assert quality is not None and quality.hard is True


def test_a_dark_but_sharp_picture_is_not_flagged():
    """The audit set's UI screenshot is dark (mean 24.9) and reads
    perfectly; flagging it would make the model hedge about text it can
    see."""
    from app.engines import image_quality

    quality = image_quality.measure(_png(12, 255))
    assert quality is not None and quality.hard is False


def test_a_picture_that_cannot_be_decoded_is_silence_not_an_error():
    from app.engines import image_quality

    assert image_quality.measure("not base64 at all !!") is None
    assert image_quality.legibility_note(["not base64 at all !!"]) == ""
    assert image_quality.legibility_note([]) == ""


def test_the_unreadable_picture_gets_a_measurement_in_the_prompt(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    rec: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision.run_vision_engine("read the sign", _png(12, 34, blur=2.5), [], _collect, effort="think")
    )
    note = [p for p in _image_text_parts(rec) if "Image quality measured" in p]
    assert note, "the measurement did not reach the prompt"
    assert "measurement, not something read in the picture" in note[0]
    assert "Do NOT pick the most likely digit" in note[0]


def test_an_ordinary_picture_s_prompt_is_not_touched(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    rec: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision.run_vision_engine("read the sign", _png(12, 255), [], _collect, effort="think")
    )
    assert not [p for p in _image_text_parts(rec) if "Image quality measured" in p]


def test_an_operator_can_switch_the_measurement_off(monkeypatch):
    from app.engines import image_quality

    monkeypatch.setenv("IMAGE_QUALITY_NOTE", "false")
    assert image_quality.legibility_note([_png(12, 34, blur=2.5)]) == ""


# ---------------------------------------------------------------------------
# 3. The OCR pre-pass: labelled, filtered, and not paid for twice
# ---------------------------------------------------------------------------


def _fake_reads(recorder, reads):
    async def fake(images, **kw):
        recorder.setdefault("calls", []).append(kw)
        return list(reads)

    return fake


def test_a_looping_ocr_read_never_reaches_the_prompt(monkeypatch):
    """`ocr_images` forwards a DEGENERATE read's text verbatim — its own
    docstring says so — so a loop arrived as if it were the picture's text."""
    monkeypatch.setattr(settings, "ocr_enabled", True)
    rec_ocr: dict = {}
    monkeypatch.setattr(
        ocr_module,
        "read_images",
        _fake_reads(rec_ocr, [ocr_module.OcrRead("nije nije nije", "degenerate", "looped")]),
    )
    rec: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(vision.run_vision_engine("what is this", IMG, [], _collect, effort="think"))

    joined = "\n".join(_image_text_parts(rec))
    assert "nije" not in joined
    assert "OCR transcript" not in joined


def test_a_failed_ocr_read_never_reaches_the_prompt(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    rec_ocr: dict = {}
    monkeypatch.setattr(
        ocr_module,
        "read_images",
        _fake_reads(rec_ocr, [ocr_module.OcrRead("", "failed", "the OCR batch deadline of 10s passed")]),
    )
    rec: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(vision.run_vision_engine("what is this", IMG, [], _collect, effort="think"))

    assert "OCR transcript" not in "\n".join(_image_text_parts(rec))


def test_a_good_read_arrives_labelled_as_another_model_s_output(monkeypatch):
    """It is not text the model read. The old wording ("Transcript from the
    OCR model — if it disagrees with the pixels, trust the pixels.") was
    appended to the same user message and read as a reading; this one says
    whose output it is, that it can contain characters the picture does not,
    and that a number in it may not be reported unless it is also legible."""
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(
        ocr_module,
        "read_images",
        _fake_reads({}, [ocr_module.OcrRead("Emergency contact: ext. 44", "ok")]),
    )
    rec: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(vision.run_vision_engine("read the sign", IMG, [], _collect, effort="think"))

    block = [p for p in _image_text_parts(rec) if "OCR transcript" in p][0]
    assert "SEPARATE OCR model" in block
    assert "not text you read" in block
    assert "never report a number" in block
    assert "Emergency contact: ext. 44" in block


def test_the_image_route_sends_the_ocr_prompt_that_was_measured_to_work(monkeypatch):
    """Production runs OCR_PROMPT unset, so the chat image route sent
    "document parsing" — the prompt that prefixed "ovi…" to every read and
    invented "1. 2017年1月1日" on the dark sign. The document default stays;
    the image route sends the one that reads."""
    monkeypatch.delenv("IMAGE_OCR_PROMPT", raising=False)
    assert ocr_module.image_ocr_prompt() == "OCR"
    assert ocr_module.document_prompt() == "document parsing"

    monkeypatch.setattr(settings, "ocr_enabled", True)
    rec_ocr: dict = {}
    monkeypatch.setattr(
        ocr_module, "read_images", _fake_reads(rec_ocr, [ocr_module.OcrRead("x", "ok")])
    )
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}))

    asyncio.run(vision.run_vision_engine("read this", IMG, [], _collect, effort="think"))

    assert rec_ocr["calls"][0]["prompt"] == "OCR"


def test_an_operator_can_still_override_the_image_prompt(monkeypatch):
    monkeypatch.setenv("IMAGE_OCR_PROMPT", "document parsing")
    assert ocr_module.image_ocr_prompt() == "document parsing"


def test_a_deadline_miss_benches_the_pass_for_that_conversation(monkeypatch):
    """The pass runs BEFORE the main model can start, so a miss is 10 s of
    "Reading the text in the image…" for a transcript that is thrown away.
    A screenshot-heavy chat used to pay it every turn."""
    monkeypatch.setattr(settings, "ocr_enabled", True)
    rec_ocr: dict = {}
    monkeypatch.setattr(
        ocr_module,
        "read_images",
        _fake_reads(
            rec_ocr,
            [ocr_module.OcrRead("", "failed", "the OCR batch deadline of 10s passed before this image was read")],
        ),
    )
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}))
    vision.forget_ocr_bench("conv-ocr-slow")

    asyncio.run(
        vision.run_vision_engine(
            "what is this", IMG, [], _collect, effort="think", conversation_id="conv-ocr-slow"
        )
    )
    asyncio.run(
        vision.run_vision_engine(
            "and this?", IMG, [], _collect, effort="think", conversation_id="conv-ocr-slow"
        )
    )
    # another conversation is untouched
    asyncio.run(
        vision.run_vision_engine(
            "what is this", IMG, [], _collect, effort="think", conversation_id="conv-other"
        )
    )

    assert len(rec_ocr["calls"]) == 2
    assert vision.ocr_is_benched("conv-ocr-slow") is True
    assert vision.ocr_is_benched("conv-other") is True  # it missed too
    vision.forget_ocr_bench("conv-ocr-slow")
    vision.forget_ocr_bench("conv-other")
    assert vision.ocr_is_benched("conv-ocr-slow") is False


def test_a_sidecar_that_is_simply_down_keeps_its_chance(monkeypatch):
    """A connection error costs the turn nothing, so it does not bench."""
    monkeypatch.setattr(settings, "ocr_enabled", True)
    rec_ocr: dict = {}
    monkeypatch.setattr(
        ocr_module,
        "read_images",
        _fake_reads(rec_ocr, [ocr_module.OcrRead("", "failed", "APIConnectionError: Connection error.")]),
    )
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}))
    vision.forget_ocr_bench("conv-ocr-down")

    for _ in range(2):
        asyncio.run(
            vision.run_vision_engine(
                "what is this", IMG, [], _collect, effort="think", conversation_id="conv-ocr-down"
            )
        )

    assert len(rec_ocr["calls"]) == 2
    assert vision.ocr_is_benched("conv-ocr-down") is False


def test_the_block_keeps_each_image_in_its_place():
    """Three attached, only the second read: the transcript must not be
    attached to the wrong picture."""
    reads = [
        ocr_module.OcrRead("", "failed", "deadline"),
        ocr_module.OcrRead("INVOICE TS-2291", "ok"),
        ocr_module.OcrRead("looped", "degenerate", "looped"),
    ]
    block = ocr_module.evidence_block(reads, "image")
    assert "Image 2 of 3" in block
    assert "INVOICE TS-2291" in block
    assert "looped" not in block
    assert ocr_module.evidence_block([], "image") == ""
    assert ocr_module.evidence_block([ocr_module.OcrRead("", "empty")], "image") == ""


# ---------------------------------------------------------------------------
# 2. The image stays in the conversation
# ---------------------------------------------------------------------------

#: The audit's own two turns, verbatim.
TURN1 = "From this note: what number do I call, and how much is still owed after the deposit?"
ANSWER1 = (
    "**Number to call:** +31 6 24 88 17 05.\n\n"
    "**Still owed:** 4,820 EUR invoiced minus the 1,250 EUR deposit already paid "
    "= **3,570 EUR**."
)
FOLLOWUP = "What was the invoice number again, and what day was the meeting moved to?"


def test_the_image_is_remembered_for_the_conversation_that_sent_it():
    image_memory.remember("conv-a", [IMG], question=TURN1, answer=ANSWER1)
    assert image_memory.recall("conv-a") == [IMG]
    assert image_memory.recall("conv-b") == []
    assert image_memory.recall(None) == []


def test_the_audit_s_own_followup_is_recognised_as_being_about_the_image():
    image_memory.remember("conv-a", [IMG], question=TURN1, answer=ANSWER1)
    assert image_memory.images_for_followup("conv-a", FOLLOWUP) == [IMG]


@pytest.mark.parametrize(
    "message",
    [
        "what else does the photo say?",
        "can you read the sign again?",
        "and the total at the bottom?",
        "zoom into the screenshot please",
    ],
)
def test_ordinary_followups_reach_the_image(message):
    image_memory.remember("conv-a", [IMG], question=TURN1, answer=ANSWER1)
    assert image_memory.images_for_followup("conv-a", message) == [IMG]


@pytest.mark.parametrize(
    "message",
    [
        "what is the capital of France?",
        "write me a python function that reverses a string",
        "who won the world cup in 2018",
    ],
)
def test_an_unrelated_question_does_not_drag_the_image_in(message):
    image_memory.remember("conv-a", [IMG], question=TURN1, answer=ANSWER1)
    assert image_memory.images_for_followup("conv-a", message) == []


def test_a_conversation_that_never_sent_an_image_has_nothing_to_recall():
    assert image_memory.images_for_followup("conv-z", FOLLOWUP) == []


def test_another_account_with_the_same_conversation_id_sees_nothing():
    """/chat's conversation key is whatever the client sent, so the viewer
    has to be part of the key or one account's photo could be recalled into
    another's turn."""
    image_memory.remember("shared-id", [IMG], question=TURN1, answer=ANSWER1, user_id=7)
    assert image_memory.recall("shared-id", 7) == [IMG]
    assert image_memory.recall("shared-id", 8) == []
    assert image_memory.images_for_followup("shared-id", FOLLOWUP, 8) == []
    assert image_memory.images_for_followup("shared-id", FOLLOWUP, 7) == [IMG]


def test_the_memory_expires(monkeypatch):
    image_memory.remember("conv-a", [IMG], question=TURN1, answer=ANSWER1)
    monkeypatch.setenv("IMAGE_MEMORY_TTL_S", "0.0001")
    import time

    time.sleep(0.01)
    assert image_memory.recall("conv-a") == []


def test_the_store_is_bounded_in_conversations_and_in_bytes(monkeypatch):
    monkeypatch.setenv("IMAGE_MEMORY_CONVERSATIONS", "2")
    for name in ("c1", "c2", "c3"):
        image_memory.remember(name, [IMG])
    assert image_memory.recall("c1") == []
    assert image_memory.recall("c3") == [IMG]

    monkeypatch.setenv("IMAGE_MEMORY_MAX_CHARS", str(len(IMG)))
    image_memory.remember("c4", [IMG, IMG])
    assert image_memory.recall("c4") == [IMG]  # the second did not fit


def test_the_whole_store_fits_inside_one_budget(monkeypatch):
    """The per-conversation cap alone is not a bound: 64 conversations at
    24 M characters each is 1.5 GB of orchestrator memory."""
    monkeypatch.setenv("IMAGE_MEMORY_CONVERSATIONS", "100")
    monkeypatch.setenv("IMAGE_MEMORY_TOTAL_CHARS", str(2 * len(IMG)))
    for name in ("c1", "c2", "c3"):
        image_memory.remember(name, [IMG])
    assert image_memory.recall("c1") == []
    assert image_memory.recall("c2") == [IMG]
    assert image_memory.recall("c3") == [IMG]


# ---------------------------------------------------------------------------
# 2, at the route: turn 2 answers from the picture instead of denying it
# ---------------------------------------------------------------------------


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


def test_the_second_question_about_the_same_image_goes_to_the_vision_engine(monkeypatch):
    """Turn 2 carries no image — exactly what the composer sends — and the
    history is text only. Before this it routed to rag and answered "I don't
    see the note you're referring to in our chat"."""
    seen: list = []

    async def fake_engine(
        message, images, history, emit, *, effort="think", max_tokens=None, conversation_id=None
    ):
        seen.append({"message": message, "images": list(images), "conversation_id": conversation_id})
        await emit("token", {"text": "TS-2291, Thursday 11:15"})
        await emit("meta", {"route": "vision"})
        return "TS-2291, Thursday 11:15"

    monkeypatch.setattr(vision, "run_vision_engine", fake_engine)

    async def route_chat(message, has_image=False, history=()):
        return "vision" if has_image else "rag"

    monkeypatch.setattr(router_engine, "route_request", route_chat)

    conv = "vision-followup-conv"
    with TestClient(app) as client:
        first = client.post(
            "/chat",
            json={"message": TURN1, "image": IMG, "conversation_id": conv, "effort": "think"},
        )
        second = client.post(
            "/chat",
            json={
                "message": FOLLOWUP,
                "conversation_id": conv,
                "effort": "think",
                "history": [
                    {"role": "user", "content": TURN1},
                    {"role": "assistant", "content": ANSWER1},
                ],
            },
        )

    assert first.status_code == 200 and second.status_code == 200
    assert len(seen) == 2, "the follow-up did not reach the vision engine"
    assert seen[1]["images"] == [IMG]
    assert seen[1]["message"] == FOLLOWUP
    assert dict(_parse_sse(second.text))["meta"]["route"] == "vision"


def test_an_unrelated_question_after_an_image_turn_routes_normally(monkeypatch):
    """The memory must not swallow the rest of the conversation."""
    seen: list = []

    async def fake_engine(
        message, images, history, emit, *, effort="think", max_tokens=None, conversation_id=None
    ):
        seen.append(message)
        await emit("token", {"text": "seen"})
        await emit("meta", {"route": "vision"})
        return "seen"

    monkeypatch.setattr(vision, "run_vision_engine", fake_engine)

    async def route_chat(message, has_image=False, history=()):
        return "vision" if has_image else "chat"

    monkeypatch.setattr(router_engine, "route_request", route_chat)

    conv = "vision-unrelated-conv"
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"message": TURN1, "image": IMG, "conversation_id": conv, "effort": "fast"},
        )
        second = client.post(
            "/chat",
            json={
                "message": "what is the capital of France?",
                "conversation_id": conv,
                "effort": "fast",
            },
        )

    # The turn goes wherever it went before this change — offline that is a
    # chat engine with no model behind it, which is enough: what matters is
    # that the vision engine was NOT called with the stale picture.
    assert seen == [TURN1]


def test_an_image_followup_is_a_question_about_an_attachment():
    """The visual-refusal gate sits above the image route: "what does the map
    in that photo show?" must not be answered "I can't draw a map" one turn
    after the photo was read."""
    from app.main import ChatRequest, _asks_about_an_attachment

    request = ChatRequest(message="what does the map in that photo show?")
    text = "what does the map in that photo show?"
    assert _asks_about_an_attachment(text, request, False, False) is False
    assert _asks_about_an_attachment(text, request, False, True) is True
