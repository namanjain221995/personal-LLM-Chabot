"""The assistant pipeline of the sf_assistant package (``sf_assistant.cli``), unchanged, for the web page.

``Pipeline.from_home`` builds it once, exactly as ``sf_assistant.cli.main()`` does for merged mode with every
knowledge file and records on: one :class:`sf_assistant.config.Settings` (``load_settings(warehouse=...)``: no
``today``, no ``tz``, so live dates in the org's time zone), ``build(knowledge_files(...), settings)`` for
cfg/registry/system/llm and ``open_record_session(cfg, settings.records, knowledge_dir)`` for the record session.
``Pipeline.answer`` runs ``sf_assistant.cli.ask()`` (the same function ``python -m sf_assistant ask`` and ./ask.sh
run; until stage 2 the page ran the notebook folder's ``ask.py``, which it replaced with parity) for one question
with its stdout captured and returns the answer split into body and Source line, the data timestamp, the trace for
the admin panel, the record for the web log and, on a chart question, the chart items (``row["charts"]``, moved as they
are: never in the record for the web log).

The sf_assistant folder (``SFA_HOME``) is the live development tree (mounted read-only). So that a question never
runs a mix of old and new code, prompts or knowledge, start-up takes one consistent view of it:

- every module of ``sf_assistant.agent``, ``.engine``, ``.knowledge`` and ``.tools`` is imported at start-up (ask()
  imports some of them only on the first question; the modules of :data:`NOT_WIRED`, none now, are never imported), and
  every prompt file ``prompts/*.md`` is read once (``sf_assistant.prompts`` caches each text per process);
- the knowledge files are copied once into a private temp folder (``snapshot_knowledge``); the knowledge
  agent and every record session (also each reopen after a sync) read that copy, never the live folder. The flow
  components (``knowledge/components``: ``index.json`` and ``flows/*.json``) are copied with them, into the copy's
  ``_components`` subfolder (``snapshot_components``; no ``*.json`` of the catalog folder), and the knowledge agent's
  component tools read that copy.

Changes to the code, the prompts or the knowledge reach the page through a restart (``run.sh restart-page``).
The knowledge search must run on ``rank_bm25`` (what every evaluation used): the package falls back to its
own BM25 without it, which ranks differently, so start-up fails on that fallback.

The live read copy is replaced after each sync (``os.replace``: a new inode). Before every question
the file's identity (device, inode, mtime, size) is compared with the one the record session was
opened on; when it changed, the old session is closed and a new one is opened on the new copy. The
old one must be closed first: DuckDB keeps one database instance per path and process, so a
connection opened while the old one is still open shares the old instance and keeps reading the
replaced inode (checked with duckdb 1.5.6). An open that fails leaves records off; it is tried again
before the next question (after REOPEN_RETRY_S when the file did not change again). A record query
that fails while the file changed under it reopens once and asks the question once more.

No FastAPI import here, so the eval venv can import this module too.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import pkgutil
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from typing import Any, Callable

MODES = ("merged", "records", "knowledge")
#: The follow-up rewrite (``agent/followup.py``) read the question as unclear: the page and the ask service say this.
UNCLEAR_LINE = "I could not tell what this refers to. Please ask the full question, with the name or object."
#: The follow-up rewrite failed (its model call raised): the typed words are not answered as a fragment.
REWRITE_FAILED_LINE = ("The question could not be read together with the earlier questions this time. Please ask the "
                       "full question.")
REOPEN_RETRY_S = 60.0          # a failed open on the same file is tried again after this long
TRANSCRIPT_MAX = 60_000        # characters of captured stdout kept for the admin panel
#: the packages whose every module is imported at start-up (:func:`preload_package`)
PRELOAD_PACKAGES = ("sf_assistant.agent", "sf_assistant.engine", "sf_assistant.knowledge", "sf_assistant.tools")
#: modules no answer uses yet: never imported by the page. Empty since the term resolver is built by
#: sf_assistant.pipeline.build and open_record_session (behind SFA_RESOLVER, on by default): its modules
#: (knowledge.resolver, knowledge.terms, engine.vocabulary) are preloaded like every other one, so a reopen after an
#: edit of the live tree cannot load a newer copy of them
NOT_WIRED: tuple[str, ...] = ()


# ---------------------------------------------------------------- answer text

def read_as_line(rewritten: str) -> str:
    """``Read as: <the rewritten question>``, for a person to catch a wrong reading. It shows text the model wrote, so
    it gets the contact rule of an answer (the question does not ask: no address, no phone number) and stands in a code
    span with no backtick and no format character: nothing in it links, which backslash escapes do not give (GFM links a
    bare ``https:\\//`` after they are read). Shared by the page and the ask service."""
    from sf_assistant.engine.contact import asks_for_contact, drop_contact_details

    text = rewritten if asks_for_contact(rewritten) else drop_contact_details(rewritten)[0]
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf" and ch != "`")
    return f"Read as: `{text}`"


def _typed_may_run(question: str) -> bool:
    """The typed question is complete on its own, so the page may answer it as typed when the follow-up rewrite failed
    or reworded it: ``agent.followup``'s own rules, its strict one (``_stands_alone``) or its kinder one for the typed
    text (``_complete`` and no fragment). Page only: the ask service keeps its refusal."""
    from sf_assistant.agent.followup import _complete, _fragment, _stands_alone

    try:
        return bool(_stands_alone(question) or (_complete(question) and not _fragment(question)))
    except Exception:  # noqa: BLE001 - when in doubt, the refusal as before
        return False


def turns_problem(turns: Any) -> str | None:
    """What is wrong with the shape of ``recent_turns`` (a list of ``{role, content}`` strings), in fixed words; None
    when nothing is. A role other than user or assistant and the sizes are no fault: ``agent/followup.py`` drops and
    cuts them. Shared by the page (``POST /api/ask``) and the ask service."""
    if not isinstance(turns, list) or not all(isinstance(t, dict) and set(t) == {"role", "content"}
                                              and isinstance(t["role"], str) and isinstance(t["content"], str)
                                              for t in turns):
        return "recent_turns must be a list of {role, content}"
    return None


def is_source_line(line: str) -> bool:
    """A "Source: ..." line in any bullet or bold form (the same rule as sf_assistant.agent.checks._is_source_line)."""
    return re.match(r"source\s*:", re.sub(r"[*_`]+", "", line or "").strip().lstrip("-> ").strip().lower()) is not None


