"""Documents at ChatGPT scale: no size or count limit in the app since
2026-10-03 (512 MB files and five per message before; docs/chat-media/LIMITS.md),
chunked over the tunnel, never read into memory whole.

WHY (owner request 2026-09-02). The chat attach path capped documents at
25 MB because a PDF travelled as base64 INSIDE the chat JSON — a limit set by
the transport, not by anything the engine needs. Meanwhile the dataset rail
already streamed 100 GB to disk. Documents now ride that rail (with
purpose=document: keep the original bytes, extract nothing), the chat request
carries references, and files too big for Cloudflare's 100 MB edge cap arrive
in parts and are reassembled server-side.
"""
from __future__ import annotations

import base64
import os

import pytest

from app import uploads as up
from app.config import settings


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


@pytest.fixture()
def bob(login_client):
    return login_client("bob")


@pytest.fixture()
def conv(alice):
    resp = alice.post(
        "/history/conversations", json={"id": "conv-docs", "title": "docs"}
    )
    assert resp.status_code == 200, resp.text
    return "conv-docs"


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))


PDF_BYTES = b"%PDF-1.4 fake but sniffable content for tests\n" * 100


# ── purpose=document on the single-shot rail ────────────────────────────────


def test_document_purpose_keeps_the_original_bytes(alice, conv):
    resp = alice.post(
        "/uploads",
        files={"file": ("contract.pdf", PDF_BYTES, "application/pdf")},
        data={"conversation_id": conv, "purpose": "document"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    root = up.upload_root(conv, body["upload_id"])
    stored = os.path.join(root, "_original", "contract.pdf")
    assert os.path.isfile(stored), "a document's original bytes must survive"
    with open(stored, "rb") as fh:
        assert fh.read() == PDF_BYTES
    assert body["bytes"] == len(PDF_BYTES)
    assert body["profile"] == [], "documents are not profiled as datasets"


def test_dataset_purpose_still_drops_the_original(alice, conv):
    """The dataset contract is unchanged: extract, profile, drop the archive."""
    resp = alice.post(
        "/uploads",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        data={"conversation_id": conv},
    )
    assert resp.status_code == 200, resp.text
    root = up.upload_root(conv, resp.json()["upload_id"])
    assert not os.path.isdir(os.path.join(root, "_original"))
    assert os.path.isdir(os.path.join(root, "extracted"))


def test_an_unknown_purpose_is_rejected(alice, conv):
    resp = alice.post(
        "/uploads",
        files={"file": ("x.pdf", PDF_BYTES, "application/pdf")},
        data={"conversation_id": conv, "purpose": "exfiltrate"},
    )
    assert resp.status_code == 400


# ── the chunked rail ────────────────────────────────────────────────────────


def _init(client, conv, filename="big.pdf", purpose="document"):
    resp = client.post(
        "/uploads/chunked/init",
        data={"conversation_id": conv, "filename": filename, "purpose": purpose},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def test_chunked_document_reassembles_byte_for_byte(alice, conv):
    upload_id = _init(alice, conv)
    part_a, part_b = PDF_BYTES[: len(PDF_BYTES) // 2], PDF_BYTES[len(PDF_BYTES) // 2:]
    for i, part in enumerate((part_a, part_b)):
        resp = alice.put(
            f"/uploads/chunked/{conv}/{upload_id}/part/{i}", content=part
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["received"] == len(part)
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_id"] == upload_id
    assert body["bytes"] == len(PDF_BYTES)
    stored = os.path.join(up.upload_root(conv, upload_id), "_original", "big.pdf")
    with open(stored, "rb") as fh:
        assert fh.read() == PDF_BYTES, "parts must concatenate in order"
    # The scaffolding is gone once assembled.
    root = up.upload_root(conv, upload_id)
    assert not os.path.isdir(os.path.join(root, up._PARTS_DIR))


def test_a_missing_part_fails_loudly_not_quietly(alice, conv):
    """Silently assembling around a hole would hand the engine a corrupt file."""
    upload_id = _init(alice, conv)
    assert alice.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/0", content=b"aa"
    ).status_code == 200
    assert alice.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/2", content=b"cc"
    ).status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    # 2026-09-10: a hole is named, not described — the client sends exactly
    # the part that is missing and calls complete again.
    assert resp.status_code == 409
    assert resp.json()["missing_parts"] == [1]
    assert resp.json()["accepted_parts"] == [0, 2]


def test_a_forged_upload_id_is_a_404_not_a_write(alice, conv):
    """init mints ids; a guessed directory name must not become storage."""
    forged = "f" * 32
    assert alice.put(
        f"/uploads/chunked/{conv}/{forged}/part/0", content=b"x"
    ).status_code == 404
    assert alice.post(
        f"/uploads/chunked/{conv}/{forged}/complete"
    ).status_code == 404


def test_chunked_uploads_are_owner_scoped(alice, bob, conv):
    """conv belongs to alice; bob's session must see 404 everywhere."""
    upload_id = _init(alice, conv)
    assert bob.post(
        "/uploads/chunked/init",
        data={"conversation_id": conv, "filename": "x.pdf", "purpose": "document"},
    ).status_code == 404
    assert bob.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/0", content=b"x"
    ).status_code == 404
    assert bob.post(
        f"/uploads/chunked/{conv}/{upload_id}/complete"
    ).status_code == 404


def test_part_index_is_bounded(alice, conv):
    upload_id = _init(alice, conv)
    assert alice.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/{up._MAX_PARTS}", content=b"x"
    ).status_code == 400


def test_an_oversized_part_is_413_and_leaves_no_debris(alice, conv, monkeypatch):
    monkeypatch.setattr(up, "_PART_CAP", 8)
    upload_id = _init(alice, conv)
    resp = alice.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/0", content=b"123456789"
    )
    assert resp.status_code == 413
    parts_dir = os.path.join(up.upload_root(conv, upload_id), "_parts")
    assert os.listdir(parts_dir) == [], "the truncated part must not linger"


