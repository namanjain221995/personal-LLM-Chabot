"""Name grounding of filters and the lookup checks of the record tools (RECORDS_SPEC section 21.4, 21.5).

Moved verbatim from ``okrec/tools.py`` (``RecordTools`` methods ``_g1`` ... ``_e4h`` and ``_people`` ... ``_scan``).

The Group B people fixes (docs/people.md, 2026-10-21) add to a person filter's lookup check: the same person through
one or two parent records in the same role (``paths``, ``any_path``: a candidate's sessions through the candidate's
trainings), child rows that hold the person in a role sharing a role word (``related``: a Candidate Owner next to
OwnerId), the source of a lookup copied from a child object (``copy_source``) and, for list_records, the rows of the
other lookups that hold the person (``also_held``); same-name records get their own counts even when no lookup check
ran (:meth:`LookupsMixin._per_record`).
"""

from __future__ import annotations

import time
from typing import Any

from sf_assistant.engine.catalog import (PEOPLE_SCAN_SKIP, PERSON_OBJECTS, ROLE_CUES, SYSTEM_LOOKUPS, FieldSpec,
                                         IdentityLink, ObjectSpec, RecordError, norm_name)
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.engine.query_builder import FilterOut, Frag, NameCheck, Scope, join_frags, name_norm_sql, where_sql
from sf_assistant.engine.values import escape_like, id18
from sf_assistant.knowledge.store import split_api_name

from sf_assistant.tools import record_base as _base   # the limits SCAN_BUDGET_S: read (and patched) there
from sf_assistant.tools.names import _suggested_names   # the spelling help (SFA_RESOLVER=on): E4h close spellings
from sf_assistant.tools.record_base import _Call, _disp, _and_more, _and, _role, _without_words


#: Per-call limits of the name grounding and lookup check (RECORDS_SPEC §21.4, §21.5).
MAX_LOOKUP_CHECKS = 2
MAX_OTHERS = 12
CHECK_MIN_BUDGET_S = 2.0
PATHS_MAX = 6             # paths through parents per lookup (C5 paths)
RELATED_MAX = 3           # child objects with the same role per lookup check (C5 related)
ALSO_ROWS = 10            # rows per other lookup that holds the person (list_records also_held)
ALSO_LOOKUPS = 3          # other lookups whose rows list_records lists (also_held)
#: Role stems that only the candidate side of ROLE_CUES has ("candidate", "applicant", "lead"): a lookup to a Lead or
#: a Person Account whose role words start with one holds the record's subject (:meth:`LookupsMixin._subject_role`).
SUBJECT_STEMS = tuple(sorted({st for cue in ROLE_CUES for st in cue[2]} - {st for cue in ROLE_CUES for st in cue[1]}))

SCAN_MAX_CHARS = 15_000