def clean_source_line(line: str) -> str:
    """'- **Source:** preprod ...' -> 'Source: preprod ...'."""
    text = re.sub(r"[*_`]+", "", line or "").strip().lstrip("-> ").strip()
    return re.sub(r"^source\s*:\s*", "Source: ", text, flags=re.IGNORECASE)


def split_source(answer: str) -> tuple[str, str | None]:
    """``(body, source line)`` of a record or merged answer: the Source line the record agent ends it with, taken out.

    The record agent writes the Source line last (``with_source_line``; the lookup footer goes before it), so only
    the Source lines at the end of the answer are taken (the last one is returned cleaned). A "Source:"
    line anywhere else is content (for example a field labelled Source) and stays in the body.
    """
    lines = (answer or "").replace("\r\n", "\n").split("\n")
    source = None
    while lines:
        if not lines[-1].strip():
            lines.pop()
        elif is_source_line(lines[-1]):
            last = clean_source_line(lines.pop())
            source = source or last
        else:
            break
    return "\n".join(lines).strip(), source


# ---------------------------------------------------------------- stdout capture

class StdoutCapture:
    """``sys.stdout`` stand-in: while a capture is open, print() output of every thread goes to it.

    ask() prints its answer and trace; its record agent runs in a second thread. Only one question runs
    at a time, so one global buffer is enough (and a question that ran past the timeout keeps writing to
    its own buffer until it ends, because the next question waits for it).
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self._buf: io.StringIO | None = None

    def write(self, text: str) -> int:
        buf = self._buf
        (buf if buf is not None else self._real).write(text)
        return len(text)

    def flush(self) -> None:
        if self._buf is None:
            self._real.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @contextlib.contextmanager
    def capture(self):
        buf = io.StringIO()
        self._buf = buf
        try:
            yield buf
        finally:
            self._buf = None


def install_capture() -> StdoutCapture:
    """Put a :class:`StdoutCapture` on ``sys.stdout`` once and return it."""
    if not isinstance(sys.stdout, StdoutCapture):
        sys.stdout = StdoutCapture(sys.stdout)
    return sys.stdout


# ---------------------------------------------------------------- the live read copy

def allow_read_copy(warehouse_module: Any, read_copy: str, forbidden: str, unavailable: type) -> None:
    """Let the record engine open exactly the live read copy ``read_copy``, nothing else that it refuses.

    The engine refuses every path under /data/ (it was written for a local ``docker cp`` snapshot). The web
    container mounts the data volume read-only at /data and reads the read copy the sync-worker publishes,
    so ``sf_assistant.engine.warehouse.check_warehouse_path`` (the check ``Warehouse.open`` runs) is wrapped:

    - a path that resolves to the writer file ``forbidden`` is refused first (the sync-worker's own file:
      a reader there would hold a lock the writer needs);
    - a path whose real path is exactly ``read_copy`` (a regular file; a symlink to anything else resolves
      elsewhere and does not match) is allowed;
    - every other path goes through the engine's own check unchanged (so the rest of /data stays refused).

    ``sf_assistant.engine.snapshot.check_warehouse_path`` (the refresh path, never used here) is not touched.
    """
    original = getattr(warehouse_module.check_warehouse_path, "sfa_original", warehouse_module.check_warehouse_path)

    def check_warehouse_path(path: str, *, must_exist: bool = True) -> str:
        real = os.path.realpath(os.path.expanduser(str(path)))
        if real == forbidden or real == os.path.realpath(forbidden):
            raise unavailable(f"refusing to open {real}: the sync-worker's writer file is never opened")
        if real == read_copy:
            if must_exist and not os.path.isfile(real):
                raise unavailable(f"no read copy at {real}; is the data volume mounted at /data?")
            return real
        return original(path, must_exist=must_exist)

    check_warehouse_path.sfa_original = original  # type: ignore[attr-defined]
    warehouse_module.check_warehouse_path = check_warehouse_path


def snapshot_knowledge(src_dir: str, files_of: Callable[[str], list[str]], root: str | None = None,
                       attempts: int = 3, wait_s: float = 1.0) -> tuple[str, list[str]]:
    """Copy the knowledge files of ``src_dir`` into a new private temp folder: ``(folder, its files)``.

    ``files_of`` is ``sf_assistant.knowledge.store.knowledge_files`` (``*.json`` without ``*.raw.json``), used on
    both sides.
    A file that does not parse as JSON (caught while the owner's editor rewrites it) is read again up to
    ``attempts`` times before start-up fails on it.
    """
    paths = files_of(src_dir)
    if not paths:
        raise RuntimeError(f"no knowledge files in {src_dir}")
    dest = tempfile.mkdtemp(prefix="sfa-knowledge-", dir=root)
    try:
        for path in paths:
            for attempt in range(attempts):
                with open(path, "rb") as fh:
                    data = fh.read()
                try:
                    json.loads(data)
                    break
                except ValueError:
                    if attempt == attempts - 1:
                        raise RuntimeError(f"knowledge file {path} is not valid JSON") from None
                    time.sleep(wait_s)
            with open(os.path.join(dest, os.path.basename(path)), "wb") as fh:
                fh.write(data)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest, files_of(dest)


def snapshot_components(src_dir: str, dest: str, attempts: int = 3, wait_s: float = 1.0) -> str | None:
    """Copy the flow components of ``src_dir`` (``index.json`` and every ``flows/*.json``) into ``dest`` (a new folder
    inside the knowledge copy): ``dest``, or None when ``src_dir`` holds no ``index.json`` (no component tools). A file
    that does not parse as JSON is read again up to ``attempts`` times before start-up fails on it."""
    if not os.path.isfile(os.path.join(src_dir, "index.json")):
        return None
    flows = os.path.join(src_dir, "flows")
    names = ["index.json"]
    if os.path.isdir(flows):
        names += sorted(os.path.join("flows", n) for n in os.listdir(flows) if n.endswith(".json"))
    for name in names:
        path = os.path.join(src_dir, name)
        for attempt in range(attempts):
            with open(path, "rb") as fh:
                data = fh.read()
            try:
                json.loads(data)
                break
            except ValueError:
                if attempt == attempts - 1:
                    raise RuntimeError(f"component file {path} is not valid JSON") from None
                time.sleep(wait_s)
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
    return dest


def import_package(home: str) -> Any:
    """Import ``sf_assistant`` from ``<home>/src`` (put on ``sys.path`` when needed) and check where it came from.

    ``SFA_HOME`` must name ``home`` before the first import: ``sf_assistant.config.HOME`` (the base of the prompt
    and knowledge folders) is read once, at import. Returns ``sf_assistant.config``.
    """
    src = os.path.join(home, "src")
    if not os.path.isdir(os.path.join(src, "sf_assistant")):
        raise RuntimeError(f"no src/sf_assistant in {home}")
    if src not in sys.path:
        sys.path.insert(0, src)
    pkg = importlib.import_module("sf_assistant")
    init = getattr(pkg, "__file__", None)       # None for a namespace package (a folder without __init__.py)
    where = os.path.dirname(os.path.dirname(os.path.realpath(init))) if init else list(getattr(pkg, "__path__", []))
    if where != os.path.realpath(src):
        raise RuntimeError(f"sf_assistant was imported from {where}, not from {src}")
    config = importlib.import_module("sf_assistant.config")
    if os.path.realpath(config.HOME) != home:
        raise RuntimeError(f"sf_assistant.config.HOME is {config.HOME}, not {home}: set SFA_HOME={home} "
                           "before sf_assistant is imported")
    return config


def preload_package() -> list[str]:
    """Import every module of :data:`PRELOAD_PACKAGES` (but :data:`NOT_WIRED`), ``sf_assistant.cli`` and ``openai``
    now, and read every prompt file; returns the module names imported from the packages.

    ask() imports the record session, the lean step, the renderer and others only inside a question, and a prompt
    file is read on its first use. Loaded at start-up, a question cannot combine modules or prompt texts loaded
    before and after an edit of the live tree.
    """
    names: list[str] = []
    for pkg_name in PRELOAD_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        names += [f"{pkg_name}.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)
                  if f"{pkg_name}.{m.name}" not in NOT_WIRED]
    for name in sorted(names):
        importlib.import_module(name)
    importlib.import_module("sf_assistant.cli")
    importlib.import_module("sf_assistant.prompts").all_prompts()
    importlib.import_module("openai")  # sf_assistant.agent.llm imports it at the first model call
    return sorted(names)


@contextlib.contextmanager
def _memory_run(memory: Any):
    """``sf_assistant.pipeline.memory_run(memory)`` for a conversation-memory run; nothing (and no import) for None."""
    if memory is None:
        yield None
        return
    from sf_assistant.pipeline import memory_run

    with memory_run(memory):
        yield memory


def file_identity(path: str) -> tuple | None:
    """``(device, inode, mtime_ns, size)`` of ``path``, None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _swap_error(row: dict | None) -> bool:
    """A record query of ``row`` failed in DuckDB (E13) or with an I/O error."""
    jl = (row or {}).get("jsonl") or {}
    for q in jl.get("queries") or []:
        err = str(q.get("error") or "")
        if not q.get("ok") and (q.get("error_code") == "E13" or "IOException" in err or "DuckDB" in err):
            return True
    return "IOException" in str(jl.get("record_error") or "")


# ---------------------------------------------------------------- the pipeline

class Pipeline:
    """sf_assistant's pipeline, built once. :meth:`answer` is called from one worker thread at a time.

    ``ask_mod`` is the module with ``ask()`` and ``open_record_session()`` (``sf_assistant.cli``; tests pass a fake).
    """

    def __init__(self, ask_mod: Any, cfg: Any, registry: Any, system: Any, llm: Any, rcfg: Any,
                 log: Callable[[dict], None] | None = None, max_rows: int = 20, objects: list[str] | None = None,
                 knowledge_dir: str | None = None, remove_knowledge_dir: bool = False, resolver: Any = None,
                 memory: Any = None) -> None:
        self.ask_mod = ask_mod
        self.resolver = resolver                    # the spelling help settings (settings.resolver); None: the env
        self.memory = memory                        # conversation memory (settings.memory); None: the page as before
        self.knowledge_dir = knowledge_dir          # the start-up copy every record session reads
        self._remove_knowledge_dir = remove_knowledge_dir
        self.cfg, self.registry, self.system, self.llm, self.rcfg = cfg, registry, system, llm, rcfg
        self.objects = list(objects or [])
        self.max_rows = max_rows
        self._log = log or (lambda event: None)
        self._capture = install_capture()
        self._run_lock = threading.Lock()
        self._n = 0
        self.session: Any = None
        self.session_identity: tuple | None = None
        self.status_lines: list[str] = []
        self.records_error: str | None = None
        self._failed: tuple[tuple | None, float] | None = None
        self._status: dict = {}
        self._reopen: dict | None = None   # close_s and open_s of the last _open_session (latency logging)
        self._open_session("startup")

    # ---- build (sf_assistant.cli main, merged mode, every knowledge file, records on)
    @classmethod
    def from_home(cls, home: str, warehouse: str, log: Callable[[dict], None] | None = None,
                  knowledge_root: str | None = None) -> "Pipeline":
        """Build the pipeline of the sf_assistant folder ``home`` (``SFA_HOME``) on the snapshot file ``warehouse``."""
        home = os.path.realpath(home)
        config = import_package(home)
        preload_package()

        import sf_assistant.cli as ask_mod
        import sf_assistant.engine.warehouse as engine_warehouse
        from sf_assistant.engine.types import RecordsUnavailable
        from sf_assistant.knowledge.store import knowledge_files

        if os.path.realpath(warehouse) == config.FORBIDDEN_SOURCE:
            raise RuntimeError(f"SFA_WAREHOUSE is the sync-worker's writer file {config.FORBIDDEN_SOURCE}; "
                               "use the read copy")
        allow_read_copy(engine_warehouse, config.CONTAINER_SOURCE, config.FORBIDDEN_SOURCE, RecordsUnavailable)
        t0 = time.perf_counter()
        # what main() loads without --knowledge, copied once: the knowledge agent and the record catalog
        # (rebuilt at every reopen) then always read the same files
        knowledge_dir, paths = snapshot_knowledge(ask_mod.KNOWLEDGE_DIR, knowledge_files, root=knowledge_root)
        try:
            # the flow components with them (a subfolder of the copy: no *.json of the catalog folder)
            components = snapshot_components(str(config.COMPONENTS_DIR), os.path.join(knowledge_dir, "_components"))
            # one Settings for the page, as cli.main builds it: the model endpoint (OKEVAL_* / SFA_* env), the
            # record settings on ``warehouse`` (no tz / today: the org's zone, live dates) and the knowledge copy
            # (and its components copy; none: no component tools)
            settings = config.load_settings(records=True, warehouse=warehouse, knowledge_dir=knowledge_dir,
                                            components_dir=components or os.path.join(knowledge_dir, "_components"))
            rcfg = settings.records
            try:
                cfg, objects, registry, system, llm = ask_mod.build(paths, settings)
            except SystemExit as exc:                               # build() exits on an invalid knowledge file
                raise RuntimeError(str(exc)) from None
            backend = getattr(getattr(registry, "index", None), "backend", None)
            if backend != "rank_bm25":
                raise RuntimeError(f"the knowledge search runs on {backend!r}, not rank_bm25 (is rank_bm25 installed?); "
                                   "its results would differ from every evaluated run")
            if log:
                event = {"event": "knowledge_built", "objects": len(objects), "files": len(paths),
                         "seconds": round(time.perf_counter() - t0, 2), "backend": backend,
                         "knowledge_copy": knowledge_dir, "model": cfg.model, "base_url": cfg.base_url}
                comps = getattr(registry, "components", None)
                if comps is not None:   # the flow components of the copy (counts only)
                    event["components"] = comps.knowledge.counts()
                spelling = list(getattr(registry, "resolver_lines", None) or [])   # none with SFA_RESOLVER=off
                if spelling:
                    event["spelling"] = spelling
                log(event)
            return cls(ask_mod, cfg, registry, system, llm, rcfg, log=log, objects=objects,
                       knowledge_dir=knowledge_dir, remove_knowledge_dir=True, resolver=settings.resolver,
                       memory=settings.memory)
        except BaseException:
            shutil.rmtree(knowledge_dir, ignore_errors=True)
            raise

    # ---- record session
    def _open_session(self, reason: str) -> bool:
        """Close the record session (if any), then open one on the current file."""
        old, self.session, self.session_identity = self.session, None, None
        t_close = time.perf_counter()
        if old is not None:
            # first: a connection opened while the old one is open would share DuckDB's instance of the old inode
            try:
                old.close()
            except Exception:  # noqa: BLE001 - closing never fails a question
                pass
            old = None   # released before the new open: the old catalog (and its term resolver) is not kept alive
        close_seconds = round(time.perf_counter() - t_close, 2)
        t0 = time.perf_counter()
        ident = file_identity(self.rcfg.warehouse)
        spelling = {"resolver": self.resolver} if self.resolver is not None else {}   # the page's one Settings
        if self.knowledge_dir:
            session, lines = self.ask_mod.open_record_session(self.cfg, self.rcfg, knowledge_dir=self.knowledge_dir,
                                                              **spelling)
        else:
            session, lines = self.ask_mod.open_record_session(self.cfg, self.rcfg, **spelling)
        seconds = round(time.perf_counter() - t0, 2)
        self._reopen = {"close_s": close_seconds, "open_s": seconds}   # for the ask that pays for it (answer)
        if session is None:
            self.records_error = (lines or ["records off"])[0]
            self._failed = (ident, time.monotonic())
            self._log({"event": "records_open_failed", "reason": reason, "lines": lines, "seconds": seconds,
                       "close_seconds": close_seconds})
            self._refresh_status()
            return False
        self.session, self.session_identity, self.status_lines = session, ident, list(lines)
        self.records_error, self._failed = None, None
        self._refresh_status()
        event = {"event": "records_opened" if reason == "startup" else "records_reopened", "reason": reason,
                 "seconds": seconds, "lines": lines, "inode": ident[1] if ident else None,
                 "mtime_ns": ident[2] if ident else None}
        if reason != "startup":
            event["close_seconds"] = close_seconds   # closing the old session, before the open
        self._log(event)
        load_errors = getattr(getattr(getattr(session, "tools", None), "catalog", None), "load_errors", None)
        if load_errors:
            self._log({"event": "catalog_load_errors", "errors": list(load_errors)[:20]})
        return True

    def ensure_fresh(self) -> None:
        """Reopen the record session when the read copy was replaced (or records are off)."""
        ident = file_identity(self.rcfg.warehouse)
        if self.session is not None and ident == self.session_identity:
            return
        if self._failed is not None and self._failed[0] == ident and time.monotonic() - self._failed[1] < REOPEN_RETRY_S:
            return
        self._open_session("the read copy was replaced" if self.session is not None else "records were off")

    def _refresh_status(self) -> None:
        st = None
        if self.session is not None:
            try:
                st = self.session.status()
            except Exception:  # noqa: BLE001
                st = None
        self._status = {
            "records_ok": self.session is not None,
            "data_synced": getattr(st, "newest_sync_utc", None),
            "age_hours": getattr(st, "age_hours", None),
            "origin": getattr(st, "origin", None),
            "status_line": st.line() if st is not None else None,
            "records_error": self.records_error,
            "model": getattr(self.cfg, "model", None),
            "base_url": getattr(self.cfg, "base_url", None),
            "objects": len(self.objects),
        }

    def status(self) -> dict:
        """The last known state (no DuckDB call; safe from the event loop thread)."""
        return dict(self._status)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        if self._remove_knowledge_dir and self.knowledge_dir:
            shutil.rmtree(self.knowledge_dir, ignore_errors=True)

    # ---- one question
    def _run(self, question: str, mode: str, context_objects: Any = (), memory: Any = None) -> tuple[dict, str, Any]:
        session = self.session if mode in ("merged", "records") else None
        self._n += 1
        extra = {"context_objects": tuple(context_objects)} if context_objects else {}   # fakes without it still work
        with self._capture.capture() as buf, _memory_run(memory):
            row = self.ask_mod.ask(question, self.cfg, self.registry, self.system, self.llm, self._n, False, session,
                                   self.max_rows, records_only=mode == "records" and session is not None,
                                   records_wanted=mode != "knowledge", lean_knowledge=True, **extra)
        return row, buf.getvalue(), session

    def answer(self, question: str, mode: str = "merged", context_objects: Any = (), memory: Any = None) -> dict:
        """Answer one question. Returns answer, kind, source_line, data_ts, trace (admin), log (web log) and, with a
        chart, charts. ``context_objects``: the objects the last answer named (prerequisite P2b; only the clarifying
        question of a record step that got no data reads them). ``memory``: a ``sf_assistant.pipeline.MemoryRun`` (the
        ask service's conversation memory): the question runs under it (``sf_assistant.pipeline.memory_run``, around
        ``ask_mod.ask``), and it gets back what ran; None (the page, the ask service with ``SFA_MEMORY=off``): nothing
        changes."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self._run_lock:
            self._reopen, reopen = None, None   # reopen: the reopen this ask paid for, {close_s, open_s, s}
            if mode != "knowledge":
                t_fresh = time.perf_counter()
                self.ensure_fresh()
                if self._reopen is not None:
                    reopen = {**self._reopen, "s": round(time.perf_counter() - t_fresh, 2)}
            if mode == "records" and self.session is None:
                out = self._records_off(question)
                if reopen:
                    out["log"]["reopen"] = reopen
                return out
            retried = False
            retry_s = None   # a swap retry: seconds from the first run's start to the second's (the lost run, the reopen)
            failed_reopen = None   # the reopen after a swap error that failed (no retry): {close_s, open_s, s}
            t_run = time.perf_counter()
            try:
                row, stdout, session = self._run(question, mode, context_objects, memory)
            except Exception:
                swapped = self.session is not None and file_identity(self.rcfg.warehouse) != self.session_identity
                if not swapped:
                    raise
                self._log({"event": "swap_retry", "why": "ask raised while the read copy was replaced"})
                if not self._open_session("error during a file swap"):
                    raise
                retry_s = round(time.perf_counter() - t_run, 2)
                row, stdout, session = self._run(question, mode, context_objects, memory)
                retried = True
            if (not retried and session is not None and _swap_error(row)
                    and file_identity(self.rcfg.warehouse) != self.session_identity):
                self._log({"event": "swap_retry", "why": "a record query failed while the read copy was replaced"})
                t_swap = time.perf_counter()
                if self._open_session("query error during a file swap"):
                    retry_s = round(time.perf_counter() - t_run, 2)
                    row, stdout, session = self._run(question, mode, context_objects, memory)
                    retried = True
                else:
                    failed_reopen = {**(self._reopen or {}), "s": round(time.perf_counter() - t_swap, 2)}
            self._refresh_status()
            return self._shape(question, mode, row, stdout, session, retried, reopen, retry_s, failed_reopen)

    def answer_followup(self, question: str, turns: Any, mode: str = "merged") -> dict:
        """:meth:`answer` after the follow-up rewrite of the ask service (``agent.followup.rewrite``, the same rules):
        with no usable earlier turn the typed question goes to :meth:`answer` unchanged and nothing else differs. A
        rewritten question is answered and the answer starts with its Read-as line (:func:`read_as_line`); an unclear
        one gets :data:`UNCLEAR_LINE` and a failed rewrite :data:`REWRITE_FAILED_LINE`, kind none, with no other model
        call. A question complete on its own (:func:`_typed_may_run`) is answered as typed when the rewrite call failed
        or the model reworded it (``standalone_changed``): only it could have come back (review of 2026-11-16: the page
        refused complete questions on a busy model); ``log["rewrite"]["answered"]`` is then "typed". The model's own
        UNCLEAR stays a refusal. ``log["rewrite"]`` says what the rewrite did (status, reason, ``seconds`` of the
        rewrite call; the question used only when it changed).

        With ``SFA_MEMORY=on`` (``self.memory``, from the page's Settings) the follow-up resolver reads the turns in
        place of the rewrite (:meth:`_followup_memory`; design v2 §6.1): the page has no store, so it runs with no state
        and can only answer a reply or run a question of its own. A fault of the resolver itself falls back to the
        rewrite below."""
        from sf_assistant.agent.followup import FAILED, UNCLEAR, last_answer_objects, rewrite, usable_turns

        if not usable_turns(turns):
            return self.answer(question, mode)
        if getattr(getattr(self, "memory", None), "on", False) and mode != "knowledge":
            got = self._followup_memory(question, turns, mode)
            if got is not None:
                return got
        t0 = time.perf_counter()
        step = rewrite(question, turns, self.llm)
        info = {"status": step.status, "reason": step.reason, "seconds": round(time.perf_counter() - t0, 2)}
        if ((step.status == FAILED or (step.status == UNCLEAR and step.reason == "standalone_changed"))
                and _typed_may_run(question)):
            if step.error:
                info["error"] = step.error
            info["answered"] = "typed"
            out = self.answer(question, mode)
            out.setdefault("log", {})["rewrite"] = info
            if isinstance(out.get("trace"), dict):
                out["trace"]["rewrite"] = dict(info)
            return out
        if step.status in (FAILED, UNCLEAR):
            if step.status == UNCLEAR and step.reply:
                info["reply"] = step.reply
            if step.error:
                info["error"] = step.error
            text = UNCLEAR_LINE if step.status == UNCLEAR else REWRITE_FAILED_LINE
            return {"answer": text, "kind": "none", "source_line": None, "data_ts": None,
                    "trace": {"mode": mode, "rewrite": dict(info), "queries": [], "timings": {}},
                    "log": {"rewrite": info}}
        objects = last_answer_objects(turns)   # P2b: only the clarifying question reads them
        out = self.answer(step.question, mode, objects) if objects else self.answer(step.question, mode)
        if step.question != question:
            info["question_used"] = step.question
            out["answer"] = read_as_line(step.question) + "\n\n" + str(out.get("answer") or "")
        out.setdefault("log", {})["rewrite"] = info
        if isinstance(out.get("trace"), dict):
            out["trace"]["rewrite"] = dict(info)
        return out

    def _followup_memory(self, question: str, turns: Any, mode: str) -> dict | None:
        """The page's follow-up with ``SFA_MEMORY=on``: ``agent.resolver.resolve`` with no state (the page stores none),
        then the reply it wrote or its question, run under a ``MemoryRun`` with its terms (``begin_question(terms=)``)
        and with a Read-as line when it changed. None when the resolver itself failed (the rewrite then answers)."""
        from sf_assistant.agent import query_state as qs
        from sf_assistant.agent import resolver
        from sf_assistant.agent.followup import last_answer_objects
        from sf_assistant.config import charts_settings
        from sf_assistant.pipeline import MemoryRun

        t0 = time.perf_counter()
        self.ensure_fresh()
        tools = getattr(self.session, "tools", None)
        catalog = getattr(tools, "catalog", None)
        try:
            today = tools.today() if callable(getattr(tools, "today", None)) else None
            names = tuple(tools.other_record_types()) if callable(getattr(tools, "other_record_types", None)) else ()
        except Exception:  # noqa: BLE001 - no business date: resolver_error, and the rewrite answers
            today, names = None, ()
        mem = self.memory
        res = resolver.resolve(question, turns, None, 0, "", self.llm, today=today,
                               view=qs.PlanView(catalog) if catalog is not None else None, max_calls=mem.max_calls,
                               max_age=mem.max_age, max_tokens=mem.tokens, shortcuts=mem.shortcuts,
                               charts_on=charts_settings().on, other_record_types=names)
        info = {"memory": res.log(), "seconds": round(time.perf_counter() - t0, 2)}
        if res.action == "reply":
            if res.reason == "resolver_error":
                return None
            if res.status == "failed" and _typed_may_run(question):   # as the rewrite: a complete question runs
                run = MemoryRun(terms=None)
                out = self.answer(question, mode, memory=run)
                out.setdefault("log", {})["rewrite"] = {**info, "answered": "typed"}
                return out
            text = {"answered": res.text, "unclear": UNCLEAR_LINE}.get(res.status, REWRITE_FAILED_LINE)
            return {"answer": text, "kind": "none", "source_line": None, "data_ts": None,
                    "trace": {"mode": mode, "rewrite": dict(info), "queries": [], "timings": {}},
                    "log": {"rewrite": info}}
        if res.action != "new":   # no state: no plan to patch or to ask about
            return None
        objects = last_answer_objects(list(turns)[-4:] if isinstance(turns, list) else turns)
        run = MemoryRun(terms=res.terms)
        out = (self.answer(res.question, mode, objects, memory=run) if objects
               else self.answer(res.question, mode, memory=run))
        if res.question != question:
            info["question_used"] = res.question
            out["answer"] = read_as_line(res.question) + "\n\n" + str(out.get("answer") or "")
        out.setdefault("log", {})["rewrite"] = info
        if isinstance(out.get("trace"), dict):
            out["trace"]["rewrite"] = dict(info)
        return out

    def _records_off(self, question: str) -> dict:
        text = "Record data is not available right now. Ask again in a few minutes, or ask with records and knowledge."
        return {"answer": text, "kind": "none", "source_line": None, "data_ts": None,
                "trace": {"mode": "records", "records_error": self.records_error, "queries": [], "timings": {}},
                "log": {"records_error": self.records_error}}

    def _shape(self, question: str, mode: str, row: dict, stdout: str, session: Any, retried: bool,
               reopen: dict | None = None, retry_s: float | None = None, failed_reopen: dict | None = None) -> dict:
        jl = dict(row.get("jsonl") or {})
        kind = jl.get("answer_kind") or row.get("answer") or "none"
        if kind in ("merged", "records"):        # the record agent ends these with its Source line
            body, source = split_source(jl.get("answer") or "")
        else:
            body, source = (jl.get("answer") or "").strip(), None
        if not body:
            # "not needed" only when the record side found no record question (no record error); an empty or failed
            # record answer in records mode is no answer (review of 2026-10-22, evaluation fix 2: such an answer now
            # ends as kind "none")
            not_needed = mode == "records" and kind == "none" and not (jl.get("record_error") or row.get("record_error"))
            body = ("No record data is needed for this question." if not_needed
                    else "No answer was found for this question.")
        if source is None and kind == "knowledge":
            source = "Source: object knowledge files."   # the page adds "No record data was used for this answer."
        data_ts = self._status.get("data_synced") if (session is not None and kind in ("merged", "records")) else None
        queries = [{"tool": q.get("tool"), "object": q.get("object"), "ok": q.get("ok"), "error": q.get("error"),
                    "error_code": q.get("error_code"), "sql": q.get("sql") or [], "params": q.get("params") or [],
                    "row_count": q.get("row_count"), "summary": q.get("summary")}
                   for q in jl.get("queries") or []]
        tri = jl.get("triage") if isinstance(jl.get("triage"), dict) else {}
        # the route and why the triage overruled it (a named person, a name no stored person has, a stored record value
        # the question names: value spots, 2026-10-28); the values as the JSONL keeps them (no stored value, no Id)
        route = {"route": tri.get("route"), "routed": tri.get("routed"), "overruled_by": tri.get("overruled_by"),
                 "overrule_reason": tri.get("overrule_reason"), "values": list(tri.get("values") or [])}
        trace = {
            "mode": mode, "knowledge_mode": jl.get("knowledge_mode"), "route": route, "records": row.get("records"),
            "audit": row.get("audit"), "knowledge_tool_calls": row.get("ktools"), "record_tool_calls": row.get("rtools"),
            "knowledge_model_calls": jl.get("knowledge_model_calls"),
            "queries": queries, "record_answer": jl.get("record_answer"), "knowledge_answer": jl.get("knowledge_answer"),
            "record_error": jl.get("record_error"), "merge_error": jl.get("merge_error"), "error": row.get("error"),
            "field_check": jl.get("field_check") or [], "lookup_footer": jl.get("lookup_footer"),
            "timings": {"total_s": jl.get("seconds"), "route_s": jl.get("route_seconds"),
                        "knowledge_s": jl.get("knowledge_seconds"), "records_s": jl.get("record_seconds"),
                        "merge_s": jl.get("merge_seconds")},
            "status_line": self._status.get("status_line"), "retried_after_swap": retried,
            "transcript": stdout[-TRANSCRIPT_MAX:],
        }
        for key in ("line_reason", "small_talk", "clarify", "display"):   # the prerequisites of 2026-11-25
            if jl.get(key):
                trace[key] = jl[key]
        jl.pop("record_facts", None)  # rows can be large; the queries keep SQL, params and row counts
        jl.pop("charts", None)        # the web log holds no chart; row["charts"] goes to the job reply only
        log = {"summary": {k: row.get(k) for k in ("answer", "records", "rq", "audit", "ktools", "rtools", "error",
                                                     "record_error")},
               "jsonl": jl, "retried_after_swap": retried, "status_line": self._status.get("status_line")}
        if reopen:   # page seconds = rewrite.seconds + reopen.s + retry_s + jsonl.seconds + failed_reopen.s + hand-off
            log["reopen"] = reopen
        if retry_s is not None:
            log["retry_s"] = retry_s
        if failed_reopen:
            log["failed_reopen"] = failed_reopen
        shaped = {"answer": body, "kind": kind, "source_line": source, "data_ts": data_ts, "trace": trace, "log": log}
        if row.get("charts"):
            shaped["charts"] = row["charts"]
        return shaped
