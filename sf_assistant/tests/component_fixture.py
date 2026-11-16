"""A synthetic flow components folder (knowledge/components shape: ``flows/<Api>.json`` and ``index.json``) for the
component tests: fake flow names, fake objects of the warehouse fixture, no real text. Clean-checkout safe.

Every file plants development evidence (:data:`EVIDENCE_MARKERS`), at the top level as the build writes it and once
nested inside an effect, so a test can prove that no tool output and no search chunk ever holds it.

Flows:

* ``Fake_Outcome_Sync`` - after save on ``Interview__c`` (create, update; only when the save makes the entry rule
  true; an asynchronous path), updates the triggering interview (``Status__c`` = ``Closed``) and sends an e-mail;
  a production caveat and a dev10 note; ``facts_match_running_version`` true;
* ``Fake_Daily_Reminder`` - scheduled daily over ``Interview__c``, posts a Slack message and creates a ``Task``;
* ``Fake_Big_Wizard`` - a screen flow with :data:`BIG_ELEMENTS` elements and one effect whose ``when`` lists
  :data:`BIG_ROUTES` routes (larger than a page: split into parts), facts that do not describe the running version;
* ``Fake_Old_Inactive`` - an inactive short file (``detail`` note, no field list);
* ``Fake_Status_Setter`` - an autolaunched flow that ``Fake_Outcome_Sync`` runs (index ``called_by``) and that
  sets ``Interview__c.Status__c`` from a variable (a value not fixed in the flow; review H1);
* ``Fake_Filler_NN`` - ``fillers`` tiny after-save flows on ``Account`` (more than 40 rows for one target).

The index lists them, an excluded flow (``Fake_B2B_Excluded``), a not-built active flow (``Fake_Not_Built``) that
writes ``Interview__c.Status__c``, a not-built inactive flow whose name is the start of a built one's
(``Fake_Daily``, next to ``Fake_Daily_Reminder``; review L2), and an Apex writer (``apex:FakeStatusWriter``) of
``Interview__c.Status__c``.
"""
from __future__ import annotations

import json
from pathlib import Path

EVIDENCE_MARKERS = ("PLANTEDEVIDENCESOURCE7f3a", "PLANTEDLOCATOR7f3a", "PLANTEDNESTED7f3a")
BIG_ELEMENTS = 140
BIG_ROUTES = 400
ALIAS_SHARED = "shared fake alias"


def _evidence(api: str) -> list[dict]:
    return [{"source": f"{EVIDENCE_MARKERS[0]}_{api}.txt", "locator": f"{EVIDENCE_MARKERS[1]} section 3"},
            {"source": f"xml:prod_mirror:flows/{api}.flow-meta.xml {EVIDENCE_MARKERS[0]}", "locator": "start"}]


def _state(active: bool = True, match: bool | None = True, latest: str = "Active") -> dict:
    return {"is_active": active, "effective_status": "active" if active else "inactive",
            "latest_version_status": latest, "active_version_number": 2 if active else None,
            "active_version_basis": "activeVersionNumber 2 in the fake mirror" if active else "no active version",
            "facts_from": "fake production mirror", "facts_match_running_version": match, "owner_rules": []}


def _flow(api: str, label: str, title: str, *, start: dict, effects: list, state: dict, caveats: list,
          fields: list | None = None, elements: list | None = None, aliases: list | None = None,
          objects: list | None = None, detail: str | None = None, also: dict | None = None) -> dict:
    d = {"format_version": 1, "kind": "flow", "id": f"flow:{api}", "api_name": api, "label": label, "title": title,
         "one_line": f"Fake flow {title.lower()} for the component tests.", "aliases": aliases or [],
         "state": state}
    if detail:
        d["detail"] = detail
    d.update({"business_meaning": f"{title}: a synthetic flow. It exists only in the tests. Nothing real is in it.",
              "semantic": {"concept": f"fake {title.lower()}"}, "business_processes": ["fake_process"],
              "features": [{"id": "F99", "name": "Fake feature"}], "departments": ["Training"],
              "flow": {"process_type": "AutoLaunchedFlow", "api_version": "65.0", "start": start,
                       "elements": elements or [], "effects": effects, "ends_without_action": [],
                       "interface": {"inputs": [], "outputs": []},
                       "run_context": {"declared": "none", "effective": "system_context", "basis": "fake"}},
              "caveats": caveats})
    if detail:
        d["flow"] = {"process_type": "Flow", "api_version": "64.0", "start": start, "effects": effects}
    if also is not None:
        d["also_written_by"] = also
    d["calls"] = []
    d["objects"] = objects or []
    if fields is not None:
        d["fields"] = fields
    d["evidence"] = _evidence(api)
    return d


