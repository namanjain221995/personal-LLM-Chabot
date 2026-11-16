"""The local warehouse snapshot: refresh (one ``docker cp``), metadata, file mode and path checks.

The only container interaction anywhere in okrec is::

    docker cp <container>:/data/warehouse.read.duckdb <dest>.tmp-<pid>

run as an argument list (no shell). The writer file ``/data/warehouse.duckdb`` is never opened or
copied. A refresh validates the copy read-only, replaces the old snapshot atomically and never
leaves a partial file behind.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
from typing import Any, Callable

from sf_assistant.config import CONTAINER_SOURCE, FORBIDDEN_SOURCE, RecordsConfig
from .types import RecordsUnavailable, SnapshotInfo

__all__ = ["SnapshotError", "refresh_snapshot", "read_meta", "ensure_private", "check_warehouse_path",
           "CONTAINER_SOURCE", "FORBIDDEN_SOURCE", "MIN_MAIN_TABLES"]

assert CONTAINER_SOURCE != FORBIDDEN_SOURCE

#: A valid copy has at least this many ``main`` tables/views (1058 on 2026-10-13). Tests lower it.
MIN_MAIN_TABLES = 1000

_FORBIDDEN_PREFIXES = ("/data/", "/var/lib/docker/")


class SnapshotError(Exception):
    """A refresh failed; ``str(exc)`` is the one-line reason. The old snapshot is kept."""


def _utc_text(ts: dt.datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def check_warehouse_path(path: str, *, must_exist: bool = True) -> str:
    """Resolve ``path`` and refuse anything that is not a local snapshot copy.

    Raises :class:`RecordsUnavailable` for a path under ``/data/`` or ``/var/lib/docker/`` (or the
    writer file itself) and, with ``must_exist``, for a missing file. Returns the real path.
    """
    real = os.path.realpath(os.path.expanduser(str(path)))
    if real.startswith(_FORBIDDEN_PREFIXES) or real in ("/data", FORBIDDEN_SOURCE) or \
            (os.path.basename(real) == "warehouse.duckdb" and os.path.dirname(real) == "/data"):
        raise RecordsUnavailable(f"refusing to open {real}: only a local snapshot copy may be opened")
    if must_exist and not os.path.isfile(real):
        raise RecordsUnavailable(f"no warehouse snapshot at {real}; run ./ask.sh --refresh-warehouse")
    return real


def ensure_private(path: str) -> bool:
    """chmod 600 when group or others can access ``path``; True when the mode was changed."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        os.chmod(path, 0o600)
        return True
    return False


def read_meta(path: str) -> dict | None:
    """Contents of ``<path>.meta.json`` written by the last refresh, or None."""
    try:
        with open(str(path) + ".meta.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _validate_copy(path: str) -> dict[str, Any]:
    """Open the copy read-only and check it looks like the warehouse. Raises SnapshotError."""
    try:
        import duckdb
    except ImportError:
        raise SnapshotError("duckdb is not installed in .venv (uv pip install --python .venv/bin/python "
                            "--no-deps -r web/requirements.lock)") from None
    from .warehouse import CONNECT_CONFIG

    try:
        con = duckdb.connect(path, read_only=True, config=dict(CONNECT_CONFIG))
    except Exception as exc:
        raise SnapshotError(f"copied file is not a valid warehouse: {str(exc).splitlines()[0][:200]}") from None
    try:
        con.execute("SET GLOBAL TimeZone='UTC'")
        n = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main'").fetchone()[0]
        if n < MIN_MAIN_TABLES:
            raise SnapshotError(f"copied file is not a valid warehouse: only {n} main tables (expected at least {MIN_MAIN_TABLES})")
        has_meta = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='raw' "
                               "AND table_name='_sync_meta'").fetchone()[0]
        if not has_meta:
            raise SnapshotError("copied file is not a valid warehouse: raw._sync_meta is missing")
        rows, newest = con.execute('SELECT COUNT(*), MAX(updated_at) FROM raw."_sync_meta"').fetchone()
        if not rows:
            raise SnapshotError("copied file is not a valid warehouse: raw._sync_meta is empty")
        has_org = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main' "
                              "AND table_name='Organization'").fetchone()[0]
        if not has_org:
            raise SnapshotError('copied file is not a valid warehouse: main."Organization" is missing')
        org = con.execute('SELECT "IsSandbox" FROM main."Organization" LIMIT 1').fetchone()
        sandbox = None if org is None or org[0] is None else bool(org[0])
        return {"newest_sync_utc": _utc_text(newest), "org_is_sandbox": sandbox, "duckdb_version": duckdb.__version__}
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"copied file is not a valid warehouse: {type(exc).__name__}: "
                            f"{str(exc).splitlines()[0][:200]}") from None
    finally:
        con.close()


def refresh_snapshot(rcfg: RecordsConfig, *, progress: Callable[[str], None] = print) -> SnapshotInfo:
    """Copy the container's read file to ``rcfg.warehouse`` atomically and validate it.

    Raises :class:`SnapshotError` (the old snapshot and its meta file are kept, no temp file is left).
    """
    try:
        dest = check_warehouse_path(rcfg.warehouse, must_exist=False)
    except RecordsUnavailable as exc:
        raise SnapshotError(str(exc)) from None
    tmp = dest + f".tmp-{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        source = f"{rcfg.container}:{CONTAINER_SOURCE}"
        progress(f"copying {source} ...")
        try:
            proc = subprocess.run(["docker", "cp", source, tmp], check=False, capture_output=True, text=True,
                                  timeout=300)
        except FileNotFoundError:
            raise SnapshotError("docker is not available on this machine") from None
        except subprocess.TimeoutExpired:
            raise SnapshotError("docker cp failed: timed out after 300 s") from None
        if proc.returncode != 0:
            first = ((proc.stderr or "").strip().splitlines() or ["no error output"])[0][:300]
            raise SnapshotError(f"docker cp failed: {first}")
        if not os.path.isfile(tmp):
            raise SnapshotError("docker cp failed: no file was copied")
        os.chmod(tmp, 0o600)
        progress("validating ...")
        facts = _validate_copy(tmp)
        size = os.path.getsize(tmp)
        os.replace(tmp, dest)
        copied = _utc_text(dt.datetime.now(dt.timezone.utc)) or ""
        meta = {"copied_at_utc": copied, "source": source, "bytes": size, "duckdb_version": facts["duckdb_version"],
                "newest_sync_utc": facts["newest_sync_utc"], "org_is_sandbox": facts["org_is_sandbox"]}
        _write_private(dest + ".meta.json", json.dumps(meta, indent=2) + "\n")
        info = SnapshotInfo(path=dest, copied_at_utc=copied, bytes=size, newest_sync_utc=facts["newest_sync_utc"],
                            org_is_sandbox=facts["org_is_sandbox"], duckdb_version=facts["duckdb_version"])
        progress(info.line())
        return info
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"refresh failed: {type(exc).__name__}: {str(exc)[:300]}") from None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
