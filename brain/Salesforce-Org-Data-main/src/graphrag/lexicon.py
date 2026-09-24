"""Map the words people use to the components Salesforce actually stores.

The catalog knows every API name, label and picklist value, which is enough to
resolve "interview outcome" but not "placed", and not "candidate". Those are
private business facts: no parser and no embedding can recover them, because
they are not in the metadata at all. Someone has to write them down.

Four tiers, most trusted first:

  curated       hand-written in brain/lexicon/curated.yaml. The only tier that
                can carry an implied filter, and the only one a human signs.
  pack_trigger  the `triggers` lists in brain/packs/, each mapped to that
                pack's `tables`. Broad rather than precise: a trigger names a
                subject area, so it resolves to several objects at once.
  pack_glossary the `glossary` blocks — terms with prose meanings, often with
                no single component behind them.
  generated     derived from the catalog: API-name tokens, labels, plurals and
                picklist values.

A curated entry marked `needs_review` is loaded but never resolves. It sits in
the review queue instead. Encoding a guess would be worse than the gap it
fills, because a wrong alias is invisible at the point it does damage.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE entry (
    id            INTEGER PRIMARY KEY,
    surface       TEXT    NOT NULL,
    component_id  TEXT,
    target_kind   TEXT    NOT NULL,
    tier          TEXT    NOT NULL,
    confidence    REAL    NOT NULL,
    implied_filter TEXT,
    note          TEXT,
    source        TEXT    NOT NULL,
    needs_review  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_entry_surface ON entry(surface COLLATE NOCASE);
CREATE INDEX idx_entry_component ON entry(component_id);
CREATE INDEX idx_entry_tier ON entry(tier, needs_review);

-- What the resolver reads: usable entries only, best tier first. A
-- needs_review entry is deliberately absent.
CREATE VIEW resolvable AS
SELECT surface, component_id, target_kind, tier, confidence, implied_filter, source
FROM entry
WHERE needs_review = 0 AND component_id IS NOT NULL
ORDER BY confidence DESC;

CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# An object outranks a field on the same surface. "background check" names the
# entity far more often than it names one of the six foreign keys pointing at
# it, and without the split those foreign keys crowd the object out of the top
# results purely by alphabetical luck.
OBJECT_BOOST = 0.04

TIER_CONFIDENCE = {
    "curated": 1.0,
    "generated_api": 0.9,
    "generated_label": 0.9,
    "generated_plural": 0.85,
    "schema_label": 0.85,
    "generated_token": 0.5,
    "generated_picklist": 0.6,
    "pack_trigger": 0.4,
    "pack_glossary": 0.3,
}

_SPLIT = re.compile(r"[_\s]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SUFFIX = re.compile(r"__(c|mdt|e|b|x|kav)$")

# Tokens that match almost everything and so resolve nothing.
STOPWORDS = {
    "name", "id", "date", "status", "type", "value", "record", "field",
    "new", "old", "the", "and", "for", "all", "data", "info", "number",
    "count", "total", "active", "created", "modified", "last", "first",
}


class LexiconError(RuntimeError):
    """Raised when a required input is missing or malformed."""


@dataclass
class Entry:
    surface: str
    component_id: str | None
    target_kind: str
    tier: str
    confidence: float
    source: str
    implied_filter: str | None = None
    note: str | None = None
    needs_review: bool = False


@dataclass
class LexiconStats:
    by_tier: dict[str, int] = dataclass_field(default_factory=dict)
    total: int = 0
    resolvable: int = 0
    needs_review: int = 0
    distinct_surfaces: int = 0
    curated_terms: int = 0
    packs_read: int = 0
    schema_labels_applied: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "resolvable": self.resolvable,
            "needs_review": self.needs_review,
            "distinct_surfaces": self.distinct_surfaces,
            "curated_terms": self.curated_terms,
            "packs_read": self.packs_read,
            "schema_labels_applied": self.schema_labels_applied,
            "by_tier": dict(sorted(self.by_tier.items())),
        }


def _norm(text: str) -> str:
    return _SPLIT.sub(" ", str(text).strip().lower())


def _tokens(api_name: str) -> list[str]:
    bare = _SUFFIX.sub("", api_name)
    return [w.lower() for w in _SPLIT.split(_CAMEL.sub(" ", bare)) if w]


def _generated(catalog: sqlite3.Connection) -> Iterable[Entry]:
    """Everything derivable from the catalog itself."""
    rows = catalog.execute(
        "SELECT id, kind, api_name, qualified_name, label, plural_label, is_stub"
        " FROM component WHERE kind IN ('object','field','record_type','flow',"
        "'validation_rule','permission_set','profile','layout','flexipage')"
    ).fetchall()
    for row in rows:
        cid, kind, api_name, qualified, label, plural, is_stub = row
        boost = OBJECT_BOOST if kind == "object" else 0.0
        tokens = _tokens(api_name)
        if tokens:
            phrase = " ".join(tokens)
            yield Entry(phrase, cid, kind, "generated_api",
                        TIER_CONFIDENCE["generated_api"] + boost, "catalog:api_name")
            # Single tokens only help when they are not near-universal, and
            # never for a standard component the org never customised: a bare
            # word hitting a platform field is noise, not a match. "recipe"
            # reaching BatchJob.IsDebugRecipeDeleted is the whole category.
            for token in (() if is_stub else tokens):
                if len(token) > 3 and token not in STOPWORDS and token != phrase:
                    yield Entry(token, cid, kind, "generated_token",
                                TIER_CONFIDENCE["generated_token"] + boost,
                                "catalog:api_name token")
        if label:
            yield Entry(_norm(label), cid, kind, "generated_label",
                        TIER_CONFIDENCE["generated_label"] + boost, "catalog:label")
        if plural and plural != label:
            yield Entry(_norm(plural), cid, kind, "generated_plural",
                        TIER_CONFIDENCE["generated_plural"] + boost, "catalog:plural_label")

    for field_id, value in catalog.execute(
            "SELECT field_id, value FROM picklist_value WHERE is_active=1"):
        text = _norm(value)
        if len(text) > 2 and text not in STOPWORDS:
            yield Entry(text, field_id, "picklist_value", "generated_picklist",
                        TIER_CONFIDENCE["generated_picklist"], "catalog:picklist_value")


def _schema_labels(schema_path: Path, catalog: sqlite3.Connection) -> tuple[list[Entry], int]:
    """Object labels for standard objects a DX retrieve leaves bare.

    A retrieve returns only what the org customised, so Account, Lead and
    Opportunity arrive with no label at all. This fills those in from the
    exported org schema; it covers the business-relevant standard objects and
    leaves the platform ones unlabelled, which is honest — nobody asks about
    AIInsightReason by name.
    """
    if not schema_path.is_file():
        return [], 0
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise LexiconError(f"{schema_path}: {exc}") from exc
    unlabelled = {r[0] for r in catalog.execute(
        "SELECT api_name FROM component WHERE kind='object' AND label IS NULL")}
    out: list[Entry] = []
    applied = 0
    for obj in data.get("objects", []):
        api_name, label = obj.get("apiName"), obj.get("label")
        if not api_name or not label or api_name not in unlabelled:
            continue
        applied += 1
        out.append(Entry(_norm(label), f"object:{api_name}", "object", "schema_label",
                         TIER_CONFIDENCE["schema_label"], f"{schema_path.name}:label"))
        plural = obj.get("pluralLabel")
        if plural and plural != label:
            out.append(Entry(_norm(plural), f"object:{api_name}", "object", "schema_label",
                             TIER_CONFIDENCE["schema_label"], f"{schema_path.name}:pluralLabel"))
    return out, applied


def _packs(packs_dir: Path, known_objects: set[str]) -> tuple[list[Entry], int]:
    """Subject-area phrases from brain/packs/.

    A pack's `triggers` are the words that mean its subject, and its `tables`
    are the objects that subject touches. The mapping is therefore one phrase
    to several objects — useful for narrowing a search, too coarse to resolve
    an entity on its own, which is what the low confidence records.
    """
    if not packs_dir.is_dir():
        return [], 0
    try:
        import yaml  # optional: the core build has no dependencies
    except ImportError:
        return [], 0

    out: list[Entry] = []
    count = 0
    for path in sorted(packs_dir.glob("*.yaml")):
        try:
            pack = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(pack, dict):
            continue
        count += 1
        tables = [t for t in (pack.get("tables") or [])
                  if f"object:{t}" in known_objects]
        for trigger in (pack.get("triggers") or []):
            phrase = _norm(trigger)
            if not phrase or phrase in STOPWORDS:
                continue
            for table in tables:
                out.append(Entry(phrase, f"object:{table}", "object", "pack_trigger",
                                 TIER_CONFIDENCE["pack_trigger"], f"packs/{path.name}:triggers"))
        glossary = pack.get("glossary")
        if isinstance(glossary, dict):
            for term, meaning in glossary.items():
                phrase = _norm(term)
                if not phrase:
                    continue
                out.append(Entry(phrase, None, "concept", "pack_glossary",
                                 TIER_CONFIDENCE["pack_glossary"],
                                 f"packs/{path.name}:glossary",
                                 note=str(meaning)[:1000]))
    return out, count


def _curated(path: Path, known: set[str]) -> tuple[list[Entry], int]:
    if not path.is_file():
        return [], 0
    try:
        import yaml
    except ImportError as exc:
        raise LexiconError(
            "brain/lexicon/curated.yaml needs PyYAML; install it or move the file aside"
        ) from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise LexiconError(f"{path}: {exc}") from exc
    terms = data.get("terms") or {}
    out: list[Entry] = []
    for surface, spec in terms.items():
        if not isinstance(spec, dict):
            raise LexiconError(f"{path}: term {surface!r} must be a mapping")
        target = spec.get("target")
        if target and target not in known:
            raise LexiconError(
                f"{path}: term {surface!r} targets {target!r}, which is not in the catalog")
        out.append(Entry(
            surface=_norm(surface),
            component_id=target,
            target_kind=(target or "concept:").split(":", 1)[0],
            tier="curated",
            confidence=TIER_CONFIDENCE["curated"],
            source=str(spec.get("source") or f"{path.name}"),
            implied_filter=spec.get("filter"),
            note=spec.get("note"),
            needs_review=bool(spec.get("needs_review")),
        ))
    return out, len(terms)


def build_lexicon(catalog_path: str, output: str, packs_dir: str | None = None,
                  curated_path: str | None = None,
                  org_schema: str | None = None) -> LexiconStats:
    catalog_file = Path(catalog_path)
    if not catalog_file.is_file():
        raise LexiconError(f"catalog not found: {catalog_path}; run `graphrag catalog` first")
    catalog = sqlite3.connect(f"file:{catalog_file}?mode=ro", uri=True)
    try:
        known = {r[0] for r in catalog.execute("SELECT id FROM component")}
        known_objects = {i for i in known if i.startswith("object:")}

        entries: list[Entry] = list(_generated(catalog))
        stats = LexiconStats()

        if org_schema:
            schema_entries, applied = _schema_labels(Path(org_schema), catalog)
            entries += schema_entries
            stats.schema_labels_applied = applied
        if packs_dir:
            pack_entries, pack_count = _packs(Path(packs_dir), known_objects)
            entries += pack_entries
            stats.packs_read = pack_count
        if curated_path:
            curated_entries, term_count = _curated(Path(curated_path), known)
            entries += curated_entries
            stats.curated_terms = term_count
    finally:
        catalog.close()

    # Collapse duplicates: the same surface reaching the same component through
    # two tiers keeps the more trusted one.
    best: dict[tuple[str, str | None], Entry] = {}
    for entry in entries:
        key = (entry.surface, entry.component_id)
        current = best.get(key)
        if current is None or entry.confidence > current.confidence:
            best[key] = entry
    final = list(best.values())

    for entry in final:
        stats.by_tier[entry.tier] = stats.by_tier.get(entry.tier, 0) + 1
    stats.total = len(final)
    stats.needs_review = sum(1 for e in final if e.needs_review)
    stats.resolvable = sum(1 for e in final if not e.needs_review and e.component_id)
    stats.distinct_surfaces = len({e.surface for e in final})

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(target)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO entry (surface, component_id, target_kind, tier,"
            " confidence, implied_filter, note, source, needs_review)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(e.surface, e.component_id, e.target_kind, e.tier, e.confidence,
              e.implied_filter, e.note, e.source, int(e.needs_review)) for e in final])
        connection.executemany(
            "INSERT INTO manifest (key, value) VALUES (?,?)",
            [("built_at", datetime.now(timezone.utc).isoformat()),
             ("catalog_source", str(catalog_file.resolve())),
             ("lexicon_version", "1"),
             ("stats", json.dumps(stats.as_dict(), sort_keys=True))])
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return stats