def flows(fillers: int = 45) -> dict[str, dict]:
    """``{ApiName: component document}`` of the fixture."""
    out: dict[str, dict] = {}
    entry = {"logic": "and", "conditions": ["(1) $Record.Interview_Outcome__c IsNull false"], "formula": None,
             "only_when_changed_to_meet_criteria": True,
             "plain": "Runs when a save gives the interview an outcome, not on every save."}
    out["Fake_Outcome_Sync"] = _flow(
        "Fake_Outcome_Sync", "Fake Outcome Sync", "Fake interview outcome sync",
        start={"type": "after_save", "object": "Interview__c", "events": ["create", "update"], "entry": entry,
               "paths": [{"id": "immediate", "kind": "immediate"},
                         {"id": "Async_Path", "kind": "async_after_commit", "label": "Async"}], "next": "Decide"},
        effects=[{"kind": "update", "object": "Interview__c", "element": "Close_It", "path": "immediate",
                  "when": ["Decide=Has_Outcome"], "target": "triggering_record",
                  "sets": {"Status__c": {"literal": "Closed"}, "Notes__c": {"from": "$Record.Interview_Outcome__c"}},
                  "leaves_unchanged": [], "on_fault": "none",
                  "evidence": [{"source": EVIDENCE_MARKERS[2], "locator": EVIDENCE_MARKERS[2]}]},
                 {"kind": "email", "element": "Mail_Owner", "path": "Async_Path", "when": [],
                  "recipients": [{"as": "to", "from": "User.Email", "hops": ["Interview__c.OwnerId"]}],
                  "sender": "an org-wide sender address (not copied)", "on_fault": "none"}],
        state=_state(), caveats=["The flow re-runs on every outcome change, so a second change closes it again.",
                                 "dev10 (fake KT): the flow is not in dev10."],
        fields=[{"field": "Interview__c.Interview_Outcome__c", "operations": ["read"], "used_in": ["$Start", "Decide"]},
                {"field": "Interview__c.Status__c", "operations": ["update"], "used_in": ["Close_It"],
                 "values_written": ["Interview__c.Status__c=Closed"]},
                {"field": "Interview__c.Notes__c", "operations": ["update"], "used_in": ["Close_It"]}],
        elements=[{"api_name": "Decide", "type": "decision", "label": "Decide", "path": "immediate",
                   "outcomes": [{"name": "Has_Outcome", "label": "Has outcome", "logic": "and",
                                 "conditions": ["$Record.Interview_Outcome__c IsNull false"], "next": "Close_It"}],
                   "default": {"label": "Other", "next": None}}],
        aliases=["outcome closer", ALIAS_SHARED],
        objects=[{"object": "Interview__c", "operations": ["read", "update"], "role": ["triggered_on", "updates"]}],
        also={"Interview__c.Status__c": ["apex:FakeStatusWriter", "flow:Fake_Not_Built", "flow:Fake_Status_Setter"],
              "Interview__c.Notes__c": []})
    out["Fake_Daily_Reminder"] = _flow(
        "Fake_Daily_Reminder", "Fake Daily Reminder", "Fake daily interview reminder",
        start={"type": "scheduled", "object": "Interview__c", "runs": "once per matching record",
               "schedule": {"frequency": "Daily", "start_date": "2026-01-01", "start_time_xml": "10:00:00.000Z",
                            "org_time": "10:00", "org_time_zone": "America/New_York",
                            "utc": {"edt": "14:00", "est": "15:00"}, "ist": {"edt": "19:30", "est": "20:30"},
                            "first_run": "2026-01-01 10:00 EST"},
               "entry": {"logic": "and", "conditions": ["(1) $Record.Status__c EqualTo Open"], "formula": None,
                         "only_when_changed_to_meet_criteria": False, "plain": "Every open interview, once a day."},
               "paths": [{"id": "immediate", "kind": "immediate"}], "next": "Post"},
        effects=[{"kind": "slack", "element": "Post", "path": "immediate", "when": [],
                  "recipients": [{"as": "conversation", "literal": "fixed value in the flow (not copied)"}],
                  "on_fault": "none"},
                 {"kind": "create", "object": "Task", "element": "Make_Task", "path": "immediate", "when": [],
                  "target": "new_record", "sets": {"Subject": {"literal": "Fake reminder"}}, "on_fault": "none"}],
        state=_state(), caveats=[], aliases=[ALIAS_SHARED, "daily nudge"],
        fields=[{"field": "Interview__c.Status__c", "operations": ["read"], "used_in": ["$Start"]},
                {"field": "Task.Subject", "operations": ["create"], "used_in": ["Make_Task"],
                 "values_written": ["Task.Subject=Fake reminder"]}],
        objects=[{"object": "Interview__c", "operations": ["read"], "role": ["scheduled_on"]},
                 {"object": "Task", "operations": ["create"], "role": ["creates"]}],
        also={"Task.Subject": []})
    big_elements = [{"api_name": f"Screen_Step_{i:03d}", "type": "screen", "label": f"Screen step {i} " + "x" * 60,
                     "path": "immediate", "next": f"Screen_Step_{i + 1:03d}",
                     "inputs": [{"name": f"input_{i}", "type": "String", "writes": [], "required": False}]}
                    for i in range(BIG_ELEMENTS)]
    routes = [f"Screen_Step_{i % BIG_ELEMENTS:03d}=Route_{i:04d} AND Check_{i:04d}=Yes" for i in range(BIG_ROUTES)]
    out["Fake_Big_Wizard"] = _flow(
        "Fake_Big_Wizard", "Fake Big Wizard", "Fake big setup wizard",
        start={"type": "screen", "object": None, "entry": None, "paths": [{"id": "immediate", "kind": "immediate"}],
               "next": "Screen_Step_000"},
        effects=[{"kind": "create", "object": "Interview__c", "element": "Save_All", "path": "immediate",
                  "when": routes, "target": "new_record",
                  "sets": {f"Field_{i:02d}__c": {"variable": f"var{i}"} for i in range(30)}, "on_fault": "none"}],
        state=_state(match=False, latest="Draft"), caveats=[f"Caveat {i}: " + "a long fake caveat sentence. " * 6
                                                            for i in range(12)],
        fields=[{"field": f"Interview__c.Field_{i:02d}__c", "operations": ["create"], "used_in": ["Save_All"]}
                for i in range(30)],
        elements=big_elements, aliases=["setup wizard"],
        objects=[{"object": "Interview__c", "operations": ["create"], "role": ["creates"]}],
        also={f"Interview__c.Field_{i:02d}__c": [] for i in range(30)})
    out["Fake_Old_Inactive"] = _flow(
        "Fake_Old_Inactive", "Fake Old Inactive", "Fake retired interview stamp",
        start={"type": "after_save", "object": "Interview__c", "events": ["update"], "entry": None,
               "paths": [{"id": "immediate", "kind": "immediate"}], "next": None},
        effects=[], state=_state(active=False, match=None, latest="Obsolete"), caveats=[],
        detail="short: inactive flow; elements, decisions, interface, run context and field list are left out",
        objects=[])
    out["Fake_Status_Setter"] = _flow(
        "Fake_Status_Setter", "Fake Status Setter", "Fake interview status setter",
        start={"type": "autolaunched", "object": None, "entry": None, "paths": [{"id": "immediate", "kind": "immediate"}],
               "next": "Set_Status"},
        effects=[{"kind": "update", "object": "Interview__c", "element": "Set_Status", "path": "immediate", "when": [],
                  "target": "by_filter", "sets": {"Status__c": {"variable": "varStatus"}}, "on_fault": "none"}],
        state=_state(), caveats=[],
        fields=[{"field": "Interview__c.Status__c", "operations": ["update"], "used_in": ["Set_Status"]}],
        objects=[{"object": "Interview__c", "operations": ["update"], "role": ["updates"]}],
        also={"Interview__c.Status__c": ["apex:FakeStatusWriter", "flow:Fake_Not_Built", "flow:Fake_Outcome_Sync"]})
    for i in range(fillers):
        api = f"Fake_Filler_{i:02d}"
        out[api] = _flow(api, f"Fake Filler {i:02d}", f"Fake account filler {i:02d}",
                         start={"type": "after_save" if i % 2 else "before_save", "object": "Account",
                                "events": ["update"], "entry": None, "paths": [{"id": "immediate", "kind": "immediate"}],
                                "next": None},
                         effects=[], state=_state(), caveats=[], fields=[],
                         objects=[{"object": "Account", "operations": ["read"], "role": ["triggered_on"]}], also={})
    return out


