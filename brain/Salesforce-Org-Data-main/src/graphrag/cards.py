"""Render one fact card per component, deterministically, from catalog + graph.

Two renderings, because progressive discovery needs two shapes. The compact
card is what a discovery result carries: short enough to list twenty of them
in a prompt, and dense enough to pick the right one. The detail card is what a
describe call returns once a component has been chosen.

Nothing here is written by a model. Every line is formatted from a row, so a
card cannot state something the org does not contain, and a rebuild on an
unchanged mirror produces byte-identical text — which is what lets an approval
in the review stage be bound to a content hash.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE card (
    component_id TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    api_name     TEXT NOT NULL,
    compact      TEXT NOT NULL,
    detail       TEXT NOT NULL,
    keywords     TEXT NOT NULL,
    embeddable   INTEGER NOT NULL,
    redacted     INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX idx_card_kind ON card(kind, tier);
CREATE INDEX idx_card_embeddable ON card(embeddable);
CREATE INDEX idx_card_redacted ON card(redacted);

CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Custom metadata RECORDS hold configuration values, and three of this org's
# carry live credentials (an AWS key pair, flow token secrets). Their names and
# types are safe to describe; their values must never reach a card, and no card
# is worth the risk of a near miss, so the whole kind renders name-only.
VALUE_BEARING_KINDS = {"custom_metadata_record"}

# Defence in depth: even a name-only render is scanned before it is stored.
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
)

RELATIONSHIP_EDGE_KINDS = ("lookup_to", "master_detail_to")

# Edges worth counting on a card. `references` is tier 2 and excluded by the
# graph store already; these are the typed ones a reader would act on.
# (singular, plural) so a card reads "1 page layout", not "1 page layouts".
USAGE_EDGE_LABELS = {
    "field_permission": ("field-permission grant", "field-permission grants"),
    "object_permission": ("object-permission grant", "object-permission grants"),
    "displays_field": ("page layout", "page layouts"),
    "filters": ("report filter", "report filters"),
    "reads_field": ("flow reads it", "flows read it"),
    "writes_field": ("flow writes it", "flows write it"),
    "references_field": ("validation rule", "validation rules"),
    "uses_picklist_value": ("record type", "record types"),
    "groups_by_field": ("report grouping", "report groupings"),
    "targets": ("flow targets it", "flows target it"),
    "triggers_on": ("flow triggers on it", "flows trigger on it"),
    "creates": ("flow creates it", "flows create it"),
    "updates": ("flow updates it", "flows update it"),
    "deletes": ("flow deletes it", "flows delete it"),
}

# Edges that say nothing about a component. Every field has a `has_field` edge
# and almost every field appears in some permission grant, so their presence is
# not evidence that a card is worth embedding.
STRUCTURAL_EDGE_KINDS = {"has_field", "field_permission", "object_permission",
                         "system_permission", "defines", "references",
                         "stored_in", "contains"}

_SPLIT = re.compile(r"[_\s]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class CardError(RuntimeError):
    """Raised when the catalog or graph store cannot be read."""


@dataclass
class CardStats:
    total: int = 0
    embeddable: int = 0
    redacted: int = 0
    by_kind: dict[str, int] = dataclass_field(default_factory=dict)
    by_tier: dict[str, int] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "embeddable": self.embeddable,
            "not_embeddable": self.total - self.embeddable,
            "redacted": self.redacted,
            "by_kind": dict(sorted(self.by_kind.items())),
            "by_tier": dict(sorted(self.by_tier.items())),
        }


def _keywords(api_name: str, label: str | None, plural: str | None,
              parent_label: str | None = None) -> list[str]:
    """Derive lexical aliases from names alone.

    This is the generated half of the hybrid-retrieval signal: a user writing
    "interview outcome" should reach Interview_Outcome__c without anyone
    curating that mapping. Business aliases a name cannot imply — "placed",
    "candidate" meaning Account — belong to the lexicon stage, not here.
    """
    out: list[str] = []
    bare = re.sub(r"__(c|mdt|e|b|x|kav)$", "", api_name)
    words = [w for w in _SPLIT.split(_CAMEL.sub(" ", bare)) if w]
    if words:
        out.append(" ".join(words).lower())
        out.extend(w.lower() for w in words if len(w) > 2)
    for value in (label, plural, parent_label):
        if value:
            out.append(value.lower())
            out.extend(w.lower() for w in _SPLIT.split(value) if len(w) > 2)
    seen: dict[str, None] = {}
    for item in out:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


def _redact(text: str) -> tuple[str, bool]:
    hit = False
    for pattern in SECRET_PATTERNS:
        text, count = pattern.subn("[REDACTED]", text)
        hit = hit or bool(count)
    return text, hit


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


@dataclass
class Context:
    """Everything the renderers need, loaded once."""
    components: dict[str, sqlite3.Row]
    children: dict[str, list[str]]
    picklists: dict[str, list[tuple[str, str | None]]]
    inbound: dict[str, dict[str, int]]
    outbound: dict[str, dict[str, int]]
    inbound_names: dict[str, dict[str, list[str]]]
    node_status: dict[str, str]


def _load(catalog_path: Path, graph_path: Path) -> Context:
    if not catalog_path.is_file():
        raise CardError(f"catalog not found: {catalog_path}; run `graphrag catalog` first")
    if not graph_path.is_file():
        raise CardError(f"graph store not found: {graph_path}; run `graphrag graphdb` first")

    catalog = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    catalog.row_factory = sqlite3.Row
    components = {r["id"]: r for r in catalog.execute("SELECT * FROM component")}
    children: dict[str, list[str]] = defaultdict(list)
    for row in catalog.execute(
            "SELECT id, parent_id FROM component WHERE parent_id IS NOT NULL"):
        children[row["parent_id"]].append(row["id"])
    picklists: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for row in catalog.execute(
            "SELECT field_id, value, label FROM picklist_value"
            " WHERE is_active=1 ORDER BY field_id, value"):
        picklists[row["field_id"]].append((row["value"], row["label"]))
    catalog.close()

    graph = sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True)
    graph.row_factory = sqlite3.Row
    inbound: dict[str, dict[str, int]] = defaultdict(dict)
    outbound: dict[str, dict[str, int]] = defaultdict(dict)
    for row in graph.execute(
            "SELECT t.id target, e.kind, count(*) n FROM edge e"
            " JOIN node t ON t.nid=e.target_nid WHERE e.tier=1"
            " GROUP BY t.id, e.kind"):
        inbound[row["target"]][row["kind"]] = row["n"]
    for row in graph.execute(
            "SELECT s.id source, e.kind, count(*) n FROM edge e"
            " JOIN node s ON s.nid=e.source_nid WHERE e.tier=1"
            " GROUP BY s.id, e.kind"):
        outbound[row["source"]][row["kind"]] = row["n"]
    # Named endpoints for the small, high-value relationship edges.
    inbound_names: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    placeholders = ",".join("?" * len(RELATIONSHIP_EDGE_KINDS))
    for row in graph.execute(
            f"SELECT s.id source, t.id target, e.kind FROM edge e"
            f" JOIN node s ON s.nid=e.source_nid JOIN node t ON t.nid=e.target_nid"
            f" WHERE e.tier=1 AND e.kind IN ({placeholders})", RELATIONSHIP_EDGE_KINDS):
        inbound_names[row["target"]][row["kind"]].append(row["source"])
    node_status = {r["id"]: r["status"] for r in graph.execute(
        "SELECT id, status FROM node WHERE node_class='component'")}
    graph.close()

    return Context(components, children, picklists, inbound, outbound,
                   inbound_names, node_status)


def _usage_lines(ctx: Context, component_id: str) -> list[str]:
    counts = dict(ctx.inbound.get(component_id, {}))
    counts.update({k: v for k, v in ctx.outbound.get(component_id, {}).items()
                   if k in USAGE_EDGE_LABELS})
    lines = []
    for kind, (singular, plural) in USAGE_EDGE_LABELS.items():
        n = counts.get(kind)
        if n:
            lines.append(f"{n} {singular if n == 1 else plural}")
    return lines


def _render_field(ctx: Context, row: sqlite3.Row) -> tuple[str, str, list[str]]:
    props = json.loads(row["properties"])
    parent = ctx.components.get(row["parent_id"] or "")
    parent_label = parent["label"] if parent else None
    object_api = (row["parent_id"] or "object:?")[len("object:"):]
    field_type = props.get("type")
    values = ctx.picklists.get(row["id"], [])
    references = props.get("referenceTo") or []
    if isinstance(references, str):
        references = [references]

    title = f'{row["qualified_name"]}'
    if row["label"]:
        title += f' — "{row["label"]}"'

    # --- compact -------------------------------------------------------
    compact = [title]
    if field_type:
        line = f"{field_type} field on {object_api}"
        if references:
            line += f" referencing {', '.join(references)}"
        compact.append(line + ".")
    elif row["is_stub"]:
        compact.append(
            f"Standard Salesforce field on {object_api}. Not customized in this org.")
    else:
        compact.append(f"Field on {object_api}.")
    if row["description"]:
        compact.append(row["description"])
    if values:
        compact.append("Values: " + ", ".join(v for v, _ in values[:12])
                       + (" …" if len(values) > 12 else ""))

    # --- detail --------------------------------------------------------
    detail = [title, ""]
    detail.append(f"Field on object {object_api}"
                  + (f' ("{parent_label}")' if parent_label else "") + ".")
    if field_type:
        detail.append(f"Type: {field_type}")
    for key, caption in (("required", "Required"), ("unique", "Unique"),
                         ("externalId", "External id"), ("trackHistory", "History tracked"),
                         ("length", "Length"), ("precision", "Precision"),
                         ("scale", "Scale"), ("relationshipName", "Relationship name"),
                         ("summaryOperation", "Rollup operation"),
                         ("deleteConstraint", "Delete constraint")):
        if key in props:
            detail.append(f"{caption}: {props[key]}")
    if references:
        detail.append(f"References: {', '.join(references)}")
    if props.get("formula"):
        detail.append(f"Formula: {props['formula']}")
    if props.get("inlineHelpText"):
        detail.append(f"Help text: {props['inlineHelpText']}")
    if row["description"]:
        detail += ["", "Description:", row["description"]]
    if values:
        detail += ["", f"Picklist values ({len(values)}):"]
        detail += [f"  - {v}" + (f'  ("{l}")' if l and l != v else "")
                   for v, l in values]
    if row["is_stub"]:
        detail += ["", "This is a standard Salesforce field with no customization"
                       " in this org; it has no org-specific label or description."]
    usage = _usage_lines(ctx, row["id"])
    if usage:
        detail += ["", "Used by: " + ", ".join(usage) + "."]
    detail += ["", f"Source: {row['source_path']}"]

    keywords = _keywords(row["api_name"], row["label"], None, parent_label)
    keywords += [v.lower() for v, _ in values[:20]]
    return "\n".join(compact), "\n".join(detail), keywords


def _render_object(ctx: Context, row: sqlite3.Row) -> tuple[str, str, list[str]]:
    props = json.loads(row["properties"])
    child_ids = ctx.children.get(row["id"], [])
    by_kind: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for cid in child_ids:
        child = ctx.components.get(cid)
        if child:
            by_kind[child["kind"]].append(child)
    fields = by_kind.get("field", [])
    status = ctx.node_status.get(row["id"], "documented")

    title = row["api_name"]
    if row["label"]:
        title += f' — "{row["label"]}"'
        if row["plural_label"] and row["plural_label"] != row["label"]:
            title += f' (plural "{row["plural_label"]}")'

    lookups_in = ctx.inbound_names.get(row["id"], {})
    inbound_rel = sum(len(v) for v in lookups_in.values())

    # --- compact -------------------------------------------------------
    compact = [title]
    kind_word = "Custom object" if row["is_custom"] else "Standard Salesforce object"
    if row["is_stub"] and not row["is_custom"]:
        compact.append(f"{kind_word}, present in this org with no customization.")
    else:
        parts = [kind_word]
        if fields:
            parts.append(_plural(len(fields), "field"))
        if inbound_rel:
            parts.append(f"{inbound_rel} inbound relationships")
        compact.append(", ".join(parts) + ".")
    if row["description"]:
        compact.append(row["description"])
    rel_out = []
    for child in fields:
        cprops = json.loads(child["properties"])
        if cprops.get("type") in ("Lookup", "MasterDetail"):
            refs = cprops.get("referenceTo") or []
            refs = [refs] if isinstance(refs, str) else refs
            for ref in refs:
                rel_out.append(f"{child['api_name']} -> {ref}")
    if rel_out:
        compact.append("Relationships: " + ", ".join(rel_out[:8])
                       + (" …" if len(rel_out) > 8 else ""))

    # --- detail --------------------------------------------------------
    detail = [title, ""]
    detail.append(f"{kind_word}"
                  + (" (no customization in this org)" if row["is_stub"] and not row["is_custom"] else "")
                  + ".")
    for key, caption in (("sharingModel", "Sharing model"),
                         ("deploymentStatus", "Deployment status"),
                         ("enableReports", "Reports enabled"),
                         ("enableHistory", "History enabled"),
                         ("enableActivities", "Activities enabled"),
                         ("nameFieldLabel", "Name field"),
                         ("nameFieldType", "Name field type")):
        if key in props and props[key] is not None:
            detail.append(f"{caption}: {props[key]}")
    if row["description"]:
        detail += ["", "Description:", row["description"]]
    if status == "referenced_only":
        detail += ["", "This object appears in permission grants only. The org holds"
                       " no field, layout or automation metadata for it."]
    if fields:
        by_type: dict[str, int] = defaultdict(int)
        for child in fields:
            by_type[json.loads(child["properties"]).get("type") or "standard"] += 1
        detail += ["", f"Fields ({len(fields)}): "
                       + ", ".join(f"{n} {t}" for t, n in sorted(
                           by_type.items(), key=lambda kv: -kv[1]))]
    if rel_out:
        detail += ["", f"Outbound relationships ({len(rel_out)}):"]
        detail += [f"  - {r}" for r in rel_out]
    for kind, sources in sorted(lookups_in.items()):
        detail += ["", f"Inbound {kind} ({len(sources)}):"]
        detail += [f"  - {s[len('field:'):]}" for s in sorted(sources)]
    for kind, caption in (("record_type", "Record types"),
                          ("validation_rule", "Validation rules"),
                          ("compact_layout", "Compact layouts"),
                          ("business_process", "Business processes"),
                          ("list_view", "List views"),
                          ("web_link", "Buttons and links")):
        items = by_kind.get(kind, [])
        if items:
            names = sorted(c["api_name"] for c in items)
            detail += ["", f"{caption} ({len(names)}): " + ", ".join(names[:25])
                       + (" …" if len(names) > 25 else "")]
    usage = _usage_lines(ctx, row["id"])
    if usage:
        detail += ["", "Used by: " + ", ".join(usage) + "."]
    detail += ["", f"Source: {row['source_path']}"]

    keywords = _keywords(row["api_name"], row["label"], row["plural_label"])
    return "\n".join(compact), "\n".join(detail), keywords


KIND_CAPTIONS = {
    "flow": "Flow",
    "validation_rule": "Validation rule",
    "record_type": "Record type",
    "layout": "Page layout",
    "flexipage": "Lightning record page",
    "permission_set": "Permission set",
    "permission_set_group": "Permission set group",
    "profile": "Profile",
    "apex_class": "Apex class",
    "apex_trigger": "Apex trigger",
    "report": "Report",
    "dashboard": "Dashboard",
    "quick_action": "Quick action",
    "compact_layout": "Compact layout",
    "list_view": "List view",
    "global_value_set": "Global value set",
    "sharing_rule": "Sharing rule",
    "custom_tab": "Custom tab",
    "email_template": "Email template",
    "lwc_bundle": "Lightning web component",
    "aura_bundle": "Aura component",
    "static_resource": "Static resource",
    "custom_metadata_record": "Custom metadata record",
    "duplicate_rule": "Duplicate rule",
    "named_credential": "Named credential",
    "app": "Lightning app",
    "role": "Role",
    "queue": "Queue",
    "group": "Public group",
    "web_link": "Button or link",
    "business_process": "Business process",
    "workflow": "Workflow rule set",
    "approval_process": "Approval process",
    "visualforce_page": "Visualforce page",
    "custom_permission": "Custom permission",
}


def _render_generic(ctx: Context, row: sqlite3.Row) -> tuple[str, str, list[str]]:
    props = json.loads(row["properties"])
    caption = KIND_CAPTIONS.get(row["kind"], row["kind"].replace("_", " ").capitalize())
    parent = ctx.components.get(row["parent_id"] or "")
    name_only = row["kind"] in VALUE_BEARING_KINDS

    title = row["qualified_name"]
    if row["label"] and row["label"] != row["api_name"]:
        title += f' — "{row["label"]}"'

    compact = [title]
    line = caption
    if parent:
        line += f" on {parent['api_name']}"
    compact.append(line + ".")
    if row["description"] and not name_only:
        compact.append(row["description"])

    detail = [title, "", caption + (f" on object {parent['api_name']}." if parent else ".")]
    if name_only:
        detail += ["", "Configuration values are not included in this card."]
    else:
        for key, cap in (("active", "Active"), ("status", "Status"),
                         ("processType", "Process type"), ("type", "Type"),
                         ("errorMessage", "Error message"),
                         ("errorDisplayField", "Error display field"),
                         ("errorConditionFormula", "Condition formula")):
            if key in props and props[key] is not None:
                detail.append(f"{cap}: {props[key]}")
        if row["description"]:
            detail += ["", "Description:", row["description"]]
    usage = _usage_lines(ctx, row["id"])
    if usage:
        detail += ["", "Used by: " + ", ".join(usage) + "."]
    detail += ["", f"Source: {row['source_path']}"]

    keywords = [] if name_only else _keywords(
        row["api_name"], row["label"], None, parent["label"] if parent else None)
    return "\n".join(compact), "\n".join(detail), keywords


def _embeddable(row: sqlite3.Row, ctx: Context, keywords: list[str]) -> bool:
    """Would embedding this card help, or only crowd the index?

    A card with nothing distinguishing — a standard field the org never touched
    — matches every query weakly and pushes the useful card out of the top-K.
    Those stay reachable by exact lookup through the catalog, which is the right
    tool for "does Account have AccountNumber" anyway.
    """
    if row["kind"] in VALUE_BEARING_KINDS:
        return False
    if row["description"] or row["label"]:
        return True
    if ctx.picklists.get(row["id"]):
        return True
    edges = set(ctx.inbound.get(row["id"], {})) | set(ctx.outbound.get(row["id"], {}))
    return bool(edges - STRUCTURAL_EDGE_KINDS)


def build_cards(catalog_path: str, graph_path: str, output: str) -> CardStats:
    ctx = _load(Path(catalog_path), Path(graph_path))
    stats = CardStats()
    rows: list[tuple] = []

    for component_id, row in ctx.components.items():
        if row["kind"] == "field":
            compact, detail, keywords = _render_field(ctx, row)
        elif row["kind"] == "object":
            compact, detail, keywords = _render_object(ctx, row)
        else:
            compact, detail, keywords = _render_generic(ctx, row)

        compact, hit_a = _redact(compact)
        detail, hit_b = _redact(detail)
        redacted = hit_a or hit_b
        embeddable = _embeddable(row, ctx, keywords)

        stats.total += 1
        stats.by_kind[row["kind"]] = stats.by_kind.get(row["kind"], 0) + 1
        tier_key = f"tier{row['tier']}"
        stats.by_tier[tier_key] = stats.by_tier.get(tier_key, 0) + 1
        stats.embeddable += int(embeddable)
        stats.redacted += int(redacted)

        rows.append((
            component_id, row["kind"], row["tier"], row["api_name"],
            compact, detail, json.dumps(keywords),
            int(embeddable), int(redacted),
            sha256((compact + "\n\n" + detail).encode("utf-8")).hexdigest(),
        ))

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(target)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO card (component_id, kind, tier, api_name, compact,"
            " detail, keywords, embeddable, redacted, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        connection.executemany(
            "INSERT INTO manifest (key, value) VALUES (?,?)",
            [("built_at", datetime.now(timezone.utc).isoformat()),
             ("catalog_source", str(Path(catalog_path).resolve())),
             ("graph_source", str(Path(graph_path).resolve())),
             ("cards_version", "1"),
             ("stats", json.dumps(stats.as_dict(), sort_keys=True))])
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return stats
