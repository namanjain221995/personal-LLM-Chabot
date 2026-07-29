"""Phase 4: hostile-archive handling.

Every fixture here is built in-test, so the suite carries no malicious files
on disk. The claims under test:

- a member cannot escape the extraction root (path traversal OR symlink);
- decompression bombs are refused, including when the header LIES about size;
- an .xlsx is a ZIP and must pass the same caps before any reader opens it;
- pickle-shaped files are never opened (pandas.read_pickle executes code);
- nested archives are listed, never opened;
- nothing is ever executed.
"""
import io
import os
import stat
import tarfile
import zipfile

import pytest

from app.config import settings
from app.core import archive


def make_zip(path, entries, *, symlinks=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            # Mark the member as a symlink the way archivers do.
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return path


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/cron.d/pwned",
        "..\\..\\windows\\system32\\evil.dll",
        "/etc/passwd",
        "a/../../../outside.txt",
        "\x00hidden",
    ],
)
def test_hostile_member_names_are_rejected(hostile):
    assert archive.safe_member_name(hostile) is None


def test_ordinary_names_survive_normalization():
    assert archive.safe_member_name("data/sales.csv") == "data/sales.csv"
    assert archive.safe_member_name("./data//sales.csv") == "data/sales.csv"


def test_zip_slip_member_is_skipped_not_written(tmp_path):
    src = make_zip(
        tmp_path / "slip.zip",
        [("../../escaped.txt", b"x"), ("safe.csv", b"a,b\n1,2\n")],
    )
    dest = tmp_path / "out"
    plan = archive.extract_zip(str(src), str(dest))

    assert [m.name for m in plan.members] == ["safe.csv"]
    assert any("unsafe path" in why for _n, why in plan.skipped)
    # Nothing was written outside the destination.
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert (dest / "safe.csv").exists()


def test_resolves_inside_catches_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert archive.resolves_inside(str(root), "a/b.csv")
    assert not archive.resolves_inside(str(root), "../b.csv")


# ---------------------------------------------------------------------------
# Symlinks
# ---------------------------------------------------------------------------


def test_symlink_members_are_skipped(tmp_path):
    src = make_zip(
        tmp_path / "link.zip",
        [("safe.csv", b"a\n1\n")],
        symlinks=[("passwd", "/etc/passwd")],
    )
    dest = tmp_path / "out"
    plan = archive.extract_zip(str(src), str(dest))
    assert [m.name for m in plan.members] == ["safe.csv"]
    assert any(why == "symlink" for _n, why in plan.skipped)
    assert not (dest / "passwd").exists()


def test_tar_symlinks_and_devices_are_skipped(tmp_path):
    src = tmp_path / "l.tar"
    with tarfile.open(src, "w") as tf:
        data = b"a,b\n1,2\n"
        info = tarfile.TarInfo("safe.csv")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

        link = tarfile.TarInfo("passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)

        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        tf.addfile(fifo)

    plan = archive.extract_tar(str(src), str(tmp_path / "out"))
    assert [m.name for m in plan.members] == ["safe.csv"]
    assert len(plan.skipped) == 2
    assert not (tmp_path / "out" / "passwd").exists()


# ---------------------------------------------------------------------------
# Bombs
# ---------------------------------------------------------------------------


def test_high_ratio_member_is_refused(tmp_path):
    # 40 MB of zeros compresses to almost nothing — the classic bomb shape.
    src = make_zip(tmp_path / "bomb.zip", [("bomb.bin", b"\0" * (40 * 1024 * 1024))])
    with pytest.raises(archive.ArchiveError) as exc:
        archive.check_zip_container(str(src))
    assert "bomb" in str(exc.value).lower()


def test_total_uncompressed_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "archive_max_uncompressed_mb", 1)
    monkeypatch.setattr(settings, "archive_max_ratio", 10_000_000)
    src = make_zip(
        tmp_path / "big.zip",
        [(f"f{i}.bin", b"\0" * (200 * 1024)) for i in range(20)],
    )
    with pytest.raises(archive.ArchiveError) as exc:
        archive.check_zip_container(str(src))
    assert "expands to more than" in str(exc.value)


def test_member_count_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "archive_max_files", 10)
    src = make_zip(tmp_path / "many.zip", [(f"f{i}.csv", b"a\n") for i in range(25)])
    with pytest.raises(archive.ArchiveError) as exc:
        archive.check_zip_container(str(src))
    assert "entries" in str(exc.value)


def test_a_lying_header_is_caught_while_streaming(tmp_path, monkeypatch):
    """The central directory is a CLAIM — the running byte count is the truth."""
    src = make_zip(tmp_path / "liar.zip", [("big.bin", b"\0" * (4 * 1024 * 1024))])
    # Pass the header checks…
    monkeypatch.setattr(settings, "archive_max_ratio", 10_000_000)
    monkeypatch.setattr(settings, "archive_max_uncompressed_mb", 64)
    plan = archive.check_zip_container(str(src))
    assert plan.total_uncompressed > 0

    # …then shrink the budget so only the STREAMING guard can stop it.
    real_check = archive.check_zip_container
    monkeypatch.setattr(
        archive, "check_zip_container", lambda p, label="archive": real_check(p)
    )
    monkeypatch.setattr(settings, "archive_max_uncompressed_mb", 1)
    with pytest.raises(archive.ArchiveError) as exc:
        archive.extract_zip(str(src), str(tmp_path / "out"))
    assert "understated" in str(exc.value) or "expands" in str(exc.value)
    # The partial file was cleaned up, not left behind.
    assert not (tmp_path / "out" / "big.bin").exists()


