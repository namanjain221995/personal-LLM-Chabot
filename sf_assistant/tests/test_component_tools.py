"""The flow components on the answering side (stage 4 of the flow build, 2026-10-24): the loader
(sf_assistant.knowledge.components), the three component tools (sf_assistant.tools.component_tools), their place in
the several-object registry, the pipeline and the prompt note, the budgets and the evidence rule.

The synthetic fixture (tests/component_fixture.py) runs on a clean checkout; the checks on the real files
(knowledge/components, git-ignored) skip when the folder is not there. No model, no record data.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from component_fixture import ALIAS_SHARED, BIG_ELEMENTS, BIG_ROUTES, EVIDENCE_MARKERS, make_components  # noqa: E402
from warehouse_fixture import make_knowledge  # noqa: E402

from sf_assistant import config as C  # noqa: E402
from sf_assistant import pipeline  # noqa: E402
from sf_assistant.agent import knowledge_prompts as KP  # noqa: E402
from sf_assistant.agent.lean import LeanRegistry  # noqa: E402
from sf_assistant.knowledge.components import (EVIDENCE_KEYS, ComponentError, ComponentKnowledge, effect_phrase,  # noqa: E402
                                               has_index, load_components, start_phrase, strip_evidence, value_sources)
from sf_assistant.knowledge.store import ObjectKnowledge, knowledge_files  # noqa: E402
from sf_assistant.tools import registry as R  # noqa: E402
from sf_assistant.tools.component_tools import (MORE_SCORE_SHARE, PAGE_CHARS, SEARCH_CHARS, SUMMARY_CHARS,  # noqa: E402
                                                USAGE_CHARS, USAGE_ROWS, ComponentTools, _size, split_caveats)
from sf_assistant.tools.knowledge_tools import MultiToolRegistry  # noqa: E402

REAL = ROOT / "knowledge" / "components"
real = pytest.mark.skipif(not (REAL / "index.json").is_file(), reason="knowledge/components (git-ignored) is not here")
NAMES = ("search_components", "get_component", "find_usages")


@pytest.fixture(scope="module")
def comp_dir(tmp_path_factory):
    return make_components(tmp_path_factory.mktemp("components"))


@pytest.fixture(scope="module")
def ck(comp_dir):
    return ComponentKnowledge.load(comp_dir)


@pytest.fixture(scope="module")
def ct(ck):
    return ComponentTools(ck, object_flows={"Fake_Outcome_Sync": ["Interview__c", "Account"]})


@pytest.fixture(scope="module")
def real_ct():
    return ComponentTools(ComponentKnowledge.load(REAL))


def _ok(out: dict) -> dict:
    assert out["ok"], out
    return out["result"]


def every_output(tools: ComponentTools, queries=("interview outcome", "daily reminder", "wizard", "nothing at all")):
    """Every result and error the three tools give over a components set: each component at every detail level and
    page, a search per query, find_usages for every object and field of the index in every role, and a few bad
    calls."""
    outs = []
    for cid in tools.knowledge.ids():
        for detail in R.COMPONENT_DETAILS:
            first = tools.call("get_component", {"name": cid, "detail": detail})
            outs.append(first)
            for page in range(2, ((first.get("result") or {}).get("pages") or 1) + 1):
                outs.append(tools.call("get_component", {"name": cid, "detail": detail, "page": page}))
    for q in queries:
        outs.append(tools.call("search_components", {"query": q, "k": 10}))
    idx = tools.knowledge.index
    for target in list(idx.get("by_object") or {}) + list(idx.get("by_field") or {})[:400]:
        for role in R.USAGE_ROLES:
            outs.append(tools.call("find_usages", {"target": target, "role": role}))
    for key, rows in list((idx.get("by_field") or {}).items())[:400]:   # a value query of every literal written
        for v in sorted({str(x).split("=", 1)[1] for r in rows for x in r.get("values_written") or [] if "=" in str(x)}):
            outs.append(tools.call("find_usages", {"target": key, "role": "writes", "value": v}))
    outs += [tools.call("get_component", {"name": "no such flow"}), tools.call("find_usages", {"target": "Nope__c"}),
             tools.call("get_component", {"name": ALIAS_SHARED})]
    return outs


# ------------------------------------------------------------------------------------------------------ the loader
def test_loader_reads_the_folder_and_strips_the_evidence(ck, comp_dir):
    assert ck.counts() == {"flow": 50} and ck.evidence_entries == 2 * 50
    assert ck.ids()[0] == "flow:Fake_Big_Wizard" and ck.folder == str(comp_dir)
    raw = json.loads((comp_dir / "flows" / "Fake_Outcome_Sync.json").read_text(encoding="utf-8"))
    assert raw["evidence"] and raw["flow"]["effects"][0]["evidence"]          # the files keep it (development reference)
    for c in ck.components.values():
        text = json.dumps(c.doc)
        assert not any(m in text for m in EVIDENCE_MARKERS) and '"evidence"' not in text   # the loaded view never
    assert strip_evidence({"a": [{"evidence": 1, "b": {"evidence": 2, "c": 3}}]}) == {"a": [{"b": {"c": 3}}]}
    assert EVIDENCE_KEYS == {"evidence"}
    assert ck.validate() == []


def test_loader_refuses_broken_folders(tmp_path):
    with pytest.raises(ComponentError, match="no index.json"):
        ComponentKnowledge.load(tmp_path)
    assert load_components(None) is None and load_components(tmp_path) is None and not has_index(tmp_path)
    d = make_components(tmp_path / "c", fillers=1)
    assert has_index(d) and load_components(d).counts() == {"flow": 6}
    f = d / "flows" / "Fake_Outcome_Sync.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["id"] = "flow:Other"
    f.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ComponentError, match="do not match the file name"):
        ComponentKnowledge.load(d)
    doc["id"] = "flow:Fake_Outcome_Sync"
    doc["state"]["is_active"] = "yes"
    f.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ComponentError, match="state.is_active"):
        ComponentKnowledge.load(d)
    doc["state"]["is_active"] = True
    del doc["caveats"]
    f.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ComponentError, match="missing keys"):
        ComponentKnowledge.load(d)
    make_components(d, fillers=1)
    doc = json.loads(f.read_text(encoding="utf-8"))
    (d / "flows" / "Stray_Flow.json").write_text(json.dumps({**doc, "id": "flow:Stray_Flow", "api_name": "Stray_Flow"}),
                                                 encoding="utf-8")
    with pytest.raises(ComponentError, match="no index.json components entry flow:Stray_Flow"):
        ComponentKnowledge.load(d)
    (d / "flows" / "Stray_Flow.json").unlink()
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    idx["by_field"]["Interview__c.Status__c"].append({"flow": "flow:Ghost", "operations": ["read"], "active": True})
    idx["components"]["flow:Fake_Daily_Reminder"]["title"] = "another title"
    (d / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    with pytest.raises(ComponentError) as exc:
        ComponentKnowledge.load(d)
    assert "flow:Ghost" in str(exc.value) and "title differs" in str(exc.value)
    (d / "index.json").write_text("{", encoding="utf-8")
    with pytest.raises(ComponentError, match="not readable JSON"):
        ComponentKnowledge.load(d)


def test_names_resolve_by_api_name_label_title_alias_with_did_you_mean(ck):
    for typed, how in (("Fake_Outcome_Sync", "api_name"), ("flow:Fake_Outcome_Sync", "api_name"),
                       ("fake outcome sync", "label"), ("Fake interview outcome sync", "title"),
                       ("FAKE-OUTCOME-SYNC", "loose"), ("outcome closer", "alias")):
        c, got = ck.resolve(typed)
        assert c is not None and c.cid == "flow:Fake_Outcome_Sync" and got == {"matched": how}, typed
    c, got = ck.resolve(ALIAS_SHARED)
    assert c is None and got == {"ambiguous": ["flow:Fake_Daily_Reminder", "flow:Fake_Outcome_Sync"]}
    c, got = ck.resolve("Fake_B2B_Excluded")
    assert c is None and got["left_out"]["built"] is False and "owner" in got["left_out"]["reason"]
    c, got = ck.resolve("fake not built")
    assert c is None and got["left_out"]["status"] == "not built"
    c, got = ck.resolve("Fake_Outcome_Synk")
    assert c is None and got["did_you_mean"][0] == "flow:Fake_Outcome_Sync"


def test_phrases_and_cards(ck):
    c = ck.get("flow:Fake_Outcome_Sync")
    assert effect_phrase(c.effects[0]) == "updates the triggering Interview__c (Status__c = Closed, Notes__c)"
    assert start_phrase(c.start) == ("after save of Interview__c on create, update; only when the save makes the entry "
                                     "rule true; async_after_commit path Async_Path: after the save commits")
    card = ck.card("flow:Fake_Outcome_Sync")
    assert list(card) == ["component", "title", "label", "one_line", "aliases", "runs", "effects", "state"]
    assert "Status__c (Closed)" in card["effects"] and card["state"] == "active (fake production mirror)"
    assert "facts describe a version that does not run" in ck.card("flow:Fake_Big_Wizard")["state"]
    chunk = next(x for x in ck.chunks() if x["id"] == "component::flow:Fake_Outcome_Sync")
    assert chunk["section"] == "flow" and "outcome closer" in chunk["text"] and "Interview_Outcome__c" not in chunk["text"]


# ------------------------------------------------------------------------------------------------- search_components
def test_search_components_finds_cards_within_budget(ct):
    res = _ok(ct.call("search_components", {"query": "interview outcome closer"}))
    assert res["results"][0]["component"] == "flow:Fake_Outcome_Sync" and res["count"] == len(res["results"])
    assert set(res["results"][0]) == {"component", "title", "label", "one_line", "aliases", "runs", "effects", "state",
                                      "score"}
    out = ct.call("search_components", {"query": "fake", "k": 10})
    assert out["chunk_ids"][0].startswith("component::flow:") and _size(out["result"]) <= SEARCH_CHARS
    assert out["result"]["count"] <= 10 and "more" in out["result"]
    assert _ok(ct.call("search_components", {"query": "fake", "k": 99}))["count"] <= 10   # k is clamped
    none = _ok(ct.call("search_components", {"query": "zzzqqq"}))
    assert none["count"] == 0 and none["coverage"]["components"] == {"flow": 50} and none["coverage"]["not_covered"]
    left = _ok(ct.call("search_components", {"query": "what does Fake_B2B_Excluded do"}))["left_out"]
    assert left == [{"component": "flow:Fake_B2B_Excluded", "built": False, "status": "excluded by an owner rule",
                     "reason": "fake flows are left out (owner)"}]
    assert not ct.call("search_components", {"query": "x", "kind": "apex"})["ok"]          # kinds: flow only
    assert not ct.call("search_components", {})["ok"]


# ------------------------------------------------------------------------------------------------- get_component
def test_summary_opens_with_state_and_keeps_production_first(ct):
    s = _ok(ct.call("get_component", {"name": "Fake interview outcome sync"}))
    assert list(s)[:7] == ["component", "kind", "api_name", "label", "title", "one_line", "state"]
    assert s["state"] == {"active": True, "status": "active", "facts_from": "fake production mirror",
                          "active_version": 2, "facts_match_running_version": True}
    assert s["start"]["entry_rule"].startswith("Runs when a save gives") and s["start"]["events"] == ["create", "update"]
    assert s["caveats"] == ["The flow re-runs on every outcome change, so a second change closes it again."]
    assert s["dev10_notes"] == ["dev10 (fake KT): the flow is not in dev10."]
    assert s["object_entries"]["objects"] == ["Account", "Interview__c"] and "component" in s["object_entries"]["rule"]
    assert s["resolved_from"] == {"typed": "Fake interview outcome sync", "matched": "title"}
    assert set(s["more"]) == {"effects", "logic", "fields", "caveats"}
    big = _ok(ct.call("get_component", {"name": "Fake_Big_Wizard"}))
    assert list(big)[6] == "warning" and "not the version that runs" in big["warning"]
    assert _size(big) <= SUMMARY_CHARS and big["effects_note"].endswith("detail=logic")
    assert "detail=caveats" in big["caveats_note"] and big["start"]["launched_from"].startswith("launch points")
    assert "called_by" not in big["start"] and "called_by" not in big["start"]["launched_from"]
    old = _ok(ct.call("get_component", {"name": "Fake_Old_Inactive"}))
    assert old["state"]["active"] is False and old["detail_note"].startswith("short: inactive")
    assert old["effects"] == "none in the flow file"
    assert split_caveats(["dev10: a", "b", "DEV10 (x): c", "the dev10 copy differs"]) == (
        ["b", "the dev10 copy differs"], ["dev10: a", "DEV10 (x): c"])


def test_paged_levels_fit_a_page_and_lose_nothing(ct, ck):
    for cid in ck.ids():
        c = ck.get(cid)
        for detail in ("logic", "fields", "caveats"):
            first = _ok(ct.call("get_component", {"name": cid, "detail": detail}))
            pages = [first] + [_ok(ct.call("get_component", {"name": cid, "detail": detail, "page": p}))
                               for p in range(2, first["pages"] + 1)]
            assert [p["page"] for p in pages] == list(range(1, first["pages"] + 1))
            assert [p["next_page"] for p in pages] == list(range(2, first["pages"] + 1)) + [None]
            assert all(_size(p) <= PAGE_CHARS for p in pages)
            text = json.dumps([p["content"] for p in pages])
            if detail == "logic":
                for el in (c.doc["flow"].get("elements") or []):
                    assert json.dumps(el["api_name"]) in text
                for e in c.effects:
                    for w in e.get("when") or []:
                        assert json.dumps(w) in text
            if detail == "fields":
                assert all(json.dumps(f["field"]) in text for f in c.doc.get("fields") or [])
            if detail == "caveats":
                assert all(json.dumps(x) in text for x in c.doc["caveats"])
    logic = ct.pages(ck.get("flow:Fake_Big_Wizard"), "logic")
    effects = [x for p in logic for x in p["content"].get("effects", [])]
    assert len(effects) > 1 and effects[0]["part"] == f"1/{len(effects)}" and effects[-1]["element"] == "Save_All"
    assert sum(len(x.get("when") or []) for x in effects) == BIG_ROUTES                  # every route, once
    assert list(logic[0]["content"])[0] == "effects"                                    # effects before logic
    assert sum(len(p["content"].get("elements", [])) for p in logic) == BIG_ELEMENTS
    fields = ct.pages(ck.get("flow:Fake_Outcome_Sync"), "fields")[0]["content"]["fields"]
    assert fields[1]["also_written_by"] == ["apex:FakeStatusWriter", "flow:Fake_Not_Built", "flow:Fake_Status_Setter"]
    assert "also_written_by" not in fields[0]                                           # a read-only field
    cav = ct.pages(ck.get("flow:Fake_Outcome_Sync"), "caveats")[0]["content"]
    assert list(cav)[:4] == ["state", "business_meaning", "caveats", "dev10_notes"]


def test_get_component_errors_and_left_out_flows(ct):
    out = ct.call("get_component", {"name": "Fake_Outcome_Synk"})
    assert not out["ok"] and "did you mean flow:Fake_Outcome_Sync (Fake interview outcome sync)" in out["error"]
    out = ct.call("get_component", {"name": ALIAS_SHARED})
    assert not out["ok"] and "alias of several flows" in out["error"] and "flow:Fake_Daily_Reminder" in out["error"]
    left = _ok(ct.call("get_component", {"name": "Fake_Not_Built"}))
    assert left["built"] is False and left["status"] == "not built" and left["note"].startswith("no component file")
    out = ct.call("get_component", {"name": "Fake_Outcome_Sync", "detail": "logic", "page": 9})
    assert not out["ok"] and "past the last page (1)" in out["error"]
    assert not ct.call("get_component", {"name": "Fake_Outcome_Sync", "detail": "everything"})["ok"]
    assert not ct.call("get_component", {"name": "Fake_Outcome_Sync", "kind": "apex"})["ok"]
    assert not ct.call("no_such_tool", {})["ok"]
    assert _ok(ct.call("get_component", '{"name": "Fake_Outcome_Sync", "detail": "LOGIC"}'))["detail"] == "logic"


# ------------------------------------------------------------------------------------------------- find_usages
def test_find_usages_rows_roles_values_and_coverage(ct):
    res = _ok(ct.call("find_usages", {"target": "interview__c.status__c", "role": "writes"}))
    assert res["target"] == "Interview__c.Status__c" and res["count"] == 4
    assert [r.get("flow") or r.get("writer") for r in res["rows"]] == [
        "flow:Fake_Outcome_Sync", "flow:Fake_Status_Setter", "apex:FakeStatusWriter", "flow:Fake_Not_Built"]
    assert res["rows"][0]["values"] == ["Closed"] and res["rows"][0]["only_when_changed"] is True
    assert "value_from" not in res["rows"][0] and res["rows"][1]["value_from"] == ["variable varStatus"]
    assert res["rows"][3]["built"] is False and len(res["not_covered"]) == 4
    notes = _ok(ct.call("find_usages", {"target": "Interview__c.Notes__c", "role": "writes"}))["rows"][0]
    assert notes["value_from"] == ["from $Record.Interview_Outcome__c"] and "values" not in notes   # A07: computed
    assert "other_writers" not in res
    reads = _ok(ct.call("find_usages", {"target": "Interview__c.Status__c", "role": "reads"}))
    assert [r["flow"] for r in reads["rows"]] == ["flow:Fake_Daily_Reminder"]
    trig = _ok(ct.call("find_usages", {"target": "Interview__c.Interview_Outcome__c", "role": "triggered_on"}))
    assert [r["flow"] for r in trig["rows"]] == ["flow:Fake_Outcome_Sync"] and "order_note" in trig
    obj = _ok(ct.call("find_usages", {"target": "Interview__c", "role": "triggered_on"}))
    assert [r["flow"] for r in obj["rows"]] == ["flow:Fake_Outcome_Sync", "flow:Fake_Daily_Reminder"]
    assert obj["rows"][1]["roles"] == ["scheduled_on"]
    anyrole = _ok(ct.call("find_usages", {"target": "Interview__c"}))
    assert {"writer": "apex:FakeStatusWriter", "roles": ["writes"], "fields": 1,
            "from": "object knowledge entry (Apex body not scanned)"} in anyrole["rows"]
    many = _ok(ct.call("find_usages", {"target": "Account", "role": "triggered_on"}))
    assert many["count"] == 45 and len(many["rows"]) == USAGE_ROWS and many["more"] == 5
    assert [r["start"] for r in many["rows"][:23]] == ["before_save"] * 23                  # before save first
    miss = _ok(ct.call("find_usages", {"target": "Interveiw__c"}))
    assert miss["count"] == 0 and miss["did_you_mean"][0] == "Interview__c" and miss["not_covered"]
    assert not ct.call("find_usages", {"target": "Interview__c", "value": "x"})["ok"]          # value needs a field
    out = ct.call("find_usages", {"target": "Interview__c.Status__c", "role": "reads", "value": "Closed"})
    assert not out["ok"] and "role writes or any" in out["error"]                              # value: writers only
    assert not ct.call("find_usages", {"target": "Interview__c", "role": "deletes"})["ok"]


def test_find_usages_value_names_every_writer_that_may_set_it(ct):
    """Review H1: a value query never answers 'no flow writes V' while a writer sets the field from a variable."""
    val = _ok(ct.call("find_usages", {"target": "Interview__c.Status__c", "role": "writes", "value": "closed"}))
    assert [r["flow"] for r in val["rows"]] == ["flow:Fake_Outcome_Sync"] and val["count"] == 1
    assert [(r["flow"], r["value_from"], r["may_write"]) for r in val["values_not_fixed"]] == [
        ("flow:Fake_Status_Setter", ["variable varStatus"], True)]
    assert val["values_unknown"] == ["apex:FakeStatusWriter (Apex body not scanned)",
                                     "flow:Fake_Not_Built (not built: its values are not in the knowledge)"]
    assert val["writes_other_values"] == [] and "note" not in val and "other_writers" not in val
    other = _ok(ct.call("find_usages", {"target": "Interview__c.Status__c", "value": "Open"}))
    assert other["rows"] == [] and other["count"] == 0 and len(other["values_not_fixed"]) == 1
    assert other["writes_other_values"] == ["flow:Fake_Outcome_Sync (Closed)"]
    assert other["note"].startswith("no flow component writes the literal value Open to Interview__c.Status__c; the "
                                    "writers under values_not_fixed")
    none = _ok(ct.call("find_usages", {"target": "Task.Subject", "role": "writes", "value": "Other text"}))
    assert none["values_not_fixed"] == [] and none["values_unknown"] == []
    assert none["writes_other_values"] == ["flow:Fake_Daily_Reminder (Fake reminder)"]
    assert none["note"] == "no flow component writes Task.Subject with the value Other text"
    assert _ok(ct.call("find_usages", {"target": "Task.Subject", "value": "fake REMINDER"}))["count"] == 1
    effects = [{"kind": "update", "object": "A__c", "sets": {"F__c": {"literal": ""}}},
               {"kind": "create", "object": "A__c", "sets": {"F__c": {"any_of": [{"literal": None}, {"literal": True},
                                                                               {"computed": "formula:fX"}]}}},
               {"kind": "update", "object": "B__c", "sets": {"F__c": {"variable": "v"}}}]
    assert value_sources(effects, "A__c", "F__c") == (["blank", "true"], ["computed: formula:fX"])


def test_find_usages_takes_object_labels_and_tells_unknown_targets(tmp_path, ck):
    """Review L4: an object label resolves (Interviews -> Interview__c); an object no flow touches and an unknown
    name get different notes."""
    kns = [ObjectKnowledge.load(p) for p in knowledge_files(str(make_knowledge(tmp_path)))]
    reg = MultiToolRegistry(kns, components=ck)
    for typed in ("Interview", "interviews", "INTERVIEW__C"):
        res = _ok(reg.call("find_usages", {"target": typed, "role": "triggered_on"}))
        assert res["target"] == "Interview__c" and res["count"] == 2, typed
        assert ("resolved_from" in res) == (typed.lower() != "interview__c"), typed
    res = _ok(reg.call("find_usages", {"target": "Interview.Status__c", "role": "writes"}))
    assert res["target"] == "Interview__c.Status__c" and res["resolved_from"]["matched"] == "object label"
    assert _ok(reg.call("find_usages", {"target": "Session"}))["note"] == "no flow component acts on Session__c"
    unknown = _ok(reg.call("find_usages", {"target": "Spaceship"}))
    assert unknown["count"] == 0 and unknown["note"].startswith("unknown target Spaceship")
    plain = ComponentTools(ck)                       # no object knowledge: the API name read loosely still resolves
    res = _ok(plain.call("find_usages", {"target": "interview", "role": "triggered_on"}))
    assert res["target"] == "Interview__c" and res["resolved_from"]["matched"] == "object loose"


def test_called_by_and_launch_points(ct):
    """Review L1: the start names the flows that run a subflow (index called_by) and says what is not scanned."""
    s = _ok(ct.call("get_component", {"name": "Fake_Status_Setter"}))
    assert s["start"]["called_by"] == ["flow:Fake_Outcome_Sync"]
    assert s["start"]["launched_from"].startswith("Apex callers and launch points")
    assert s["start"]["launched_from"].endswith("called_by lists the flows that run it as a subflow")
    logic = ct.pages(ct.knowledge.get("flow:Fake_Status_Setter"), "logic")[0]["content"]
    assert logic["start"][0]["called_by"] == ["flow:Fake_Outcome_Sync"]
    assert "launched_from" not in _ok(ct.call("get_component", {"name": "Fake_Outcome_Sync"}))["start"]


def test_left_out_names_resolve_first_and_point_to_close_built_flows(ck, ct):
    """Review P3: an excluded or not-built flow named by its id or API name is never another flow's alias; review
    L2: a left-out name close to a built flow's says so (did_you_mean)."""
    docs = {f"flows/{c.file.rsplit('/', 1)[-1]}": json.loads(json.dumps(c.doc)) for c in ck.components.values()}
    docs["flows/Fake_Outcome_Sync.json"]["aliases"].append("Fake Not Built")
    ck2 = ComponentKnowledge(ck.index, docs)
    for typed in ("flow:Fake_Not_Built", "Fake_Not_Built", "FLOW:fake_not_built"):
        c, how = ck2.resolve(typed)
        assert c is None and how["left_out"]["component"] == "flow:Fake_Not_Built", typed
    c, how = ck2.resolve("fake not built")             # words: the alias of a built flow
    assert c is not None and c.cid == "flow:Fake_Outcome_Sync" and how == {"matched": "alias"}
    c, how = ck.resolve("Fake Daily")
    assert c is None and how["left_out"]["status"] == "not built" and how["did_you_mean"][0] == "flow:Fake_Daily_Reminder"
    left = _ok(ct.call("get_component", {"name": "Fake Daily"}))
    assert left["built"] is False and left["did_you_mean"][0].startswith("flow:Fake_Daily_Reminder (")
    assert "did_you_mean lists built flows" in left["note"]
    for ref in list(ck.index["excluded"]) + list(ck.index["not_built"]):
        for typed in (ref, ref.split(":", 1)[1]):
            assert ck.resolve(typed)[1]["left_out"]["component"] == ref, typed