def test_chunked_dataset_lands_on_the_dataset_finaliser(alice, conv):
    upload_id = _init(alice, conv, filename="data.csv", purpose="dataset")
    assert alice.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/0", content=b"a,b\n1,2\n"
    ).status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == 1
    root = up.upload_root(conv, upload_id)
    assert os.path.isdir(os.path.join(root, "extracted"))
    assert not os.path.isdir(os.path.join(root, "_original"))


# ── resolving references in a chat request ──────────────────────────────────


class _Req:
    def __init__(self, pdf_uploads=None, pdf=None, pdf_filename=None):
        self.pdf_uploads = pdf_uploads
        self.pdf_data = pdf
        self.pdf_filename = pdf_filename


def _stored_document(conv, name=b"stored words", filename="notes.txt"):
    import uuid as _uuid

    upload_id = _uuid.uuid4().hex
    original = os.path.join(up.upload_root(conv, upload_id), "_original")
    os.makedirs(original)
    with open(os.path.join(original, filename), "wb") as fh:
        fh.write(name)
    return upload_id


def test_references_resolve_to_the_stored_bytes():
    from app.main import _resolve_document_refs
    import asyncio

    upload_id = _stored_document("conv-r")
    docs, images, err = asyncio.run(
        _resolve_document_refs(
            _Req(pdf_uploads=[{"upload_id": upload_id, "name": "client-name.txt"}]),
            "conv-r",
        )
    )
    assert err is None and images == []
    assert docs == [("notes.txt", base64.b64encode(b"stored words").decode())]
    # The STORED filename won — the client's claim is advisory only.


def test_a_swept_reference_is_one_clear_sentence():
    from app.main import _resolve_document_refs
    import asyncio

    docs, _images, err = asyncio.run(
        _resolve_document_refs(
            _Req(pdf_uploads=[{"upload_id": "a" * 32, "name": "gone.pdf"}]),
            "conv-r",
        )
    )
    assert docs == []
    assert "gone.pdf" in err and "re-attach" in err


def test_only_the_technical_ceiling_refuses_documents():
    """No limit in the app since 2026-10-03 (docs/chat-media/LIMITS.md):
    1,000 references is the only count refused."""
    from app.main import _resolve_document_refs
    import asyncio

    refs = [{"upload_id": "a" * 32, "name": f"d{i}.pdf"} for i in range(1000)]
    docs, _images, err = asyncio.run(_resolve_document_refs(_Req(pdf_uploads=refs), "c"))
    assert docs == [] and err == "A message can carry at most 999 documents."


