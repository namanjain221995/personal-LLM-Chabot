"""Every read and write against the runtime schema store.

The one place that issues SQL. Scattering queries across the codebase is how a
column rename becomes a week of grep, and how two call sites quietly disagree
about what "hot" means.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .database import Database
from .models import (Alias, ChildRelationship, PicklistValue, RecordType,
                     Relationship, SchemaBundle, SField, SObject)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool(value: bool | None) -> int | None:
    """None survives as None. A describe-only column must not become 0."""
    return None if value is None else int(bool(value))


class SchemaRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    @property
    def _c(self):
        return self.db.connection

    # -- writes -----------------------------------------------------------
    def upsert_objects(self, objects: Sequence[SObject], *, source: str) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO objects (api_name, label, plural_label, description,
                   key_prefix, is_custom, is_queryable, is_searchable,
                   is_retrieveable, is_createable, is_updateable, is_deletable,
                   is_triggerable, is_deprecated, record_type_supported,
                   field_count, relationship_count, source, last_refreshed_at,
                   raw_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(api_name) DO UPDATE SET
                   label=excluded.label, plural_label=excluded.plural_label,
                   description=excluded.description,
                   key_prefix=COALESCE(excluded.key_prefix, objects.key_prefix),
                   is_custom=excluded.is_custom,
                   is_queryable=COALESCE(excluded.is_queryable, objects.is_queryable),
                   is_searchable=COALESCE(excluded.is_searchable, objects.is_searchable),
                   is_retrieveable=COALESCE(excluded.is_retrieveable, objects.is_retrieveable),
                   is_createable=COALESCE(excluded.is_createable, objects.is_createable),
                   is_updateable=COALESCE(excluded.is_updateable, objects.is_updateable),
                   is_deletable=COALESCE(excluded.is_deletable, objects.is_deletable),
                   is_triggerable=COALESCE(excluded.is_triggerable, objects.is_triggerable),
                   is_deprecated=COALESCE(excluded.is_deprecated, objects.is_deprecated),
                   record_type_supported=excluded.record_type_supported,
                   field_count=excluded.field_count,
                   relationship_count=excluded.relationship_count,
                   source=excluded.source,
                   last_refreshed_at=excluded.last_refreshed_at,
                   raw_hash=excluded.raw_hash""",
            # COALESCE on every describe-only column: a mirror refresh passes
            # None for these, and must not erase values a live describe found.
            [(o.api_name, o.label, o.plural_label, o.description, o.key_prefix,
              int(o.is_custom), _bool(o.is_queryable), _bool(o.is_searchable),
              _bool(o.is_retrieveable), _bool(o.is_createable),
              _bool(o.is_updateable), _bool(o.is_deletable),
              _bool(o.is_triggerable), _bool(o.is_deprecated),
              int(o.record_type_supported), o.field_count, o.relationship_count,
              source, stamp, o.raw_hash) for o in objects])
        return len(objects)

    def upsert_fields(self, fields: Sequence[SField], *, source: str) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO fields (object_api_name, api_name, label, description,
                   inline_help_text, data_type, length, precision_value,
                   scale_value, is_nullable, is_unique, is_external_id,
                   is_auto_number, is_calculated, calculated_formula,
                   is_createable, is_updateable, is_filterable, is_sortable,
                   is_groupable, is_name_field, relationship_name, reference_to,
                   default_value, defaulted_on_create, restricted_picklist,
                   source, last_refreshed_at, raw_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(object_api_name, api_name) DO UPDATE SET
                   label=excluded.label, description=excluded.description,
                   inline_help_text=excluded.inline_help_text,
                   data_type=excluded.data_type, length=excluded.length,
                   precision_value=excluded.precision_value,
                   scale_value=excluded.scale_value,
                   is_nullable=excluded.is_nullable, is_unique=excluded.is_unique,
                   is_external_id=excluded.is_external_id,
                   is_auto_number=excluded.is_auto_number,
                   is_calculated=excluded.is_calculated,
                   calculated_formula=excluded.calculated_formula,
                   is_createable=COALESCE(excluded.is_createable, fields.is_createable),
                   is_updateable=COALESCE(excluded.is_updateable, fields.is_updateable),
                   is_filterable=COALESCE(excluded.is_filterable, fields.is_filterable),
                   is_sortable=COALESCE(excluded.is_sortable, fields.is_sortable),
                   is_groupable=COALESCE(excluded.is_groupable, fields.is_groupable),
                   is_name_field=excluded.is_name_field,
                   relationship_name=excluded.relationship_name,
                   reference_to=excluded.reference_to,
                   default_value=excluded.default_value,
                   defaulted_on_create=excluded.defaulted_on_create,
                   restricted_picklist=excluded.restricted_picklist,
                   source=excluded.source,
                   last_refreshed_at=excluded.last_refreshed_at,
                   raw_hash=excluded.raw_hash""",
            [(f.object_api_name, f.api_name, f.label, f.description,
              f.inline_help_text, f.data_type, f.length, f.precision_value,
              f.scale_value, int(f.is_nullable), int(f.is_unique),
              int(f.is_external_id), int(f.is_auto_number), int(f.is_calculated),
              f.calculated_formula, _bool(f.is_createable), _bool(f.is_updateable),
              _bool(f.is_filterable), _bool(f.is_sortable), _bool(f.is_groupable),
              int(f.is_name_field), f.relationship_name,
              json.dumps(f.reference_to) if f.reference_to else None,
              f.default_value, int(f.defaulted_on_create),
              int(f.restricted_picklist),
              # A field that knows where it came from keeps it: platform
              # fields injected at refresh are not from the mirror, and
              # stamping them so would hide which rows were inferred.
              f.source if f.source not in ("", "mirror") else source,
              stamp, f.raw_hash)
             for f in fields])
        return len(fields)

    def upsert_relationships(self, rows: Sequence[Relationship]) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO relationships (source_object, source_field,
                   relationship_name, target_object, relationship_type,
                   is_polymorphic, cascade_delete, last_refreshed_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source_object, source_field, target_object)
               DO UPDATE SET relationship_name=excluded.relationship_name,
                   relationship_type=excluded.relationship_type,
                   is_polymorphic=excluded.is_polymorphic,
                   cascade_delete=excluded.cascade_delete,
                   last_refreshed_at=excluded.last_refreshed_at""",
            [(r.source_object, r.source_field, r.relationship_name,
              r.target_object, r.relationship_type, int(r.is_polymorphic),
              int(r.cascade_delete), stamp) for r in rows])
        return len(rows)

    def upsert_child_relationships(self, rows: Sequence[ChildRelationship]) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO child_relationships (parent_object, child_object,
                   child_field, relationship_name, cascade_delete,
                   last_refreshed_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(parent_object, child_object, child_field)
               DO UPDATE SET relationship_name=excluded.relationship_name,
                   cascade_delete=excluded.cascade_delete,
                   last_refreshed_at=excluded.last_refreshed_at""",
            [(r.parent_object, r.child_object, r.child_field,
              r.relationship_name, int(r.cascade_delete), stamp) for r in rows])
        return len(rows)

    def upsert_picklist_values(self, rows: Sequence[PicklistValue]) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO picklist_values (object_api_name, field_api_name,
                   label, value, is_active, is_default, valid_for, sort_order,
                   last_refreshed_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(object_api_name, field_api_name, value)
               DO UPDATE SET label=excluded.label, is_active=excluded.is_active,
                   is_default=excluded.is_default, sort_order=excluded.sort_order,
                   last_refreshed_at=excluded.last_refreshed_at""",
            [(p.object_api_name, p.field_api_name, p.label, p.value,
              int(p.is_active), int(p.is_default), p.valid_for, p.sort_order,
              stamp) for p in rows])
        return len(rows)

    def upsert_record_types(self, rows: Sequence[RecordType]) -> int:
        stamp = _now()
        self._c.executemany(
            """INSERT INTO record_types (object_api_name, record_type_id,
                   developer_name, name, description, is_active, is_default,
                   last_refreshed_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(object_api_name, developer_name)
               DO UPDATE SET
                   record_type_id=COALESCE(excluded.record_type_id,
                                           record_types.record_type_id),
                   name=excluded.name, description=excluded.description,
                   is_active=excluded.is_active,
                   last_refreshed_at=excluded.last_refreshed_at""",
            [(r.object_api_name, r.record_type_id, r.developer_name, r.name,
              r.description, int(r.is_active), int(r.is_default), stamp)
             for r in rows])
        return len(rows)

    def upsert_aliases(self, rows: Sequence[Alias], *, field_level: bool) -> int:
        if field_level:
            self._c.executemany(
                """INSERT INTO field_aliases (object_api_name, field_api_name,
                       alias, source, confidence, is_manual)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(object_api_name, field_api_name, alias)
                   DO UPDATE SET confidence=MAX(field_aliases.confidence,
                                                excluded.confidence)""",
                [(a.object_api_name, a.field_api_name, a.alias, a.source,
                  a.confidence, int(a.is_manual)) for a in rows])
        else:
            self._c.executemany(
                """INSERT INTO object_aliases (object_api_name, alias, source,
                       confidence, is_manual)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(object_api_name, alias)
                   DO UPDATE SET confidence=MAX(object_aliases.confidence,
                                                excluded.confidence)""",
                [(a.object_api_name, a.alias, a.source, a.confidence,
                  int(a.is_manual)) for a in rows])
        return len(rows)

    def write_bundle(self, bundle: SchemaBundle, *, source: str) -> dict[str, int]:
        return {
            "objects": self.upsert_objects(bundle.objects, source=source),
            "fields": self.upsert_fields(bundle.fields, source=source),
            "relationships": self.upsert_relationships(bundle.relationships),
            "child_relationships": self.upsert_child_relationships(
                bundle.child_relationships),
            "picklist_values": self.upsert_picklist_values(bundle.picklist_values),
            "record_types": self.upsert_record_types(bundle.record_types),
            "object_aliases": self.upsert_aliases(bundle.object_aliases,
                                                  field_level=False),
            "field_aliases": self.upsert_aliases(bundle.field_aliases,
                                                 field_level=True),
        }

    # -- reads ------------------------------------------------------------
    def object_catalog(self) -> list[dict[str, Any]]:
        """Compact entries only: enough to rank a candidate, never every field."""
        return [dict(r) for r in self._c.execute(
            """SELECT o.api_name, o.label, o.plural_label, o.description,
                      o.is_custom, o.is_hot_object, o.field_count,
                      o.relationship_count,
                      (SELECT group_concat(alias, '|') FROM object_aliases a
                        WHERE a.object_api_name = o.api_name) AS aliases
               FROM objects o ORDER BY o.api_name""")]

    def get_object(self, api_name: str) -> dict[str, Any] | None:
        row = self._c.execute("SELECT * FROM objects WHERE api_name=?",
                              (api_name,)).fetchone()
        return dict(row) if row else None

    def get_fields(self, api_name: str) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._c.execute(
            "SELECT * FROM fields WHERE object_api_name=? ORDER BY api_name",
            (api_name,))]
        for row in rows:
            row["reference_to"] = json.loads(row["reference_to"] or "[]")
        return rows

    def get_field(self, api_name: str, field_api_name: str) -> dict[str, Any] | None:
        row = self._c.execute(
            "SELECT * FROM fields WHERE object_api_name=? AND api_name=?",
            (api_name, field_api_name)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["reference_to"] = json.loads(out["reference_to"] or "[]")
        return out

    def get_relationships(self, api_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM relationships WHERE source_object=?"
            " ORDER BY source_field", (api_name,))]

    def get_child_relationships(self, api_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM child_relationships WHERE parent_object=?"
            " ORDER BY child_object, child_field", (api_name,))]

    def get_picklist_values(self, api_name: str,
                            field_api_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM picklist_values WHERE object_api_name=?"
            " AND field_api_name=? ORDER BY sort_order, value",
            (api_name, field_api_name))]

    def get_record_types(self, api_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM record_types WHERE object_api_name=?"
            " ORDER BY developer_name", (api_name,))]

    def get_object_aliases(self, api_name: str) -> list[str]:
        return [r[0] for r in self._c.execute(
            "SELECT alias FROM object_aliases WHERE object_api_name=?"
            " ORDER BY confidence DESC, alias", (api_name,))]

    def get_field_aliases(self, api_name: str,
                          field_api_name: str) -> list[str]:
        return [r[0] for r in self._c.execute(
            "SELECT alias FROM field_aliases WHERE object_api_name=?"
            " AND field_api_name=? ORDER BY confidence DESC, alias",
            (api_name, field_api_name))]

    def counts(self) -> dict[str, int]:
        tables = ("objects", "fields", "relationships", "child_relationships",
                  "picklist_values", "record_types", "object_aliases",
                  "field_aliases")
        return {t: self._c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in tables}

    def object_names(self) -> list[str]:
        return [r[0] for r in self._c.execute(
            "SELECT api_name FROM objects ORDER BY api_name")]

    def hashes(self) -> dict[str, str]:
        """api_name -> raw_hash, for skipping unchanged objects on refresh."""
        return {r[0]: r[1] for r in self._c.execute(
            "SELECT api_name, raw_hash FROM objects")}

    # -- refresh audit ----------------------------------------------------
    def start_refresh(self, refresh_id: str, environment: str, source: str) -> None:
        self._c.execute(
            "INSERT INTO schema_refresh_runs (refresh_id, started_at,"
            " environment, source, status) VALUES (?,?,?,?, 'running')",
            (refresh_id, _now(), environment, source))

    def finish_refresh(self, refresh_id: str, *, status: str,
                       counts: dict[str, int], errors: Sequence[str]) -> None:
        self._c.execute(
            """UPDATE schema_refresh_runs SET completed_at=?, status=?,
                   objects_discovered=?, objects_updated=?, fields_updated=?,
                   relationships_updated=?, error_count=?, error_summary=?
               WHERE refresh_id=?""",
            (_now(), status, counts.get("objects", 0), counts.get("objects", 0),
             counts.get("fields", 0), counts.get("relationships", 0),
             len(errors), "\n".join(errors[:20]) or None, refresh_id))

    def last_refresh(self) -> dict[str, Any] | None:
        row = self._c.execute(
            "SELECT * FROM schema_refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