def test_summary_shortens_the_least_needed_first_and_keeps_recipients(ck):
    """Review M1: dev10 notes, the meaning, the caveats and the lists give way before the effect lines, and folded
    effects still name their recipients."""
    docs = {f"flows/{c.file.rsplit('/', 1)[-1]}": json.loads(json.dumps(c.doc)) for c in ck.components.values()}
    d = docs["flows/Fake_Outcome_Sync.json"]
    mail = {"kind": "email", "element": "Mail_X", "path": "immediate", "when": ["Decide=Has_Outcome"],
            "recipients": [{"as": "to", "from": "Recruiter__c.Email__c", "hops": ["Interview__c.Recruiter__c"]}],
            "on_fault": "none"}
    d["flow"]["effects"] = d["flow"]["effects"][:1] + [{**mail, "element": f"Mail_{i}"} for i in range(9)]
    d["business_meaning"] = "A long synthetic meaning sentence for the summary order test. " * 25
    d["caveats"] = ["A production caveat that is long enough to matter. " * 6 for _ in range(4)] + \
        ["dev10 (fake): a long dev10 note that the summary should give up first. " * 5 for _ in range(3)]
    c = ComponentKnowledge(ck.index, docs).get("flow:Fake_Outcome_Sync")
    s = ComponentTools(ComponentKnowledge(ck.index, docs)).summary(c)
    assert _size(s) <= SUMMARY_CHARS
    assert s["dev10_notes"] == 3 and s["dev10_notes_note"].startswith("dev10 notes (not production)")
    assert s["business_meaning"].endswith("(in full: detail=caveats)")
    assert isinstance(s["effects"], list) and len(s["effects"]) == 10                 # one line per effect kept
    assert all("Recruiter__c.Email__c" in line for line in s["effects"][1:])
    big = [{**mail, "element": f"Mail_{i}", "when": [f"Route_{i}=Yes AND " + "x" * 120]} for i in range(80)]
    d["flow"]["effects"] = big
    c = ComponentKnowledge(ck.index, docs).get("flow:Fake_Outcome_Sync")
    s = ComponentTools(ComponentKnowledge(ck.index, docs)).summary(c)
    assert _size(s) <= SUMMARY_CHARS and s["effects_note"].startswith("80 effects in one line")
    assert "sends an e-mail to Recruiter__c.Email__c via Interview__c.Recruiter__c (80)" in s["effects"][0]
    assert isinstance(s["caveats"], list) and s["caveats"]                             # caveats kept once folded


