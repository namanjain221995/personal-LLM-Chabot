"""Durable trace store for the knowledge pipeline.

Kept deliberately separate from the orchestrator's `query_traces` in
PostgreSQL. Those two describe different pipelines -- the old path routes
through brain packs, this one through extraction, lexicon and vectors -- and
mixing them would make "did the new architecture do better?" unanswerable,
which is the only question this store exists to settle.

SQLite rather than PostgreSQL: the knowledge service is one process reading a
read-only bundle, the volume is one row per question, and a file can be copied
to a laptop and queried without credentials. The schema mirrors
`query_trace_events` column for column so a later move to PostgreSQL is a copy,
not a redesign.

WHAT IS STORED, AND THE PRIVACY LINE. A question is the user's own words and
may name a person. Discovery needs it, evaluation needs it, and it is the one
field here that is not schema metadata -- everything else (API names, ranks,
scores) describes the org's structure, not anybody's records. `redact` lets a
deployment mask it; the default keeps it, because an evaluation harness reading
its own dataset has nothing to protect.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import weakref
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace (
    trace_id            TEXT PRIMARY KEY,
    request_id          TEXT NOT NULL,
    test_case_id        TEXT,
    question            TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    total_duration_ms   INTEGER,
    status              TEXT NOT NULL DEFAULT 'running',
    -- Which org this request ran against. Taken from the pipeline's runtime
    -- configuration, never inferred from a file path: two environments can
    -- share a warehouse and the path would then say the wrong thing.
    environment         TEXT,
    -- Identifies the persisted structure, so a later reader knows which
    -- columns to expect without guessing from what is populated.
    trace_schema_version INTEGER NOT NULL DEFAULT 1,

    -- Per-stage wall time, derived at finalisation from the STAGE_COMPLETED
    -- events. Derived, not timed again: a second set of timers would drift
    -- from the events and there would be no way to tell which was right.
    extraction_duration_ms   REAL,
    routing_duration_ms      REAL,
    discovery_duration_ms    REAL,
    linking_duration_ms      REAL,
    record_query_duration_ms REAL,
    answer_duration_ms       REAL,

    -- Step 1: what read the question, and what it said.
    extraction_model    TEXT,
    extraction_endpoint TEXT,
    extraction_mode     TEXT,
    extraction_ms       INTEGER,
    extraction_json     TEXT,

    -- Step 2: the route, and the flags that produced it.
    route               TEXT,

    -- Step 3: what the runtime schema catalog returned.
    schema_objects      TEXT NOT NULL DEFAULT '[]',
    schema_fields       TEXT NOT NULL DEFAULT '[]',
    schema_served_from  TEXT,

    -- Step 4: the grounded schema plan, and why it failed when it did.
    grounded_ok            INTEGER,
    grounded_primary_object TEXT,
    grounded_objects       TEXT NOT NULL DEFAULT '[]',
    grounded_json          TEXT,
    grounding_failures     TEXT NOT NULL DEFAULT '[]',
    -- How stage 4 decided: offline | llm_assisted, and whether the main model
    -- was actually called or retrieval settled it (the fast path). Kept as
    -- columns because "how often did we need the model?" is the question
    -- step 7's latency cost is justified by.
    linker_mode            TEXT,
    linker_decided_by      TEXT,
    linker_model           TEXT,
    linker_model_calls     INTEGER,
    linker_model_ms        REAL,

    -- Step 8: the semantic IR's shape, as columns so traces can be filtered
    -- and grouped in SQL without reading extraction_json. The full IR stays
    -- in extraction_json.
    ir_family              TEXT,
    ir_output_mode         TEXT,
    ir_capabilities        TEXT,
    ir_sources             TEXT,
    ir_retried             INTEGER,
    ir_follow_up           INTEGER,

    -- Step 5: the record query. The SQL text is built from catalog
    -- identifiers and is safe to store; the bound values are the user's own
    -- and are counted, never written.
    record_sql          TEXT,
    record_param_count  INTEGER,
    record_row_count    INTEGER,
    record_truncated    INTEGER,
    record_ms           REAL,
    record_error        TEXT,
    data_freshness      TEXT,

    -- Step 6: which model wrote the answer, and whether it stayed grounded.
    -- answer_model is recorded on every answer so an evaluation can attribute
    -- a hallucination to a model rather than to "the pipeline".
    answer_model        TEXT,
    answer_model_role   TEXT,
    answer_result_type  TEXT,
    answer_attempts     INTEGER,
    answer_regenerated  INTEGER,
    answer_fallback     INTEGER,
    answer_grounded     INTEGER,
    answer_ms           REAL,
    answer_text         TEXT,
    grounding_codes     TEXT NOT NULL DEFAULT '[]',
    grounding_violations TEXT NOT NULL DEFAULT '[]',

    -- What the pipeline concluded.
    resolved_objects    TEXT NOT NULL DEFAULT '[]',
    resolved_fields     TEXT NOT NULL DEFAULT '[]',
    clarifications      TEXT NOT NULL DEFAULT '[]',
    signals             TEXT NOT NULL DEFAULT '{}',

    -- Bundle identity, so a run can be reproduced against the same artifacts.
    versions            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trace_started ON trace(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_case ON trace(test_case_id)
    WHERE test_case_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trace_status ON trace(status);
CREATE INDEX IF NOT EXISTS idx_trace_route ON trace(route)
    WHERE route IS NOT NULL;

CREATE TABLE IF NOT EXISTS trace_event (
    id                INTEGER PRIMARY KEY,
    trace_id          TEXT NOT NULL REFERENCES trace(trace_id) ON DELETE CASCADE,
    sequence_number   INTEGER NOT NULL,
    stage             TEXT NOT NULL,
    status            TEXT NOT NULL,
    component         TEXT NOT NULL DEFAULT '',
    component_version TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    duration_ms       INTEGER,
    details           TEXT NOT NULL DEFAULT '{}',
    UNIQUE (trace_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_event_trace ON trace_event(trace_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_event_stage ON trace_event(stage, created_at DESC);
"""


