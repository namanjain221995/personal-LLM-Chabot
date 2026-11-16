"""``open_records``: snapshot -> warehouse connection -> catalog -> :class:`sf_assistant.tools.record_tools.RecordTools`.

With the spelling help on (``SFA_RESOLVER=on``, :class:`sf_assistant.config.ResolverConfig`) the full term resolver is
built right after the catalog (:func:`attach_spelling_help`) and attached to it (``catalog.resolver``); with it off
nothing more is imported or run.
"""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sf_assistant.config import RecordsConfig, ResolverConfig, resolver_settings
from sf_assistant.engine.types import RecordsUnavailable

__all__ = ["open_records"]

#: Limits of one snapshot statement of the vocabulary build (through the warehouse guard): the person statements of
#: the current snapshot return thousands of rows, so 600 would cut them silently; a statement that still hits a limit
#: is counted (``info["snapshot"]["truncated"]``) and warned about on the start-up lines.
VOCAB_MAX_ROWS = 500_000
VOCAB_MAX_BYTES = 100_000_000


def _valid_tz(name: object) -> bool:
    try:
        ZoneInfo(str(name))
        return bool(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def attach_spelling_help(wh, catalog, rcfg: RecordsConfig, knowledge_dir: str,
                         resolver: ResolverConfig | None = None) -> list[str]:
    """Build the full term resolver of a record session and attach it to ``catalog`` (``catalog.resolver``); returns
    the start-up lines. ``resolver``: the spelling help settings (None: read ``SFA_RESOLVER`` / ``SFA_LEXICON`` now).

    Off (``SFA_RESOLVER=off``; on is the default): returns [] and does nothing else. On: the vocabulary of every
    knowledge file in ``knowledge_dir``, the English word list and the snapshot (stored values and person names) read
    through the warehouse's guarded, read-only connection (``wh.run``, never a second connection to the file) with the
    catalog's policy (allowed tables, blocked columns and fields, sensitive objects), then indexed once (the knowledge
    and word-list part is kept per process, ``terms.start_vocabulary``; measured cost: docs/resolver.md "Cost"). Never
    raises: a bad setting, a missing word list or any error leaves ``catalog.resolver`` None and gives one line
    ``spelling help off: <reason>``; the records stay on.
    """
    rc = resolver_settings() if resolver is None else resolver
    if rc.problem:   # an invalid SFA_RESOLVER: off, with one line (never fatal)
        return [f"spelling help off: {rc.problem}"]
    if not rc.enabled:
        return []
    t0 = time.perf_counter()
    try:
        from sf_assistant.engine.catalog import SENSITIVE_OBJECTS
        from sf_assistant.engine.vocabulary import SnapshotPolicy, build_vocabulary
        from sf_assistant.knowledge.resolver import build
        from sf_assistant.knowledge.terms import lexicon_arg, spelling_status, word_list_problem

        cut = [0]

        def query(sql: str) -> list:
            st = wh.run(sql, max_rows=VOCAB_MAX_ROWS, max_bytes=VOCAB_MAX_BYTES, timeout_s=rcfg.timeout_s)
            if st.truncated:
                cut[0] += 1
            return st.rows

        lexicon = lexicon_arg(rc.lexicon)
        policy = SnapshotPolicy.from_guard(catalog.allowed, catalog.blocked_columns, catalog.blocked_fields,
                                           SENSITIVE_OBJECTS)
        vocab = build_vocabulary(knowledge_dir, query, lexicon=lexicon, columns=wh.columns(), policy=policy)
        vocab.info.setdefault("snapshot", {})["truncated"] = cut[0]
        problem = word_list_problem(vocab, lexicon)
        if problem:
            return [f"spelling help off: {problem}"]
        built = build(vocab)
    except Exception as exc:  # noqa: BLE001 - the spelling help is optional: records stay on without it
        first = (str(exc).splitlines() or [""])[0][:300]
        return [f"spelling help off: the record vocabulary could not be built: {type(exc).__name__}: {first}"]
    catalog.attach_resolver(built)
    return spelling_status(built, "knowledge files + snapshot", time.perf_counter() - t0)


def open_records(rcfg: RecordsConfig, knowledge_dir: str,
                 resolver: ResolverConfig | None = None) -> "RecordTools":  # noqa: F821
    """Open the snapshot and build catalog, value index and tools.

    ``resolver``: the spelling help settings (None: ``SFA_RESOLVER`` / ``SFA_LEXICON`` from the environment). When it
    is on, the full term resolver is built right after the catalog (:func:`attach_spelling_help`): then
    ``tools.catalog.resolver`` is set and ``tools.resolver_lines`` holds its start-up lines; off, both stay empty.

    Raises :class:`RecordsUnavailable` with a one-line reason (duckdb not installed, no snapshot,
    forbidden path, invalid snapshot, bad time zone); never anything else.
    """
    try:
        import duckdb  # noqa: F401
    except ImportError:
        raise RecordsUnavailable("duckdb is not installed in .venv (uv pip install --python .venv/bin/python "
                                 "--no-deps -r web/requirements.lock)") from None
    from sf_assistant.engine.catalog import Catalog
    from sf_assistant.engine.snapshot import read_meta
    from sf_assistant.engine.warehouse import Warehouse
    from sf_assistant.tools.record_tools import RecordTools

    wh = Warehouse.open(rcfg.warehouse, timeout_s=rcfg.timeout_s)
    try:
        if rcfg.tz:
            if not _valid_tz(rcfg.tz):
                raise RecordsUnavailable(f"unknown time zone '{rcfg.tz}' in OKEVAL_RECORDS_TZ")
            tz, tz_from = rcfg.tz, "OKEVAL_RECORDS_TZ"
        else:
            org_tz = wh.org().get("TimeZoneSidKey")
            if org_tz and _valid_tz(org_tz):
                tz, tz_from = str(org_tz), "Organization.TimeZoneSidKey"
            else:
                tz, tz_from = "UTC", "default UTC"
        catalog = Catalog.build(wh, knowledge_dir)
        spelling = attach_spelling_help(wh, catalog, rcfg, knowledge_dir, resolver)   # [] when the switch is off
        tools = RecordTools(wh, catalog, rcfg, tz, tz_from, read_meta(wh.path))
        tools.resolver_lines = spelling
        return tools
    except RecordsUnavailable:
        wh.close()
        raise
    except Exception as exc:
        wh.close()
        raise RecordsUnavailable(f"could not open the warehouse snapshot: {type(exc).__name__}: "
                                 f"{str(exc).splitlines()[0][:300] if str(exc) else ''}") from None