def test_effects_detail_is_one_line_per_effect(ct, ck):
    for cid in ck.ids():
        c = ck.get(cid)
        pages = ct.pages(c, "effects")
        lines = [x for p in pages for x in p["content"].get("effects", [])]
        assert len(lines) == len(c.effects) and all(_size(p) <= PAGE_CHARS for p in pages), cid
    lines = ct.pages(ck.get("flow:Fake_Outcome_Sync"), "effects")[0]["content"]["effects"]
    assert lines[0].startswith("Close_It: updates the triggering Interview__c (Status__c = Closed, Notes__c)")
    assert lines[1].startswith("Mail_Owner: sends an e-mail to User.Email via Interview__c.OwnerId [path Async_Path]")
    s = _ok(ct.call("get_component", {"name": "Fake_Outcome_Sync"}))
    assert list(s["more"]) == ["effects", "logic", "fields", "caveats"]


def test_search_more_counts_only_close_hits(ct):
    """Review L3: 'more' counts the other hits that score at least MORE_SCORE_SHARE of the best one."""
    for q in ("fake", "interview outcome closer", "daily nudge"):
        res = _ok(ct.call("search_components", {"query": q, "k": 2}))
        hits = ct.index.search(q, k=12, section="flow")
        close = [h for h in hits[res["count"]:] if h["score"] >= MORE_SCORE_SHARE * hits[0]["score"]]
        assert res.get("more", 0) == len(close), q
    assert "more" in _ok(ct.call("search_components", {"query": "fake", "k": 2}))


