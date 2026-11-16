"""The discovery tools: ``find_objects``, ``describe_fields`` and ``get_field_values`` with their helpers.

Moved verbatim from ``okrec/tools.py``.
"""

from __future__ import annotations

import re
from typing import Any

from sf_assistant.knowledge.store import split_api_name
from sf_assistant.engine.catalog import (INTERVIEW_NOTE, SYSTEM_LOOKUPS, FieldSpec, ObjectSpec, PathRef, RecordError,
                                         blocked_text, contact_text, no_read_as)
from sf_assistant.engine.query_builder import Frag, Scope, group_sql, sensitive_object_text, where_sql
from sf_assistant.engine.values import escape_like, normalize

from sf_assistant.tools.record_base import _cell, _round, _Call, _short, _role


_FIELD_NAME_RE = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z][A-Za-z0-9_]*__c\b")


class DiscoveryMixin:
    """Discovery tools of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    # ================================================================== tools
    def _find_objects(self, call: _Call, query: str, k: int = 5) -> dict:
        cat = self.catalog
        hits = cat.search_objects(query, k=k)
        rows = []
        for spec, _score, matched in hits:
            rows.append({k2: v for k2, v in {
                "object": spec.api, "label": spec.label, "plural_label": spec.plural_label,
                "one_line": _short(spec.one_line or spec.description, 200), "record_grain": _short(spec.record_grain, 200),
                "records": spec.rows if spec.has_table else None, "has_table": spec.has_table,
                "documented": spec.documented, "synced_at": cat.synced_at(spec.api) if spec.has_table else None,
                "matched_on": matched, "note": spec.note,
                "lookup_groups": self._group_strings(spec) if spec.has_table else None}.items() if v not in (None, "", [])})
        apis = [r["object"] for r in rows]
        vals = cat.value_matches(query, top_objects=apis, n=8)
        vals.sort(key=lambda t: -t[3])
        for obj, fld, _v, _n in vals:
            entry = cat.values_of(obj, fld)
            if entry:
                call.cached(entry.statement)
        for spec, _s, _m in hits:
            call.cached(spec.stat_statements[0] if spec.stat_statements else None)
        if "Interview__c" in apis or "Internal_Interview__c" in apis:
            call.note(INTERVIEW_NOTE)
        if "Account" in apis and cat.objects["Account"].note:
            call.note(cat.objects["Account"].note)
        if not rows:
            call.note(f"no object matched '{query}'; try other words (e.g. the business name of the records)")
        call.summary = f"{len(rows)} objects"
        out = {"objects": rows, "value_matches": [{"object": o, "field": f, "value": v, "records": n}
                                                  for o, f, v, n in vals]}
        read = cat.search_read_as(query)   # the spelling help (SFA_RESOLVER=on): the misspelt words the search read
        if read:
            out["read_as"] = read
        return out

    def _distinct_alternative(self, spec: ObjectSpec, f: FieldSpec) -> FieldSpec | None:
        """Another lookup of ``spec`` that a distinct count on the lookup ``f`` can stand in for by mistake, when the
        question asks about it: its label is the start of ``f``'s label and ``f``'s extra words name a role of it
        ("Candidate" for "Candidate Owner": the owner of the candidate, a User, is no candidate), it points to another
        object, it holds data, and a question word shares its stem with the last word of its label ("candidates").
        None otherwise (also when the extra words only name another record of the same person: "user", "contact")."""
        cat = self.catalog
        q = re.findall(r"[a-z]+", (self._question or "").lower())
        fw = re.findall(r"[a-z0-9]+", (f.label or "").lower())
        if not q or not fw:
            return None
        same = {"user", "users", "id", "name", "record", "contact", "account", "login", "portal", "lookup"}
        for g in sorted(spec.fields.values(), key=lambda x: (-x.filled, x.api)):
            if (g is f or g.kind != "reference" or g.filled <= 0 or g.sensitivity == "blocked" or g.api in SYSTEM_LOOKUPS
                    or cat.target_of(g) is None or cat.target_of(g) == cat.target_of(f)):
                continue
            gw = re.findall(r"[a-z0-9]+", (g.label or "").lower())
            if not gw or len(gw) >= len(fw) or fw[:len(gw)] != gw or set(fw[len(gw):]) <= same:
                continue
            last = gw[-1]
            if any(len(w) >= 4 and len(last) >= 4 and (w == last or (len(w) >= 5 and len(last) >= 5 and w[:5] == last[:5]))
                   for w in q):
                return g
        return None

    def _group_strings(self, spec: ObjectSpec) -> list[str]:
        """At most 3 short lookup-group texts for find_objects."""
        cat = self.catalog
        out = []
        for t, items in cat.lookup_groups(spec)[:3]:
            parts = [f"{g.api} ({_role(g)})" if isinstance(g, FieldSpec) else f"{g} (not in the warehouse)" for g in items[:5]]
            out.append(f"{t} ({cat.label_of(t)}): " + ", ".join(parts) + (", ..." if len(items) > 5 else ""))
        return out

    def _lookup_groups_key(self, spec: ObjectSpec) -> list[dict]:
        """describe_fields 'lookup_groups': lookups of the object to the same target, with their roles (§21.7)."""
        cat = self.catalog
        out = []
        for t, items in cat.lookup_groups(spec):
            fields = []
            for g in items[:8]:
                if isinstance(g, FieldSpec):
                    e = {"field": g.api, "role": _role(g)}
                    if cat.almost_empty(spec, g):
                        e["almost_empty"] = True
                else:
                    nf = spec.not_synced.get(g) or {}
                    e = {"field": g, "role": str(nf.get("role") or "").replace("_", " ") or _short(nf.get("concept"), 60),
                         "not_in_warehouse": True}
                fields.append(e)
            out.append({"target": t, "target_label": cat.label_of(t), "note": "several lookups to the same object: pick by "
                        "role", "fields": fields})
        return out

    def _describe_fields(self, call: _Call, object: str, query: str | None = None, k: int = 12) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        res: dict[str, Any] = {"label": spec.label, "record_grain": _short(spec.record_grain, 240), "records": spec.rows,
                               "synced_at": cat.synced_at(spec.api)}
        if spec.note:
            res["note"] = spec.note
        res["date_fields"] = [{k2: v for k2, v in {
            "field": f.api, "type": f.kind, "temporal_key": f.temporal_keys[0][0] if f.temporal_keys else None,
            "meaning": _short(f.temporal_keys[0][1] if f.temporal_keys else f.meaning, 140), "has_data": f.has_data}.items()
            if v is not None} for f in spec.date_fields()]
        if spec.stat_statements:
            call.cached(spec.stat_statements[0])
        blocked = [f.api for f in spec.fields.values() if f.sensitivity == "blocked"]
        if query:
            ranked: list[FieldSpec] = []
            q = query.strip()
            direct = None
            try:
                with no_read_as():   # a search query is not a field argument: never read as its closest field name
                    direct = cat.field(spec, q)
            except RecordError:
                direct = None
            if direct is not None and direct.sensitivity != "blocked":
                ranked.append(direct)
            for hit in spec.field_index().search(q, k=max(k * 3, 20)):
                f = spec.fields.get(hit["id"].split(":", 1)[1])
                if f is not None and f not in ranked and f.sensitivity != "blocked":
                    ranked.append(f)
            vm = self._matching_values(spec, q)
            for fld, _v, _n, _p in vm:
                f = spec.fields.get(fld)
                if f is not None and f not in ranked[:k]:
                    ranked.insert(min(len(ranked), 3), f)
            ranked = list(dict.fromkeys(ranked))
            # the object's own field for the query ("Interview Type" on Interview__c for "type") before fields that
            # only share a word of it ("Incentive Type"): its meaning may point to the field that holds it in practice
            own = {f.api for f in ranked if _own_label_field(spec, f, q)}
            ranked.sort(key=lambda f: (0 if f is direct else 1, 0 if f.has_data else 1, 0 if f.api in own else 1))
            ranked, inserted = self._with_named_fields(call, spec, ranked, k, q)
            out = []
            rich_left = 5
            for f in ranked[:k]:
                rich = rich_left > 0 and f not in inserted   # fields a meaning names come in short form
                rich_left -= rich
                out.append(self._field_row(call, spec, f, rich=rich))
                call.use(PathRef(f.api, [], spec.api, f))
            res["fields"] = out
            res["matching_values"] = [{"field": fld, "value": v, "records": n, "in_picklist": p}
                                      for fld, v, n, p in vm[:12]]
        else:
            fields = sorted(spec.fields.values(), key=lambda f: (0 if f.has_data else 1,
                                                                 1 if not f.documented and f.kind != "reference" else 0,
                                                                 1 if f.legacy else 0,
                                                                 0 if not f.documented and f.kind == "reference" else 1))
            res["fields"] = [{"field": f.api, "label": f.label, "type": f.type_text()}
                             for f in fields if f.sensitivity != "blocked"][:80]
            res["note"] = ((res.get("note") or "") + " " if res.get("note") else "") + "pass query to see meanings and values"
        kids = [c for c in spec.children if c.via not in ("CreatedById", "LastModifiedById", "OwnerId")]
        if query:
            def kid_filled(c: Any) -> int:
                cs = cat.objects.get(c.child)
                g = cs.fields.get(c.via) if cs is not None else None
                return g.filled if g is not None else 0
            kids.sort(key=lambda c: -kid_filled(c))
            res["children"] = [{"child_object": c.child, "via_field": c.via, "meaning": _short(c.meaning, 120)}
                               for c in kids[:25]]
            if len(kids) > 25:
                res["children_total"] = len(kids)
        else:
            res["children"] = [{"child_object": c.child, "via_field": c.via} for c in kids[:25]]
        groups = self._lookup_groups_key(spec)
        if groups:
            res["lookup_groups"] = groups
        if spec.record_types:
            res["record_types"] = [{"name": r["name"], "developer_name": r.get("developer_name"), "records": r["records"]}
                                   for r in spec.record_types]
        nq: dict[str, Any] = {}
        if spec.not_synced:
            nq["documented_not_synced"] = sorted(spec.not_synced)[:40]
        if blocked:
            nq["sensitive"] = blocked
        if nq:
            res["not_queryable"] = nq
        call.summary = f"{len(res.get('fields') or [])} fields"
        return res

    def _with_named_fields(self, call: _Call, spec: ObjectSpec, ranked: list[FieldSpec], k: int,
                           query: str = "") -> tuple[list[FieldSpec], list[FieldSpec]]:
        """A top field that is blank on most records and whose meaning names other fields of the object ("the create
        flows do not set it; Round__c and Round_Info__c are the main round fields") brings those fields in right after
        it, with a note: otherwise the field the meaning points to is not among the rows.

        The first 5 rows are looked at, and every shown row (the first ``k``) whose label the query names
        (:func:`_label_fits_query`: "Interview Type" for "type"), those first. Such a field also moves its named fields
        that are listed further down up next to it (the result may be trimmed from the end), and its note marks the
        named fields whose label or meaning has a query word; for a field that only shares a word with the query
        ("Incentive Type" for "type") the note says that its named fields stand in for it only for a question about
        that field's label, with no mark."""
        out = list(ranked)
        added = 0
        inserted: list[FieldSpec] = []
        qwords = set(_toks(query)) - _object_words(spec)
        # the first 5 rows, and every shown row whose label the query names
        sparse = [f for i, f in enumerate(ranked[:k]) if _sparse(spec, f) and (i < 5 or _label_fits_query(spec, f, query))]
        sparse.sort(key=lambda f: 0 if _label_fits_query(spec, f, query) else 1)   # stable: rank order otherwise
        for f in sparse:
            if added >= 3:
                break
            named = _named_fields_of(spec, f)
            if not named:
                continue
            fits = _label_fits_query(spec, f, query)

            # the named fields whose label or meaning has a query word first ("type" -> "Type or focus of the round")
            def says(g: FieldSpec) -> str:
                if not fits:
                    return ""   # a shared word ("type" in "Incentive Type") says nothing about the named fields
                hit = sorted(qwords & set(_toks(f"{g.label} {g.meaning}")))
                return hit[0] if hit else ""

            named.sort(key=lambda g: 0 if says(g) else 1)
            named = named[:3 - added]
            mine: list[FieldSpec] = []   # inserted right after f, in the sorted order
            for g in named:
                shown = g in out[:k]
                if shown and (not fits or out.index(g) < out.index(f)):
                    continue   # listed already; a field the query names moves its named fields up next to it
                if g in out:
                    out.remove(g)
                out.insert(out.index(f) + 1 + len(mine), g)
                mine.append(g)
                if g not in inserted:
                    inserted.append(g)
                if not shown:
                    added += 1
            listed = ", ".join(f"{g.api} (filled {g.filled}" + (f"; its label or meaning says '{says(g)}'"
                                                                if says(g) else "") + ")" for g in named)
            if fits:
                call.note(f"{f.api} is filled on only {f.filled} of {spec.rows} {spec.api} records; its meaning names "
                          f"{listed}: use the field the meaning names when the question means the same thing")
            else:
                call.note(f"{f.api} ({f.label}) is filled on only {f.filled} of {spec.rows} {spec.api} records; its "
                          f"meaning names {listed}: they stand in for {f.api} only when the question asks about "
                          f"{f.label}, not when it only shares a word with it")
        return out, inserted

    def _field_row(self, call: _Call, spec: ObjectSpec, f: FieldSpec, rich: bool = True) -> dict:
        """One describe_fields row; ``rich`` rows carry value meanings and more values."""
        row: dict[str, Any] = {"field": f.api, "label": f.label, "type": f.type_text(),
                               "meaning": _short(f.meaning, 200 if rich else 120), "filled": f.filled}
        redirect = _redirect_text(spec, f, 200 if rich else 120)
        if redirect:
            row["redirect"] = redirect   # the meaning's "use X" clause that the shortened meaning cut off
        if f.kind in ("picklist", "multipicklist") and f.sensitivity is None:
            entry = self.catalog.values_of(spec.api, f.api)
            meanings = {normalize(v): m for v, _l, m in f.picklist}
            vals = []
            stored_norm = set()
            top, cap = (20, 30) if rich else (10, 14)
            if entry is not None:
                call.cached(entry.statement)
                for v, n in entry.values[:top]:
                    item = {"value": v, "records": n}
                    m = meanings.get(normalize(v))
                    if m and rich:
                        item["meaning"] = _short(m, 90)
                    elif not m and f.picklist:
                        item["legacy"] = True
                    vals.append(item)
                stored_norm = {normalize(v) for v, _ in entry.values}
            for v, _l, m in f.picklist:
                if normalize(v) not in stored_norm and len(vals) < cap:
                    item = {"value": v, "records": 0}
                    if m and rich:
                        item["meaning"] = _short(m, 90)
                    vals.append(item)
            row["values"] = vals
        if f.kind == "reference":
            cat = self.catalog
            row["lookup_to"] = f.ref_targets
            if _role(f):
                row["role"] = _role(f)
            if f.concept and f.ref_role != "undocumented":
                row["concept"] = _short(f.concept, 100)
            if f.from_target:
                row["from_target"] = f.from_target
            sibs = [g.api for g in cat.same_target_lookups(spec, f) if g.filled > 0 and
                    (f.api in SYSTEM_LOOKUPS or g.api not in SYSTEM_LOOKUPS)]
            if sibs:
                row["same_target"] = sibs[:8]
            if cat.almost_empty(spec, f):
                row["almost_empty"] = True
            if f.unresolved:
                row["points_to_missing"] = f.unresolved
            if f.mirror_of:
                row["mirror_of"] = f.mirror_of   # a copy of this path: group or filter by the path (the source)
        if f.legacy:
            row["legacy"] = True
        if f.replaced_by:
            row["replaced_by"] = f.replaced_by
        if not f.has_data:
            row["no_data"] = True
        if not f.documented:
            row["undocumented"] = True
        if f.sensitivity == "contact":
            row["contact_detail"] = True
        return row

    def _matching_values(self, spec: ObjectSpec, query: str) -> list[tuple[str, str, int, bool]]:
        """(field, stored value, records, in_picklist) of indexed values matching the query words."""
        qn = normalize(query)
        qtoks = set(_toks(query))
        out = []
        for (obj, fld), entry in self.catalog.values.items():
            if obj != spec.api:
                continue
            f = spec.fields.get(fld)
            if f is None or f.sensitivity:
                continue
            known = {normalize(v) for v, _l, _m in f.picklist}
            for v, n in entry.values:
                nv = normalize(v)
                vt = set(_toks(v))
                if (vt and vt <= qtoks) or (qn and len(qn) >= 3 and re.search(r"(^| )" + re.escape(qn) + r"( |$)", nv)):
                    out.append((fld, v, n, nv in known if f.picklist else False))
        out.sort(key=lambda t: -t[2])
        return out

    def _get_field_values(self, call: _Call, object: str, field: str, contains: str | None = None, limit: int = 30) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        ref = cat.resolve_path(spec, field)
        f = ref.field
        if f.sensitivity == "blocked":
            raise RecordError(blocked_text(ref.obj, f.api), "E2c")
        if f.sensitivity == "contact" and not self._contact_ok:   # phase 1: only when the question asks for them
            raise RecordError(contact_text(ref.obj, f.api), "E2d")
        if spec.sensitive and f.kind in ("text", "longtext", "name"):
            raise RecordError(sensitive_object_text(spec.api), "E1d")
        call.use(ref)
        scope = Scope(cat, spec)
        res: dict[str, Any] = {"field": ref.text, "kind": f.kind}
        if f.is_number or f.is_date or f.kind == "time":
            col = scope.col(ref)
            extra = f", AVG({col.expr}) AS avg" if f.is_number else ""
            st = call.run(Frag(f"SELECT MIN({col.expr}) AS min, MAX({col.expr}) AS max{extra}, COUNT({col.expr}) AS "
                               f"with_value, COUNT(*) AS n FROM {scope.from_sql()}"), max_rows=1)
            for t in scope.tables:
                call.table(t)
            row = dict(zip(st.columns, st.rows[0])) if st.rows else {}
            res["min"] = _cell(row.get("min"), f.kind)
            res["max"] = _cell(row.get("max"), f.kind)
            if f.is_number:
                res["avg"] = _round(row.get("avg"), 2)
                if f.kind == "percent":
                    call.note(f"{ref.text} is a percent stored as 0-100")
            res["records_with_value"] = row.get("with_value", 0)
            res["blank_records"] = (row.get("n") or 0) - (row.get("with_value") or 0)
            if contains:
                call.note(f"contains is ignored for {f.kind} fields")
            if not res["records_with_value"]:
                call.note("no record has a value in this field")
            call.summary = f"min {res['min']}, max {res['max']}"
            return res
        if f.kind in ("longtext", "id"):
            raise RecordError(f"{ref.text} is {f.kind}; its values cannot be listed. Use count_records with op contains, "
                              "or list_records.", "E7")
        # With contains the statement filters in SQL, so the evidence shows the statement that gave the values.
        entry = cat.values_of(ref.obj, f.api) if not ref.hops and not contains else None
        statuses = {normalize(v): v for v, _l, _m in f.picklist}
        meanings = {normalize(v): m for v, _l, m in f.picklist}
        multi = f.kind == "multipicklist"
        if contains:
            call.applied["contains"] = contains
        if f.kind == "reference":
            col = scope.col(ref, label=True)
            conds = []
            if contains:
                conds.append(Frag(f"{col.label_expr} ILIKE ? ESCAPE '\\'", (f"%{escape_like(contains)}%",)))
            st = call.run(group_sql(scope, conds, col, "count", None, order="desc", limit=1001), max_rows=1001)
            for t in scope.tables:
                call.table(t)
            call.main(st)
            target = next((t for t in f.ref_targets if t in cat.objects), None)
            groups = self._label_groups(call, st.rows, bool(col.id_expr), target, self._numbering(call, col, st.rows))
            blank = sum(g["value"] for g in groups if g["group"] == "(blank)")
            unresolved = sum(g["value"] for g in groups if g["group"] == "(unresolved id)")
            vals = [g for g in groups if g["group"] not in ("(blank)", "(unresolved id)")]
            res.update({"distinct_values": len(vals), "records_with_value": sum(g["value"] for g in vals) + unresolved,
                        "blank_records": blank, "unresolved": unresolved,
                        "values": [{"value": g["group"], "records": g["value"]} for g in vals[:limit]],
                        "truncated": len(vals) > limit or st.truncated})
            if unresolved:
                call.note(f"{unresolved} records point to a {target or 'related'} record that is not in the warehouse")
            call.summary = f"{len(vals)} values"
            return res
        filled = None  # multi-select: records with any value (a record holds several values)
        if entry is None:
            col = scope.col(ref)
            pat = Frag("ILIKE ? ESCAPE '\\'", (f"%{escape_like(contains)}%",)) if contains else None
            if not contains:
                st0 = call.run(Frag(f"SELECT approx_count_distinct({col.expr}) AS d FROM {scope.from_sql()}"), max_rows=1)
                d = int(st0.rows[0][0] or 0) if st0.rows else 0
                if d > 200 and not multi:
                    raise RecordError(f"{spec.api}.{ref.text} has {d} different values; pass contains to see matching ones.",
                                      "E7")
            if multi:  # one row per selected value; contains applies to each value, not to the stored combination
                body = (Frag(f"SELECT s.v AS value, COUNT(*) AS n FROM (SELECT unnest(string_split({col.expr}, ';')) AS v "
                             f"FROM {scope.from_sql()}) AS s")
                        + (Frag(" WHERE s.v ") + pat if pat else Frag(""))
                        + Frag(" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 501"))
            else:
                body = (Frag(f"SELECT {col.expr} AS value, COUNT(*) AS n FROM {scope.from_sql()}")
                        + where_sql([Frag(f"{col.expr} ") + pat] if pat else [])
                        + Frag(" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 501"))
            st = call.run(body, max_rows=501)
            call.main(st)
            for t in scope.tables:
                call.table(t)
            stored = [(_cell(v), int(n)) for v, n in st.rows if v is not None]
            blank = sum(int(n) for v, n in st.rows if v is None)
            truncated = st.truncated
            if multi and not contains:
                st1 = call.run(Frag(f"SELECT COUNT({col.expr}) AS filled, COUNT(*) AS n FROM {scope.from_sql()}"),
                               max_rows=1)
                filled, total = (int(st1.rows[0][0] or 0), int(st1.rows[0][1] or 0)) if st1.rows else (0, 0)
                blank = total - filled
        else:
            call.cached(entry.statement)
            stored = list(entry.values)
            blank = entry.blank
            truncated = entry.truncated
            if multi:
                filled = f.filled
        vals = []
        for v, n in stored:
            nv = normalize(v)
            if f.picklist:
                st_ = "current" if nv in statuses else "legacy"
            else:
                st_ = "unknown"
            item = {"value": v, "records": n, "status": st_}
            if meanings.get(nv):
                item["meaning"] = _short(meanings[nv], 100)
            if f.picklist and nv in statuses and statuses[nv] != v:
                call.note(f"knowledge spells it '{statuses[nv]}'; stored as '{v}'")
            vals.append(item)
        stored_norm = {normalize(v) for v, _ in stored}
        unused = [v for v, _l, _m in f.picklist if normalize(v) not in stored_norm] if not contains else []
        res.update({"distinct_values": len(vals), "records_with_value": sum(n for _v, n in stored),
                    "blank_records": blank if not contains else None, "values": vals[:limit],
                    "unused_picklist_values": unused, "truncated": truncated or len(vals) > limit})
        if multi:
            res["value_selections"] = res.pop("records_with_value")
            if filled is not None:
                res["records_with_value"] = filled
        if res["blank_records"] is None:
            res.pop("blank_records")
        if not unused:
            res.pop("unused_picklist_values")
        if multi:
            call.note("multi-select: a record with several values is counted once per value, so value_selections (the "
                      "sum of the value counts) is more than the records" + (" with a value (records_with_value)"
                                                                            if filled is not None else ""))
        call.summary = f"{len(vals)} values"
        return res


def _toks(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[a-z0-9]+", normalize(text)):
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        if len(tok) >= 3:
            out.append(tok)
    return out


def _object_words(spec: ObjectSpec) -> set[str]:
    """The words of the object's label, plural label and API name ("interview" for Interview__c)."""
    return set(_toks(f"{spec.label} {spec.plural_label} {split_api_name(spec.api)}"))


def _own_label_field(spec: ObjectSpec, f: FieldSpec, query: str) -> bool:
    """The field's label is the query's words, the object's own label words aside (the same set of words): the object's
    own field for the query ("Interview Type" on Interview__c, or "Type", for "type"; not "Incentive Type" or "Record
    Type ID", and not "Status" for "visa status", where the label holds only one of the query's words)."""
    obj = _object_words(spec)
    words = set(_toks(f.label)) - obj
    return bool(words) and words == set(_toks(query)) - obj


def _label_fits_query(spec: ObjectSpec, f: FieldSpec, query: str) -> bool:
    """Most of the field's label words, the object's own label words aside, are words of the query: the query asks about
    this field ("Interview Type" for "type", "Client Bill Rate" for "bill rate"), not about another field that only
    shares a word with it ("Incentive Type" for "type")."""
    obj = _object_words(spec)
    words = set(_toks(f.label)) - obj
    hit = words & (set(_toks(query)) - obj)
    return bool(hit) and len(hit) * 2 > len(words)


def _sparse(spec: ObjectSpec, f: FieldSpec) -> bool:
    """Filled on fewer than half of the object's records."""
    return spec.rows > 0 and f.filled * 2 < spec.rows


def _named_fields_of(spec: ObjectSpec, f: FieldSpec) -> list[FieldSpec]:
    """The fields of the object that ``f``'s meaning names and that hold more data than ``f`` (not blocked), in the
    meaning's order."""
    named: list[FieldSpec] = []
    for api in dict.fromkeys(_FIELD_NAME_RE.findall(str(f.meaning or ""))):
        g = spec.fields.get(api) or next((x for a, x in spec.fields.items() if a.lower() == api.lower()), None)
        if g is None or g is f or g.sensitivity == "blocked" or not g.has_data or g.filled <= f.filled or g in named:
            continue
        named.append(g)
    return named


#: A clause that sends the reader to another field ("use Round_Info__c for ...", "the type ... is Round_Info__c",
#: "replaced by X", "the live value is the formula X", "fills X instead"): a redirect verb within a few words before the
#: API name, "is" / "are" right before it ("is X", "is held in X", "is the formula X"; not "is below X" or "fills it
#: from X"), or "instead" right after it.
_REDIRECT_VERB_RE = re.compile(r"\b(?:use|uses|see|replaced\s+by|instead\s+of|holds?)\b", re.IGNORECASE)
_REDIRECT_IS_RE = re.compile(r"\b(?:is|are)\s+(?:(?:held|kept|stored|recorded)\s+in\s+|in\s+|the\s+(?:[a-z]+\s+){0,2})?"
                             r"[`*]*$", re.IGNORECASE)
_REDIRECT_AFTER_RE = re.compile(r"^[`*)]*\s+instead\b", re.IGNORECASE)
_CLAUSE_CUT_RE = re.compile(r"(?<=[:;])\s+|,\s+(?=(?:so|but|and\s+so)\b)")


def _redirect_text(spec: ObjectSpec, f: FieldSpec, n: int) -> str:
    """The sentence (or, when that is long, the clause) of a mostly blank field's meaning that sends the reader to a
    field the meaning names (:func:`_named_fields_of`: "use Round_Info__c for the type of a client round"), when the
    meaning shortened to ``n`` characters does not show it; else ''. At most 160 characters."""
    if not _sparse(spec, f):
        return ""
    text = " ".join(str(f.meaning or "").split())
    if len(text) <= n:
        return ""
    apis = [g.api for g in _named_fields_of(spec, f)]
    if not apis:
        return ""
    api_re = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(a) for a in apis) + r")(?![A-Za-z0-9_])")

    def redirects(s: str) -> bool:   # a redirect verb next to one of the named fields
        return any(_REDIRECT_VERB_RE.search(s[max(0, m.start() - 40):m.start()]) or _REDIRECT_IS_RE.search(s[:m.start()])
                   or _REDIRECT_AFTER_RE.match(s[m.end():]) for m in api_re.finditer(s))

    sents = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if api_re.search(s)]
    # only a sentence that sends the reader to the named field: "kept separate from X" names X but is no redirect
    pick = next((s for s in sents if redirects(s)), "")
    if len(pick) > 160:
        parts = [p for p in _CLAUSE_CUT_RE.split(pick) if p.strip()]
        pick = next((p for p in parts if redirects(p)), pick)
        pick = re.sub(r"^(?:so|but|and\s+so)\s+", "", pick.strip())
        pick = pick[:1].upper() + pick[1:]
    if not pick or pick.rstrip(".") in _short(text, n):
        return ""
    return _short(pick, 160)
