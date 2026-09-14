"""`apifiles/sniff.py`: the bytes decide the kind, never the name (design §4.1)."""
from __future__ import annotations

import io
import os
import struct
import zipfile

import pytest

from app.apifiles import sniff
from tests.apifiles_test_support import isolated_app_db  # noqa: F401 - fixture by name


def _zip(members, name="x.zip"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            archive.writestr(member, b"<x/>")
    return buffer.getvalue()


def _write(tmp_path, data, name="blob"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


BMP = b"BM" + struct.pack("<IHHI", 70, 0, 0, 54) + struct.pack("<I", 40) + b"\x00" * 50

CASES = [
    ("pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "report.bin", "pdf", "application/pdf"),
    ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "a.jpg", "image", "image/png"),
    ("jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 40, "a.png", "image", "image/jpeg"),
    ("gif", b"GIF89a" + b"\x00" * 40, "", "image", "image/gif"),
    ("webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 40, "", "image", "image/webp"),
    ("tiff", b"II*\x00" + b"\x00" * 40, "", "image", "image/tiff"),
    ("bmp", BMP, "", "image", "image/bmp"),
    ("mp4", b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41", "clip.mov", "video", "video/mp4"),
    ("m4a", b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00M4A isom", "voice.mp4", "audio", "audio/mp4"),
    ("mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ", "", "video", "video/quicktime"),
    ("heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", "photo.jpg", "unsupported", "image/heic"),
    ("webm", b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x84webm", "", "video", "video/webm"),
    ("avi", b"RIFF\x00\x00\x00\x00AVI LIST", "", "video", "video/x-msvideo"),
    ("wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", "a.mp4", "audio", "audio/wav"),
    ("ogg", b"OggS\x00\x02" + b"\x00" * 60, "", "audio", "audio/ogg"),
    ("flac", b"fLaC\x00\x00\x00\x22", "", "audio", "audio/flac"),
    ("mp3-id3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 20, "", "audio", "audio/mpeg"),
    ("mp3-frame", b"\xff\xfb\x90\x64" + b"\x00" * 40, "", "audio", "audio/mpeg"),
    ("mpeg-ts", (b"\x47" + b"\x00" * 187) * 3, "", "video", "video/mp2t"),
    ("playlist-named-mp4", b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10,\nsegment0.ts\n", "movie.mp4", "unsupported", "application/octet-stream"),
    ("concat-list", b"ffconcat version 1.0\nfile 'part1.mp3'\n", "a.mp3", "unsupported", "application/octet-stream"),
    ("binary", b"\x01\x02\x00\x03garbage", "notes.txt", "unsupported", "application/octet-stream"),
    ("html", b"  <!DOCTYPE html><html><body>hi</body></html>", "page.txt", "html", "text/html"),
    ("csv-by-shape", b"a,b,c\n1,2,3\n4,5,6\n7,8,9\n", "data", "tabular", "text/csv"),
    ("tsv-by-name", b"just one column\nstill one\n", "data.tsv", "tabular", "text/tab-separated-values"),
    ("jsonl", b'{"a": 1}\n{"a": 2}\n', "rows.jsonl", "tabular", "application/x-ndjson"),
    ("json-array-of-objects", b'[\n  {"a": 1},\n  {"a": 2}\n]', "rows.json", "tabular", "application/json"),
    ("json-object-is-text", b'{"openapi": "3.1.0", "paths": {}}', "spec.json", "text", "application/json"),
    ("markdown", b"# Title\n\nSome prose.\n", "README.md", "text", "text/markdown"),
    ("plain", b"hello world\n", "", "text", "text/plain"),
    ("utf16", "héllo wörld\n".encode("utf-16"), "", "text", "text/plain"),
    ("empty", b"", "", "text", "text/plain"),
]


@pytest.mark.parametrize("label,data,filename,kind,mime", CASES, ids=[c[0] for c in CASES])
def test_the_kind_comes_from_the_bytes(tmp_path, label, data, filename, kind, mime):
    found = sniff.detect_path(_write(tmp_path, data), filename=filename)
    assert (found.kind, found.mime_type) == (kind, mime)
    assert found.lane == ("media" if kind in ("audio", "video") else "cpu")
    if kind in ("audio", "video"):
        assert found.needs_probe, "ffprobe has the last word on audio vs video (design §6.4)"


@pytest.mark.parametrize(
    "members,filename,kind,ext",
    [
        (["[Content_Types].xml", "word/document.xml"], "x.bin", "document", "docx"),
        (["[Content_Types].xml", "ppt/presentation.xml"], "", "presentation", "pptx"),
        (["[Content_Types].xml", "xl/workbook.xml"], "", "spreadsheet", "xlsx"),
        (["[Content_Types].xml", "xl/workbook.xml", "xl/vbaProject.bin"], "book.xlsx", "unsupported", ""),
        (["[Content_Types].xml", "xl/workbook.xml"], "book.xlsm", "unsupported", ""),
        (["notes.txt", "more.txt"], "bundle.docx", "unsupported", ""),
    ],
)
def test_office_containers_are_told_apart_by_their_members(tmp_path, members, filename, kind, ext):
    found = sniff.detect_path(_write(tmp_path, _zip(members)), filename=filename)
    assert (found.kind, found.ext) == (kind, ext)


def test_a_zip_whose_central_directory_claims_more_than_the_bound_is_not_parsed(tmp_path):
    data = bytearray(_zip(["word/document.xml"]))
    eocd = data.rfind(b"PK\x05\x06")
    struct.pack_into("<I", data, eocd + 12, sniff.MAX_CENTRAL_DIRECTORY_BYTES + 1)
    found = sniff.detect_path(_write(tmp_path, bytes(data)))
    assert found.kind == "unsupported"


def test_a_zip_member_list_is_read_from_the_tail_of_a_large_file(tmp_path):
    """The central directory sits at the end; a 1 MiB stored member puts it
    far outside the 64 KiB head."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("xl/media/big.bin", os.urandom(1024 * 1024))
        archive.writestr("xl/workbook.xml", b"<workbook/>")
    assert sniff.detect_path(_write(tmp_path, buffer.getvalue())).kind == "spreadsheet"


def test_parquet_needs_its_magic_at_both_ends(tmp_path):
    body = b"PAR1" + os.urandom(100_000) + b"PAR1"
    assert sniff.detect_path(_write(tmp_path, body, "a")).kind == "tabular"
    assert sniff.detect_path(_write(tmp_path, b"PAR1" + b"\x00" * 100_000, "b")).kind != "tabular"


def test_a_window_that_cuts_a_multibyte_character_is_still_text(tmp_path):
    text = ("é" * (sniff.HEAD_BYTES // 2)).encode("utf-8") + b"x"  # the 64 KiB head ends mid-character
    assert len(text) > sniff.HEAD_BYTES
    found = sniff.detect_path(_write(tmp_path, b"a" + text))
    assert found.kind == "text"


def test_every_kind_has_a_fixed_stage_table():
    for kind in sniff.KINDS:
        stages = sniff.STAGES_BY_KIND[kind]
        assert stages[0] == "sniff" and stages[-1] == "finalize"
    assert len(sniff.STAGES_BY_KIND["video"]) == 11 and len(sniff.STAGES_BY_KIND["pdf"]) == 6


def test_the_source_link_is_a_hard_link_with_the_extension_and_is_idempotent(tmp_path):
    original = _write(tmp_path, b"a,b\n1,2\n", "original")
    derived = str(tmp_path / "derived")
    link = sniff.link_source(original, derived, "csv")
    assert link.endswith("src.csv")
    assert os.stat(link).st_ino == os.stat(original).st_ino
    assert sniff.link_source(original, derived, "csv") == link
    assert sniff.link_source(original, derived, "") is None
    assert sniff.link_source(original, derived, "../x") is None


# --------------------------------------------------- review fixes 2026-09-13 --


@pytest.mark.parametrize(
    "body, filename",
    [(b"[" * 60000, "a.txt"), (b'{"a":' * 30000, "a.txt"), (b'{"a":' * 30000, "a.jsonl"), (b"[" * 60000 + b"\n" + b"[" * 60000, "")],
)
def test_json_nested_past_the_recursion_limit_is_classified_not_raised(body, filename, tmp_path):
    found = sniff.detect(body, size=len(body), filename=filename)
    assert (found.kind, found.mime_type) == ("text", "text/plain"), "not JSON lines, so ordinary text"
    assert sniff.detect_path(_write(tmp_path, body), filename=filename).kind == "text"


def test_detect_never_raises_whatever_a_classifier_does(monkeypatch):
    def broken(*args, **kwargs):
        raise MemoryError("stands in for any failure inside classification")

    monkeypatch.setattr(sniff, "_text_kind", broken)
    found = sniff.detect(b"plain words", filename="a.txt")
    assert (found.kind, found.reason) == ("unknown", "sniff-error")


def _id3(payload_len, *, footer=False):
    size = payload_len
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00" + (b"\x10" if footer else b"\x00") + synchsafe + b"\x00" * payload_len + (b"3DI" + b"\x00" * 7 if footer else b"")


@pytest.mark.parametrize(
    "playlist",
    [b"ffconcat version 1.0\nfile victim.wav\n", b"#EXTM3U\n#EXTINF:1,\nvictim.wav\n", b"#EXT-X-VERSION:3\n"],
)
@pytest.mark.parametrize("tags", [[0], [32], [5, 7], [12]])
def test_a_playlist_behind_id3v2_tags_is_still_unsupported(playlist, tags, tmp_path):
    """Review LOW (2026-09-13): ffmpeg 7.0.2 skips ID3v2 tags before choosing a
    demuxer, so `ID3…ffconcat` opened the concat demuxer while this module
    called it `audio/mpeg`."""
    body = b"".join(_id3(n, footer=(i == 1)) for i, n in enumerate(tags)) + playlist
    assert sniff.detect(body, filename="song.mp3").reason == "playlist"
    assert sniff.detect_path(_write(tmp_path, body), filename="song.mp3").kind == "unsupported"


def test_a_playlist_behind_an_id3_tag_larger_than_the_sniff_window_is_unsupported(tmp_path):
    body = _id3(sniff.HEAD_BYTES * 2) + b"ffconcat version 1.0\nfile victim.wav\n"
    assert sniff.detect_path(_write(tmp_path, body), filename="song.mp3").kind == "unsupported"


def test_an_ordinary_id3_tagged_mp3_is_still_audio(tmp_path):
    body = _id3(sniff.HEAD_BYTES * 2) + b"\xff\xfb\x90\x00" + b"\x00" * 4096
    found = sniff.detect_path(_write(tmp_path, body), filename="song.mp3")
    assert (found.kind, found.mime_type) == ("audio", "audio/mpeg")
    assert sniff.detect(_id3(10) + b"\xff\xfb\x90\x00" + b"\x00" * 64).kind == "audio"