def test_load_refuses_private_values_in_a_file(tmp_path):
    """Review P4: a component file whose public view holds an e-mail address, a URL or a record Id does not load
    (the message gives counts, never the value)."""
    d = make_components(tmp_path / "c", fillers=1)
    f = d / "flows" / "Fake_Outcome_Sync.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    for planted, kind in (("someone@example.org", "e-mail address"), ("https://example.org/x", "URL"),
                          ("a0B5g00000AbCdE", "record Id")):
        bad = json.loads(json.dumps(doc))
        bad["caveats"].append(f"planted {planted} here")
        f.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ComponentError) as exc:
            ComponentKnowledge.load(d)
        assert f"1 {kind}-like value(s)" in str(exc.value) and planted not in str(exc.value), kind
    bad = json.loads(json.dumps(doc))
    bad["evidence"].append({"source": "someone@example.org", "locator": "https://example.org"})   # evidence: stripped
    f.write_text(json.dumps(bad), encoding="utf-8")
    assert ComponentKnowledge.load(d).counts() == {"flow": 6}


# ------------------------------------------------------------------------------------------------- evidence
def test_planted_evidence_never_reaches_any_output_or_the_index(ct):
    outs = every_output(ct)
    text = json.dumps(outs, ensure_ascii=False)
    assert len(outs) > 300
    assert not any(m.lower() in text.lower() for m in EVIDENCE_MARKERS)
    assert '"evidence"' not in text and "locator" not in text
    for m in EVIDENCE_MARKERS:   # the search index never holds it: nothing matches the planted words
        res = _ok(ct.call("search_components", {"query": f"{m} {m.lower()} source locator"}))
        assert res["count"] == 0 and m not in json.dumps({k: v for k, v in res.items() if k != "query"})
    assert not any(m.lower() in " ".join(" ".join(d) for d in ct.index._docs) for m in EVIDENCE_MARKERS)


