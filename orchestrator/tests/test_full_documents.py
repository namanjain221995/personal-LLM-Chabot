"""Full-document reading + per-conversation document memory (2026-08-07).

The old pipeline read the first 6 PDF pages of everything and forgot the
file after its one turn; .docx was rejected outright.
"""
import asyncio
import base64
import io
import zipfile

import pytest

from app import db
from app.core.docx import DocxError, extract_docx_text, is_docx
from app.engines.document import run_pdf_engine


def make_docx(paragraphs, rows=()) -> bytes:
    """A minimal but genuine .docx: zip + word/document.xml."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    table = ""
    if rows:
        trs = "".join(
            "<w:tr>" + "".join(
                f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row
            ) + "</w:tr>"
            for row in rows
        )
        table = f"<w:tbl>{trs}</w:tbl>"
    xml = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}">'
        f"<w:body>{paras}{table}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_paragraphs_and_tables_are_extracted():
    data = make_docx(
        ["Product Requirements Document", "Build a local-first platform."],
        rows=[("Item", "Decision"), ("Primary runtime", "DGX Spark")],
    )
    assert is_docx(data)
    text = extract_docx_text(data)
    assert "Product Requirements Document" in text
    assert "Primary runtime\tDGX Spark" in text


def test_a_zip_that_is_not_word_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not word")
    assert not is_docx(buf.getvalue())
    with pytest.raises(DocxError):
        extract_docx_text(buf.getvalue())


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def answer(self):
        return "".join(d["text"] for e, d in self.events if e == "token")


@pytest.fixture()
def fake_model(monkeypatch):
    from app import llm

    async def fake_stream(msgs, **kw):
        # Echo back whether the document text reached the prompt.
        user = msgs[-1]["content"]
        text = " ".join(
            p["text"] for p in user if p.get("type") == "text"
        ) if isinstance(user, list) else str(user)
        yield "token", ("SAW-DOC" if "unified memory" in text else "NO-DOC")

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)


def test_docx_upload_is_read_and_remembered(fake_model, monkeypatch):
    data = make_docx(["The DGX Spark has 128 GB unified memory."])
    b64 = base64.b64encode(data).decode()
    rec = Rec()
    conv = "conv-docmem-1"
    answer = asyncio.run(
        run_pdf_engine("what memory does it have?", b64, "prd.docx", [], rec.emit,
                       conversation_id=conv)
    )
    assert answer == "SAW-DOC"
    stored = db.get_documents(conv)
    assert stored and "unified memory" in stored[0]["text"]
    assert stored[0]["filename"] == "prd.docx"


def test_plain_text_uploads_work_too(fake_model):
    b64 = base64.b64encode("notes: 128 GB unified memory box".encode()).decode()
    rec = Rec()
    answer = asyncio.run(
        run_pdf_engine("what memory?", b64, "notes.txt", [], rec.emit)
    )
    assert answer == "SAW-DOC"


def test_documents_table_upserts_by_filename():
    conv = "conv-docmem-2"
    db.save_document(conv, "a.pdf", "first version", 3)
    db.save_document(conv, "a.pdf", "second version", 3)
    docs = db.get_documents(conv)
    assert len(docs) == 1
    assert docs[0]["text"] == "second version"
    assert docs[0]["total_pages"] == 3
