"""Load graph.jsonl into an indexed SQLite store, reconciled against the catalog.

`graph.jsonl` is correct about relationships and unreliable about identity. It
overloads `kind` across file nodes and component nodes, so a count by kind
double-counts; its object nodes include entities that appear only in permission
grants; and generic XML extraction invents field ids out of formula text.

None of that is fixed by reparsing, because the extraction is sound — only the
labelling is wrong. So this stage re-labels on load:

  * every node is classified component / file / reference
  * object and field nodes are reconciled against the catalog, which reads the
    mirror directly and is therefore the authority on what exists
  * `references`, the untyped bulk of the edge set, is kept but demoted so it
    never pollutes a default traversal

The result answers "what is related to what" without ever being asked "what
exists" — that question belongs to the catalog.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
-- Node ids are interned. A text id repeated across two edge columns and two
-- indexes costs more than the evidence does; integers make the edge indexes
-- roughly a third of the size and the recursive traversal faster.
CREATE TABLE node (
    nid        INTEGER PRIMARY KEY,
    id         TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    node_class TEXT NOT NULL,
    name       TEXT NOT NULL,
    path       TEXT,
    catalog_id TEXT,
    status     TEXT NOT NULL,
    properties TEXT NOT NULL
);
CREATE INDEX idx_node_kind ON node(kind, node_class);
CREATE INDEX idx_node_name ON node(name COLLATE NOCASE);
CREATE INDEX idx_node_status ON node(status);
CREATE INDEX idx_node_catalog ON node(catalog_id);

CREATE TABLE edge (
    id         INTEGER PRIMARY KEY,
    source_nid INTEGER NOT NULL REFERENCES node(nid),
    target_nid INTEGER NOT NULL REFERENCES node(nid),
    kind       TEXT NOT NULL,
    tier       INTEGER NOT NULL,
    evidence   TEXT
);
CREATE INDEX idx_edge_source ON edge(source_nid, tier, kind);
CREATE INDEX idx_edge_target ON edge(target_nid, tier, kind);
CREATE INDEX idx_edge_kind ON edge(kind, tier);

-- Text-id view, so callers can query without joining by hand.
CREATE VIEW edge_ids AS
SELECT e.id, s.id AS source, t.id AS target, e.kind, e.tier, e.evidence,
       s.kind AS source_kind, t.kind AS target_kind,
       s.status AS source_status, t.status AS target_status
FROM edge e JOIN node s ON s.nid = e.source_nid
            JOIN node t ON t.nid = e.target_nid;

CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# Untyped bulk extraction. Kept for provenance, demoted out of default traversal.
TIER2_EDGE_KINDS = {"references", "defines"}

# Node kinds that are file classifications, not components. The scanner names
# them after the directory it found them in, which is why `custom_object` counts
# every file under objects/ including fields and list views.
FILE_NODE_KINDS = {"custom_object", "salesforce_metadata"}

REFERENCE_NODE_KINDS = {"xml_reference"}


class GraphDbError(RuntimeError):
    """Raised when the graph artifact or the catalog cannot be read."""


@dataclass
class Reconciliation:
    node_total: int = 0
    edge_total: int = 0
    components: int = 0
    files: int = 0
    references: int = 0
    objects_documented: int = 0
    objects_referenced_only: int = 0
    fields_documented: int = 0
    fields_malformed: int = 0
    edges_tier1: int = 0
    edges_tier2: int = 0
    edges_dropped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items()}


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise GraphDbError(f"{path}:{number}: {exc}") from exc


def _load_catalog(path: Path) -> tuple[set[str], set[str]]:
    """Return the catalog's object ids and field ids."""
    if not path.is_file():
        raise GraphDbError(f"catalog not found: {path}; run `graphrag catalog` first")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        objects = {r[0] for r in connection.execute(
            "SELECT id FROM component WHERE kind='object'")}
        fields = {r[0] for r in connection.execute(
            "SELECT id FROM component WHERE kind='field'")}
    finally:
        connection.close()
    if not objects:
        raise GraphDbError(f"catalog at {path} contains no objects")
    return objects, fields


