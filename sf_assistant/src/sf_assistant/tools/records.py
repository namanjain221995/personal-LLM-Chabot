"""The record query tools: count, aggregate, group, trend, compare, percentage, list and search records.

Moved verbatim from ``okrec/tools.py`` (with the status-field helpers). list_records also gives the people that child
rows of the listed records hold in the role of a listed column (``child_people``, docs/people.md item 7f).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from typing import Any

from sf_assistant.knowledge.store import split_api_name
from sf_assistant.engine.catalog import (BLOCKED_COLUMN_RE, PEOPLE_SCAN_SKIP, PERSON_OBJECTS, FieldSpec, ObjectSpec,
                                         PathRef, RecordError, blocked_text, norm_key, norm_name)
from sf_assistant.engine.periods import GRAIN_CAPS, Period, bucket_end, bucket_label, bucket_sequence, bucket_start
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.engine.query_builder import (Frag, Scope, aggregate_sql, count_sql, date_fields_text, filter_frag,
                                               group_measure_expr, group_sql, join_frags, list_sql, measure_expr,
                                               name_norm_sql, percentage_sql, period_frag, rate_trend_sql,
                                               sensitive_object_text, trend_sql, where_sql)
from sf_assistant.engine.values import escape_like, is_record_number

from sf_assistant.tools import record_base as _base   # the limit SCAN_BUDGET_S: read (and patched) there
from sf_assistant.tools.record_base import _ROLE_WORDS, _cell, _disp, _round, _Call, _role


_STATUS_ROLES = ("business_status", "status", "outcome", "lifecycle_state")
#: Knowledge roles of a date field that is never the default date column of list_records.
_DEFAULT_DROP_ROLES = frozenset({"contact_attribute"})
CHILD_PEOPLE_MAX = 2      # child objects per list_records call whose rows hold people in a listed column's role
#: The most stored values a status typed text in the catalog may have (:meth:`RecordTools._status_field`).
STATUS_TEXT_MAX_VALUES = 30


def _put_split(d: dict, key: str, value: Any, measure: str) -> None:
    """Two stored values can hide to one split key: counts and sums add, any other measure reads None."""
    if key not in d:
        d[key] = value
    elif measure in ("count", "sum"):
        d[key] = (d[key] or 0) + (value or 0)
    else:
        d[key] = None


def _pct(part: float, base: float) -> float | None:
    if not base:
        return None
    p = part * 100.0 / base
    return round(p, 2) if abs(p) < 1 else round(p, 1)


class RecordQueryMixin:
    """Record query tools of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    def _count_records(self, call: _Call, object: str, filters: list | None = None, any_of: list | None = None,
                       date_field: str | None = None, period: str | None = None, related: list | None = None,
                       count_distinct: str | None = None) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        args = {"filters": filters, "any_of": any_of, "date_field": date_field, "period": period, "related": related}
        conds = self._where(call, spec, scope, args, lookup_check=True)
        dcol = None
        counted = f"{spec.api} records"
        if count_distinct:
            ref = cat.resolve_path(spec, count_distinct)
            self._check_use(spec, ref, "count")
            call.use(ref)
            dcol = scope.col(ref)
            if ref.field.kind == "reference":
                tgt = "/".join(ref.field.ref_targets) or "related"
                t1 = cat.target_of(ref.field)
                tlabel = cat.label_of(t1) if t1 else tgt
                counted = (f"different {ref.text} values (records with a value): each a different {tlabel} record "
                           f"({_role(ref.field)})")
                call.note(f"counts different {tgt} records referenced by {ref.text} (by Id)")
                alt = self._distinct_alternative(cat.objects.get(ref.obj) or spec, ref.field)
                if alt is not None:
                    t2 = cat.target_of(alt)
                    call.note(f"{ref.text} holds the {_role(ref.field)} ({tlabel}), not the {alt.label.lower()} "
                              f"itself: different {alt.label.lower()} records are counted with count_distinct="
                              f"'{alt.api}' ({cat.label_of(t2) if t2 else 'its target'})")
            else:
                counted = f"different {ref.text} values (records with a value)"
            for t in scope.tables:
                call.table(t)
        st = call.run(count_sql(scope, conds, dcol), max_rows=1)
        call.main(st)
        call.main_scope, call.main_conds = scope, conds
        n = int(st.rows[0][0]) if st.rows else 0
        self._base_notes(call, spec)
        if not count_distinct:
            self._record_type_note(call, spec, args)
        self._future_note(call)
        call.summary = str(n)
        out: dict[str, Any] = {"count": n, "counted": counted}
        if n > 0 and not count_distinct and self._person_filtered(call):
            split = self._count_breakdown(call, spec, scope, conds)
            if split:
                out["breakdown"] = split
                call.note("breakdown: the same records per value of the status and the role or purpose field (each "
                          "field's values add up to the count); a status split tells assigned from finished records")
        return out

    def status_values(self, objects: list[str]) -> list[str]:
        """The stored values and labels of the status field (:meth:`_status_field`, knowledge picklist) of each of
        these objects: the labels a status breakdown of their records can use (the session's breakdown check)."""
        out: list[str] = []
        for o in objects or []:
            spec = self.catalog.objects.get(str(o))
            f = self._status_field(spec) if spec is not None else None
            if f is not None:
                out += self._status_labels(spec, f)
        return list(dict.fromkeys(out))

    @staticmethod
    def _person_filtered(call: _Call) -> bool:
        """The call filters a lookup to a person object on a stored person (a count of one person's records)."""
        return any(chk.field is not None and chk.positive and any(t in PERSON_OBJECTS for t in chk.targets)
                   and any(chk.found.get(t) for t in chk.targets) for chk, _out, base in call.name_checks if base)

    def _count_breakdown(self, call: _Call, spec: ObjectSpec, scope: Scope, conds: list[Frag]) -> dict[str, dict]:
        """A person count's records per value of the object's status field and of its role or purpose picklist (the
        field about_people groups by): ``{"<Field>": {"<value>": n, ..., "(other)": n}}``, at most 8 values each, one
        bound statement per field. Empty when the object has neither or the statement fails."""
        cat = self.catalog
        fields: list[FieldSpec] = []
        st_f = self._status_field(spec)
        if st_f is not None:
            fields.append(st_f)
        pick = [g for g in spec.fields.values() if g.kind == "picklist" and g.has_data and not g.sensitivity
                and not g.legacy and any(w in _ROLE_WORDS for w in split_api_name(g.api).split())]
        if pick:
            pf = max(pick, key=lambda g: (g.filled, g.api))
            if all(pf.api != f.api for f in fields):
                fields.append(pf)
        out: dict[str, dict] = {}
        for f in fields:
            try:
                col = scope.col(cat.resolve_path(spec, f.api))
                st = call.run(Frag(f"SELECT COALESCE(CAST({col.expr} AS VARCHAR), '(blank)') AS v, COUNT(*) AS n "
                                   f"FROM {scope.from_sql()}") + where_sql(conds)
                              + Frag(" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 50"), max_rows=50)
            except RecordError:
                continue
            by = {str(v): int(k) for v, k in st.rows[:8]}
            rest = sum(int(k) for _v, k in st.rows[8:])
            if rest:
                by["(other)"] = rest
            if by:
                out[f.api] = by
                call.use(cat.resolve_path(spec, f.api))
        return out

    def _aggregate_records(self, call: _Call, object: str, op: str, field: str, filters: list | None = None,
                           any_of: list | None = None, date_field: str | None = None, period: str | None = None,
                           related: list | None = None) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        ref = cat.resolve_path(spec, field)
        self._check_use(spec, ref, "measure")
        f = ref.field
        if op in ("sum", "avg", "median") and not f.is_number:
            raise RecordError(self._e6(op, spec, ref), "E6")
        if op in ("min", "max") and not (f.is_number or f.is_date):
            raise RecordError(self._e6(op, spec, ref), "E6")
        call.use(ref)
        col = scope.col(ref)
        conds = self._where(call, spec, scope, {"filters": filters, "any_of": any_of, "date_field": date_field,
                                                "period": period, "related": related}, lookup_check=True)
        st = call.run(aggregate_sql(scope, conds, op, col), max_rows=1)
        call.main(st)
        call.main_scope, call.main_conds = scope, conds
        v, with_value, matched = st.rows[0] if st.rows else (None, 0, 0)
        if op in ("avg", "median") and v is not None:
            v = round(float(v), 2)
        elif isinstance(v, float):
            v = round(v, 4)
        if f.kind == "datetime" and isinstance(v, str):
            v = v[:16] + " UTC"
        if f.kind == "percent":
            call.note(f"{ref.text} is a percent stored as 0-100")
        if v is None:
            call.note("no matching record has a value in this field")
        self._base_notes(call, spec)
        self._future_note(call)
        call.summary = str(v)
        return {"op": op, "field": ref.text, "value": v, "records_with_value": with_value,
                "records_without_value": int(matched or 0) - int(with_value or 0), "records_matched": matched}

    def _group_records(self, call: _Call, object: str, group_by: str, split_by: str | None = None, measure: str = "count",
                       measure_field: str | None = None, filters: list | None = None, any_of: list | None = None,
                       date_field: str | None = None, period: str | None = None, related: list | None = None,
                       top: int | None = None, order: str = "desc", min_value: float | None = None) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        gcol = self._group_col(call, spec, scope, group_by, "group")
        scol = None
        if split_by:
            sref = cat.resolve_path(spec, split_by)
            if sref.text == gcol.ref.text or (sref.obj == gcol.ref.obj and sref.field.api == gcol.field.api
                                              and len(sref.hops) == len(gcol.ref.hops)):
                raise RecordError("split_by must be a different field from group_by.", "E7")
            scol = self._group_col(call, spec, scope, split_by, "split")
            if gcol.multi and scol.multi:
                raise RecordError(f"{gcol.display} and {scol.display} are both multi-select fields; use one of them as "
                                  "group_by and filter the other (op contains).", "E7")
        mcol = self._measure(call, spec, scope, measure, measure_field)
        conds = self._where(call, spec, scope, {"filters": filters, "any_of": any_of, "date_field": date_field,
                                                "period": period, "related": related}, lookup_check=True)
        having = (Frag(f"{group_measure_expr(measure, mcol, gcol)} >= ?", (Decimal(str(min_value)),))
                  if min_value is not None else None)
        st = call.run(group_sql(scope, conds, gcol, measure, mcol, None, having, order, 1001), max_rows=1000)
        call.main(st)
        call.main_scope, call.main_conds = scope, conds
        call.before_having = having is not None
        target = next((t for t in gcol.field.ref_targets if t in cat.objects), None) if gcol.field.kind == "reference" else None
        numbering = self._numbering(call, gcol, st.rows)
        groups = self._label_groups(call, st.rows, bool(gcol.id_expr), target, numbering)
        rev = order != "asc"
        groups.sort(key=lambda g: ((g["value"] is None), (-(g["value"] or 0) if rev else (g["value"] or 0)), str(g["group"])))
        total = None
        matched = None
        if gcol.multi:  # groups overlap: report the records, not a sum of the groups
            st2 = call.run(count_sql(scope, conds), max_rows=1)
            matched = int(st2.rows[0][0]) if st2.rows else 0
            call.note(f"{gcol.display} is multi-select: a record with several values is counted once in the group of "
                      f"each value, so the groups add up to more than the {matched} matching records")
        elif measure == "count":
            total = sum(int(g["value"] or 0) for g in groups)
            if st.truncated:
                st2 = call.run(count_sql(scope, conds), max_rows=1)
                total = int(st2.rows[0][0]) if st2.rows else total
        unresolved = sum(int(g["value"] or 0) for g in groups if g["group"] == "(unresolved id)") if measure == "count" else 0
        special = ("(blank)", "(unresolved id)")
        group_count = sum(1 for g in groups if g["group"] not in special)  # groups with a value: "N departments"
        blank = next((g["value"] for g in groups if g["group"] == "(blank)"), None)
        ranked = groups
        ties: list[str] = []
        n_ties = 0
        if top:
            ranked = [g for g in groups if g["group"] not in special]
            kept = ranked[:top]
            if len(ranked) > top and kept:
                last = kept[-1]["value"]
                tied = [g["group"] for g in ranked[top:] if g["value"] == last]
                n_ties, ties = len(tied), tied[:5]
                if n_ties:
                    listed = ", ".join(map(str, ties))
                    call.note(f"{n_ties} more group(s) have the same value as the last one shown ({last}): "
                              + (listed if n_ties <= 5 else f"{listed} and {n_ties - 5} more"))
            ranked = kept
            if any(g["group"] == "(blank)" for g in groups):
                call.note("the (blank) group is left out of the ranking")
        shown = ranked[:50]
        if len(ranked) > 50:
            call.note(f"showing 50 of {len(ranked)} groups")
        split_totals = None
        if scol is not None:
            st_s = call.run(group_sql(scope, conds, gcol, measure, mcol, scol, None, order, 5001), max_rows=5000)
            totals = self._attach_split(call, shown, st_s.rows, bool(gcol.id_expr), measure, st.rows, numbering)
            if measure in ("count", "sum"):
                split_totals = self._split_totals(totals, measure)
            if scol.multi:
                call.note(f"split_by {scol.display} is multi-select: in 'by' a record with several values is counted "
                          "once per value")
        self._lookup_col_notes(call, gcol)
        if scol is not None:
            self._lookup_col_notes(call, scol)
        if unresolved and not gcol.field.unresolved:
            call.note(f"{unresolved} records point to a {target or 'related'} record that is not in the warehouse "
                      "('(unresolved id)')")
        if measure == "count" and blank:
            whole = total if total is not None else matched
            if whole and blank * 2 > whole:
                call.note(f"{gcol.display} is blank on {blank} of the {whole} matching records; if the question means "
                          "another field, describe_fields shows how many records each field fills (filled)")
        self._base_notes(call, spec)
        self._future_note(call)
        for t in scope.tables:
            call.table(t)
        res: dict[str, Any] = {"grouped_by": self._grouped_by_text(cat, gcol), "measure": measure}
        if measure != "count":
            res["measure_field"] = mcol.display if mcol else None
        if scol is not None:
            res["split_by"] = self._grouped_by_text(cat, scol)
        res.update({"groups": shown, "group_count": group_count, "shown": len(shown)})
        if total is not None:
            res["total_across_groups"] = total
        if matched is not None:
            res["records_matched"] = matched
        if blank is not None:  # the (blank) group is not counted in group_count
            res["blank_group_value"] = blank
        res["unresolved_ids"] = unresolved
        res["ties_at_cutoff"] = ties  # at most 5 names
        res["ties_at_cutoff_count"] = n_ties
        if split_totals is not None:
            res["split_totals"] = split_totals
        stl = [x for x in (self._same_target_key(gcol), self._same_target_key(scol) if scol is not None else None) if x]
        if stl:
            res["same_target_lookups"] = stl
        call.summary = f"{group_count} groups" + (f", top {len(shown)}" if top else "")
        return res

    def _attach_split(self, call: _Call, shown: list[dict], rows: list[list], has_gid: bool, measure: str,
                      plain_rows: list[list], numbering: dict | None = None) -> dict[str, float]:
        """Add 'by' to each shown group: the 10 most frequent split values, the rest as '(other)'. Returns the totals
        per split value over every row of the split statement."""
        # Map gid/label of the plain rows to the shown labels.
        label_of: dict[Any, str] = {}
        tmp_call_notes = list(call.notes)
        labelled = self._label_groups(call, plain_rows, has_gid, None, numbering)
        call.notes[:] = tmp_call_notes
        for r, g in zip(plain_rows, labelled):
            key = (r[0], r[1]) if has_gid else r[0]
            label_of[key] = g["group"]
        per: dict[str, dict[str, Any]] = {}
        totals: dict[str, float] = {}
        for r in rows:
            if has_gid:
                gid, grp, split, value = r
                key = (gid, grp)
            else:
                grp, split, value = r
                key = grp
            label = label_of.get(key, str(grp))
            split = "(blank)" if split is None else _cell(str(split))
            _put_split(per.setdefault(label, {}), split, value, measure)
            totals[split] = totals.get(split, 0) + (float(value) if isinstance(value, (int, float, Decimal)) else 0)
        keep = [s for s, _ in sorted(totals.items(), key=lambda t: (-t[1], t[0]))[:10]]
        for g in shown:
            d = per.get(str(g["group"]), {})
            by = {s: _round(d[s], 2) for s in keep if s in d}
            if measure == "count":
                other = sum(int(v or 0) for s, v in d.items() if s not in keep)
                if other:
                    by["(other)"] = other
            g["by"] = by
        if len(totals) > 10:
            call.note(f"split_by has {len(totals)} values; the 10 most frequent are shown, the rest as '(other)'")
        return totals

    def _trend_records(self, call: _Call, object: str, grain: str, date_field: str, period: str,
                       split_by: str | None = None, measure: str = "count", measure_field: str | None = None,
                       filters: list | None = None, any_of: list | None = None, related: list | None = None,
                       part_filters: list | None = None) -> dict:
        """Measure per bucket. With ``part_filters`` (placement fixes, 2026-11-16) a rate per bucket instead: each
        bucket gives ``part``, ``base`` and ``percent`` (``value`` = percent) from one statement
        (:func:`rate_trend_sql`), with the window's own part, base and percent; a bucket with no base record has no
        percent (null, never 0%)."""
        if part_filters:
            if split_by or measure not in (None, "count") or measure_field:
                raise RecordError("part_filters gives a rate per bucket (part / base x 100) of record counts: leave "
                                  "split_by, measure and measure_field out.", "E8")
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        today = call.ctx.today
        dref = self._date_ref(spec, date_field)
        per = self._period(period, today)
        if per.start is None or (per.end_exclusive is None and per.start >= today and per.label.strip().lower() in
                                 ("upcoming", "future", "in the future")):
            raise RecordError("a trend needs a bounded period such as 'this year' or 'last 6 months'.", "E7")
        today_end = bucket_end(grain, today)
        if per.start > today:
            if per.end_exclusive is None:
                raise RecordError("a trend needs a bounded period such as 'this year' or 'last 6 months'.", "E7")
            end = per.end_exclusive
        else:
            end = min(per.end_exclusive, today_end) if per.end_exclusive else today_end
        clipped = Period(per.start, end, per.label)
        seq = bucket_sequence(grain, clipped.start, clipped.end_exclusive)
        cap = GRAIN_CAPS[grain]
        if len(seq) > cap:
            raise RecordError(f"a {grain} trend over '{period}' gives {len(seq)} buckets (max {cap}); use grain=week or "
                              "month." if grain in ("day", "week") else
                              f"a {grain} trend over '{period}' gives {len(seq)} buckets (max {cap}); use a longer grain "
                              "or a shorter period.", "E7")
        if part_filters:
            return self._rate_trend(call, spec, scope, grain, dref, per, clipped, seq, today, end, filters, any_of,
                                    related, part_filters)
        mcol = self._measure(call, spec, scope, measure, measure_field)
        scol = None
        trend_split_totals = None
        if split_by:
            scol = self._group_col(call, spec, scope, split_by, "split")
        trend_conds: list[Frag] = []
        base_conds = self._where(call, spec, scope, {"filters": filters, "any_of": any_of, "related": related},
                                 use_period=False, result_conds=trend_conds, lookup_check=True)
        pf, pa = period_frag(scope, dref, clipped, call.ctx)
        trend_conds.extend(base_conds + [pf])
        call.applied["period"] = pa
        call.use(dref)
        dcol = scope.col(dref)
        st = call.run(trend_sql(scope, base_conds + [pf], dcol, grain, call.ctx, measure, mcol), max_rows=1000)
        call.main(st)
        call.main_scope, call.main_conds = scope, base_conds + [pf]
        got = {}
        for b, v in st.rows:
            d = dt.date.fromisoformat(b) if isinstance(b, str) else b
            got[bucket_start(grain, d)] = v
        buckets = []
        for s in seq:
            v = got.get(s, 0 if measure in ("count", "count_distinct", "sum") else None)
            if v is None and measure == "sum":
                v = 0
            buckets.append({"bucket": bucket_label(grain, s), "value": _round(v, 2) if isinstance(v, (float, Decimal)) else v})
        if scol is not None:
            st_s = call.run(trend_sql(scope, base_conds + [pf], dcol, grain, call.ctx, measure, mcol, scol), max_rows=5000)
            if scol.multi:
                call.note(f"split_by {scol.display} is multi-select: in 'by' a record with several values is counted "
                          "once per value")
            per_b: dict[str, dict[str, Any]] = {}
            totals: dict[str, float] = {}
            for b, sp, v in st_s.rows:
                d = dt.date.fromisoformat(b) if isinstance(b, str) else b
                lab = bucket_label(grain, d)
                sp = "(blank)" if sp is None else _cell(str(sp))
                _put_split(per_b.setdefault(lab, {}), sp, v, measure)
                totals[sp] = totals.get(sp, 0) + (float(v) if isinstance(v, (int, float)) else 0)
            keep = [s for s, _ in sorted(totals.items(), key=lambda t: (-t[1], t[0]))[:10]]
            if measure in ("count", "sum"):
                trend_split_totals = self._split_totals(totals, measure)
            for bk in buckets:
                d = per_b.get(bk["bucket"], {})
                by = {s: d[s] for s in keep if s in d}
                if measure == "count":
                    other = sum(int(v or 0) for s, v in d.items() if s not in keep)
                    if other:
                        by["(other)"] = other
                bk["by"] = by
        st_nd = call.run(count_sql(scope, base_conds + [Frag(f"{dcol.expr} IS NULL")]), max_rows=1)
        nodate = int(st_nd.rows[0][0]) if st_nd.rows else 0
        vals = [b["value"] for b in buckets if b["value"] is not None]
        res: dict[str, Any] = {"grain": grain, "date_field": dref.text, "buckets": buckets}
        if measure != "count":
            res["measure"] = measure
            res["measure_field"] = mcol.display if mcol else None
        if measure in ("count", "sum"):
            res["total"] = _round(sum(vals), 2) if vals else 0
        if buckets:
            first, last = buckets[0]["value"], buckets[-1]["value"]
            res["first"], res["last"] = first, last
            nn = [b for b in buckets if b["value"] is not None]
            if nn:
                hi = max(nn, key=lambda b: b["value"])
                lo = min(nn, key=lambda b: b["value"])
                res["highest"] = {"bucket": hi["bucket"], "value": hi["value"]}
                res["lowest"] = {"bucket": lo["bucket"], "value": lo["value"]}
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                res["change_first_to_last"] = _round(last - first, 2)
                res["change_percent_first_to_last"] = round((last - first) * 100.0 / first, 2) if first else None
        res["first_bucket_partial"] = bool(seq) and clipped.start > seq[0]
        res["last_bucket_partial"] = bool(seq) and seq[-1] <= today < bucket_end(grain, seq[-1])
        res["records_without_date"] = nodate
        if trend_split_totals is not None:
            res["split_totals"] = trend_split_totals
        if scol is not None:
            stk = self._same_target_key(scol)
            if stk:
                res["same_target_lookups"] = [stk]
            self._lookup_col_notes(call, scol)
        if res["first_bucket_partial"]:
            call.note(f"the first bucket ({buckets[0]['bucket']}) covers only {clipped.start.isoformat()} onward (the "
                      f"period starts inside that {grain}); first, lowest and change values include this partial bucket")
        if res["last_bucket_partial"]:
            call.note(f"the last bucket ({buckets[-1]['bucket']}) is still running (today is {today.isoformat()})")
        elif seq and clipped.end_exclusive < bucket_end(grain, seq[-1]):
            call.note(f"the last bucket ({buckets[-1]['bucket']}) covers only up to "
                      f"{(clipped.end_exclusive - dt.timedelta(days=1)).isoformat()} (the period ends inside that {grain})")
        if per.end_exclusive is None or (per.end_exclusive and per.end_exclusive > end):
            call.note(f"buckets after today's {grain} are left out; the period ends {end.isoformat()} (exclusive)")
        self._base_notes(call, spec)
        self._period_notes(call, per, today)
        for t in scope.tables:
            call.table(t)
        call.summary = f"{len(buckets)} buckets"
        return res

    def _rate_trend(self, call: _Call, spec: ObjectSpec, scope: Scope, grain: str, dref: Any, per: Period,
                    clipped: Period, seq: list, today: dt.date, end: dt.date, filters: list | None, any_of: list | None,
                    related: list | None, part_filters: list) -> dict:
        """:meth:`_trend_records` with ``part_filters``: the base is ``filters``, ``any_of`` and ``related`` in the
        window, the part is the base records that also match every part filter (as in
        :meth:`_percentage_of_records`). Every bucket of the window is listed; one with no base record has
        ``percent`` and ``value`` null. Window totals: ``part_total``, ``base_total``, ``percent_overall``."""
        base_conds = self._where(call, spec, scope, {"filters": filters, "any_of": any_of, "related": related},
                                 use_period=False, result_name="the base", lookup_check=True)
        base_texts = list(call.applied.get("filters") or [])
        parts = [filter_frag(scope, item, call.ctx) for item in part_filters]
        self._ground(call, parts)
        part_texts = [p.text for p in parts]
        if set(part_texts) <= set(base_texts):
            raise RecordError("part_filters repeat filters, so the rate would always be 100%. part_filters should "
                              "hold only what makes a record count as the part; filters are the base.", "E8")
        pf, pa = period_frag(scope, dref, clipped, call.ctx)
        conds = base_conds + [pf]
        call.applied["period"] = pa
        call.use(dref)
        part_conds: list[Frag] = []   # base + part conditions, filled below
        for p in parts:
            call.use(p.ref)
            self._filter_notes(call, scope, p, part_conds, "the part")
        call.applied["part_filters"] = part_texts
        part = join_frags(" AND ", [p.frag for p in parts])
        part_conds.extend(conds + [part])
        dcol = scope.col(dref)
        st = call.run(rate_trend_sql(scope, conds, dcol, grain, call.ctx, part), max_rows=1000)
        call.main(st)
        call.main_scope, call.main_conds = scope, conds
        got: dict[dt.date, tuple[int, int]] = {}
        for b, p_, b_ in st.rows:
            d = dt.date.fromisoformat(b) if isinstance(b, str) else b
            got[bucket_start(grain, d)] = (int(p_ or 0), int(b_ or 0))
        buckets = []
        for s in seq:
            p_, b_ = got.get(s, (0, 0))
            pct = _pct(p_, b_)
            buckets.append({"bucket": bucket_label(grain, s), "part": p_, "base": b_, "percent": pct, "value": pct})
        part_total, base_total = sum(b["part"] for b in buckets), sum(b["base"] for b in buckets)
        first = parts[0]
        if first.ref is not None and not str(part_filters[0].get("op", "")).startswith("is_"):
            st_b = call.run(percentage_sql(scope, conds, part, scope.col(first.ref)), max_rows=1)
            row = dict(zip(st_b.columns, st_b.rows[0])) if st_b.rows else {}
            out_, in_ = int(row.get("part_field_blank_out") or 0), int(row.get("part_field_blank_in") or 0)
            if out_:
                call.note(f"{out_} base records have a blank {first.ref.text}; they are in the base but not in the part")
            if in_:
                call.note(f"{in_} base records have a blank {first.ref.text}; they are counted in the part")
        st_nd = call.run(count_sql(scope, base_conds + [Frag(f"{dcol.expr} IS NULL")]), max_rows=1)
        nodate = int(st_nd.rows[0][0]) if st_nd.rows else 0
        empty = [b["bucket"] for b in buckets if not b["base"]]
        res: dict[str, Any] = {"grain": grain, "date_field": dref.text, "measure": "percent", "buckets": buckets,
                               "part_total": part_total, "base_total": base_total,
                               "percent_overall": _pct(part_total, base_total),
                               "part_is": " and ".join(part_texts),
                               "base_is": " and ".join(base_texts + (call.applied.get("related") or []))
                               or f"all {spec.api} records"}
        nn = [b for b in buckets if b["value"] is not None]
        if nn:
            hi = max(nn, key=lambda b: b["value"])
            lo = min(nn, key=lambda b: b["value"])
            res["highest"] = {"bucket": hi["bucket"], "value": hi["value"]}
            res["lowest"] = {"bucket": lo["bucket"], "value": lo["value"]}
        if buckets:
            res["first"], res["last"] = buckets[0]["value"], buckets[-1]["value"]
        res["first_bucket_partial"] = bool(seq) and clipped.start > seq[0]
        res["last_bucket_partial"] = bool(seq) and seq[-1] <= today < bucket_end(grain, seq[-1])
        res["records_without_date"] = nodate
        if empty:
            call.note(f"no base records in {', '.join(empty)}: no rate there (not 0%)")
        if res["first_bucket_partial"]:
            call.note(f"the first bucket ({buckets[0]['bucket']}) covers only {clipped.start.isoformat()} onward (the "
                      f"period starts inside that {grain})")
        if res["last_bucket_partial"]:
            call.note(f"the last bucket ({buckets[-1]['bucket']}) is still running (today is {today.isoformat()})")
        elif seq and clipped.end_exclusive < bucket_end(grain, seq[-1]):
            call.note(f"the last bucket ({buckets[-1]['bucket']}) covers only up to "
                      f"{(clipped.end_exclusive - dt.timedelta(days=1)).isoformat()} (the period ends inside that {grain})")
        if per.end_exclusive is None or (per.end_exclusive and per.end_exclusive > end):
            call.note(f"buckets after today's {grain} are left out; the period ends {end.isoformat()} (exclusive)")
        self._base_notes(call, spec)
        self._period_notes(call, per, today)
        for t in scope.tables:
            call.table(t)
        call.summary = f"{len(buckets)} buckets, {res['percent_overall']}%"
        return res

    def _compare_records(self, call: _Call, object: str, segments: list, date_field: str | None = None,
                         measure: str = "count", measure_field: str | None = None, filters: list | None = None,
                         related: list | None = None) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        mcol = self._measure(call, spec, scope, measure, measure_field)
        shared = self._where(call, spec, scope, {"filters": filters, "related": related}, use_period=False,
                             result_name="the shared filters")
        shared_fields = {str(f.get("field", "")).strip().lower() for f in (filters or [])}
        seen: dict[tuple, str] = {}
        out = []
        dref = None
        for seg in segments:
            label = str(seg.get("label") or "").strip() or f"segment {len(out) + 1}"
            for fi in seg.get("filters") or []:
                if str(fi.get("field", "")).strip().lower() in shared_fields:
                    raise RecordError(f"{fi.get('field')} is in the shared filters and in segment '{label}'; keep it only "
                                      "in the segments.", "E8")
            if seg.get("period") and not date_field:
                raise RecordError(f"segment '{label}' has a period; set date_field. Date fields of {spec.api}: "
                                  f"{date_fields_text(spec)}.", "E8")
            sconds: list[Frag] = []
            stexts: list[str] = []
            seg_conds: list[Frag] = []  # shared + this segment's conditions, filled below
            fos = [filter_frag(scope, fi, call.ctx) for fi in seg.get("filters") or []]
            self._ground(call, fos)
            for fo in fos:
                sconds.append(fo.frag)
                stexts.append(fo.text)
                call.use(fo.ref)
                self._filter_notes(call, scope, fo, seg_conds, f"segment '{label}'")
            sapplied: dict[str, Any] = {}
            if stexts:
                sapplied["filters"] = stexts
            if seg.get("period"):
                dref = dref or self._date_ref(spec, date_field)
                per = self._period(seg["period"], call.ctx.today)
                pf, pa = period_frag(scope, dref, per, call.ctx)
                sconds.append(pf)
                sapplied["period"] = pa
                call.use(dref)
                self._period_notes(call, per, call.ctx.today)
            key = (tuple(sorted(stexts)), json.dumps(sapplied.get("period"), sort_keys=True))
            if key in seen:
                raise RecordError(f"segments '{seen[key]}' and '{label}' have identical conditions.", "E8")
            seen[key] = label
            seg_conds.extend(shared + sconds)
            sel = f"{measure_expr(measure, mcol)} AS v"
            st = call.run(Frag(f"SELECT {sel} FROM {scope.from_sql()}") + where_sql(shared + sconds), max_rows=1)
            if not out:
                call.main(st)
            v = st.rows[0][0] if st.rows else None
            v = _round(v, 2) if isinstance(v, (float, Decimal)) else v
            out.append({"label": label, "value": v, "applied": sapplied})
        if date_field and not any(s.get("period") for s in segments):
            call.note("date_field is ignored: no segment has a period")
        res: dict[str, Any] = {"segments": out}
        a, b = out[0]["value"], out[1]["value"]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            res["higher"] = out[0]["label"] if a > b else out[1]["label"] if b > a else "equal"
            res["first_minus_second"] = _round(a - b, 2)
            res["ratio_first_to_second"] = round(a / b, 2) if b else None
            res["change_percent_second_to_first"] = round((a - b) * 100.0 / b, 2) if b else None
        if len(out) > 2:
            res["ranked"] = [s["label"] for s in sorted(out, key=lambda s: -(s["value"] or 0))]
        self._base_notes(call, spec)
        for t in scope.tables:
            call.table(t)
        call.summary = f"{len(out)} segments"
        return res

    def _percentage_of_records(self, call: _Call, object: str, part_filters: list, base_filters: list | None = None,
                               date_field: str | None = None, period: str | None = None,
                               related: list | None = None) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        scope = Scope(cat, spec)
        conds = self._where(call, spec, scope, {"filters": base_filters, "date_field": date_field, "period": period,
                                                "related": related}, result_name="the base")
        base_texts = list(call.applied.get("filters") or [])
        if base_texts:
            call.applied["base_filters"] = call.applied.pop("filters")
        parts = [filter_frag(scope, item, call.ctx) for item in part_filters]
        self._ground(call, parts)
        part_texts = [p.text for p in parts]
        if set(part_texts) <= set(base_texts):
            raise RecordError("part_filters repeat base_filters, so the percentage would always be 100%. part_filters "
                              "should hold only what makes a record count as the part.", "E8")
        part_conds: list[Frag] = []  # base + part conditions, filled below
        for p in parts:
            call.use(p.ref)
            self._filter_notes(call, scope, p, part_conds, "the part")
        call.applied["part_filters"] = part_texts
        part = join_frags(" AND ", [p.frag for p in parts])
        part_conds.extend(conds + [part])
        first = parts[0]
        blank_col = None
        if first.ref is not None and not str(part_filters[0].get("op", "")).startswith("is_"):
            blank_col = scope.col(first.ref)
        for t in scope.tables:
            call.table(t)
        st = call.run(percentage_sql(scope, conds, part, blank_col), max_rows=1)
        call.main(st)
        row = dict(zip(st.columns, st.rows[0])) if st.rows else {"part": 0, "base": 0}
        p_, b_ = int(row.get("part") or 0), int(row.get("base") or 0)
        pct = _pct(p_, b_)
        if b_ == 0:
            call.note("no records in the base")
        if first.ref is not None:
            out_, in_ = int(row.get("part_field_blank_out") or 0), int(row.get("part_field_blank_in") or 0)
            if out_:
                call.note(f"{out_} base records have a blank {first.ref.text}; they are in the base but not in the part")
            if in_:
                call.note(f"{in_} base records have a blank {first.ref.text}; they are counted in the part")
        self._base_notes(call, spec)
        self._future_note(call)
        base_is = " and ".join(base_texts + (call.applied.get("related") or [])) or f"all {spec.api} records"
        if call.applied.get("period"):
            pa = call.applied["period"]
            base_is += f" with {pa['date_field']} in [{pa['from'] or '…'}, {pa['to_exclusive'] or '…'})"
        call.summary = f"{pct}%"
        return {"part": p_, "base": b_, "percent": pct, "part_is": " and ".join(part_texts), "base_is": base_is}

    def _default_list_fields(self, spec: ObjectSpec) -> list[FieldSpec]:
        out: list[FieldSpec] = []
        # a date or checkbox named after a secret (User.LastPasswordChangeDate, readable since phase 1) is no default
        ok = lambda f: (not f.legacy and f.kind != "longtext" and not f.sensitivity and f.has_data  # noqa: E731
                        and not BLOCKED_COLUMN_RE.search(f.api))
        for f in spec.fields.values():
            if f.kind == "picklist" and ok(f) and any(r in f.roles for r in _STATUS_ROLES):
                out.append(f)
            if len(out) >= 2:
                break
        # phase 1: a date the knowledge marks as personal contact data (a date of birth) is never the default date
        dates = [f for f in spec.date_fields() if f.api not in ("CreatedDate", "LastModifiedDate") and ok(f)
                 and not _DEFAULT_DROP_ROLES & set(f.roles)]
        if dates:
            out.append(dates[0])
        refs = [f for f in spec.fields.values() if f.kind == "reference" and ok(f) and not f.system]
        out += refs[:2]
        if "CreatedDate" in spec.fields and spec.fields["CreatedDate"] not in out:
            out.append(spec.fields["CreatedDate"])
        return list(dict.fromkeys(out))[:6]

    def _list_records(self, call: _Call, object: str, fields: list | None = None, filters: list | None = None,
                      any_of: list | None = None, date_field: str | None = None, period: str | None = None,
                      related: list | None = None, order_by: str | None = None, order: str = "desc",
                      limit: int = 10) -> dict:
        cat = self.catalog
        spec = self._object(call, object)
        if spec.sensitive:
            raise RecordError(sensitive_object_text(spec.api), "E1d")
        scope = Scope(cat, spec)
        refs: list[PathRef] = []
        if "Name" in spec.fields:
            refs.append(PathRef("Name", [], spec.api, spec.fields["Name"]))
        left_out: list[str] = []   # contact columns the question does not ask for (phase 1)
        instead: list[str] = []    # the lookups of left-out contact paths, listed in their place (review F9)
        if fields:
            for name in fields:
                ref = cat.resolve_path(spec, name)
                if ref.field.sensitivity == "blocked":
                    raise RecordError(blocked_text(ref.obj, ref.field.api), "E2c")
                if ref.field.api == "Id" and not ref.hops:
                    continue
                if ref.field.sensitivity == "contact" and not self._contact_ok:
                    if ref.text not in left_out:
                        left_out.append(ref.text)
                    if ref.hops:   # "Recruiter__c.Email__c": the recruiter's name instead, so people can be named
                        try:
                            ref = cat.resolve_path(spec, ".".join(h.field.api for h in ref.hops))
                        except RecordError:
                            continue
                        if ref.field.sensitivity or any(r.text == ref.text for r in refs):
                            continue
                        instead.append(f"{ref.text} (name)")
                    else:
                        continue
                if not any(r.text == ref.text for r in refs):
                    refs.append(ref)
        # nothing but Name left after the contact columns: the default columns, as for a call without fields
        if not fields or (left_out and all(r.text == "Name" and not r.hops for r in refs)):
            for f in self._default_list_fields(spec):
                refs.append(PathRef(f.api, [], spec.api, f))
        cols: list[tuple[str, str]] = []
        kinds: list[str | None] = []
        names: list[str] = []
        for ref in refs:
            call.use(ref)
            if ref.field.kind == "reference":
                col = scope.col(ref, label=True)
                alias = f"{ref.text} (name)"
                cols.append((col.label_expr or col.expr, alias))
            else:
                col = scope.col(ref)
                alias = ref.text
                cols.append((col.expr, alias))
            names.append(alias)
            kinds.append(ref.field.kind)
        conds = self._where(call, spec, scope, {"filters": filters, "any_of": any_of, "date_field": date_field,
                                                "period": period, "related": related}, keep_date_field=True,
                            lookup_check=True)
        oref = None
        if order_by:
            oref = cat.resolve_path(spec, order_by)
            if oref.field.sensitivity == "blocked":
                raise RecordError(blocked_text(oref.obj, oref.field.api), "E2c")
        elif date_field:
            oref = self._date_ref(spec, date_field)
        elif "CreatedDate" in spec.fields:
            oref = PathRef("CreatedDate", [], spec.api, spec.fields["CreatedDate"])
        elif "Name" in spec.fields:
            oref = PathRef("Name", [], spec.api, spec.fields["Name"])
        if oref is not None:
            ocol = scope.col(oref, label=oref.field.kind == "reference")
            order_expr = ocol.label_expr or ocol.expr
            order_txt = f"{oref.text} {order}"
            call.use(oref)
        else:
            order_expr, order_txt = f"{scope.alias}.\"Id\"", f"Id {order}"
        for t in scope.tables:
            call.table(t)
        st = call.run(list_sql(scope, conds, cols, order_expr, order, limit + 1), max_rows=limit + 1)
        call.main(st)
        call.main_scope, call.main_conds = scope, conds
        call.list_view = {"scope": scope, "cols": cols, "names": names, "kinds": kinds, "order_expr": order_expr,
                          "order": order}
        more = len(st.rows) > limit or st.truncated
        rows = []
        for r in st.rows[:limit]:
            rows.append({names[i]: _cell(r[i + 1], kinds[i]) for i in range(len(names))})
        total = len(rows)
        if more:
            st2 = call.run(count_sql(scope, conds), max_rows=1)
            total = int(st2.rows[0][0]) if st2.rows else total
        self._base_notes(call, spec)
        if left_out:
            call.note(f"left out {', '.join(left_out)}: contact details are shown only when the user's question asks "
                      "for them, and this question does not; identify people by name"
                      + (f" (listed instead: {', '.join(instead)})" if instead else ""))
        if any(r.field.sensitivity == "contact" for r in refs):   # only when the question asks (phase 1)
            call.note("contact details are shown: the question asks for contact details")
        call.summary = f"{len(rows)} of {total} rows"
        res: dict[str, Any] = {"columns": names, "rows": rows, "returned": len(rows), "total_count": total,
                               "truncated": total > len(rows), "order": order_txt}
        try:   # C8: people in the same role on child rows of the listed records (best effort)
            child = self._child_people(call, spec, refs, names, [(str(r[0]), rows[i]) for i, r in
                                                                 enumerate(st.rows[:limit])])
        except RecordError:
            child = []
        if child:
            res["child_people"] = child
            call.note("child_people: people that rows pointing to the listed records hold in the role of a listed "
                      "column (an attendee row whose role is that column's label); they are not in that column")
        return res

    def _child_people(self, call: _Call, spec: ObjectSpec, refs: list[PathRef], names: list[str],
                      rows: list[tuple[str, dict]]) -> list[dict]:
        """C8 ``child_people``: for each listed lookup column to a person object, the child objects of the listed
        object that hold a person of the same object in a lookup and have a role or purpose picklist with a value
        that is a word of the column's label (an attendee row with role 'Interviewer' next to Interviewer__c): the
        people per listed row, when they are not the row's own value of the column. At most CHILD_PEOPLE_MAX objects,
        one statement each, bound Ids. ``[{"object", "field", "role_field", "role", "column", "by_row"}]``."""
        cat = self.catalog
        if not rows:
            return []
        ids = [rid for rid, _r in rows]
        out: list[dict] = []
        for ref, col in zip(refs, names):
            f = ref.field
            if ref.hops or f.kind != "reference" or f.api in ("OwnerId", "CreatedById", "LastModifiedById"):
                continue
            t = cat.target_of(f)
            if t not in PERSON_OBJECTS or not self._queryable(t):
                continue
            words = {w for w in re.findall(r"[a-z0-9]+", norm_name(f.label)) if len(w) >= 3}
            for ch in spec.children:
                if len(out) >= CHILD_PEOPLE_MAX:
                    break
                xs = cat.objects.get(ch.child)
                k = xs.fields.get(ch.via) if xs is not None else None
                if xs is None or k is None or not xs.has_table or xs.sensitive or k.filled <= 0 or \
                        cat.target_of(k) != spec.api or k.api in ("OwnerId", "CreatedById", "LastModifiedById"):
                    continue
                pick = None
                for rf in xs.fields.values():
                    if rf.kind != "picklist" or not rf.has_data or rf.sensitivity or \
                            not any(w in _ROLE_WORDS for w in split_api_name(rf.api).split()):
                        continue
                    vals = [v for v, _lab, _m in rf.picklist] or [v for v, _n in (getattr(cat.values_of(xs.api, rf.api),
                                                                                         "values", None) or [])]
                    hit = next((v for v in vals if norm_name(v) in words), None)
                    if hit is not None:
                        pick = (rf, hit)
                        break
                if pick is None:
                    continue
                gs = [g for g in xs.fields.values() if g.kind == "reference" and g is not k and g.filled > 0
                      and g.api not in ("OwnerId", "CreatedById", "LastModifiedById") and g.sensitivity != "blocked"
                      and cat.target_of(g) == t]
                if not gs:
                    continue
                g = max(gs, key=lambda x: (x.filled, x.api))
                rf, val = pick
                if call.remaining() < 0.5:   # best effort: never at the cost of the call's own rows
                    return out
                st = call.run(Frag(f'SELECT cx.{quote_ident(k.api)} AS row_id, trim(px."Name") AS name FROM '
                                   f'main.{quote_ident(xs.api)} AS cx JOIN main.{quote_ident(t)} AS px ON px."Id" = '
                                   f'cx.{quote_ident(g.api)} WHERE cx.{quote_ident(k.api)} = ANY(?) AND '
                                   f'cx.{quote_ident(rf.api)} = ? ORDER BY 1, 2 LIMIT 200', (list(ids), val)),
                              max_rows=200, timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.3)))
                call.table(xs.api)
                by_id: dict[str, list[str]] = {}
                for rid, nm in st.rows:
                    if nm:
                        by_id.setdefault(str(rid), [])
                        if _disp(nm) not in by_id[str(rid)]:
                            by_id[str(rid)].append(_disp(nm))
                by_row: dict[str, list[str]] = {}
                for rid, row in rows:
                    got = [n for n in by_id.get(rid, []) if norm_name(n) != norm_name(row.get(col))]
                    if got:
                        by_row[str(row.get("Name") or rid)] = got
                if by_row:
                    out.append({"object": xs.api, "field": g.api, "role_field": rf.api, "role": val, "column": col,
                                "by_row": by_row})
        return out

    def _status_field(self, spec: ObjectSpec) -> FieldSpec | None:
        """The record's status picklist (for "all assigned or only completed" splits), or None: a lifecycle state
        first, then a name ending in Status__c, then Status__c itself or the object's own <Object>_Status__c.
        Without such a picklist, a status that is a picklist in Salesforce but typed text in the catalog and the
        warehouse (``Lead.Status``): a text field named like a status (or a lifecycle state) that is filled on at least
        half of the records and holds a short value list (its knowledge picklist values, else at most
        :data:`STATUS_TEXT_MAX_VALUES` stored values in the catalog's value index); ``Status`` / ``Status__c`` /
        <Object>_Status__c first, then the most filled."""
        stem = spec.api[:-3] if spec.api.endswith("__c") else spec.api
        cands = [f for f in spec.fields.values() if f.kind == "picklist" and not f.sensitivity and f.has_data
                 and not f.legacy and ("lifecycle_state" in f.roles or "status" in f.api.lower())]
        if cands:
            return min(cands, key=lambda f: (0 if "lifecycle_state" in f.roles else 1,
                                             0 if f.api.lower().endswith("status__c") else 1,
                                             0 if f.api == "Status__c" or f.api.startswith(stem + "_") else 1,
                                             -f.filled, f.api))
        texts = [f for f in spec.fields.values() if f.kind == "text" and not f.sensitivity and f.has_data
                 and not f.legacy and not f.deprecated and f.filled * 2 >= max(1, spec.rows)
                 and ("lifecycle_state" in f.roles or self._own_status_name(spec, f.api))
                 and self._short_value_list(spec, f)]
        if not texts:
            return None
        return min(texts, key=lambda f: (0 if f.api in ("Status", "Status__c") or
                                         f.api.lower() == (stem + "_Status__c").lower() else 1, -f.filled, f.api))

    @staticmethod
    def _own_status_name(spec: ObjectSpec, api: str) -> bool:
        """``api`` names the record's own status: ``Status`` / ``Status__c``, or ``<words>_Status__c`` whose words end
        the object's name (``Lead_Status__c`` on Lead, ``Submission_Status__c`` on Job_Submission__c); a status of
        something else (``Visa_Status__c``, ``Required_Visa_Status__c``) is not."""
        low = api.lower()
        if low in ("status", "status__c"):
            return True
        if not low.endswith("_status__c"):
            return False
        stem = (spec.api[:-3] if spec.api.endswith("__c") else spec.api).lower()
        head = low[:-len("_status__c")]
        return stem == head or stem.endswith("_" + head)

    def _short_value_list(self, spec: ObjectSpec, f: FieldSpec) -> bool:
        """``f`` has a short list of values: knowledge picklist values, else a complete value-index entry with 2 to
        :data:`STATUS_TEXT_MAX_VALUES` stored values."""
        if f.picklist:
            return True
        entry = (getattr(self.catalog, "values", None) or {}).get((spec.api, f.api))
        return entry is not None and not entry.truncated and 2 <= len(entry.values) <= STATUS_TEXT_MAX_VALUES

    def _status_labels(self, spec: ObjectSpec, f: FieldSpec) -> list[str]:
        """The values and labels of the status field ``f``: its knowledge picklist, else its stored values (value
        index; a status typed text in the catalog)."""
        if f.picklist:
            return [str(x) for v, lab, _m in f.picklist for x in (v, lab) if x]
        entry = (getattr(self.catalog, "values", None) or {}).get((spec.api, f.api))
        return [str(v) for v, _n in (entry.values if entry is not None else [])]

    def _status_fields(self, spec: ObjectSpec) -> list[FieldSpec]:
        out = [f for f in spec.fields.values() if f.kind == "picklist" and not f.sensitivity and f.has_data
               and (any(r in f.roles for r in _STATUS_ROLES) or "status" in f.api.lower() or "outcome" in f.api.lower())]
        return out[:3]

    def _search_records(self, call: _Call, name: str, objects: list | None = None, limit: int = 5) -> dict:
        cat = self.catalog
        text = (name or "").strip()
        if len(text) < 2:
            raise RecordError("search_records needs 'name' with at least 2 characters.", "E14")
        if objects:
            specs = []
            for o in objects:
                s = cat.resolve_object(o)
                if s.sensitive:
                    raise RecordError(sensitive_object_text(s.api), "E1d")
                specs.append(s)
        else:
            # Contact mirrors person accounts and Opportunity holds conversion by-products named after the
            # candidate (engine.catalog PEOPLE_SCAN_SKIP): searched only when the call names them
            specs = [s for s in cat.queryable() if "Name" in s.fields and not s.sensitive and s.api not in
                     PEOPLE_SCAN_SKIP and s.api not in ("RecordType", "Group") and s.rows > 0 and
                     (not s.name_auto or s.api in ("Account", "User", "Recruiter__c"))]
        number = is_record_number(text)
        if number:
            m = re.match(r"^([A-Za-z]{1,6}-?)\d", text)
            pref = norm_key(m.group(1)) if m else ""
            routed = [cat.objects[a] for a in cat.number_prefixes.get(pref, []) if a in cat.objects]
            if not objects:
                specs = routed or [s for s in cat.queryable() if s.name_auto and not s.sensitive]
        matches: list[dict] = []
        truncated = False
        searched = 0

        def run(match: str) -> None:
            nonlocal truncated, searched
            for s in specs:
                if "Name" not in s.fields or s.fields["Name"].sensitivity == "blocked":
                    continue
                searched += 1
                scope = Scope(cat, s)
                sel = [f"{scope.alias}.\"Name\" AS name"]
                st_fields = self._status_fields(s)
                for f in st_fields:
                    sel.append(f"{scope.alias}.\"{f.api}\" AS \"{f.api}\"")
                rt_col = None
                if s.fields.get("RecordTypeId") is not None and s.fields["RecordTypeId"].has_data and "RecordType" in cat.objects:
                    rt_col = scope.col(PathRef("RecordTypeId", [], s.api, s.fields["RecordTypeId"]), label=True)
                    sel.append(f"{rt_col.label_expr} AS record_type")
                    sel.append(f"{rt_col.label_expr.replace('Name', 'DeveloperName')} AS rt_dev")
                if match == "exact":
                    ncol = f'{scope.alias}."Name"'
                    cond = Frag(f"{name_norm_sql(ncol)} = ?", (norm_name(text),))
                else:
                    cond = Frag(f"{scope.alias}.\"Name\" ILIKE ? ESCAPE '\\'", (f"%{escape_like(text)}%",))
                st = call.run(Frag(f"SELECT {', '.join(sel)} FROM {scope.from_sql()}") + where_sql([cond]) +
                              Frag(f" ORDER BY {scope.alias}.\"Name\", {scope.alias}.\"Id\" LIMIT {limit + 1}"),
                              max_rows=limit + 1)
                if not st.rows:
                    continue
                call.table(s.api)
                if len(st.rows) > limit:
                    truncated = True
                for r in st.rows[:limit]:
                    row = dict(zip(st.columns, r))
                    item: dict[str, Any] = {"object": s.api, "object_label": s.label, "name": _cell(row.get("name"))}
                    if rt_col is not None:
                        dev = row.get("rt_dev")
                        if s.api == "Account":
                            item["record_type"] = {"PersonAccount": "candidate (Person Account)",
                                                   "Recruiter": "Recruiter account",
                                                   "B2B_Client": "B2B client"}.get(str(dev), row.get("record_type"))
                        elif row.get("record_type"):
                            item["record_type"] = row.get("record_type")
                    fl = {f.api: _cell(row.get(f.api)) for f in st_fields if row.get(f.api) is not None}
                    if fl:
                        item["fields"] = fl
                    matches.append(item)

        run("exact")
        match_type = "exact"
        if not matches and not number:
            searched = 0
            run("contains")
            match_type = "contains"
        if not matches:
            call.note(f"no record is named '{text}'")
        call.summary = f"{len(matches)} matches"
        return {"match_type": match_type, "matches": matches, "objects_searched": searched, "truncated": truncated}