# ------------------------------------------------------------------------------------------------- registry
def test_several_object_registry_appends_the_component_tools(tmp_path, ck):
    kns = [ObjectKnowledge.load(p) for p in knowledge_files(str(make_knowledge(tmp_path)))]
    plain, with_c = MultiToolRegistry(kns), MultiToolRegistry(kns, components=ck)
    assert plain.components is None and with_c.components is not None
    assert with_c.list_tools()[: len(plain.list_tools())] == plain.list_tools()          # the list before is unchanged
    assert [t["name"] for t in with_c.list_tools()[len(plain.list_tools()):]] == list(NAMES)
    assert with_c.list_tools()[-3:] == R.component_tool_list()
    assert with_c.list_tools() == R.multi_tool_list(sorted(with_c.objects), kns[0].api_name,
                                                    with_c.registries[kns[0].api_name].index.sections(), True)
    assert [t["function"]["name"] for t in with_c.openai_tools()][-3:] == list(NAMES)
    out = with_c.call("get_component", {"name": "Fake_Outcome_Sync"})
    assert out["ok"] and out["chunk_ids"] == ["component::flow:Fake_Outcome_Sync"]
    assert plain.call("get_component", {"name": "Fake_Outcome_Sync"})["ok"] is False      # unknown without components
    for name in ("list_objects", "describe_object"):                                   # object tools: the same results
        args = {} if name == "list_objects" else {"object": kns[0].api_name}
        assert with_c.call(name, dict(args)) == plain.call(name, dict(args))
    assert list(LeanRegistry(with_c).tools) == list(LeanRegistry(plain).tools)          # the lean step never sees them
    assert KP.knowledge_system(plain) == KP.MULTI_AGENT_SYSTEM
    assert KP.knowledge_system(with_c) == KP.MULTI_AGENT_SYSTEM + KP.KNOWLEDGE_COMPONENTS_NOTE


