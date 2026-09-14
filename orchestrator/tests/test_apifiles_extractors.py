"""The extraction child and the per-kind extractors, on real generated files.

Every test here runs the REAL subprocess (`extract_worker.run`), because the
properties that matter — the rlimits, the missing core dump, the kill on a
deadline or a cancellation, "the parent never reads the original" — only exist
across a process boundary. No database, no engines: the three autouse
database fixtures from conftest are replaced by no-ops for this module.

Fixtures are built here, deterministically, with no dependency the image lacks:
* a 1,000-page PDF written as raw PDF objects (text pages in Helvetica, pages
  500-509 as image-only JPEG pages) — PDFium reads it like any producer's;
* DOCX / PPTX / XLSX through python-docx, python-pptx and openpyxl;
* CSV, TSV, NDJSON, a JSON array, a JSON object and a Parquet file;
* a zip bomb dressed as a DOCX and as an XLSX, and a pixel bomb PNG whose
  header claims 30,000 × 30,000 pixels in a few hundred bytes.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import random
import struct
import sys
import time
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from app.apifiles import extract_worker, limits
from app.apifiles.extractors import ExtractError, FileCorrupt, FileTooComplex, Spec


# --- no database for this module ------------------------------------------
@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


def run(coro):
    return asyncio.run(coro)


# ================================================================ builders ==

WORDS = (
    "market revenue quarter growth customer supply logistics warehouse forecast budget margin "
    "pricing product region sales policy compliance audit vendor inventory shipment demand plant"
).split()

NEEDLE_137 = "The Ostrava facility 2025 water usage was 48,213 cubic metres."
NEEDLE_842 = "Contract TS-7741 renews on 3 March 2027 with a 4.5% uplift."


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


#: Marker blocks: hundreds, tens and units of the page number as grey levels
#: 20 apart, so JPEG's quantisation (a few levels) can never misread a digit.
MARKER_STEP = 20
MARKER_BLOCK = 48


def page_marker_jpeg(page: int, size: Tuple[int, int] = (400, 566)) -> bytes:
    """A scanned-looking page whose top-left blocks encode the page number —
    the stub OCR engine reads it back, which proves the right render reached
    the right page record."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index, digit in enumerate((page // 100, (page // 10) % 10, page % 10)):
        grey = digit * MARKER_STEP
        left = index * MARKER_BLOCK
        draw.rectangle([left, 0, left + MARKER_BLOCK - 1, MARKER_BLOCK - 1], fill=(grey, grey, grey))
    draw.text((20, 80), f"Scanned page {page}", fill="black")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def read_page_marker(image) -> int:
    """The page number `page_marker_jpeg` drew, from a render at any scale
    (sampled at the centre of each block, as a fraction of the width)."""
    rgb = image.convert("RGB")
    width, _height = rgb.size
    block = width * MARKER_BLOCK / 400.0
    digits = []
    for index in range(3):
        x = int(block * index + block / 2)
        y = int(block / 2)
        grey = rgb.getpixel((x, y))[0]
        digits.append(int(round(grey / MARKER_STEP)))
    return digits[0] * 100 + digits[1] * 10 + digits[2]


def build_pdf(path: str, pages: Sequence[Tuple[str, Any]]) -> None:
    """Raw PDF 1.4: ("text", [lines]), ("image", (jpeg_bytes, (w, h))) or
    ("mixed", ((jpeg_bytes, (w, h)), [lines])) — a scan with typed text over it."""
    objects: List[Optional[bytes]] = []

    def add(payload: Optional[bytes]) -> int:
        objects.append(payload)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_ref = add(None)
    kids: List[int] = []
    for kind, content in pages:
        image_part, lines = (content, None) if kind == "image" else ((None, content) if kind == "text" else content)
        ops: List[str] = []
        resources = b"<< "
        if image_part is not None:
            jpeg, (width, height) = image_part
            image = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (width, height, len(jpeg))
                + jpeg
                + b"\nendstream"
            )
            ops.append("q 595 0 0 842 0 0 cm /Im1 Do Q")
            resources += b"/XObject << /Im1 %d 0 R >> " % image
        if lines is not None:
            # Text starts below the scan's marker blocks (top-left 144 x 48 px).
            ops += ["BT /F1 9 Tf 11 TL 40 700 Td"] + [f"({_pdf_escape(line)}) Tj T*" for line in lines] + ["ET"]
            resources += b"/Font << /F1 %d 0 R >> " % font
        resources += b">>"
        stream = zlib.compress("\n".join(ops).encode("latin-1"))
        contents = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources %s >>"
                % (pages_ref, contents, resources)
            )
        )
    objects[pages_ref - 1] = (
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    )
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_ref)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + (payload or b"null") + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))
    with open(path, "wb") as fh:
        fh.write(out.getvalue())