class LookupsMixin:
    """Name grounding and lookup checks of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    def _g1(self, call: _Call | None, obj: str, norms: Any = (), pattern: str | None = None) -> dict[str, list[tuple]]:
        """G1: records of ``obj`` whose normalised name is one of ``norms`` (or whose Name matches the ILIKE
        ``pattern``, key ``'~' + pattern``): ``{key: [(Id, stored name, detail)]}`` in Id order; cached per question."""
        if not self._queryable(obj):
            return {}
        keys = [f"~{pattern}"] if pattern is not None else list(dict.fromkeys(str(n) for n in norms if n))
        todo = [k for k in keys if (obj, k) not in self._g1_rows]
        if todo:
            det, join = self._detail_sql(obj, "x")
            if pattern is not None:
                cond = Frag("x.\"Name\" ILIKE ? ESCAPE '\\'", (pattern,))
            else:
                nsql = name_norm_sql('x."Name"')
                cond = Frag(f"{nsql} = ANY(?)", (list(todo),))
            frag = (Frag(f'SELECT x."Id" AS id, trim(x."Name") AS name, {det} AS detail FROM main.{quote_ident(obj)} AS x'
                         f'{join} WHERE ') + cond + Frag(" ORDER BY 2, 1 LIMIT 51"))
            st = self._exec(call, frag, max_rows=51)
            rows = [(str(r[0]), str(r[1] or ""), r[2]) for r in st.rows]
            if pattern is not None:
                self._g1_rows[(obj, todo[0])] = sorted(rows, key=lambda r: r[0])
                self._g1_st[(obj, todo[0])] = st
            else:
                by: dict[str, list[tuple]] = {k: [] for k in todo}
                for r in rows:
                    by.setdefault(norm_name(r[1]), []).append(r)
                for k in todo:
                    self._g1_rows[(obj, k)] = sorted(by.get(k, []), key=lambda r: r[0])
                    self._g1_st[(obj, k)] = st
        if call is not None:
            call.table(obj)
            for k in keys:
                if k not in todo:
                    call.reuse(self._g1_st.get((obj, k)))
        return {k: self._g1_rows.get((obj, k), []) for k in keys}

    def _name_rows(self, call: _Call | None, objs: list[str], norm: str) -> list[tuple]:
        """Records of ``objs`` with this normalised name, ``[(object, Id, name, detail)]`` in Id order ('Name (#k)')."""
        out = [(o, r[0], r[1], r[2]) for o in objs for r in self._g1(call, o, [norm]).get(norm, [])]
        out.sort(key=lambda r: r[1])
        return out

    def _by_ids(self, call: _Call | None, objs: list[str], ids: list[str]) -> dict[str, list[tuple]]:
        """Records with these Ids (15-character Ids are compared in their 18-character form): ``{object: [(Id, name,
        detail)]}``."""
        ids = [id18(i) for i in ids]
        out: dict[str, list[tuple]] = {}
        for o in objs:
            spec = self.catalog.objects.get(o)
            if not self._queryable(o) or not spec or (spec.id_prefix and not any(str(i)[:3] == spec.id_prefix for i in ids)):
                continue
            det, join = self._detail_sql(o, "x")
            st = self._exec(call, Frag(f'SELECT x."Id" AS id, trim(x."Name") AS name, {det} AS detail FROM '
                                       f'main.{quote_ident(o)} AS x{join} WHERE x."Id" = ANY(?) LIMIT 51', (list(ids),)),
                            max_rows=51)
            if call is not None:
                call.table(o)
            rows = sorted(((str(r[0]), str(r[1] or ""), r[2]) for r in st.rows), key=lambda r: r[0])
            if rows:
                out[o] = rows
        return out

    def _g2(self, call: _Call | None, obj: str, v: str) -> list[tuple[str, int]]:
        """G2: close names of ``v`` (normalised) in ``obj``: (name, how) with how 1 = first name, 2 = contains,
        3 = close spelling; cached per question."""
        if not self._queryable(obj) or not v:
            return []
        key = (obj, v)
        if key in self._g2_rows:
            if call is not None:
                call.reuse(self._g2_st.get(key))
            return self._g2_rows[key]
        n = name_norm_sql('x."Name"')
        like = f"%{escape_like(v)}%"
        frag = Frag(f"SELECT trim(x.\"Name\") AS name, CASE WHEN split_part({n}, ?, 1) = ? THEN 1 WHEN x.\"Name\" ILIKE ? "
                    f"ESCAPE '\\' THEN 2 ELSE 3 END AS how FROM main.{quote_ident(obj)} AS x WHERE split_part({n}, ?, 1) = ? "
                    f"OR x.\"Name\" ILIKE ? ESCAPE '\\' OR greatest(jaro_winkler_similarity({n}, ?), "
                    f"jaro_winkler_similarity(split_part({n}, ?, 1), ?)) >= 0.88 GROUP BY 1, 2 ORDER BY 2, 1 LIMIT 13",
                    (" ", v, like, " ", v, like, v, " ", v))
        st = self._exec(call, frag, max_rows=13)
        if call is not None:
            call.table(obj)
        rows: list[tuple[str, int]] = []
        for name, how in st.rows:
            d = _disp(name)
            if d and all(d != r[0] for r in rows):
                rows.append((d, int(how)))
        self._g2_rows[key], self._g2_st[key] = rows, st
        return rows

    @staticmethod
    def _close_parts(rows: list[tuple[str, int]], v: str, *, sentence: bool = True) -> str:
        groups: dict[int, list[str]] = {1: [], 2: [], 3: []}
        for name, how in rows:
            if name not in groups[how]:
                groups[how].append(name)
        if not sentence:
            return _and_more(groups[1])
        out = ""
        if groups[1]:
            out += f" First name '{v}' fits: {_and_more(groups[1])}."
        if groups[2]:
            out += f" Names containing it: {_and_more(groups[2])}."
        if groups[3]:
            out += f" Close spellings: {_and_more(groups[3])}."
        if not any(groups.values()):
            out += " No name contains it and no name is close."
        return out

    # ------------------------------------------------------------------ identity links (G3)
    @staticmethod
    def _left(deadline: float | None) -> float | None:
        """Seconds left before ``deadline`` (None: no deadline); RecordError E10 when it has passed."""
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise RecordError("statements stopped (time budget)", "E10")
        return left

    def _forward(self, call: _Call | None, src: str, ids: list[str], deadline: float | None = None) -> None:
        """Linked records of ``src`` records through their identity links, cached per question."""
        cat = self.catalog
        links = cat.identity_links(src)
        todo = [i for i in dict.fromkeys(ids) if i not in self._fwd]
        if not links or not self._queryable(src):
            for i in todo:
                self._fwd[i] = []
            return
        if todo:
            det, join = self._detail_sql(src, "r")
            sel = ['r."Id" AS id', 'trim(r."Name") AS name', f"{det} AS detail"]
            joins = [join] if join else []
            usable: list[tuple[int, IdentityLink]] = []
            for k, lk in enumerate(links, 1):
                if not self._queryable(lk.dst) or lk.dst_col not in cat.objects[lk.dst].fields:
                    continue
                a = f"l{k}"
                d, j = self._detail_sql(lk.dst, a)
                joins.append(f" LEFT JOIN main.{quote_ident(lk.dst)} AS {a} ON {a}.{quote_ident(lk.dst_col)} = "
                             f"r.{quote_ident(lk.col)}")
                if j:
                    joins.append(j)
                sel += [f'{a}."Id" AS {a}_id', f'trim({a}."Name") AS {a}_name', f"{d} AS {a}_detail"]
                usable.append((k, lk))
            frag = Frag(f"SELECT {', '.join(sel)} FROM main.{quote_ident(src)} AS r{''.join(joins)} WHERE r.\"Id\" = ANY(?) "
                        "LIMIT 50", (list(todo),))
            st = self._exec(call, frag, max_rows=50, timeout=self._left(deadline))
            if call is not None:
                call.table(src)
                for _k, lk in usable:
                    call.table(lk.dst)
            per: dict[str, list[tuple]] = {i: [] for i in todo}
            for row in st.rows:
                rid = str(row[0])
                self._fwd_src[rid] = (src, rid, str(row[1] or ""), row[2])
                for n, (_k, lk) in enumerate(usable):
                    lid = row[3 + 3 * n]
                    if lid is None:
                        continue
                    item = (lk, lk.dst, str(lid), str(row[4 + 3 * n] or ""), row[5 + 3 * n])
                    if all(x[2] != item[2] for x in per.setdefault(rid, [])):
                        per[rid].append(item)
            for i in todo:
                self._fwd[i] = per.get(i, [])[:5]
                self._fwd_st[i] = st
        elif call is not None:
            for i in dict.fromkeys(ids):
                call.reuse(self._fwd_st.get(i))

    def _reverse(self, call: _Call | None, dst: str, ids: list[str], deadline: float | None = None) -> None:
        """Person records (e.g. employees) that ``dst`` records (users, accounts) are linked to; cached per question."""
        cat = self.catalog
        links = [lk for lk in cat.linked_from(dst) if self._queryable(lk.src)]
        todo = [i for i in dict.fromkeys(ids) if i not in self._rev]
        if not links:
            for i in todo:
                self._rev[i] = []
            return
        if not todo:
            if call is not None:
                for i in dict.fromkeys(ids):
                    call.reuse(self._rev_st.get(i))
            return
        by_src: dict[str, list[IdentityLink]] = {}
        for lk in links:
            by_src.setdefault(lk.src, []).append(lk)
        for i in todo:
            self._rev[i] = []
        for src, lks in by_src.items():
            conds = []
            for lk in lks:
                if lk.dst_col == "Id":
                    conds.append(Frag(f"r.{quote_ident(lk.col)} = ANY(?)", (list(todo),)))
                else:
                    conds.append(Frag(f"r.{quote_ident(lk.col)} IN (SELECT u.{quote_ident(lk.dst_col)} FROM "
                                      f"main.{quote_ident(lk.dst)} AS u WHERE u.\"Id\" = ANY(?))", (list(todo),)))
            st = self._exec(call, Frag(f'SELECT r."Id" AS id FROM main.{quote_ident(src)} AS r WHERE ')
                            + join_frags(" OR ", conds) + Frag(" LIMIT 50"), max_rows=50, timeout=self._left(deadline))
            if call is not None:
                call.table(src)
            srcs = [str(r[0]) for r in st.rows]
            self._forward(call, src, srcs, deadline)
            for i in todo:
                self._rev_st[i] = st
                for sid in srcs:
                    if any(m[2] == i for m in self._fwd.get(sid, [])) and sid not in self._rev[i]:
                        self._rev[i].append(sid)

    def _identities(self, call: _Call | None, recs: list[tuple[str, str]],
                    deadline: float | None = None) -> dict[str, tuple[tuple | None, list[tuple]]]:
        """For each (object, Id): (anchor record (object, Id, name, detail) or None, linked records
        [(link, object, Id, name, detail)] of the anchor). ``deadline`` (time.monotonic) bounds the statements
        (hints); past it, RecordError E10."""
        cat = self.catalog
        by_obj: dict[str, list[str]] = {}
        for o, i in recs:
            by_obj.setdefault(o, []).append(i)
        for o, ids in by_obj.items():
            if cat.identity_links(o):
                self._forward(call, o, ids, deadline)
            if cat.linked_from(o):
                self._reverse(call, o, ids, deadline)
        out: dict[str, tuple[tuple | None, list[tuple]]] = {}
        for o, i in recs:
            if cat.identity_links(o):
                out[i] = (self._fwd_src.get(i) or (o, i, "", None), self._fwd.get(i, []))
            elif self._rev.get(i):
                sid = self._rev[i][0]
                out[i] = (self._fwd_src.get(sid), self._fwd.get(sid, []))
            else:
                out[i] = (None, [])
        return out

    # ------------------------------------------------------------------ grounding
    def _g1_objects(self, chk: NameCheck) -> list[str]:
        cat = self.catalog
        targets = [t for t in chk.targets if self._queryable(t)]
        objs = list(targets)
        if chk.pattern is None and chk.field is not None and any(t in PERSON_OBJECTS for t in targets):
            holder = cat.objects.get(chk.obj)
            if holder is not None:
                objs += list(cat.person_lookups(holder, system=True))
            for t in targets:
                objs += [lk.src for lk in cat.linked_from(t)]
        return [o for o in dict.fromkeys(objs) if self._queryable(o)]

    def _ground(self, call: _Call, outs: list[FilterOut], base: Any = False) -> None:
        """Ground the name checks of filter outputs before the main statement (G1 once per object for all names)."""
        flags = base if isinstance(base, list) else [bool(base)] * len(outs)
        pairs = [(chk, out, fl) for out, fl in zip(outs, flags) for chk in out.checks]
        if not pairs:
            return
        need: dict[str, list[str]] = {}
        for chk, _o, _f in pairs:
            names = _check_names(chk)
            if chk.pattern is None and names:
                for o in self._g1_objects(chk):
                    need.setdefault(o, []).extend(norm_name(n) for n in names)
        for o, norms in need.items():
            self._g1(call, o, norms)
        for chk, out, fl in pairs:
            self._ground_one(call, chk, out, fl)

    def _ground_one(self, call: _Call, chk: NameCheck, out: FilterOut, base: bool) -> None:
        cat = self.catalog
        targets = [t for t in chk.targets if self._queryable(t)]
        if not targets:
            return  # names of a sensitive or nameless object cannot be read: nothing to check
        found: dict[str, list[tuple]] = {}
        vtxt = "', '".join(chk.raw or chk.names)
        if chk.ids:
            found.update(self._by_ids(call, targets, chk.ids))
            tprefixes = {cat.objects[t].id_prefix for t in targets if cat.objects[t].id_prefix}
            missing_ids = [i for i in chk.ids if str(i)[:3] in tprefixes  # only Ids of an object whose records are read
                           and not any(r[0] == id18(i) for rows in found.values() for r in rows)]
            if missing_ids and chk.positive:
                f0 = chk.field
                tt = cat.target_text(chk.target) if len(targets) == 1 else " or ".join(cat.label_of(t) for t in targets)
                if f0 is not None and f0.unresolved > 0:   # the lookup holds Ids of records that are not synced
                    call.note(f"no {tt} record in the warehouse has Id '{missing_ids[0]}'; {chk.obj}.{f0.api} also holds "
                              f"Ids of records that are not in the warehouse ({f0.unresolved} values), so the count still "
                              "uses the Id as given")
                elif len(missing_ids) == len(chk.ids):
                    raise RecordError(f"no {tt} record has Id '{missing_ids[0]}'. Check the Id, or filter with the "
                                      f"record's name (search_records finds a record by name).", "E4h")
                else:
                    call.note(f"no {tt} record has Id '{missing_ids[0]}'; the other Ids matched")
        elif chk.pattern is not None:
            for t in targets:
                rows = self._g1(call, t, pattern=chk.pattern).get(f"~{chk.pattern}", [])
                if rows:
                    found[t] = rows
            n_rec = sum(len(r) for r in found.values())
            names = list(dict.fromkeys(_disp(r[1]) for rows in found.values() for r in rows))
            if not found and chk.positive:
                raise RecordError(self._e4h(call, chk, chk.names[0], contains=True), "E4h")
            if n_rec > 50:
                call.note(f"'{vtxt}' matches more than 50 {self._tlabel(chk)} records; other lookups were not checked")
                chk.kind = "many"
            elif len(names) >= 2 and chk.positive:
                call.note(f"'{vtxt}' matches {len(names)} different {self._tlabel(chk)} names ({_and_more(names)}); "
                          "the counts combine them")
        else:
            missing: list[str] = []
            for n in chk.names:
                nv = norm_name(n)
                hit = False
                for t in targets:
                    rows = self._g1(call, t, [nv]).get(nv, [])
                    if rows:
                        hit = True
                        lst = found.setdefault(t, [])
                        lst += [r for r in rows if r not in lst]
                if not hit:
                    missing.append(n)
            for _v, pick in chk.picked.items():
                o, rid, name, det = pick
                lst = found.setdefault(o, [])
                if all(r[0] != rid for r in lst):
                    lst.append((rid, name, det))
            # Same-name records of the other person objects (a picked 'Name (#k)' value included: the Employee with
            # the name of a picked candidate record is still a different record with the same name).
            for o in self._g1_objects(chk):
                if o in targets:
                    continue
                rows = [r for n in _check_names(chk) for r in self._g1(call, o, [norm_name(n)]).get(norm_name(n), [])]
                if rows:
                    found[o] = list(dict.fromkeys(rows))
            chk.missing = missing
            if missing:
                if chk.positive and len(missing) == len(chk.names) and not chk.picked:
                    text = self._e4h(call, chk, missing[0])
                    if len(missing) > 1:
                        text += " Also not found: " + ", ".join(f"'{m}'" for m in missing[1:]) + "."
                        for m in missing[1:]:
                            self._not_found(call, chk, m)
                    raise RecordError(text, "E4h")
                tl = self._tlabel(chk)
                for m in missing:
                    if chk.op == "in":
                        call.note(f"no {tl} is named '{m}'; the other names matched")
                    else:
                        fits = ""
                        rows = [r for t in targets for r in self._g2(call, t, norm_name(m))]
                        first = self._close_parts(rows, m, sentence=False)
                        if first:
                            fits = f"; first name fits: {first}"
                        call.note(f"no {tl} is named '{m}'{fits}; this filter excludes nothing for it")
        chk.found = found
        tfound = [(t, r) for t in targets for r in found.get(t, [])]
        if chk.kind != "many":
            if not tfound:
                chk.kind = "other"
            elif any(t == "Account" for t, _r in tfound):
                chk.kind = "candidate" if any(cat.is_person_account(r[2]) for t, r in tfound if t == "Account") else "other"
            else:
                chk.kind = cat.person_kind(tfound[0][0], tfound[0][1][2])
        # Account records that are not candidates (N4).
        accs = [r for t, r in tfound if t == "Account" and not cat.is_person_account(r[2])] if chk.field is not None else []
        if accs:
            rt = accs[0][2] or "(none)"
            text = (f"'{vtxt}' matches {len(accs)} Account record(s) of record type '{rt}', which are not candidates "
                    "(candidates are Person Accounts)")
            ids = self._identities(call, [("Account", r[0]) for r in accs])
            for r in accs:
                anchor, members = ids.get(r[0], (None, []))
                if anchor is not None:
                    lk = next((m[0] for m in members if m[2] == r[0]), None)
                    text += (f"; it is the own account of {cat.label_of(anchor[0])} '{_disp(anchor[2])}'"
                             + (f" ({lk.label})" if lk else ""))
                    break
            call.note(text)
        # The lookup itself: empty, almost empty, pointing to missing records (N5, N5b, N6).
        f = chk.field
        holder = cat.objects.get(chk.obj)
        if f is not None and holder is not None:
            if chk.positive and f.filled <= 0:
                call.no_data.append(f"{holder.api}.{f.api}")   # structured twin of N5 (session answer check)
                call.note(f"{holder.api}.{f.api} is blank on every {holder.api} record in the warehouse (0 of "
                          f"{holder.rows}), so no record can match '{vtxt}': the data does not record this; do not report "
                          f"it as 0 for '{vtxt}'")
            elif chk.positive and cat.almost_empty(holder, f):
                sibs = [g for g in cat.same_target_lookups(holder, f) if g.filled > 0 and g.api not in SYSTEM_LOOKUPS][:4]
                call.note(f"{holder.api}.{f.api} is filled on only {f.filled} of {holder.rows} {holder.api} records (under "
                          f"1%); if the question means another role, the other lookups of {holder.api} to "
                          f"{self._tlabel(chk)} are " + (", ".join(f"{g.api} ({_role(g)})" for g in sibs) or "none"))
            if f.unresolved > 0:
                call.note(self._n6(holder, f))
        call.name_checks.append((chk, out, base))

    def _n6(self, holder: ObjectSpec, f: FieldSpec) -> str:
        t = self.catalog.target_of(f)
        tl = "User or Group" if f.api == "OwnerId" else self.catalog.label_of(t) if t else "related"
        return (f"{f.unresolved} of the {f.filled} values of {holder.api}.{f.api} point to {tl} records that are not in "
                "the warehouse; records holding them cannot match a name and group as '(unresolved id)'")

    def _tlabel(self, chk: NameCheck) -> str:
        return " or ".join(self.catalog.label_of(t) for t in chk.targets)

    def _not_found(self, call: _Call, chk: NameCheck, v: str, contains: bool = False,
                   close: list[tuple[str, int]] | None = None) -> None:
        """Log a name the filter found no record for (``QueryEvidence.not_found``): code builds the not-found answer
        from it when the record agent's reply is dropped. ``close`` holds the close names the error lists (G2 rows):
        a later value list that holds one of them is data about the corrected name (session ``_data_found``)."""
        cat = self.catalog
        targets = [t for t in chk.targets if self._queryable(t)]
        entry = {"name": str(v), "target": cat.target_text(chk.target) if len(targets) == 1
                 else " or ".join(cat.label_of(t) for t in targets), "object": chk.obj,
                 "field": chk.field.api if chk.field is not None else None, "match": "contains" if contains else "exact",
                 "close": list(dict.fromkeys(name for name, _how in close or []))[:13]}
        if entry not in call.not_found:
            call.not_found.append(entry)

    def _e4h(self, call: _Call, chk: NameCheck, v: str, contains: bool = False) -> str:
        """Error E4h: no record of the target has the name (lists the names that fit, never counts).

        With the spelling help on (``catalog.resolver``, docs/resolver_integration.md step (5)), the stored names the
        resolver offers for ``v`` (``corrected`` or ``ambiguous``; records of the filter's targets only) come first
        among the close spellings, in the error text and in ``not_found`` "close", as E15a does
        (:meth:`PeopleMixin._about_people`). They are only listed: the filter never binds them, the code stays E4h and
        the count never runs."""
        cat = self.catalog
        targets = [t for t in chk.targets if self._queryable(t)]
        tt = cat.target_text(chk.target) if len(targets) == 1 else " or ".join(cat.label_of(t) for t in targets)
        tl = self._tlabel(chk)
        nv = norm_name(v)
        text = f"no {tt} record name contains '{v}'." if contains else f"no {tt} record is named '{v}'."
        rows = [r for t in targets for r in self._g2(call, t, nv)]
        speller = getattr(cat, "resolver", None)
        if speller is not None:   # spelling help: the resolver's names first among the close spellings (as E15a)
            dym = _suggested_names(self, speller, v, set(targets))
            rows = [(n, 3) for n in dym] + [r for r in rows if r[0] not in dym]
        self._not_found(call, chk, v, contains, rows)
        text += self._close_parts(rows, v)
        holder = cat.objects.get(chk.obj)
        user_srcs = [lk.src for lk in cat.linked_from("User")] if "User" in targets else []
        if not contains and holder is not None and chk.field is not None:
            for o in self._g1_objects(chk):
                if o in targets or o in user_srcs:
                    continue
                if not self._g1(call, o, [nv]).get(nv):
                    continue
                looks = [g for g in cat.person_lookups(holder, system=True).get(o, []) if g.filled > 0][:4]
                text += f" '{v}' is the name of {cat.target_text(o)} record(s)"
                text += (f"; lookups of {holder.api} to {o}: " + ", ".join(f"{g.api} ({_role(g)})" for g in looks)
                         if looks else f"; {holder.api} has no lookup to {o} with data") + "."
            for src in dict.fromkeys(user_srcs):
                recs = self._g1(call, src, [nv]).get(nv, [])
                if not recs:
                    continue
                ids = self._identities(call, [(src, r[0]) for r in recs])
                users = [(m[3], m[0].label) for r in recs for m in ids.get(r[0], (None, []))[1] if m[1] == "User"]
                slabel = cat.label_of(src)
                if users:
                    text += (f" '{v}' is {slabel} ({src}): User lookups hold Salesforce users, and this {slabel} is "
                             "linked to " + ", ".join(f"'{_disp(n)}' ({lab})" for n, lab in dict.fromkeys(users))
                             + "; filter with that name.")
                else:
                    labels = [lk.label for lk in cat.identity_links(src) if lk.dst == "User"]
                    ulooks = [g.api for g in (cat.person_lookups(holder, system=True).get("User") or []) if g.filled > 0]
                    text += (f" '{v}' is {slabel} ({src}) with no linked user ({_and(labels)} find no user), so User "
                             f"lookups ({', '.join(ulooks) or 'none'}) cannot match this {slabel}.")
        text += (f" Filter with a name exactly as listed, or call search_records(name='{v}'); if none of them is meant, "
                 f"answer that no {tl} has this name, without a count.")
        return text

    # ------------------------------------------------------------------ after the handler
    def _people(self, call: _Call, chk: NameCheck) -> dict[str, list[dict]]:
        """Per normalised name: the different people of the target holding it, ``{"label","detail","ids"}``; two
        logins of one employee are one person."""
        targets = [t for t in chk.targets if self._queryable(t)]
        by_name: dict[str, list[tuple]] = {}
        for t in targets:
            for r in chk.found.get(t, []):
                by_name.setdefault(norm_name(r[1]), []).append((t, r))
        out: dict[str, list[dict]] = {}
        for nv, recs in by_name.items():
            recs.sort(key=lambda x: x[1][0])
            anchors: dict[str, str] = {}
            users = [r for t, r in recs if t == "User"]
            if len(users) >= 2:
                ids = self._identities(call, [("User", r[0]) for r in users])
                for r in users:
                    a = ids.get(r[0], (None, []))[0]
                    if a is not None:
                        anchors[r[0]] = a[1]
            people: list[dict] = []
            for k, (t, r) in enumerate(recs, 1):
                key = anchors.get(r[0])
                same = next((p for p in people if key and p.get("anchor") == key), None)
                if same is not None:
                    same["ids"].append(r[0])
                    same["records"] += 1
                    continue
                people.append({"label": f"{_disp(r[1])} (#{k})", "detail": r[2], "ids": [r[0]], "anchor": key,
                               "records": 1, "object": t})
            out[nv] = people
        return out

    def _finish_names(self, call: _Call) -> dict:
        """After the handler: lookup checks of base person filters (at most 2), N1, N3, N3b and N11; list_records
        also gets ``also_held`` (C8: the rows of the other lookups that hold the filter's person)."""
        lcs: list[dict] = []
        done = 0
        for chk, out, base in call.name_checks:
            try:
                lc = self._finish_check(call, chk, out, base, done)
            except RecordError:   # best effort: the extra statements never fail the call (N12)
                lc = None
                call.note(f"other lookups of {chk.obj} were not checked (the check failed)")
            if lc is not None:
                lcs.append(lc)
                done += 1
        res: dict = {"lookup_check": lcs} if lcs else {}
        if call.also_held:
            res["also_held"] = list(call.also_held)
            call.note("also_held: the rows where another lookup of the object holds the same person (a linked record "
                      "of the person, or another lookup to the same object) and this filter does not, with the same "
                      "columns; they are not in rows and total_count (total: how many such records)")
        return res

    def _finish_check(self, call: _Call, chk: NameCheck, out: FilterOut, base: bool, done: int) -> dict | None:
        """One name check after the handler: its lookup check (or N1) and the notes on same-name records."""
        cat = self.catalog
        people = self._people(call, chk) if chk.kind != "many" and chk.pattern is None else {}
        lc = None
        person = chk.field is not None and any(t in PERSON_OBJECTS for t in chk.targets)
        tfound = any(chk.found.get(t) for t in chk.targets)
        eligible = (base and chk.field is not None and chk.on_base and chk.positive and tfound
                    and chk.kind != "many" and call.main_scope is not None)
        if eligible and done < MAX_LOOKUP_CHECKS:
            holder = cat.objects[chk.obj]
            if call.remaining() < CHECK_MIN_BUDGET_S:
                call.note(f"other lookups of {holder.api} were not checked (time budget)")
            else:
                try:
                    lc = self._lookup_check(call, chk, out, people)
                except RecordError:
                    call.note(f"other lookups of {holder.api} were not checked (the check failed)")
                    lc = None
        elif person and chk.positive and chk.field is not None and not eligible:
            self._n1(call, chk)
        per = {b["record"]: b["records"] for b in (lc or {}).get("by_record", [])}
        if not per and base:   # no lookup check split them: one statement gives each same-name record its own count
            per = self._per_record(call, chk, out, people)
        for nv, ppl in people.items():
            v = next((n for n in chk.names if norm_name(n) == nv), None) or (ppl[0]["label"].rsplit(" (#", 1)[0])
            tl = self._tlabel(chk)
            n_rec = sum(p["records"] for p in ppl)
            if len(ppl) == 1 and n_rec >= 2 and ppl[0]["object"] == "User" and ppl[0]["anchor"]:
                a = self._fwd_src.get(ppl[0]["anchor"])
                labels = [m[0].label for m in self._fwd.get(ppl[0]["anchor"], []) if m[2] in ppl[0]["ids"]]
                call.note(f"'{v}' matches {n_rec} User records that are logins of the same "
                          f"{cat.label_of(a[0]) if a else 'person'} '{_disp(a[2]) if a else v}' "
                          f"({', '.join(dict.fromkeys(labels))}); this result counts them together")
            elif len(ppl) > 5:
                call.note(f"'{v}' matches {len(ppl)} {tl} records with the same name; this result counts them together")
            elif len(ppl) >= 2:
                listed = ", ".join(f"{p['label']} {p['detail'] or ''}".strip() for p in ppl)
                text = (f"'{v}' matches {len(ppl)} {tl} records with the same name ({listed}); this result counts them "
                        "together")
                if per:
                    text += "; per record: " + ", ".join(f"#{p['label'].rsplit('#', 1)[1].rstrip(')')} "
                                                         f"{per.get(p['label'], 0)}" for p in ppl)
                text += f"; filter '{v} (#k)' for one of them"
                call.note(text)
        return lc

    def _per_record(self, call: _Call, chk: NameCheck, out: FilterOut, people: dict[str, list[dict]]) -> dict[str, int]:
        """The records of the main query per same-name record of a base person filter (2 to 5 records with one name)
        when no lookup check counted them (not eligible, the third name check, the time budget): ``{label: n}``, one
        statement with one ``COUNT(*) FILTER`` per record over the query's other conditions; {} when it cannot run.
        Records with the same name are never merged: each is counted on its own."""
        scope = call.main_scope
        f = chk.field
        split = [p for ppl in people.values() if 2 <= len(ppl) <= 5 for p in ppl]
        if not split or scope is None or f is None or not chk.on_base or not chk.positive or f.api not in scope.spec.fields \
                or call.remaining() < 0.3:
            return {}
        alias = scope.alias
        sel = [Frag(f'COUNT(*) FILTER (WHERE {alias}.{quote_ident(f.api)} = ANY(?)) AS "d{j}"', (list(p["ids"]),))
               for j, p in enumerate(split, 1)]
        others = [c for c in call.main_conds if c is not out.frag]
        try:
            st = call.run(Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM {scope.from_sql()}") + where_sql(others),
                          max_rows=1, timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.2)))
        except RecordError:
            return {}
        row = st.rows[0] if st.rows else [0] * len(split)
        return {p["label"]: int(n or 0) for p, n in zip(split, row)}

    def _unsynced_check(self, call: _Call) -> dict:
        """``unsynced_fits`` and a note when the counted object has a documented lookup to a person object that is not
        synced and whose role fits the question ("evaluations ... done" -> the evaluator lookup), while this query
        reaches a person through another lookup or path: the warehouse does not record what the question asks, and
        this result is only a proxy. Nothing when a person lookup of the object itself fits the same question."""
        scope = call.main_scope
        if scope is None or not self._question:
            return {}
        cat = self.catalog
        base = scope.spec
        if not base.not_synced:
            return {}
        names = [n for chk, _o, _b in call.name_checks for n in (chk.raw or chk.names)]
        q = _without_words(self._question, names)
        used: list[tuple[str, FieldSpec, str]] = []
        for o, fapi in dict.fromkeys(call.fields_used):
            spec = cat.objects.get(o)
            g = spec.fields.get(fapi) if spec is not None else None
            t = cat.target_of(g) if g is not None and g.kind == "reference" else None
            if t in PERSON_OBJECTS:
                used.append((o, g, t))
        if not used:
            return {}
        # the person kind per person object: from the grounded names, else Employee/User employee, Account/Lead candidate
        kinds: dict[str, str] = {}
        for chk, _o, _b in call.name_checks:
            if chk.kind in ("employee", "candidate"):
                for t in chk.targets:
                    kinds.setdefault(t, chk.kind)
        for _o, _g, t in used:
            kinds.setdefault(t, "employee" if t in ("Recruiter__c", "User") else "candidate")
        fits = cat.not_synced_fits(q, base, kinds)
        if not fits:
            return {}
        for o, g, t in used:
            if o == base.api and cat.role_fits(q, base, g, kinds.get(t, "other")):
                return {}   # a synced lookup of this object answers the asked role
        proxy = list(dict.fromkeys(f"{o}.{g.api}" for o, g, _t in used))[:3]
        out = []
        for x in fits[:2]:
            out.append({**x, "proxy": proxy})
            call.note(f"{base.api}.{x['field']} ({x['role']}) fits the question's word '{x['fits']}' but is documented "
                      "only, not synced to the warehouse, so the data does not record it: say that first, never 0 or "
                      f"none for it; this result reaches the person through {', '.join(proxy)} instead, which is only "
                      "a proxy: call it one")
        return {"unsynced_fits": out}

    def _n1(self, call: _Call, chk: NameCheck) -> None:
        cat = self.catalog
        holder = cat.objects.get(chk.obj)
        f = chk.field
        if holder is None or f is None:
            return
        sibs = [g for g in cat.same_target_lookups(holder, f) if g.filled > 0 and g.api not in SYSTEM_LOOKUPS]
        if not sibs:
            return
        t = cat.target_of(f)
        call.note(f"{f.api} is one of {len(sibs) + 1} lookups from {holder.api} to {cat.label_of(t) if t else 'it'}; "
                  f"this result uses {f.api} ({_role(f)}); the others are "
                  + ", ".join(f"{g.api} ({_role(g)})" for g in sibs[:6]))

    # ------------------------------------------------------------------ paths through parents and children (C5)
    def _parent_lookups(self, o: str, exclude: set[str] | frozenset = frozenset()) -> list[FieldSpec]:
        """Lookups of ``o`` with data to a parent that is no person object, has a table and records, can be queried
        and is not sensitive (no system lookup, record type or identity link; not ``o`` itself or ``exclude``)."""
        cat = self.catalog
        spec = cat.objects[o]
        ident = {(lk.src, lk.col) for lk in cat.links} | {(lk.dst, lk.dst_col) for lk in cat.links if lk.dst_col != "Id"}
        out = []
        for g in spec.fields.values():
            if g.kind != "reference" or g.filled <= 0 or g.api in SYSTEM_LOOKUPS or g.api == "RecordTypeId" or \
                    g.sensitivity == "blocked" or g.api.lower() in cat.blocked_columns or (o, g.api) in ident:
                continue
            c = cat.target_of(g)
            if not c or c == o or c in exclude or c in PERSON_OBJECTS or not self._queryable(c) or cat.objects[c].rows <= 0:
                continue
            out.append(g)
        return out

    def _parent_paths(self, base: ObjectSpec, f: FieldSpec) -> list[dict]:
        """Paths from ``base`` to the target of lookup ``f`` through one or two parent records (C5, item 8): ``base.P
        -> C.K`` and ``base.P -> D.Q -> C.K`` over parents that are no person objects (:meth:`_parent_lookups`), where
        the last lookup ``K`` points to the same target with the SAME role (``ref_role``) as ``f``: a session's
        candidate through its training's candidate, never a host through a training's trainer. At most
        PATHS_MAX, the most filled first; cached. ``[{"hops": [(object, lookup)], "obj": C, "field": K, "path",
        "short"}]`` ("<P> -> <K>"); [] for a system lookup, a lookup without a role, a lookup that does not hold the
        record's subject (:meth:`_subject_role`: a trainer or an interviewer of a step has a holder of its own, so the
        training's trainer is no path to it; a candidate's role carries over from a training to its sessions) and rows
        of one person in a role (:meth:`_role_rows`: an attendee row)."""
        key = (base.api, f.api)
        cache = self.__dict__.setdefault("_paths_cache", {})
        if key in cache:
            return cache[key]
        cat = self.catalog
        t = cat.target_of(f)
        out: list[tuple[tuple, dict]] = []
        if t is not None and f.api not in SYSTEM_LOOKUPS and f.ref_role and f.ref_role != "undocumented" \
                and self._subject_role(base, f) and not self._role_rows(base, f):
            def ends(c: str) -> list[FieldSpec]:
                cs = cat.objects[c]
                return [k for k in cs.fields.values() if k.kind == "reference" and k.filled > 0
                        and k.api not in SYSTEM_LOOKUPS and k.sensitivity != "blocked" and cat.target_of(k) == t
                        and k.ref_role == f.ref_role and k.api.lower() not in cat.blocked_columns]
            for p in self._parent_lookups(base.api):
                c = cat.target_of(p)
                for k in ends(c):
                    out.append(((-p.filled, -k.filled, 1), {"hops": [(base.api, p.api)], "obj": c, "field": k.api,
                                                            "path": f"{base.api}.{p.api} -> {c}.{k.api}",
                                                            "short": f"{p.api} -> {k.api}"}))
                for q in self._parent_lookups(c, exclude={base.api}):
                    d = cat.target_of(q)
                    for k in ends(d):
                        out.append(((-p.filled, -k.filled, 2), {
                            "hops": [(base.api, p.api), (c, q.api)], "obj": d, "field": k.api,
                            "path": f"{base.api}.{p.api} -> {c}.{q.api} -> {d}.{k.api}",
                            "short": f"{p.api} -> {q.api} -> {k.api}"}))
        out.sort(key=lambda x: (x[0], x[1]["path"]))
        cache[key] = [x[1] for x in out][:PATHS_MAX]
        return cache[key]

    def _subject_role(self, spec: ObjectSpec, g: FieldSpec) -> bool:
        """Lookup ``g`` of ``spec`` holds the record's subject: it points to a candidate object (a Lead, a Person
        Account) and its role words (:meth:`Catalog.role_words`) start with a stem that only the candidate side of
        :data:`ROLE_CUES` has (:data:`SUBJECT_STEMS`: "candidate", "applicant", "lead"), or an undocumented lookup
        has no role word besides its target's name ("Lead__c"). A referrer, a client account, an employee's own
        account, a parent or a converted account is no subject."""
        cat = self.catalog
        t = cat.target_of(g) if g.kind == "reference" else None
        if t not in PERSON_OBJECTS or cat.person_kind(t, "Person Account") != "candidate":
            return False
        words = [w for w in cat.role_words(spec, g) if w != "id"]
        if not words:
            return not g.ref_role or g.ref_role == "undocumented"
        return any(w.startswith(SUBJECT_STEMS) for w in words)

    def _role_rows(self, spec: ObjectSpec, f: FieldSpec | None = None) -> bool:
        """``spec`` holds one row per person and role (an attendee row): a role or purpose picklist with data has a
        value that is a word of the role of one of the object's person lookups with data (Role__c 'Candidate' next to
        Candidate__c). The parents of such a row belong to every person on it, so they say nothing about whose row it
        is: no lookup of the object gets parent paths (also its Lead__c, whose role no value names). ``f`` is not
        read (the object decides); cached."""
        cache = self.__dict__.setdefault("_role_rows_cache", {})
        if spec.api in cache:
            return cache[spec.api]
        cat = self.catalog
        words = {w for g in spec.fields.values() if g.kind == "reference" and g.filled > 0 and g.ref_role
                 and g.ref_role != "undocumented" and cat.target_of(g) in PERSON_OBJECTS
                 for part in g.ref_role.split("_") for w in norm_name(part).split() if len(w) >= 3}
        cache[spec.api] = bool(words) and self._role_value(spec, words)
        return cache[spec.api]

    def _role_value(self, spec: ObjectSpec, words: set[str]) -> bool:
        """A role or purpose picklist of ``spec`` with data has a value that is one of ``words``."""
        for g in spec.fields.values():
            if g.kind != "picklist" or not g.has_data or g.sensitivity or \
                    not any(w in ("role", "purpose") for w in split_api_name(g.api).split()):
                continue
            vals = [v for v, _lab, _m in g.picklist] or [v for v, _n in (getattr(self.catalog.values_of(spec.api, g.api),
                                                                                   "values", None) or [])]
            if any(norm_name(v) in words for v in vals):
                return True
        return False

    @staticmethod
    def _path_frag(col: str, path: dict, ids: list[str], n: int) -> Frag:
        """``col IN (parents whose ... K holds ids)`` for one :meth:`_parent_paths` path, every column alias-qualified
        (aliases pa<n>, pb<n>), the Ids bound."""
        hops = path["hops"]
        inner = Frag(f'SELECT pa{n}."Id" FROM main.{quote_ident(path["obj"])} AS pa{n} WHERE '
                     f'pa{n}.{quote_ident(path["field"])} = ANY(?)', (list(ids),))
        if len(hops) == 2:
            c, q = hops[1]
            inner = (Frag(f'SELECT pb{n}."Id" FROM main.{quote_ident(c)} AS pb{n} WHERE pb{n}.{quote_ident(q)} IN (')
                     + inner + Frag(")"))
        return Frag(f"{col}.{quote_ident(hops[0][1])} IN (") + inner + Frag(")")

    def _related_children(self, base: ObjectSpec, f: FieldSpec, targets: dict[str, list[str]]) -> list[dict]:
        """Child objects of ``base`` whose rows hold the filter's person in a lookup that shares a role word with ``f``
        (C5 ``related``, item 7d): a Candidate Owner on trainings next to an account's OwnerId, an attendee row's
        candidate next to a session's candidate. ``targets``: person object -> the person's Ids there (the filter's
        target and the linked identities). At most RELATED_MAX objects, the most filled first, one entry per (object,
        lookup); ``same_role`` when the role is the same (``ref_role``). ``[{"object", "via", "field", "ids", "role",
        "same_role"}]``.

        A child row in the same role counts only for a lookup that holds the record's subject (:meth:`_subject_role`:
        a candidate's attendee rows next to a session's candidate; a step's trainer has a holder of its own, so it is
        no training's trainer). A role that only shares a word counts only when ``base`` is a candidate object and the
        child points to it through its subject lookup (a Candidate Owner of the candidate's trainings next to the
        account's OwnerId; never the owner of a program's or a cohort's trainings, and never a child that points to an
        employee through a staff lookup such as a department's head)."""
        cat = self.catalog
        ident = {(lk.src, lk.col) for lk in cat.links} | {(lk.dst, lk.dst_col) for lk in cat.links if lk.dst_col != "Id"}
        f_subject = self._subject_role(base, f)
        base_subject = base.api in PERSON_OBJECTS and cat.person_kind(base.api, "Person Account") == "candidate"
        out: list[dict] = []
        for ch in base.children:
            xs = cat.objects.get(ch.child)
            if xs is None or not xs.has_table or xs.sensitive or ch.child == base.api or not self._queryable(ch.child):
                continue
            k = xs.fields.get(ch.via)
            if k is None or k.filled <= 0 or k.api in SYSTEM_LOOKUPS or k.sensitivity == "blocked" or \
                    cat.target_of(k) != base.api or (ch.child, k.api) in ident:
                continue
            k_subject = base_subject and self._subject_role(xs, k)
            for g in xs.fields.values():
                if g is k or g.kind != "reference" or g.filled <= 0 or g.api in SYSTEM_LOOKUPS or \
                        g.sensitivity == "blocked" or g.api.lower() in cat.blocked_columns or (ch.child, g.api) in ident:
                    continue
                gt = cat.target_of(g)
                if gt not in targets or not targets[gt] or not cat.shared_role_words(base, f, xs, g):
                    continue
                same = bool(f.ref_role) and g.ref_role == f.ref_role
                if (same and not f_subject) or (not same and not k_subject):
                    continue
                out.append({"object": ch.child, "via": k.api, "field": g.api, "ids": list(targets[gt]), "target": gt,
                            "role": _role(g), "same_role": same, "filled": g.filled})
        out.sort(key=lambda x: (-x["filled"], x["object"], x["field"], x["via"]))
        seen: set[tuple[str, str]] = set()
        uniq = []
        for x in out:   # one entry per (object, lookup): the most filled path to the base
            if (x["object"], x["field"]) not in seen:
                seen.add((x["object"], x["field"]))
                uniq.append(x)
        objs = list(dict.fromkeys(x["object"] for x in uniq))[:RELATED_MAX]
        return [x for x in uniq if x["object"] in objs]

    def _related_counts(self, call: _Call, scope: Scope, others: list[Frag], rel: list[dict]) -> list[dict]:
        """Rows and different base records of each :meth:`_related_children` entry, one statement per (object, lookup
        to the base), over the rows that point to a base record that meets the query's other conditions (with none: to
        a base record in the warehouse; a row whose lookup to the base is blank is no row of a base record); entries
        that reach a base record."""
        by_obj: dict[tuple[str, str], list[dict]] = {}
        for x in rel:
            by_obj.setdefault((x["object"], x["via"]), []).append(x)
        out: list[dict] = []
        for (o, via), items in by_obj.items():
            if call.remaining() < 0.5:
                break
            sel: list[Frag] = []
            for j, x in enumerate(items, 1):
                g, k = quote_ident(x["field"]), quote_ident(x["via"])
                sel.append(Frag(f'COUNT(*) FILTER (WHERE rx.{g} = ANY(?)) AS "r{j}"', (list(x["ids"]),)))
                sel.append(Frag(f'COUNT(DISTINCT rx.{k}) FILTER (WHERE rx.{g} = ANY(?)) AS "b{j}"', (list(x["ids"]),)))
            frag = (Frag("SELECT ") + join_frags(", ", sel)
                    + Frag(f' FROM main.{quote_ident(o)} AS rx WHERE rx.{quote_ident(via)} IN (SELECT {scope.alias}."Id" '
                           f'FROM {scope.from_sql()}') + where_sql(others) + Frag(")"))
            try:
                st = call.run(frag, max_rows=1, timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.3)))
            except RecordError:
                continue
            call.table(o)
            row = dict(zip(st.columns, st.rows[0])) if st.rows else {}
            for j, x in enumerate(items, 1):
                n, b = int(row.get(f"r{j}") or 0), int(row.get(f"b{j}") or 0)
                if n > 0 and b > 0:
                    out.append({**{k: v for k, v in x.items() if k not in ("ids", "filled")}, "rows": n, "records": b})
        return out

    def _lookup_check(self, call: _Call, chk: NameCheck, out: FilterOut, people: dict[str, list[dict]]) -> dict | None:
        """One statement: the filter's own count and the same person through the object's other lookups (L3)."""
        cat = self.catalog
        scope = call.main_scope
        base = scope.spec
        f = chk.field
        t = cat.target_of(f)
        if t is None:
            return None
        q = _without_words(self._question, chk.raw or chk.names)
        kind = chk.kind if chk.kind in ("employee", "candidate") else "other"
        is_person = kind != "other"   # a niche, a department, a company: no person wording, no reference scan
        tobjs = [x for x in chk.targets if self._queryable(x)]
        t_recs = [(o, r) for o in tobjs for r in chk.found.get(o, [])]
        t_ids = [r[0] for o, r in t_recs if o == t]
        ident = self._identities(call, [(o, r[0]) for o, r in t_recs])
        own_ids = {r[0] for _o, r in t_recs}
        linked: dict[str, list[tuple[str, str, str]]] = {}   # object -> (Id, name, label)
        for o, r in t_recs:
            anchor, members = ident.get(r[0], (None, []))
            if anchor is not None and anchor[1] not in own_ids:
                lk = next((m[0] for m in members if m[2] == r[0]), None)
                linked.setdefault(anchor[0], []).append((anchor[1], anchor[2], lk.label if lk else "linked record"))
            for m in members:
                if m[2] not in own_ids and all(x[0] != m[2] for x in linked.get(m[1], [])):
                    linked.setdefault(m[1], []).append((m[2], m[3], m[0].label))
        linked_ids = {i for lst in linked.values() for i, _n, _l in lst}
        cands: list[tuple[str, FieldSpec, list[str], str | None]] = []
        empty: list[str] = []
        for g in cat.same_target_lookups(base, f):
            if g.api in SYSTEM_LOOKUPS:
                continue
            if g.filled <= 0:
                empty.append(g.api)
                continue
            if t_ids:
                cands.append(("same_target", g, t_ids, None))
        plooks = cat.person_lookups(base)
        for o, lst in linked.items():
            if o == t:
                continue
            for g in plooks.get(o, []):
                if g.filled > 0 and g is not f:
                    cands.append(("linked", g, [i for i, _n, _l in lst], "; ".join(dict.fromkeys(lab for _i, _n, lab in lst))))

        def different(o: str, rows: list[tuple]) -> str:
            dets = ", ".join(dict.fromkeys(str(r[2]) for r in rows if r[2]))
            head = (f"a different {cat.label_of(o)} record with the same name" if len(rows) == 1 else
                    f"{len(rows)} different {cat.label_of(o)} records with the same name")  # every one is counted
            return head + (f" ({dets})" if dets else "")

        own_kind: dict[str, str] = {}   # same-name lookup -> person kind of the records it points to
        for o, rows in chk.found.items():
            if o in tobjs:
                continue
            rows = [r for r in rows if r[0] not in linked_ids]
            if not rows:
                continue
            okind = ("candidate" if any(cat.person_kind(o, r[2]) == "candidate" for r in rows)
                     else cat.person_kind(o, rows[0][2]))
            for g in plooks.get(o, []):
                if g.filled > 0 and g is not f:
                    cands.append(("same_name", g, [r[0] for r in rows], different(o, rows)))
                    own_kind[g.api] = okind
        # System lookups. This filter's users and the users linked to the person are one identity (one count); a User
        # that only shares the name is a different record: counted apart (group same_name), never in the same count.
        own_users: dict[str, str] = {}
        for o, r in t_recs:
            if o == "User":
                own_users[r[0]] = f"'{_disp(r[1])}' (this filter's user)"
        for i, name, lab in linked.get("User", []):
            own_users.setdefault(i, f"'{_disp(name)}' ({lab})")
        other_users = [r for r in (chk.found.get("User", []) if "User" not in tobjs else []) if r[0] not in own_users]
        sys_looks = [base.fields[s] for s in SYSTEM_LOOKUPS if s in base.fields]
        sys_looks = [g for g in sys_looks if g is not f and g.filled > 0 and g.sensitivity != "blocked"]
        for g in sys_looks if own_users else []:
            cands.append(("system", g, list(own_users), "; ".join(dict.fromkeys(own_users.values()))))
        for g in sys_looks if other_users else []:
            cands.append(("same_name", g, [r[0] for r in other_users], different("User", other_users)))
            own_kind[g.api] = "employee"
        # the same person through parent records (item 8), child rows in the same role (7d) and the source of a
        # partial copy (item 9): metadata first, the counts in a second statement (best effort, after the lookup check)
        paths = self._parent_paths(base, f) if is_person and t_ids else []
        targets: dict[str, list[str]] = {t: list(t_ids)} if t_ids else {}
        for o, lst in linked.items():
            targets.setdefault(o, []).extend(i for i, _n, _l in lst)
        copy = f.child_copy if is_person and t_ids and f.child_copy else ()
        related = self._related_children(base, f, targets) if is_person and targets else []
        if copy:   # the copy's source is counted by copy_source: no related entry repeats it
            related = [x for x in related if (x["object"], x["field"]) != (copy[0], copy[2])]
        split = [p for ppl in people.values() if 2 <= len(ppl) <= 5 for p in ppl]
        if split:   # same-name records: the rows of a role that only shares a word are not split by record (left out)
            related = [x for x in related if x["same_role"]]
        if not cands and not paths and not related and not copy:
            return None
        alias = scope.alias
        own = out.frag
        sel: list[Frag] = [Frag("COUNT(*) FILTER (WHERE ") + own + Frag(') AS "n0"')]
        for i, (_grp, g, ids, _via) in enumerate(cands, 1):
            sel.append(Frag(f'COUNT(*) FILTER (WHERE {alias}.{quote_ident(g.api)} = ANY(?)) AS "n{i}"', (list(ids),)))
        st_idx = [i for i, c in enumerate(cands, 1) if c[0] == "same_target"]
        if st_idx:
            anyf = Frag("COUNT(*) FILTER (WHERE (") + own + Frag(")")
            for i in st_idx:
                anyf = anyf + Frag(f" OR {alias}.{quote_ident(cands[i - 1][1].api)} = ANY(?)", (list(cands[i - 1][2]),))
            sel.append(anyf + Frag(') AS "a"'))
        for j, p in enumerate(split, 1):
            sel.append(Frag(f'COUNT(*) FILTER (WHERE {alias}.{quote_ident(f.api)} = ANY(?)) AS "d{j}"', (list(p["ids"]),)))
        others_conds = [c for c in call.main_conds if c is not own]
        frag = Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM {scope.from_sql()}") + where_sql(others_conds)
        st = call.run(frag, max_rows=1)
        same_rel = [x for x in related if x["same_role"]]
        prow = self._path_counts(call, scope, others_conds, own, f, paths, same_rel, copy, t_ids, split) \
            if (paths or same_rel or copy) else {}
        if prow is None:
            paths, same_rel, copy, prow = [], [], (), {}
            related = [x for x in related if not x["same_role"]]
        row = dict(zip(st.columns, st.rows[0])) if st.rows else {}
        n0 = int(row.get("n0") or 0)
        entries: list[tuple[int, dict]] = []
        order = {"same_target": 0, "linked": 1, "same_name": 2, "system": 3}
        for i, (grp, g, _ids, via) in enumerate(cands, 1):
            n = int(row.get(f"n{i}") or 0)
            if grp != "same_target" and n <= 0:
                continue
            e: dict[str, Any] = {"field": g.api, "target": cat.target_of(g), "group": grp}
            if via:
                e["via"] = via
            e.update({"role": _role(g), "fits": cat.role_fits(q, base, g, kind), "records": n,
                      "same_person": grp != "same_name"})
            sparse = cat.sparse_fits(q, base, g, kind) if grp == "same_target" else None
            if sparse:   # the asked role's lookup exists but is almost empty (role_fits never marks it)
                e["fits_sparse"] = sparse
            okind = own_kind.get(g.api) if grp == "same_name" else None
            if okind in ("employee", "candidate") and okind != kind:
                # A different record with the same name, of another person kind (an Employee named like the
                # candidate record of this filter): does the question's verb fit this lookup for THAT kind?
                ofit = cat.role_fits(q, base, g, okind)
                if ofit:
                    e["own_kind"], e["own_fits"] = okind, ofit
            entries.append((order[grp], e))
        if not is_person:   # a department or a niche: another lookup that does not point to the value is no fact
            entries = [(o, e) for o, e in entries if e["group"] != "same_target" or e["records"] > 0]
        entries.sort(key=lambda x: (x[0], -x[1]["records"], x[1]["field"]))
        others = [e for _o, e in entries][:MAX_OTHERS]
        lc: dict[str, Any] = {"filter": out.text, "object": base.api, "field": f.api, "target": t,
                              "target_label": cat.label_of(t), "person_kind": kind, "role": _role(f),
                              "fits": cat.role_fits(q, base, f, kind), "cues": cat.question_cues(q, kind), "records": n0}
        sparse = cat.sparse_fits(q, base, f, kind)
        if sparse:
            lc["fits_sparse"] = sparse
        if st_idx:
            lc["any_role"] = int(row.get("a") or 0)
        lc["others"] = others
        got_paths = [{"path": path["path"], "role": _role(cat.objects[path["obj"]].fields[path["field"]]),
                      "fits": cat.role_fits(q, cat.objects[path["obj"]], cat.objects[path["obj"]].fields[path["field"]],
                                            kind), "records": int(prow.get(f"p{k}") or 0)}
                     for k, path in enumerate(paths, 1)]
        got_paths = [x for x in got_paths if x["records"] > 0]
        rel_out: list[dict] = []
        if related:
            for x in self._related_counts(call, scope, others_conds, related):
                xs = cat.objects[x["object"]]
                rel_out.append({"object": x["object"], "field": x["field"], "via": x["via"], "role": x["role"],
                                "fits": cat.role_fits(q, xs, xs.fields[x["field"]], kind), "records": x["rows"],
                                "distinct": x["records"], "same_role": x["same_role"]})
        if got_paths or any(x["same_role"] for x in rel_out):
            lc["paths"] = got_paths
            lc["any_path"] = int(prow.get("ap") or 0)
        if rel_out:
            lc["related"] = rel_out
        if copy:
            lc["copy_source"] = {"field": f.api, "source": f"{copy[0]}.{copy[2]}", "via": f"{copy[0]}.{copy[1]}",
                                 "records": int(prow.get("cs") or 0)}
        view = call.list_view
        if view is not None and view.get("scope") is scope:   # C8: list_records also lists the rows of those lookups
            ids_of = {(grp, g.api): ids for grp, g, ids, _via in cands}
            for e in others:
                if e["group"] not in ("same_target", "linked") or e["records"] <= 0 or call.remaining() < 0.5 or \
                        len(call.also_held) >= ALSO_LOOKUPS:
                    continue
                ids = ids_of.get((e["group"], e["field"]))
                if not ids:
                    continue
                try:
                    held, total = self._also_rows(call, scope, view, others_conds, own, e["field"], ids)
                except RecordError:
                    continue
                if held:
                    call.also_held.append({"field": e["field"], "via": e.get("via") or e["role"], "group": e["group"],
                                           "rows": held, "total": total})
        if n0 == 0 and empty:
            lc["empty_lookups"] = empty
        nw = cat.not_synced_lookups(base, t)
        if nw:
            lc["not_in_warehouse"] = nw
        if split:
            lc["by_record"] = [{"record": p["label"], "detail": p["detail"], "records": int(row.get(f"d{j}") or 0),
                                "kind": cat.person_kind(p["object"], p["detail"])}
                               for j, p in enumerate(split, 1)]
            for j, b in enumerate(lc["by_record"], 1):   # each same-name record on its own (never merged)
                if "any_path" in lc:
                    b["any_path"] = int(prow.get(f"ap_d{j}") or 0)
                if "copy_source" in lc:
                    b["copy_source"] = int(prow.get(f"cs_d{j}") or 0)
        vtxt = "', '".join(chk.raw or chk.names or chk.ids)
        # How many people the filter covers: an elsewhere scan of several people would mix their counts.
        if chk.pattern is not None or chk.ids:
            n_people = len(own_ids)
        else:
            n_people = sum(len(ppl) for ppl in people.values()) or len(own_ids)
        scan_note, scanned = "", 0
        # The person is not on this object when this lookup, the same-target lookups and the linked identities all
        # count 0. Same-name records (a different Account or Lead with the same name: on the real data most employees
        # have one) and system lookups do not hold this person, so they never block the scan.
        person_zero = all(e["records"] == 0 for e in others if e["group"] in ("same_target", "linked"))
        if n0 == 0 and person_zero and t_recs and is_person:
            if n_people > 1:
                scan_note = "several"
            elif call.remaining() < 0.3:
                scan_note = "skipped"
            else:
                if others_conds:   # other conditions (a period, a status) can explain the 0: look at all records first
                    try:
                        held = self._all_records(call, scope, own, f, [c for c in cands if c[0] in ("same_target",
                                                                                                    "linked")])
                    except RecordError:
                        held = None
                    if held is None:
                        scan_note = "skipped"
                    elif held:
                        lc["all_records"] = held
                if not scan_note and not lc.get("all_records"):
                    recs = [(o, r[0], "same record") for o, r in t_recs]
                    for o, lst in linked.items():
                        recs += [(o, i, lab) for i, _n, lab in lst]
                    via_of = {i: lab for _o, i, lab in recs}
                    pairs = self._child_pairs([(o, i) for o, i, _l in recs], system=False, exclude={base.api})
                    scanned = len({c for c, _v, _i in pairs})
                    try:
                        counts = self._scan(call, pairs, timeout=min(_base.SCAN_BUDGET_S, call.remaining() - 0.2))
                    except RecordError:
                        counts = None
                    if counts is None:
                        scan_note = "skipped"
                    else:
                        hits = sorted(((n, c, v, i) for (c, v, i), n in counts.items() if n > 0),
                                      key=lambda x: (-x[0], x[1], x[2]))[:5]
                        if hits:
                            lc["elsewhere"] = []
                            for n, c, v, i in hits:
                                cs = cat.objects[c]
                                g = cs.fields[v]
                                lc["elsewhere"].append({"object": c, "field": v, "role": _role(g),
                                                        "fits": cat.role_fits(q, cs, g, kind), "records": n,
                                                        "via": via_of.get(i, "same record"), "same_person": True})
                        else:
                            scan_note = "none"
        lc["scope"] = ("all other conditions of this query, before min_value" if call.before_having
                       else "all other conditions of this query")
        # Note N2: the same person through other lookups, then different records that only share the name.
        what = "person" if is_person else "value"
        mine = [e for e in others if e["same_person"]]
        alike = [e for e in others if not e["same_person"]]

        def part(e: dict) -> str:
            if e["target"] == t:
                if not is_person:   # "Team__c (team unit) 0" reads as "0 records have a team unit"
                    return f"{e['field']} ({e['role']}) = '{vtxt}' on {e['records']} records"
                return f"{e['field']} ({e['role']}) {e['records']}"
            return f"{e['field']} ({cat.label_of(e['target'] or '')}, {e.get('via', '')}, {e['role']}) {e['records']}"

        if mine and not is_person:
            text = (f"'{vtxt}' through other lookups of {base.api} (each count is the {base.api} records whose lookup "
                    f"points to '{vtxt}', over all records that meet this query's other conditions; not the records "
                    "where that lookup is filled with any value; a record can hold the same value in several lookups, "
                    f"so never add these counts): {', '.join(part(e) for e in mine)}")
        elif mine:
            text = (f"'{vtxt}' through other lookups of {base.api} (each count is over all {base.api} records that meet "
                    "this query's other conditions, not only the records this result counted; a record can hold the "
                    f"same {what} in several lookups, so never add these counts): {', '.join(part(e) for e in mine)}")
        else:
            text = f"'{vtxt}' is held by no other lookup of {base.api} (all other conditions the same)"
        if mine and "any_role" in lc:
            text += (f"; in any of the {cat.label_of(t)} lookups ({f.api} and the {cat.label_of(t)} lookups above; "
                     f"creator, last editor and owner not included): {lc['any_role']}")
        text += (f"; this result used {f.api} ({_role(f)}) = '{vtxt}': {n0}" if not is_person
                 else f"; this result used {f.api} ({_role(f)}): {n0}")
        if call.before_having:
            text += " (all matching records, before min_value removed groups)"
        if alike:
            text += ("; different records that only share the name (not this person; never present them as this person "
                     "and never add them): " + ", ".join(part(e) for e in alike))
        if lc.get("empty_lookups"):
            text += f"; empty in the warehouse: {', '.join(lc['empty_lookups'])}"
        for x in nw:
            nf = base.not_synced.get(x) or {}
            role = str(nf.get("role") or "").replace("_", " ") or "lookup"
            text += f"; {x} ({role}) also points to {cat.label_of(t)} but is not in the warehouse"
        # With other conditions the base object may still hold the person on records outside them: scope the claim.
        where = " under the other conditions of this query" if others_conds else ""
        not_scanned = ("objects without a knowledge file and sensitive objects are not scanned")
        if lc.get("all_records"):
            held_txt = ", ".join(f"{fld} ({_role(base.fields[fld])}) {n}" for fld, n in lc["all_records"].items())
            text += (f"; '{vtxt}' is on no {base.api} record under the other conditions of this query; over all "
                     f"{base.api} records (no other conditions; never add these): {held_txt}")
        elif lc.get("elsewhere"):
            text += (f"; no lookup of {base.api} holds '{vtxt}'{where}; it is referenced elsewhere (all records, no other "
                     "conditions; never add these): " + ", ".join(f"{e['object']}.{e['field']} ({e['role']}) {e['records']}"
                                                                 for e in lc["elsewhere"])
                     + "; count the object the question's noun names")
        elif scan_note == "none":
            text += (f"; no lookup of {base.api} holds '{vtxt}' on any of its records, and no other object the record "
                     f"tools can query references it ({scanned} other objects scanned; {not_scanned})" if others_conds
                     else f"; no lookup of {base.api} or of any other object the record tools can query holds '{vtxt}' "
                          f"({scanned} other objects scanned; {not_scanned})")
        elif scan_note == "several":
            text += f"; other objects were not checked ('{vtxt}' matches several people)"
        call.note(text)
        if scan_note == "skipped":
            call.note(f"other objects that reference '{vtxt}' were not checked (time budget)")
        def per_rec(key: str) -> str:   # "; per record: #1 a, #2 b" for same-name records (never merged)
            recs = [b for b in lc.get("by_record") or [] if key in b]
            if not recs:
                return ""
            return " (the same-name records together); per record: " + ", ".join(
                f"#{str(b['record']).rsplit('#', 1)[-1].rstrip(')')} {b[key]}" for b in recs)
        if lc.get("paths") is not None:   # C5: through parent records and child rows in the same role (lookup_check)
            call.note(f"'{vtxt}' through the parents of {base.api} records and rows that point to them, in the same role "
                      "(lookup_check paths and related; all other conditions the same; a record can be reached several "
                      f"ways, so never add these): {base.api} records in {f.api} or any of these: {lc['any_path']}"
                      + per_rec("any_path"))
        alike_role = [x for x in rel_out if not x["same_role"]]
        if alike_role:   # a role that shares a word (a Candidate Owner next to OwnerId): a different lookup, never added
            call.note(f"'{vtxt}' also holds a role sharing a word with {f.api} ({_role(f)}) on rows of other objects that "
                      f"point to {base.api} records (all other conditions the same; never add): "
                      + ", ".join(f"{x['object']}.{x['field']} ({x['role']}; through {x['object']}.{x['via']}) on "
                                  f"{x['records']} rows of {x['distinct']} different {base.api} records"
                                  for x in alike_role))
        if lc.get("copy_source"):
            cs = lc["copy_source"]
            # one fact, one note: record_tools._mirror_notes adds it to the copy's own note (else it is noted there)
            call.copy_sources[f.api] = (f"through the source {cs['source']}: {cs['records']} different {base.api} "
                                        f"records have a {copy[0]} row whose {copy[2]} is '{vtxt}' (all other "
                                        f"conditions the same; the copy counted {n0})" + per_rec("copy_source"))
        return lc

    def _also_rows(self, call: _Call, scope: Scope, view: dict, others: list[Frag], own: Frag, field: str,
                   ids: list[str]) -> tuple[list[dict], int]:
        """The rows list_records would list when another lookup of the object (``field``) holds the filter's person
        and the filter itself (``own``) does not (those rows are listed already): same columns and order, the query's
        other conditions, at most ALSO_ROWS rows; and how many such records (one count statement only when there are
        more). ``(rows, total)``."""
        from sf_assistant.engine.query_builder import count_sql, list_sql
        from sf_assistant.tools.record_base import _cell
        conds = list(others) + [Frag(f"{scope.alias}.{quote_ident(field)} = ANY(?)", (list(ids),)),
                                Frag("NOT COALESCE((") + own + Frag("), FALSE)")]
        st = call.run(list_sql(scope, conds, view["cols"], view["order_expr"], view["order"], ALSO_ROWS + 1),
                      max_rows=ALSO_ROWS + 1)
        names, kinds = view["names"], view["kinds"]
        rows = [{names[i]: _cell(r[i + 1], kinds[i]) for i in range(len(names))} for r in st.rows[:ALSO_ROWS]]
        total = len(rows)
        if len(st.rows) > ALSO_ROWS or st.truncated:
            st2 = call.run(count_sql(scope, conds), max_rows=1)
            total = int(st2.rows[0][0]) if st2.rows else total
        return rows, total

    def _path_counts(self, call: _Call, scope: Scope, others: list[Frag], own: Frag, f: FieldSpec, paths: list[dict],
                     same_rel: list[dict], copy: tuple, t_ids: list[str], split: list[dict]) -> dict | None:
        """The counts of the parent paths (``p<k>``), of the filter or any path or same-role child row (``ap``) and
        of the copy's source (``cs``) under the query's other conditions; per same-name record of ``split``
        (``ap_d<j>``, ``cs_d<j>``: each record's own Ids). One statement after the lookup check, within
        SCAN_BUDGET_S; None when it cannot run (the lookup check stands without these)."""
        alias = scope.alias
        cat_t = self.catalog.target_of(f)

        def union(ids: list[str], own_frag: Frag, n0: int) -> Frag:
            parts = [Frag("(") + own_frag + Frag(")")] + [self._path_frag(alias, path, ids, n0 + k)
                                                           for k, path in enumerate(paths, 1)]
            whole = ids is t_ids   # the filter's records with their linked identities; else one same-name record
            parts += [Frag(f'{alias}."Id" IN (SELECT ru{n0 + j}.{quote_ident(x["via"])} FROM '
                           f'main.{quote_ident(x["object"])} AS ru{n0 + j} WHERE ru{n0 + j}.{quote_ident(x["field"])} '
                           '= ANY(?))', (list(x["ids"]) if whole else list(ids),))
                      for j, x in enumerate(same_rel, 1) if whole or x.get("target") == cat_t]
            return join_frags(" OR ", parts)

        def source(ids: list[str]) -> Frag:
            return Frag(f'{alias}."Id" IN (SELECT cs.{quote_ident(copy[1])} FROM main.{quote_ident(copy[0])} AS cs '
                        f'WHERE cs.{quote_ident(copy[2])} = ANY(?))', (list(ids),))

        sel: list[Frag] = []
        for k, path in enumerate(paths, 1):
            sel.append(Frag("COUNT(*) FILTER (WHERE ") + self._path_frag(alias, path, t_ids, k) + Frag(f') AS "p{k}"'))
        step = len(paths) + len(same_rel) + 1
        if paths or same_rel:   # the filter or any path in the same role (never added to anything)
            sel.append(Frag("COUNT(*) FILTER (WHERE ") + union(t_ids, own, step) + Frag(') AS "ap"'))
        if copy:   # the base records the copied lookup's source holds the person on
            sel.append(Frag("COUNT(*) FILTER (WHERE ") + source(t_ids) + Frag(') AS "cs"'))
        for j, p in enumerate(split, 1):
            mine = Frag(f"{alias}.{quote_ident(f.api)} = ANY(?)", (list(p["ids"]),))
            if paths or same_rel:
                sel.append(Frag("COUNT(*) FILTER (WHERE ") + union(list(p["ids"]), mine, step * (j + 1))
                           + Frag(f') AS "ap_d{j}"'))
            if copy:
                sel.append(Frag("COUNT(*) FILTER (WHERE ") + source(list(p["ids"])) + Frag(f') AS "cs_d{j}"'))
        if not sel or call.remaining() < 0.5:
            return None
        try:
            st = call.run(Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM {scope.from_sql()}") + where_sql(others),
                          max_rows=1, timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.3)))
        except RecordError:
            call.note(f"the paths through other records to this {f.api} value were not counted (time budget)")
            return None
        for path in paths:
            call.table(path["obj"])
            for o, _c in path["hops"][1:]:
                call.table(o)
        for x in same_rel:
            call.table(x["object"])
        if copy:
            call.table(copy[0])
        return dict(zip(st.columns, st.rows[0])) if st.rows else {}

    def _all_records(self, call: _Call, scope: Scope, own: Frag, f: FieldSpec,
                     cands: list[tuple[str, FieldSpec, list[str], str | None]]) -> dict[str, int]:
        """The filter's own count and the person's other lookups of the base object over ALL its records (no other
        condition): ``{field: records}`` for the counts above 0, the filter's own lookup first. One statement."""
        alias = scope.alias
        sel: list[Frag] = [Frag("COUNT(*) FILTER (WHERE ") + own + Frag(') AS "n0"')]
        for i, (_grp, g, ids, _via) in enumerate(cands, 1):
            sel.append(Frag(f'COUNT(*) FILTER (WHERE {alias}.{quote_ident(g.api)} = ANY(?)) AS "n{i}"', (list(ids),)))
        st = call.run(Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM {scope.from_sql()}"), max_rows=1,
                      timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.2)))
        row = dict(zip(st.columns, st.rows[0])) if st.rows else {}
        held = [(f.api, int(row.get("n0") or 0))]
        held += sorted(((g.api, int(row.get(f"n{i}") or 0)) for i, (_grp, g, _ids, _via) in enumerate(cands, 1)),
                       key=lambda x: (-x[1], x[0]))
        out: dict[str, int] = {}
        for fld, n in held:
            if n > 0 and fld not in out:
                out[fld] = n
        return out

    # ------------------------------------------------------------------ reference scans
    def _child_pairs(self, recs: list[tuple[str, str]], *, system: bool, exclude: set[str] | frozenset = frozenset(),
                     in_objects: set[str] | None = None, with_empty: bool = False) -> list[tuple[str, str, str]]:
        """(child object, lookup, record Id) for every lookup of a child object that points to these records."""
        cat = self.catalog
        out: list[tuple[str, str, str]] = []
        links = {(lk.src, lk.col) for lk in cat.links} | {(lk.dst, lk.dst_col) for lk in cat.links if lk.dst_col != "Id"}
        for obj, rid in recs:
            spec = cat.objects.get(obj)
            if spec is None:
                continue
            for c in spec.children:
                if (c.child, c.via) in links:
                    continue  # identity links tie the person's own records together; find_references lists them
                child = cat.objects.get(c.child)
                if child is None or not child.has_table or child.sensitive or c.child in exclude or \
                        c.child in PEOPLE_SCAN_SKIP:   # a mirror or by-product object (engine.catalog)
                    continue
                if in_objects is not None and c.child not in in_objects:
                    continue
                g = child.fields.get(c.via)
                if g is None or g.sensitivity == "blocked" or g.api.lower() in cat.blocked_columns or g.api == "RecordTypeId":
                    continue
                if not system and g.api in SYSTEM_LOOKUPS:
                    continue
                if g.filled <= 0 and not with_empty:
                    continue
                if obj not in g.ref_targets and not (g.api == "OwnerId" and obj in ("User", "Group")):
                    continue
                out.append((c.child, c.via, rid))
        return list(dict.fromkeys(out))

    def _scan(self, call: _Call | None, pairs: list[tuple[str, str, str]], timeout: float | None = None,
              deadline: float | None = None) -> dict[tuple[str, str, str], int]:
        """Counts per (child, lookup, record Id): one derived table per child object, cross-joined into statements of
        at most SCAN_MAX_CHARS characters; cached per question."""
        pairs = list(dict.fromkeys(pairs))
        todo = [p for p in pairs if p not in self._scan_counts]
        by_child: dict[str, list[tuple[str, str, str]]] = {}
        for p in todo:
            by_child.setdefault(p[0], []).append(p)
        tables: list[tuple[str, list[tuple[str, str, str]]]] = []
        for child, ps in by_child.items():
            for i in range(0, len(ps), 60):
                tables.append((child, ps[i:i + 60]))
        statements: list[list[tuple[str, list[tuple[str, str, str]]]]] = []
        cur: list[tuple[str, list[tuple[str, str, str]]]] = []
        size = 0
        for child, ps in tables:
            est = 80 + len(child) + sum(60 + len(v) for _c, v, _i in ps)
            if cur and size + est > SCAN_MAX_CHARS:
                statements.append(cur)
                cur, size = [], 0
            cur.append((child, ps))
            size += est
        if cur:
            statements.append(cur)
        for group in statements:
            if deadline is not None and time.monotonic() > deadline:
                raise RecordError("reference scan stopped (time budget)", "E10")
            sel: list[str] = []
            derived: list[Frag] = []
            keys: list[tuple[str, str, str]] = []
            n = 0
            for k, (child, ps) in enumerate(group, 1):
                items: list[Frag] = []
                for c, v, i in ps:
                    n += 1
                    items.append(Frag(f'COUNT(*) FILTER (WHERE r{k}.{quote_ident(v)} = ?) AS "n{n}"', (i,)))
                    sel.append(f'a{k}."n{n}"')
                    keys.append((c, v, i))
                derived.append(Frag("(SELECT ") + join_frags(", ", items)
                               + Frag(f" FROM main.{quote_ident(child)} AS r{k}) AS a{k}"))
            frag = Frag(f"SELECT {', '.join(sel)} FROM ") + join_frags(" CROSS JOIN ", derived)
            t_out = timeout
            if deadline is not None:
                t_out = max(0.1, min(t_out if t_out is not None else 10.0, deadline - time.monotonic()))
            st = self._exec(call, frag, max_rows=1, timeout=t_out)
            vals = st.rows[0] if st.rows else [0] * len(keys)
            for key, val in zip(keys, vals):
                self._scan_counts[key] = int(val or 0)
                self._scan_st[key] = st
            if call is not None:
                for child, _ps in group:
                    call.table(child)
        if call is not None:
            for p in pairs:
                if p not in todo:
                    call.reuse(self._scan_st.get(p))
                    call.table(p[0])
        return {p: self._scan_counts.get(p, 0) for p in pairs}


def _check_names(chk: NameCheck) -> list[str]:
    """The plain names of a name check plus the names of its picked 'Name (#k)' values (stored spelling)."""
    out = list(chk.names)
    for pick in chk.picked.values():
        nm = str(pick[2]) if len(pick) > 2 and pick[2] is not None else ""
        if nm and all(norm_name(nm) != norm_name(n) for n in out):
            out.append(nm)
    return out