# Columns added after the first stores were written. SQLite has no
# ADD COLUMN IF NOT EXISTS, so a store built before steps 4 and 5 existed is
# brought forward by difference rather than rebuilt -- traces are the evidence
# the new architecture is judged on and must outlive every schema change.
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("grounded_ok", "INTEGER"),
    ("grounded_primary_object", "TEXT"),
    ("grounded_objects", "TEXT NOT NULL DEFAULT '[]'"),
    ("grounded_json", "TEXT"),
    ("grounding_failures", "TEXT NOT NULL DEFAULT '[]'"),
    ("record_sql", "TEXT"),
    ("record_param_count", "INTEGER"),
    ("record_row_count", "INTEGER"),
    ("record_truncated", "INTEGER"),
    ("record_ms", "REAL"),
    ("record_error", "TEXT"),
    ("data_freshness", "TEXT"),
    ("answer_model", "TEXT"),
    ("answer_model_role", "TEXT"),
    ("answer_result_type", "TEXT"),
    ("answer_attempts", "INTEGER"),
    ("answer_regenerated", "INTEGER"),
    ("answer_fallback", "INTEGER"),
    ("answer_grounded", "INTEGER"),
    ("answer_ms", "REAL"),
    ("answer_text", "TEXT"),
    ("grounding_codes", "TEXT NOT NULL DEFAULT '[]'"),
    ("grounding_violations", "TEXT NOT NULL DEFAULT '[]'"),
    ("environment", "TEXT"),
    ("linker_mode", "TEXT"),
    ("linker_decided_by", "TEXT"),
    ("linker_model", "TEXT"),
    ("linker_model_calls", "INTEGER"),
    ("linker_model_ms", "REAL"),
    ("ir_family", "TEXT"),
    ("ir_output_mode", "TEXT"),
    ("ir_capabilities", "TEXT"),
    ("ir_sources", "TEXT"),
    ("ir_retried", "INTEGER"),
    ("ir_follow_up", "INTEGER"),
    ("trace_schema_version", "INTEGER NOT NULL DEFAULT 1"),
    ("extraction_duration_ms", "REAL"),
    ("routing_duration_ms", "REAL"),
    ("discovery_duration_ms", "REAL"),
    ("linking_duration_ms", "REAL"),
    ("record_query_duration_ms", "REAL"),
    ("answer_duration_ms", "REAL"),
)

# Pipeline stage -> the trace column its duration lands in. The stage names
# are `pipeline.dispatch.Stage` values, carried in every STAGE_COMPLETED
# event's details.
STAGE_DURATION_COLUMNS = {
    "DISCOVERY": "discovery_duration_ms",
    "SCHEMA_LINKING": "linking_duration_ms",
    "RECORD_QUERY": "record_query_duration_ms",
    "ANSWER": "answer_duration_ms",
}

