"""render_version end to end, the worker's job.json round trip, honest
capabilities, safe error messages — and the timings the render timeout
rests on (printed with -s; asserted loosely so a slow CI box passes).

Everything here produces a preview.pdf, so it needs WeasyPrint and skips
without it (see requirements-dev.txt).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.artifacts import spec as S
from app.artifacts import types as T
from app.artifacts.render import RenderError, RenderReport, capabilities, render_version
from tests.test_artifact_render_samples import deck, document, workbook

pytestmark = pytest.mark.skipif(not capabilities().get("pdf"), reason="WeasyPrint is not installed (excluded from requirements-dev)")

TIMINGS: dict = {}


def _timed(label, fn):
    t = time.perf_counter()
    out = fn()
    TIMINGS[label] = round(time.perf_counter() - t, 2)
    return out


def test_capabilities_are_truthful(monkeypatch):
    caps = capabilities()
    # CONTRACT-2 §1: csv is a format; its writer is the standard library,
    # so the capability is always true (it was the four Office formats).
    assert set(caps) == {"pdf", "docx", "pptx", "xlsx", "csv"}
    assert all(caps.values())
    # Block python-pptx the way Python itself blocks an import (None in
    # sys.modules): the capability turns false, the others stay true.
    monkeypatch.setitem(sys.modules, "pptx", None)
    assert capabilities()["pptx"] is False and capabilities()["xlsx"] is True
    # And a missing pypdfium2 takes the PDF capability with it.
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    assert capabilities()["pdf"] is False


def test_health_reports_the_csv_renderer(tmp_path, monkeypatch):
    """/health's artifacts block passes capabilities() through, so the CSV
    writer shows up beside the four Office renderers (wave 2c, brief 8)."""
    from app import health
    from app.config import settings

    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    monkeypatch.setattr(settings, "artifacts_enabled", True)
    block = health._check_artifacts()
    assert block["renderers"]["csv"] is True and set(block["renderers"]) == {"pdf", "docx", "pptx", "xlsx", "csv"}
    assert block["status"] == "ok" and block["volume_writable"] is True


def test_capabilities_do_not_import_the_office_writers():
    """/health calls capabilities() on every probe from the API process; the
    subprocess design keeps the writers out of that process, so the probe
    must not pull them in. WeasyPrint is the one exception, imported once."""
    script = (
        "import sys, time\n"
        "from app.artifacts.render import capabilities\n"
        "caps = capabilities()\n"
        "loaded = [m for m in ('docx', 'pptx', 'openpyxl', 'pypdfium2', 'matplotlib.pyplot') if m in sys.modules]\n"
        "if loaded: raise SystemExit('imported: ' + ', '.join(loaded))\n"
        "t = time.perf_counter(); [capabilities() for _ in range(50)]; warm = (time.perf_counter() - t) / 50\n"
        "if warm > 0.01: raise SystemExit(f'warm probe {warm * 1000:.1f} ms')\n"
        "print(caps)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False,
                            cwd=str(Path(__file__).resolve().parents[1]))
    assert result.returncode == 0, result.stderr[-800:] or result.stdout


def test_title_slug_must_be_a_filename_stem(tmp_path):
    for bad in ("../escape", "a/b", "Upper", "", "x" * 61, "sp ace", "dot.pdf"):
        with pytest.raises(RenderError) as exc:
            render_version(workbook(), ["xlsx"], str(tmp_path), title_slug=bad, version=1)
        assert exc.value.category == "invalid_request" and "file name" in exc.value.message, bad
    assert not list(tmp_path.parent.glob("escape-v1.xlsx"))
    assert render_version(workbook(), ["xlsx"], str(tmp_path), title_slug=T.slug_for("Sales Tracker (Q2)!"), version=1).files[0].filename == "sales-tracker-q2-v1.xlsx"


def test_document_renders_both_formats_and_a_preview(tmp_path):
    spec = document("executive_report", sections=10)
    report = _timed("document-10-sections pdf+docx", lambda: render_version(spec, ["pdf", "docx"], str(tmp_path), title_slug="exec-report", version=3, effort="think"))
    assert isinstance(report, RenderReport)
    assert [f.format for f in report.files] == ["pdf", "docx"]
    assert [f.filename for f in report.files] == ["exec-report-v3.pdf", "exec-report-v3.docx"]
    # CONTRACT-2 §2: the role follows the kind's native format.
    assert [f.role for f in report.files] == ["companion", "primary"]
    assert list(report.paths) == ["companion:pdf:", "primary:docx:"]
    pdf = report.files[0]
    assert pdf.pages and 3 <= pdf.pages <= T.MAX_PAGES and len(pdf.sha256) == 64 and pdf.size > 1000
    assert pdf.mime_type == "application/pdf"
    assert report.preview_kind == "pages" and report.preview_pages == pdf.pages
    assert Path(report.preview_pdf).name == T.PREVIEW_PDF_NAME
    assert (tmp_path / "preview.pdf").read_bytes() == (tmp_path / "exec-report-v3.pdf").read_bytes()
    assert report.chart_files == ["chart-1.png", "chart-2.png"]
    assert all((tmp_path / c).is_file() for c in report.chart_files)
    assert report.validation["files"]["companion:pdf:"]["pages"] == pdf.pages
    assert report.validation["effort"] == "think"
    assert report.warnings == []
    # The wire form the worker writes: FileRef-like dicts with the role and
    # no file id (the pipeline mints those, never the worker).
    wire = report.to_json()["files"]
    assert wire[1]["role"] == "primary" and "file_id" not in wire[1] and "sheet" not in wire[1]
    # Per-format timings for artifact_render_seconds{format}: one key per
    # format written, plus the charts and the preview copy.
    assert set(report.timings) == {"charts", "pdf", "docx", "preview"}
    assert all(0 <= v < 60 for v in report.timings.values()) and report.timings["pdf"] > 0
    assert report.to_json()["timings"]["docx"] == round(report.timings["docx"], 4)


def test_docx_only_still_gets_a_preview_from_the_same_spec(tmp_path):
    report = render_version(document("brief", sections=1), ["docx"], str(tmp_path), title_slug="brief", version=1)
    assert [f.format for f in report.files] == ["docx"]
    assert report.preview_pages >= 1 and (tmp_path / "preview.pdf").is_file()
    assert not (tmp_path / "brief-v1.pdf").exists()


def test_brief_over_two_pages_carries_a_warning(tmp_path):
    report = render_version(document("brief", sections=10), ["pdf"], str(tmp_path), title_slug="brief", version=1)
    assert any("meant to fit on 2 pages" in w for w in report.warnings)


def test_presentation_preview_pages_equal_slides(tmp_path):
    spec = deck("ceo")
    report = _timed("deck-12-slides pptx+pdf", lambda: render_version(spec, ["pptx", "pdf"], str(tmp_path), title_slug="deck", version=1))
    by_format = {f.format: f for f in report.files}
    assert by_format["pptx"].slides == 13 and by_format["pdf"].pages == 13
    assert report.preview_pages == 13 and report.preview_kind == "pages"
    assert report.chart_files == ["chart-1.png", "chart-2.png", "chart-3.png"]
    # The plan's bullet trims surface as warnings.
    assert any("dropped" in w for w in report.warnings)


def test_workbook_gets_a_grid_preview_and_a_summary_pdf(tmp_path):
    report = _timed("workbook-3-sheets xlsx", lambda: render_version(workbook("dashboard"), ["xlsx"], str(tmp_path), title_slug="tracker", version=2))
    assert [f.format for f in report.files] == ["xlsx"]
    assert report.files[0].sheets == 5 and report.files[0].filename == "tracker-v2.xlsx"
    assert report.files[0].role == "primary" and report.files[0].rows == 45 and report.files[0].columns == 6
    assert report.preview_kind == "grid" and report.preview_pages >= 1
    assert report.chart_files == []
    assert set(report.timings) == {"charts", "xlsx", "preview"} and report.timings["charts"] < report.timings["preview"]


def test_workbook_in_four_formats_is_four_kinds_of_file(tmp_path):
    """CONTRACT-2 §1-§3: "XLSX, Word, PDF and CSV of this audit" from one
    spec — the primary xlsx, one CSV per sheet (role data, part names for
    a multi-sheet workbook, each titled by its SHEET — CONTRACT-2 §3/§11:
    the pipeline builds the wire title "<title> — <sheet>" from that, and
    would collapse a title that already starts with the artifact's), and the Word
    and PDF companions carrying every row; the preview.pdf IS the PDF
    companion; preview_kind stays grid."""
    spec = workbook(rows=30)
    report = _timed("workbook-3-sheets xlsx+csv+docx+pdf", lambda: render_version(spec, ["xlsx", "csv", "docx", "pdf"], str(tmp_path), title_slug="sales-tracker", version=1))
    assert [(f.role, f.format, f.filename) for f in report.files] == [
        ("primary", "xlsx", "sales-tracker-v1.xlsx"),
        ("data", "csv", "sales-tracker-v1-pipeline.csv"),
        ("data", "csv", "sales-tracker-v1-regions.csv"),
        ("data", "csv", "sales-tracker-v1-notes-draft.csv"),
        ("companion", "docx", "sales-tracker-v1.docx"),
        ("companion", "pdf", "sales-tracker-v1.pdf"),
    ]
    assert [f.filename for f in report.files] == [
        T.download_name("Sales tracker", 1, "xlsx"), T.download_name("Sales tracker", 1, "csv", part="Pipeline"),
        T.download_name("Sales tracker", 1, "csv", part="Regions"), T.download_name("Sales tracker", 1, "csv", part="Notes   draft"),
        T.download_name("Sales tracker", 1, "docx"), T.download_name("Sales tracker", 1, "pdf"),
    ], "the names the pipeline checks against (CONTRACT-2 §2)"
    assert list(report.paths) == ["primary:xlsx:", "data:csv:pipeline", "data:csv:regions", "data:csv:notes-draft", "companion:docx:", "companion:pdf:"]
    csvs = [f for f in report.files if f.format == "csv"]
    assert [f.rows for f in csvs] == [30, 3, 2] and [f.columns for f in csvs] == [6, 3, 2]
    assert [f.sheet for f in csvs] == ["Pipeline", "Regions", "Notes   draft"]
    assert [f.title for f in csvs] == ["Pipeline", "Regions", "Notes   draft"]
    assert csvs[0].mime_type == "text/csv; charset=utf-8"
    wire = report.to_json()["files"]
    assert wire[1]["sheet"] == "Pipeline" and wire[1]["role"] == "data" and wire[1]["title"] == "Pipeline"
    # Through the pipeline's own reopen the wire title is composed from
    # the sheet's (tests/test_artifact_jobs.py pins "IR Session Audit — Data").
    from app.artifacts import pipeline as P

    checked = P._validate_files(str(tmp_path), report.to_json(), ["xlsx", "csv", "docx", "pdf"], "Sales tracker", 1, artifact_id="a" * 32, spec=spec)
    assert checked["problems"] == [], checked["problems"]
    assert [f["title"] for f in checked["files"] if f["role"] == "data"] == ["Sales tracker — Pipeline", "Sales tracker — Regions", "Sales tracker — Notes   draft"]
    assert [f["file_id"] for f in checked["files"]][1] == T.file_id_for("a" * 32, 1, "data", "csv", "Pipeline")
    docx_file, pdf_file = report.files[4], report.files[5]
    assert docx_file.rows == 35 and pdf_file.rows == 35 and pdf_file.pages and pdf_file.pages >= 2
    assert report.preview_kind == "grid" and report.preview_pages == pdf_file.pages
    assert (tmp_path / "preview.pdf").read_bytes() == (tmp_path / "sales-tracker-v1.pdf").read_bytes()
    assert set(report.timings) == {"charts", "xlsx", "csv", "docx", "pdf", "preview"}
    # The CSV carries the data only: the four hostile cells are apostrophe-led and said so.
    assert any("leading apostrophe in the CSV" in w for w in report.warnings)
    assert report.transform == {}
    # The pipeline's own reopen (tests/test_artifact_jobs.py) reads these
    # files by name; every one is where the report says.
    for f in report.files:
        assert (tmp_path / f.filename).stat().st_size == f.size


def test_one_sheet_workbook_csv_keeps_the_plain_name(tmp_path):
    spec = S.ArtifactSpec(kind="workbook", workbook=S.WorkbookSpec(title="Leads", sheets=[
        S.Sheet(name="Leads", columns=[S.Column(name="Id"), S.Column(name="Name")], rows=[["1", "a"], ["2", "b"]]),
    ]))
    report = render_version(spec, ["csv"], str(tmp_path), title_slug="leads", version=1)
    assert [(f.role, f.filename, f.title, f.sheet, f.rows) for f in report.files] == [("data", "leads-v1.csv", "Leads", "Leads", 2)]
    assert list(report.paths) == ["data:csv:"]
    assert report.preview_kind == "grid" and (tmp_path / "preview.pdf").is_file(), "the summary pdf still gives the card a thumbnail"
    assert not (tmp_path / "leads-v1.xlsx").exists()


def test_unsupported_and_duplicate_formats(tmp_path):
    # CONTRACT-2 §1: a workbook CAN be a Word/PDF tabular document now; the
    # genuinely impossible pair is a document asked for as csv.
    with pytest.raises(RenderError) as exc:
        render_version(document("brief", sections=1), ["csv"], str(tmp_path), title_slug="w", version=1)
    assert exc.value.category == "invalid_request"
    with pytest.raises(RenderError) as exc:
        render_version(workbook(), ["pptx"], str(tmp_path), title_slug="w", version=1)
    assert exc.value.category == "invalid_request"
    report = render_version(workbook(), ["xlsx", "xlsx", "pptx"], str(tmp_path), title_slug="w", version=1)
    assert [f.format for f in report.files] == ["xlsx"]


def test_tabular_document_row_ceiling_is_a_sentence(tmp_path, monkeypatch):
    import app.artifacts.render as R

    monkeypatch.setattr(R, "TABULAR_MAX_ROWS", 20)
    with pytest.raises(RenderError) as exc:
        render_version(workbook(rows=30), ["xlsx", "pdf"], str(tmp_path), title_slug="w", version=1)
    assert exc.value.category == "invalid_request" and "Excel or CSV" in exc.value.message and "35 rows" in exc.value.message
    assert not (tmp_path / "w-v1.pdf").exists()


def test_csv_row_count_mismatch_is_refused_by_validation(tmp_path, monkeypatch):
    """Belt and braces (CONTRACT-2 §11): the worker's own reopen counts the
    CSV's rows against the sheet; a writer that lost a row is caught here
    before the pipeline ever sees the file."""
    import app.artifacts.render as R
    from app.artifacts.render import csv as CSV

    real = CSV.write_csv

    def drops_one(columns, rows, path, **kw):
        rows = list(rows)
        return real(columns, rows[:-1], path, **kw)

    monkeypatch.setattr(CSV, "write_csv", drops_one)
    with pytest.raises(R.RenderError) as exc:
        render_version(workbook(rows=30), ["csv"], str(tmp_path), title_slug="w", version=1)
    assert exc.value.category == "validation_failure"
    assert "29 data rows; 30 were required" in exc.value.message


def test_page_bomb_is_a_validation_failure_with_a_safe_message(tmp_path):
    blocks = [S.Paragraph(text="p"), S.PageBreak()] * (T.MAX_PAGES + 1)
    spec = S.ArtifactSpec(kind="document", document=S.DocumentSpec(title="bomb", blocks=blocks))
    with pytest.raises(RenderError) as exc:
        render_version(spec, ["pdf"], str(tmp_path), title_slug="bomb", version=1)
    assert exc.value.category == "validation_failure"
    assert str(T.MAX_PAGES) in exc.value.message
    assert str(tmp_path) not in exc.value.message and "Traceback" not in exc.value.message


def test_library_crash_becomes_renderer_failure_without_a_path(tmp_path, monkeypatch):
    import app.artifacts.render.pptx as PX

    def boom(*a, **k):
        raise RuntimeError(f"secret path {tmp_path}/x")

    monkeypatch.setattr(PX, "render_pptx", boom)
    with pytest.raises(RenderError) as exc:
        render_version(deck(), ["pptx"], str(tmp_path), title_slug="d", version=1)
    assert exc.value.category == "renderer_failure"
    assert str(tmp_path) not in exc.value.message and "secret" not in exc.value.message
    assert exc.value.message.endswith(".")


def test_missing_out_dir_and_unknown_category(tmp_path):
    with pytest.raises(RenderError) as exc:
        render_version(deck(), ["pptx"], str(tmp_path / "nope"), title_slug="d", version=1)
    assert exc.value.category == "storage_failure"
    with pytest.raises(ValueError):
        RenderError("not_a_category", "x")


def test_font_coverage_warning_for_non_latin_text(tmp_path):
    spec = S.ArtifactSpec(kind="document", document=S.DocumentSpec(title="नमस्ते report", blocks=[S.Paragraph(text="Body in 中文 too")]))
    report = render_version(spec, ["pdf"], str(tmp_path), title_slug="intl", version=1)
    assert any("Devanagari" in w and "CJK" in w for w in report.warnings)
    assert report.files[0].pages == 1


# ---------------------------------------------------------------- worker --


def _job(tmp_path, spec, formats, **over):
    job = {"spec": spec.model_dump(), "formats": formats, "out_dir": str(tmp_path), "title_slug": "job", "version": 1, "effort": "fast"}
    job.update(over)
    return job


def test_worker_round_trips_in_process(tmp_path):
    from app.artifacts.render import worker

    result = worker.run_job(_job(tmp_path, workbook(), ["xlsx"]))
    assert "error" not in result and result["files"][0]["filename"] == "job-v1.xlsx"
    assert result["preview_kind"] == "grid" and result["files"][0]["role"] == "primary"
    assert result["paths"] == {"primary:xlsx:": str(tmp_path / "job-v1.xlsx")} and result["transform"] == {}
    # A workbook as pdf is a tabular document now (CONTRACT-2 §1); the
    # impossible pair is a document as csv.
    failed = worker.run_job(_job(tmp_path, document("brief", sections=1), ["csv"]))
    assert failed["error"]["category"] == "invalid_request"
    # The job may carry the engine's transform report; it is echoed.
    echoed = worker.run_job(_job(tmp_path, workbook(), ["xlsx"], transform={"rows": 34, "forward_filled": 25}))
    assert echoed["transform"] == {"rows": 34, "forward_filled": 25}
    bad = worker.run_job({"spec": {"kind": "nothing"}, "out_dir": str(tmp_path)})
    assert bad["error"]["category"] == "invalid_request" and "could not be read" in bad["error"]["message"]


def test_worker_subprocess_with_a_scrubbed_environment(tmp_path):
    """The exact way the job runner will call it: argv array, cwd = out_dir,
    an environment of six names, exit 0 and render-report.json."""
    from app.artifacts.render import worker

    orchestrator = Path(__file__).resolve().parents[1]
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(_job(tmp_path, document("sop", sections=2), ["pdf", "docx"])))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LANG": "C.UTF-8",
        "PYTHONPATH": str(orchestrator), "MPLCONFIGDIR": str(tmp_path / "mpl"),
    }
    if os.environ.get("FONTCONFIG_FILE"):
        env["FONTCONFIG_FILE"] = os.environ["FONTCONFIG_FILE"]
    (tmp_path / "mpl").mkdir()
    t = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "app.artifacts.render.worker", str(job_path)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=180, check=False)
    TIMINGS["worker subprocess sop pdf+docx (incl. interpreter start)"] = round(time.perf_counter() - t, 2)
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads((tmp_path / worker.REPORT_NAME).read_text())
    assert [f["format"] for f in report["files"]] == ["pdf", "docx"]
    assert (tmp_path / "job-v1.pdf").is_file() and (tmp_path / "job-v1.docx").is_file() and (tmp_path / "preview.pdf").is_file()
    # A failing job: exit 1 and an error file, nothing else leaks.
    job_path.write_text(json.dumps(_job(tmp_path, deck(), ["csv"])))
    proc = subprocess.run([sys.executable, "-m", "app.artifacts.render.worker", str(job_path)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=180, check=False)
    assert proc.returncode == 1
    assert json.loads((tmp_path / worker.REPORT_NAME).read_text())["error"]["category"] == "invalid_request"
    assert "Traceback" not in (tmp_path / worker.REPORT_NAME).read_text()
    # Usage errors.
    assert subprocess.run([sys.executable, "-m", "app.artifacts.render.worker"], cwd=str(tmp_path), env=env, capture_output=True, timeout=60).returncode == 2
    # The parent's MPLCONFIGDIR was used as given; nothing was made in TMPDIR.
    assert (tmp_path / "mpl").is_dir() and not list(tmp_path.glob("mpl-*"))


def test_worker_removes_the_matplotlib_dir_it_made_when_none_was_given(tmp_path):
    """Without MPLCONFIGDIR the worker makes a private one — and removes it,
    or every test and manual run leaked a directory under /tmp."""
    from app.artifacts.render import worker

    orchestrator = Path(__file__).resolve().parents[1]
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(_job(tmp_path, workbook(), ["xlsx"])))
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LANG": "C.UTF-8",
           "PYTHONPATH": str(orchestrator), "TMPDIR": str(scratch)}
    if os.environ.get("FONTCONFIG_FILE"):
        env["FONTCONFIG_FILE"] = os.environ["FONTCONFIG_FILE"]
    proc = subprocess.run([sys.executable, "-m", "app.artifacts.render.worker", str(job_path)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=180, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (tmp_path / worker.REPORT_NAME).is_file()
    assert list(scratch.iterdir()) == [], "the mpl-* directory was not removed"


def test_report_timings_ground_the_render_timeout():
    """Runs last (file order): prints what was measured. The render timeout
    is 180 s (config.artifact_render_timeout_s); every measured render must
    be far inside it even on a slow box."""
    print("\nrender timings (s):", json.dumps(TIMINGS, indent=1))
    for label, seconds in TIMINGS.items():
        assert seconds < 60, (label, seconds)
