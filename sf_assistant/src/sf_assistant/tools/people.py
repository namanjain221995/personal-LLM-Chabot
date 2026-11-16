"""``about_people``: who a named person is, their profiles, identities and linked records.

Moved verbatim from ``okrec/tools.py``. With the spelling help on (``SFA_RESOLVER=on``) a misspelt asked name is read
through the term resolver (:meth:`PeopleMixin._resolve_asked`; the helpers are in :mod:`sf_assistant.tools.names`);
with it off every result is the one without it.

The Group B people fixes (docs/people.md, 2026-10-21) add, with the switch on or off: each record's top lookup in a
person's share of references (``more_references`` lists the rest in short), the person's other records one line each
(``linked``), whose record a reference counts (``who``) and the system work of a person (``system_work``), how many
different candidates a reference's rows hold (``distinct``), the same person through the parents of a reference's
rows (``via_parents``, ``any_path``), the other people in the same role on a link (``others``) and a link's status
split (``by_status``), the direction text of a reporting line's profile key and the source of a lookup copied from a
child object. They are details: the result-size budget (:meth:`PeopleMixin._people_budget`) cuts them first.

Evaluation fixes (2026-10-22): a profile keeps, per record, the first :data:`PROFILE_MAX_FILLED` filled values in the
rank order of ``Catalog.profile_fields`` (a field blank on the record never takes a place), and the values of the
fields past the first :data:`PROFILE_CORE_FIELDS` are the result-size budget's first cut (a larger profile never costs
what the result showed with the core fields alone), except a field filled on most records of the object while the
record has at most :data:`PROFILE_CORE_FIELDS` filled values (:func:`_core_value`: an employee's joining date stays
when a rarely filled field ranks above it); a record joins a person only through an identity link of the
person's records, and a record that the links of several listed people's records reach (two same-name employees with
one person account and its portal user) belongs to none of them (review of 2026-10-22: the data does not say whose
it is): it is listed once with ``shared_by`` instead of ``person``, its references and system references carry
``shared_by`` too and count once, its system work is on its matched record, and ``shared_records``, a note and, for
people of different asked names, a "shared record" link name it. Such a record counts for no person's ranking total
and links nobody in the link finder (:meth:`PeopleMixin._multi_held`; review of 2026-10-23: a link credited the shared
portal user's creations to whichever person the order of the names made its holder).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from sf_assistant.knowledge.store import split_api_name
from sf_assistant.engine.catalog import (PEOPLE_SCAN_SKIP, PERSON_OBJECTS, SYSTEM_LOOKUPS, FieldSpec, ObjectSpec,
                                         RecordError, norm_key, norm_name)
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.engine.query_builder import NUMBERED_RE, Frag, join_frags, sensitive_object_text
from sf_assistant.engine.values import id18, is_record_number, looks_like_id

from sf_assistant.tools import record_base as _base   # the limits MAX_PEOPLE, MAX_RESULT_CHARS: read (and patched) there
from sf_assistant.tools.names import _person_reading, _suggested_names   # the spelling help (SFA_RESOLVER=on)
from sf_assistant.tools.record_base import (_POSSESSIVE_RE, _ROLE_WORDS, _HINT_STOPWORDS, _PERSON_ORDER, _cell, _Call,
                                            _short, _disp, _and_more, _and, _role, _cut_text, _close_surname,
                                            name_key, _q_tokens, _without_words)


PEOPLE_LINKS_EXTRA_CHARS = 4_000   # about_people with two or more names: room for the links next to who each is

PEOPLE_LISTED = 6         # people listed per asked name (the rest: more_people)
PEOPLE_DETAILED = 8       # people with full detail (profile, references) per call
PEOPLE_CANDIDATES = 12    # people per asked name that are ranked (by kind, then business references)
PERSON_REFS = 8           # business references per person (two or more people detailed: 24 // people, 2 to 8)
PERSON_SYSREFS = 3        # system references (creator, last editor, owner) per person
PERSON_REFS_ONE = 40      # business references when one person is detailed (as find_references gave)
PERSON_SYSREFS_ONE = 10   # system references when one person is detailed
MORE_REFS_MAX = 8         # references left out (share or result size) listed per person (more_references)
SYSTEM_WORK_MAX = 4       # system references per person in its people item (system_work)
DISTINCT_SUBJECTS = 2     # distinct-count subjects per reference (references of 2 or more records)
DISTINCT_BUDGET_S = 0.8   # time budget of the distinct counts (and the parent paths) of the references shown
REF_PATHS_MAX = 3         # parent paths per reference (via_parents)
PROFILE_MAX_FILLED = 12   # profile values per record: the first filled ones in the order of Catalog.profile_fields
PROFILE_CORE_FIELDS = 10  # the first fields of that order: values of later fields are the budget's first cut,
#                           unless the field is filled on most of the object's records (_core_value)
PROFILE_CONTACT_MAX = 6   # contact values per record, after the profile, when the question asks for contact details
#                           (phase 1; Catalog.contact_fields order; part of the core, not counted in PROFILE_MAX_FILLED)

MAX_LINKS = 24            # links per call
LINK_OTHERS_MAX = 5       # names of the other people in the same role on a link (C4 others)
LINK_ROWS_MAX = 20_000    # rows per touch statement of the link finder
LINK_BYTES_MAX = 8_000_000   # bytes per statement of the link finder (about 20,000 rows of 15 Id columns)
HOP_IDS_MAX = 2_000       # parent Ids fetched per middle object (two hops)
PEOPLE_BUDGET_S = 2.0     # time budget of the link finder


class PeopleMixin:
    """The ``about_people`` tool of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    # ================================================================== about_people (RECORDS_SPEC §22.5, §22.6)
    def _person_of(self, o: str, detail: Any) -> str:
        """The kind of person a record is: employee, user, candidate, lead, other account; record for any other
        object."""
        if o == "Recruiter__c":
            return "employee"
        if o == "User":
            return "user"
        if o == "Lead":
            return "lead"
        if o == "Account":
            return "candidate" if self.catalog.is_person_account(detail) else "other account"
        return "record"

    def kind_of(self, o: str, detail: Any = None) -> str:
        """The kind of person a record is (employee, user, candidate, lead, other account; record for any other object);
        never raises."""
        try:
            return self._person_of(o, detail)
        except Exception:  # noqa: BLE001 - optional
            return ""

    def _order_key(self, o: str, detail: Any) -> int:
        """:data:`_PERSON_ORDER` rank of a record (Employee, User, candidate Account, Lead, other Account; then any
        other object)."""
        return _PERSON_ORDER.get((o, self.catalog.person_kind(o, detail)), 9)

    def _identity_cols(self) -> set[tuple[str, str]]:
        """(object, column) of every identity link: the columns that tie one person's records together."""
        cat = self.catalog
        return {(lk.src, lk.col) for lk in cat.links} | {(lk.dst, lk.dst_col) for lk in cat.links if lk.dst_col != "Id"}

    def _resolve_asked(self, call: _Call, v: str, spec: ObjectSpec | None, objs: list[str],
                       persons: list[str]) -> dict | None:
        """One asked name of about_people -> ``{"asked", "match", "recs": [(object, Id, name, detail)], "note"}``, or
        None when nothing is stored under it. Order: Id, 'Name (#k)', full name, first and last word of a longer stored
        name, first name (one word), one close stored full name with the same first word, a record name of another
        object (no ``object`` given).

        With the spelling help on (``catalog.resolver``, docs/resolver_integration.md step (5)): one word, after the
        exact first and last name, the stored name the resolver reads it as (``corrected``: doubled letters or an
        identical sound only) gives "closest stored name" with the same note; 2 or more words, the resolver's one
        clear stored spelling of these objects replaces the close-surname step (``_close_surname``), and
        :meth:`_close_people` and the G2 spelling query follow as before. A did-you-mean (``ambiguous``) is never
        used: the name is not found, and :meth:`_about_people` lists the resolver's names first (E15a)."""
        cat = self.catalog
        out = {"asked": v, "match": "", "recs": [], "note": None}
        if looks_like_id(v):
            v18 = id18(v)
            obj = spec.api if spec else cat.prefix_to_object.get(v18[:3])
            ospec = cat.objects.get(obj) if obj else None
            if ospec is not None and ospec.sensitive:
                raise RecordError(sensitive_object_text(ospec.api), "E1d")
            if not obj:
                return None
            got = self._by_ids(call, [obj], [v18])
            out.update(match="Id", recs=[(o, r[0], r[1], r[2]) for o, rows in got.items() for r in rows])
            return out if out["recs"] else None
        m = NUMBERED_RE.match(v)
        if m:
            # 'Name (#k)' numbers the records that share a name within one object (L12). Without `object`, the label
            # came from the object where the name is shared: prefer an object with several such records.
            nm, k = m["name"].strip(), int(m["k"])
            by_obj = [(o, self._g1(call, o, [norm_name(nm)]).get(norm_name(nm), [])) for o in objs]
            by_obj = [(o, rows) for o, rows in by_obj if rows]
            pick = next((x for x in by_obj if len(x[1]) >= 2 and 1 <= k <= len(x[1])), None) or \
                next((x for x in by_obj if 1 <= k <= len(x[1])), None)
            if pick is not None:
                r = pick[1][k - 1]
                out.update(match="numbered label", recs=[(pick[0], r[0], r[1], r[2])])
                return out
            if by_obj:
                listed = "; ".join(f"{cat.label_of(o)}: " + ", ".join(f"#{i} {r[2] or _disp(r[1])}"
                                                                       for i, r in enumerate(rows, 1))
                                   for o, rows in by_obj)
                raise RecordError(f"'{nm}' has no #{k} in any object that has the name ({listed}).", "E15a")
            return None
        try:
            full, fl, first = self._name_index()
        except Exception:  # noqa: BLE001 - the exact statements below still run
            full, fl, first = {}, {}, {}
        toks = _q_tokens(v)
        key = " ".join(toks)
        ok = set(objs)

        def mine(recs: Any) -> list[tuple]:
            return [r for r in recs or [] if r[0] in ok]

        recs = mine(full.get(key))
        if not recs:
            nv = norm_name(_POSSESSIVE_RE.sub(r"\1", v))
            recs = [(o, r[0], r[1], r[2]) for o in objs for r in self._g1(call, o, [nv]).get(nv, [])]
        fl_recs = [r for r in mine(fl.get(key)) if r not in recs] if len(toks) == 2 else []
        if recs:   # a stored 'First Last' never hides a 'First Middle Last' person
            out.update(match="full name", recs=recs + fl_recs, fl_ids={r[1] for r in fl_recs})
            return out
        if fl_recs:
            out.update(match="first and last name", recs=fl_recs)
            return out
        speller = getattr(cat, "resolver", None)
        if len(toks) == 1 and toks[0] not in _HINT_STOPWORDS:
            w = toks[0]
            recs = mine(first.get(w)) or (mine(first.get(w[:-1])) if w.endswith("s") and len(w) > 3 else [])
            if recs:
                out.update(match="first name", recs=recs)
                return out
            recs = mine(self._names_last.get(w)) if len(w) >= 3 else []
            if recs:
                out.update(match="last name", recs=recs)
                return out
            got = _person_reading(self, speller, v, ok) if speller is not None else None
            if got is not None:   # spelling help: doubled letters or an identical sound only
                shown = list(dict.fromkeys(_disp(r[2]) for r in got[1]))
                out.update(match="closest stored name", recs=got[1],
                           note=f"no record is named '{v}'; the closest stored name"
                                + (f" is '{shown[0]}'" if len(shown) == 1
                                   else "s are " + ", ".join(f"'{x}'" for x in shown)))
                return out
        if len(toks) >= 2:
            if speller is None:
                cands = mine(first.get(toks[0]))
                close = [r for r in cands if _close_surname(toks[1], name_key(r[2]).split()[1:])]
            else:   # spelling help: the resolver's one clear stored spelling (of these objects) or nothing
                got = _person_reading(self, speller, v, ok)
                close = got[1] if got is not None else []
            names = list(dict.fromkeys(name_key(r[2]) for r in close))
            if len(names) != 1 and (speller is None or not names):
                # the same name written another way (order, initials, extra or fewer words)
                close = self._close_people(toks, ok)
                names = list(dict.fromkeys(name_key(r[2]) for r in close))
            if not names:   # a close spelling of the whole name with the same first word
                rows = [r for o in objs for r in self._g2(call, o, norm_name(v)) if r[1] == 3]
                spell = list(dict.fromkeys(name_key(r[0]) for r in rows if name_key(r[0]).split()[:1] == toks[:1]))
                if len(spell) == 1:
                    names = spell
                    close = mine(full.get(spell[0]))
            if names and close:
                shown = list(dict.fromkeys(_disp(r[2]) for r in close))
                out.update(match="closest stored name", recs=close,
                           note=f"no record is named '{v}'; the closest stored name"
                                + (f" is '{shown[0]}'" if len(shown) == 1 else "s are " + ", ".join(f"'{x}'" for x in shown)))
                return out
        if spec is None:
            if is_record_number(v):
                mm = re.match(r"^([A-Za-z]{1,6}-?)\d", v)
                more = [a for a in cat.number_prefixes.get(norm_key(mm.group(1)) if mm else "", [])]
            else:
                more = [s.api for s in cat.queryable() if self._queryable(s.api) and s.api not in persons and
                        s.api not in PEOPLE_SCAN_SKIP and s.api not in ("RecordType", "Group") and s.rows > 0
                        and not s.name_auto]
            nv = norm_name(v)
            for o in more:
                rows = self._g1(call, o, [nv]).get(nv, [])
                if rows:
                    out["recs"] += [(o, r[0], r[1], r[2]) for r in rows]
            if out["recs"]:
                out["match"] = "record name"
                return out
        return None

    def _about_people(self, call: _Call, names: list, object: str | None = None, in_objects: list | None = None) -> dict:
        cat = self.catalog
        asked: list[str] = []
        for n in names or []:
            v = " ".join(str(n if n is not None else "").split()).strip("'\"` ")
            if v and all(name_key(v) != name_key(a) for a in asked):
                asked.append(v)
        if not asked or any(len(v) < 2 for v in asked):
            raise RecordError("about_people needs 'names' with at least 2 characters each.", "E14")
        if len(asked) > _base.MAX_PEOPLE:
            raise RecordError(f"about_people takes at most {_base.MAX_PEOPLE} names; call it again for the rest", "E16a")
        spec = None
        if object:
            spec = cat.resolve_object(object)
            if spec.sensitive:
                raise RecordError(sensitive_object_text(spec.api), "E1d")
        ins: set[str] | None = None
        if in_objects:
            ins = set()
            for o in in_objects:
                so = cat.resolve_object(o)
                if so.sensitive:   # never scanned: an empty result would read as "no references"
                    raise RecordError(sensitive_object_text(so.api), "E1d")
                ins.add(so.api)
        call.applied.update({"names": asked, "object": spec.api if spec else "person objects",
                             "in_objects": sorted(ins) if ins else None})
        persons = [o for o in PERSON_OBJECTS if self._queryable(o)]
        objs = [spec.api] if spec else persons
        labels = [cat.label_of(o) for o in objs]
        who = (", ".join(labels[:-1]) + " or " + labels[-1]) if len(labels) > 1 else "".join(labels)
        found: list[dict] = []
        missing: list[str] = []
        for v in asked:
            res = self._resolve_asked(call, v, spec, objs, persons)
            if res is None:
                missing.append(v)
            else:
                found.append(res)
        not_found: list[dict] = []
        texts: list[str] = []
        speller = getattr(cat, "resolver", None)
        for v in missing:
            rows = [r for o in objs for r in self._g2(call, o, norm_name(_POSSESSIVE_RE.sub(r"\1", v)))]
            if speller is not None:   # spelling help: the resolver's names first among the close spellings
                dym = _suggested_names(self, speller, v, set(objs))
                rows = [(n, 3) for n in dym] + [r for r in rows if r[0] not in dym]
            close = list(dict.fromkeys(r[0] for r in rows))
            # "<first> <unknown word>" (a title, an honorific or an unknown surname): the people with that first name
            w, wrecs, how = self._name_word(_q_tokens(v), set(objs)) if len(_q_tokens(v)) >= 2 else ("", [], "")
            same_word = list(dict.fromkeys(_disp(r[2]) for r in sorted(
                wrecs, key=lambda r: (self._order_key(r[0], r[3]), str(r[1])))))
            close = list(dict.fromkeys(close + same_word))
            not_found.append({"asked": v, "close": close[:8]})
            call.not_found.append({"name": str(v), "target": who, "object": spec.api if spec else None, "field": None,
                                   "match": "exact", "close": close[:13]})
            text = f"no {who} record is named '{v}'" + (f" in {spec.api}" if spec else "") + "."
            if self._names_partial and not spec:
                text += " (The person-name index stopped early: a first or last name alone can be missing from it.)"
            parts = self._close_parts(rows, v)
            text += parts if "No name contains" not in parts else ""
            if same_word:
                text += f" Stored people with the {how} '{w}': {_and_more(same_word, 8)}."
            texts.append(text + f" Call search_records(name='{v}') for partial names.")
        if not found:
            raise RecordError(" ".join(texts), "E15a")

        # ---- people: the matched records grouped by identity, per asked name
        people: list[dict] = []        # every candidate person (internal); refs are given to the listed ones
        by_key: dict[str, dict] = {}   # group key (anchor Id) -> person
        # record Id -> the one person it belongs to. A record joins a person only through an identity link of the
        # person's records (never through its name); a record that the links of two people's records reach (two
        # same-name employees with one person account and its portal user) is held by the first of them, in the
        # order of ``matched``, and the other keeps it in "shared": it is nobody's own (:meth:`_multi_held`, shared_by)
        held: dict[str, dict] = {}
        more: dict[str, dict] = {}
        for side, res in enumerate(found):
            v = res["asked"]
            matched = sorted(res["recs"], key=lambda r: (self._order_key(r[0], r[3]), str(r[1])))
            capped, rest = matched[:45], matched[45:]
            pcap = [(r[0], r[1]) for r in capped if r[0] in PERSON_OBJECTS]
            try:
                ident = self._identities(call, pcap) if pcap else {}
            except RecordError:
                ident = {}
            mine: list[dict] = []
            for o, rid, nm, det in capped:
                anchor, members = ident.get(rid, (None, []))
                if anchor is not None and not anchor[2] and anchor[1] == rid:
                    anchor = (o, rid, nm, det)
                key = anchor[1] if anchor is not None else rid
                # a record (or its anchor) that an earlier person holds already is that person's
                p = held.get(rid) or held.get(key) or by_key.get(key)
                if p is None:
                    recs = [anchor if anchor is not None else (o, rid, nm, det)]
                    links = {}
                    shared: list[tuple] = []
                    for x in members:
                        if any(x[2] == r[1] for r in recs):
                            continue
                        if x[2] in held:   # linked to an earlier person's records too: listed once, under that person
                            shared.append(((x[1], x[2], x[3], x[4]), x[0].label, held[x[2]]["key"]))
                            continue
                        recs.append((x[1], x[2], x[3], x[4]))
                        links[x[2]] = x[0].label
                    if all(rid != r[1] for r in recs):
                        recs.append((o, rid, nm, det))
                    main = min(recs, key=lambda r: (self._order_key(r[0], r[3]), recs.index(r)))
                    p = {"key": key, "side": side, "asked": v, "note": res["note"],
                         "match": "first and last name" if rid in (res.get("fl_ids") or ()) else res["match"],
                         "main": main, "anchor": recs[0], "recs": recs, "links": links, "matched": {rid},
                         "kind": self._person_of(main[0], main[3]), "shared": shared}
                    by_key[key] = p
                    people.append(p)
                    for r in recs:
                        held.setdefault(r[1], p)
                else:
                    p["matched"].add(rid)
                    if all(rid != r[1] for r in p["recs"]):
                        p["recs"].append((o, rid, nm, det))
                        held.setdefault(rid, p)
                    if p["side"] != side and v not in p.setdefault("also_asked", []):
                        p["also_asked"].append(v)   # two asked names, one person
                if p not in mine:
                    mine.append(p)
            mine.sort(key=lambda p: (self._order_key(p["main"][0], p["main"][3]), str(p["main"][1])))
            cand, extra = mine[:PEOPLE_CANDIDATES], mine[PEOPLE_CANDIDATES:]
            for p in extra:
                p["out"] = True
            res["people"] = cand
            more[v] = {"extra": extra, "rest": rest}
        cands = [p for p in people if not p.get("out")]
        # a record that the identity links of several candidates' records reach is nobody's own (review of 2026-10-23):
        # it counts for no candidate's ranking total, and the link finder maps it to nobody
        nobody = self._multi_held(cands)
        # ---- business references of every candidate (ranking; one batched scan)
        bpairs: dict[str, list[tuple[str, str, str]]] = {}
        allp: list[tuple[str, str, str]] = []
        for p in cands:
            bpairs[p["key"]] = self._child_pairs([(r[0], r[1]) for r in p["recs"]], system=False, in_objects=ins)
            allp += bpairs[p["key"]]
        counts: dict[tuple[str, str, str], int] = {}
        partial_refs = False
        try:
            counts = self._scan(call, allp, deadline=time.monotonic() + max(0.3, min(1.5, call.remaining() - 1.0)))
        except RecordError:
            partial_refs = True
            counts = {k: self._scan_counts[k] for k in allp if k in self._scan_counts}
        biz: dict[str, int] = {}   # record Id -> its business references (every lookup added: a ranking total only)
        for (_c, _via, rid), n in counts.items():
            biz[str(rid)] = biz.get(str(rid), 0) + n
        for p in cands:
            p["refs"] = sum(biz.get(str(r[1]), 0) for r in p["recs"] if str(r[1]) not in nobody)
        # ---- links between the people of different asked names
        links: list[dict] = []
        link_info: dict[str, Any] = {}
        sides = {p["side"] for p in cands}
        if len(sides) >= 2:
            call.max_chars = _base.MAX_RESULT_CHARS + PEOPLE_LINKS_EXTRA_CHARS
            links, link_info = self._people_links(call, cands, ins)
        linked = {k for x in links for k in x["between_keys"]}
        # ---- listing: per asked name, people linked to another name first, then by kind and references
        listed: list[dict] = []
        same_name: dict[str, list[dict]] = {}
        more_out: dict[str, dict] = {}
        per_name = min(PEOPLE_LISTED, max(3, 12 // max(1, len(found))))   # 4 names x 6 people do not fit a result
        for res in found:
            v = res["asked"]
            ppl = sorted(res["people"], key=lambda p: (0 if p["key"] in linked else 1,
                                                       self._order_key(p["main"][0], p["main"][3]), -p.get("refs", 0),
                                                       _disp(p["main"][2])))
            shown = [p for p in ppl if p not in listed][:per_name]
            listed += shown
            left = [p for p in ppl if p not in shown and p not in listed] + more[v]["extra"]
            rest = more[v]["rest"]
            if left or rest:
                kinds: dict[str, int] = {}
                for p in left:
                    kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
                for r in rest:
                    k = self._person_of(r[0], r[3])
                    kinds[k] = kinds.get(k, 0) + 1
                nm = list(dict.fromkeys([_disp(p["main"][2]) for p in left] + [_disp(r[2]) for r in rest]))
                more_out[v] = {"count": len(left) + len(rest), "by_kind": kinds, "names": nm[:10]}
            byname: dict[str, list[dict]] = {}
            for p in shown:
                byname.setdefault(name_key(p["main"][2]), []).append(p)
            groups = [g for g in byname.values() if len(g) >= 2]
            if groups:
                same_name[v] = [p for g in groups for p in g]
        # a record that the links of several people's records reach and whose holder is not listed goes to the first
        # listed person whose records link to it (one person per record; the others keep it in "shared")
        listed_keys = {p["key"] for p in listed}
        for p in listed:
            for s in list(p["shared"]):
                if s[2] not in listed_keys and not any(s[0][1] == r[1] for q in listed for r in q["recs"]):
                    p["recs"].append(s[0])
                    p["links"][s[0][1]] = s[1]
                    p["shared"].remove(s)
        # refs: P for the listed people, R for their records
        recs_out: list[dict] = []
        ref_of: dict[str, str] = {}
        for i, p in enumerate(listed, 1):
            p["ref"] = f"P{i}"
        by_ref = {p["ref"]: p for p in listed}
        # a record that the identity links of several listed people's records reach (two same-name employees whose
        # Recruiter_Contact__c point to one person account, with its portal user) belongs to none of them: the data
        # does not say whose it is (review of 2026-10-22). It is listed once with "shared_by" (the people in listing
        # order), and its references, system references and system work count once, as shared. A person's own main
        # or anchor record is never shared.
        holder_of: dict[str, dict] = {}
        for p in listed:
            for r in p["recs"]:
                holder_of.setdefault(r[1], p)
        shared_by_id: dict[str, list[str]] = {}
        shared_how: dict[str, list[tuple[dict, str]]] = {}   # record Id -> (another person, its identity link label)
        for p in listed:
            for rec, lab, _key in p["shared"]:
                h = holder_of.get(rec[1])
                if h is None or h is p or rec[1] in (h["main"][1], h["anchor"][1]):
                    continue
                lst = shared_by_id.setdefault(rec[1], [h["ref"]])
                if p["ref"] not in lst:
                    lst.append(p["ref"])
                    shared_how.setdefault(rec[1], []).append((p, lab))
        for lst in shared_by_id.values():
            lst.sort(key=lambda x: int(x[1:]))
        # the totals of the listed people's own records (no_business_references, business_reference_total): a shared
        # record counts for none of them, a record moved here from "shared" (its holder not listed) for its new holder
        for p in listed:
            p["refs"] = sum(biz.get(str(r[1]), 0) for r in p["recs"] if r[1] not in shared_by_id)
        for p in listed:
            ordered = [p["main"]] + [r for r in p["recs"] if r[1] != p["main"][1]]
            for r in ordered:
                if r[1] in ref_of:
                    continue
                ref_of[r[1]] = f"R{len(recs_out) + 1}"
                recs_out.append({"ref": ref_of[r[1]], "person": p["ref"], "object": r[0], "id": r[1], "name": r[2],
                                 "detail": r[3], "rec": r})
                if r[1] in shared_by_id:
                    recs_out[-1]["shared_by"] = list(shared_by_id[r[1]])
        for p in listed:
            mref = ref_of[p["main"][1]]
            aref = ref_of[p["anchor"][1]]
            for e in recs_out:
                if e["person"] != p["ref"]:
                    continue
                rid = e["id"]
                if rid == p["main"][1]:
                    e["how"] = ("Id" if p["match"] == "Id" else "name") if rid in p["matched"] else \
                        f"{p['kind']} record of the person"
                elif rid in p["links"]:
                    e["how"] = f"{p['links'][rid]} of {aref}"
                elif rid == p["anchor"][1]:
                    lab = next(iter(p["links"].values()), "identity link")
                    e["how"] = f"linked to {mref} through {lab}"
                else:
                    e["how"] = "Id" if p["match"] == "Id" else "name"
                if e.get("shared_by"):   # how the other people's records reach it too
                    e["how"] = "; ".join([e["how"]] + [f"{lab} of {ref_of[q['anchor'][1]]}"
                                                       for q, lab in shared_how.get(rid, [])])
        # labels: 'Name (#k)' when the name is shared in its object (L12); one statement per object
        norms_by_obj: dict[str, list[str]] = {}
        for e in recs_out:
            norms_by_obj.setdefault(e["object"], []).append(norm_name(e["name"]))
        same: dict[tuple[str, str], list[str]] = {}
        for o, norms in norms_by_obj.items():
            got = self._g1(call, o, norms)
            for nv, rows in got.items():
                same[(o, nv)] = [x[0] for x in rows]
        for e in recs_out:
            ids = same.get((e["object"], norm_name(e["name"])), [])
            e["label"] = f"{_disp(e['name'])} (#{ids.index(e['id']) + 1})" if len(ids) > 1 and e["id"] in ids \
                else _disp(e["name"])
        label_of = {e["id"]: e["label"] for e in recs_out}
        # ---- full detail: the first PEOPLE_DETAILED listed people, round-robin over the names
        detailed: list[dict] = []
        queues = [[p for p in listed if p["side"] == s] for s in range(len(found))]
        while len(detailed) < PEOPLE_DETAILED and any(queues):
            for q in queues:
                if q and len(detailed) < PEOPLE_DETAILED:
                    detailed.append(q.pop(0))
        det_refs = {p["ref"] for p in detailed}
        det_ids = {r[1] for p in detailed for r in p["recs"]}
        # a shared record of a detailed person whose holder is not detailed: its profile and system lookups too
        shared_extra = [e["rec"] for e in recs_out if e.get("shared_by") and e["id"] not in det_ids
                        and any(x in det_refs for x in e["shared_by"])]
        prof = self._profiles(call, [r for p in detailed for r in p["recs"]] + shared_extra)
        spairs = [k for p in detailed for k in self._child_pairs([(r[0], r[1]) for r in p["recs"]], system=True,
                                                                 in_objects=ins) if k[1] in SYSTEM_LOOKUPS]
        if shared_extra:
            spairs += [k for k in self._child_pairs([(r[0], r[1]) for r in shared_extra], system=True, in_objects=ins)
                       if k[1] in SYSTEM_LOOKUPS]
        try:
            counts.update(self._scan(call, spairs, deadline=time.monotonic() + max(0.2, min(1.0, call.remaining() - 0.8))))
        except RecordError:
            partial_refs = True
        refs: list[dict] = []
        sysrefs: list[dict] = []
        # each detailed person gets a share of the result: the result stays under MAX_RESULT_CHARS for everyone
        # one person: every lookup with records, as find_references gave (_people_budget notes what does not fit)
        n_refs = PERSON_REFS_ONE if len(detailed) == 1 else max(2, min(PERSON_REFS, 24 // max(1, len(detailed))))
        n_sys = PERSON_SYSREFS_ONE if len(detailed) == 1 else max(1, min(PERSON_SYSREFS, 8 // max(1, len(detailed))))
        # the one person whose own name is the asked name of 2 or more words gets the share of a single person, when
        # the other people of that name only share part of it (first and last name, a linked record's name)
        primary = self._primary_people(found, detailed)
        rec_by_id = {e["id"]: e for e in recs_out}
        same_keys = {p["key"] for ppl in same_name.values() for p in ppl}   # people sharing a name with another

        def who(rid: str) -> str:
            """'<record label> (<object label>, <detail>)': which record (and person) a reference counts."""
            e = rec_by_id[rid]
            det = str(e["detail"] or "")
            return f"{label_of[rid]} ({cat.label_of(e['object'])}" + (f", {det}" if det else "") + ")"

        for p in detailed:
            ids = {r[1] for r in p["recs"] if r[1] not in shared_by_id}   # a shared record is nobody's own
            mine_b, mine_s = [], []
            for (c, via, rid), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                if n <= 0 or rid not in ids or rid not in ref_of:
                    continue
                g = cat.objects[c].fields[via]
                e: dict[str, Any] = {"person": p["ref"], "record": ref_of[rid]}
                if p["key"] in same_keys:   # people with the same name: say whose record each reference counts (C2)
                    e["who"] = who(rid)
                e.update({"object": c, "field": via, "role": _role(g)})
                if via in SYSTEM_LOOKUPS:
                    e["records"] = n
                    mine_s.append(e)
                else:
                    if g.from_target:
                        e["meaning"] = g.from_target
                    e["records"] = n
                    mine_b.append(e)
            share_b = max(n_refs, PERSON_REFS_ONE // len(primary)) if p["key"] in primary else n_refs
            share_s = max(n_sys, PERSON_SYSREFS_ONE // len(primary)) if p["key"] in primary else n_sys
            # each record's top lookup first, then by count; listed in the order of the counts
            pick = _tops_first(mine_b, share_b)
            refs += [e for i, e in enumerate(mine_b) if i in pick]
            p["more_refs"] = [_ref_text(e) for i, e in enumerate(mine_b) if i not in pick][:MORE_REFS_MAX]
            sysrefs += mine_s[:share_s]
            p["detail_refs"] = len(mine_b)
            p["system_work"] = [f"{e['object']}.{e['field']} {e['records']}" for e in mine_s[:SYSTEM_WORK_MAX]]
        # the shared records of the detailed people: their references and system references once, with "shared_by"
        # (the share of one person per group of people), their system work on their matched record
        groups: dict[tuple[str, ...], list[str]] = {}
        for e in recs_out:
            if e.get("shared_by") and any(x in det_refs for x in e["shared_by"]):
                groups.setdefault(tuple(e["shared_by"]), []).append(e["id"])
        for prefs, rids in groups.items():
            ids = set(rids)
            same = any(by_ref[x]["key"] in same_keys for x in prefs)
            mine_b, mine_s = [], []
            for (c, via, rid), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                if n <= 0 or rid not in ids or rid not in ref_of:
                    continue
                g = cat.objects[c].fields[via]
                e = {"shared_by": list(prefs), "record": ref_of[rid]}
                if same:
                    e["who"] = who(rid)
                e.update({"object": c, "field": via, "role": _role(g)})
                if via in SYSTEM_LOOKUPS:
                    e["records"] = n
                    mine_s.append(e)
                else:
                    if g.from_target:
                        e["meaning"] = g.from_target
                    e["records"] = n
                    mine_b.append(e)
            pick = _tops_first(mine_b, n_refs)
            refs += [e for i, e in enumerate(mine_b) if i in pick]
            sysrefs += mine_s[:n_sys]
            for rid in rids:
                work = [f"{x['object']}.{x['field']} {x['records']}" for x in mine_s if x["record"] == ref_of[rid]]
                if work:
                    rec_by_id[rid]["system_work"] = work[:SYSTEM_WORK_MAX]
        try:   # how many different candidates (leads, accounts) the counted rows of a reference hold (best effort)
            self._distinct_subjects(call, refs, rec_by_id)
        except RecordError:
            pass
        try:   # the same person through the parents of the counted rows, in the same role (best effort)
            self._ref_paths(call, refs, rec_by_id)
        except RecordError:
            pass
        notes = ["a record can hold the same person in several lookups, and different objects count different things: "
                 "never add these counts"]
        if partial_refs:
            notes.append("the reference scan stopped (time budget): counts are missing for some lookups")
        # Role or purpose breakdown (F4).
        done = 0
        for e in refs:
            if done >= 3:
                break
            cs = cat.objects[e["object"]]
            pick = [g for g in cs.fields.values() if g.kind == "picklist" and g.has_data and not g.sensitivity
                    and any(w in _ROLE_WORDS for w in split_api_name(g.api).split())]
            if not pick:
                continue
            pf = max(pick, key=lambda g: (g.filled, g.api))
            rid = next(r["id"] for r in recs_out if r["ref"] == e["record"])
            st = call.run(Frag(f"SELECT COALESCE(CAST(r.{quote_ident(pf.api)} AS VARCHAR), '(blank)') AS v, COUNT(*) AS n "
                               f"FROM main.{quote_ident(cs.api)} AS r WHERE r.{quote_ident(e['field'])} = ? GROUP BY 1 "
                               "ORDER BY 2 DESC, 1 LIMIT 50", (rid,)), max_rows=50)
            by = {str(val): int(n) for val, n in st.rows[:6]}
            rest_n = sum(int(n) for _val, n in st.rows[6:])
            if rest_n:
                by["(other)"] = rest_n
            e["by_field"], e["by"] = pf.api, by
            notes.append(f"{cs.api}.{e['field']} rows mix roles or purposes ({pf.api}): filter or group by them; to count "
                         "a person's interviews or sessions, count the object the question names, not these rows")
            done += 1
        # Notes F2, F5.
        shared = self._shared_records(recs_out)   # record ref -> the people whose records link to it
        for p in detailed:
            r = p["main"]
            ulinks = [lk for lk in cat.identity_links(r[0]) if lk.dst == "User"]
            # a user of the person's records that is listed under another person (shared) is a linked user too
            if ulinks and not any(x[0] == "User" for x in p["recs"] + [s[0] for s in p["shared"]]):
                notes.append(f"{ref_of[r[1]]} ({cat.label_of(r[0])}) has no linked user ({_and([lk.label for lk in ulinks])} "
                             "find no user), so User lookups such as CreatedById and OwnerId cannot point to it")
            if p["kind"] == "user" and not any(x[0] == PERSON_OBJECTS[0] for x in p["recs"]):
                notes.append(f"{p['ref']} is a Salesforce user with no linked {cat.label_of(PERSON_OBJECTS[0])} record "
                             f"({PERSON_OBJECTS[0]}): say so; the records they own, create or edit (system references) "
                             "show what they work on")
            logins = [ref_of[x[1]] for x in p["recs"] if x[0] == "User" and x[1] in ref_of and x[1] not in shared_by_id]
            if len(logins) >= 2 and r[0] != "User":
                notes.append(f"{' and '.join(logins)} are logins of the same {cat.label_of(r[0])} {ref_of[r[1]]}")
        notes += self._shared_notes(listed, shared, recs_out, ref_of)
        for v, ppl in same_name.items():
            mine_refs = {p["ref"] for p in ppl}
            through: dict[tuple[str, ...], list[str]] = {}   # people of this name -> the records they share
            for rref, prefs in shared.items():
                inside = tuple(x for x in prefs if x in mine_refs)
                if len(inside) >= 2:
                    through.setdefault(inside, []).append(rref)
            how = (" not linked except through the records they share (" + "; ".join(
                f"{_and(list(k))}: {', '.join(rr)}" for k, rr in through.items()) + ")") if through else " not linked"
            notes.append(f"'{v}': " + ", ".join(f"{p['ref']} ({p['kind']})" for p in ppl)
                         + f" have the same name,{how}: the data does not record whether they are one human")
        for res in found:
            if res["note"]:
                notes.append(res["note"])
        for p in listed:
            if p.get("also_asked"):
                notes.append(f"'{p['asked']}' and " + " and ".join(f"'{w}'" for w in p["also_asked"])
                             + f" are the same person: {p['ref']} ({label_of[p['main'][1]]}), through their linked records")
        for n_ in notes:
            call.note(n_)
        nw: list[str] = []
        robjs = {e["object"] for e in recs_out}
        for s in cat.objects.values():
            for api, nf in s.not_synced.items():
                t = cat._not_synced_target(nf)
                if t in robjs:
                    role = str(nf.get("role") or "").replace("_", " ") or _cut_text(nf.get("concept") or "", 40)
                    nw.append(f"{s.api}.{api} ({role})")
        pairs_all = [k for p in detailed for k in self._child_pairs([(r[0], r[1]) for r in p["recs"]], system=True,
                                                                    in_objects=ins)]
        lookups = {(c, via) for c, via, _i in pairs_all}
        with_recs = {(c, via) for (c, via, i), n in counts.items() if n > 0 and (c, via) in lookups and i in ref_of}
        call.object = listed[0]["main"][0] if listed else None
        for e in recs_out:
            call.table(e["object"])
        # the people's own objects first: the Source line shows their sync times (the link finder reads ~25 objects)
        own_objs = list(dict.fromkeys(e["object"] for e in recs_out))
        call.tables = own_objs + [t for t in call.tables if t not in own_objs]
        # ---- the result
        dset = {p["key"] for p in detailed}
        people_out = []
        for p in listed:
            item: dict[str, Any] = {"ref": p["ref"], "asked": p["asked"], "label": label_of[p["main"][1]],
                                    "kind": p["kind"], "match": p["match"],
                                    "records": [ref_of[r[1]] for r in p["recs"] if r[1] in ref_of
                                                and r[1] not in shared_by_id]}
            if p["main"][3]:
                item["detail"] = p["main"][3]
            lines = self._linked_lines(p, recs_out, prof, call)
            if lines:   # the person's other records, one line each (an employee's own account, logins, a lead)
                item["linked"] = lines
            if p["key"] in dset:
                pr = prof.get(p["main"][1]) or {}
                if pr:
                    item["profile"] = pr
                if not p.get("refs") and not partial_refs:   # a stopped scan never says "no references"
                    item["no_business_references"] = True
                if p.get("system_work"):   # the records this person owns, created or last changed (system lookups)
                    item["system_work"] = list(p["system_work"])
                if p.get("more_refs"):     # lookups with records left out of references (the per-person share)
                    item["more_references"] = list(p["more_refs"])
            else:   # no references listed: the sum over all business lookups ranks them (one record can count twice)
                item["business_reference_total"] = p.get("refs", 0)
            people_out.append(item)
        matched_out = []
        for e in recs_out:
            item = {"ref": e["ref"], **({"shared_by": list(e["shared_by"])} if e.get("shared_by") else
                                        {"person": e["person"]}),
                    "label": e["label"], "object": e["object"], "object_label": cat.label_of(e["object"])}
            if e["detail"]:
                item["detail"] = e["detail"]
            item["how"] = e["how"]
            owner = next(p for p in listed if p["ref"] == e["person"])
            seen = owner["key"] in dset or any(x in det_refs for x in e.get("shared_by") or [])
            if seen and e["id"] != owner["main"][1] and prof.get(e["id"]):
                item["profile"] = prof[e["id"]]
            if e.get("system_work"):   # a shared record: what it owns, created or last changed (nobody's own)
                item["system_work"] = list(e["system_work"])
            matched_out.append(item)
        out: dict[str, Any] = {"asked": asked, "people": people_out, "matched_records": matched_out,
                               "references": refs, "system_references": sysrefs}
        if same_name:
            out["same_name"] = {v: [p["ref"] for p in ppl] for v, ppl in same_name.items()}
        if shared:   # a record the links of several people's records reach: nobody's own, listed once (shared_by)
            out["shared_records"] = shared
        if more_out:
            out["more_people"] = more_out
        if not_found:
            out["not_found"] = not_found
        if len(found) >= 2:
            ref_by_key = {p["key"]: p["ref"] for p in listed}
            lk_out = []
            for x in links:
                a, b = x["between_keys"]
                if a not in ref_by_key or b not in ref_by_key:
                    continue
                ra, rb = ref_by_key[a], ref_by_key[b]
                lab = (lambda k: f"{ref_by_key[k]} ({label_of[by_key[k]['main'][1]]})")
                item = {"between": [ra, rb], "how": x["how"], "path": x["path"], "records": x["records"],
                        **({"examples": x["examples"]} if x.get("examples") else {}), "text": x["text"](lab)}
                if x.get("holder_key") in ref_by_key:   # an own-record link: the person whose record it is
                    item["holder"] = ref_by_key[x["holder_key"]]
                if x.get("by_status"):   # C4: the link's records per status value
                    item["by_status"] = x["by_status"]
                oth = x.get("others")
                if oth and oth["of_key"] in ref_by_key:   # C4: the other people in that role on one side's rows
                    item["others"] = {"field": oth["field"], "role": oth["role"], "of": ref_by_key[oth["of_key"]],
                                      "records": oth["records"], "names": oth["names"]}
                lk_out.append(item)
            # a shared record of people of different asked names links them (review of 2026-10-22): first
            shared_lk = []
            rec_by_ref = {e["ref"]: e for e in recs_out}
            for rref, prefs in shared.items():
                e = rec_by_ref[rref]
                for i, a in enumerate(prefs):
                    for b in prefs[i + 1:]:
                        pa, pb = by_ref[a], by_ref[b]
                        if pa["side"] == pb["side"]:
                            continue
                        if pa["side"] > pb["side"]:
                            a, b, pa, pb = b, a, pb, pa
                        shared_lk.append({
                            "between": [a, b], "how": "shared record",
                            "path": f"{e['object']} (identity links of both)", "records": 1,
                            "text": f"{a} ({label_of[pa['main'][1]]}) and {b} ({label_of[pb['main'][1]]}) both link to "
                                    f"{cat.label_of(e['object'])} '{e['label']}' ({rref}) through the identity links "
                                    "of their records; the data does not say which of them uses it"})
            out["links"] = (shared_lk + lk_out)[:MAX_LINKS]
            done_pairs = {tuple(sorted(x["between"])) for x in out["links"]}
            pw = []
            for i, p in enumerate(listed):
                for q in listed[i + 1:]:
                    if p["side"] != q["side"] and p["key"] != q["key"] and tuple(sorted((p["ref"], q["ref"]))) not in done_pairs:
                        pw.append([p["ref"], q["ref"]])
            out["pairs_without_links"] = pw
            out["links_checked"] = link_info.get("checked", "")
            nc = list(link_info.get("not_checked") or [])
            if any(more_out.values()):
                nc.append("links of the people not listed (more_people)")
            if nc:
                out["not_checked"] = nc
            out["partial"] = bool(link_info.get("partial"))
        out["scope"] = "counts over all records of each object (no period, filter or status)"
        if partial_refs:
            out["references_partial"] = True
        out.update({"lookups_checked": len(lookups), "lookups_with_records": len(with_recs), "not_in_warehouse": nw[:5],
                    "not_scanned": "text fields that hold names (such as a typed interviewer name), objects without a "
                                   "knowledge file and sensitive objects are not scanned"})
        keep = {p["ref"] for p in listed if p["key"] in primary or p["key"] in linked}
        # the links whose other people (C4 others) are in a role the question asks about keep them longest
        q_rest = _without_words(self._question or "", [str(a) for a in asked])
        protect: set[int] = set()
        for x in out.get("links") or []:
            o_f = str((x.get("others") or {}).get("field") or "").split(".", 1)
            spec_o = cat.objects.get(o_f[0]) if len(o_f) == 2 else None
            g_o = spec_o.fields.get(o_f[1]) if spec_o is not None else None
            if g_o is not None and q_rest and cat.role_fits(q_rest, spec_o, g_o, "employee"):
                protect.add(id(x))
        reserve = len(json.dumps(call.applied, default=str)) + len(json.dumps(self._source(call), default=str)) + 300
        self._people_budget(call, out, len(detailed), reserve=reserve, keep=keep, protect=protect)
        keys_note = _keys_note(out)   # what the keys of the Group B fixes left in the result mean: only when it fits
        if keys_note and len(json.dumps(out, default=str)) + len(json.dumps(call.notes + [keys_note])) \
                + 60 * len(out.get("people") or []) + reserve <= call.max_chars:
            call.note(keys_note)
        # references left out (the per-person share or the result size): say how many, never silently
        shown_n: dict[str, int] = {}
        for e in out.get("references") or []:
            if e.get("person"):   # a shared record's references are nobody's share
                shown_n[e["person"]] = shown_n.get(e["person"], 0) + 1
        cut: list[str] = []
        for item in people_out:
            p = next((x for x in detailed if x.get("ref") == item["ref"]), None)
            total = int(p.get("detail_refs") or 0) if p is not None else 0
            if p is not None and shown_n.get(item["ref"], 0) < total:
                item["references_shown"] = f"{shown_n.get(item['ref'], 0)} of {total} lookups with records"
                cut.append(f"{item['ref']} {shown_n.get(item['ref'], 0)} of {total}")
        if cut:   # one note for all of them (the result size)
            call.note(f"lookups with records shown: {', '.join(cut)}; call about_people with that one name (or "
                      "in_objects) for the rest")
        call.summary = f"{len(with_recs)} lookups with records" + (f", {len(out.get('links') or [])} links"
                                                                   if "links" in out else "")
        first_scan = next((s for s in call.statements if s.sql.startswith("SELECT a1.")), None)
        if first_scan is not None:
            call.main(first_scan)
        return out

    def _primary_people(self, found: list[dict], detailed: list[dict]) -> set[str]:
        """Keys of the detailed people who are the one person of their asked name whose own (main) record has exactly
        that name, for an asked name of 2 or more words that other detailed people only share part of."""
        out: set[str] = set()
        for side, res in enumerate(found):
            v = str(res.get("asked") or "")
            if len(_q_tokens(v)) < 2:
                continue
            mine = [p for p in detailed if p["side"] == side]
            exact = [p for p in mine if p["match"] != "first and last name" and name_key(p["main"][2]) == name_key(v)]
            if len(exact) == 1 and len(mine) >= 2:
                out.add(exact[0]["key"])
        return out

    @staticmethod
    def _multi_held(people: list[dict]) -> set[str]:
        """Ids of the records that the identity links of several of ``people``'s records reach: held by the first of
        them (its ``recs``), kept by the others in ``shared``, never the holder's main or anchor record (the rule of
        ``shared_by``). Such a record is nobody's own (review of 2026-10-23): no ranking total counts it and the link
        finder maps it to nobody. No statement."""
        holder: dict[str, dict] = {}
        for p in people:
            for r in p["recs"]:
                holder.setdefault(str(r[1]), p)
        out: set[str] = set()
        for p in people:
            for rec, _lab, _key in p.get("shared") or []:
                h = holder.get(str(rec[1]))
                if h is not None and h is not p and rec[1] not in (h["main"][1], h["anchor"][1]):
                    out.add(str(rec[1]))
        return out

    @staticmethod
    def _shared_records(recs_out: list[dict]) -> dict[str, list[str]]:
        """The records that the identity links of several listed people's records reach (two same-name employees whose
        records point to one person account and its portal user): ``{record ref: [person refs]}`` in listing order
        (their ``shared_by``). Such a record belongs to none of them (the data does not say whose it is), so its
        references and system work count once, as shared. No statement."""
        return {e["ref"]: list(e["shared_by"]) for e in recs_out if e.get("shared_by")}

    def _shared_notes(self, listed: list[dict], shared: dict[str, list[str]], recs_out: list[dict],
                      ref_of: dict[str, str]) -> list[str]:
        """One note per group of people that share records (:meth:`_shared_records`): which records, through which
        identity links of each person's records, that the data does not say whose they are, and that their counts
        (``shared_by``) count once, as shared, never as one person's. No statement."""
        cat = self.catalog
        rec_by_ref = {e["ref"]: e for e in recs_out}
        by_ref = {p.get("ref"): p for p in listed}
        groups: dict[tuple[str, ...], list[str]] = {}
        for rref, prefs in shared.items():
            groups.setdefault(tuple(prefs), []).append(rref)
        out: list[str] = []
        for prefs, rrefs in groups.items():
            ids = [rec_by_ref[x]["id"] for x in rrefs]   # in record ref order
            via = []
            for pr in prefs:
                q = by_ref[pr]
                labels = [q["links"][i] for i in ids if i in q["links"]]           # the person it was reached for
                labels += [lab for rec, lab, _k in q["shared"] if rec[1] in ids]   # the others
                labels = list(dict.fromkeys(labels))
                if labels:
                    via.append(f"{ref_of[q['anchor'][1]]}: {_and(labels)}")
            many = len(rrefs) > 1
            what = _and([f"{x} ({cat.label_of(rec_by_ref[x]['object'])})" for x in rrefs])
            own = (", ".join(f"{x}'s" for x in prefs[:-1]) + f" or {prefs[-1]}'s own") if len(prefs) > 1 \
                else f"{prefs[0]}'s own"
            out.append(f"{what} {'are' if many else 'is'} linked to the records of {'both ' if len(prefs) == 2 else ''}"
                       f"{_and(list(prefs))} ({'; '.join(via)}): the data does not say which of them "
                       f"{'uses them' if many else 'uses it'}, so {'their' if many else 'its'} references, system "
                       f"references and system work (shared_by) count once, as shared, never as {own}")
        return out

    def _linked_lines(self, p: dict, recs_out: list[dict], prof: dict, call: _Call | None = None) -> list[str]:
        """The person's records other than the main one, one line each (C1): "<Object label> '<label>' (<detail>;
        <how it is linked>)"; a user's line says whether it is active when its profile holds IsActive. No
        statement. ``call.linked_plain`` gets each line's short form without how it is linked (the result-size
        budget's first step for these lines; matched_records says how). A shared record (``shared_by``) is nobody's
        own: no line."""
        cat = self.catalog
        out: list[str] = []
        for e in recs_out:
            if e["person"] != p.get("ref") or e["id"] == p["main"][1] or e.get("shared_by"):
                continue
            parts = [str(e["detail"])] if e.get("detail") else []
            active = (prof.get(e["id"]) or {}).get("IsActive")
            if e["object"] == "User" and isinstance(active, bool):
                parts.append("active" if active else "inactive")
            inner = ", ".join(parts)
            line = (f"{cat.label_of(e['object'])} '{e['label']}' (" + (f"{inner}; " if inner else "")
                    + f"{e.get('how') or 'linked record'})")
            if call is not None:   # the line without how it is linked (matched_records has it): the budget's step
                call.linked_plain[line] = f"{cat.label_of(e['object'])} '{e['label']}'" + (f" ({inner})" if inner else "")
            out.append(line)
        return out

    def _subject_objects(self) -> list[str]:
        """The person objects whose records are candidates (a Lead, a Person Account): the subjects a distinct count
        counts."""
        return [o for o in PERSON_OBJECTS if self.catalog.person_kind(o, "Person Account") == "candidate"]

    def _subject_lookups(self, c: str, via: str) -> list[FieldSpec]:
        """Business lookups of object ``c`` to a candidate object (:meth:`_subject_objects`) with data that hold the
        record's subject (:meth:`_subject_role`: a candidate or a lead, never a referrer or a client account), other
        than ``via``, the identity links and a lookup of ``c`` to ``c`` itself, most filled first (at most
        DISTINCT_SUBJECTS)."""
        cat = self.catalog
        spec = cat.objects[c]
        subj = set(self._subject_objects())
        ident = self._identity_cols()
        out = [g for g in spec.fields.values() if g.kind == "reference" and g.api != via and g.filled > 0
               and g.api not in SYSTEM_LOOKUPS and g.api != "RecordTypeId" and g.sensitivity != "blocked"
               and g.api.lower() not in cat.blocked_columns and (c, g.api) not in ident and cat.target_of(g) in subj
               and cat.target_of(g) != c and self._subject_role(spec, g)]
        out.sort(key=lambda g: (-g.filled, g.api))
        return out[:DISTINCT_SUBJECTS]

    def _distinct_subjects(self, call: _Call, refs: list[dict], rec_by_id: dict[str, dict]) -> None:
        """C3: for each business reference of 2 or more records whose lookup points to no candidate object (an
        employee's or a user's rows) and whose object is no candidate object (rows that are candidates already count
        different candidates), how many different candidates those rows hold through the object's subject lookups:
        ``distinct: {"<subject lookup>": n}`` (an owner of 55 trainings of 36 candidates). One statement per object,
        bound Ids, alias-qualified columns, within DISTINCT_BUDGET_S."""
        cat = self.catalog
        id_of = {e["ref"]: e["id"] for e in rec_by_id.values()}
        subj = set(self._subject_objects())
        todo: dict[str, list[tuple[dict, str, FieldSpec]]] = {}
        for e in refs:
            c, via = e["object"], e["field"]
            if int(e.get("records") or 0) < 2 or via in SYSTEM_LOOKUPS or e.get("record") not in id_of:
                continue
            g = cat.objects[c].fields.get(via)
            if g is None or c in subj or cat.target_of(g) in subj or not self._queryable(c):
                continue
            for s in self._subject_lookups(c, via):
                todo.setdefault(c, []).append((e, id_of[e["record"]], s))
        if not todo:
            return
        deadline = time.monotonic() + max(0.1, min(DISTINCT_BUDGET_S, call.remaining() - 1.0))
        for c, items in todo.items():
            if time.monotonic() > deadline:
                break
            sel = [Frag(f'COUNT(DISTINCT r.{quote_ident(s.api)}) FILTER (WHERE r.{quote_ident(e["field"])} = ?) '
                        f'AS "d{k}"', (rid,)) for k, (e, rid, s) in enumerate(items, 1)]
            st = call.run(Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM main.{quote_ident(c)} AS r"),
                          max_rows=1, timeout=max(0.1, deadline - time.monotonic()))
            call.table(c)
            row = st.rows[0] if st.rows else []
            for (e, _rid, s), n in zip(items, row):
                if n:
                    e.setdefault("distinct", {})[s.api] = int(n)

    def _ref_paths(self, call: _Call, refs: list[dict], rec_by_id: dict[str, dict]) -> None:
        """Item 8 for about_people: a business reference whose object reaches the person's record through its parents
        in the same role (:meth:`_parent_paths`: a candidate's sessions through the candidate's trainings) gets
        ``via_parents`` ``{"<P> -> <K>": n}`` (the lookups of each hop; the REF_PATHS_MAX paths with the most records)
        and ``any_path``: the records in the reference's
        lookup, any of these paths, or a child row in the same role (:meth:`_related_children`). One statement per
        object, within DISTINCT_BUDGET_S."""
        cat = self.catalog
        id_of = {e["ref"]: e["id"] for e in rec_by_id.values()}
        todo: dict[str, list[tuple[dict, str, list[dict], list[dict]]]] = {}
        for e in refs:
            c, via = e["object"], e["field"]
            if via in SYSTEM_LOOKUPS or e.get("record") not in id_of or not self._queryable(c):
                continue
            spec = cat.objects[c]
            g = spec.fields.get(via)
            if g is None:
                continue
            paths = self._parent_paths(spec, g)
            if not paths:
                continue
            rid = id_of[e["record"]]
            t = cat.target_of(g)
            rel = [x for x in self._related_children(spec, g, {t: [rid]}) if x["same_role"]] if t else []
            todo.setdefault(c, []).append((e, rid, paths, rel))
        if not todo:
            return
        deadline = time.monotonic() + max(0.1, min(DISTINCT_BUDGET_S, call.remaining() - 1.0))
        for c, items in todo.items():
            if time.monotonic() > deadline:
                break
            sel: list[Frag] = []
            n = 0
            for j, (e, rid, paths, rel) in enumerate(items, 1):
                union = [Frag(f'r.{quote_ident(e["field"])} = ?', (rid,))]
                for path in paths:
                    n += 1
                    pf = self._path_frag("r", path, [rid], n)
                    sel.append(Frag("COUNT(*) FILTER (WHERE ") + pf + Frag(f') AS "p{n}"'))
                    union.append(self._path_frag("r", path, [rid], n))
                for k, x in enumerate(rel, 1):
                    union.append(Frag(f'r."Id" IN (SELECT ru{j}_{k}.{quote_ident(x["via"])} FROM '
                                      f'main.{quote_ident(x["object"])} AS ru{j}_{k} WHERE '
                                      f'ru{j}_{k}.{quote_ident(x["field"])} = ?)', (rid,)))
                sel.append(Frag("COUNT(*) FILTER (WHERE ") + join_frags(" OR ", union) + Frag(f') AS "a{j}"'))
            st = call.run(Frag("SELECT ") + join_frags(", ", sel) + Frag(f" FROM main.{quote_ident(c)} AS r"),
                          max_rows=1, timeout=max(0.1, deadline - time.monotonic()))
            call.table(c)
            for _e, _rid, paths, rel in items:
                for path in paths:
                    for o in [path["obj"]] + [h[0] for h in path["hops"][1:]]:
                        call.table(o)
                for x in rel:
                    call.table(x["object"])
            row = dict(zip(st.columns, st.rows[0])) if st.rows else {}
            n = 0
            for j, (e, _rid, paths, _rel) in enumerate(items, 1):
                got: list[tuple[int, str]] = []
                for path in paths:
                    n += 1
                    if int(row.get(f"p{n}") or 0) > 0:
                        got.append((int(row[f"p{n}"]), path["short"]))
                if got:   # the paths with the most records (the lookups of each hop: "<P> -> <K>")
                    e["via_parents"] = {txt: k for k, txt in sorted(got, key=lambda x: (-x[0], x[1]))[:REF_PATHS_MAX]}
                    e["any_path"] = int(row.get(f"a{j}") or 0)

    @staticmethod
    def _people_budget(call: _Call, out: dict, detailed: int, reserve: int = 900, keep: set[str] | None = None,
                       protect: set[int] | None = None) -> None:
        """Keep an about_people result within the result size with something for everyone. First, without a note, the
        profile values of the fields past the core (``call.profile_details``, :data:`PROFILE_CORE_FIELDS`), the last
        value of the longest profile first (the profiles are then what the core fields hold). Then the details the
        Group B fixes add (docs/people.md), without a note: the lists of references left out (``more_references``),
        the details of links past 3 (``others``, ``by_status``), the parent paths of references (``via_parents``),
        the system work lines; with links (two or more names) also the references' ``distinct`` and ``who``, the
        people's ``linked`` lines, the direction text and copy flag of profile keys with a copy's source values, and
        the details of the other links (the first link keeps the other people in its role: they answer "who else"),
        so the links and references never yield to them (``protect``: ``id()`` of the links whose ``others`` are in a
        role the question asks about, else the first link with ``others``: their ``others`` are the last detail to
        go); without links the ``distinct`` counts, the profile key details, how each ``linked`` line is linked
        (``call.linked_plain``: matched_records says how) and the ``linked`` lines of the people not in ``keep`` (they
        repeat matched_records; a first name of many people needs the room for references). Then, in this order (each step noted): the last reference of the
        person with the most among the people not in ``keep`` (when ``keep`` is set: the asked person of a full name,
        the people that links join; down to one each), system references, the profiles of the people not in ``keep``
        (down to 2 items), links past 8, a reference of the person with the most (down to the top lookup of each
        record), links past 3, the profiles of linked records, the names of the people not listed (their count
        stays), references down to none, profiles down to 3 items; last the references' ``who`` and the ``linked``
        lines (no links). A person's reference is the smallest that is not the top lookup of one of
        the person's records (:func:`_ref_to_drop`); ``references_shown`` says how many of a person's lookups are
        shown. ``reserve``: the size of what comes after (applied, source, query id, sync notes, the references-shown
        notes)."""
        keep = keep or set()
        dropped = {"links": 0, "references": 0, "system references": 0, "linked record profiles": 0,
                   "names of people not listed": 0, "profile items": 0, "left-out references": 0,
                   "system work lines": 0, "link details": 0, "reference details": 0, "linked lines": 0,
                   "profile wording": 0, "profile details": 0}
        people = {p.get("ref"): p for p in out.get("people") or [] if isinstance(p, dict)}

        # the details of the Group B fixes and the profile values past the core: left out without a note
        # (references_shown keeps the reference counts)
        extra = ("left-out references", "system work lines", "link details", "reference details", "linked lines",
                 "profile wording", "profile details")

        def over() -> bool:   # applied, notes (with the ones below) and source come after
            return len(json.dumps(out, default=str)) + len(json.dumps(call.notes)) + 160 * sum(
                1 for k, n in dropped.items() if n and k not in extra) + 60 * len(out.get("people") or []) \
                + reserve > call.max_chars

        def cut(refs: list[dict], i: int) -> None:
            refs.pop(i)
            dropped["references"] += 1

        def link_detail(lst: list[dict], start: int = 0, keep_first: bool = False) -> bool:
            """A link's ``others`` / ``by_status`` from the last link down to ``lst[start]``; ``keep_first``: the
            links of ``protect`` (else the first link of the whole list with ``others``) keep them (they answer "who
            else")."""
            kept = {i for i, x in enumerate(lst) if id(x) in (protect or set()) and "others" in x} if keep_first else set()
            if keep_first and not kept:
                kept = {next((i for i, x in enumerate(lst) if "others" in x), -1)}
            for i in range(len(lst) - 1, start - 1, -1):
                for k in ("others", "by_status"):
                    if k in lst[i] and not (i in kept and k == "others"):
                        lst[i].pop(k)
                        dropped["link details"] += 1
                        return True
            return False

        def ref_detail(refs: list[dict]) -> bool:
            for e in reversed(refs):
                if "via_parents" in e:
                    e.pop("via_parents")
                    e.pop("any_path", None)
                    dropped["reference details"] += 1
                    return True
            return False

        def distinct_detail(refs: list[dict]) -> bool:
            for e in reversed(refs):
                if "distinct" in e:
                    e.pop("distinct")
                    dropped["reference details"] += 1
                    return True
            return False

        def ref_extra(refs: list[dict]) -> bool:
            """The distinct counts, then the ``who`` of the references, from the last; then the people's linked lines."""
            for key in ("distinct", "who"):
                for e in reversed(refs):
                    if key in e:
                        e.pop(key)
                        dropped["reference details"] += 1
                        return True
            return linked_drop()

        def linked_short() -> bool:
            """The ``linked`` lines of one person (the last first) without how each is linked."""
            plain = call.linked_plain
            for p in reversed(list(people.values())):
                lines = p.get("linked") or []
                if any(x in plain and plain[x] != x for x in lines):
                    p["linked"] = [plain.get(x, x) for x in lines]
                    dropped["linked lines"] += 1
                    return True
            return False

        def linked_drop(skip: set[str] | frozenset = frozenset()) -> bool:
            """The ``linked`` lines of one person (the last first) not in ``skip``."""
            for p in reversed(list(people.values())):
                if p.get("linked") and p.get("ref") not in skip:
                    p.pop("linked")
                    dropped["linked lines"] += 1
                    return True
            return False

        def longest(key: str, floor: int = 0) -> bool:
            lst = [p for p in people.values() if len(p.get(key) or []) > floor]
            if not lst:
                return False
            p = max(lst, key=lambda x: (len(x[key]), str(x.get("ref"))))
            p[key].pop()
            if not p[key]:
                p.pop(key)
            dropped["left-out references" if key == "more_references" else "system work lines"] += 1
            return True

        def wording() -> bool:
            """Profile keys back to "<Api> (<label>)" (the direction text of a reporting line, the copy flag) and the
            values of a copy's source left out (``call.profile_plain``: key -> plain key, or None to leave it out)."""
            plain = call.profile_plain
            for m in list(out.get("people") or []) + list(out.get("matched_records") or []):
                prof = m.get("profile") if isinstance(m, dict) else None
                if isinstance(prof, dict) and any(k in plain for k in prof):
                    m["profile"] = {plain.get(k, k): v for k, v in prof.items() if k not in plain or plain[k] is not None}
                    dropped["profile wording"] += 1
                    return True
            return False

        def profile_detail() -> bool:
            """The last value of a field past the profile core (``call.profile_details``) of the longest profile
            shown; of two as long, the later one in the result."""
            shown = [m.get("profile") for m in list(out.get("people") or []) + list(out.get("matched_records") or [])
                     if isinstance(m, dict) and isinstance(m.get("profile"), dict)]
            best: tuple[int, int, dict, str] | None = None
            for d, keys in call.profile_details:
                at = next((i for i, s in enumerate(shown) if s is d), None)
                left = [k for k in keys if k in d] if at is not None else []
                if left and (best is None or (len(d), at) > best[:2]):
                    best = (len(d), at, d, left[-1])
            if best is None:
                return False
            best[2].pop(best[3])
            dropped["profile details"] += 1
            return True

        has_links = bool(out.get("links"))
        while over():
            links, refs, sysrefs = out.get("links") or [], out.get("references") or [], out.get("system_references") or []
            profiled = [m for m in out.get("matched_records") or [] if "profile" in m]
            per_other: dict[str, int] = {}
            for e in refs:
                if _ref_owner(e) not in keep:
                    per_other[_ref_owner(e)] = per_other.get(_ref_owner(e), 0) + 1
            i_any = _ref_to_drop(refs, {_ref_owner(e) for e in refs})
            # the profile values past the core, then the details of the Group B fixes yield first (docs/people.md);
            # with links (two or more names) all of them but the first link's other people, before anything the
            # result had without them
            if profile_detail() or longest("more_references") or link_detail(links, 3, keep_first=True) \
                    or ref_detail(refs) or longest("system_work"):
                pass
            elif has_links and (ref_extra(refs + sysrefs) or wording() or link_detail(links, keep_first=True)
                                or link_detail(links)):
                pass   # the protected links' others go last of the details, never at the cost of what OLD showed
            elif not has_links and (distinct_detail(refs) or wording() or linked_short() or linked_drop(skip=keep)):
                pass   # the linked lines repeat matched_records: they never cost what OLD showed (keep: theirs last)
            elif keep and any(n > 1 for n in per_other.values()):   # the other people: their last reference first
                top = max(per_other, key=lambda k: (per_other[k], k))
                cut(refs, max(i for i, e in enumerate(refs) if _ref_owner(e) == top))
            elif sysrefs:
                sysrefs.pop()
                dropped["system references"] += 1
            elif keep and any(p.get("ref") not in keep and len(p.get("profile") or {}) > 2 for p in out.get("people") or []):
                for p in out["people"]:
                    if p.get("ref") not in keep and len(p.get("profile") or {}) > 2:
                        p["profile"] = dict(list(p["profile"].items())[:2])
                        dropped["profile items"] += 1
            elif len(links) > 8:
                links.pop()
                dropped["links"] += 1
            elif len(refs) > max(1, detailed) and i_any is not None:
                cut(refs, i_any)
            elif len(links) > 3:
                links.pop()
                dropped["links"] += 1
            elif profiled:
                profiled[-1].pop("profile")
                dropped["linked record profiles"] += 1
            elif any(isinstance(m, dict) and m.get("names") for m in (out.get("more_people") or {}).values()):
                for m in out["more_people"].values():
                    if isinstance(m, dict) and m.pop("names", None):
                        dropped["names of people not listed"] += 1
            elif refs:
                cut(refs, len(refs) - 1)
            elif any(len(p.get("profile") or {}) > 3 for p in out.get("people") or []):
                p = max(out["people"], key=lambda x: len(x.get("profile") or {}))
                p["profile"] = dict(list(p["profile"].items())[:3])
                dropped["profile items"] += 1
            elif ref_extra(refs + sysrefs):
                pass
            else:
                break
        for key, n in dropped.items():
            if not n or key in extra:
                continue
            if key == "names of people not listed":
                call.note("the names of the people not listed are left out (result size); more_people keeps their count")
            elif key == "profile items":
                call.note(f"{n} profiles are cut to fewer items (result size); call about_people with one "
                          "name for a full profile")
            else:
                call.note(f"{n} more {key} not shown (result size); call about_people with fewer names or in_objects "
                          "for them")


    def _profiles(self, call: _Call, recs: list[tuple]) -> dict[str, dict[str, Any]]:
        """Profile values of these records (RECORDS_SPEC §22.4): ``{Id: {"<Api> (<label>)": value}}``, per record the
        first :data:`PROFILE_MAX_FILLED` values that are not blank, in the order of :meth:`Catalog.profile_fields` (a
        blank field never takes a place: a lookup filled on few records never pushes out a date filled on most); a
        lookup shows the linked record's name. One statement per object. The values of the fields past the first
        :data:`PROFILE_CORE_FIELDS` go to ``call.profile_details``: the result-size budget leaves them out first, so a
        larger profile never costs a reference, a link or a detail the result had with the core fields alone; a later
        field filled on most of the object's records stays in the core while the record has at most
        :data:`PROFILE_CORE_FIELDS` filled values (:func:`_core_value`: the joining date of an employee whose
        Designation__c, filled on few employees, is blank).

        The key of a lookup from a person object to the same object adds how the record reads it
        (``FieldSpec.from_source``: "Reporting to; senior recruiter this employee reports to"), and a lookup that copies a lookup of a child object
        (``FieldSpec.child_copy``) says so ("partial copy of Marketing__c.Recruiter__c") and is followed by the
        source's values on the record's child rows (``"Marketing__c.Recruiter__c (via Candidate__c)": "<names>"``,
        also when the copy is blank): one more statement per such lookup.

        Phase 1 (2026-10-26): when the question asks for contact details (``begin_question``,
        :func:`sf_assistant.engine.contact.asks_for_contact`) the filled values of the first :data:`PROFILE_CONTACT_MAX`
        contact fields of :meth:`Catalog.contact_fields` follow (in the same statement; part of the core); otherwise a
        profile never holds a contact field."""
        cat = self.catalog
        by_obj: dict[str, list[str]] = {}
        for r in recs:
            if r[0] in cat.objects:
                by_obj.setdefault(r[0], []).append(r[1])
        out: dict[str, dict[str, Any]] = {}
        plain_of = call.profile_plain   # direction-worded key -> the plain key (_people_budget)
        for o, ids in by_obj.items():
            spec = cat.objects[o]
            if not self._queryable(o):
                continue
            fields = cat.profile_fields(spec)
            contacts = cat.contact_fields(spec)[:PROFILE_CONTACT_MAX] if self._contact_ok else []
            if not fields and not contacts:
                continue
            sel = ['x."Id" AS id']
            joins: list[str] = []
            keys: list[tuple[str, FieldSpec]] = []
            for k, f in enumerate(fields):
                t = cat.target_of(f) if f.kind == "reference" else None
                if t is not None and self._queryable(t):
                    joins.append(f' LEFT JOIN main.{quote_ident(t)} AS p{k} ON p{k}."Id" = x.{quote_ident(f.api)}')
                    sel.append(f'trim(p{k}."Name") AS "v{k}"')
                else:
                    sel.append(f'x.{quote_ident(f.api)} AS "v{k}"')
                keys.append((f"v{k}", f))
            for j, f in enumerate(contacts):
                sel.append(f'x.{quote_ident(f.api)} AS "c{j}"')
            uids = list(dict.fromkeys(ids))
            st = call.run(Frag(f"SELECT {', '.join(sel)} FROM main.{quote_ident(o)} AS x{''.join(joins)} "
                               "WHERE x.\"Id\" = ANY(?)", (uids,)), max_rows=200)
            call.table(o)
            sources = {f.api: self._copy_sources(call, f, uids) for _c, f in keys if f.child_copy}
            for row in st.rows:
                vals: dict[str, Any] = {}
                late: list[str] = []   # keys of the values past the core: the result-size budget's first cut
                filled = 0             # the record's filled field values so far (a copy's source values not counted)
                for k, ((_c, f), val) in enumerate(zip(keys, row[1:])):
                    if len(vals) >= PROFILE_MAX_FILLED:
                        break
                    src = sources.get(f.api)
                    if val is not None and not (isinstance(val, str) and not val.strip()):
                        key = self._profile_key(spec, f, plain_of)
                        vals[key] = _cell(_disp(val) if isinstance(val, str) else val, f.kind)
                        filled += 1
                        if not _core_value(k, filled, f, spec):
                            late.append(key)
                    if src is not None and src[1].get(str(row[0])) and len(vals) < PROFILE_MAX_FILLED:
                        vals[src[0]] = src[1][str(row[0])]
                        plain_of[src[0]] = None   # a detail: the result-size budget leaves it out first
                        if not _core_value(k, filled, f, spec):
                            late.append(src[0])
                for f, val in zip(contacts, row[1 + len(keys):]):   # asked contact details: core, not capped above
                    if val is not None and not (isinstance(val, str) and not val.strip()):
                        vals[self._profile_key(spec, f, plain_of)] = _cell(_disp(val) if isinstance(val, str) else val,
                                                                          f.kind)
                out[str(row[0])] = vals
                if late:
                    call.profile_details.append((vals, late))
        return out

    def _profile_key(self, spec: ObjectSpec, f: FieldSpec, plain_of: dict[str, str] | None = None) -> str:
        """``"<Api> (<label>)"``; a lookup from a person object to the same object (a reporting line, a manager: the
        value is read from this record, and the other direction is a reference) adds ``"; <from_source>"``, and a
        child copy ``"; partial copy of <C>.<L>"``, inside the brackets (C9: the key starts with the API name)."""
        cat = self.catalog
        parts = [f.label] if f.label and f.label != f.api else []
        if f.kind == "reference" and f.from_source and spec.api in PERSON_OBJECTS and cat.target_of(f) == spec.api:
            plain = f"{f.api} ({'; '.join(parts)})" if parts else f.api
            parts.append(f.from_source)
            if plain_of is not None:   # the key without it, for _people_budget
                plain_of[f"{f.api} ({'; '.join(parts)})"] = plain
        if f.child_copy:
            base = f"{f.api} ({'; '.join(parts)})" if parts else f.api
            parts.append(("partial copy of " if f.copy_source_filled > f.filled else "copy of ")
                         + f"{f.child_copy[0]}.{f.child_copy[2]}")
            if plain_of is not None:
                plain_of[f"{f.api} ({'; '.join(parts)})"] = (plain_of.get(base) or base) if parts[:-1] else f.api
        if not parts:
            return f.api
        return f"{f.api} ({'; '.join(parts)})"

    def _copy_sources(self, call: _Call, f: FieldSpec, ids: list[str]) -> tuple[str, dict[str, str]] | None:
        """The values of the source of a child copy (``FieldSpec.child_copy`` (C, K, L)) on these records' C rows:
        ``("<C>.<L> (via <K>)", {Id: "<names>"})`` (at most 3 names, then "and N more"), or None when it cannot be
        read. One statement."""
        cat = self.catalog
        c, k, lk = f.child_copy
        t = cat.target_of(cat.objects[c].fields[lk]) if c in cat.objects and lk in cat.objects[c].fields else None
        if t is None or not self._queryable(t) or not self._queryable(c):
            return None
        try:
            # ordered names and groups: the statement's rows (evidence, the RECORDS block) are the same every run
            st = call.run(Frag(f'SELECT cc.{quote_ident(k)} AS id, array_agg(DISTINCT trim(pc."Name") ORDER BY '
                               f'trim(pc."Name")) AS names FROM main.{quote_ident(c)} AS cc JOIN main.{quote_ident(t)} '
                               f'AS pc ON pc."Id" = cc.{quote_ident(lk)} WHERE cc.{quote_ident(k)} = ANY(?) GROUP BY 1 '
                               'ORDER BY 1', (list(ids),)),
                          max_rows=500, timeout=max(0.1, min(_base.SCAN_BUDGET_S, call.remaining() - 0.3)))
        except RecordError:
            return None
        call.table(c)
        got: dict[str, str] = {}
        for rid, names in st.rows:
            ns = sorted({_disp(n) for n in (names or []) if n})
            if ns:
                got[str(rid)] = ", ".join(ns[:3]) + (f", and {len(ns) - 3} more" if len(ns) > 3 else "")
        return f"{c}.{lk} (via {k})", got

    # ------------------------------------------------------------------ link finder (RECORDS_SPEC §22.6)
    def _people_links(self, call: _Call, people: list[dict], ins: set[str] | None) -> tuple[list[dict], dict]:
        """Every stored record that links people of different asked names: ``(links, info)``. ``people``: the
        candidate people (``key``, ``side`` = index of the asked name, ``recs`` = all their records, identity members
        included; a record several of them reach, :meth:`_multi_held`, is nobody's and links nobody). Each link:
        ``{"between_keys", "how", "path", "records", "examples", "text"}``; ``text(label)``
        writes the sentence (``label(key)`` names a person). ``info``: ``checked`` (what was checked, with numbers),
        ``not_checked`` and ``partial``.

        Step 1 (touch): one statement per object that holds a business person lookup with data (and per object of the
        people's own records): its rows whose business person lookups (or Id) hold one of the people, with the status
        field, the person lookups, the system lookups (read, never filtered on) and the parent lookups. Step 2 (hop):
        the parents of those rows that step 1 did not fetch, one statement per middle object. Step 3, in Python: same
        record, own record, parent, two hops, shared parent. Every Id list is a bound parameter and every column is
        alias-qualified. The whole step stops after PEOPLE_BUDGET_S (``partial``)."""
        cat = self.catalog
        deadline = time.monotonic() + PEOPLE_BUDGET_S
        idmap: dict[str, str] = {}
        side: dict[str, int] = {}
        own: dict[str, list[str]] = {}
        # a record that the identity links of several of these people's records reach is nobody's own (review of
        # 2026-10-23): it maps to no person, so no link gives its rows (a shared portal user's creations) to the person
        # the order of the names made its holder (about_people's "shared record" link is its only link), and it is
        # never a parent, a shared parent or another person in a role either
        nobody = self._multi_held(people)
        for p in people:
            side[p["key"]] = p["side"]
            for r in p["recs"]:
                if str(r[1]) in nobody:
                    continue
                idmap[str(r[1])] = p["key"]
                own.setdefault(r[0], []).append(str(r[1]))
        ident = self._identity_cols()
        plk_c: dict[str, list[tuple[str, str]]] = {}
        par_c: dict[str, list[tuple[str, str]]] = {}
        stat_c: dict[str, str | None] = {}

        def plk(o: str) -> list[tuple[str, str]]:
            """Business person lookups with data (not identity links): [(column, person object)]."""
            if o not in plk_c:
                spec = cat.objects[o]
                plk_c[o] = [(g.api, t) for t, lst in cat.person_lookups(spec, system=False).items() for g in lst
                            if g.filled > 0 and (o, g.api) not in ident and g.api.lower() not in cat.blocked_columns]
            return plk_c[o]

        def parents(o: str) -> list[tuple[str, str]]:
            """Lookups with data to a queryable, non-sensitive object that is no person object: [(column, object)]."""
            if o not in par_c:
                out = []
                for f in cat.objects[o].fields.values():
                    if f.kind != "reference" or f.filled <= 0 or f.api in SYSTEM_LOOKUPS or f.api == "RecordTypeId" or \
                            f.sensitivity == "blocked" or (o, f.api) in ident:
                        continue
                    t = cat.target_of(f)
                    if t and t not in PERSON_OBJECTS and self._queryable(t):
                        out.append((f.api, t))
                par_c[o] = out
            return par_c[o]

        def hop(o: str) -> list[tuple[str, str]]:
            """Parent lookups of step 3: :func:`parents` and the business person lookups (a row's candidate, recruiter
            or manager): a row whose person lookup holds a third person whose own row holds B links A and B through
            that person. Only rows step 1 or 2 fetched are read: no extra statement."""
            return parents(o) + plk(o)

        def status_col(o: str) -> str | None:
            if o not in stat_c:
                f = self._status_field(cat.objects[o])
                stat_c[o] = f.api if f is not None else None
            return stat_c[o]

        holds = {s.api for s in cat.objects.values() if s.has_table and not s.sensitive and self._queryable(s.api)
                 and s.api not in PEOPLE_SCAN_SKIP and plk(s.api)}
        rows: dict[str, list[dict]] = {}
        row_of: dict[str, tuple[str, dict]] = {}
        not_checked: list[str] = []
        partial = False
        n_lookups = n_objects = 0
        read_objs: list[str] = []   # the objects step 1 read (named in links_checked: what "not linked" covers)
        for spec in sorted(cat.objects.values(), key=lambda s: s.api):
            o = spec.api
            if not spec.has_table or spec.sensitive or not self._queryable(o) or o in PEOPLE_SCAN_SKIP:
                continue
            if ins is not None and o not in ins and o not in own:
                continue
            pl = plk(o)
            conds = [(c, own[t]) for c, t in pl if own.get(t)]
            if own.get(o):
                conds.append(("Id", own[o]))
            if not conds:
                continue
            if time.monotonic() > deadline:
                not_checked.append(o)
                partial = True
                continue
            n_objects += 1
            n_lookups += len(pl)
            read_objs.append(o)
            sysl = [s for s in SYSTEM_LOOKUPS if s in spec.fields and spec.fields[s].filled > 0
                    and spec.fields[s].sensitivity != "blocked"]
            st_c = status_col(o)
            cols = ["Id"] + (["Name"] if "Name" in spec.fields and spec.fields["Name"].sensitivity != "blocked" else []) \
                + ([st_c] if st_c else []) + [c for c, _t in pl] + sysl + [c for c, _t in parents(o)]
            cols = list(dict.fromkeys(cols))
            frag = (Frag("SELECT " + ", ".join(f"x.{quote_ident(c)}" for c in cols) + f" FROM main.{quote_ident(o)} AS x "
                         "WHERE ")
                    + join_frags(" OR ", [Frag(f"x.{quote_ident(c)} = ANY(?)", (list(dict.fromkeys(ids)),))
                                          for c, ids in conds]))
            try:
                st = call.run(frag, max_rows=LINK_ROWS_MAX, timeout=max(0.1, deadline - time.monotonic()),
                              max_bytes=LINK_BYTES_MAX)
            except RecordError:
                not_checked.append(o)
                partial = True
                continue
            call.table(o)
            if st.truncated:
                not_checked.append(f"{o} (row or size limit: only {len(st.rows)} of the records that hold these "
                                   "people were read)")
                partial = True
            got = [dict(zip(cols, r)) for r in st.rows]
            rows[o] = got
            for r in got:
                row_of[str(r["Id"])] = (o, r)
        # Step 2: the parents of touched rows that step 1 did not fetch (two hops; names of shared parents).
        need: dict[str, set[str]] = {}
        for o, got in rows.items():
            for c, t in parents(o):
                for r in got:
                    v = r.get(c)
                    if v and str(v) not in row_of:
                        need.setdefault(t, set()).add(str(v))
        for t, ids in sorted(need.items()):
            if time.monotonic() > deadline:
                not_checked.append(f"two hops via {t}")
                partial = True
                continue
            spec = cat.objects[t]
            ids_l = sorted(ids)
            if len(ids_l) > HOP_IDS_MAX:
                not_checked.append(f"two hops via {t} (more than {HOP_IDS_MAX} records)")
                ids_l = ids_l[:HOP_IDS_MAX]
                partial = True
            st_c = status_col(t)
            cols = list(dict.fromkeys(["Id", "Name"] + ([st_c] if st_c else []) + [c for c, _t in parents(t)]))
            cols = [c for c in cols if c in spec.fields and spec.fields[c].sensitivity != "blocked"]
            try:
                st = call.run(Frag("SELECT " + ", ".join(f"x.{quote_ident(c)}" for c in cols)
                                   + f" FROM main.{quote_ident(t)} AS x WHERE x.\"Id\" = ANY(?)", (ids_l,)),
                              max_rows=HOP_IDS_MAX, timeout=max(0.1, deadline - time.monotonic()),
                              max_bytes=LINK_BYTES_MAX)
            except RecordError:
                not_checked.append(f"two hops via {t}")
                partial = True
                continue
            call.table(t)
            for r in st.rows:
                d = dict(zip(cols, r))
                row_of.setdefault(str(d["Id"]), (t, d))

        # Step 3: the links.
        def hits(o: str, r: dict) -> list[tuple[str, str]]:
            """(person key, column) of the people a row holds: business person lookups, system lookups, its own Id."""
            out = []
            for c, _t in plk(o):
                k = idmap.get(str(r.get(c) or ""))
                if k is not None:
                    out.append((k, c))
            for c in SYSTEM_LOOKUPS:
                k = idmap.get(str(r.get(c) or "")) if c in r else None
                if k is not None:
                    out.append((k, c))
            k = idmap.get(str(r.get("Id") or ""))
            if k is not None:
                out.append((k, "Id"))
            return out

        def name_of(o: str, r: dict) -> str:
            nm = _disp(r.get("Name"))
            st_c = status_col(o)
            sv = r.get(st_c) if st_c else None
            return nm + (f" ({_disp(sv)})" if nm and sv not in (None, "") else "")

        def role(o: str, c: str) -> str:
            f = cat.objects[o].fields.get(c)
            return _short(_role(f), 40) if f is not None else c

        def plural(o: str) -> str:   # "<n> <label> records"
            return cat.label_of(o)

        def who(L: Any, k: str, o: str, c: str) -> str:
            """'<A> is `<c>` (<role>)' or, for a person's own record, "<A>'s own"."""
            return f"{L(k)}'s own" if c == "Id" else f"{L(k)} is `{c}` ({role(o, c)})"

        acc: dict[tuple, dict] = {}
        order = {"same record": 0, "own record": 1, "parent": 2, "two hops": 3, "shared parent": 4}

        def add(ka: str, kb: str, how: str, path: str, unit: str, ex: str, text: Any,
                cols: dict[str, str] | None = None) -> None:
            a, b = (ka, kb) if side[ka] < side[kb] else (kb, ka)
            key = (a, b, how, path)
            e = acc.get(key)
            if e is None:
                e = acc[key] = {"between_keys": [a, b], "how": how, "path": path, "units": set(), "examples": [],
                                "text": text, "cols": cols}
            e["units"].add(unit)
            if ex and ex not in e["examples"] and len(e["examples"]) < 2:
                e["examples"].append(ex)

        stop = deadline + 1.0   # step 3 is Python over the fetched rows; it stops too (partial)
        for o, got in rows.items():
            if time.monotonic() > stop:
                not_checked.append(f"links on {o} (time budget)")
                partial = True
                continue
            for r in got:
                hs = hits(o, r)
                if not hs:
                    continue
                rid = str(r["Id"])
                ex = name_of(o, r)
                for i, (ka, ca) in enumerate(hs):   # the same record / the person's own record
                    for kb, cb in hs[i + 1:]:
                        if ka == kb or side[ka] == side[kb] or ca == cb:
                            continue
                        if ca == "Id" or cb == "Id":
                            owner, other, oc = (ka, kb, cb) if ca == "Id" else (kb, ka, ca)
                            if oc in ("CreatedById", "LastModifiedById"):
                                continue   # creator and last editor of a person's own record: audit, no link
                            text = (lambda L, n, owner=owner, other=other, o=o, oc=oc:
                                    f"{L(owner)}'s {cat.label_of(o)} record has {L(other)} as `{oc}` ({role(o, oc)})")
                            add(owner, other, "own record", f"{o}.{oc} (own record)", rid, ex, text,
                                cols={owner: "Id", other: oc})
                            continue
                        if ca in SYSTEM_LOOKUPS and cb in SYSTEM_LOOKUPS:
                            continue
                        text = (lambda L, n, ka=ka, kb=kb, ca=ca, cb=cb, o=o:
                                f"{L(ka)} is `{ca}` ({role(o, ca)}) and {L(kb)} is `{cb}` ({role(o, cb)}) on {n} "
                                f"{plural(o)} records")
                        add(ka, kb, "same record", f"{o}.{ca} + {o}.{cb}", rid, ex, text, cols={ka: ca, kb: cb})
                mine = [(k, c) for k, c in hs if c not in SYSTEM_LOOKUPS]
                for pc, _pt in hop(o) if mine else []:   # one and two parent hops
                    pid = str(r.get(pc) or "")
                    if not pid or pid not in row_of or pid in idmap or pid in nobody:   # an asked person's record
                        continue
                    po, prow = row_of[pid]
                    third = po in PERSON_OBJECTS   # through a third person (a candidate, a recruiter, a manager)
                    for kb, cb in hits(po, prow):
                        if cb in SYSTEM_LOOKUPS or (third and cb == "Id"):
                            continue
                        for ka, ca in mine:
                            if ka == kb or side[ka] == side[kb]:
                                continue
                            text = (lambda L, n, ka=ka, kb=kb, ca=ca, cb=cb, o=o, pc=pc, po=po:
                                    f"{who(L, ka, o, ca)} on {n} {plural(o)} records whose `{pc}` ({cat.label_of(po)}) "
                                    + (f"is {L(kb)}" if cb == "Id" else f"has {L(kb)} as `{cb}` ({role(po, cb)})"))
                            add(ka, kb, "parent", f"{o}.{ca} -> {o}.{pc} -> {po}.{cb}", rid,
                                f"{ex} -> {name_of(po, prow)}" if third else ex, text)
                    if third:
                        continue   # two hops only through activity records, never through a third person
                    for gc, _gt in parents(po):
                        gid = str(prow.get(gc) or "")
                        if not gid or gid not in row_of or gid == rid:
                            continue
                        go, grow = row_of[gid]
                        for kb, cb in hits(go, grow):
                            if cb in SYSTEM_LOOKUPS:
                                continue
                            for ka, ca in mine:
                                if ka == kb or side[ka] == side[kb]:
                                    continue
                                text = (lambda L, n, ka=ka, kb=kb, ca=ca, cb=cb, o=o, po=po, gc=gc, go=go:
                                        f"{who(L, ka, o, ca)} on {n} {plural(o)} records under {plural(po)} records "
                                        f"whose `{gc}` ({cat.label_of(go)}) "
                                        + (f"is {L(kb)}" if cb == "Id" else f"has {L(kb)} as `{cb}` ({role(go, cb)})"))
                                add(ka, kb, "two hops", f"{o}.{ca} -> {o}.{pc} -> {po}.{gc} -> {go}.{cb}", rid, ex, text)
        # The same parent record: an activity record that holds people (a session, a training, a department...), or the
        # same person lookup on the people's own records (the same manager or team lead).
        # (kind, object, parent Id) -> (key, path) -> the rows (one entry per person and path: pairs stay few)
        under: dict[tuple[str, str, str], dict[tuple[str, str], set[str]]] = {}
        for o, got in rows.items():
            own_obj = o in PERSON_OBJECTS
            # a shared candidate of activity rows counts too (a lookup to a person object that is no staff object: two
            # people who only work with the same colleague are not linked); on the people's own records the same
            # person lookup is the "person" kind below
            subject = [] if own_obj else [(c, t) for c, t in plk(o) if cat.person_kind(t) != "employee"]
            for pc, pt in parents(o) + subject:
                if pt not in holds:
                    continue
                for r in got:
                    pid = str(r.get(pc) or "")
                    if not pid or pid in idmap or pid in nobody:
                        continue
                    for k, c in hits(o, r):
                        if c in SYSTEM_LOOKUPS or (c == "Id") != own_obj:
                            continue
                        under.setdefault(("parent", pt, pid), {}).setdefault(
                            (k, f"{o}.{c if c != 'Id' else pc}"), set()).add(str(r["Id"]))
            if own_obj:
                for c, t in plk(o):
                    for r in got:
                        k = idmap.get(str(r.get("Id") or ""))
                        u = str(r.get(c) or "")
                        if k is None or not u or u in idmap or u in nobody:
                            continue
                        under.setdefault(("person", t, u), {}).setdefault((k, f"{o}.{c}"), set()).add(str(r["Id"]))
        wanted: dict[str, set[str]] = {}
        shared: list[tuple] = []
        for (kind, t, pid), items in under.items():
            if time.monotonic() > stop:
                not_checked.append("shared parent records (time budget)")
                partial = True
                break
            its = list(items.items())
            for i, ((ka, pa), ra) in enumerate(its):
                for (kb, pb), rb in its[i + 1:]:
                    if ka == kb or side[ka] == side[kb] or len(ra | rb) < 2:   # one row holding both: same record
                        continue
                    if kind == "person" and pa != pb:
                        continue
                    shared.append((kind, t, pid, ka, kb, pa, pb))
                    if pid not in row_of:
                        wanted.setdefault(t, set()).add(pid)
        names_of: dict[str, str] = {}
        for t, ids in sorted(wanted.items()):
            if time.monotonic() > deadline:
                partial = True
                continue
            try:
                got_ = self._by_ids(call, [t], sorted(ids)[:50])
            except RecordError:
                continue
            for rws in got_.values():
                for r in rws:
                    names_of[r[0]] = _disp(r[1])
        for kind, t, pid, ka, kb, pa, pb in shared:
            pname = names_of.get(pid) or (name_of(*row_of[pid]) if pid in row_of else "")
            oa, fa = pa.split(".", 1)
            ob, fb = pb.split(".", 1)
            if pa == pb and oa in PERSON_OBJECTS:
                text = (lambda L, n, ka=ka, kb=kb, oa=oa, fa=fa, t=t, pname=pname, kind=kind:
                        f"{L(ka)} and {L(kb)} have the same `{fa}` ("
                        + (role(oa, fa) if kind == "person" else cat.label_of(t)) + f"): '{pname}'")
                add(ka, kb, "shared parent", f"{pa} (the same {cat.label_of(t)} on both own records)", pid, pname, text)
            else:
                text = (lambda L, n, ka=ka, kb=kb, fa=fa, fb=fb, t=t:
                        f"{L(ka)} (`{fa}`) and {L(kb)} (`{fb}`) are on the same {n} {cat.label_of(t)} record(s)")
                add(ka, kb, "shared parent", f"{pa} + {pb} -> {t}", pid, pname, text)
        def by_status(units: set[str]) -> dict[str, dict[str, int]]:
            """C4: the link's records per value of their object's status field (rows step 1 or 2 fetched)."""
            got: dict[str, dict[str, int]] = {}
            for u in sorted(units):
                if u not in row_of:
                    continue
                uo, urow = row_of[u]
                sc = status_col(uo)
                if not sc or sc not in urow:
                    continue
                v = _disp(urow.get(sc)) or "(blank)"
                got.setdefault(sc, {})[v] = got.setdefault(sc, {}).get(v, 0) + 1
            return dict(list(got.items())[:1])

        def others_of(e: dict) -> dict | None:
            """C4: the other people in the same role on the rows of one person of a same-record link, from the rows
            step 1 fetched (every row that holds a person of the question is fetched): ``{"field", "of", "records",
            "ids": {Id: rows}}``, or None when nobody else holds that role on them. Of the people whose rows hold
            others, the one whose own column holds the rows' subject (:meth:`_subject_role`: the candidate, so the
            other column lists the candidate's other trainers or interviewers, never a trainer's other candidates),
            else the one with fewer rows."""
            cols = e.get("cols") or {}
            o = e["path"].split(".", 1)[0]
            if len(cols) != 2 or o not in rows or any(c in SYSTEM_LOOKUPS for c in cols.values()):
                return None   # business roles only: the other creators or owners of a person's rows say nothing
            spec_o = cat.objects[o]
            best, best_key = None, None
            for x, y in (tuple(e["between_keys"]), tuple(reversed(e["between_keys"]))):
                cx, cy = cols.get(x), cols.get(y)
                if not cx or not cy:
                    continue
                mine_rows = [r for r in rows[o] if idmap.get(str(r.get(cx) or "")) == x]
                vals: dict[str, int] = {}
                for r in mine_rows:
                    v = str(r.get(cy) or "")
                    if v and idmap.get(v) != y and v not in nobody:
                        vals[v] = vals.get(v, 0) + 1
                fx = spec_o.fields.get(cx)
                key = (0 if fx is not None and self._subject_role(spec_o, fx) else 1, len(mine_rows))
                if vals and (best_key is None or key < best_key):
                    best, best_key = {"field": f"{o}.{cy}", "obj": o, "col": cy, "of": x, "records": len(mine_rows),
                                      "ids": vals}, key
            return best

        links = []
        for e in acc.values():
            n = len(e["units"])
            x = {"between_keys": e["between_keys"], "how": e["how"], "path": e["path"], "records": n,
                 "examples": e["examples"], "text": (lambda L, fn=e["text"], n=n: fn(L, n))}
            bs = by_status(e["units"]) if n >= 2 else {}   # one record: its example shows the status
            if bs:
                x["by_status"] = bs
            oth = others_of(e) if e["how"] == "same record" else None
            if oth is not None:
                x["others"] = oth
            if e["how"] == "own record":   # whose own record holds the lookup (the agent's relation checks)
                x["holder_key"] = next((k for k, c in (e.get("cols") or {}).items() if c == "Id"), None)
            links.append(x)
        # names of the other people (C4): one statement per target object, at most LINK_OTHERS_MAX names per link
        want: dict[str, set[str]] = {}
        for x in links:
            oth = x.get("others")
            if oth:
                t = cat.target_of(cat.objects[oth["obj"]].fields[oth["col"]])
                if t:
                    oth["target"] = t
                    want.setdefault(t, set()).update(sorted(oth["ids"], key=lambda i: -oth["ids"][i])[:LINK_OTHERS_MAX])
        names_by_id: dict[str, str] = {}
        for t, ids in sorted(want.items()):
            if time.monotonic() > stop:
                break
            try:
                got_ = self._by_ids(call, [t], sorted(ids)[:50])
            except RecordError:
                continue
            for rws in got_.values():
                for r in rws:
                    names_by_id[r[0]] = _disp(r[1])
        for x in links:
            oth = x.pop("others", None)
            if not oth:
                continue
            top = sorted(oth["ids"].items(), key=lambda kv: (-kv[1], names_by_id.get(kv[0], kv[0])))
            names = [f"{names_by_id.get(i) or '(no name)'} ({k})" for i, k in top[:LINK_OTHERS_MAX]]
            if len(top) > LINK_OTHERS_MAX:
                names.append(f"and {len(top) - LINK_OTHERS_MAX} more")
            x["others"] = {"field": oth["field"], "role": role(oth["obj"], oth["col"]), "of_key": oth["of"],
                           "records": oth["records"], "names": names}
        links.sort(key=lambda x: (order[x["how"]], any(f".{c}" in x["path"] for c in SYSTEM_LOOKUPS), -x["records"],
                                  x["path"]))
        # a two-hop link that joins the same two lookups of the same pair as a parent link says nothing new
        ends = {(tuple(x["between_keys"]), _path_ends(x["path"])) for x in links if x["how"] == "parent"}
        links = [x for x in links if x["how"] != "two hops"
                 or (tuple(x["between_keys"]), _path_ends(x["path"])) not in ends]
        checked = (f"{n_lookups} lookups on {n_objects} objects ({', '.join(read_objs)}): the same record (person lookups and the people's own "
                   "Employee, User, Account and Lead records; creator and last editor only next to a business lookup), "
                   "one parent hop (also through a third person: the candidate, recruiter or manager of a record), two "
                   "parent hops through activity records, the same activity record or candidate, the same manager, "
                   "team lead or department; never checked: names typed in text fields, objects without a knowledge "
                   "file, sensitive objects, links of three or more hops")
        if partial or not_checked:
            checked += "; this check stopped early or hit a limit, so a link can be missing: " + ("; ".join(not_checked[:6])
                                                                                          or "time budget")
        return links, {"checked": checked, "not_checked": not_checked, "partial": partial}


#: What each key of the Group B fixes means (one part per key present in an about_people result).
_KEYS_NOTE_PARTS = (
    ("linked", "people[].linked: the person's other records, one line each"),
    ("system_work", "people[].system_work: the records the person owns, created or last changed"),
    ("shared_by", "shared_by: the listed people whose records link to a shared record, which is nobody's own; "
                  "matched_records[].system_work: the records a shared record owns, created or last changed"),
    ("who", "references[].who: whose record (of people with the same name) a reference counts"),
    ("distinct", "references[].distinct: how many different candidates or leads the counted rows hold through that "
                 "lookup"),
    ("via_parents", "references[].via_parents: the same person through the parents of the counted rows; any_path: "
                    "rows in the lookup or any of these (a union)"),
    ("others", "links[].others: the other people in that role on one side's rows"),
    ("by_status", "links[].by_status: the link's records per status value"),
    ("more_references", "people[].more_references: lookups with records left out of references"),
)


def _keys_note(out: dict) -> str | None:
    """One note on the Group B keys present in an about_people result (:data:`_KEYS_NOTE_PARTS`), or None. A shared
    record (``shared_by``, review of the evaluation fixes, 2026-10-22) is named when one is listed."""
    have: set[str] = set()
    for p in out.get("people") or []:
        if isinstance(p, dict):
            have.update(k for k in ("linked", "system_work", "more_references") if p.get(k))
    if any(isinstance(m, dict) and m.get("shared_by") for m in out.get("matched_records") or []):
        have.add("shared_by")
    for e in list(out.get("references") or []) + list(out.get("system_references") or []):
        if isinstance(e, dict):
            have.update(k for k in ("who", "distinct", "via_parents") if e.get(k))
    for x in out.get("links") or []:
        if isinstance(x, dict):
            have.update(k for k in ("others", "by_status") if x.get(k))
    parts = [text for key, text in _KEYS_NOTE_PARTS if key in have]
    if not parts:
        return None
    return "; ".join(parts) + " (never add these counts to each other or to references)"


def _path_ends(path: str) -> tuple[str, str]:
    """The first and last lookup of a link path ("A.f -> A.p -> B.g" -> ("A.f", "B.g"))."""
    parts = [x.strip() for x in str(path).split("->")]
    return parts[0], parts[-1]


def _tops_first(entries: list[dict], n: int) -> set[int]:
    """Indexes of at most ``n`` references of one person (``entries`` in count order): first the top lookup of each of
    the person's records (so a linked record's lookups are never all left out: a lead's intake interviews next to the
    candidate account's many references), then the rest in count order."""
    tops: dict[str, int] = {}
    for i, e in enumerate(entries):
        tops.setdefault(str(e.get("record")), i)
    out = set(sorted(tops.values())[:n])
    for i in range(len(entries)):
        if len(out) >= n:
            break
        out.add(i)
    return out


def _ref_to_drop(refs: list[dict], persons: set[str]) -> int | None:
    """Index of the reference to cut next among the people ``persons`` who have 2 or more references (the person with
    the most first; ties: the later code): that person's last reference (the smallest count) that is not the top
    lookup of its record; None when only records' top lookups are left. A linked record keeps its top lookup until the
    references are cut down to none."""
    per: dict[str, list[int]] = {}
    for i, e in enumerate(refs):
        if _ref_owner(e) in persons:
            per.setdefault(_ref_owner(e), []).append(i)
    for person in sorted(per, key=lambda k: (len(per[k]), k), reverse=True):
        idx = per[person]
        if len(idx) < 2:
            continue
        tops: dict[str, int] = {}
        for i in idx:
            tops.setdefault(str(refs[i].get("record")), i)
        rest = [i for i in idx if i not in tops.values()]
        if rest:
            return rest[-1]
    return None


def _ref_owner(e: dict) -> str:
    """Whose share a reference is: its person, or the group of a shared record ("shared P1+P2": nobody's own)."""
    return str(e.get("person") or "shared " + "+".join(str(x) for x in e.get("shared_by") or []))


def _ref_text(e: dict) -> str:
    """A reference in one short line: '<Obj>.<field> <records> (<record code>)'."""
    return f"{e.get('object')}.{e.get('field')} {e.get('records')} ({e.get('record')})"


def _ref_count(text: str) -> int:
    """The records count of a :func:`_ref_text` line (0 when it has none)."""
    try:
        return int(str(text).rsplit(" (", 1)[0].rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


def _core_value(k: int, filled: int, f: FieldSpec, spec: ObjectSpec) -> bool:
    """Whether the value of the ``k``-th field of the object's profile list (``Catalog.profile_fields``), the record's
    ``filled``-th filled value, is in the profile core, which the result-size budget never cuts first (evaluation fix
    1, 2026-10-22): a field among the first :data:`PROFILE_CORE_FIELDS` of the list (the old 10-field profile), or a
    later field filled on most of the object's records (``2 * filled >= rows``: Date_of_Joining__c on most
    employees) while the record has at most :data:`PROFILE_CORE_FIELDS` filled values. So a field filled on few
    records that ranks above it (Designation__c, about a third) never moves an employee's joining date to the first cut
    on a record where it is blank; a later field filled on few records (most later Account fields) stays a detail."""
    return k < PROFILE_CORE_FIELDS or (filled <= PROFILE_CORE_FIELDS and 2 * f.filled >= spec.rows)
