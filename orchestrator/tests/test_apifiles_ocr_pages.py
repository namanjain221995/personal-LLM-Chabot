"""The PDF `ocr` stage against a STUB OCR engine over real HTTP.

Everything but the engine is real: the extraction child renders the pages
(PDFium at the chat app's scale), the chat app's own `engines.ocr.read_images`
talks to the stub through the OpenAI client exactly as it talks to
`vllm-ocr`, `engines.ocr.classify` judges the transcripts, and the public
`capacity.hold('ocr', …)` gate admits each unit. The stub reads the page
number back out of the rendered PNG (the marker blocks `page_marker_jpeg`
draws), so a transcript that lands on the wrong page fails the test.

No database: the conftest database fixtures are replaced by no-ops.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import threading
import time
from typing import Any, Dict, List

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.apifiles import extract_worker, ocr_pages, render
from app.apifiles.extractors import Spec
from app.config import settings
from app.publicapi import capacity, errors
from tests.test_apifiles_extractors import (
    blob_layout,
    build_pdf,
    caps_for,
    page_marker_jpeg,
    pages_of,
    read_page_marker,
)


@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


# ============================================================ stub engine ==


class StubOcr:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: List[int] = []
        self.prompts: List[str] = []
        self.degenerate: set = set()
        self.fail_pages: set = set()
        self.down = False
        # page -> the engine's raw answer, verbatim, when a test needs one.
        self.raw: Dict[int, str] = {}

    def transcript(self, page: int) -> str:
        if page in self.degenerate:
            return "nije " * 400
        return f"Scanned page {page}: Escrow agent: Halden Trust, ledger line {page * 7}."


STUB = StubOcr()


async def _chat(request: Request):
    if STUB.down:
        return JSONResponse({"error": {"message": "engine loading"}}, status_code=503)
    body = await request.json()
    content = body["messages"][0]["content"]
    url = next(p["image_url"]["url"] for p in content if p.get("type") == "image_url")
    prompt = next(p["text"] for p in content if p.get("type") == "text")
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        page = read_page_marker(image)
    STUB.calls.append(page)
    STUB.prompts.append(prompt)
    if page in STUB.fail_pages:
        return JSONResponse({"error": {"message": "bad image"}}, status_code=400)
    return JSONResponse(
        {
            "id": "cmpl-stub",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub-ocr",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": STUB.raw.get(page, STUB.transcript(page))}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


@pytest.fixture(scope="module")
def stub_engine():
    app = Starlette(routes=[Route("/v1/chat/completions", _chat, methods=["POST"])])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(autouse=True)
def engine_settings(stub_engine, monkeypatch):
    STUB.reset()
    capacity.reset_for_tests()
    monkeypatch.setattr(settings, "ocr_base_url", stub_engine)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    # One attempt per read: the stage's own deferral is what this suite tests,
    # not the OpenAI client's backoff.
    monkeypatch.setattr(settings, "llm_max_retries", 0)
    # The OCR gate steps aside while a chat turn is busy; no chat runs here.
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    yield


# =============================================================== fixture ==

#: 12 pages. 3, 5, 7 and 11 are image-only (thin: no text layer); page 9 has a
#: typed 40-character footer (thin: under 200 characters); the rest are text.
SCANNED = (3, 5, 7, 11)
SHORT = 9
FOOTER = "Typed footer: confidential, page nine."


def scanned_pdf(tmp_path) -> tuple:
    pages = []
    for number in range(1, 13):
        if number in SCANNED:
            pages.append(("image", (page_marker_jpeg(number), (400, 566))))
        elif number == SHORT:
            # A typed footer over a scan: under 200 characters of text layer.
            pages.append(("mixed", ((page_marker_jpeg(number), (400, 566)), [FOOTER])))
        else:
            pages.append(("text", [f"Body text line {i} of page {number} with enough words to be real." for i in range(8)]))
    path = tmp_path / "scan.pdf"
    build_pdf(str(path), pages)
    derived, source = blob_layout(tmp_path, "scan", path.read_bytes(), "pdf")
    asyncio.run(extract_worker.run(Spec(op="extract", kind="pdf", derived_dir=derived, source=source, caps=caps_for("pdf", 1)), wall_s=60))
    return derived, source


def renderer(derived: str, source: str):
    async def do_render(pages):
        return await render.render_pages(derived, source, pages, caps=caps_for("pdf", 1), wall_s=60)

    return do_render


def run_stage(derived: str, source: str, **kwargs):
    kwargs.setdefault("budget", 1000)
    return asyncio.run(ocr_pages.run_stage(derived, render=renderer(derived, source), **kwargs))


# ================================================================= tests ==


def test_only_thin_pages_are_read_in_page_order_with_the_ocr_prompt_and_land_on_their_own_page(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    STUB.degenerate = {11}
    before = {p["page"]: p for p in pages_of(derived)}
    facts = run_stage(derived, source)
    assert sorted(STUB.calls) == [3, 5, 7, 9, 11]
    assert set(STUB.prompts) == {"OCR"}
    pages = {p["page"]: p for p in pages_of(derived)}
    for number in (3, 5, 7):
        assert pages[number]["source"] == "ocr"
        assert pages[number]["text"] == STUB.transcript(number)
    # A typed footer keeps its text and gains the transcript after it.
    assert pages[SHORT]["text"] == FOOTER + "\n" + STUB.transcript(SHORT)
    # A repetition loop is rejected: the page is exactly as the text layer left it.
    assert pages[11] == before[11] and "nije" not in json.dumps(pages)
    for number in (1, 2, 4, 12):
        assert pages[number] == before[number]
    assert facts == {
        "ocr_pages": 4,
        "ocr_skipped_pages": 0,
        "ocr_degenerate_pages": 1,
        "ocr_empty_pages": 0,
        "ocr_failed_pages": 0,
    }
    assert not os.listdir(os.path.join(derived, "renders"))
    with open(os.path.join(derived, "ocr.jsonl")) as fh:
        recorded = [json.loads(line) for line in fh]
    assert {r["page"]: r["status"] for r in recorded} == {3: "ok", 5: "ok", 7: "ok", 9: "ok", 11: "degenerate"}
    assert next(r for r in recorded if r["page"] == 11)["text"] == ""


def test_a_preamble_only_read_leaves_the_page_alone_and_a_preamble_line_is_never_merged(tmp_path):
    """The public side shares `engines.ocr.classify` with the chat app. Until
    2026-09-18 the engine's preamble — '":"', "result '", a "result" line in
    front of the text, all recorded on this deployment's engine with the
    "OCR" prompt this stage sends — was an `ok` read, so it became a page's
    text or was glued onto it and could be cited from the Files API."""
    derived, source = scanned_pdf(tmp_path)
    STUB.raw = {3: '":"', 5: "result '", 7: "result\n" + STUB.transcript(7)}
    before = {p["page"]: p for p in pages_of(derived)}
    facts = run_stage(derived, source)
    pages = {p["page"]: p for p in pages_of(derived)}
    assert pages[3] == before[3] and pages[5] == before[5]
    assert pages[7]["source"] == "ocr" and pages[7]["text"] == STUB.transcript(7)
    assert '":"' not in json.dumps(pages) and "result" not in json.dumps(pages)
    assert facts["ocr_empty_pages"] == 2 and facts["ocr_pages"] == 3
    recorded = ocr_pages.load_recorded(derived)
    assert {p: r["status"] for p, r in recorded.items()} == {3: "empty", 5: "empty", 7: "ok", 9: "ok", 11: "ok"}


def test_a_scanned_page_whose_text_is_shaped_like_a_region_keeps_all_of_it(tmp_path):
    """QA 2026-09-18, live answers (3 of 3 runs): the engine writes a slide's
    'input [1, 3, 224, 224]' as 'text [x, y, x, y]input [1, 3, 224, 224]'.
    The answer was cleaned twice, and the second pass took the exposed
    content for a region: page 3 became '' with source 'text' (its OCR
    thrown away and counted as an empty page) and page 5 lost its title."""
    derived, source = scanned_pdf(tmp_path)
    STUB.raw = {
        3: "title [58, 109, 397, 190]Tensor Shapes\ntext [74, 279, 421, 335]input [1, 3, 224, 224]",
        5: " result\ntitle [60, 109, 368, 188]Colour picker\ntext [77, 279, 327, 335][0, 0, 255, 255]\n"
           "text [74, 380, 268, 436]Primary blue",
    }
    facts = run_stage(derived, source)
    pages = {p["page"]: p for p in pages_of(derived)}
    assert (pages[3]["source"], pages[3]["text"]) == ("ocr", "Tensor Shapes\ninput [1, 3, 224, 224]")
    assert (pages[5]["source"], pages[5]["text"]) == ("ocr", "Colour picker\n[0, 0, 255, 255]\nPrimary blue")
    assert facts["ocr_empty_pages"] == 0


def test_a_page_the_reader_looped_on_with_preamble_tokens_is_degenerate_not_empty(tmp_path):
    """A loop of the model's own "ovi" is a failed read (ocr_degenerate_pages),
    not a blank page (ocr_empty_pages): stripping the preamble one token at a
    time used to eat the whole loop."""
    derived, source = scanned_pdf(tmp_path)
    STUB.raw = {3: "ovi " * 700}
    facts = run_stage(derived, source)
    recorded = ocr_pages.load_recorded(derived)
    assert recorded[3]["status"] == "degenerate", recorded[3]
    assert facts["ocr_degenerate_pages"] == 1 and facts["ocr_empty_pages"] == 0
    assert "ovi" not in json.dumps(pages_of(derived))


def test_a_scanned_page_keeps_the_row_the_engine_fused_behind_a_malformed_region(tmp_path):
    """Security review 2026-09-19, live on this path (a 'Matrix rows / [1, 2,
    3, 4] / [5, 6, 7, 8]' slide as a one-page PDF, prompt "OCR", 3 of 3
    runs): the engine wrote the first row behind a three-number region
    marker. 327a5ac dropped the whole line as chatter, so the page's
    citable text lost a row of numbers and the read still counted as ok."""
    derived, source = scanned_pdf(tmp_path)
    STUB.raw = {3: " result [0, 0, 2558][1, 2, 3, 4]\ntext [55, 456, 300, 530][5, 6, 7, 8]"}
    run_stage(derived, source)
    pages = {p["page"]: p for p in pages_of(derived)}
    assert (pages[3]["source"], pages[3]["text"]) == ("ocr", "[1, 2, 3, 4]\n[5, 6, 7, 8]")
    assert "2558" not in json.dumps(pages), "the marker's numbers are not on the page"


def test_the_page_budget_reads_the_first_thin_pages_and_lists_the_rest_as_skipped(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    facts = run_stage(derived, source, budget=2)
    assert sorted(STUB.calls) == [3, 5]
    assert facts["ocr_pages"] == 2 and facts["ocr_skipped_pages"] == 3
    with open(os.path.join(derived, "ocr_skipped.json")) as fh:
        assert json.load(fh) == [7, 9, 11]
    pages = {p["page"]: p for p in pages_of(derived)}
    assert pages[7]["source"] == "text" and pages[7]["text"] == ""


def test_every_unit_passes_the_public_ocr_gate_with_yield_to_chat(tmp_path, monkeypatch):
    derived, source = scanned_pdf(tmp_path)
    seen: List[Dict[str, Any]] = []
    real_hold = capacity.hold

    def spy(engine, **kwargs):
        seen.append({"engine": engine, **kwargs})
        return real_hold(engine, **kwargs)

    monkeypatch.setattr(capacity, "hold", spy)
    run_stage(derived, source)
    assert len(seen) == len(STUB.calls) == 5
    assert all(c["engine"] == "ocr" and c["yield_to_chat"] is True for c in seen)
    # No clock on a processing job's gate wait (2026-09-14): it ends on
    # admission or abandonment only.
    assert all(c["wait_s"] is None for c in seen)


def test_an_engine_that_is_down_defers_the_stage_and_records_nothing(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    STUB.down = True
    before = pages_of(derived)
    with pytest.raises(ocr_pages.EngineUnavailable):
        run_stage(derived, source)
    assert pages_of(derived) == before
    assert ocr_pages.load_recorded(derived) == {}
    assert not os.listdir(os.path.join(derived, "renders"))


def test_a_gate_that_refuses_after_its_wait_defers_the_stage(tmp_path, monkeypatch):
    derived, source = scanned_pdf(tmp_path)

    def refusing(engine, **kwargs):
        raise errors.model_at_capacity(retry_after=30)

    monkeypatch.setattr(capacity, "hold", refusing)
    with pytest.raises(ocr_pages.EngineUnavailable):
        run_stage(derived, source)
    assert STUB.calls == []


def test_one_page_the_engine_rejects_is_counted_and_the_others_are_still_merged(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    STUB.fail_pages = {5}
    facts = run_stage(derived, source)
    assert facts["ocr_failed_pages"] == 1 and facts["ocr_pages"] == 4
    # Recorded as its FIRST rejection, not final: the next attempt tries it once more.
    assert ocr_pages.load_recorded(derived)[5] == {"page": 5, "status": "failed", "attempts": 1}
    assert "Scanned page 5" not in {p["page"]: p["text"] for p in pages_of(derived)}.get(5, "")


def _record_ok(derived: str, pages) -> None:
    with open(os.path.join(derived, "ocr.jsonl"), "w") as fh:
        for page in pages:
            fh.write(json.dumps({"page": page, "status": "ok", "text": STUB.transcript(page)}) + "\n")


def test_a_page_the_engine_always_rejects_becomes_final_on_its_second_attempt_instead_of_failing_the_whole_pdf(tmp_path):
    # The review's reproduction: every other thin page was read by earlier
    # attempts, so the rejected page sits ALONE in its batch on every attempt.
    derived, source = scanned_pdf(tmp_path)
    _record_ok(derived, [3, 5, 7, 9])
    STUB.fail_pages = {11}
    with pytest.raises(ocr_pages.EngineUnavailable):
        run_stage(derived, source)  # first rejection: may be the engine, defer once
    assert STUB.calls == [11]
    facts = run_stage(derived, source)  # second rejection: final, the stage completes
    assert STUB.calls == [11, 11]
    assert facts["ocr_failed_pages"] == 1 and facts["ocr_pages"] == 4
    texts = {p["page"]: p["text"] for p in pages_of(derived)}
    assert texts[7] == STUB.transcript(7) and texts[9].startswith(FOOTER)
    STUB.calls.clear()
    again = run_stage(derived, source)  # a crash before the marker: nothing is read again
    assert STUB.calls == [] and again["ocr_failed_pages"] == 1


def test_a_failed_read_that_gives_no_reason_counts_against_its_page_and_cannot_defer_the_pdf_forever(tmp_path):
    from types import SimpleNamespace

    assert ocr_pages.failure_kind(SimpleNamespace(status="failed", text="")) == "rejected"
    assert ocr_pages.failure_kind(SimpleNamespace(status="failed", error="APITimeoutError: Request timed out.")) == "unreachable"
    assert ocr_pages.failure_kind(SimpleNamespace(status="failed", error="OCR is not enabled on this deployment")) == "unreachable"
    assert ocr_pages.failure_kind(SimpleNamespace(status="failed", error="BadRequestError: Error code: 400")) == "rejected"
    derived, source = scanned_pdf(tmp_path)
    _record_ok(derived, [3, 5, 7, 9])

    async def reasonless(images):
        return [SimpleNamespace(status="failed", text="")]

    with pytest.raises(ocr_pages.EngineUnavailable):
        asyncio.run(ocr_pages.run_stage(derived, budget=1000, render=renderer(derived, source), read=reasonless))
    facts = asyncio.run(ocr_pages.run_stage(derived, budget=1000, render=renderer(derived, source), read=reasonless))
    assert facts["ocr_failed_pages"] == 1 and facts["ocr_pages"] == 4


def test_with_ocr_switched_off_the_stage_reads_nothing_and_every_thin_page_keeps_its_text_layer(tmp_path, monkeypatch):
    derived, source = scanned_pdf(tmp_path)
    before = pages_of(derived)
    monkeypatch.setattr(settings, "ocr_enabled", False)
    rendered: List[Any] = []

    async def no_render(pages):
        rendered.append(pages)
        return {}

    facts = asyncio.run(ocr_pages.run_stage(derived, budget=1000, render=no_render))
    assert STUB.calls == [] and rendered == []
    assert pages_of(derived) == before
    assert facts["ocr_disabled"] is True and facts["ocr_skipped_pages"] == 5 and facts["ocr_pages"] == 0
    with open(os.path.join(derived, "ocr_skipped.json")) as fh:
        assert json.load(fh) == [3, 5, 7, 9, 11]


def test_on_the_last_attempt_an_engine_still_down_ends_the_stage_with_the_text_layers_instead_of_failing(tmp_path, monkeypatch):
    derived, source = scanned_pdf(tmp_path)
    before = pages_of(derived)
    STUB.down = True
    facts = run_stage(derived, source, final_attempt=True)
    assert facts["ocr_failed_pages"] == 5 and facts["ocr_pages"] == 0
    assert pages_of(derived) == before
    assert ocr_pages.load_recorded(derived) == {}  # an outage is never held against a page

    def refusing(engine, **kwargs):
        raise errors.model_at_capacity(retry_after=30)

    STUB.down = False
    monkeypatch.setattr(capacity, "hold", refusing)
    facts = run_stage(derived, source, final_attempt=True)
    assert facts["ocr_failed_pages"] == 5 and STUB.calls == []


def test_a_resumed_stage_reads_only_unrecorded_pages_and_never_merges_a_transcript_twice(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    with open(os.path.join(derived, "ocr.jsonl"), "w") as fh:
        fh.write(json.dumps({"page": 3, "status": "ok", "text": STUB.transcript(3)}) + "\n")
        fh.write('{"page": 5, "sta')  # a torn line from a crash mid-append
    run_stage(derived, source)
    assert sorted(STUB.calls) == [5, 7, 9, 11]
    first = {p["page"]: p["text"] for p in pages_of(derived)}
    assert first[3] == STUB.transcript(3)
    STUB.calls.clear()
    facts = run_stage(derived, source)  # a crash after the merge, before the marker
    assert STUB.calls == []
    assert {p["page"]: p["text"] for p in pages_of(derived)} == first
    assert facts["ocr_pages"] == 5


def test_a_checkpoint_that_stops_the_job_ends_the_stage_between_pages_without_merging(tmp_path):
    derived, source = scanned_pdf(tmp_path)
    before = pages_of(derived)
    calls = {"n": 0}

    class Stop(Exception):
        pass

    async def checkpoint():
        calls["n"] += 1
        if len(STUB.calls) >= 2:
            raise Stop()

    with pytest.raises(Stop):
        asyncio.run(
            ocr_pages.run_stage(derived, budget=1000, render=renderer(derived, source), checkpoint=checkpoint, concurrency=1)
        )
    assert len(STUB.calls) == 2
    assert pages_of(derived) == before
    assert sorted(ocr_pages.load_recorded(derived)) == sorted(STUB.calls)


def test_the_plan_counts_recorded_pages_against_the_budget_but_does_not_read_them_again():
    plan = ocr_pages.plan([9, 3, 5, 3, 0, 11], budget=3, already=[3])
    assert plan.pages == [5, 9] and plan.skipped == [11]
    assert ocr_pages.plan([1, 2], budget=0).pages == []
