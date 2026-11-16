"""sf_assistant.engine.snapshot: refresh (docker cp patched), file mode, meta file and path checks. No docker is run."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import sf_assistant.engine.snapshot as snap  # noqa: E402
from sf_assistant.engine.types import RecordsUnavailable  # noqa: E402
from sf_assistant.config import load_records_config  # noqa: E402
from warehouse_fixture import make_warehouse  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
                 "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(snap, "MIN_MAIN_TABLES", 1)


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _fake_run(source: Path, calls: list, returncode: int = 0, stderr: str = ""):
    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        if returncode == 0:
            shutil.copyfile(source, args[-1])
            os.chmod(args[-1], 0o644)
        return subprocess.CompletedProcess(args, returncode, "", stderr)
    return run


def test_refresh_replaces_atomically_with_private_mode_and_meta(tmp_path, monkeypatch):
    src = make_warehouse(tmp_path / "src")
    dest = tmp_path / "run" / "warehouse.duckdb"
    calls: list = []
    monkeypatch.setattr(snap.subprocess, "run", _fake_run(src, calls))
    lines: list[str] = []
    info = snap.refresh_snapshot(load_records_config(warehouse=str(dest), container="test-container"),
                                 progress=lines.append)
    assert calls[0][0][:3] == ["docker", "cp", "test-container:/data/warehouse.read.duckdb"]
    assert len(calls[0][0]) == 4 and calls[0][0][3] == str(dest) + f".tmp-{os.getpid()}"
    assert calls[0][1].get("check") is False and "shell" not in calls[0][1]
    assert dest.is_file() and _mode(dest) == 0o600
    meta = json.loads((tmp_path / "run" / "warehouse.duckdb.meta.json").read_text())
    assert _mode(str(dest) + ".meta.json") == 0o600
    assert set(meta) == {"copied_at_utc", "source", "bytes", "duckdb_version", "newest_sync_utc", "org_is_sandbox"}
    assert meta["source"] == "test-container:/data/warehouse.read.duckdb" and meta["org_is_sandbox"] is True
    assert meta["newest_sync_utc"] == "2026-10-13 14:30 UTC"
    assert info.newest_sync_utc == "2026-10-13 14:30 UTC" and info.bytes == dest.stat().st_size
    assert lines[0].startswith("copying test-container:/data/warehouse.read.duckdb") and lines[1] == "validating ..."
    assert lines[-1].startswith("snapshot refreshed:") and "preprod SANDBOX" in lines[-1]
    assert not list((tmp_path / "run").glob("*.tmp-*"))
    assert snap.read_meta(str(dest))["bytes"] == info.bytes


def test_failed_copy_keeps_old_snapshot_and_leaves_no_temp(tmp_path, monkeypatch):
    dest = tmp_path / "warehouse.duckdb"
    dest.write_bytes(b"old snapshot")
    calls: list = []
    monkeypatch.setattr(snap.subprocess, "run", _fake_run(tmp_path, calls, returncode=1,
                                                          stderr="Error: No such container: x\nmore"))
    with pytest.raises(snap.SnapshotError) as exc:
        snap.refresh_snapshot(load_records_config(warehouse=str(dest)), progress=lambda s: None)
    assert str(exc.value) == "docker cp failed: Error: No such container: x"
    assert dest.read_bytes() == b"old snapshot"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_invalid_copy_is_rejected_and_old_snapshot_kept(tmp_path, monkeypatch):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not a duckdb file" * 100)
    dest = tmp_path / "warehouse.duckdb"
    dest.write_bytes(b"old snapshot")
    monkeypatch.setattr(snap.subprocess, "run", _fake_run(bad, []))
    with pytest.raises(snap.SnapshotError) as exc:
        snap.refresh_snapshot(load_records_config(warehouse=str(dest)), progress=lambda s: None)
    assert str(exc.value).startswith("copied file is not a valid warehouse")
    assert dest.read_bytes() == b"old snapshot"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_too_few_tables_is_invalid(tmp_path, monkeypatch):
    src = make_warehouse(tmp_path / "src")
    monkeypatch.setattr(snap, "MIN_MAIN_TABLES", 1000)
    monkeypatch.setattr(snap.subprocess, "run", _fake_run(src, []))
    with pytest.raises(snap.SnapshotError, match="main tables"):
        snap.refresh_snapshot(load_records_config(warehouse=str(tmp_path / "w.duckdb")), progress=lambda s: None)


def test_docker_missing(tmp_path, monkeypatch):
    def run(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(snap.subprocess, "run", run)
    with pytest.raises(snap.SnapshotError, match="docker is not available on this machine"):
        snap.refresh_snapshot(load_records_config(warehouse=str(tmp_path / "w.duckdb")), progress=lambda s: None)


def test_forbidden_source_constant():
    assert snap.CONTAINER_SOURCE == "/data/warehouse.read.duckdb"
    assert snap.FORBIDDEN_SOURCE == "/data/warehouse.duckdb"
    assert snap.CONTAINER_SOURCE != snap.FORBIDDEN_SOURCE
    text = Path(snap.__file__).read_text()
    assert text.count('"docker", "cp"') == 1


def test_ensure_private(tmp_path):
    p = tmp_path / "f.duckdb"
    p.write_bytes(b"x")
    os.chmod(p, 0o644)
    assert snap.ensure_private(str(p)) is True
    assert _mode(p) == 0o600
    assert snap.ensure_private(str(p)) is False


@pytest.mark.parametrize("path", ["/data/warehouse.duckdb", "/data/warehouse.read.duckdb", "/data/x/y.duckdb",
                                  "/var/lib/docker/volumes/v/_data/warehouse.duckdb"])
def test_check_warehouse_path_refuses_container_paths(path):
    with pytest.raises(RecordsUnavailable, match="only a local snapshot copy may be opened"):
        snap.check_warehouse_path(path)


def test_check_warehouse_path_missing_file(tmp_path):
    with pytest.raises(RecordsUnavailable, match="no warehouse snapshot at .*run ./ask.sh --refresh-warehouse"):
        snap.check_warehouse_path(str(tmp_path / "none.duckdb"))
    assert snap.check_warehouse_path(str(tmp_path / "none.duckdb"), must_exist=False).endswith("none.duckdb")


def test_refresh_refuses_forbidden_destination(monkeypatch):
    monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: pytest.fail("docker must not run"))
    with pytest.raises(snap.SnapshotError, match="refusing"):
        snap.refresh_snapshot(load_records_config(warehouse="/data/warehouse.duckdb"), progress=lambda s: None)