def index(docs: dict[str, dict]) -> dict:
    """The index.json of ``docs`` (the shape of kb_build/flows/build_flow_index.py)."""
    comps, by_obj, by_field, by_value = {}, {}, {}, {}
    roles = ("triggered_on", "scheduled_on", "creates", "reads", "updates", "deletes")
    for api, d in sorted(docs.items()):
        ref = f"flow:{api}"
        st = d["flow"]["start"]
        c = {"file": f"flows/{api}.json", "label": d["label"], "title": d["title"], "start": st["type"],
             "object": st.get("object")}
        if st.get("events"):
            c["events"] = st["events"]
        c.update({"is_active": d["state"]["is_active"], "effective_status": d["state"]["effective_status"],
                  "facts_from": d["state"]["facts_from"]})
        comps[ref] = c
        for o in d.get("objects") or []:
            row = by_obj.setdefault(o["object"], {r: [] for r in roles + ("inactive",)})
            for r in o["role"]:
                row[r].append(ref)
            if not d["state"]["is_active"]:
                row["inactive"].append(ref)
        for f in d.get("fields") or []:
            e = {"flow": ref, "operations": f["operations"]}
            if f.get("values_written"):
                e["values_written"] = f["values_written"]
            e["active"] = d["state"]["is_active"]
            by_field.setdefault(f["field"], []).append(e)
            for v in f.get("values_written") or []:
                by_value.setdefault(v, {"set_by": []})["set_by"].append(ref)
    written = {"Interview__c.Status__c": ["apex:FakeStatusWriter", "flow:Fake_Not_Built", "flow:Fake_Outcome_Sync",
                                          "flow:Fake_Status_Setter"],
               "Interview__c.Notes__c": ["flow:Fake_Outcome_Sync"], "Task.Subject": ["flow:Fake_Daily_Reminder"]}
    written.update({f"Interview__c.Field_{i:02d}__c": ["flow:Fake_Big_Wizard"] for i in range(30)})
    return {"format_version": 1, "kind": "component_index", "scope_note": "fake components of the tests",
            "inputs_sha256": "0" * 64, "components": comps,
            "excluded": {"flow:Fake_B2B_Excluded": {"rule": "fake_rule", "decided": "2026-10-22",
                                                    "reason": "fake flows are left out (owner)"}},
            "not_built": {"flow:Fake_Daily": "production flow with no document source, inactive in production (left "
                                             "out of the build)",
                          "flow:Fake_Not_Built": "production flow with no document source, active in production "
                                                 "(left out of the build)"},
            "called_by": {"flow:Fake_Status_Setter": ["flow:Fake_Outcome_Sync"]}, "by_object": by_obj, "by_field": by_field, "by_value": by_value,
            "written_by": dict(sorted(written.items())), "by_feature": {"F99": sorted(comps)},
            "by_process": {"fake_process": sorted(comps)}, "by_department": {"Training": sorted(comps)}}


def make_components(folder: Path, fillers: int = 45) -> Path:
    """Write the fixture into ``folder`` (``flows/*.json`` and ``index.json``); returns ``folder``."""
    docs = flows(fillers)
    (folder / "flows").mkdir(parents=True, exist_ok=True)
    for api, d in docs.items():
        (folder / "flows" / f"{api}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
    (folder / "index.json").write_text(json.dumps(index(docs), indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return folder
