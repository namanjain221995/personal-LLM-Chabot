"""The OCR classifier tells a read from the model's preamble junk (B5a).

Until 2026-09-18 `engines.ocr.classify` had three answers: empty (nothing
came back), degenerate (a repetition loop) and ok (everything else). So these
reads, all recorded against this deployment's Unlimited-OCR engine, were `ok`:

  '":"' and "result '"         the "OCR" prompt, four legible video slides
                                (speech-video audit, 2026-09-17)
  'result\\nSERVER ROOM B'       the "OCR" prompt on a dark sign
  'ovi\\nSERVER ROOM B\\n...'     "document parsing" on the same sign
                                (vision builder, 2026-09-18)

and every consumer of `ok` believed them: the video stage said "4/4 frames had
readable text" when two were read, the junk was written into the downloadable
screen_text.txt, and the chat image route and the public Files API accepted
the preamble as the picture's text. These tests pin the classifier's answer
on those recorded strings, and on the real short reads it must still keep.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.engines import ocr

# The loop fixtures the video suite already pins as degenerate
# (tests/test_video_media_hardening.py): both BEGIN with an "ovi…" token, so
# they are exactly the reads a careless preamble rule would empty out.
_LOOP = "ovišnje pjeski je " + "nije " * 700
_GLUED_LOOP = "ovišati" + "ševanja" * 400


# ---------------------------------------------------- the recorded junk --


@pytest.mark.parametrize("raw", ['":"', "result '", ": 3", ":", "result", "ovi", "  \":\"  \n"])
def test_a_preamble_with_nothing_after_it_is_an_empty_read(raw):
    read = ocr.classify(raw)
    assert read.status == "empty", (raw, read)
    assert read.text == "", "an empty read carries no text for anyone to index"


@pytest.mark.parametrize(
    "raw",
    [
        "result\nSERVER ROOM B",
        "ovi\nSERVER ROOM B",
        "result '\nSERVER ROOM B",
        '":"\nSERVER ROOM B',
        ": 3\nSERVER ROOM B",
    ],
)
def test_a_preamble_line_is_dropped_and_the_read_behind_it_kept(raw):
    read = ocr.classify(raw)
    assert read.status == "ok"
    assert read.text == "SERVER ROOM B"


def test_the_recorded_document_parsing_read_of_the_sign_loses_its_ovi_line():
    """The line after it is the model's own and stays: judging whether a date
    is invented is not something a classifier can do from the text alone."""
    read = ocr.classify("ovi\nSERVER ROOM B\n[Unreadable due to extreme blurriness]\n1. 2017年1月1日")
    assert read.status == "ok"
    assert read.text.startswith("SERVER ROOM B\n")
    assert "ovi" not in read.text


def test_an_ovi_token_before_text_on_the_same_line_is_dropped():
    assert ocr.clean_transcript("ovi Weekly Planning Meeting\nAgenda") == "Weekly Planning Meeting\nAgenda"


# ------------------------- measured 2026-09-18: 96 reads, 16 labelled images --
#
# Raw answers of the live engine on synthetic slides, screenshots, signs and
# pages. The engine writes what it reads as "type [x, y, x, y]text" region
# lines; every answer that had region lines opened with one line that had
# none, and that line was the model talking in all 73 cases.


@pytest.mark.parametrize(
    "raw, text",
    [
        (" output\ntitle [61, 104, 678, 188]Quarterly Planning Review\ntext [75, 279, 535, 333]1. Pricing",
         "Quarterly Planning Review\n1. Pricing"),
        (", T\ntitle [78, 58, 595, 88]Master Services Agreement", "Master Services Agreement"),
        (": I\ntitle [80, 60, 300, 110]INVOICE", "INVOICE"),
        (": 1. General Text, Math Processing\ntitle [61, 104, 495, 186]Migration Timeline", "Migration Timeline"),
        (" (提示: )\nheader [434, 15, 544, 40]Orders - Admin", "Orders - Admin"),
        ("and non-text figures\ntext [61, 92, 792, 172]Regional Revenue (USD thousands)",
         "Regional Revenue (USD thousands)"),
        (" result, which consists of underscores. Underscores are not equivalent to a solid line.\n"
         "title [60, 96, 453, 172]Retry with backoff", "Retry with backoff"),
        ("ovi\ntitle [61, 104, 495, 186]Migration Timeline", "Migration Timeline"),
        # The same answer with vLLM keeping special tokens (probed live):
        # the model card's det-block spelling of a region line.
        ("and non-text figures\n<|det|>text [61, 95, 792, 172]<|/det|>Regional Revenue (USD thousands)",
         "Regional Revenue (USD thousands)"),
    ],
)
def test_a_line_before_the_engine_s_layout_is_dropped(raw, text):
    read = ocr.classify(raw)
    assert (read.status, read.text) == ("ok", text)


def test_only_one_line_before_the_layout_goes():
    """Measured: the video frame of the orders screen came back with the
    window title, written without a region, second."""
    raw = ", or outputatted values.\nOrder - Admin\ntext [28, 89, 135, 132]Orders"
    assert ocr.clean_transcript(raw) == "Order - Admin\nOrders"


def test_a_first_line_inside_the_layout_is_the_image_s_text():
    raw = "title [1, 2, 3, 4]Results\ntext [5, 6, 7, 8]Q3 revenue up 12%"
    assert ocr.classify(raw).text == "Results\nQ3 revenue up 12%"
    # Without any region lines there is no layout to stand outside of.
    assert ocr.classify("Results\nQ3 revenue up 12%").text == "Results\nQ3 revenue up 12%"


@pytest.mark.parametrize("raw", [" result.", " result\nimage [0, 0, 999, 999]", "result:", "output"])
def test_the_measured_answers_for_pictures_with_no_text_are_empty(raw):
    assert ocr.classify(raw).status == "empty"


# ------------------------------------------------ real text must survive --


@pytest.mark.parametrize(
    "raw",
    [
        "Results for Q3",
        "Result\nRevenue up 4%",
        "result of the vote: 12 for, 3 against",
        "Ovid, Metamorphoses",
        "oviparous species\nfrogs, birds",
        ": the colon heading\nnext",
        "Q3",
        "5%",
        "$5",
        "4471",
        "OK",
        "東京",
    ],
)
def test_a_real_read_is_ok_and_unchanged(raw):
    read = ocr.classify(raw)
    assert read.status == "ok", (raw, read)
    assert read.text == raw.strip()


def test_one_alphanumeric_character_is_not_a_read():
    """Fewer than two content characters after cleaning is what the model
    says when it saw nothing it could read. A sign that belongs to a number
    counts ("5%" above), but only beside a letter or digit ("%" alone does
    not)."""
    for raw in ("'", "-", "|", "*", "7", "%", "$", "|  |\n|---|", "[]"):
        assert ocr.classify(raw).status == "empty", raw


def test_the_truncation_marker_does_not_turn_junk_into_a_read():
    """`_ocr_one` appends this note to a transcript cut off at the output
    limit. It is the client's words, not the image's."""
    raw = '":"\n[transcript truncated at the OCR output limit]'
    assert ocr.classify(raw).status == "empty"


# --------------------------------------------------- loops stay loops --


@pytest.mark.parametrize(
    "loop",
    [_LOOP, _GLUED_LOOP, "nije " * 400, "result\n" + "nije " * 400],
    ids=["ovi-word-loop", "ovi-glued-loop", "word-loop", "result-then-loop"],
)
def test_a_loop_is_degenerate_through_the_classifier_and_the_production_path(loop):
    assert ocr.classify(loop).status == "degenerate"
    # The production path cleans in `_ocr_one` and classifies after it.
    assert ocr.classify(ocr.clean_transcript(loop)).status == "degenerate"


def test_cleaning_is_idempotent_so_the_second_pass_in_classify_changes_nothing():
    for raw in (
        "result\nSERVER ROOM B",
        "ovi\nresult '\nSERVER ROOM B",
        "text [31, 306, 212, 347]Vendor: TechSara",
        _LOOP,
        "Results for Q3",
    ):
        once = ocr.clean_transcript(raw)
        assert ocr.clean_transcript(once) == once, raw


# ------------------------------------- the reads every consumer receives --


def _engine_answering(monkeypatch, answers):
    """Stand in for the sidecar: each call returns the next raw answer."""
    monkeypatch.setattr(settings, "ocr_enabled", True)
    queue = list(answers)

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    return _Resp(queue.pop(0))

    from app import llm

    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None: Client())
    monkeypatch.setattr(ocr, "concurrency", lambda: 1)


def test_read_images_reports_the_recorded_junk_as_empty(monkeypatch):
    _engine_answering(
        monkeypatch,
        ['":"', "Weekly Planning Meeting\nAgenda", "result '", "result\nSERVER ROOM B"],
    )
    reads = asyncio.run(ocr.read_images(["A", "B", "C", "D"], prompt="OCR"))
    assert [r.status for r in reads] == ["empty", "ok", "empty", "ok"]
    assert reads[3].text == "SERVER ROOM B"


def test_the_image_route_evidence_block_carries_no_preamble(monkeypatch):
    _engine_answering(monkeypatch, ["result\nSERVER ROOM B", '":"'])
    reads = asyncio.run(ocr.read_images(["A", "B"], prompt="OCR"))
    block = ocr.evidence_block(reads, "image")
    assert "--- Image 1 of 2 ---\nSERVER ROOM B" in block
    assert "Image 2 of 2" not in block, "a preamble-only read is not evidence"
    assert "result" not in block.split("---", 1)[1]


def test_ocr_images_keeps_its_contract_and_drops_the_junk(monkeypatch):
    """`ocr_images` (the document route's view) still forwards a degenerate
    read's text — document.py is frozen this round and that contract is its
    own — but a preamble-only read is now empty, so it arrives as ''."""
    _engine_answering(monkeypatch, ['":"', "ovi\nInvoice 42", "nije " * 400])
    texts = asyncio.run(ocr.ocr_images(["A", "B", "C"]))
    assert texts[0] == ""
    assert texts[1] == "Invoice 42"
    assert texts[2].startswith("nije nije"), "unchanged: degenerate text is still forwarded here"
