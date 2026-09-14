"""One DuckDB instance per published warehouse snapshot (2026-09-13).

WHAT IT REMOVES. Every warehouse read — the SQL engine's `_execute`, person
resolution, the schema cache refresh, the /health probe — opened the file with
`duckdb.connect(path, read_only=True)` and closed it again. Opening a DuckDB
file deserialises its whole catalog, and this warehouse mirrors the org:
1,023 VARCHAR tables in `raw` plus 1,023 typed views in `main`. Measured on a
synthetic copy of that shape (dbperf 2026-09-13, DuckDB 1.5.5, 20,561 view
columns): connect + close = 205 ms, of which 172 ms remains with ZERO rows,
so it is the catalog, not the data. A person lookup that runs in 1.8 ms on an
open database cost 208 ms per call; a question that resolves people and runs
one statement paid it twice, the /health fan-out every few seconds.

HOW. The sync worker publishes the snapshot by `os.replace` (storage.py), so
a snapshot FILE is immutable once visible and a new one is a new inode. The
reader keeps one in-memory DuckDB instance per (device, inode, mtime, size)
with the snapshot ATTACHed read-only, and hands out a cursor per call:

  * NOT `duckdb.connect(path)` kept open. DuckDB caches instances by path, so
    while any connection to the old file is alive a new connect returns the
    OLD instance — the replaced snapshot keeps being served (verified: the
    value published second read back as the first while one connection was
    open). ATTACH inside a fresh `:memory:` instance has no such cache.
  * a new generation replaces the reference; cursors still running on the old
    one keep it alive and finish on the data they started with (verified:
    a cursor survives its parent being dropped; an explicit close would kill
    it, so nothing closes it).
  * external access off and the configuration LOCKED after the attach, so a
    statement cannot `SET` its way back to the file system or attach
    anything (PermissionException / InvalidInputException, tested).
  * a bounded buffer pool: the instance outlives the query, so what it caches
    stays. WAREHOUSE_READER_MEMORY_LIMIT (default 2GB; empty = DuckDB's own
    80%-of-RAM default). A 15-statement mix on the 1x copy grew the process
    365 MB and ran in 2.3 s against 5.6 s connecting per statement.

ONLY THE SNAPSHOT. The live `warehouse.duckdb` is the sync worker's write
target, and DuckDB is many-readers-OR-one-writer across processes: holding a
read connection on it open would lock the worker out for as long as it was
held. When the snapshot is not what `settings.duckdb_path` resolves to (no
snapshot yet, DUCKDB_USE_SNAPSHOT=false, a pinned test path) callers keep the
open-per-call path with its lock wait, unchanged.

WAREHOUSE_READER_CACHE=false turns the whole thing off.

REVIEWED 2026-09-14 (dbperf2, duckdb-datasets track), two additions:

  * SELF-HEAL. `lock_configuration` does not stop `DETACH`: a cursor that has
    not USEd the catalog can detach it for the whole instance (probed on
    DuckDB 1.5.5; once a cursor has USEd it, DuckDB refuses to detach its
    default database). sql_guard rejects DETACH/USE long before this, so this
    is defence in depth: a cursor that cannot USE the catalog drops the
    generation and re-attaches once, instead of every later call quietly
    falling back to the 205 ms open-per-call path until the next snapshot.
  * SPILL. A `:memory:` instance spills to `.tmp` under the process CWD; the
    reader points it at the process temp directory instead
    (WAREHOUSE_READER_TEMP_DIR). Spilling works with external access off
    (probed: an 8M-row ORDER BY under a 200 MB limit completes).

VERIFIED 2026-09-14 (dbperf2 adversarial verifier), three corrections:

  * ONE SPILL DIRECTORY PER GENERATION. DuckDB names its spill files
    `duckdb_temp_storage_<size>-0.tmp` with no per-instance part, and the
    instance that created the directory removes the whole directory when it
    closes. Two instances spilling into one directory at once (an old
    generation's cursor still sorting while the new generation sorts too)
    SEGFAULTED the process 2 of 2 times on DuckDB 1.5.5, and closing one
    instance killed the other's running spill ("Could not remove file").
    Separate directories: 2 of 2 correct. Each generation now spills into its
    own `gen-<pid>-<random>` leaf under WAREHOUSE_READER_TEMP_DIR, which
    DuckDB creates on first spill and removes when the generation closes.
  * NO WRITABLE CATALOG. The old per-call `connect(path, read_only=True)`
    instance had no writable database at all (even `ATTACH ':memory:'` is
    refused). A `:memory:` instance keeps a writable `memory` catalog, and
    what a statement created there (a table, a macro) stayed visible to every
    later cursor in the process, for every user, until the next snapshot. The
    `memory` catalog is detached after the USE, and every cursor checks that
    `warehouse` is still the only non-internal database: anything a statement
    attached since (`ATTACH ':memory:'` is still allowed with external access
    off) drops the generation, exactly like a lost catalog. sql_guard rejects
    CREATE/ATTACH first; this restores the read-only posture it documents.
    Cost of the check: ~0.1 ms per cursor (0.04 -> 0.14 ms p50).
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import uuid
from typing import Optional, Tuple

from ..config import settings

log = logging.getLogger(__name__)

#: The schema name the snapshot is attached under; every cursor USEs it so an
#: unqualified `FROM Interview__c` resolves exactly as on a direct connect.
CATALOG = "warehouse"


def _enabled() -> bool:
    raw = os.environ.get("WAREHOUSE_READER_CACHE", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _memory_limit() -> str:
    return os.environ.get("WAREHOUSE_READER_MEMORY_LIMIT", "2GB").strip()


def _temp_directory() -> str:
    return os.environ.get("WAREHOUSE_READER_TEMP_DIR", "").strip() or os.path.join(
        tempfile.gettempdir(), "warehouse-reader-spill"
    )


class _Generation:
    __slots__ = ("key", "instance")

    def __init__(self, key: Tuple, instance) -> None:
        self.key = key
        self.instance = instance


_lock = threading.Lock()
_current: Optional[_Generation] = None


def _identity(path: str) -> Tuple:
    st = os.stat(path)
    return (os.path.realpath(path), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _is_snapshot(path: str) -> bool:
    snapshot = getattr(settings, "duckdb_snapshot_path", "") or ""
    return bool(snapshot) and os.path.realpath(path) == os.path.realpath(snapshot)


def _generation_spill_dir() -> str:
    """A spill directory of this generation's own (see ONE SPILL DIRECTORY
    PER GENERATION). Only the parent is created here: DuckDB creates the leaf
    on first spill and removes it when the instance closes."""
    base = _temp_directory()
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass  # a spill then fails with DuckDB's own error, as before
    return os.path.join(base, f"gen-{os.getpid()}-{uuid.uuid4().hex}")


_CATALOGS_SQL = "SELECT database_name FROM duckdb_databases() WHERE NOT internal"


def _open_generation(path: str):
    import duckdb  # lazy

    instance = duckdb.connect(
        ":memory:",
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
    )
    try:
        quoted = path.replace("'", "''")
        instance.execute(f"ATTACH '{quoted}' AS {CATALOG} (READ_ONLY)")
        instance.execute(f"USE {CATALOG}")
        limit = _memory_limit()
        if limit:
            instance.execute("SET memory_limit = ?", [limit])
        instance.execute("SET temp_directory = ?", [_generation_spill_dir()])
        # The `:memory:` default catalog is writable and shared by every
        # cursor of this instance (see NO WRITABLE CATALOG above).
        instance.execute("DETACH memory")
        instance.execute("SET enable_external_access = false")
        instance.execute("SET lock_configuration = true")
    except Exception:
        instance.close()
        raise
    return instance


def cursor(path: str):
    """A read cursor on the cached snapshot instance, or None when the cache
    does not apply (`path` is not the published snapshot, caching is off, or
    the snapshot cannot be opened — the caller's own connect then reports it).

    The caller closes the cursor exactly as it closed its connection."""
    if not _enabled() or not _is_snapshot(path):
        return None
    try:
        key = _identity(path)
    except OSError:
        return None
    for attempt in (1, 2):
        try:
            generation = _generation_for(path, key)
        except Exception:  # noqa: BLE001 — never worse than the open-per-call path
            log.warning("warehouse reader: snapshot %s could not be attached", path, exc_info=True)
            return None
        cur = None
        try:
            cur = generation.instance.cursor()
            cur.execute(f"USE {CATALOG}")
            catalogs = [row[0] for row in cur.execute(_CATALOGS_SQL).fetchall()]
            if catalogs != [CATALOG]:
                raise RuntimeError(f"unexpected catalogs attached: {catalogs}")
            return cur
        except Exception:  # noqa: BLE001
            if cur is not None:
                cur.close()
            # The cached instance lost its catalog, or something was attached
            # next to it (see SELF-HEAL and NO WRITABLE CATALOG above).
            _forget(generation)
            if attempt == 2:
                log.warning("warehouse reader: snapshot %s is not usable", path, exc_info=True)
    return None


def _generation_for(path: str, key: Tuple) -> _Generation:
    global _current
    with _lock:
        generation = _current
        if generation is None or generation.key != key:
            generation = _Generation(key, _open_generation(path))
            # The previous generation is only dereferenced: cursors still
            # running on it keep it alive until they close.
            _current = generation
        return generation


def _forget(generation: _Generation) -> None:
    global _current
    with _lock:
        if _current is generation:
            _current = None


def reset() -> None:
    """Forget the cached generation (tests; an operator's manual refresh)."""
    global _current
    with _lock:
        _current = None
