"""Reports path sanitization: .., absolute, nested, symlink escape (spec §8)."""
import pytest

from app.core.report_paths import ReportPathError, list_reports, resolve_report_file


@pytest.fixture()
def reports_dir(tmp_path):
    d = tmp_path / "reports"
    d.mkdir()
    return d


def test_valid_filename_resolves(reports_dir):
    (reports_dir / "q3-report.pdf").write_bytes(b"%PDF-1.4")
    path = resolve_report_file(reports_dir, "q3-report.pdf")
    assert path == (reports_dir / "q3-report.pdf").resolve()


def test_resolution_does_not_require_existence(reports_dir):
    # Existence is the endpoint's 404 concern, not the sanitizer's.
    path = resolve_report_file(reports_dir, "not-yet.docx")
    assert path.name == "not-yet.docx"


@pytest.mark.parametrize(
    "bad",
    [
        "../secret.txt",
        "..",
        ".",
        "",
        "   ",
        "foo/../bar.pdf",
        "a/b.pdf",           # nested
        "/etc/passwd",       # absolute
        "..\\evil.docx",     # windows-style traversal
        "nested\\path.pdf",  # windows-style nesting
        ".hidden.pdf",
        "report..v2.pdf",    # contains '..'
        "bad\x00name.pdf",
    ],
)
def test_bad_filenames_rejected(reports_dir, bad):
    with pytest.raises(ReportPathError):
        resolve_report_file(reports_dir, bad)


def test_symlink_escape_rejected(tmp_path, reports_dir):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = reports_dir / "sneaky.pdf"
    link.symlink_to(outside)
    with pytest.raises(ReportPathError):
        resolve_report_file(reports_dir, "sneaky.pdf")


def test_symlink_inside_reports_dir_allowed(reports_dir):
    real = reports_dir / "real.pdf"
    real.write_bytes(b"%PDF-1.4")
    link = reports_dir / "alias.pdf"
    link.symlink_to(real)
    path = resolve_report_file(reports_dir, "alias.pdf")
    assert path == real.resolve()


def test_list_reports(reports_dir):
    (reports_dir / "a.docx").write_bytes(b"x")
    (reports_dir / "b.pdf").write_bytes(b"yy")
    (reports_dir / ".hidden").write_bytes(b"z")
    (reports_dir / "subdir").mkdir()
    items = list_reports(reports_dir)
    names = {i["filename"] for i in items}
    assert names == {"a.docx", "b.pdf"}
    assert all({"filename", "size_bytes", "modified"} <= set(i) for i in items)


def test_list_reports_missing_dir(tmp_path):
    assert list_reports(tmp_path / "nope") == []