def test_attach_resolver_reaches_the_component_index(tmp_path, ck):
    kns = [ObjectKnowledge.load(p) for p in knowledge_files(str(make_knowledge(tmp_path)))]
    reg = MultiToolRegistry(kns, components=ck)

    class Speller:   # a stand-in resolver: reads "outcom" as "outcome"
        def spell_correct(self, text):
            return []

        def resolve_term(self, tok, kinds=None, wide=False, **_k):
            return type("R", (), {"decision": "corrected" if tok == "outcom" else "none", "suggestion": "outcome",
                                  "suggestions": [], "candidates": [], "targets": [], "missing_words": ()})()

        def is_known_word(self, tok):
            return False

    reg.attach_resolver(Speller())
    res = reg.call("search_components", {"query": "outcom closer"})
    assert res["ok"] and res["result"]["read_as"] == {"outcom": "outcome"}
    reg.attach_resolver(None)
    assert reg.components.index.expand is None


def test_definitions_are_compact_and_name_their_handlers():
    defs = R.component_tool_defs()
    assert [d.name for d in defs] == list(NAMES)
    assert all(callable(getattr(ComponentTools, d.handler, None)) for d in defs)
    assert len(json.dumps(R.mcp_tool_list(defs))) <= 2000                               # A18: no object enums
    assert "enum" not in json.dumps([d.input_schema["properties"].get("target", {}) for d in defs])
    doc = R.tools_markdown()
    assert all(f"### `{n}`" in doc for n in NAMES) and "## Flow component tools" in doc


