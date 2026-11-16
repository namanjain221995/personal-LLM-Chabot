"""The term resolver's wiring, step (3) on the knowledge side (docs/resolver_integration.md): misspelt object and field
arguments of the knowledge tools.

With the spelling help on (``registry.resolver`` set), an ``object`` argument (the several-object tools) or a field
argument (``get_field``, ``get_picklist_values``, ``get_validation_rules``, ``get_automation``) that no exact tier knows
is read by the knowledge resolver: one ``corrected`` reading is used inside a tool call (the correction channel
``ARGUMENT_READ_AS`` is set then) and stated in ``result["read_as"]`` (``read_as_arguments`` next to a search's own
``read_as`` dict); the knowledge agent's trace keeps that list and the answer's "Read as" line states it. An
``ambiguous`` reading, an ``exact`` one the tiers missed and a ``corrected`` one outside a tool call only give a
did-you-mean in today's error; another object's exact field API name is never read as a field of this one;
``find_links_to_object`` stays exact. With the switch off (``SFA_RESOLVER=off``) every result is the old one (the first
tests here; the parity replay P3 compares every recorded knowledge call with OLD). Every test sets the switch itself.
Fake data only: the made-up knowledge below and a made-up word list.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sf_assistant import config as C
from sf_assistant import prompts as P
from sf_assistant.agent import knowledge_agent as KA
from sf_assistant.config import ResolverConfig
from sf_assistant.knowledge.resolver import Candidate, Resolution
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.knowledge.terms import knowledge_resolver
from sf_assistant.tools import knowledge_tools as KT
from sf_assistant.tools.knowledge_tools import ARGUMENT_READ_AS, MultiToolRegistry, ToolError, ToolRegistry


# ------------------------------------------------------------------------------------------------ fake knowledge
def _field(api: str, label: str, typ: str, meaning: str, values: list | None = None,
           targets: list | None = None) -> dict:
    f = {"api_name": api, "label": label, "type": typ, "business_meaning": meaning, "required": False,
         "calculated": False, "semantic": {"concept": meaning[:40], "roles": []}, "picklist": None, "reference": None}
    if values is not None:
        f["picklist"] = {"restricted": True, "values": [{"label": v, "value": v} for v in values]}
    if targets is not None:
        f["reference"] = {"target_objects": targets, "relationship_name": api.replace("__c", "s"),
                          "relationship_type": "lookup", "business_role": label.lower(), "business_meaning": meaning}
    return f


def _obj(api: str, label: str, plural: str, fields: list, *, rules: tuple = (), flows: tuple = (),
         relationships: tuple = ()) -> dict:
    return {"object": {"api_name": api, "label": label, "plural_label": plural, "custom": True,
                       "description": f"{label}: one {label.lower()} per record.", "business_domain": ["Synthetic"],
                       "record_grain": {"description": f"one {label.lower()}"},
                       "discovery_summary": {"one_line": f"{api} holds one {label.lower()} per record.",
                                             "key_concepts": [label.lower()], "key_relationships": []}},
            "record_types": [], "fields": fields, "relationships": list(relationships), "child_relationships": [],
            "field_dependencies": [], "temporal_semantics": {}, "validation_rules": list(rules),
            "automation": {"flows": list(flows), "apex": []}, "business_process_usage": [],
            "provenance": {"source_environment": "synthetic"}}


DOCS = [
    _obj("Gadget__c", "Gadget", "Gadgets", [
        _field("Name", "Gadget Name", "string", "Name of the gadget."),
        _field("Status__c", "Status", "picklist", "Lifecycle status of the gadget.", values=["Draft", "Active", "Retired"]),
        _field("Colour_Code__c", "Colour Code", "string", "Colour code printed on the gadget."),
        _field("Color_Code__c", "Color Code", "string", "Older colour code field."),
        _field("Gizmo__c", "Gizmo", "reference", "The gizmo this gadget belongs to.", targets=["Gizmo__c"]),
        _field("Launch_Date__c", "Launch Date", "date", "Day the gadget was launched."),
    ], rules=({"name": "Launch_Date_Required", "fields_involved": ["Launch_Date__c", "Status__c"],
               "condition_summary": "an active gadget needs a launch date", "error_meaning": "set the launch date"},),
       flows=({"name": "Gadget_Status_Update", "type": "record_triggered", "trigger": "after update",
               "fields_read": ["Status__c"], "fields_written": ["Launch_Date__c"], "business_process": None},),
       relationships=({"field": "Gizmo__c", "type": "lookup", "target_objects": ["Gizmo__c"],
                       "relationship_name": "Gizmos", "business_meaning": "The gizmo this gadget belongs to."},)),
    _obj("Gizmo__c", "Gizmo", "Gizmos", [
        _field("Name", "Gizmo Name", "string", "Name of the gizmo."),
        _field("Widget_Count__c", "Widget Count", "number", "How many widgets the gizmo holds."),
        _field("Approval_State__c", "Approval State", "picklist", "Approval state.", values=["Pending", "Approved"]),
    ]),
    _obj("Sprocket__c", "Sprocket", "Sprockets", [
        _field("Name", "Sprocket Name", "string", "Name of the sprocket."),
        _field("Widget_Kount__c", "Widget Kount", "number", "Widget tally of the sprocket."),
        _field("Tension_Level__c", "Tension Level", "number", "Tension of the sprocket."),
    ]),
]
OBJECTS = ["Gadget__c", "Gizmo__c", "Sprocket__c"]
#: A made-up stand-in for the English word list: generic words only (the knowledge's own words are known anyway).
WORDS = ("a about all and are by count did does for has have how is many me of records show the to what when where "
         "which who why with work mean means used list").split()


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    for name in C.RESOLVER_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


def _words(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / "words.txt"
    p.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def resolver(tmp_path_factory):
    r, lines = knowledge_resolver(copy.deepcopy(DOCS), [str(_words(tmp_path_factory.mktemp("words")))])
    assert r is not None, lines
    return r


def _multi(resolver=None) -> MultiToolRegistry:
    reg = MultiToolRegistry([ObjectKnowledge(copy.deepcopy(d)) for d in DOCS])
    if resolver is not None:
        reg.attach_resolver(resolver)
    return reg


def _single(api: str, resolver=None) -> ToolRegistry:
    kn = ObjectKnowledge(copy.deepcopy(next(d for d in DOCS if d["object"]["api_name"] == api)))
    reg = ToolRegistry(kn, KnowledgeIndex(kn.chunks()))
    if resolver is not None:
        reg.attach_resolver(resolver)
    return reg


def _entry(arg, typed, used, kind, target, how) -> dict:
    return {"arg": arg, "typed": typed, "used": used, "kind": kind, "target": target, "how": how}


class _Spy:
    """The real resolver, counting the object and field readings asked of it."""

    def __init__(self, real) -> None:
        self.real, self.asked = real, []

    def resolve_term(self, text, kinds=None, context_object=None, within=None, **kw):
        if kinds is not None and set(kinds) <= {"object", "field"}:
            self.asked.append((text, tuple(kinds), within))
        return self.real.resolve_term(text, kinds=kinds, context_object=context_object, within=within, **kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


class _Forced(_Spy):
    """The real resolver, with fixed readings for some ``(text, within)`` (the binding rules, apart from scoring)."""

    def __init__(self, real, forced: dict) -> None:
        super().__init__(real)
        self.forced = forced

    def resolve_term(self, text, kinds=None, context_object=None, within=None, **kw):
        hit = self.forced.get((text, within))
        return hit if hit is not None else super().resolve_term(text, kinds, context_object, within, **kw)


def _corrected(text: str, obj: str, field: str) -> Resolution:
    c = Candidate("field", f"{obj}.{field}", field, text.lower(), "fuzzy", 0.9, obj, "api")
    return Resolution(text, "corrected", field, (field,), (c,), "fuzzy", 0.9)


BAD_CALLS = [
    ("describe_object", {"object": "Spocket"}),
    ("describe_object", {"object": "Gadgets"}),
    ("get_field", {"object": "Gadgte__c", "name": "Lanch_Date__c"}),
    ("get_field", {"object": "Gadget__c", "name": "Lanch_Date__c"}),
    ("get_field", {"object": "Gadget__c", "name": "Colr_Code__c"}),
    ("get_field", {"object": "Sprocket__c", "name": "Widget_Count__c"}),
    ("get_picklist_values", {"object": "Gadget__c", "field": "Stauts__c"}),
    ("get_validation_rules", {"object": "Gadget__c", "field": "Stauts__c"}),
    ("get_automation", {"object": "Gadget__c", "field": "Lanch Date"}),
    ("search_knowledge", {"object": "Gadgte", "query": "launch"}),
    ("search_all_knowledge", {"query": "launch date", "object": "Gadgte"}),
    ("find_links_to_object", {"target": "Gizmoo__c"}),
]
GOOD_CALLS = [
    ("list_objects", {}),
    ("describe_object", {"object": "Sprocket__c"}),
    ("describe_object", {"object": "Gadget"}),
    ("get_field", {"object": "Gadget__c", "name": "Launch_Date__c"}),
    ("get_field", {"object": "Gadget__c", "name": "launch date"}),
    ("get_field", {"object": "Gadget__c", "name": "Gadget__c.Status__c"}),
    ("get_picklist_values", {"object": "gizmo", "field": "Approval State"}),
    ("get_validation_rules", {"object": "Gadget__c", "field": "Status__c"}),
    ("get_automation", {"object": "Gadget__c", "field": "Launch_Date__c", "kind": "flow"}),
    ("get_relationships", {"object": "Gadget__c"}),
    ("search_fields", {"object": "Gadget__c", "query": "launch"}),
    ("search_knowledge", {"object": "Gizmo__c", "query": "widgets"}),
    ("search_all_knowledge", {"query": "tension of the sprocket"}),
    ("find_links_to_object", {"target": "Gizmo__c"}),
]


# =============================================================================================== switch off
def test_switch_off_errors_are_todays_text():
    reg = _multi()
    assert reg.resolver is None and all(r.resolver is None for r in reg.registries.values())
    valid = f"valid objects: {OBJECTS}"
    expected = {
        ("describe_object", "Spocket"): f"unknown or missing object 'Spocket'; {valid}",
        ("describe_object", "Gadgets"): f"unknown or missing object 'Gadgets'; {valid}",
        ("get_field", "Gadgte__c"): f"unknown or missing object 'Gadgte__c'; {valid}",
        ("search_knowledge", "Gadgte"): f"unknown or missing object 'Gadgte'; {valid}",
        ("search_all_knowledge", "Gadgte"): f"unknown object 'Gadgte'; {valid}",
    }
    for (name, obj), err in expected.items():
        args = next(a for n, a in BAD_CALLS if n == name and a.get("object") == obj)
        assert reg.call(name, dict(args)) == {"ok": False, "result": None, "error": err, "chunk_ids": []}
    # a misspelt field: today's difflib / BM25 closest list, no did-you-mean first
    for name, arg, typed in (("get_field", "name", "Lanch_Date__c"), ("get_picklist_values", "field", "Stauts__c"),
                             ("get_validation_rules", "field", "Stauts__c"), ("get_automation", "field", "Lanch Date")):
        out = reg.call(name, {"object": "Gadget__c", arg: typed})
        closest = reg.registries["Gadget__c"]._suggest_fields(typed)
        assert out["error"] == f"no field named {typed}; closest: {closest}"
    out = reg.call("get_field", {"object": "Sprocket__c", "name": "Widget_Count__c"})
    assert out["error"] == "no field named Widget_Count__c; closest: ['Widget_Kount__c']"
    assert reg.call("get_field", {"object": "Gadget__c", "name": "Colr_Code__c"})["error"] == \
        "no field named Colr_Code__c; closest: ['Color_Code__c', 'Colour_Code__c', 'Launch_Date__c']"
    links = reg.call("find_links_to_object", {"target": "Gizmoo__c"})["result"]
    assert links["count"] == 0 and links["has_own_knowledge_file"] is False
    assert ARGUMENT_READ_AS.get() is None


def test_switch_off_nothing_is_read_and_nothing_is_stated():
    for reg in (_multi(), _single("Gadget__c")):
        for name, args in BAD_CALLS + GOOD_CALLS:
            if isinstance(reg, ToolRegistry):
                if args.get("object") not in (None, "Gadget__c") or name not in reg.tools:
                    continue
                args = {k: v for k, v in args.items() if k != "object"}
            out = reg.call(name, dict(args))
            res = out.get("result") if isinstance(out.get("result"), dict) else {}
            assert not isinstance(res.get("read_as"), list) and "read_as_arguments" not in res, (name, args)
            assert "read as" not in str(out.get("error") or "") and "did you mean" not in str(out.get("error") or "")
    assert ARGUMENT_READ_AS.get() is None


# =============================================================================================== switch on
def test_switch_on_exact_names_never_ask_the_resolver_and_equal_switch_off(resolver):
    spy = _Spy(resolver)
    on, off = _multi(spy), _multi()
    for name, args in GOOD_CALLS:
        assert on.call(name, dict(args)) == off.call(name, dict(args)), (name, args)
    assert spy.asked == []
    single_on, single_off = _single("Gadget__c", spy), _single("Gadget__c")
    for name, args in (("get_field", {"name": "Status__c"}), ("get_picklist_values", {"field": "status"}),
                       ("describe_object", {})):
        assert single_on.call(name, dict(args)) == single_off.call(name, dict(args))
    assert spy.asked == [] and ARGUMENT_READ_AS.get() is None


def test_switch_on_corrected_object_is_used_and_stated(resolver):
    reg = _multi(resolver)
    out = reg.call("describe_object", {"object": "Spocket"})
    assert out["ok"] and out["result"]["object"] == "Sprocket__c" and out["result"]["api_name"] == "Sprocket__c"
    assert out["result"]["read_as"] == [_entry("object", "Spocket", "Sprocket__c", "object", "Sprocket__c",
                                               "closest object name")]
    assert all(c.startswith("Sprocket__c::") for c in out["chunk_ids"]) and out["chunk_ids"]
    # the result is the exact name's result plus read_as
    exact = reg.call("describe_object", {"object": "Sprocket__c"})
    assert {k: v for k, v in out["result"].items() if k != "read_as"} == exact["result"]
    assert ARGUMENT_READ_AS.get() is None


def test_switch_on_corrected_field_is_used_and_stated_by_each_field_tool(resolver):
    reg = _multi(resolver)
    out = reg.call("get_field", {"object": "Gadget__c", "name": "Lanch_Date__c"})
    assert out["ok"] and out["result"]["api_name"] == "Launch_Date__c" and out["result"]["resolved_from"] == "Lanch_Date__c"
    assert out["result"]["read_as"] == [_entry("name", "Lanch_Date__c", "Launch_Date__c", "field",
                                               "Gadget__c.Launch_Date__c", "closest field name")]
    for name, arg, typed, used, key, value in (
            ("get_picklist_values", "field", "Stauts__c", "Status__c", "field", "Status__c"),
            ("get_validation_rules", "field", "Stauts__c", "Status__c", "filter_field", "Status__c"),
            ("get_automation", "field", "Lanch Date", "Launch_Date__c", "count", 1)):
        out = reg.call(name, {"object": "Gadget__c", arg: typed})
        assert out["ok"], out
        assert out["result"][key] == value
        assert out["result"]["read_as"] == [_entry(arg, typed, used, "field", f"Gadget__c.{used}",
                                                   "closest field name")]
    # the object and the field misspelt in one call: both stated, the object first
    out = reg.call("get_field", {"object": "Gadgte__c", "name": "Lanch_Date__c"})
    assert out["ok"] and out["result"]["object"] == "Gadget__c" and out["result"]["api_name"] == "Launch_Date__c"
    assert [(e["arg"], e["typed"], e["used"]) for e in out["result"]["read_as"]] == \
        [("object", "Gadgte__c", "Gadget__c"), ("name", "Lanch_Date__c", "Launch_Date__c")]
    json.dumps(out)
    assert ARGUMENT_READ_AS.get() is None


def test_switch_on_one_object_registry_states_its_field_reading(resolver):
    reg = _single("Gadget__c", resolver)
    out = reg.call("get_field", {"name": "Lanch_Date__c"})
    assert out["ok"] and out["result"]["api_name"] == "Launch_Date__c"
    assert out["result"]["read_as"] == [_entry("name", "Lanch_Date__c", "Launch_Date__c", "field",
                                               "Gadget__c.Launch_Date__c", "closest field name")]
    out = reg.call("get_picklist_values", json.dumps({"field": "Stauts__c"}))   # arguments as a JSON string
    assert out["ok"] and out["result"]["read_as"][0]["arg"] == "field"
    assert ARGUMENT_READ_AS.get() is None


def test_switch_on_ambiguous_or_exact_reading_is_a_did_you_mean_never_bound(resolver):
    reg = _multi(resolver)
    off = _multi()
    out = reg.call("get_field", {"object": "Gadget__c", "name": "Colr_Code__c"})       # two close fields
    assert not out["ok"] and out["error"].startswith("no field named Colr_Code__c; closest: ['Color_Code__c', "
                                                     "'Colour_Code__c'")
    out = reg.call("get_field", {"object": "Gadget__c", "name": "Launch_Dates__c"})     # exact for the resolver only
    assert not out["ok"] and out["error"].startswith("no field named Launch_Dates__c; closest: ['Launch_Date__c'")
    assert "read as" not in out["error"]
    out = reg.call("describe_object", {"object": "Gadgets"})                             # a plural: offered, not used
    assert out == {"ok": False, "result": None, "chunk_ids": [],
                   "error": f"unknown or missing object 'Gadgets' (did you mean Gadget__c (Gadget)?); valid objects: "
                            f"{OBJECTS}"}
    old = off.call("describe_object", {"object": "Gadgets"})["error"]
    assert old.split(";")[0] in out["error"] and old.split("; ", 1)[1] in out["error"]   # today's text kept
    # a name the resolver cannot read keeps today's error exactly
    for name, args in (("describe_object", {"object": "Zzyzx__c"}),
                       ("get_field", {"object": "Gadget__c", "name": "Qqq_Zzz__c"})):
        assert reg.call(name, dict(args)) == off.call(name, dict(args))
    assert ARGUMENT_READ_AS.get() is None


def test_switch_on_outside_a_tool_call_a_corrected_reading_is_only_suggested(resolver):
    reg = _multi(resolver)
    sub = reg.registries["Gadget__c"]
    with pytest.raises(ToolError) as e:
        sub._resolve_field("Lanch_Date__c")                       # a handler called directly: no channel
    assert str(e.value).startswith("no field named Lanch_Date__c; closest: ['Launch_Date__c'")
    assert reg._close_object("Spocket") == (None, ["Sprocket__c"])
    assert reg._close_object("") == (None, []) and reg._close_object(None) == (None, [])
    token = ARGUMENT_READ_AS.set([])                              # inside a call: used, and recorded
    try:
        assert reg._close_object("Spocket") == ("Sprocket__c", [])
        assert sub._resolve_field("Lanch_Date__c")["api_name"] == "Launch_Date__c"
        assert [e["typed"] for e in ARGUMENT_READ_AS.get()] == ["Spocket", "Lanch_Date__c"]
    finally:
        ARGUMENT_READ_AS.reset(token)
    assert ARGUMENT_READ_AS.get() is None


def test_switch_on_another_objects_exact_field_name_is_never_bound(resolver):
    """A forced ``corrected`` reading of Gizmo__c's exact field API name among Sprocket__c's fields is not used (the
    agent picked the wrong object): today's error, its closest list led by the resolver's names. The same forced
    reading of a real typo is used."""
    forced = _Forced(resolver, {("Widget_Count__c", "Sprocket__c."): _corrected("Widget_Count__c", "Sprocket__c",
                                                                                  "Widget_Kount__c"),
                                ("Widgit_Kount__c", "Sprocket__c."): _corrected("Widgit_Kount__c", "Sprocket__c",
                                                                                  "Widget_Kount__c")})
    reg = _multi(forced)
    out = reg.call("get_field", {"object": "Sprocket__c", "name": "Widget_Count__c"})
    assert out == {"ok": False, "result": None, "chunk_ids": [],
                   "error": "no field named Widget_Count__c; closest: ['Widget_Kount__c']"}
    assert reg.registries["Sprocket__c"]._fields_elsewhere("Widget_Count__c") == ["Gizmo__c"]
    out = reg.call("get_field", {"object": "Sprocket__c", "name": "Widgit_Kount__c"})
    assert out["ok"] and out["result"]["api_name"] == "Widget_Kount__c" and out["result"]["read_as"][0]["arg"] == "name"
    # the real resolver: another object's field name keeps today's error exactly
    for name, arg in (("get_field", "name"), ("get_picklist_values", "field")):
        args = {"object": "Sprocket__c", arg: "Launch_Date__c"}
        assert _multi(resolver).call(name, dict(args)) == _multi().call(name, dict(args))


def test_switch_on_search_tools_keep_their_query_reading_apart(resolver):
    reg = _multi(resolver)
    obj_entry = _entry("object", "Gadgte", "Gadget__c", "object", "Gadget__c", "closest object name")
    out = reg.call("search_knowledge", {"object": "Gadgte", "query": "lanch"})          # both kinds of reading
    res = out["result"]
    assert out["ok"] and res["object"] == "Gadget__c"
    assert isinstance(res["read_as"], dict) and res["read_as"]                           # the query expansion
    assert res["read_as_arguments"] == [obj_entry]
    out = reg.call("search_knowledge", {"object": "Gadgte", "query": "launch"})         # the argument only
    assert out["ok"] and out["result"]["read_as"] == [obj_entry] and "read_as_arguments" not in out["result"]
    out = reg.call("search_all_knowledge", {"query": "launch date", "object": "Gadgte"})
    assert out["ok"] and out["result"]["read_as"] == [obj_entry]
    assert all(r["object"] == "Gadget__c" for r in out["result"]["results"])
    out = reg.call("search_all_knowledge", {"query": "lanch date", "object": "Gadgte"})
    assert isinstance(out["result"]["read_as"], dict) and out["result"]["read_as_arguments"] == [obj_entry]


def test_switch_on_failed_call_states_the_object_reading_in_its_error(resolver):
    out = _multi(resolver).call("get_field", {"object": "Gadgte", "name": "Qqq_Zzz__c"})
    assert not out["ok"] and out["result"] is None
    assert out["error"].startswith("object 'Gadgte' read as 'Gadget__c' (closest object name): no field named "
                                   "Qqq_Zzz__c; closest: ")


def test_switch_on_find_links_to_object_stays_exact(resolver):
    on, off = _multi(resolver), _multi()
    for target in ("Gizmoo__c", "Gizmo__c", "gizmo"):
        a, b = on.call("find_links_to_object", {"target": target}), off.call("find_links_to_object", {"target": target})
        assert a == b and "read_as" not in a["result"]
    assert on.call("find_links_to_object", {"target": "Gizmo__c"})["result"]["count"] == 1
    assert on.call("find_links_to_object", {"target": "Gizmoo__c"})["result"]["has_own_knowledge_file"] is False


def test_switch_on_trace_and_read_as_line_state_a_corrected_knowledge_argument(resolver):
    from sf_assistant import pipeline

    reg = _multi(resolver)
    payload, record = KA._execute_tool("get_field", {"object": "Gadgte__c", "name": "Lanch_Date__c"}, reg)
    assert payload["ok"] and [e["typed"] for e in record["read_as"]] == ["Gadgte__c", "Lanch_Date__c"]
    sp = KA.QuestionSpelling("when was the gadget launched")
    text = "Launch_Date__c is the day the gadget was launched.\nSource: Gadget__c knowledge"
    final = pipeline.spelling_final(sp, text, "knowledge", r=SimpleNamespace(tool_calls=[record]))
    line = P.fill(KT.READ_AS_LINE, PAIRS="'Gadgte__c' as Gadget__c; 'Lanch_Date__c' as Launch_Date__c")
    assert final == "Launch_Date__c is the day the gadget was launched.\n" + line + "\nSource: Gadget__c knowledge"
    # a search's own read_as (a dict) is no argument reading; its read_as_arguments are not stated (documented)
    _p, rec2 = KA._execute_tool("search_knowledge", {"object": "Gadgte", "query": "lanch"}, reg)
    assert "read_as" not in rec2
    assert pipeline.spelling_final(sp, text, "knowledge", r=SimpleNamespace(tool_calls=[rec2])) == text
    # switch off: no reading, no line
    _p, rec3 = KA._execute_tool("get_field", {"object": "Gadget__c", "name": "Launch_Date__c"}, _multi())
    assert "read_as" not in rec3
    assert pipeline.spelling_final(sp, text, "knowledge", r=SimpleNamespace(tool_calls=[rec3])) == text


def test_pipeline_build_switch_on_reads_misspelt_arguments(tmp_path):
    from sf_assistant import pipeline

    folder = tmp_path / "knowledge"
    folder.mkdir()
    for d in DOCS:
        (folder / f"{d['object']['api_name']}.json").write_text(json.dumps(d), encoding="utf-8")
    paths = sorted(str(p) for p in folder.glob("*.json"))
    for on in (False, True):
        settings = dataclasses.replace(C.load_settings(records=False),
                                       resolver=ResolverConfig(enabled=on, lexicon=(str(_words(tmp_path)),)))
        multi = pipeline.build(paths, settings)[2]
        single = pipeline.build([paths[0]], settings)[2]
        assert (multi.resolver is not None) is on and (single.resolver is not None) is on
        out = multi.call("describe_object", {"object": "Spocket"})
        assert out["ok"] is on
        out = single.call("get_field", {"name": "Lanch_Date__c"})
        assert out["ok"] is on
        if on:
            assert out["result"]["read_as"][0]["used"] == "Launch_Date__c"
        else:
            assert out["error"].startswith("no field named Lanch_Date__c; closest: ")