TRACE_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    """Append-only trace sink. One row per question, one row per stage."""

    def __init__(self, path: str | Path, *,
                 redact: Callable[[str], str] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redact = redact or (lambda text: text)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[tuple[Any, sqlite3.Connection]] = []  # (weakref to owner thread, connection)
        connection = self._connect()
        # WAL: a reader (the evaluation runner, a psql-style inspection) must
        # not block the service writing its next trace, and vice versa.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
        self._migrate(connection)
        connection.commit()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        present = {row["name"] for row in
                   connection.execute("PRAGMA table_info(trace)")}
        for column, definition in MIGRATIONS:
            if column not in present:
                connection.execute(
                    f"ALTER TABLE trace ADD COLUMN {column} {definition}")
        if "ir_family" not in present:
            # Once, when the IR columns arrive: fill them for traces that
            # already stored an IR, so old and new traces filter alike.
            for row in connection.execute(
                    "SELECT trace_id, extraction_json FROM trace"
                    " WHERE extraction_json LIKE '%\"family\"%'").fetchall():
                try:
                    ir = json.loads(row["extraction_json"])
                except (TypeError, ValueError):
                    continue
                connection.execute(
                    "UPDATE trace SET ir_family=?, ir_output_mode=?, ir_capabilities=?,"
                    " ir_sources=?, ir_retried=?, ir_follow_up=? WHERE trace_id=?",
                    (ir.get("family"), ir.get("output_mode"),
                     json.dumps(ir.get("capabilities") or []),
                     json.dumps(ir.get("sources") or []),
                     1 if ir.get("retried") else 0, 1 if ir.get("follow_up") else 0,
                     row["trace_id"]))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            # One connection per thread, owned by that thread. When a new
            # thread connects, connections whose thread has ENDED are closed:
            # a pool that replaces its workers otherwise leaks one handle per
            # retired thread per component -- 4 per question, found live when
            # a 500-question run hit 'Too many open files' at question 145.
            alive = []
            for owner, held in self._connections:
                thread = owner()
                if thread is not None and thread.is_alive():
                    alive.append((owner, held))
                    continue
                try:
                    held.close()
                except Exception:                    # noqa: BLE001
                    pass
            alive.append((weakref.ref(threading.current_thread()), connection))
            self._connections = alive
        return connection

    @property
    def db(self) -> sqlite3.Connection:
        connection = getattr(self._local, "db", None)
        if connection is None:
            connection = self._connect()
            self._local.db = connection
        return connection

    def close(self) -> None:
        with self._lock:
            for _owner, connection in self._connections:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()

    # -- writing ----------------------------------------------------------
    def begin(self, question: str, *, test_case_id: str | None = None,
              request_id: str | None = None,
              environment: str | None = None,
              versions: dict[str, Any] | None = None) -> str:
        """Open a trace for one question. Returns its trace_id."""
        trace_id = f"kt_{uuid.uuid4().hex}"
        self.db.execute(
            "INSERT INTO trace (trace_id, request_id, test_case_id, question,"
            " started_at, environment, trace_schema_version, versions)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (trace_id, request_id or f"kr_{uuid.uuid4().hex[:16]}",
             test_case_id, self._redact(question), _now(), environment,
             TRACE_SCHEMA_VERSION,
             json.dumps(versions or {}, sort_keys=True)))
        return trace_id

    def record_extraction(self, trace_id: str, extraction: Any, *,
                          model: str, endpoint: str) -> None:
        """Step 1: the question, the model that read it, and its JSON answer.

        Written even when extraction FAILED -- a null extraction_json beside a
        recorded model and endpoint is the evidence that the model was asked
        and could not answer, which a missing row would hide.
        """
        payload = None
        mode = None
        duration = None
        if extraction is not None and hasattr(extraction, "family"):
            # The semantic IR (step 8): stored whole. It is the question's
            # meaning in business words -- no rows, no secrets, no reasoning.
            payload = json.dumps(extraction.as_dict(), sort_keys=True, default=str)
            mode = extraction.mode
            duration = extraction.duration_ms
        elif extraction is not None:
            payload = json.dumps({
                "request_type": extraction.request_type,
                "intent": extraction.intent,
                "action": extraction.action,
                "business_entities": extraction.business_entities,
                "filters": extraction.filters,
                "requested_attributes": extraction.requested_attributes,
                "metadata_types": extraction.metadata_types,
                "requires_record_query": extraction.requires_record_query,
                "requires_metadata_context": extraction.requires_metadata_context,
            }, sort_keys=True)
            mode = extraction.mode
            duration = extraction.duration_ms
        self.db.execute(
            "UPDATE trace SET extraction_model=?, extraction_endpoint=?,"
            " extraction_mode=?, extraction_ms=?, extraction_json=?"
            " WHERE trace_id=?",
            (model, endpoint, mode, duration, payload, trace_id))
        if extraction is not None and hasattr(extraction, "family"):
            self.db.execute(
                "UPDATE trace SET ir_family=?, ir_output_mode=?, ir_capabilities=?,"
                " ir_sources=?, ir_retried=?, ir_follow_up=? WHERE trace_id=?",
                (extraction.family, extraction.output_mode,
                 json.dumps(list(extraction.capabilities or [])),
                 json.dumps(list(extraction.sources or [])),
                 1 if extraction.retried else 0,
                 1 if extraction.follow_up else 0, trace_id))

    def record_route(self, trace_id: str, route: Any) -> None:
        """Step 2: the single path chosen."""
        self.db.execute("UPDATE trace SET route=? WHERE trace_id=?",
                        (getattr(route, "value", str(route)), trace_id))

    def record_schema(self, trace_id: str, *, objects: Iterable[str],
                      fields: Iterable[str], served_from: str | None = None) -> None:
        """Step 3: the components discovery ranked for this question."""
        self.db.execute(
            "UPDATE trace SET schema_objects=?, schema_fields=? WHERE trace_id=?",
            (json.dumps(list(objects)), json.dumps(list(fields)), trace_id))
        if served_from:
            self.record_schema_source(trace_id, served_from)

    def record_schema_source(self, trace_id: str,
                             served_from: str | None) -> None:
        """Which runtime-schema level answered: L1 memory or L2 SQLite.

        Written separately from the candidate lists because the two come from
        different components -- discovery ranks the candidates, the runtime
        schema cache serves the schema -- and folding them into one call made
        this column silently NULL on every request, because the discovery
        result has no cache level to report.
        """
        if not served_from:
            return
        self.db.execute(
            "UPDATE trace SET schema_served_from=? WHERE trace_id=?",
            (served_from, trace_id))

    def record_grounding(self, trace_id: str, plan: Any) -> None:
        """Step 4: the grounded schema plan, whether or not it grounded.

        A failed plan is written with `grounded_ok = 0` and its failure codes
        kept. Which concept could not be resolved is the most useful thing a
        trace carries, and a missing row would say only that nothing happened.
        """
        if plan is None:
            return
        payload = plan.as_dict() if hasattr(plan, "as_dict") else dict(plan)
        self.db.execute(
            "UPDATE trace SET grounded_ok=?, grounded_primary_object=?,"
            " grounded_objects=?, grounded_json=?, grounding_failures=?"
            " WHERE trace_id=?",
            (1 if payload.get("schema_grounded") else 0,
             payload.get("primary_object"),
             json.dumps(payload.get("objects") or []),
             json.dumps(payload, sort_keys=True, default=str),
             json.dumps(payload.get("failures") or [], default=str),
             trace_id))
        semantic = payload.get("semantic") or {}
        self.db.execute(
            "UPDATE trace SET linker_mode=?, linker_decided_by=?, linker_model=?,"
            " linker_model_calls=?, linker_model_ms=? WHERE trace_id=?",
            (semantic.get("linker_mode") or ("offline" if payload else None),
             semantic.get("mode"), semantic.get("model"),
             int(semantic.get("model_calls") or 0),
             float(semantic.get("duration_ms") or 0), trace_id))

    def record_records(self, trace_id: str, result: Any) -> None:
        """Step 5: what the record query executed and what came back.

        The SQL text is stored; the bound parameters are counted. Identifiers
        come from the physical catalog and describe the org's structure, while
        the values are the user's own and may name a person.
        """
        if result is None:
            return
        freshness = getattr(result, "freshness", None)
        error = getattr(result, "error", None)
        # The count comes off the SQL_GENERATED event because the result keeps
        # the statement text and deliberately drops the bindings.
        params = None
        for event in getattr(result, "trace", []) or []:
            data = event.as_dict() if hasattr(event, "as_dict") else dict(event)
            if data.get("stage") == "SQL_GENERATED":
                params = (data.get("details") or {}).get("param_count")
        if params is None:
            # The semantic-IR path: summed over its subplans.
            params = getattr(result, "param_count", None)
        self.db.execute(
            "UPDATE trace SET record_sql=?, record_param_count=?,"
            " record_row_count=?, record_truncated=?, record_ms=?,"
            " record_error=?, data_freshness=? WHERE trace_id=?",
            (getattr(result, "sql", "") or None,
             params,
             getattr(result, "row_count", None),
             1 if getattr(result, "truncated", False) else 0,
             round(float(getattr(result, "execution_ms", 0) or 0), 3),
             getattr(error, "value", error),
             json.dumps(freshness.as_dict(), default=str) if freshness else None,
             trace_id))

    def record_answer(self, trace_id: str, final: Any) -> None:
        """Step 6: the answer, the model that wrote it, and its grounding.

        The answer text is stored because an evaluation scores the words a
        user actually read. The grounding violations store reason codes and
        the offending value -- the number that was wrong, the name that was
        invented -- never the supported records, which are already in the
        result and are somebody's personal data.
        """
        if final is None:
            return
        grounding = getattr(final, "grounding", None)
        self.db.execute(
            "UPDATE trace SET answer_model=?, answer_model_role=?,"
            " answer_result_type=?, answer_attempts=?, answer_regenerated=?,"
            " answer_fallback=?, answer_grounded=?, answer_ms=?,"
            " answer_text=?, grounding_codes=?, grounding_violations=?"
            " WHERE trace_id=?",
            (getattr(final, "model", "") or None,
             getattr(final, "model_role", "") or None,
             getattr(final, "result_type", "") or None,
             int(getattr(final, "attempts", 0) or 0),
             1 if getattr(final, "regenerated", False) else 0,
             1 if getattr(final, "fallback_used", False) else 0,
             1 if getattr(final, "grounded", False) else 0,
             round(float(getattr(final, "duration_ms", 0) or 0), 3),
             self._redact(getattr(final, "text", "") or ""),
             json.dumps(grounding.codes() if grounding else []),
             json.dumps([v.as_dict() for v in grounding.violations]
                        if grounding else [], default=str),
             trace_id))

    def add_events(self, trace_id: str, events: Iterable[Any]) -> None:
        """Append stage records. Accepts TraceEvent objects or plain dicts."""
        row = self.db.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) FROM trace_event"
            " WHERE trace_id=?", (trace_id,)).fetchone()
        sequence = int(row[0])
        rows = []
        for event in events:
            data = event.as_dict() if hasattr(event, "as_dict") else dict(event)
            sequence += 1
            rows.append((trace_id, sequence, data.get("stage", ""),
                         data.get("status", "success"), data.get("component", ""),
                         data.get("component_version", ""), _now(),
                         data.get("duration_ms"),
                         json.dumps(data.get("details") or {}, sort_keys=True)))
        if rows:
            self.db.executemany(
                "INSERT OR IGNORE INTO trace_event (trace_id, sequence_number,"
                " stage, status, component, component_version, created_at,"
                " duration_ms, details) VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def _record_stage_durations(self, trace_id: str) -> None:
        """Per-stage wall time, read back off this trace's own events.

        Derived rather than timed a second time. A parallel set of timers
        would eventually disagree with the events, and nothing would say which
        one was the truth. Extraction has no STAGE_COMPLETED event -- it runs
        before dispatch -- so its duration comes from the column step 1 already
        wrote, and routing is a dict lookup with no measurable duration.
        """
        rows = self.db.execute(
            "SELECT details, duration_ms FROM trace_event"
            " WHERE trace_id=? AND stage='STAGE_COMPLETED'"
            " ORDER BY sequence_number", (trace_id,)).fetchall()
        totals: dict[str, float] = {}
        for row in rows:
            try:
                details = json.loads(row["details"] or "{}")
            except json.JSONDecodeError:
                continue
            column = STAGE_DURATION_COLUMNS.get(str(details.get("pipeline_stage")))
            if column is None or row["duration_ms"] is None:
                continue
            # A stage can run more than once in a route; sum rather than
            # overwrite, so a retry is visible as time rather than lost.
            totals[column] = totals.get(column, 0.0) + float(row["duration_ms"])
        extraction = self.db.execute(
            "SELECT extraction_ms FROM trace WHERE trace_id=?",
            (trace_id,)).fetchone()
        if extraction and extraction["extraction_ms"] is not None:
            totals["extraction_duration_ms"] = float(extraction["extraction_ms"])
        if not totals:
            return
        assignments = ", ".join(f"{column}=?" for column in totals)
        self.db.execute(f"UPDATE trace SET {assignments} WHERE trace_id=?",
                        (*totals.values(), trace_id))

    def finish(self, trace_id: str, *, status: str = "ok",
               discovery: Any = None, total_duration_ms: int | None = None,
               signals: dict[str, Any] | None = None,
               grounded_plan: Any = None, records: Any = None,
               answer: Any = None) -> None:
        if grounded_plan is not None:
            self.record_grounding(trace_id, grounded_plan)
        if records is not None:
            self.record_records(trace_id, records)
        if answer is not None:
            self.record_answer(trace_id, answer)
        objects: list[str] = []
        fields: list[str] = []
        clarifications: list[str] = []
        if discovery is not None:
            objects = [c.api_name for c in getattr(discovery, "objects", [])]
            fields = [c.component_id.split(":", 1)[-1]
                      for c in getattr(discovery, "fields", [])]
            clarifications = [r.surface
                              for r in getattr(discovery, "needs_clarification", [])]
        elif grounded_plan is not None:
            # Direct routes run no discovery. Without this the trace would say
            # the pipeline resolved nothing on exactly the questions it
            # answered fastest.
            objects = list(getattr(grounded_plan, "objects", []) or [])
            fields = [f"{m.object_api_name or ''}.{m.field}".lstrip(".")
                      for m in getattr(grounded_plan, "filter_mappings", []) or []]
            fields += [f"{m.target_object}.{m.target_field}" for m in
                       getattr(grounded_plan, "requested_attribute_mappings", []) or []]
        self.db.execute(
            "UPDATE trace SET status=?, completed_at=?, total_duration_ms=?,"
            " resolved_objects=?, resolved_fields=?, clarifications=?, signals=?"
            " WHERE trace_id=?",
            (status, _now(), total_duration_ms,
             json.dumps(objects), json.dumps(fields), json.dumps(clarifications),
             json.dumps(signals or {}, sort_keys=True), trace_id))
        # Last, so it reads every event this trace appended.
        self._record_stage_durations(trace_id)

    # -- reading ----------------------------------------------------------
    def get(self, trace_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM trace WHERE trace_id=?",
                              (trace_id,)).fetchone()
        if row is None:
            return None
        trace = dict(row)
        for key in ("resolved_objects", "resolved_fields", "clarifications",
                    "signals", "versions", "schema_objects", "schema_fields",
                    "grounded_objects", "grounding_failures",
                    "grounding_codes", "grounding_violations",
                    "ir_capabilities", "ir_sources"):
            trace[key] = json.loads(trace[key] or "null")
        for key in ("extraction_json", "grounded_json", "data_freshness"):
            if trace.get(key):
                trace[key] = json.loads(trace[key])
        trace["events"] = [
            {**dict(e), "details": json.loads(e["details"] or "{}")}
            for e in self.db.execute(
                "SELECT * FROM trace_event WHERE trace_id=? ORDER BY sequence_number",
                (trace_id,))]
        return trace

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT trace_id, test_case_id, question, status, total_duration_ms,"
            " extraction_model, extraction_mode, extraction_ms, route,"
            " schema_served_from, grounded_ok, grounded_primary_object,"
            " record_row_count, record_ms, record_error, answer_model,"
            " answer_grounded, answer_fallback, answer_ms, environment,"
            " linker_mode, linker_decided_by, linker_model_ms,"
            " ir_family, ir_output_mode, ir_capabilities,"
            " started_at"
            " FROM trace ORDER BY started_at DESC LIMIT ?", (limit,))]

    def stats(self) -> dict[str, Any]:
        """Enough to answer 'is the new pipeline working?' in one query."""
        row = self.db.execute(
            "SELECT count(*) n,"
            " sum(CASE WHEN extraction_json IS NOT NULL THEN 1 ELSE 0 END) extracted,"
            " sum(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,"
            " sum(CASE WHEN grounded_ok=1 THEN 1 ELSE 0 END) grounded,"
            " sum(CASE WHEN record_sql IS NOT NULL THEN 1 ELSE 0 END) queried,"
            " sum(CASE WHEN record_error IS NOT NULL THEN 1 ELSE 0 END) query_failed,"
            " sum(CASE WHEN answer_text IS NOT NULL THEN 1 ELSE 0 END) answered,"
            " sum(CASE WHEN answer_grounded=1 THEN 1 ELSE 0 END) answer_grounded,"
            " sum(CASE WHEN answer_regenerated=1 THEN 1 ELSE 0 END) regenerated,"
            " sum(CASE WHEN answer_fallback=1 THEN 1 ELSE 0 END) fell_back,"
            " round(avg(answer_ms), 2) avg_answer_ms,"
            " round(avg(total_duration_ms)) avg_ms,"
            " round(avg(record_ms), 2) avg_record_ms,"
            " round(avg(extraction_ms)) avg_extract_ms FROM trace").fetchone()
        by_stage = {r["stage"]: r["n"] for r in self.db.execute(
            "SELECT stage, count(*) n FROM trace_event GROUP BY stage ORDER BY n DESC")}
        by_mode = {r["extraction_mode"] or "none": r["n"] for r in self.db.execute(
            "SELECT extraction_mode, count(*) n FROM trace GROUP BY extraction_mode")}
        by_route = {r["route"] or "none": r["n"] for r in self.db.execute(
            "SELECT route, count(*) n FROM trace GROUP BY route ORDER BY n DESC")}
        by_level = {r["schema_served_from"] or "none": r["n"] for r in self.db.execute(
            "SELECT schema_served_from, count(*) n FROM trace"
            " GROUP BY schema_served_from")}
        by_error = {r["record_error"]: r["n"] for r in self.db.execute(
            "SELECT record_error, count(*) n FROM trace"
            " WHERE record_error IS NOT NULL GROUP BY record_error"
            " ORDER BY n DESC")}
        by_code: dict[str, int] = {}
        for entry in self.db.execute(
                "SELECT grounding_codes FROM trace WHERE grounding_codes != '[]'"):
            for code in json.loads(entry["grounding_codes"] or "[]"):
                by_code[code] = by_code.get(code, 0) + 1
        by_linker = {f"{r['linker_mode']}/{r['linker_decided_by']}": r["n"]
                     for r in self.db.execute(
            "SELECT linker_mode, linker_decided_by, count(*) n FROM trace"
            " WHERE linker_mode IS NOT NULL GROUP BY 1, 2")}
        by_family = {f"{r['ir_family']}/{r['ir_output_mode']}": r["n"]
                     for r in self.db.execute(
            "SELECT ir_family, ir_output_mode, count(*) n FROM trace"
            " WHERE ir_family IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC")}
        by_model = {r["answer_model"]: r["n"] for r in self.db.execute(
            "SELECT answer_model, count(*) n FROM trace"
            " WHERE answer_model IS NOT NULL GROUP BY answer_model")}
        answered = row["answered"] or 0
        return {"traces": row["n"], "with_extraction": row["extracted"],
                "ok": row["ok"], "grounded": row["grounded"],
                "with_record_query": row["queried"],
                "record_query_failed": row["query_failed"],
                "avg_total_ms": row["avg_ms"],
                "avg_extraction_ms": row["avg_extract_ms"],
                "avg_record_ms": row["avg_record_ms"],
                "events_by_stage": by_stage, "extraction_modes": by_mode,
                "routes": by_route, "schema_served_from": by_level,
                "record_errors": by_error,
                "ir_families": by_family,
                "answers": answered,
                "answers_grounded": row["answer_grounded"],
                "answers_regenerated": row["regenerated"],
                "answers_fell_back": row["fell_back"],
                "avg_answer_ms": row["avg_answer_ms"],
                # The headline number the spec asks for: how often the main
                # model grounded its answer without being told to try again.
                "first_pass_grounding_rate": (
                    round((answered - (row["regenerated"] or 0)
                           - (row["fell_back"] or 0)) / answered, 3)
                    if answered else None),
                "safety_fallback_rate": (
                    round((row["fell_back"] or 0) / answered, 3)
                    if answered else None),
                "grounding_failure_codes": by_code,
                "answer_models": by_model,
                # How often stage 4 needed the main model at all.
                "linker_decisions": by_linker}
