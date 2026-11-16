"""``get_field_history`` and ``get_sync_status``.

Moved verbatim from ``okrec/tools.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sf_assistant.engine.catalog import (BLOCKED_COLUMN_RE, CONTACT_COLUMN_RE, FieldSpec, PathRef, RecordError,
                                         blocked_text, contact_text, norm_name)
from sf_assistant.engine.query_builder import Col, Frag, name_norm_sql, period_frag, sensitive_object_text, where_sql
from sf_assistant.engine.values import escape_like, normalize

from sf_assistant.tools.record_base import _cell, _Call


class MetaMixin:
    """History and sync status tools of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    def _get_field_history(self, call: _Call, object: str, record: str | None = None, field: str | None = None,
                           changed_to: str | None = None, period: str | None = None, count_by: str | None = None,
                           limit: int = 20) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        if spec.sensitive:
            raise RecordError(sensitive_object_text(spec.api), "E1d")
        if not spec.history_table:
            raise RecordError(f"{spec.api} has no field-history table; changes over time are not recorded. Do not infer "
                              "them from LastModifiedDate.", "E7")
        h, parent = spec.history_table, spec.history_parent
        hq = f"main.\"{h}\""
        call.table(h)
        blocked = [f.api for f in spec.fields.values() if f.sensitivity == "blocked"]
        # contact fields' old and new values: hidden unless the question asks for contact details (phase 1); the SQL
        # itself selects '(hidden)' for them (_hist_value), so no statement row holds one (review F1, F4)
        contact = [] if self._contact_ok else [f.api for f in spec.fields.values() if f.sensitivity == "contact"]
        base: list[Frag] = [Frag("h.\"DataType\" IS DISTINCT FROM 'EntityId'")]
        if blocked:
            base.append(Frag("NOT (h.\"Field\" = ANY(?))", (blocked,)))
        tracked_sql = (Frag(f"SELECT h.\"Field\" AS field, COUNT(*) AS changes, MIN(h.\"CreatedDate\") AS first, "
                            f"MAX(h.\"CreatedDate\") AS last FROM {hq} AS h"), Frag(" GROUP BY 1 ORDER BY 2 DESC, 1"))
        st_t = call.run(tracked_sql[0] + where_sql(base) + tracked_sql[1], max_rows=500)
        # history rows of a blocked field that is no column of the table (deleted, renamed or not synced) are left
        # out too: their OldValue / NewValue would show the secret. The table's fields decide the SQL above, so on a
        # history table without such a row every statement stays as it was.
        extra = sorted({str(r[0]) for r in st_t.rows if r[0] and str(r[0]) not in blocked
                        and BLOCKED_COLUMN_RE.search(str(r[0]))})
        if extra:
            blocked = blocked + extra
            base = [base[0], Frag("NOT (h.\"Field\" = ANY(?))", (blocked,))]
            st_t = call.run(tracked_sql[0] + where_sql(base) + tracked_sql[1], max_rows=500)
        if not self._contact_ok:   # the same for a contact field that is no column of the table
            contact += sorted({str(r[0]) for r in st_t.rows if r[0] and str(r[0]) not in spec.fields
                               and CONTACT_COLUMN_RE.search(str(r[0]))} - set(contact))
        tracked = [{"field": "record created" if r[0] == "created" else r[0], "changes": int(r[1])} for r in st_t.rows]
        firsts = [r[2] for r in st_t.rows if r[2]]
        lasts = [r[3] for r in st_t.rows if r[3]]
        coverage = {"from": (min(firsts)[:16] + " UTC") if firsts else None, "to": (max(lasts)[:16] + " UTC") if lasts else None}
        conds = list(base)
        applied: dict[str, Any] = {}
        if record:
            nx = name_norm_sql('x."Name"')
            st_r = call.run(Frag(f"SELECT COUNT(*) AS n FROM main.\"{spec.api}\" AS x WHERE {nx} = ?", (norm_name(record),)),
                            max_rows=1)
            n = int(st_r.rows[0][0]) if st_r.rows else 0
            if not n:
                st_c = call.run(Frag(f"SELECT x.\"Name\" AS name FROM main.\"{spec.api}\" AS x WHERE x.\"Name\" ILIKE ? "
                                     "ESCAPE '\\' GROUP BY 1 ORDER BY 1 LIMIT 3", (f"%{escape_like(record)}%",)), max_rows=3)
                close = [str(r[0]) for r in st_c.rows]
                raise RecordError(f"no {spec.api} record named '{record}'." + (f" Close: {', '.join(close)}." if close else ""),
                                  "E7")
            if n > 1:
                call.note(f"{n} {spec.api} records are named '{record}'; changes of all of them are shown")
            conds.append(Frag(f"h.\"{parent}\" IN (SELECT x.\"Id\" FROM main.\"{spec.api}\" AS x WHERE {nx} = ?)",
                              (norm_name(record),)))
            applied["record"] = record
        fld_api = None
        if field:
            if field.strip().lower() in ("created", "record created", "creation"):
                fld_api = "created"
            else:
                fref = cat.field(spec, field)
                if fref.sensitivity == "blocked":
                    raise RecordError(blocked_text(spec.api, fref.api), "E2c")
                fld_api = fref.api
                call.use(PathRef(fref.api, [], spec.api, fref))
            if not any(r[0] == fld_api for r in st_t.rows):
                listed = ", ".join(f"{t['field']} ({t['changes']})" for t in tracked[:15]) or "none"
                raise RecordError(f"{fld_api} has no history rows in {h}. Tracked fields: {listed}", "E7")
            conds.append(Frag("h.\"Field\" = ?", (fld_api,)))
            applied["field"] = "record created" if fld_api == "created" else fld_api
        if changed_to:
            if not fld_api or fld_api == "created":
                raise RecordError("changed_to needs field (the tracked field whose new value it is).", "E11c")
            if fld_api in contact:   # the stored values of a contact field are not read for this question
                raise RecordError(contact_text(spec.api, fld_api), "E2d")
            st_v = call.run(Frag(f"SELECT h.\"NewValue\" AS value, COUNT(*) AS n FROM {hq} AS h") +
                            where_sql(base + [Frag("h.\"Field\" = ?", (fld_api,))]) +
                            Frag(" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 501"), max_rows=501)
            stored = [(str(v), int(n)) for v, n in st_v.rows if v is not None]
            ns = normalize(changed_to)
            bound = [v for v, _ in stored if v == changed_to] or [v for v, _ in stored if normalize(v) == ns]
            if not bound:
                listed = ", ".join(f"{_cell(v)} {n}" for v, n in stored[:12])
                raise RecordError(f"'{changed_to}' is not a new value recorded for {fld_api}. Recorded new values "
                                  f"(changes): {listed or 'none'}. Use one of them exactly.", "E4")
            conds.append(Frag("h.\"NewValue\" = ANY(?)", (bound,)))
            applied["changed_to"] = bound[0] if len(bound) == 1 else bound
        if period:
            per = self._period(period, call.ctx.today)
            hist_field = FieldSpec(api="CreatedDate", label="CreatedDate", kb_type=None, db_type="TIMESTAMP",
                                   kind="datetime", meaning="when the change was recorded", has_data=True, system=True)
            hscope = _HistScope(h)
            pf, pa = period_frag(hscope, PathRef("CreatedDate", [], h, hist_field), per, call.ctx)  # type: ignore[arg-type]
            conds.append(pf)
            pa["date_field"] = "change time (history CreatedDate)"
            applied["period"] = pa
        call.applied.update(applied)
        call.table("User")
        frm = (f"{hq} AS h LEFT JOIN main.\"User\" AS u ON u.\"Id\" = h.\"CreatedById\" LEFT JOIN main.\"{spec.api}\" AS p "
               f"ON p.\"Id\" = h.\"{parent}\"")
        res: dict[str, Any] = {"history_table": h, "tracked_fields": tracked[:30], "coverage": coverage}
        if count_by:
            if count_by == "month":
                head = Frag("SELECT strftime(CAST(date_trunc('month', (h.\"CreatedDate\" AT TIME ZONE 'UTC') AT TIME ZONE ?) "
                            "AS DATE), '%Y-%m') AS grp, COUNT(*) AS value", (call.ctx.tz,))
                order = " GROUP BY 1 ORDER BY 1"
            else:
                key = (_hist_value("NewValue", contact) if count_by == "new_value" else
                       Frag(str({"field": "h.\"Field\"", "changed_by": "u.\"Name\""}.get(count_by))))
                head = Frag("SELECT COALESCE(CAST(") + key + Frag(" AS VARCHAR), '(blank)') AS grp, COUNT(*) AS value")
                order = " GROUP BY 1 ORDER BY 2 DESC, 1"
            st = call.run(head + Frag(f" FROM {frm}") + where_sql(conds) + Frag(order + " LIMIT 1001"), max_rows=1000)
            call.main(st)
            groups = []
            for g, v in st.rows:
                if g == "created" and count_by == "field":
                    g = "record created"
                groups.append({"group": _cell(g), "value": int(v)})
            res["count_by"] = count_by
            res["groups"] = groups[:50]
            res["total_count"] = sum(g["value"] for g in groups)
            res["truncated"] = len(groups) > 50 or st.truncated
            call.summary = f"{len(groups)} groups"
            return res
        st = call.run(Frag("SELECT h.\"CreatedDate\" AS changed_at, p.\"Name\" AS record, h.\"Field\" AS field, ") +
                      _hist_value("OldValue", contact) + Frag(" AS old, ") + _hist_value("NewValue", contact) +
                      Frag(f" AS new, u.\"Name\" AS changed_by FROM {frm}") + where_sql(conds) +
                      Frag(f" ORDER BY h.\"CreatedDate\" DESC, h.\"Id\" LIMIT {limit + 1}"), max_rows=limit + 1)
        call.main(st)
        changes = []
        for r in st.rows[:limit]:
            when, rec, fld, old, new, by = r
            changes.append({"when": _cell(when, "datetime"), "record": _cell(rec),
                            "field": "record created" if fld == "created" else fld,
                            "old": _cell(old), "new": _cell(new), "changed_by": _cell(by)})
        total = len(changes)
        if len(st.rows) > limit:
            st2 = call.run(Frag(f"SELECT COUNT(*) AS n FROM {frm}") + where_sql(conds), max_rows=1)
            total = int(st2.rows[0][0]) if st2.rows else total
        res["changes"] = changes
        res["total_count"] = total
        res["truncated"] = total > len(changes)
        if not changes:
            call.note("no recorded change matches")
        call.summary = f"{len(changes)} of {total} changes"
        return res

    def _get_sync_status(self, call: _Call, objects: list | None = None) -> dict:
        cat = self.catalog
        st = call.run(Frag('SELECT object_name, updated_at FROM raw."_sync_meta"'), max_rows=5000)
        status = self.status()
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        rows = []
        if objects:
            specs = [cat.resolve_object(o) for o in objects]
        else:
            q = [s for s in cat.queryable() if cat.sync_dt(s.api) is not None]
            q.sort(key=lambda s: cat.sync_dt(s.api))
            specs = q[:10]
        for s in specs:
            ts = cat.sync_dt(s.api)
            rows.append({"object": s.api, "synced_at": cat.synced_at(s.api),
                         "age_hours": round((now - ts).total_seconds() / 3600.0, 1) if ts else None, "records": s.rows})
        q_all = [cat.sync_dt(s.api) for s in cat.queryable() if cat.sync_dt(s.api) is not None]
        sb = self.org.get("IsSandbox")
        origin = status.origin + (f" (Organization.IsSandbox = {'true' if sb else 'false'})" if sb is not None else "")
        if not objects:
            call.note("without objects, the 10 queryable objects with the oldest syncs are listed")
        call.summary = f"{len(rows)} objects"
        return {"snapshot": {"file": status.snapshot_path, "copied_at": status.copied_at_utc, "origin": origin},
                "objects": rows, "newest_sync": status.newest_sync_utc,
                "oldest_sync_of_queryable_objects": min(q_all).strftime("%Y-%m-%d %H:%M UTC") if q_all else None,
                "not_recorded": ["sync failures", "API usage", "batch history"], "rows_in_sync_meta": st.row_count}


def _hist_value(col: str, contact: list[str]) -> Frag:
    """``h."OldValue"`` / ``h."NewValue"``, or, when the question does not ask for contact details and the object has
    contact fields, the same with '(hidden)' for the rows of those fields (a blank value stays blank)."""
    if not contact:
        return Frag(f"h.\"{col}\"")
    return Frag(f"CASE WHEN h.\"Field\" = ANY(?) AND h.\"{col}\" IS NOT NULL THEN '(hidden)' ELSE h.\"{col}\" END",
                (list(contact),))


class _HistScope:
    """Minimal scope for the history table alias ``h`` (period bounds only)."""

    def __init__(self, table: str) -> None:
        self.alias = "h"
        self.spec = type("S", (), {"api": table, "date_fields": lambda self: []})()

    def col(self, ref: PathRef, *, label: bool = False) -> Col:
        return Col(f"h.\"{ref.field.api}\"", ref)