def _classify(node: dict[str, Any]) -> str:
    kind = node.get("kind", "")
    if kind in REFERENCE_NODE_KINDS:
        return "reference"
    if kind in FILE_NODE_KINDS or str(node.get("id", "")).startswith("file:"):
        return "file"
    return "component"


def build_graphdb(graph_path: str, catalog_path: str, output: str) -> Reconciliation:
    """Load the graph, reconcile it against the catalog, write an indexed store."""
    source = Path(graph_path)
    if not source.is_file():
        raise GraphDbError(f"graph artifact not found: {graph_path}")
    catalog_objects, catalog_fields = _load_catalog(Path(catalog_path))

    stats = Reconciliation()
    nodes: list[tuple] = []
    nid_of: dict[str, int] = {}

    for record in _read_jsonl(source):
        if record.get("type") != "node":
            continue
        stats.node_total += 1
        node_id = record.get("id", "")
        node_class = _classify(record)
        status = "ok"
        catalog_id = None

        if node_id.startswith("object:"):
            if node_id in catalog_objects:
                status, catalog_id = "documented", node_id
                stats.objects_documented += 1
            else:
                # Real Salesforce entities, but the org never customised them;
                # they reach the graph only through permission grants. Keeping
                # them preserves those grants, labelling them stops a count.
                status = "referenced_only"
                stats.objects_referenced_only += 1
        elif node_id.startswith("field:"):
            if node_id in catalog_fields:
                status, catalog_id = "documented", node_id
                stats.fields_documented += 1
            else:
                # Parsed out of formula or SOQL text: ACCOUNT.NAME,
                # Account.Activities$Subject, or a bare field with no object.
                status = "malformed"
                stats.fields_malformed += 1

        if node_class == "component":
            stats.components += 1
        elif node_class == "file":
            stats.files += 1
        else:
            stats.references += 1

        nid_of[node_id] = len(nid_of) + 1
        nodes.append((
            nid_of[node_id],
            node_id,
            record.get("kind", ""),
            node_class,
            record.get("name", ""),
            record.get("path"),
            catalog_id,
            status,
            json.dumps(record.get("properties") or {}, sort_keys=True),
        ))

    edges: list[tuple] = []
    for record in _read_jsonl(source):
        if record.get("type") != "edge":
            continue
        stats.edge_total += 1
        source_nid = nid_of.get(record.get("source", ""))
        target_nid = nid_of.get(record.get("target", ""))
        if source_nid is None or target_nid is None:
            stats.edges_dropped += 1
            continue
        tier = 2 if record.get("kind") in TIER2_EDGE_KINDS else 1
        if tier == 1:
            stats.edges_tier1 += 1
        else:
            stats.edges_tier2 += 1
        # Evidence is kept only for typed edges. A tier-2 `references` edge
        # cites a generic XML mention, which no answer would ever quote.
        evidence = record.get("evidence") or []
        edges.append((
            source_nid, target_nid, record.get("kind", ""), tier,
            json.dumps(evidence[0], sort_keys=True) if evidence and tier == 1 else None,
        ))

    target_path = Path(output)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    connection = sqlite3.connect(target_path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO node (nid, id, kind, node_class, name, path,"
            " catalog_id, status, properties) VALUES (?,?,?,?,?,?,?,?,?)", nodes)
        connection.executemany(
            "INSERT INTO edge (source_nid, target_nid, kind, tier, evidence)"
            " VALUES (?,?,?,?,?)", edges)
        connection.executemany(
            "INSERT INTO manifest (key, value) VALUES (?,?)",
            [
                ("built_at", datetime.now(timezone.utc).isoformat()),
                ("graph_source", str(source.resolve())),
                ("catalog_source", str(Path(catalog_path).resolve())),
                ("graphdb_version", "1"),
                *[(k, str(v)) for k, v in stats.as_dict().items()],
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return stats