def test_component_limits_are_bound_at_import():
    assert {"component_search_chars", "component_summary_chars", "component_page_chars", "component_usage_rows",
            "component_usage_chars"} <= set(C.IMPORT_BOUND_LIMITS)
    assert (SEARCH_CHARS, SUMMARY_CHARS, PAGE_CHARS, USAGE_ROWS, USAGE_CHARS) == (4000, 3500, 6000, 40, 9000)


# ------------------------------------------------------------------------------------------------- pipeline, prompt
def test_pipeline_build_loads_components_for_several_files_only(tmp_path, comp_dir):
    paths = knowledge_files(str(make_knowledge(tmp_path / "kn")))
    s = C.load_settings(records=False, components_dir=str(comp_dir))
    _cfg, _o, reg, system, _llm = pipeline.build(paths, s)
    assert reg.components is not None and reg.components.knowledge.folder == str(comp_dir)
    assert system == KP.MULTI_AGENT_SYSTEM + KP.KNOWLEDGE_COMPONENTS_NOTE and list(reg.tools)[-3:] == list(NAMES)
    _cfg, _o, reg, system, _llm = pipeline.build(paths, C.load_settings(records=False,
                                                                        components_dir=str(tmp_path / "none")))
    assert reg.components is None and system == KP.MULTI_AGENT_SYSTEM and "find_usages" not in reg.tools
    _cfg, _o, one, system, _llm = pipeline.build(paths[:1], s)
    assert system is None and not hasattr(one, "components") and "get_component" not in one.tools
    broken = make_components(tmp_path / "broken", fillers=1)
    (broken / "flows" / "Fake_Outcome_Sync.json").write_text('{"kind": "flow"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="component knowledge in .* is invalid"):
        pipeline.build(paths, C.load_settings(records=False, components_dir=str(broken)))


def test_components_note_says_when_to_use_which_tool_and_that_evidence_is_never_cited():
    from sf_assistant import prompts as P

    p = P.load("knowledge_components_note")
    assert p.meta["name"] == "KNOWLEDGE_COMPONENTS_NOTE" and p.meta["module"] == "sf_assistant.agent.knowledge_prompts"
    text = KP.KNOWLEDGE_COMPONENTS_NOTE
    assert text == p.text and text.startswith("\n\n") and not P.TOKEN_RE.findall(text)
    for word in (*NAMES, "next_page", "dev10", "evidence", "never cited", "follow the", "not scanned", "triggered_on",
                 "values_not_fixed", "value_from", "documented", "facts_from", '"effects"'):
        assert word in text, word
    assert "KNOWLEDGE_COMPONENTS_NOTE" not in KP.__all__ and "KNOWLEDGE_COMPONENTS_NOTE" not in KP.TEMPLATES


# ------------------------------------------------------------------------------------------------- the real files
@real
def test_real_files_load_and_every_output_keeps_its_budget(real_ct):
    ck = real_ct.knowledge
    assert ck.validate() == [] and ck.counts()["flow"] >= 170 and ck.evidence_entries > 0
    for cid in ck.ids():
        assert _size(_ok(real_ct.call("get_component", {"name": cid}))) <= SUMMARY_CHARS, cid
        for detail in ("logic", "fields", "caveats"):
            assert all(_size(p) <= PAGE_CHARS for p in real_ct.pages(ck.get(cid), detail)), (cid, detail)
    for obj in ck.index["by_object"]:
        for role in R.USAGE_ROLES:
            res = _ok(real_ct.call("find_usages", {"target": obj, "role": role}))
            assert _size(res) <= USAGE_CHARS and len(res["rows"]) <= USAGE_ROWS, (obj, role)
    for q in ("how is the zoom meeting created for a session", "who gets notified when a background check is pending",
              "reminder", "interview", "what happens when a candidate training is dropped"):
        assert _size(_ok(real_ct.call("search_components", {"query": q, "k": 10}))) <= SEARCH_CHARS, q


@real
def test_real_files_paging_on_the_largest_files_loses_nothing(real_ct):
    ck = real_ct.knowledge
    largest = sorted(ck.ids(), key=lambda cid: -_size(ck.get(cid).doc))[:4]
    for cid in largest:
        c = ck.get(cid)
        logic = json.dumps([p["content"] for p in real_ct.pages(c, "logic")], ensure_ascii=False)
        fields = json.dumps([p["content"] for p in real_ct.pages(c, "fields")], ensure_ascii=False)
        assert len(real_ct.pages(c, "logic")) > 1 and "[cut:" not in logic
        assert all(json.dumps(el["api_name"]) in logic for el in c.doc["flow"].get("elements") or [])
        assert all(json.dumps(w, ensure_ascii=False) in logic for e in c.effects for w in e.get("when") or [])
        assert all(json.dumps(f["field"]) in fields for f in c.doc.get("fields") or [])


@pytest.fixture(scope="module")
def real_outputs(real_ct):
    return every_output(real_ct, queries=("zoom meeting", "background check", "deliverable approved",
                                          "solution document", "handbook", "decision sheet"))


def _evidence_sources() -> set[str]:
    sources = set()
    for p in sorted((REAL / "flows").glob("*.json")):
        for e in json.loads(p.read_text(encoding="utf-8")).get("evidence") or []:
            sources.add(str(e.get("source") or ""))
    return sources


@real
def test_real_files_evidence_never_in_any_output(real_outputs):
    sources = {s for s in _evidence_sources() if re.search(r"\.(txt|md|html|json)$", s) or s.startswith("xml:")}
    assert len(sources) > 10
    text = json.dumps(real_outputs, ensure_ascii=False)
    assert '"evidence"' not in text and '"locator"' not in text
    assert not [s for s in sources if s in text]


@real
def test_real_files_name_no_source_document_in_any_output_or_chunk(real_ct, real_outputs):
    """Review P1: the document titles of the evidence sources (the NN_ prefix, the date and the extension removed,
    _ read as a space) and the document types (a handbook, the Solution Document, the KB, ...) are in no output and in
    no search chunk. The registry's feature names are the exception (one carries a ticket key, as in the object
    knowledge); they are taken out of the text first."""
    if not (ROOT / "kb_build" / "flows").is_dir():
        pytest.skip("needs kb_build/flows, which stays out of this repository")
    sys.path.insert(0, str(ROOT / "kb_build" / "flows"))
    import flowlib as L

    titles = set(L.source_titles())
    for src in _evidence_sources():
        t = re.sub(r"\.(txt|md|html|json|pdf)$", "", src.rsplit("/", 1)[-1])
        t = " ".join(re.sub(r"_?\d{4}-\d{2}(?:-\d{2})?", "", re.sub(r"^\d+_", "", t)).replace("_", " ").split()).lower()
        if len(t) >= 8 and ":" not in src:
            titles.add(t)
    assert len(titles) > 10
    features = sorted({str(f.get("name")) for c in real_ct.knowledge.components.values()
                       for f in c.doc.get("features") or [] if isinstance(f, dict) and f.get("name")}, key=len,
                      reverse=True)
    def clean(text: str) -> str:
        for f in features:
            text = text.replace(json.dumps(f, ensure_ascii=False)[1:-1], "")
        return text

    hits = {}
    for out in real_outputs:   # a search result echoes the query (and read_as its words): left out of the scan
        res = out.get("result")
        res = {k: v for k, v in res.items() if k not in ("query", "read_as")} if isinstance(res, dict) else res
        text = clean(json.dumps(res, ensure_ascii=False) + " " + str(out.get("error") or ""))
        for h in L.attribution_hits(text, sorted(titles)):
            hits[h] = hits.get(h, 0) + 1
    assert hits == {}, hits
    chunks = clean(" ".join(json.dumps(c, ensure_ascii=False) for c in real_ct.knowledge.chunks()))
    assert L.attribution_hits(chunks, sorted(titles)) == []


@real
def test_real_files_find_the_documented_flows():
    tools = ComponentTools(ComponentKnowledge.load(REAL))
    for q, want in (("how is the zoom meeting created for a session", "flow:Session_Scheduling_Callout_Flow"),
                    ("who gets notified when a background check is pending", "flow:BG_Check_Pending_Notifications"),
                    ("what happens when a candidate training is dropped", "flow:Candidate_Training_Dropped_Cascade")):
        top = [r["component"] for r in _ok(tools.call("search_components", {"query": q}))["results"][:3]]
        assert want in top, (q, top)
    writes = _ok(tools.call("find_usages", {"target": "Session__c.Status__c", "role": "writes", "value": "Scheduled"}))
    assert "flow:Session_Scheduling_Callout_Flow" in [r.get("flow") for r in writes["rows"]]
