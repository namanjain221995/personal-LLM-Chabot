"""Documents at ChatGPT scale: 512 MB files, five per message, chunked over
the tunnel.

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
    assert not os.path.isdir(os.path.join(root, "_parts"))
    assert not os.path.isfile(os.path.join(root, up._MARKER))


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
    assert resp.status_code == 400
    assert "contiguous" in resp.json()["detail"]


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


def test_more_than_five_documents_is_refused():
    from app.main import _resolve_document_refs
    import asyncio

    refs = [{"upload_id": "a" * 32, "name": f"d{i}.pdf"} for i in range(6)]
    docs, _images, err = asyncio.run(_resolve_document_refs(_Req(pdf_uploads=refs), "c"))
    assert docs == [] and "at most 5" in err


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
