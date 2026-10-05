"""Schema and connection handling for the runtime schema store.

One SQLite file per environment rather than a discriminator column: a dev
refresh can then never half-overwrite preprod, and a bad refresh is undone by
deleting one file.

A NULL in this database means "the source could not know", never "false". The
metadata mirror cannot supply 15 of these columns -- key_prefix, the CRUD and
queryable/filterable/sortable flags, record_type_id -- because Salesforce
computes them per org and per user and never puts them in a metadata retrieve.
Defaulting those to 0 would tell a query planner a field is NOT filterable,
which is a different and worse claim than saying nothing. Every row therefore
carries `source`, so a later live-describe refresh can fill the gaps and say so.
"""
from __future__ import annotations

import sqlite3
from typing import Any
import threading
import weakref
from pathlib import Path

SCHEMA_VERSION = "1.0"

TABLES = """
CREATE TABLE IF NOT EXISTS org_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT,
    org_alias TEXT,
    environment TEXT,
    instance_url TEXT,
    api_version TEXT,
    organization_type TEXT,
    created_at TEXT,
    updated_at TEXT,
    schema_version TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_name TEXT NOT NULL UNIQUE,
    label TEXT,
    plural_label TEXT,
    description TEXT,
    key_prefix TEXT,
    is_custom INTEGER DEFAULT 0,
    -- The next seven are describe-only. NULL, not 0.
    is_queryable INTEGER,
    is_searchable INTEGER,
    is_retrieveable INTEGER,
    is_createable INTEGER,
    is_updateable INTEGER,
    is_deletable INTEGER,
    is_triggerable INTEGER,
    is_deprecated INTEGER,
    record_type_supported INTEGER DEFAULT 0,
    field_count INTEGER DEFAULT 0,
    relationship_count INTEGER DEFAULT 0,
    is_hot_object INTEGER DEFAULT 0,
    hot_source TEXT CHECK (hot_source IN ('manual', 'usage') OR hot_source IS NULL),
    source TEXT NOT NULL DEFAULT 'mirror',
    last_refreshed_at TEXT,
    raw_hash TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_api_name TEXT NOT NULL,
    api_name TEXT NOT NULL,
    label TEXT,
    description TEXT,
    inline_help_text TEXT,
    data_type TEXT,
    length INTEGER,
    precision_value INTEGER,
    scale_value INTEGER,
    is_nullable INTEGER DEFAULT 1,
    is_unique INTEGER DEFAULT 0,
    is_external_id INTEGER DEFAULT 0,
    is_auto_number INTEGER DEFAULT 0,
    is_calculated INTEGER DEFAULT 0,
    calculated_formula TEXT,
    -- describe-only: NULL from the mirror.
    is_createable INTEGER,
    is_updateable INTEGER,
    is_filterable INTEGER,
    is_sortable INTEGER,
    is_groupable INTEGER,
    is_name_field INTEGER DEFAULT 0,
    relationship_name TEXT,
    reference_to TEXT,              -- JSON array: a lookup may be polymorphic
    default_value TEXT,
    defaulted_on_create INTEGER DEFAULT 0,
    restricted_picklist INTEGER DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'mirror',
    last_refreshed_at TEXT,
    raw_hash TEXT,
    UNIQUE (object_api_name, api_name)
);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_object TEXT NOT NULL,
    source_field TEXT NOT NULL,
    relationship_name TEXT,
    target_object TEXT NOT NULL,
    relationship_type TEXT,
    is_polymorphic INTEGER DEFAULT 0,
    cascade_delete INTEGER DEFAULT 0,
    last_refreshed_at TEXT,
    UNIQUE (source_object, source_field, target_object)
);

CREATE TABLE IF NOT EXISTS child_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_object TEXT NOT NULL,
    child_object TEXT NOT NULL,
    child_field TEXT,
    relationship_name TEXT,
    cascade_delete INTEGER DEFAULT 0,
    last_refreshed_at TEXT,
    UNIQUE (parent_object, child_object, child_field)
);

CREATE TABLE IF NOT EXISTS picklist_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_api_name TEXT NOT NULL,
    field_api_name TEXT NOT NULL,
    label TEXT,
    value TEXT,
    is_active INTEGER DEFAULT 1,
    is_default INTEGER DEFAULT 0,
    valid_for TEXT,
    sort_order INTEGER,
    last_refreshed_at TEXT,
    UNIQUE (object_api_name, field_api_name, value)
);

CREATE TABLE IF NOT EXISTS record_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_api_name TEXT NOT NULL,
    record_type_id TEXT,            -- runtime id; NULL from the mirror
    developer_name TEXT,
    name TEXT,
    description TEXT,
    is_active INTEGER DEFAULT 1,
    is_default INTEGER DEFAULT 0,
    last_refreshed_at TEXT,
    UNIQUE (object_api_name, developer_name)
);

CREATE TABLE IF NOT EXISTS object_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_api_name TEXT NOT NULL,
    alias TEXT NOT NULL,
    source TEXT,
    confidence REAL DEFAULT 1.0,
    is_manual INTEGER DEFAULT 0,
    UNIQUE (object_api_name, alias)
);

CREATE TABLE IF NOT EXISTS field_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_api_name TEXT NOT NULL,
    field_api_name TEXT NOT NULL,
    alias TEXT NOT NULL,
    source TEXT,
    confidence REAL DEFAULT 1.0,
    is_manual INTEGER DEFAULT 0,
    UNIQUE (object_api_name, field_api_name, alias)
);

CREATE TABLE IF NOT EXISTS object_usage_stats (
    object_api_name TEXT PRIMARY KEY,
    access_count INTEGER DEFAULT 0,
    access_count_window INTEGER DEFAULT 0,
    window_started_at TEXT,
    last_accessed_at TEXT,
    is_pinned INTEGER DEFAULT 0,
    is_auto_hot INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refresh_id TEXT NOT NULL UNIQUE,
    started_at TEXT,
    completed_at TEXT,
    environment TEXT,
    source TEXT,
    status TEXT,
    objects_discovered INTEGER DEFAULT 0,
    objects_updated INTEGER DEFAULT 0,
    fields_updated INTEGER DEFAULT 0,
    relationships_updated INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    error_summary TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_fields_object ON fields(object_api_name);
CREATE INDEX IF NOT EXISTS idx_fields_api_name ON fields(api_name);
CREATE INDEX IF NOT EXISTS idx_relationship_source ON relationships(source_object);
CREATE INDEX IF NOT EXISTS idx_relationship_target ON relationships(target_object);
CREATE INDEX IF NOT EXISTS idx_child_rel_parent ON child_relationships(parent_object);
CREATE INDEX IF NOT EXISTS idx_picklist_field ON picklist_values(object_api_name, field_api_name);
CREATE INDEX IF NOT EXISTS idx_record_type_object ON record_types(object_api_name);
CREATE INDEX IF NOT EXISTS idx_object_alias ON object_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_field_alias ON field_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_objects_hot ON objects(is_hot_object);
"""

# Contentless-external FTS: the tables carry their own copies because the
# search text is a BUILT document (name plus label plus aliases plus picklist
# values), not a column that exists anywhere to point back at.
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS object_search USING fts5(
    api_name, label, description, aliases, related_objects, search_text
);
CREATE VIRTUAL TABLE IF NOT EXISTS field_search USING fts5(
    object_api_name, api_name, label, description, aliases,
    data_type, picklist_values, search_text
);
"""


class Database:
    """Connection handling. Every statement against the store goes through here."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[tuple[Any, sqlite3.Connection]] = []  # (weakref to owner thread, connection)
        self.initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        # WAL so a reader -- a validation script, an export -- never blocks the
        # refresh writing, and vice versa.
        connection.execute("PRAGMA journal_mode=WAL")
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
    def connection(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._connect()
            self._local.connection = existing
        return existing

    def initialise(self) -> None:
        connection = self.connection
        connection.executescript(TABLES)
        connection.executescript(INDEXES)
        connection.executescript(FTS)

    def close(self) -> None:
        with self._lock:
            for _owner, connection in self._connections:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