def prose_lines(rng: random.Random, lines: int = 60, words: int = 5) -> List[str]:
    return [" ".join(rng.choice(WORDS) for _ in range(words)) for _ in range(lines)]


def thousand_page_pdf(path: str) -> None:
    """1,000 pages of seeded prose (~1,950 chars of text layer each), needles
    on 137 and 842, pages 500-509 image-only (the A-3 acceptance shape)."""
    rng = random.Random(7)
    pages: List[Tuple[str, Any]] = []
    for number in range(1, 1001):
        if 500 <= number <= 509:
            pages.append(("image", (page_marker_jpeg(number), (400, 566))))
            continue
        lines = prose_lines(rng)
        if number == 137:
            lines.insert(3, NEEDLE_137)
        if number == 842:
            lines.insert(10, NEEDLE_842)
        pages.append(("text", lines))
    build_pdf(path, pages)


def zip_bomb(path: str, members: Dict[str, bytes], bomb_member: str, bomb_bytes: int) -> None:
    """A valid OOXML container plus ONE member of `bomb_bytes` zeros, deflated
    (~1,000:1), written in 1 MiB chunks so the test never holds it."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
        with zf.open(bomb_member, "w", force_zip64=True) as member:
            chunk = b"\0" * (1024 * 1024)
            left = bomb_bytes
            while left > 0:
                member.write(chunk[: min(len(chunk), left)])
                left -= len(chunk)


def png_header_only(width: int, height: int) -> bytes:
    """A PNG whose IHDR claims width × height but whose IDAT is tiny."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


# ================================================================ helpers ==


def blob_layout(tmp_path, name: str, payload: bytes, ext: str) -> Tuple[str, str]:
    """<tmp>/<name>/original + derived/src.<ext> (a hard link, as sniff makes)."""
    blob = tmp_path / name
    derived = blob / "derived"
    derived.mkdir(parents=True)
    original = blob / "original"
    original.write_bytes(payload)
    os.link(original, derived / f"src.{ext}")
    return str(derived), str(derived / f"src.{ext}")


def caps_for(kind: str, original_bytes: int, **overrides: Any) -> Dict[str, Any]:
    kind_caps = limits.kind_caps(kind)
    caps = {
        "bytes": kind_caps.bytes,
        "pages": kind_caps.pages,
        "pixels": kind_caps.pixels,
        "cpu_s": 120,
        "rlimit_as_bytes": limits.extract_rlimit_as_bytes(),
        "fsize_bytes": extract_worker.fsize_limit(original_bytes),
        "thin_page_chars": 200,
    }
    caps.update(overrides)
    return caps


def extract(
    derived: str,
    source: str,
    kind: str,
    op: str = "extract",
    *,
    caps: Optional[Dict[str, Any]] = None,
    args: Optional[Dict[str, Any]] = None,
    wall_s: Optional[float] = 120,
    test_ops: bool = False,
) -> Dict[str, Any]:
    spec = Spec(
        op=op,
        kind=kind,
        derived_dir=derived,
        source=source,
        caps=caps or caps_for(kind, os.path.getsize(source) if source and os.path.exists(source) else 0),
        args=args or {},
    )
    env = {extract_worker.TEST_OPS_ENV: "1"} if test_ops else None
    return run(extract_worker.run(spec, wall_s=wall_s, env_extra=env))