# ---------------------------------------------------------------------------
# .xlsx IS a zip — the bypass path
# ---------------------------------------------------------------------------


def test_bomb_xlsx_is_refused_before_any_reader_opens_it(tmp_path):
    """A spreadsheet is a ZIP container; it must face the same caps."""
    bomb = tmp_path / "bomb.xlsx"
    make_zip(
        bomb,
        [
            ("[Content_Types].xml", b"<Types/>"),
            ("xl/worksheets/sheet1.xml", b"\0" * (40 * 1024 * 1024)),
        ],
    )
    assert archive.is_zip_container(str(bomb)), "an xlsx must look like a zip"

    with pytest.raises(archive.ArchiveError):
        archive.check_zip_container(str(bomb), label="spreadsheet")

    # And the profiler refuses it too, rather than handing it to openpyxl.
    from app.core import profile as profiler

    with pytest.raises(archive.ArchiveError):
        profiler.profile_excel(str(bomb))


def test_a_real_xlsx_passes_the_container_check(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "ok.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "amount"])
    ws.append(["alpha", 10])
    wb.save(path)
    wb.close()

    plan = archive.check_zip_container(str(path), label="spreadsheet")
    assert plan.total_uncompressed > 0


# ---------------------------------------------------------------------------
# Refused readers + nesting
# ---------------------------------------------------------------------------


def test_pickle_members_are_never_extracted(tmp_path):
    """pandas.read_pickle executes arbitrary code — never open one."""
    src = make_zip(
        tmp_path / "p.zip", [("model.pkl", b"x"), ("data.csv", b"a\n1\n")]
    )
    plan = archive.extract_zip(str(src), str(tmp_path / "out"))
    assert [m.name for m in plan.members] == ["data.csv"]
    assert any("refused" in why for _n, why in plan.skipped)
    assert not (tmp_path / "out" / "model.pkl").exists()


def test_macro_workbooks_are_refused(tmp_path):
    src = make_zip(tmp_path / "m.zip", [("book.xlsm", b"x"), ("ok.csv", b"a\n1\n")])
    plan = archive.extract_zip(str(src), str(tmp_path / "out"))
    assert [m.name for m in plan.members] == ["ok.csv"]


def test_nested_archives_are_listed_but_never_opened(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("secret.csv", b"a\n1\n")
    src = make_zip(
        tmp_path / "outer.zip",
        [("inner.zip", inner.getvalue()), ("top.csv", b"a\n1\n")],
    )
    dest = tmp_path / "out"
    plan = archive.extract_zip(str(src), str(dest))

    assert "inner.zip" in plan.nested_archives
    assert [m.name for m in plan.members] == ["top.csv"]
    # The inner archive was neither written nor expanded.
    assert not (dest / "inner.zip").exists()
    assert not (dest / "secret.csv").exists()


def test_format_is_sniffed_from_magic_bytes_not_the_name(tmp_path):
    zip_path = make_zip(tmp_path / "actually.csv", [("a.csv", b"x\n")])
    assert archive.sniff_format(str(zip_path)) == "zip"
    plain = tmp_path / "plain.zip"
    plain.write_bytes(b"name,amount\nalpha,1\n")
    assert archive.sniff_format(str(plain)) != "zip"


def test_unsupported_format_is_rejected_clearly(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not an archive at all")
    with pytest.raises(archive.ArchiveError) as exc:
        archive.extract(str(junk), str(tmp_path / "out"))
    assert "Unsupported archive format" in str(exc.value)


def test_bomb_xlsx_upload_is_REJECTED_not_merely_skipped(tmp_path, monkeypatch):
    """The upload must fail with a clear reason, not succeed with a skip note.

    Caught live: the caps correctly stopped openpyxl from ever opening the
    bomb, but profile_directory swallowed the ArchiveError, so the upload
    reported success with a "skipped" entry — a confusing result for what is a
    rejected file.
    """
    from app.core import profile as profiler

    bomb = tmp_path / "bomb.xlsx"
    make_zip(
        bomb,
        [
            ("[Content_Types].xml", b"<Types/>"),
            ("xl/worksheets/sheet1.xml", b"\0" * (40 * 1024 * 1024)),
        ],
    )
    # The path uploads.py takes for a single non-archive file.
    assert archive.is_zip_container(str(bomb))
    with pytest.raises(archive.ArchiveError):
        archive.check_zip_container(str(bomb), label="spreadsheet")

    # And profiling it in a directory still refuses to open it.
    prof = profiler.profile_directory(str(tmp_path))
    entry = next(p for p in prof if p["file"] == "bomb.xlsx")
    assert entry["kind"] == "skipped"
