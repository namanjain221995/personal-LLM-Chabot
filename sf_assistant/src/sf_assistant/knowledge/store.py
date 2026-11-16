"""Load, validate and index one object-knowledge JSON file.

The knowledge file describes a single Salesforce object (fields, relationships, child
relationships, temporal semantics, validation rules, automation, business processes,
provenance, record types and field dependencies). :class:`ObjectKnowledge` wraps the raw
dict and provides:

* case-insensitive lookups by API name or label (``field``, ``picklist_values`` ...),
* cross-reference helpers (which flows / apex / validation rules touch a field),
* ``chunks()`` – the file flattened into small retrievable units with stable ids, used by
  the BM25 index (``okeval.search``) and by coverage reporting (``okeval.report``),
* ``validate()`` – structural and referential checks, returned as readable messages.

Only the standard library is used so the module works unchanged inside Colab.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

__all__ = ["ObjectKnowledge", "SYSTEM_FIELDS", "REMOVED_OBJECT_KEYS", "split_api_name"]

# Top-level keys every knowledge file must carry.
REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "object",
    "record_types",
    "fields",
    "relationships",
    "child_relationships",
    "field_dependencies",
    "temporal_semantics",
    "validation_rules",
    "automation",
    "business_process_usage",
    "provenance",
)

# Keys under ``object`` that the clean format drops (they belong to the runtime schema).
REMOVED_OBJECT_KEYS: tuple[str, ...] = (
    "business_role",
    "queryable",
    "searchable",
    "createable",
    "updateable",
)

# Standard Salesforce system fields that may be referenced (e.g. by temporal_semantics)
# without being listed under ``fields``.
SYSTEM_FIELDS: frozenset[str] = frozenset(
    {
        "Id",
        "OwnerId",
        "CreatedDate",
        "CreatedById",
        "LastModifiedDate",
        "LastModifiedById",
        "SystemModstamp",
        "IsDeleted",
        "LastActivityDate",
        "LastViewedDate",
        "LastReferencedDate",
        "RecordTypeId",
    }
)

PICKLIST_TYPES: frozenset[str] = frozenset({"picklist", "multipicklist"})

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def split_api_name(name: str) -> str:
    """Turn an API name into plain words: ``CandidateTrainingStep__c`` -> ``candidate training step``."""
    base = re.sub(r"__(c|r|mdt|e|x|b)$", "", name or "", flags=re.IGNORECASE)
    base = _CAMEL_RE.sub(" ", base.replace("_", " "))
    return " ".join(base.lower().split())


def _norm_key(text: str) -> str:
    """Normalisation used for loose name matching (lowercase, alphanumerics only)."""
    return _NON_ALNUM_RE.sub("", (text or "").lower())


def _join(items: Iterable[Any]) -> str:
    return ", ".join(str(i) for i in items if i not in (None, ""))


def child_chunk_id(child: dict, children: list[dict]) -> str:
    """``child:<child_object>``, or ``child:<child_object>.<field>`` when that child object links
    to the parent through more than one field (two lookups to the same parent)."""
    obj = str(child.get("child_object"))
    same = sum(1 for c in children if str(c.get("child_object")) == obj)
    return f"child:{obj}" if same <= 1 else f"child:{obj}.{child.get('field')}"


class ObjectKnowledge:
    """In-memory view of one object-knowledge JSON document.

    Attributes:
        raw: the parsed JSON dict, unchanged.
        api_name: the object's API name (``raw["object"]["api_name"]``).
    """

    raw: dict
    api_name: str

    def __init__(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise TypeError("knowledge data must be a dict parsed from the JSON file")
        self.raw = data
        obj = data.get("object") or {}
        self.api_name = str(obj.get("api_name") or "UnknownObject")
        self.label = str(obj.get("label") or self.api_name)

        # Field lookup tables (built once; the file is read-only after load).
        self._fields: list[dict] = [f for f in (data.get("fields") or []) if isinstance(f, dict)]
        self._by_api: dict[str, dict] = {}
        self._by_label: dict[str, list[dict]] = {}
        self._by_norm: dict[str, dict] = {}
        for f in self._fields:
            api = str(f.get("api_name") or "")
            if not api:
                continue
            self._by_api.setdefault(api.lower(), f)
            label = str(f.get("label") or "")
            if label:
                self._by_label.setdefault(label.lower(), []).append(f)
            # Loose keys: api without suffix, api normalised, label normalised.
            for key in (_norm_key(api), _norm_key(re.sub(r"__c$", "", api, flags=re.I)), _norm_key(label)):
                if key:
                    self._by_norm.setdefault(key, f)
        self._chunks: list[dict] | None = None

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str) -> "ObjectKnowledge":
        """Read and parse a knowledge JSON file."""
        with Path(path).open("r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    # ------------------------------------------------------------------ section accessors
    @property
    def object_info(self) -> dict:
        return self.raw.get("object") or {}

    def fields(self) -> list[dict]:
        """All field dicts, in file order."""
        return list(self._fields)

    def relationships(self) -> list[dict]:
        """Parent (lookup / master-detail) relationships declared on this object."""
        return [r for r in (self.raw.get("relationships") or []) if isinstance(r, dict)]

    def child_relationships(self) -> list[dict]:
        """Child objects that point at this object."""
        return [r for r in (self.raw.get("child_relationships") or []) if isinstance(r, dict)]

    def child_chunk_id(self, child: dict) -> str:
        """Chunk id of one child relationship (see :func:`child_chunk_id`)."""
        return child_chunk_id(child, self.child_relationships())

    def temporal_semantics(self) -> dict:
        return self.raw.get("temporal_semantics") or {}

    def validation_rules(self) -> list[dict]:
        return [r for r in (self.raw.get("validation_rules") or []) if isinstance(r, dict)]

    def flows(self) -> list[dict]:
        return [a for a in ((self.raw.get("automation") or {}).get("flows") or []) if isinstance(a, dict)]

    def apex(self) -> list[dict]:
        return [a for a in ((self.raw.get("automation") or {}).get("apex") or []) if isinstance(a, dict)]

    def business_processes(self) -> list[dict]:
        return [p for p in (self.raw.get("business_process_usage") or []) if isinstance(p, dict)]

    def record_types(self) -> list[Any]:
        return list(self.raw.get("record_types") or [])

    def field_dependencies(self) -> list[Any]:
        return list(self.raw.get("field_dependencies") or [])

    def provenance(self) -> dict:
        return self.raw.get("provenance") or {}

    # ------------------------------------------------------------------ lookups
    def field(self, name_or_label: str) -> dict | None:
        """Find a field by API name or label, case-insensitively.

        Resolution order: exact API name, exact label, then a loose match that ignores
        case, spaces, underscores, punctuation and the ``__c`` suffix (so ``"status"``,
        ``"Status__c"`` and ``"date of interview"`` all resolve). Returns ``None`` when
        nothing matches. When two fields share a label the first in file order wins; use
        :meth:`fields_with_label` to see all of them.
        """
        if not name_or_label:
            return None
        key = str(name_or_label).strip()
        # Accept "Object.Field" for this object.
        if "." in key:
            obj, _, rest = key.partition(".")
            if obj.lower() == self.api_name.lower():
                key = rest
        low = key.lower()
        if low in self._by_api:
            return self._by_api[low]
        if low in self._by_label:
            return self._by_label[low][0]
        return self._by_norm.get(_norm_key(key)) or self._by_norm.get(
            _norm_key(re.sub(r"__c$", "", key, flags=re.I))
        )

    def fields_with_label(self, label: str) -> list[dict]:
        """All fields whose label equals ``label`` (case-insensitive)."""
        return list(self._by_label.get((label or "").lower(), []))

    def field_api_names(self) -> list[str]:
        return [str(f.get("api_name")) for f in self._fields if f.get("api_name")]

    def picklist_values(self, field: str) -> list[str] | str | None:
        """Picklist values of ``field``.

        Returns a list of value strings, a ``str`` when the file only describes the values
        (e.g. ``"15-minute AM/PM time-slot values"``), or ``None`` when the field is
        unknown or is not a picklist.
        """
        f = self.field(field)
        if not f or not isinstance(f.get("picklist"), dict):
            return None
        values = f["picklist"].get("values")
        if isinstance(values, str):
            return values
        if isinstance(values, list):
            out: list[str] = []
            for v in values:
                if isinstance(v, dict):
                    out.append(str(v.get("value") if v.get("value") is not None else v.get("label")))
                else:
                    out.append(str(v))
            return out
        return None

    def relationship_for_field(self, api_name: str) -> dict | None:
        """The ``relationships`` entry whose ``field`` is ``api_name``."""
        for r in self.relationships():
            if str(r.get("field", "")).lower() == (api_name or "").lower():
                return r
        return None

    def process(self, process_id: str) -> dict | None:
        for p in self.business_processes():
            if str(p.get("process_id", "")).lower() == (process_id or "").lower():
                return p
        return None

    def automation_items(self) -> list[tuple[str, dict]]:
        """``[("flow", item), ..., ("apex", item), ...]``."""
        return [("flow", f) for f in self.flows()] + [("apex", a) for a in self.apex()]

    def field_usage(self, api_name: str) -> dict[str, list[dict]]:
        """Everything in the file that reads, writes or constrains ``api_name``.

        Returns ``{"read_by": [...], "written_by": [...], "validation_rules": [...],
        "temporal_roles": [...]}``. Automation entries are compact dicts with
        ``kind``, ``name``, ``type`` and ``business_process``. Cross-object references such
        as ``Session__c.Status__c`` are *not* treated as this object's ``Status__c``.
        """
        target = (api_name or "").lower()

        def _hits(refs: Iterable[Any]) -> bool:
            for ref in refs or []:
                ref_s = str(ref)
                if "." in ref_s:
                    obj, _, fld = ref_s.partition(".")
                    if obj.lower() == self.api_name.lower() and fld.lower() == target:
                        return True
                elif ref_s.lower() == target:
                    return True
            return False

        read_by: list[dict] = []
        written_by: list[dict] = []
        for kind, item in self.automation_items():
            brief = {
                "kind": kind,
                "name": item.get("name"),
                "type": item.get("type"),
                "business_process": item.get("business_process"),
            }
            if _hits(item.get("fields_read")):
                read_by.append(brief)
            if _hits(item.get("fields_written")):
                written_by.append(brief)
        rules = [
            {"name": r.get("name"), "active": r.get("active"), "meaning": r.get("meaning")}
            for r in self.validation_rules()
            if _hits(r.get("fields_involved"))
        ]
        temporal = [
            {"key": k, "business_meaning": (v or {}).get("business_meaning")}
            for k, v in self.temporal_semantics().items()
            if isinstance(v, dict) and str(v.get("field", "")).lower() == target
        ]
        return {"read_by": read_by, "written_by": written_by, "validation_rules": rules, "temporal_roles": temporal}

    @staticmethod
    def field_flags(f: dict) -> dict[str, bool]:
        """Derived flags: ``legacy`` and ``api_name_typo`` (from roles / concept / meaning text)."""
        sem = f.get("semantic") or {}
        roles = [str(r).lower() for r in sem.get("roles") or []]
        concept = str(sem.get("concept") or "").lower()
        meaning = str(f.get("business_meaning") or "").lower()
        legacy = "legacy_attribute" in roles or concept.startswith("legacy") or "legacy" in meaning
        typo = "typo" in meaning or "misspell" in meaning
        return {"legacy": legacy, "api_name_typo": typo}

    # ------------------------------------------------------------------ chunks
    def chunks(self) -> list[dict]:
        """Flatten the file into retrievable chunks (cached).

        Each chunk is ``{"id", "section", "title", "text", "data"}``. Ids are stable:
        ``object``, ``object.semantics``, ``object.record_grain``,
        ``object.discovery_summary``, ``field:<api>``, ``picklist:<api>`` (only picklists
        with concrete values), ``relationship:<field>``, ``child:<child_object>``,
        ``temporal:<key>``, ``validation:<name>``, ``flow:<name>``, ``apex:<name>``,
        ``process:<process_id>``, ``provenance``, ``record_types``, ``field_dependencies``.
        Sections are: object, field, picklist, relationship, child_relationship, temporal,
        validation, flow, apex, process, provenance, record_types, field_dependencies.
        """
        if self._chunks is None:
            self._chunks = self._build_chunks()
        return [dict(c) for c in self._chunks]

    def section_ids(self) -> list[str]:
        """Every chunk id, in chunk order (the denominator for coverage)."""
        if self._chunks is None:
            self._chunks = self._build_chunks()
        return [c["id"] for c in self._chunks]

    def _build_chunks(self) -> list[dict]:
        chunks: list[dict] = []
        name = self.api_name

        def add(cid: str, section: str, title: str, text: str, data: Any) -> None:
            chunks.append(
                {
                    "id": cid,
                    "section": section,
                    "title": title,
                    "text": " ".join(text.split()),
                    "data": data if isinstance(data, dict) else {"value": data},
                }
            )

        obj = self.object_info
        core = {k: v for k, v in obj.items() if k not in ("business_semantics", "record_grain", "discovery_summary")}
        add(
            "object",
            "object",
            f"{obj.get('label', name)} ({name})",
            f"Object {name}, label {obj.get('label')}, plural {obj.get('plural_label')}. "
            f"Custom object: {obj.get('custom')}. {obj.get('description', '')} "
            f"Business domains: {_join(obj.get('business_domain') or [])}.",
            core,
        )
        sem = obj.get("business_semantics") or {}
        add(
            "object.semantics",
            "object",
            f"{name} business semantics",
            f"Primary concept: {sem.get('primary_concept')}. Concepts: {_join(sem.get('concepts') or [])}. "
            f"Related concepts: {_join(sem.get('related_concepts') or [])}.",
            sem,
        )
        grain = obj.get("record_grain") or {}
        add(
            "object.record_grain",
            "object",
            f"{name} record grain",
            f"Record grain of {name}: {grain.get('description', '')}",
            grain,
        )
        disc = obj.get("discovery_summary") or {}
        add(
            "object.discovery_summary",
            "object",
            f"{name} discovery summary",
            f"{disc.get('one_line', '')} Key concepts: {_join(disc.get('key_concepts') or [])}. "
            f"Key relationships: {_join(disc.get('key_relationships') or [])}.",
            disc,
        )

        for f in self._fields:
            api = str(f.get("api_name"))
            sem_f = f.get("semantic") or {}
            ref = f.get("reference") or {}
            flags = self.field_flags(f)
            parts = [
                f"Field {api} ({split_api_name(api)}), label {f.get('label')}, type {f.get('type')}.",
                str(f.get("business_meaning") or ""),
                f"Concept {str(sem_f.get('concept') or '').replace('_', ' ')}.",
                f"Roles: {_join(str(r).replace('_', ' ') for r in sem_f.get('roles') or [])}.",
            ]
            if ref:
                parts.append(
                    f"References {_join(ref.get('target_objects') or [])} "
                    f"({_join(split_api_name(t) for t in ref.get('target_objects') or [])}); "
                    f"role {ref.get('business_role')}: {ref.get('business_meaning') or ''}"
                )
            pl = self.picklist_values(api)
            if isinstance(pl, list):
                parts.append(f"Values: {_join(pl)}.")
            elif isinstance(pl, str):
                parts.append(f"Values: {pl}.")
            if flags["legacy"]:
                parts.append("Legacy field.")
            if flags["api_name_typo"]:
                parts.append("API name contains a typo / misspelling.")
            add(f"field:{api}", "field", f"{f.get('label')} ({api})", " ".join(parts), f)

        for f in self._fields:
            pick = f.get("picklist")
            if not isinstance(pick, dict) or not isinstance(pick.get("values"), list) or not pick["values"]:
                continue
            api = str(f.get("api_name"))
            rendered = []
            for v in pick["values"]:
                if isinstance(v, dict):
                    val = v.get("value") if v.get("value") is not None else v.get("label")
                    txt = str(val)
                    if v.get("label") not in (None, val):
                        txt += f" (label {v.get('label')})"
                    if v.get("business_meaning"):
                        txt += f": {v['business_meaning']}"
                    rendered.append(txt)
                else:
                    rendered.append(str(v))
            add(
                f"picklist:{api}",
                "picklist",
                f"{f.get('label')} ({api}) picklist values",
                f"{f.get('type')} {api} ({f.get('label')}) allowed values: " + "; ".join(rendered) + ".",
                {"field": api, "type": f.get("type"), **pick},
            )

        for r in self.relationships():
            fld = str(r.get("field"))
            fdef = self._by_api.get(fld.lower()) or {}
            ds = r.get("direction_semantics") or {}
            targets = r.get("target_objects") or []
            add(
                f"relationship:{fld}",
                "relationship",
                f"{fdef.get('label', fld)} ({fld}) -> {_join(targets)}",
                f"Relationship field {fld} ({fdef.get('label', split_api_name(fld))}) is a {r.get('type')} "
                f"to {_join(targets)} ({_join(split_api_name(t) for t in targets)}), cardinality "
                f"{r.get('cardinality')}. "
                + (f"Business role {str(r.get('business_role')).replace('_', ' ')}. " if r.get("business_role") else "")
                + f"{r.get('business_meaning') or ''} From {name} the target is the "
                f"{ds.get('from_source')}; from the target, these records are its {ds.get('from_target')}.",
                r,
            )

        for c in self.child_relationships():
            child = str(c.get("child_object"))
            ds = c.get("direction_semantics") or {}
            role = str(c.get("business_role") or "").replace("_", " ")
            add(
                self.child_chunk_id(c),
                "child_relationship",
                f"{child} -> {name}" + ("" if self.child_chunk_id(c) == f"child:{child}" else f" ({c.get('field')})"),
                f"Child object {child} ({split_api_name(child)}) references {name} through its field "
                f"{c.get('field')}. " + (f"Business role {role}. " if role else "") +
                f"{c.get('business_meaning') or ''} From the parent they are {ds.get('from_parent')}; "
                f"from the child the parent is the {ds.get('from_child')}.",
                c,
            )

        for key, val in self.temporal_semantics().items():
            v = val if isinstance(val, dict) else {"value": val}
            add(
                f"temporal:{key}",
                "temporal",
                f"{key.replace('_', ' ')} ({v.get('field')})",
                f"Temporal semantic {key.replace('_', ' ')} is field {v.get('field')}: {v.get('business_meaning') or ''}",
                {"key": key, **v},
            )

        for r in self.validation_rules():
            add(
                f"validation:{r.get('name')}",
                "validation",
                f"Validation rule {r.get('name')}",
                f"Validation rule {r.get('name')} (active {r.get('active')}) involves "
                f"{_join(r.get('fields_involved') or [])}. {r.get('meaning') or ''} "
                f"Condition: {r.get('condition_summary') or ''} Error meaning: {r.get('error_meaning') or ''} "
                f"Formula: {r.get('formula') or ''}",
                r,
            )

        for kind, item in self.automation_items():
            trig = item.get("trigger")
            trig_txt = (
                f"Triggered on {trig.get('object')} {_join(trig.get('events') or [])}."
                if isinstance(trig, dict)
                else "No record trigger (launched by a user or code)."
            )
            add(
                f"{kind}:{item.get('name')}",
                kind,
                f"{'Flow' if kind == 'flow' else 'Apex'} {item.get('name')}",
                f"{'Flow' if kind == 'flow' else 'Apex'} {item.get('name')} ({split_api_name(str(item.get('name')))}), "
                f"type {item.get('type')}. {item.get('purpose') or ''} {trig_txt} "
                f"Reads fields: {_join(item.get('fields_read') or []) or 'none listed'}. "
                f"Writes fields: {_join(item.get('fields_written') or []) or 'none listed'}. "
                f"Related objects: {_join(item.get('related_objects') or [])}. "
                f"Business process: {item.get('business_process')}.",
                item,
            )

        for p in self.business_processes():
            pid = str(p.get("process_id"))
            autos = [i.get("name") for _, i in self.automation_items() if i.get("business_process") == pid]
            add(
                f"process:{pid}",
                "process",
                f"Business process {p.get('process_name')} ({pid})",
                f"Business process {p.get('process_name')} ({pid}): {name} acts as "
                f"{str(p.get('object_role') or '').replace('_', ' ')}. {p.get('meaning') or ''} "
                f"Automation: {_join(autos) or 'none listed'}.",
                p,
            )

        prov = self.provenance()
        srcs = "; ".join(
            f"{s.get('type')} {s.get('source')} ({s.get('knowledge_type')}, confidence {s.get('confidence')})"
            for s in prov.get("sources") or []
            if isinstance(s, dict)
        )
        add(
            "provenance",
            "provenance",
            "Provenance and data quality",
            f"Source environment {prov.get('source_environment')}. Review status {prov.get('review_status')}. "
            f"Sources: {srcs}. Notes: {prov.get('notes') or ''}",
            prov,
        )
        rts = self.record_types()
        add(
            "record_types",
            "record_types",
            "Record types",
            f"Record types of {name}: "
            + (_join(r.get("name") or r.get("developer_name") if isinstance(r, dict) else r for r in rts) if rts else "none defined in this knowledge file."),
            {"record_types": rts},
        )
        deps = self.field_dependencies()
        add(
            "field_dependencies",
            "field_dependencies",
            "Field dependencies",
            f"Field (controlling / dependent picklist) dependencies of {name}: "
            + (json.dumps(deps, ensure_ascii=False) if deps else "none defined in this knowledge file."),
            {"field_dependencies": deps},
        )
        return chunks

    # ------------------------------------------------------------------ validation
    def validate(self) -> list[str]:
        """Check the file's structure and internal references.

        Returns human-readable problems; an empty list means valid. Problems that do not
        break lookups are prefixed ``WARN:`` (keys that should have been removed from
        ``object``, automation pointing at a business process id that
        ``business_process_usage`` does not document, reference fields with no
        ``relationships`` entry). Everything else is an error. Use
        ``[p for p in problems if not p.startswith("WARN:")]`` for hard errors only.
        """
        problems: list[str] = []
        data = self.raw
        for key in REQUIRED_TOP_LEVEL_KEYS:
            if key not in data:
                problems.append(f"missing top-level key '{key}'")
        obj = data.get("object")
        if not isinstance(obj, dict) or not obj.get("api_name"):
            problems.append("object.api_name is missing")
            obj = obj if isinstance(obj, dict) else {}
        for key in REMOVED_OBJECT_KEYS:
            if key in obj:
                problems.append(f"WARN: object.{key} should be removed (runtime schema owns it)")

        # Unique field api names.
        seen: dict[str, int] = {}
        for f in data.get("fields") or []:
            api = str((f or {}).get("api_name") or "")
            if not api:
                problems.append("field without api_name")
                continue
            seen[api.lower()] = seen.get(api.lower(), 0) + 1
        for api, n in seen.items():
            if n > 1:
                problems.append(f"duplicate field api_name '{api}' ({n} times)")

        # Picklist fields need values.
        for f in self._fields:
            if str(f.get("type") or "").lower() in PICKLIST_TYPES:
                pick = f.get("picklist")
                vals = pick.get("values") if isinstance(pick, dict) else None
                if not vals:
                    problems.append(f"picklist field '{f.get('api_name')}' has no picklist values")

        # Relationships match reference fields.
        for r in self.relationships():
            fld = str(r.get("field") or "")
            fdef = self._by_api.get(fld.lower())
            if fdef is None:
                problems.append(f"relationship field '{fld}' is not in fields")
                continue
            if str(fdef.get("type") or "").lower() != "reference":
                problems.append(f"relationship field '{fld}' has type '{fdef.get('type')}', expected 'reference'")
                continue
            ref_targets = sorted((fdef.get("reference") or {}).get("target_objects") or [])
            rel_targets = sorted(r.get("target_objects") or [])
            if ref_targets != rel_targets:
                problems.append(
                    f"relationship '{fld}' targets {rel_targets} but field reference targets {ref_targets}"
                )

        listed = {str(r.get("field") or "").lower() for r in self.relationships()}
        for f in self._fields:
            if str(f.get("type") or "").lower() == "reference" and str(f.get("api_name")).lower() not in listed:
                problems.append(
                    f"WARN: reference field '{f.get('api_name')}' has no entry in relationships "
                    "(cardinality / direction semantics missing)"
                )

        # Automation and validation references.
        def check_refs(owner: str, refs: Iterable[Any]) -> None:
            for ref in refs or []:
                ref_s = str(ref)
                if "." in ref_s:
                    obj_name, _, fld = ref_s.partition(".")
                    if obj_name.lower() != self.api_name.lower():
                        continue  # cross-object reference: allowed, cannot be checked here
                    ref_s = fld
                if ref_s.lower() not in self._by_api and ref_s not in SYSTEM_FIELDS:
                    problems.append(f"{owner} references unknown field '{ref}'")

        process_ids = {str(p.get("process_id")) for p in self.business_processes()}
        for kind, item in self.automation_items():
            owner = f"{kind} '{item.get('name')}'"
            check_refs(owner, item.get("fields_read"))
            check_refs(owner, item.get("fields_written"))
            bp = item.get("business_process")
            if bp and bp not in process_ids:
                problems.append(
                    f"WARN: {owner} references business_process '{bp}' that is not in business_process_usage"
                )
        for r in self.validation_rules():
            check_refs(f"validation rule '{r.get('name')}'", r.get("fields_involved"))
        for key, val in self.temporal_semantics().items():
            if isinstance(val, dict) and val.get("field"):
                check_refs(f"temporal_semantics.{key}", [val["field"]])

        # Chunk ids must be unique (they key coverage).
        ids = self.section_ids()
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        for d in dupes:
            problems.append(f"duplicate chunk id '{d}'")
        problems.extend(self.consistency_warnings())
        return problems

    # Wording that says a field has been superseded by a newer model without the word "legacy".
    _NEWER_RX = re.compile(r"\bnewer\b[^.]*\b(uses?|model)\b", re.IGNORECASE)
    # "mutual with Other__c.Field__c" style back-references in relationship meanings.
    _BACKREF_RX = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__c)\.([A-Za-z][A-Za-z0-9_]*__c)\b")

    def consistency_warnings(self) -> list[str]:
        """Places where the file disagrees with itself (content, not structure).

        Every line starts with ``WARN: consistency:``. These do not break lookups, but they
        make gold answers ambiguous, so the report lists them as knowledge gaps for the JSON
        authors. Checks:

        * fields with a ``schedule_mirror`` role (or "mirrored from X" wording) that no
          automation reading ``X`` actually writes;
        * field meanings that mention a back-reference ``Other__c.Field__c`` to this object
          with no matching ``child_relationships`` entry;
        * "newer ... uses ..." wording on a field that carries no legacy role.
        """
        out: list[str] = []
        own = self.api_name.lower()
        autos = list(self.automation_items())

        # 1) mirrored fields that no syncing automation writes.
        for f in self._fields:
            api = str(f.get("api_name") or "")
            roles = [str(r).lower() for r in (f.get("semantic") or {}).get("roles") or []]
            m = re.search(r"mirrored from (?:the associated )?([A-Za-z][A-Za-z0-9_]*)",
                          f.get("business_meaning") or "", re.IGNORECASE)
            if "schedule_mirror" not in roles and not m:
                continue
            src = m.group(1).rstrip(".") if m else ""
            # "mirrored from Session" and "mirrored from Session__c" name the same object.
            srcs = {src.lower(), f"{src.lower()}__c"} if src else set()
            writers = []
            for _, a in autos:
                if api not in (a.get("fields_written") or []):
                    continue
                reads = [str(x) for x in a.get("fields_read") or []]
                trig = (a.get("trigger") or {}).get("object") if isinstance(a.get("trigger"), dict) else None
                if not srcs or str(trig).lower() in srcs or any(x.split(".")[0].lower() in srcs for x in reads):
                    writers.append(a.get("name"))
            if not writers:
                out.append(f"WARN: consistency: {api} is described as mirrored"
                           f"{' from ' + src if src else ''} but no automation"
                           f"{' reading ' + src if src else ''} writes it")

        # 2) back-references with no child_relationships entry.
        children = {(str(c.get("child_object") or "").lower(), str(c.get("field") or "").lower())
                    for c in self.child_relationships()}
        for f in self._fields:
            for obj, fld in self._BACKREF_RX.findall(f.get("business_meaning") or ""):
                if obj.lower() == own:
                    continue
                if (obj.lower(), fld.lower()) not in children:
                    out.append(f"WARN: consistency: {f.get('api_name')} mentions the back-reference "
                               f"{obj}.{fld}, but child_relationships has no {obj} entry")

        # 3) superseded wording without a legacy role.
        for f in self._fields:
            roles = [str(r).lower() for r in (f.get("semantic") or {}).get("roles") or []]
            meaning = f.get("business_meaning") or ""
            if self._NEWER_RX.search(meaning) and not any("legacy" in r for r in roles) \
                    and not re.search(r"\blegacy\b", meaning, re.IGNORECASE):
                out.append(f"WARN: consistency: {f.get('api_name')} says a newer model replaces it "
                           "('" + self._NEWER_RX.search(meaning).group(0) + "') but is not marked legacy")
        return out


# moved from okeval/multi.py (the record catalog, the cli and the web page list the files with it)
def knowledge_files(directory: str) -> list[str]:
    """Clean knowledge files in ``directory`` (``*.json`` except ``*.raw.json``), sorted."""
    return sorted(p for p in glob.glob(os.path.join(directory, "*.json")) if not p.endswith(".raw.json"))