def pages_of(derived: str) -> List[Dict[str, Any]]:
    with open(os.path.join(derived, "pages.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="module")
def big_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "big.pdf"
    thousand_page_pdf(str(path))
    return path.read_bytes()


# ===================================================================== PDF ==


def test_a_thousand_page_pdf_is_read_page_by_page_in_a_child_with_its_scanned_pages_listed_for_ocr(tmp_path, big_pdf):
    derived, source = blob_layout(tmp_path, "pdf", big_pdf, "pdf")
    started = time.monotonic()
    facts = extract(derived, source, "pdf")
    elapsed = time.monotonic() - started
    pages = pages_of(derived)
    assert facts["pages"] == 1000 and len(pages) == 1000
    assert [p["page"] for p in pages] == list(range(1, 1001))
    assert NEEDLE_137 in pages[136]["text"] and NEEDLE_842 in pages[841]["text"]
    assert NEEDLE_842 not in pages[840]["text"]
    with open(os.path.join(derived, "thin_pages.json")) as fh:
        assert json.load(fh) == list(range(500, 510))
    assert facts["text_pages"] == 990 and facts["thin_pages"] == 10
    assert facts["chars"] == sum(len(p["text"]) for p in pages)
    assert facts["estimated_tokens"] > facts["chars"] // 4
    # Measured on this box: printed so the WHY comments can cite the number.
    print(f"1,000-page PDF ({len(big_pdf):,} B) extracted in a child in {elapsed:.2f}s")
    assert elapsed < 60


def test_a_pdf_over_the_page_ceiling_is_too_complex_and_says_which_ceiling(tmp_path, big_pdf):
    derived, source = blob_layout(tmp_path, "pdf", big_pdf, "pdf")
    with pytest.raises(FileTooComplex) as caught:
        extract(derived, source, "pdf", caps=caps_for("pdf", len(big_pdf), pages=999))
    assert caught.value.sentence == "The file exceeds a processing ceiling: more than 999 pages."
    assert not os.path.exists(os.path.join(derived, "pages.jsonl"))


def test_a_damaged_pdf_is_corrupt_and_the_parser_text_is_never_echoed(tmp_path):
    derived, source = blob_layout(tmp_path, "bad", b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\ntrailer garbage", "pdf")
    with pytest.raises(FileCorrupt) as caught:
        extract(derived, source, "pdf")
    assert caught.value.sentence == "The file could not be read; it may be damaged or encrypted."
    assert caught.value.ceiling == ""


def test_the_render_op_writes_one_png_per_requested_page_and_skips_pages_that_do_not_exist(tmp_path, big_pdf):
    derived, source = blob_layout(tmp_path, "render", big_pdf, "pdf")
    facts = extract(derived, source, "pdf", op="render", args={"pages": [500, 505, 1001], "scale": 1.0})
    assert facts == {"rendered": [500, 505], "skipped": [1001]}
    from PIL import Image

    with Image.open(os.path.join(derived, "renders", "505.png")) as image:
        assert read_page_marker(image) == 505
    with Image.open(os.path.join(derived, "renders", "500.png")) as image:
        assert read_page_marker(image) == 500


def test_a_pdf_over_its_byte_ceiling_is_too_complex_before_pdfium_opens_it_in_extract_and_render(tmp_path, big_pdf):
    # Review finding, 2026-09-13: PUBLIC_API_FILES_PDF_MAX_BYTES was never enforced.
    derived, source = blob_layout(tmp_path, "pdfbytes", big_pdf, "pdf")
    limit = 512 * 1024  # below the 978 KB fixture
    for op, args in (("extract", {}), ("render", {"pages": [1]})):
        with pytest.raises(FileTooComplex) as caught:
            extract(derived, source, "pdf", op=op, args=args, caps=caps_for("pdf", len(big_pdf), bytes=limit))
        # Below 1 MiB the ceiling reads in KiB, never "larger than 0 MiB".
        assert caught.value.sentence == "The file exceeds a processing ceiling: larger than 512 KiB."
    assert not os.path.exists(os.path.join(derived, "pages.jsonl"))
    assert not os.listdir(os.path.join(derived, "renders"))


def test_render_pages_without_caps_or_a_deadline_still_runs_its_child_under_a_cpu_ceiling_and_a_wall(tmp_path, big_pdf, monkeypatch):
    from app.apifiles import render

    derived, source = blob_layout(tmp_path, "rdefaults", big_pdf, "pdf")
    seen: List[Tuple[Spec, Optional[float]]] = []
    real_run = extract_worker.run

    async def spy(spec, *, wall_s=None, env_extra=None):
        seen.append((spec, wall_s))
        return await real_run(spec, wall_s=wall_s, env_extra=env_extra)

    monkeypatch.setattr(extract_worker, "run", spy)
    out_dir = tmp_path / "scratch-renders"  # a caller's scratch dir outside derived/
    rendered = run(render.render_pages(derived, source, [3, 4], out_dir=str(out_dir), scale=1.0))
    assert sorted(rendered) == [3, 4] and all(os.path.dirname(p) == str(out_dir) for p in rendered.values())
    spec, wall_s = seen[0]
    assert wall_s == render.RENDER_WALL_FLOOR_S
    assert spec.caps["cpu_s"] == render.RENDER_WALL_FLOOR_S
    assert spec.caps["bytes"] == limits.kind_caps("pdf").bytes and spec.caps["rlimit_as_bytes"] > 0
    # A caller's own ceilings still win over the defaults.
    run(render.render_pages(derived, source, [5], out_dir=str(out_dir), caps={"cpu_s": 7}, wall_s=9.0, scale=1.0))
    assert seen[1][0].caps["cpu_s"] == 7 and seen[1][1] == 9.0
    assert render.default_wall_s(40) == 600.0


# ================================================================== office ==


def make_docx(path: str) -> None:
    from docx import Document

    document = Document()
    document.add_heading("Quarterly review", level=1)
    rng = random.Random(3)
    for number in range(40):
        document.add_paragraph(f"Paragraph {number}: " + " ".join(rng.choice(WORDS) for _ in range(40)))
    table = document.add_table(rows=2, cols=3)
    for r, row in enumerate([("Region", "Revenue", "Margin"), ("North", "4,210", "12%")]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    document.save(path)


def test_a_docx_is_cut_into_sections_at_paragraph_boundaries_with_tables_tab_joined(tmp_path):
    source_path = tmp_path / "in.docx"
    make_docx(str(source_path))
    derived, source = blob_layout(tmp_path, "docx", source_path.read_bytes(), "docx")
    facts = extract(derived, source, "document")
    pages = pages_of(derived)
    assert facts["unit"] == "section" and facts["sections"] == len(pages) >= 2
    assert all(len(p["text"]) <= 3000 for p in pages)
    joined = "\n".join(p["text"] for p in pages)
    assert "Region\tRevenue\tMargin" in joined and "North\t4,210\t12%" in joined
    # A cut never lands inside a paragraph: every section starts at one.
    assert all(p["text"].startswith(("Quarterly review", "Paragraph ", "Region")) for p in pages)


def make_pptx(path: str) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for number in range(1, 4):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = f"Slide title {number}"
        slide.placeholders[1].text = f"Body text for slide {number}"
        slide.notes_slide.notes_text_frame.text = f"Speaker note {number}"
    table = deck.slides[2].shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "Quarter", "Units"
    table.cell(1, 0).text, table.cell(1, 1).text = "Q3", "9,082"
    deck.save(path)


def test_a_pptx_gives_one_page_per_slide_with_its_table_and_speaker_notes(tmp_path):
    source_path = tmp_path / "in.pptx"
    make_pptx(str(source_path))
    derived, source = blob_layout(tmp_path, "pptx", source_path.read_bytes(), "pptx")
    facts = extract(derived, source, "presentation")
    pages = pages_of(derived)
    assert facts["slides"] == 3 and [p["page"] for p in pages] == [1, 2, 3]
    assert pages[0]["text"].startswith("Slide title 1") and "Notes: Speaker note 1" in pages[0]["text"]
    assert "Quarter\tUnits" in pages[2]["text"] and "Q3\t9,082" in pages[2]["text"]


# ============================================================ spreadsheets ==


def make_xlsx(path: str) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sales = workbook.active
    sales.title = "Sales"
    sales.append(["region", "units", "secret_note"])
    for row in range(450):
        sales.append([f"R{row % 7}", row, "zz-internal-" + str(row)])
    costs = workbook.create_sheet("Costs")
    costs.append(["item", "cost"])
    for row in range(10):
        costs.append([f"item {row}", row * 1.5])
    workbook.save(path)


def test_an_xlsx_streams_one_csv_per_sheet_and_200_row_blocks_with_the_header_repeated(tmp_path):
    source_path = tmp_path / "in.xlsx"
    make_xlsx(str(source_path))
    derived, source = blob_layout(tmp_path, "xlsx", source_path.read_bytes(), "xlsx")
    facts = extract(derived, source, "spreadsheet", op="sheets")
    assert facts["sheets"] == 2 and facts["rows"] == 460 and facts["unit"] == "rows"
    with open(os.path.join(derived, "sheet-1.csv"), newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["region", "units", "secret_note"] and len(rows) == 451
    pages = pages_of(derived)
    assert [(p["sheet"], p["rows"]) for p in pages] == [
        ("Sales", [1, 200]), ("Sales", [201, 400]), ("Sales", [401, 450]), ("Costs", [1, 10])
    ]
    assert all(p["text"].split("\n", 1)[0] == "region\tunits\tsecret_note" for p in pages[:3])
    profile_facts = extract(derived, source, "spreadsheet", op="profile")
    assert profile_facts == {"sheets": 2}
    with open(os.path.join(derived, "profile.json")) as fh:
        profile = json.load(fh)
    assert profile["file"] == "spreadsheet.xlsx" and profile["sheets"][0]["rows"] == 450


def test_a_csv_keeps_its_original_cells_in_row_blocks_and_gets_the_chat_apps_profile(tmp_path):
    body = "name,amount,when\n" + "".join(f"item {i},{i}.50,2026-09-{(i % 28) + 1:02d}\n" for i in range(250))
    derived, source = blob_layout(tmp_path, "csv", body.encode(), "csv")
    facts = extract(derived, source, "tabular", op="sheets")
    pages = pages_of(derived)
    assert facts["rows"] == 250 and facts["columns"] == 3
    assert [p["rows"] for p in pages] == [[1, 200], [201, 250]]
    assert pages[1]["text"].split("\n")[:2] == ["name\tamount\twhen", "item 200\t200.50\t2026-09-05"]
    assert extract(derived, source, "tabular", op="profile") == {"rows": 250, "columns": 3}

    quoted = 'id\tnote\n1\t"first line\nsecond line"\n2\tplain\n'
    derived_t, source_t = blob_layout(tmp_path, "tsv", quoted.encode(), "tsv")
    assert extract(derived_t, source_t, "tabular", op="sheets")["rows"] == 2
    assert pages_of(derived_t)[0]["text"] == "id\tnote\n1\tfirst line second line\n2\tplain"


def test_a_json_array_of_objects_is_read_through_duckdb_and_a_parquet_is_profile_only(tmp_path):
    rows = [{"city": f"c{i}", "population": i * 10} for i in range(5)]
    derived, source = blob_layout(tmp_path, "json", json.dumps(rows).encode(), "json")
    facts = extract(derived, source, "tabular", op="sheets")
    assert facts["rows"] == 5 and pages_of(derived)[0]["text"].split("\n")[1] == "c0\t0"

    import duckdb

    parquet = tmp_path / "t.parquet"
    duckdb.sql(f"COPY (SELECT range AS n, 'v' || range AS label FROM range(1000)) TO '{parquet}' (FORMAT PARQUET)")
    derived_p, source_p = blob_layout(tmp_path, "parquet", parquet.read_bytes(), "parquet")
    assert extract(derived_p, source_p, "tabular", op="sheets")["rows"] is None
    assert pages_of(derived_p) == []
    assert extract(derived_p, source_p, "tabular", op="profile") == {"rows": 1000, "columns": 2}


# ============================================================= text / html ==


def test_a_json_object_is_text_pretty_printed_and_a_utf16_file_is_decoded(tmp_path):
    spec = {"openapi": "3.1.0", "paths": {f"/v1/thing{i}": {"get": {"summary": "x" * 50}} for i in range(80)}}
    derived, source = blob_layout(tmp_path, "spec", json.dumps(spec, separators=(",", ":")).encode(), "json")
    facts = extract(derived, source, "text")
    pages = pages_of(derived)
    assert facts["sections"] == len(pages) > 1
    assert pages[0]["text"].startswith('{\n  "openapi": "3.1.0"')

    text = "Ünïcödé line one\nline two\n" * 10
    derived16, source16 = blob_layout(tmp_path, "u16", text.encode("utf-16"), "txt")
    extract(derived16, source16, "text")
    assert pages_of(derived16)[0]["text"].startswith("Ünïcödé line one\nline two")


def test_a_32_mib_file_on_one_line_is_sectioned_in_linear_time_and_never_held_as_one_line(tmp_path):
    # Review finding, 2026-09-13: one-line files measured 15.02 s to section at
    # 32 MiB (quadratic, hours at 1 GiB); the linear walk measured under 0.05 s.
    from app.apifiles.extractors import split_sections
    from app.apifiles.extractors.text import LINE_WINDOW_CHARS, iter_lines

    word = "lorem ipsum dolor sit amet "
    block = word * ((32 << 20) // len(word))
    started = time.monotonic()
    sections = list(split_sections(iter([block])))
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, elapsed
    assert all(0 < len(x) <= 3000 for x in sections)
    assert "".join(sections).replace(" ", "") == block.replace(" ", "")

    path = tmp_path / "oneline.txt"
    path.write_text(block, encoding="utf-8")
    pieces = list(iter_lines(str(path)))
    assert max(len(p) for p in pieces) <= LINE_WINDOW_CHARS
    assert "".join(pieces) == block  # windows are cut before a space, nothing lost


def test_html_goes_through_the_chat_apps_readable_extraction_and_scripts_never_reach_the_text(tmp_path):
    html = (
        "<!doctype html><html><head><title>T</title><script>var secret='zz-script';</script></head>"
        "<body><nav>menu menu</nav><article><h1>Water report</h1>"
        + "".join(f"<p>The facility used {i},000 cubic metres of water in month {i}.</p>" for i in range(1, 30))
        + "</article></body></html>"
    )
    derived, source = blob_layout(tmp_path, "html", html.encode(), "html")
    facts = extract(derived, source, "html")
    text = "\n".join(p["text"] for p in pages_of(derived))
    assert "used 12,000 cubic metres" in text and "zz-script" not in text
    assert facts["unit"] == "section"


# =================================================================== image ==


def test_an_image_gets_variants_no_larger_than_the_source_with_exif_applied_and_stripped(tmp_path):
    from PIL import Image

    image = Image.new("RGB", (2000, 1000), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW to display
    exif[0x010F] = "zz-phone-maker"  # Make: metadata that must not survive
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif.tobytes())
    derived, source = blob_layout(tmp_path, "img", buf.getvalue(), "jpg")
    decoded = extract(derived, source, "image", op="decode")
    assert decoded["width"] == 1000 and decoded["height"] == 2000 and decoded["format"] == "JPEG"
    facts = extract(derived, source, "image", op="variants")
    assert facts["variants"] == ["image_896.jpg", "image_1600.jpg", "image.png"]
    with Image.open(os.path.join(derived, "image_1600.jpg")) as variant:
        assert variant.size == (800, 1600)
        assert not variant.getexif()
    with Image.open(os.path.join(derived, "image.png")) as normalised:
        assert normalised.size == (1000, 2000) and "exif" not in normalised.info
    for name in facts["variants"]:
        with open(os.path.join(derived, name), "rb") as fh:
            assert b"zz-phone-maker" not in fh.read()


# ============================================================= the bombs ==


def test_a_zip_bomb_docx_and_xlsx_are_refused_within_the_caps_before_any_reader_opens_them(tmp_path):
    bomb = tmp_path / "bomb.docx"
    zip_bomb(str(bomb), {"[Content_Types].xml": b"<Types/>"}, "word/document.xml", 256 * 1024 * 1024)
    size = bomb.stat().st_size
    derived, source = blob_layout(tmp_path, "docx-bomb", bomb.read_bytes(), "docx")
    started = time.monotonic()
    with pytest.raises(FileTooComplex) as caught:
        extract(derived, source, "document")
    assert time.monotonic() - started < 10
    assert caught.value.sentence == "The file exceeds a processing ceiling: it expands beyond the decompression ceiling."
    assert size < 2 * 1024 * 1024  # a quarter-gibibyte of zeros in well under 2 MiB

    xbomb = tmp_path / "bomb.xlsx"
    zip_bomb(str(xbomb), {"xl/workbook.xml": b"<workbook/>"}, "xl/worksheets/sheet1.xml", 256 * 1024 * 1024)
    derived_x, source_x = blob_layout(tmp_path, "xlsx-bomb", xbomb.read_bytes(), "xlsx")
    for op in ("sheets", "profile"):
        with pytest.raises(FileTooComplex):
            extract(derived_x, source_x, "spreadsheet", op=op)
    assert not os.path.exists(os.path.join(derived_x, "sheet-1.csv"))


def test_a_pixel_bomb_is_too_complex_from_its_header_alone(tmp_path):
    derived, source = blob_layout(tmp_path, "pixels", png_header_only(30_000, 30_000), "png")
    with pytest.raises(FileTooComplex) as caught:
        extract(derived, source, "image", op="decode")
    assert "pixels" in caught.value.ceiling
    # 10,000 × 10,000 is between 1× and 2× Pillow's limit, where Pillow only
    # WARNS and decodes anyway; the extractor turns that warning into a refusal.
    derived2, source2 = blob_layout(tmp_path, "pixels2", png_header_only(10_000, 10_000), "png")
    with pytest.raises(FileTooComplex):
        extract(derived2, source2, "image", op="variants")


def test_a_decode_that_outgrows_the_address_space_ceiling_ends_too_complex_and_the_parent_lives(tmp_path):
    from PIL import Image

    image = Image.new("RGB", (12_000, 12_000), "white")  # 144 M px, 432 MiB decoded
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=1)
    del image
    derived, source = blob_layout(tmp_path, "mem", buf.getvalue(), "png")
    caps = caps_for("image", len(buf.getvalue()), pixels=500_000_000, rlimit_as_bytes=256 * 1024 * 1024)
    with pytest.raises(FileTooComplex) as caught:
        extract(derived, source, "image", op="variants", caps=caps)
    assert caught.value.ceiling in (
        "it needs more memory than the processing ceiling",
        "it needs more time or memory than the processing ceiling",
    )
    # The control: the same 256 MiB ceiling decodes a normal photo, so the
    # refusal above is the bomb's size and not the ceiling being unworkable.
    small = io.BytesIO()
    Image.new("RGB", (1600, 1200), "white").save(small, format="PNG")
    derived_ok, source_ok = blob_layout(tmp_path, "mem-ok", small.getvalue(), "png")
    facts = extract(derived_ok, source_ok, "image", op="variants", caps={**caps, "pixels": 89_478_485})
    assert facts["width"] == 1600


# ======================================================= the child itself ==


def test_the_child_is_non_dumpable_with_core_limit_one_and_runs_under_every_ceiling_with_a_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://leak:leak@db/leak")
    monkeypatch.setenv("PUBLIC_API_WEBHOOK_SECRET_TEST", "zz-secret")
    derived = tmp_path / "d"
    derived.mkdir()
    facts = extract(
        str(derived), "", "", op="test-limits", caps={"cpu_s": 77, "rlimit_as_bytes": 3 * 1024 ** 3, "fsize_bytes": 5 * 1024 ** 2},
        test_ops=True,
    )
    assert facts["dumpable"] == 0
    assert facts["core"] == [1, 1]
    assert facts["as"] == [3 * 1024 ** 3] * 2
    assert facts["cpu"] == [77, 82]
    assert facts["nofile"] == [256, 256]
    assert facts["fsize"] == [5 * 1024 ** 2] * 2
    assert "APP_DATABASE_URL" not in facts["env_keys"]
    assert all(not k.startswith("PUBLIC_API_WEBHOOK") for k in facts["env_keys"])
    assert facts["cwd"] == extract_worker.app_root()


def test_the_child_cannot_read_the_parents_environment_other_tenants_files_or_open_any_socket(tmp_path, monkeypatch):
    # Review finding, 2026-09-13: a scrubbed environment alone let a child read
    # /proc/<ppid>/environ, the whole files tree and reach the network.
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://leak:SECRET-PASSWORD@db/leak")
    derived = tmp_path / "tenant-a" / "derived"
    derived.mkdir(parents=True)
    other = tmp_path / "tenant-b"
    other.mkdir()
    (other / "original").write_bytes(b"another tenant's bytes")
    facts = extract(
        str(derived), "", "", op="test-escape", caps={},
        args={"outside_file": str(other / "original"), "outside_dir": str(other)}, test_ops=True,
    )
    assert facts == {
        "parent_environ": "EACCES",
        "read_outside": "EACCES",
        "list_outside": "EACCES",
        "write_outside": "EACCES",
        "symlink_in_derived": "EACCES",  # a planted link would be followed by a download
        "hardlink_into_derived": "EXDEV",  # Landlock's refusal of a cross-directory link
        "fifo_in_derived": "EACCES",
        "write_derived": "ok",
        "write_tmp": "ok",
        "inet_socket": "EPERM",
        "unix_socket": "EPERM",
        "signal_parent": "EPERM",
        "read_own_status": "ok",
    }
    assert not os.path.exists(other / "planted")
    limits_facts = extract(str(derived), "", "", op="test-limits", caps={}, test_ops=True)
    status = limits_facts["status"]
    assert status["CapEff"] == status["CapPrm"] == status["CapAmb"] == "0000000000000000"
    assert status["NoNewPrivs"] == "1" and status["Seccomp"] == "2"
    assert limits_facts["landlock_abi"] >= 6 and limits_facts["seccomp"] is True
    assert limits_facts["sandbox_mode"] == "required"


def test_a_child_that_cannot_be_sandboxed_refuses_to_parse_and_the_job_defers_unless_best_effort(tmp_path, monkeypatch):
    missing = str(tmp_path / "no-such-derived")  # a writable dir the ruleset cannot name
    with pytest.raises(extract_worker.RetryableWorkerError) as caught:
        extract(missing, "", "", op="test-limits", caps={}, test_ops=True)
    assert "sandbox" in str(caught.value)
    monkeypatch.setenv("PUBLIC_API_FILES_EXTRACT_SANDBOX", "best_effort")
    facts = extract(missing, "", "", op="test-limits", caps={}, test_ops=True)
    assert facts["sandbox_mode"] == "best_effort" and facts["landlock_abi"] == 0
    assert facts["status"]["NoNewPrivs"] == "1"  # the layers that do apply still do


def test_a_child_killed_from_outside_is_a_deferral_not_a_verdict_on_the_file(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    with pytest.raises(extract_worker.RetryableWorkerError):
        extract(str(derived), "", "", op="test-sigkill", caps={}, test_ops=True)


def test_test_ops_are_refused_unless_the_parent_enabled_them_in_the_childs_environment(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    with pytest.raises(ExtractError) as caught:
        extract(str(derived), "", "", op="test-limits", caps={}, test_ops=False)
    assert caught.value.code == "internal_error"


def test_a_segfaulting_child_is_file_corrupt_and_leaves_no_core_dump_anywhere(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    crash_dir = "/var/crash"
    before = _crash_listing(crash_dir)
    with pytest.raises(FileCorrupt):
        extract(str(derived), "", "", op="test-segfault", caps={}, test_ops=True)
    time.sleep(1.0)  # apport, were it invoked, writes asynchronously
    assert _crash_listing(crash_dir) == before
    for root in (str(tmp_path), extract_worker.app_root()):
        assert not [n for n in os.listdir(root) if n == "core" or n.startswith("core.")]


def _crash_listing(path: str) -> List[Tuple[str, int, float]]:
    try:
        return sorted((e.name, e.stat().st_size, e.stat().st_mtime) for e in os.scandir(path))
    except OSError:
        return []


def test_a_child_past_its_wall_deadline_is_killed_and_the_file_is_too_complex(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    started = time.monotonic()
    with pytest.raises(FileTooComplex) as caught:
        extract(str(derived), "", "", op="test-sleep", caps={}, args={"seconds": 60}, wall_s=1.0, test_ops=True)
    assert time.monotonic() - started < 10
    assert caught.value.ceiling == "it needs more time than the processing ceiling"


def test_cancelling_the_job_kills_the_child_before_the_cancellation_returns(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    spawned: List[int] = []
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*argv, **kwargs):
        proc = await real_exec(*argv, **kwargs)
        spawned.append(proc.pid)
        return proc

    async def scenario():
        asyncio.create_subprocess_exec = recording_exec  # type: ignore[assignment]
        try:
            spec = Spec(op="test-sleep", kind="", derived_dir=str(derived), args={"seconds": 60})
            task = asyncio.ensure_future(extract_worker.run(spec, env_extra={extract_worker.TEST_OPS_ENV: "1"}))
            while not spawned:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]

    run(scenario())
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_an_output_past_the_file_size_ceiling_is_too_complex_not_a_crash(tmp_path):
    derived = tmp_path / "d"
    derived.mkdir()
    with pytest.raises(FileTooComplex) as caught:
        extract(str(derived), "", "", op="test-write", caps={"fsize_bytes": 1024 * 1024}, args={"bytes": 4 * 1024 * 1024}, test_ops=True)
    assert "extracted data is larger" in caught.value.ceiling


def test_the_parent_never_opens_the_original_or_its_source_link_while_extracting(tmp_path):
    source_path = tmp_path / "in.docx"
    make_docx(str(source_path))
    derived, source = blob_layout(tmp_path, "audit", source_path.read_bytes(), "docx")
    watched = {os.path.realpath(source), os.path.realpath(os.path.join(os.path.dirname(derived), "original"))}
    opened: List[str] = []
    state = {"on": False}

    def hook(event: str, args: tuple) -> None:
        if state["on"] and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            try:
                path = os.path.realpath(os.fsdecode(args[0]))
            except (TypeError, ValueError):
                return
            if path in watched:
                opened.append(path)

    sys.addaudithook(hook)
    state["on"] = True
    try:
        extract(derived, source, "document")
    finally:
        state["on"] = False
    assert opened == []
    assert pages_of(derived)  # the child did read it
