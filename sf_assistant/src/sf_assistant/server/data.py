"""What the MCP server serves: the knowledge registry and the record tools on the read copy.

Knowledge (:func:`load_knowledge`): the knowledge files are copied once, at start, into a private temp folder (mode
700) and loaded from that copy, so the knowledge tools, the resources and the record catalog (rebuilt at every reopen)
always read the same files, whatever happens to ``knowledge/objects/`` while the server runs. The registry is built
as :func:`sf_assistant.pipeline.build` builds the knowledge agent's: one file -> ``ToolRegistry`` (15 tools), several
-> ``MultiToolRegistry`` (18 tools, 21 with the flow components); a file that does not validate stops the start. The
flow components (``knowledge/components``: ``index.json`` and ``flows/*.json``) are copied into the same private
folder (its ``_components`` subfolder, never a ``*.json`` of the catalog folder) and loaded from that copy when the
registry has several objects, so the component tools read what was there at start, too.

Records (:class:`Records`): ``RecordTools`` (the 14 record tools of the record agent, with their field blocklist,
sensitive objects and SQL guard) opened read-only on one snapshot file. In the container that file is the live read
copy ``/data/warehouse.read.duckdb`` (the data volume, mounted read-only); the engine refuses every path under
``/data/``, so :func:`allow_read_copy` lets it open exactly that file and nothing else there; the sync-worker's writer
file ``/data/warehouse.duckdb`` stays refused. The read copy is replaced after each sync (a new inode): before every
call the file's identity is compared with the one the tools were opened on, and on a change the old connection is
closed first and the tools are opened again (DuckDB keeps one database instance per path and process, so a connection
opened while the old one is open would keep reading the replaced inode). An open that fails leaves records off with
the reason; it is tried again before a later call (after ``retry_s`` when the file did not change). A call that fails
while the file changed under it is run once more on the reopened copy. Every call starts a fresh question context
(``begin_question``): query ids restart at q1 and no per-question cache crosses calls. Calls run one at a time.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sf_assistant.config import CONTAINER_SOURCE, FORBIDDEN_SOURCE, RecordsConfig, ResolverConfig
from sf_assistant.engine.types import RecordsUnavailable
from sf_assistant.knowledge.components import KINDS, ComponentError, has_index, load_components
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge, knowledge_files
from sf_assistant.tools.knowledge_tools import MultiToolRegistry, ToolRegistry

__all__ = ["Knowledge", "load_knowledge", "copy_components", "read_copy_check", "allow_read_copy", "file_identity",
           "Records"]


# ---------------------------------------------------------------------------------------------------- knowledge

@dataclass
class Knowledge:
    """The loaded knowledge side: the registry, the copied files and one row per object."""

    registry: Any                               # ToolRegistry | MultiToolRegistry
    folder: str                                 # the private copy (the record catalog reads it too)
    paths: dict[str, str] = field(default_factory=dict)        # object API name -> its file in ``folder``
    rows: dict[str, dict] = field(default_factory=dict)        # object API name -> {label, one_line}
    resolver_lines: list[str] = field(default_factory=list)    # the spelling help's start-up lines ([] when off)
    components: dict[str, int] = field(default_factory=dict)   # loaded flow components per kind ({} when none)

    def close(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def copy_components(src: str | Path, dest: str | Path) -> str | None:
    """Copy the component knowledge of ``src`` (``index.json`` and every ``<kind folder>/*.json``) into ``dest``
    (created, mode 700; files mode 600): the folder, or None when ``src`` holds no ``index.json`` (nothing copied)."""
    if not has_index(src):
        return None
    os.makedirs(dest, mode=0o700, exist_ok=True)
    names = ["index.json"] + [os.path.join(sub, os.path.basename(p)) for sub in KINDS.values()
                              for p in sorted(Path(src, sub).glob("*.json"))]
    for name in names:
        target = os.path.join(str(dest), name)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        shutil.copyfile(os.path.join(str(src), name), target)
        os.chmod(target, 0o600)
    return str(dest)


def load_knowledge(folder: str | Path, file: str | Path | None = None, root: str | None = None,
                   resolver: ResolverConfig | None = None, components: str | Path | None = None) -> Knowledge:
    """Copy the knowledge files of ``folder`` (every ``*.json`` but ``*.raw.json``, as the cli) into a private temp
    folder and build the registry from the copies: of every file, or of ``file`` alone (``--knowledge FILE``, as the
    cli; it is copied too). The record catalog reads the whole copied folder either way, as the cli's record session
    reads its whole knowledge folder whatever ``--knowledge`` gives the knowledge agent.

    ``resolver`` (``settings.resolver``; None or off: nothing): with the spelling help on, the registry gets the
    knowledge term resolver of these files and the word list, as ``pipeline.build`` gives the knowledge agent's (the
    search reads a misspelt word as it reads; never fatal), and ``resolver_lines`` holds its start-up lines.

    ``components`` (``settings.components_dir``; None or a folder without ``index.json``: none): with several
    files the components are copied (:func:`copy_components`) and the registry gets the component tools from the
    copy, as ``pipeline.build`` gives the knowledge agent; ``components`` of the result counts them.

    Raises ValueError when there is no file, or a file of the registry or a component file does not validate."""
    paths = knowledge_files(str(folder))
    if file is None and not paths:
        raise ValueError(f"no knowledge files in {folder}")
    if file is not None and not os.path.isfile(str(file)):
        raise ValueError(f"no knowledge file {file}")
    dest = tempfile.mkdtemp(prefix="sfa-mcp-knowledge-", dir=root)
    try:
        def copy(path: str, where: str) -> str:
            os.makedirs(where, mode=0o700, exist_ok=True)
            target = os.path.join(where, os.path.basename(path))
            shutil.copyfile(path, target)
            os.chmod(target, 0o600)
            return target

        copies = [copy(p, dest) for p in paths]
        if file is not None:
            copies = [copy(str(file), os.path.join(dest, "_single"))]   # not a *.json of the catalog folder
        kns = []
        for path in copies:
            kn = ObjectKnowledge.load(path)
            problems = [p for p in kn.validate() if not p.startswith("WARN:")]
            if problems:
                raise ValueError(f"knowledge file {os.path.basename(path)} is invalid: " + "; ".join(problems[:5]))
            kns.append(kn)
        loaded = None
        if len(kns) > 1 and has_index(components):
            try:
                loaded = load_components(copy_components(components, os.path.join(dest, "_components")))
            except ComponentError as exc:
                raise ValueError(str(exc)) from None
        if len(kns) == 1:
            registry: Any = ToolRegistry(kns[0], KnowledgeIndex(kns[0].chunks()))
        else:
            registry = MultiToolRegistry(kns, components=loaded)
        out = Knowledge(registry=registry, folder=dest, components=loaded.counts() if loaded is not None else {})
        if resolver is not None and resolver.problem:
            out.resolver_lines = [f"spelling help off: {resolver.problem}"]
        elif resolver is not None and resolver.enabled:
            from sf_assistant.knowledge.terms import knowledge_resolver, lexicon_arg

            speller, out.resolver_lines = knowledge_resolver([k.raw for k in kns], lexicon_arg(resolver.lexicon))
            if speller is not None:
                registry.attach_resolver(speller)
        for kn, path in zip(kns, copies):
            info = kn.object_info
            summary = info.get("discovery_summary") or {}
            out.paths[kn.api_name] = path
            out.rows[kn.api_name] = {"label": info.get("label") or kn.api_name,
                                     "one_line": summary.get("one_line") or info.get("description") or ""}
        return out
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------------------------------- the read copy

def read_copy_check(original: Callable[..., str], read_copy: str = CONTAINER_SOURCE, forbidden: str = FORBIDDEN_SOURCE,
                    unavailable: type = RecordsUnavailable) -> Callable[..., str]:
    """A ``check_warehouse_path`` that also lets exactly ``read_copy`` through (the same rule as the web page):

    - a path that resolves to the writer file ``forbidden`` is refused first;
    - a path whose real path is exactly ``read_copy`` is allowed (a symlink to anything else resolves elsewhere);
    - every other path goes through ``original`` unchanged, so the rest of ``/data`` stays refused."""
    def check_warehouse_path(path: str, *, must_exist: bool = True) -> str:
        real = os.path.realpath(os.path.expanduser(str(path)))
        if real in (forbidden, os.path.realpath(forbidden)):
            raise unavailable(f"refusing to open {real}: the sync-worker's writer file is never opened")
        if real == read_copy:
            if must_exist and not os.path.isfile(real):
                raise unavailable(f"no read copy at {real}; is the data volume mounted at /data?")
            return real
        return original(path, must_exist=must_exist)

    check_warehouse_path.sfa_original = original  # type: ignore[attr-defined]
    return check_warehouse_path


def allow_read_copy() -> None:
    """Install :func:`read_copy_check` in ``sf_assistant.engine.warehouse`` (the check ``Warehouse.open`` runs).

    Called only when the server's snapshot is the read copy itself (the container); idempotent."""
    from sf_assistant.engine import warehouse as _wh

    original = getattr(_wh.check_warehouse_path, "sfa_original", _wh.check_warehouse_path)
    _wh.check_warehouse_path = read_copy_check(original)


def file_identity(path: str) -> tuple | None:
    """``(device, inode, mtime_ns, size)`` of ``path``, None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _swap_error(out: dict, code: str | None) -> bool:
    """The call failed in DuckDB (E13) or with an I/O error: the file may have been replaced under it."""
    return not out.get("ok") and (code == "E13" or "IOException" in str(out.get("error") or ""))


# ---------------------------------------------------------------------------------------------------- records

class Records:
    """The record tools on one snapshot file, reopened when the file is replaced."""

    def __init__(self, rcfg: RecordsConfig, knowledge_dir: str, retry_s: float = 60.0,
                 log: Callable[[str], None] | None = None, resolver: ResolverConfig | None = None) -> None:
        self.rcfg = rcfg
        self.knowledge_dir = knowledge_dir
        self.resolver = resolver   # the spelling help settings (settings.resolver); None: SFA_RESOLVER at each open
        self.retry_s = retry_s
        self._log = log or (lambda line: None)
        self._lock = threading.Lock()
        self.tools: Any = None
        self.identity: tuple | None = None
        self.error: str | None = None
        self._failed: tuple[tuple | None, float] | None = None
        if os.path.realpath(rcfg.warehouse) == CONTAINER_SOURCE:
            allow_read_copy()
        with self._lock:
            self._open("start")

    # ---- open / reopen (callers hold the lock)
    def _open(self, reason: str) -> None:
        from sf_assistant.tools.open_records import open_records

        old, self.tools, self.identity = self.tools, None, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - closing never fails a call
                pass
            old = None   # released before the new open: the old catalog (and its term resolver) is not kept alive
        ident = file_identity(self.rcfg.warehouse)
        t0 = time.perf_counter()
        try:
            self.tools = open_records(self.rcfg, self.knowledge_dir, self.resolver)
        except RecordsUnavailable as exc:
            self.error = f"records off: {exc}"
        except Exception as exc:  # noqa: BLE001 - never stops the server
            self.error = f"records off: {type(exc).__name__}: {exc}"
        if self.tools is None:
            self._failed = (ident, time.monotonic())
            self._log(f"records not opened ({reason}): {self.error}")
            return
        self.identity, self.error, self._failed = ident, None, None
        self._log(f"records opened ({reason}, {time.perf_counter() - t0:.1f} s): {self.tools.status().line()}")
        for line in self.tools.resolver_lines:   # the spelling help (on by default; none with SFA_RESOLVER=off)
            self._log(line)

    def _ensure_fresh(self) -> None:
        ident = file_identity(self.rcfg.warehouse)
        if self.tools is not None and ident == self.identity:
            return
        if self._failed is not None and self._failed[0] == ident and time.monotonic() - self._failed[1] < self.retry_s:
            return
        self._open("the snapshot file was replaced" if self.tools is not None else "records were off")

    # ---- use
    def call(self, name: str, arguments: dict | None) -> tuple[dict, int | None, str | None]:
        """``(the tool's result envelope, rows its statements returned, its error code)``. Never raises."""
        with self._lock:
            self._ensure_fresh()
            if self.tools is None:
                return ({"ok": False, "result": None, "error": self.error or "records off", "chunk_ids": []},
                        None, "records_off")
            out, rows, code = self._call(name, arguments)
            if _swap_error(out, code) and file_identity(self.rcfg.warehouse) != self.identity:
                self._open("a call failed while the snapshot file was replaced")
                if self.tools is not None:
                    out, rows, code = self._call(name, arguments)
            return out, rows, code

    def chart_context(self) -> tuple[Any, str | None]:
        """``(the catalog, today)`` now, ``(None, None)`` while records are off (a chart of a call's result is built
        outside the lock, and ``self.tools`` can be None or swapped there)."""
        with self._lock:
            if self.tools is None:
                return None, None
            return self.tools.catalog, self.tools.status().today

    def _call(self, name: str, arguments: dict | None) -> tuple[dict, int | None, str | None]:
        tools = self.tools
        tools.begin_question("")
        out = tools.call(name, dict(arguments or {}))
        evidence = tools.evidence
        rows = sum(st.row_count for ev in evidence for st in ev.statements) if evidence else None
        code = next((ev.error_code for ev in evidence if ev.error_code), None)
        return out, rows, code

    def records_status(self) -> Any:
        """The ``RecordsStatus`` now (recomputed), or None while records are off."""
        with self._lock:
            self._ensure_fresh()
            return self.tools.status() if self.tools is not None else None

    def status(self) -> dict:
        """The freshness of the data (``RecordsStatus`` as a dict), or ``{"records": "off", "reason": ...}``."""
        import dataclasses

        with self._lock:
            self._ensure_fresh()
            if self.tools is None:
                return {"records": "off", "reason": self.error}
            st = self.tools.status()
            catalog = self.tools.catalog
            sync = {s.api: catalog.synced_at(s.api) for s in sorted(catalog.queryable(), key=lambda s: s.api)}
            return {"records": "on", **dataclasses.asdict(st), "line": st.line(), "objects_synced": sync}

    @property
    def on(self) -> bool:
        return self.tools is not None

    def close(self) -> None:
        with self._lock:
            if self.tools is not None:
                try:
                    self.tools.close()
                finally:
                    self.tools = None