def test_twenty_documents_in_one_message_are_all_read(monkeypatch):
    """docs/chat-media/LIMITS.md (2026-10-03): 20 documents per message, every
    one resolved and merged into the one question, sharing ONE context
    budget (the excerpt is not twenty budgets long)."""
    import asyncio

    from app.engines import document as eng
    from app.main import _resolve_document_refs

    ids = [_stored_document("conv-20", f"facts of file {i}".encode(), f"f{i:02d}.txt") for i in range(20)]
    refs = [{"upload_id": u, "name": f"f{i:02d}.txt"} for i, u in enumerate(ids)]
    docs, images, err = asyncio.run(_resolve_document_refs(_Req(pdf_uploads=refs), "conv-20"))
    assert err is None and images == []
    assert [name for name, _ in docs] == [f"f{i:02d}.txt" for i in range(20)]

    seen = {}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    async def emit(kind, payload):
        pass

    monkeypatch.setattr(eng.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr("app.db.save_document", lambda *a, **k: None)
    assert asyncio.run(eng.run_pdf_engine_multi("compare them", docs, [], emit)) == "ok"
    user = seen["messages"][-1]["content"]
    text = " ".join(p.get("text", "") for p in user if p.get("type") == "text")
    assert "20 documents were uploaded and ALL were read" in text
    assert "===== Document 20: f19.txt =====" in text
    excerpt = text.split("Document text (most relevant sections):", 1)[1]
    assert len(excerpt) <= eng.DOC_CONTEXT_CHARS + 200


def test_inline_pdf_still_rides_along():
    from app.main import _resolve_document_refs
    import asyncio

    upload_id = _stored_document("conv-r2")
    docs, _images, err = asyncio.run(
        _resolve_document_refs(
            _Req(
                pdf_uploads=[{"upload_id": upload_id}],
                pdf=base64.b64encode(b"inline").decode(),
                pdf_filename="small.txt",
            ),
            "conv-r2",
        )
    )
    assert err is None and len(docs) == 2
    assert docs[1] == ("small.txt", base64.b64encode(b"inline").decode())


# ── the engine reads several documents as one question ─────────────────────


def test_the_engine_merges_documents_with_labels(monkeypatch):
    import asyncio

    from app.engines import document as eng

    seen = {}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    monkeypatch.setattr(eng.llm, "stream_chat_events", fake_stream)
    saved = []
    monkeypatch.setattr(
        "app.db.save_document",
        lambda conv, name, text, pages: saved.append((conv, name)),
    )

    async def emit(kind, payload):
        pass

    docs = [
        ("a.txt", base64.b64encode(b"alpha facts").decode()),
        ("b.txt", base64.b64encode(b"beta figures").decode()),
    ]
    out = asyncio.run(
        eng.run_pdf_engine_multi(
            "compare them", docs, [], emit, conversation_id="c1"
        )
    )
    assert out == "ok"
    user = seen["messages"][-1]["content"]
    text = " ".join(p.get("text", "") for p in user if p.get("type") == "text")
    assert "===== Document 1: a.txt =====" in text
    assert "===== Document 2: b.txt =====" in text
    assert "2 documents were uploaded" in text
    assert [name for _, name in saved] == ["a.txt", "b.txt"]


def test_a_single_document_keeps_its_original_header(monkeypatch):
    """Zero drift for the path every existing conversation uses."""
    import asyncio

    from app.engines import document as eng

    seen = {}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    monkeypatch.setattr(eng.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr("app.db.save_document", lambda *a: None)

    async def emit(kind, payload):
        pass

    asyncio.run(
        eng.run_pdf_engine(
            "summarise",
            base64.b64encode(b"just words").decode(),
            "one.txt",
            [],
            emit,
            conversation_id="c2",
        )
    )
    user = seen["messages"][-1]["content"]
    text = " ".join(p.get("text", "") for p in user if p.get("type") == "text")
    assert "Document: one.txt" in text
    assert "=====" not in text, "single documents must not grow merge markers"


# ── archives open like ChatGPT opens them ──────────────────────────────────


def _zip_upload(conv, members: dict, name="bundle.zip"):
    import io
    import uuid as _uuid
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for member, payload in members.items():
            zf.writestr(member, payload)
    upload_id = _uuid.uuid4().hex
    original = os.path.join(up.upload_root(conv, upload_id), "_original")
    os.makedirs(original)
    with open(os.path.join(original, name), "wb") as fh:
        fh.write(buf.getvalue())
    return upload_id


# A 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_a_zip_becomes_manifest_plus_members():
    import asyncio

    from app.main import _resolve_document_refs

    upload_id = _zip_upload(
        "conv-z",
        {
            "notes/readme.md": b"alpha notes",
            "report.txt": b"beta report",
            "photo.png": _PNG,
            "tool.exe": b"MZ\x00\x00binary",
        },
    )
    docs, images, err = asyncio.run(
        _resolve_document_refs(
            _Req(pdf_uploads=[{"upload_id": upload_id, "name": "bundle.zip"}]),
            "conv-z",
        )
    )
    assert err is None
    names = [n for n, _ in docs]
    assert names[0] == "bundle.zip (archive contents)"
    assert "bundle.zip/notes/readme.md" in names
    assert "bundle.zip/report.txt" in names
    # The manifest names EVERYTHING, including what was not read.
    manifest = base64.b64decode(docs[0][1]).decode()
    assert "photo.png" in manifest and "attached as an image" in manifest
    assert "tool.exe" in manifest and "listed only" in manifest
    # The image rides as a data URL the engine can hand to the model.
    assert len(images) == 1 and images[0].startswith("data:image/png;base64,")
    # The exe was NOT read as a document.
    assert not any("tool.exe" in n for n in names)


def test_archive_expansion_is_cached_in_extracted():
    import asyncio

    from app.main import _resolve_document_refs

    upload_id = _zip_upload("conv-z2", {"a.txt": b"aa"})
    ref = _Req(pdf_uploads=[{"upload_id": upload_id, "name": "bundle.zip"}])
    asyncio.run(_resolve_document_refs(ref, "conv-z2"))
    extracted = os.path.join(up.upload_root("conv-z2", upload_id), "extracted")
    assert os.path.isfile(os.path.join(extracted, "a.txt"))
    # Second resolve reuses the directory rather than re-extracting.
    docs, _images, err = asyncio.run(_resolve_document_refs(ref, "conv-z2"))
    assert err is None and any(n.endswith("a.txt") for n, _ in docs)


def test_a_docx_is_never_mistaken_for_an_archive():
    """A .docx IS a zip container; sniffing alone would unzip a Word file
    into its XML skeleton. The extension must win."""
    import asyncio
    import io
    import zipfile

    from app.main import _resolve_document_refs

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:t>hello</w:t>")
    upload_id = _stored_document("conv-z3", buf.getvalue(), "letter.docx")
    docs, images, err = asyncio.run(
        _resolve_document_refs(
            _Req(pdf_uploads=[{"upload_id": upload_id, "name": "letter.docx"}]),
            "conv-z3",
        )
    )
    assert err is None
    assert [n for n, _ in docs] == ["letter.docx"], "no expansion for docx"


def test_a_binary_member_becomes_an_honest_stub(monkeypatch):
    """Mojibake in the prompt helps nobody; a named stub does."""
    import asyncio

    from app.engines import document as eng

    async def emit(kind, payload):
        pass

    doc, err = asyncio.run(
        eng._read_one("tool.bin", base64.b64encode(b"MZ\x00\x01\x02junk").decode(), emit)
    )
    assert err is None
    assert "Binary file: tool.bin" in doc.full_text
    assert "not readable as text" in doc.full_text


def test_attached_images_ride_into_the_document_prompt(monkeypatch):
    """"Compare the chart to the report": images sent WITH documents reach the
    same prompt as extra image_url parts (2026-09-02)."""
    import asyncio

    from app.engines import document as eng

    seen = {}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    monkeypatch.setattr(eng.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr("app.db.save_document", lambda *a: None)

    async def emit(kind, payload):
        pass

    asyncio.run(
        eng.run_pdf_engine_multi(
            "compare the chart to the report",
            [("report.txt", base64.b64encode(b"quarterly numbers").decode())],
            [],
            emit,
            conversation_id="c3",
            extra_images=["data:image/png;base64,QUJD"],
        )
    )
    user = seen["messages"][-1]["content"]
    urls = [p["image_url"]["url"] for p in user if p.get("type") == "image_url"]
    assert urls == ["data:image/png;base64,QUJD"]


# ── the download the whole ladder ends at ──────────────────────────────────


def test_a_document_downloads_its_original_bytes(alice, conv):
    """THE regression behind "This upload has expired": documents keep their
    bytes in _original, and the download endpoint only looked in extracted —
    so every document card 410'd minutes after becoming openable at all."""
    up_resp = alice.post(
        "/uploads",
        files={"file": ("contract.pdf", PDF_BYTES, "application/pdf")},
        data={"conversation_id": conv, "purpose": "document"},
    )
    assert up_resp.status_code == 200, up_resp.text
    upload_id = up_resp.json()["upload_id"]

    got = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert got.status_code == 200, got.text
    assert got.content == PDF_BYTES
    assert got.headers["content-type"].startswith("application/pdf")


def test_a_swept_document_is_still_an_honest_410(alice, conv):
    import shutil as _shutil

    up_resp = alice.post(
        "/uploads",
        files={"file": ("gone.pdf", PDF_BYTES, "application/pdf")},
        data={"conversation_id": conv, "purpose": "document"},
    )
    upload_id = up_resp.json()["upload_id"]
    _shutil.rmtree(os.path.join(up.upload_root(conv, upload_id), "_original"))
    # And its lasting copy (2026-10-02, docs/chat-media/CONTRACT.md §9), which
    # otherwise outlives the sweep: nothing left on disk is an honest 410.
    _shutil.rmtree(os.path.dirname(up.lasting_path(conv, upload_id)))
    assert alice.get(f"/uploads/{conv}/{upload_id}/file").status_code == 410


def test_dataset_downloads_are_unchanged(alice, conv):
    """The extracted-first order stays: a dataset member still serves."""
    up_resp = alice.post(
        "/uploads",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        data={"conversation_id": conv},
    )
    upload_id = up_resp.json()["upload_id"]
    got = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert got.status_code == 200
    assert got.content == b"a,b\n1,2\n"


# ── no limit (docs/chat-media/LIMITS.md, 2026-10-03) ─────────────────────────


def _capture_engine(monkeypatch):
    from app.engines import document as eng

    seen: dict = {"saved": {}, "tokens": []}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    monkeypatch.setattr(eng.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(
        "app.db.save_document", lambda conv, name, text, total: seen["saved"].__setitem__(name, text)
    )
    return seen


def _prompt_text(seen) -> str:
    return " ".join(p.get("text", "") for p in seen["messages"][-1]["content"] if p.get("type") == "text")


def test_fifty_documents_share_one_budget_not_fifty(monkeypatch):
    """50 documents in one message: all resolved and read, the excerpt is the
    one DOC_CONTEXT_CHARS, and the text they keep is one turn budget shared
    (each keeps its share, the header and a closing line say so)."""
    import asyncio

    from app.engines import document as eng
    from app.main import _resolve_document_refs

    monkeypatch.setattr(eng, "DOC_TURN_TEXT_CHARS", 50 * 10_000)
    body = ("fact " * 4_000).encode()  # 20,000 characters each
    ids = [_stored_document("conv-50", body + f" file {i}".encode(), f"f{i:02d}.txt") for i in range(50)]
    refs = [{"upload_id": u, "name": f"f{i:02d}.txt"} for i, u in enumerate(ids)]
    docs, images, err = asyncio.run(_resolve_document_refs(_Req(pdf_uploads=refs), "conv-50"))
    assert err is None and images == [] and len(docs) == 50

    seen = _capture_engine(monkeypatch)
    sent = []

    async def emit(kind, payload):
        sent.append((kind, payload))

    answer = asyncio.run(eng.run_pdf_engine_multi("compare them", docs, [], emit, conversation_id="conv-50"))
    text = _prompt_text(seen)
    assert "50 documents were uploaded and ALL were read (some only in part" in text
    assert "  50. f49.txt — only its first 10,000 characters were read" in text
    excerpt = text.split("Document text (most relevant sections):", 1)[1]
    assert len(excerpt) <= eng.DOC_CONTEXT_CHARS + 200
    assert len(seen["saved"]) == 50 and all(len(t) <= 10_000 for t in seen["saved"].values())
    assert "only its first 10,000 characters were read" in answer
    assert answer.startswith("ok") and "_Read in part" in answer


def test_a_document_over_512_mb_is_read_without_reading_it_whole(monkeypatch, tmp_path):
    """A 600 MB document (a SPARSE file: nothing real is written) resolves to
    a file the engine reads from disk within its text budget; memory stays
    within a few MB and the answer says what part was read."""
    import asyncio
    import tracemalloc

    from app.engines import document as eng
    from app.main import _resolve_document_refs

    upload_id = _stored_document("conv-big", ("word " * 4_000).encode(), "huge.txt")
    path = os.path.join(up.upload_root("conv-big", upload_id), "_original", "huge.txt")
    with open(path, "r+b") as fh:
        fh.truncate(600 * 1024 * 1024)
    assert os.path.getsize(path) == 600 * 1024 * 1024

    seen = _capture_engine(monkeypatch)

    async def emit(kind, payload):
        pass

    async def run():
        docs, _images, err = await _resolve_document_refs(
            _Req(pdf_uploads=[{"upload_id": upload_id, "name": "huge.txt"}]), "conv-big"
        )
        assert err is None and len(docs) == 1 and isinstance(docs[0], eng.DocFile)
        return await eng.run_pdf_engine_multi("summarise", docs, [], emit, conversation_id="conv-big")

    tracemalloc.start()
    try:
        answer = asyncio.run(run())
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024, f"peak {peak:,} bytes: the file was read whole"
    assert len(seen["saved"]["huge.txt"]) == eng.DOC_MAX_CHARS
    assert "only its first 400,000 characters were read (600 MB file)" in _prompt_text(seen)
    assert "_Read in part — **huge.txt**: only its first 400,000 characters were read" in answer


def test_the_whole_read_budget_is_per_turn_and_spent_in_order(monkeypatch):
    """Under DOC_WHOLE_READ_BYTES documents resolve exactly as before
    (name, base64); the first one past it becomes a file read from disk."""
    import asyncio

    from app.engines import document as eng
    from app.main import _resolve_document_refs

    monkeypatch.setattr(eng, "DOC_WHOLE_READ_BYTES", 10)
    a = _stored_document("conv-wb", b"12345678", "a.txt")
    b = _stored_document("conv-wb", b"abcdefgh", "b.txt")
    docs, _images, err = asyncio.run(_resolve_document_refs(
        _Req(pdf_uploads=[{"upload_id": a, "name": "a.txt"}, {"upload_id": b, "name": "b.txt"}]), "conv-wb"
    ))
    assert err is None
    assert docs[0] == ("a.txt", base64.b64encode(b"12345678").decode())
    assert isinstance(docs[1], eng.DocFile) and docs[1].name == "b.txt"


def test_a_docx_on_disk_is_streamed_and_stops_at_its_budget(tmp_path):
    import asyncio
    import io
    import zipfile

    from app.core.docx import extract_docx_text
    from app.engines import document as eng

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = "".join(f"<w:p><w:r><w:t>Paragraph {i}</w:t></w:r></w:p>" for i in range(5000))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", f"<w:document {w}><w:body>{paras}</w:body></w:document>")
    path = tmp_path / "long.docx"
    path.write_bytes(buf.getvalue())
    doc, err = asyncio.run(eng.extract_document_file("long.docx", str(path), max_chars=1_000))
    assert err is None and doc.full_text == extract_docx_text(buf.getvalue(), max_chars=1_000)
    assert doc.note.startswith("only its first 1,000 characters were read")
    whole, err = asyncio.run(eng.extract_document_file("long.docx", str(path)))
    assert whole.note == "" and whole.full_text == extract_docx_text(buf.getvalue())


def test_a_pdf_on_disk_is_opened_by_path_and_page_bounded(tmp_path, monkeypatch):
    import asyncio

    pdfium = pytest.importorskip("pypdfium2")
    from app.engines import document as eng

    pdf = pdfium.PdfDocument.new()
    for _ in range(5):
        pdf.new_page(200, 200)
    path = tmp_path / "scan.pdf"
    pdf.save(str(path))
    pdf.close()
    monkeypatch.setattr(eng, "DOC_MAX_PAGES", 2)
    monkeypatch.setattr(settings, "ocr_enabled", False)
    doc, err = asyncio.run(eng.extract_document_file("scan.pdf", str(path), effort="fast"))
    assert err is None and doc.total == 5
    # A scan has no text to reach the character budget with: the page
    # budget bounds it, and the note says how many pages were looked at.
    assert doc.note == "only the first 2 of its 5 pages were read (" + f"{os.path.getsize(path):,} bytes file)"


def test_many_documents_are_read_off_the_event_loop_and_rendered_once(monkeypatch):
    """QA 2026-10-03: a turn's documents were extracted ON the event loop
    (PDFium, the DOCX reader, the base64 round trips), so 50 PDFs stalled
    every other person's stream for 6 s at once and 999 for about two
    minutes, past the SSE heartbeat and Cloudflare's cut. Every such step now
    runs in a worker thread, from the resolver through the engine. And only
    the first PDF is rendered: the others' renders were ~80% of reading a
    PDF and were thrown away (page images come from the first PDF only)."""
    import asyncio
    import io
    import threading

    from docx import Document
    from weasyprint import HTML

    from app.core import docx as docx_module
    from app.engines import document as eng
    from app.main import _resolve_document_refs

    body = "".join(
        f"<h2>Section {p}</h2><p>{'Quarterly revenue grew in every region. ' * 20}</p>"
        "<p style='page-break-after: always'></p>"
        for p in range(3)
    )
    pdf = HTML(string=f"<html><body>{body}</body></html>").write_pdf()
    word = Document()
    word.add_paragraph("The board approved the budget.")
    buf = io.BytesIO()
    word.save(buf)
    ids = [_stored_document("conv-loop", pdf, f"report-{i:02d}.pdf") for i in range(12)]
    ids.append(_stored_document("conv-loop", buf.getvalue(), "minutes.docx"))
    refs = [{"upload_id": u, "name": "x"} for u in ids]

    calls: list = []

    def watched(fn, name):
        def run(*args, **kwargs):
            calls.append((name, threading.current_thread() is threading.main_thread()))
            return fn(*args, **kwargs)
        return run

    for name in ("extract_pdf_pages", "render_pdf", "render_pdf_pages"):
        monkeypatch.setattr(eng, name, watched(getattr(eng, name), name))
    for name in ("is_docx", "extract_docx_text"):
        monkeypatch.setattr(docx_module, name, watched(getattr(docx_module, name), name))
    for name in ("b64encode", "b64decode"):
        monkeypatch.setattr(base64, name, watched(getattr(base64, name), name))
    monkeypatch.setattr(settings, "ocr_enabled", False)
    seen = _capture_engine(monkeypatch)

    async def emit(kind, payload):
        pass

    async def turn():
        docs, _images, err = await _resolve_document_refs(_Req(pdf_uploads=refs), "conv-loop")
        assert err is None and len(docs) == 13
        return await eng.run_pdf_engine_multi("summarise", docs, [], emit, effort="think")

    assert asyncio.run(turn()).startswith("ok")
    on_the_loop = sorted({name for name, main in calls if main})
    assert on_the_loop == [], f"run on the event loop: {on_the_loop}"
    names = [name for name, _ in calls]
    assert names.count("extract_pdf_pages") == 12 and names.count("extract_docx_text") == 1
    assert names.count("render_pdf") == 1  # the first PDF only
    assert "13 documents were uploaded and ALL were read" in _prompt_text(seen)
    pictures = [p for p in seen["messages"][-1]["content"] if p.get("type") == "image_url"]
    assert len(pictures) == eng.LAYOUT_PAGES
