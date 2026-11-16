"""Flow component build (kb_build/flows): parser facts on synthetic XML, masking, the schema file, and the separation
from the object build. Needs no source, no .private file and no knowledge file (clean checkout)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HOME = Path(__file__).resolve().parents[1]
FLOWS = HOME / "kb_build" / "flows"
if not FLOWS.is_dir():
    pytest.skip("needs kb_build/flows, which stays out of this repository", allow_module_level=True)
sys.path.insert(0, str(FLOWS))

import flowlib as L  # noqa: E402
from flowxml import FlowFacts  # noqa: E402

NS = "http://soap.sforce.com/2006/04/metadata"


def _schema(fields=None, labels=None, rels=None):
    s = L.Schema.__new__(L.Schema)
    s.k_fields, s.k_files = {}, {}
    s.k_objects = dict(labels or {})
    s.db_fields = dict(fields or {})
    s.db_objects, s.db_rels = {}, dict(rels or {})
    s.ci = {(o.lower(), f.lower()): (o, f) for (o, f) in s.db_fields}
    s.obj_ci = {o.lower(): o for o in s.k_objects}
    return s


RULES = {"org_time_zone": "America/New_York", "show_time_zones": {"utc": "UTC", "ist": "Asia/Kolkata"}}
WITHHELD = {"fields": {"Thing__c.Secret_Field__c"}, "objects": set(), "rx": re.compile(r"(?!x)x")}


def _facts(tmp_path, body, schema=None, names=()):
    p = tmp_path / "F.flow-meta.xml"
    p.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow xmlns="{NS}">{body}</Flow>', encoding="utf-8")
    m = L.Masker(list(names))
    ff = FlowFacts(str(p), "F", schema or _schema(), m, RULES, WITHHELD)
    return ff, ff.generated(), m


RECORD_FLOW = """
<apiVersion>65.0</apiVersion><label>Thing roll-up</label><processType>AutoLaunchedFlow</processType>
<decisions><name>Which</name><label>Which</label><defaultConnectorLabel>Other</defaultConnectorLabel>
 <rules><name>Pass</name><conditionLogic>and</conditionLogic>
  <conditions><leftValueReference>$Record.Status__c</leftValueReference><operator>EqualTo</operator>
   <rightValue><stringValue>Pass</stringValue></rightValue></conditions>
  <connector><targetReference>Set_Parent</targetReference></connector><label>Pass</label></rules>
</decisions>
<recordUpdates><name>Set_Parent</name><label>Set Parent</label><filterLogic>and</filterLogic>
 <filters><field>Id</field><operator>EqualTo</operator><value><elementReference>$Record.Parent__c</elementReference></value></filters>
 <inputAssignments><field>Score__c</field><value><elementReference>$Record.Score__c</elementReference></value></inputAssignments>
 <inputAssignments><field>Done_At__c</field><value><elementReference>$Flow.CurrentDateTime</elementReference></value></inputAssignments>
 <inputAssignments><field>Secret_Field__c</field><value><stringValue>x</stringValue></value></inputAssignments>
 <inputAssignments><field>Status__c</field><value><stringValue>Approved</stringValue></value></inputAssignments>
 <object>Thing__c</object></recordUpdates>
<start><connector><targetReference>Which</targetReference></connector>
 <filterFormula>TEXT({!$Record.Status__c}) = "Pass"</filterFormula>
 <object>Child__c</object><recordTriggerType>CreateAndUpdate</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>
<status>Active</status>
"""


def test_record_triggered_facts(tmp_path):
    schema = _schema(fields={("Child__c", "Status__c"): {"data_type": "Picklist", "restricted_picklist": 1, "is_nullable": 1},
                             ("Child__c", "Score__c"): {"data_type": "Number", "label": "AI Score", "is_nullable": 1},
                             ("Child__c", "Parent__c"): {"data_type": "MasterDetail", "label": "Thing", "is_nullable": 0},
                             ("Thing__c", "Score__c"): {"data_type": "Text", "length": 20},
                             ("Thing__c", "Status__c"): {"data_type": "Picklist"}},
                     labels={"Child__c": "Thing Result", "Thing__c": "Thing"},
                     rels={("Child__c", "Parent__c"): [{"target_object": "Thing__c", "relationship_type": "MasterDetail"}]})
    ff, g, m = _facts(tmp_path, RECORD_FLOW, schema)
    st = g["flow"]["start"]
    assert st["type"] == "after_save" and st["events"] == ["create", "update"]
    assert st["entry"]["logic"] == "formula" and st["entry"]["conditions"] == []
    assert st["paths"] == [{"id": "immediate", "kind": "immediate"}] and st["next"] == "Which"
    ef = g["flow"]["effects"][0]
    assert ef["when"] == ["Which=Pass"] and ef["target"] == "by_filter" and ef["on_fault"] == "none"
    assert ef["where"] == ["Thing__c.Id EqualTo $Record.Parent__c (the result's master-detail parent)"]
    # sets: literal, computed, from (XML order inside a kind); the withheld field is never named
    assert list(ef["sets"]) == ["[withheld field 1]", "Status__c", "Done_At__c", "Score__c"]
    assert ef["sets"]["[withheld field 1]"] == {"literal": "[value not shown]"}
    assert ef["sets"]["Score__c"] == {"from": "$Record.Score__c",
                                      "note": "number stored as text (Text(20)); blank when the result has no score"}
    assert m.counts["withheld_fields"] >= 1
    assert "Secret_Field__c" not in json.dumps(g)
    ends = g["flow"]["ends_without_action"]
    assert ends == [{"outcome": "Which=default", "label": "Other", "reachable": False,
                     "why": "the entry formula admits only Pass, and Status__c is a restricted picklist"}]
    assert g["flow"]["run_context"]["effective"] == "system_context"
    assert [o["object"] for o in g["objects"]] == ["Child__c", "Thing__c"]
    status = next(f for f in g["fields"] if f["field"] == "Thing__c.Status__c")
    assert status["values_written"] == ["Thing__c.Status__c=Approved"]


SCHEDULED_FLOW = """
<apiVersion>65.0</apiVersion><label>Daily</label><processType>AutoLaunchedFlow</processType>
<start><schedule><frequency>Daily</frequency><startDate>2026-07-09</startDate><startTime>18:00:00.000Z</startTime></schedule>
 <triggerType>Scheduled</triggerType></start><status>Active</status>
"""


def test_scheduled_time_is_org_time(tmp_path):
    _, g, _ = _facts(tmp_path, SCHEDULED_FLOW)
    sc = g["flow"]["start"]["schedule"]
    assert sc["org_time"] == "18:00" and sc["org_time_zone"] == "America/New_York"
    assert sc["utc"] == {"edt": "22:00", "est": "23:00"}
    assert sc["ist"] == {"edt": "03:30 next day", "est": "04:30 next day"}
    assert sc["first_run"] == "2026-07-09 18:00 EDT = 2026-07-09 22:00 UTC = 2026-07-10 03:30 IST"
    assert g["flow"]["start"]["runs"] == "once" and g["flow"]["start"]["entry"] is None


def test_masker_masks_identifiers():
    base = "001" + "AA" + "0000012345"             # 15 characters: key prefix 001, digits and letters
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    suffix = "".join(alpha[sum(1 << j for j, ch in enumerate(base[i * 5:(i + 1) * 5]) if ch.isupper())] for i in range(3))
    assert L.id18_valid(base + suffix)
    m = L.Masker(["Zorblat"])
    out = m.text(f"mail a.b@example.org, see https://x.example/y, id {base + suffix}, ch C0ABCDEFGH1, ask Zorblat")
    assert "@" not in out and "https" not in out and base not in out and "C0ABCDEFGH1" not in out and "Zorblat" not in out
    assert m.counts["emails"] == 1 and m.counts["urls"] == 1 and m.counts["slack_ids"] == 1 and m.counts["person_names"] == 1
    assert not L.SECRET_RX.search("Validate_Token=Expired_Token")


def test_schema_file_is_closed():
    sch = json.loads((FLOWS / "flow_component.schema.json").read_text(encoding="utf-8"))
    assert sch["additionalProperties"] is False
    for name, d in sch["$defs"].items():
        if d.get("type") == "object":
            assert d.get("additionalProperties") is False or "additionalProperties" in d, name
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(sch)


def test_object_merge_never_reads_flow_curated_files(tmp_path, monkeypatch):
    """merge.py --all lists curated/*.json only, so a curated/flows/<Api>.json can never be taken for an object."""
    sys.path.insert(0, str(HOME / "kb_build"))
    import merge  # noqa: E402
    (tmp_path / "curated" / "flows").mkdir(parents=True)
    (tmp_path / "curated" / "flows" / "Some_Flow.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(merge, "HERE", str(tmp_path))
    monkeypatch.setattr(merge, "OUT_DIR", str(tmp_path / "out"))
    assert merge.main(["merge.py", "--all"]) == 0


def test_rules_and_scope_are_consistent():
    rules = L.strip_comments(json.loads((FLOWS / "flow_rules.json").read_text(encoding="utf-8")))
    scope = L.strip_comments(json.loads((FLOWS / "flow_scope.json").read_text(encoding="utf-8")))
    excluded = {f for r in rules["excluded"] for f in r["flows"]}
    assert not excluded & set(scope["flows"])
    assert len(scope["flows"]) == 174
    assert {v["facts_from"] for v in scope["flows"].values()} <= {s for s, v in scope["sources"].items()
                                                                  if v["environment"] == "production"}


def test_formula_literals_are_tokenised(tmp_path):
    """Code between two short string literals is never taken for a long literal (only real long literals are
    described); bare $Record references outside literals are read."""
    code = "ISPICKVAL({!$Record.Status__c}, \"No Show\"),\n    ISBLANK({!$Record.Owner__c}),\n    " \
           "ISPICKVAL({!$Record.Follow_Up_Status__c}, \"Completed\")"
    long_lit = "x" * 90
    body = RECORD_FLOW.replace('TEXT({!$Record.Status__c}) = "Pass"',
                               f'AND(OR({code}), $Record.Score__c > 1, TEXT($Record__Prior.Status__c) &lt;&gt; "{long_lit}")')
    ff, g, m = _facts(tmp_path, body)
    f = g["flow"]["start"]["entry"]["formula"]
    assert code in f and "[text of 90 characters]" in f and long_lit not in f
    assert m.counts["long_texts"] == 1
    assert next(x for x in g["fields"] if x["field"] == "Child__c.Status__c").get("prior") is True
    assert any(x["field"] == "Child__c.Score__c" for x in g["fields"])
    from flowxml import formula_refs, formula_tokens
    assert "".join(t for _, t in formula_tokens(body)) == body
    refs = formula_refs('IF($Record.A__c, "$Record.B__c", {!$Record.C__c}) /* $Record.D__c */')
    assert refs == ["$Record.C__c", "$Record.A__c"]


EMAIL_FLOW = """
<apiVersion>65.0</apiVersion><label>Mail</label><processType>AutoLaunchedFlow</processType>
<actionCalls><name>Mail_Owners</name><label>Mail Owners</label><actionName>emailSimple</actionName>
 <actionType>emailSimple</actionType>
 <inputParameters><name>recipientAddressList</name><value>
  <collectionElements><elementReference>$Record.Owner_Email__c</elementReference></collectionElements>
  <collectionElements><elementReference>$Record.Backup_Email__c</elementReference></collectionElements>
 </value></inputParameters>
 <inputParameters><name>recipientAddresses</name><value>
  <collectionElements><elementReference>$Record.Owner_Email__c</elementReference></collectionElements>
  <collectionElements><elementReference>$Record.Backup_Email__c</elementReference></collectionElements>
 </value></inputParameters>
 <inputParameters><name>senderAddress</name><value><stringValue>x</stringValue></value></inputParameters>
</actionCalls>
<start><connector><targetReference>Mail_Owners</targetReference></connector><object>Thing__c</object>
 <recordTriggerType>Create</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>
<status>Active</status>
"""


def test_every_collection_element_is_a_recipient(tmp_path):
    _, g, _ = _facts(tmp_path, EMAIL_FLOW)
    ef = g["flow"]["effects"][0]
    assert ef["kind"] == "email"
    assert [r.get("from") for r in ef["recipients"]] == ["Thing__c.Owner_Email__c", "Thing__c.Backup_Email__c"]
    assert ef["sender"] == "an org-wide sender address (not copied)"


BRANCH_FLOW = """
<apiVersion>65.0</apiVersion><label>Branches</label><processType>AutoLaunchedFlow</processType>
<variables><name>rec</name><dataType>SObject</dataType><isCollection>false</isCollection><isInput>false</isInput>
 <isOutput>false</isOutput><objectType>Thing__c</objectType></variables>
<decisions><name>Which</name><label>Which</label><defaultConnector><targetReference>Set_B</targetReference></defaultConnector>
 <defaultConnectorLabel>B</defaultConnectorLabel>
 <rules><name>A</name><conditionLogic>and</conditionLogic>
  <conditions><leftValueReference>$Record.Status__c</leftValueReference><operator>EqualTo</operator>
   <rightValue><stringValue>A</stringValue></rightValue></conditions>
  <connector><targetReference>Set_A</targetReference></connector><label>A</label></rules>
</decisions>
<assignments><name>Set_A</name><label>Set A</label>
 <assignmentItems><assignToReference>rec.Score__c</assignToReference><operator>Add</operator>
  <value><numberValue>1.0</numberValue></value></assignmentItems>
 <connector><targetReference>Save_A</targetReference></connector></assignments>
<assignments><name>Set_B</name><label>Set B</label>
 <assignmentItems><assignToReference>rec.Status__c</assignToReference><operator>Assign</operator>
  <value><stringValue>Done</stringValue></value></assignmentItems>
 <connector><targetReference>Save_B</targetReference></connector></assignments>
<recordUpdates><name>Save_A</name><label>Save A</label><inputReference>rec</inputReference></recordUpdates>
<recordUpdates><name>Save_B</name><label>Save B</label><inputReference>rec</inputReference></recordUpdates>
<customErrors><name>Stop_It</name><label>Stop</label>
 <customErrorMessages><errorMessage>no</errorMessage><isFieldError>false</isFieldError></customErrorMessages>
</customErrors>
<start><connector><targetReference>Which</targetReference></connector><object>Thing__c</object>
 <recordTriggerType>Update</recordTriggerType><triggerType>RecordAfterSave</triggerType>
 <scheduledPaths><connector><targetReference>Stop_It</targetReference></connector><pathType>AsyncAfterCommit</pathType>
 </scheduledPaths></start>
<status>Active</status>
"""


def test_sets_come_from_elements_before_the_save(tmp_path):
    """A record variable saved on two branches: each save lists only the values its own branch gives; an Add on a
    number field is a counter; the branch updates are alternatives for leaves_unchanged; a custom error on the
    asynchronous path does not block the save; the default outcome runs an effect, so nothing ends without action."""
    schema = _schema(fields={("Thing__c", "Score__c"): {"data_type": "Number"},
                             ("Thing__c", "Status__c"): {"data_type": "Picklist"}})
    _, g, _ = _facts(tmp_path, BRANCH_FLOW, schema)
    eff = {e["element"]: e for e in g["flow"]["effects"]}
    assert eff["Save_A"]["sets"] == {"Score__c": {"computed": "increment by 1"}}
    assert eff["Save_B"]["sets"] == {"Status__c": {"literal": "Done"}}
    assert eff["Save_A"]["leaves_unchanged"] == ["Status__c"] and eff["Save_B"]["leaves_unchanged"] == ["Score__c"]
    assert eff["Stop_It"]["kind"] == "fails_async_run"
    assert g["flow"]["ends_without_action"] == []


LOOP_FLOW = """
<apiVersion>65.0</apiVersion><label>Loop</label><processType>Flow</processType>
<recordLookups><name>Get_Rows</name><label>Get Rows</label><object>Thing__c</object>
 <getFirstRecordOnly>false</getFirstRecordOnly><storeOutputAutomatically>true</storeOutputAutomatically>
 <connector><targetReference>Each_Row</targetReference></connector></recordLookups>
<loops><name>Each_Row</name><label>Each Row</label><collectionReference>Get_Rows</collectionReference>
 <nextValueConnector><targetReference>Is_Dup</targetReference></nextValueConnector>
 <noMoreValuesConnector><targetReference>Make_One</targetReference></noMoreValuesConnector></loops>
<decisions><name>Is_Dup</name><label>Is Dup</label><defaultConnector><targetReference>Each_Row</targetReference></defaultConnector>
 <defaultConnectorLabel>No</defaultConnectorLabel>
 <rules><name>Dup</name><conditionLogic>and</conditionLogic>
  <conditions><leftValueReference>Each_Row.Status__c</leftValueReference><operator>EqualTo</operator>
   <rightValue><stringValue>A</stringValue></rightValue></conditions>
  <connector><targetReference>Dup_Screen</targetReference></connector><label>Dup</label></rules>
</decisions>
<screens><name>Dup_Screen</name><label>Dup</label></screens>
<recordCreates><name>Make_One</name><label>Make One</label><object>Thing__c</object>
 <inputAssignments><field>Status__c</field><value><stringValue>New</stringValue></value></inputAssignments></recordCreates>
<start><connector><targetReference>Get_Rows</targetReference></connector></start>
<status>Active</status>
"""


def test_loop_exit_is_a_condition_after_the_loop(tmp_path):
    _, g, _ = _facts(tmp_path, LOOP_FLOW)
    ef = g["flow"]["effects"][0]
    assert ef["element"] == "Make_One" and ef["when"] == ["Each_Row: no item took Is_Dup=Dup"]


WITHHELD_FLOW = """
<apiVersion>65.0</apiVersion><label>Copy</label><processType>AutoLaunchedFlow</processType>
<variables><name>vSecret</name><dataType>String</dataType><isCollection>false</isCollection><isInput>true</isInput>
 <isOutput>false</isOutput></variables>
<recordUpdates><name>Copy_It</name><label>Copy It</label><filterLogic>and</filterLogic>
 <filters><field>Id</field><operator>EqualTo</operator><value><elementReference>$Record.Parent__c</elementReference></value></filters>
 <inputAssignments><field>Secret_Field__c</field><value><elementReference>vSecret</elementReference></value></inputAssignments>
 <inputAssignments><field>Score__c</field><value><elementReference>$Record.Secret_Field__c</elementReference></value></inputAssignments>
 <object>Thing__c</object></recordUpdates>
<start><connector><targetReference>Copy_It</targetReference></connector><object>Thing__c</object>
 <recordTriggerType>Update</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>
<status>Active</status>
"""


def test_withheld_fields_leave_no_trace(tmp_path):
    """A withheld source gives no copy note (it would spell the label); a variable whose only destination is a
    withheld field is masked everywhere; the guard finds bare names and labels."""
    schema = _schema(fields={("Thing__c", "Secret_Field__c"): {"data_type": "Text", "label": "Secret Field",
                                                               "is_nullable": 1},
                             ("Thing__c", "Score__c"): {"data_type": "Text", "length": 20}},
                     labels={"Thing__c": "Thing", "Other__c": "Other"})
    _, g, _ = _facts(tmp_path, WITHHELD_FLOW, schema)
    text = json.dumps(g)
    assert "Secret_Field__c" not in text and "vSecret" not in text and "secret field" not in text.lower()
    ef = g["flow"]["effects"][0]
    assert ef["sets"]["Score__c"] == {"from": "$Record.[withheld field 1]"}
    assert ef["sets"]["[withheld field 1]"] == {"variable": "[variable for withheld field 1]"}
    guard = L.WithheldGuard({"Thing__c.Secret_Field__c"}, schema)
    objs = {"Thing__c"}
    hits = guard.scan({"caveats": ["copies Secret_Field__c", "$Record.Secret_Field__c", "the secret field",
                                   "Other__c.Secret_Field__c"]}, objs, "Thing__c")
    assert [h[0] for h in hits] == ["withheld field (bare)", "withheld field (bare)", "withheld field (label)"]
    assert guard.scan({"object": "Other__c", "sets": {"Secret_Field__c": 1}}, objs) == []


# ---------------------------------------------------------------------------------------------- generator version 3
UPSERT_FLOW = """
<apiVersion>65.0</apiVersion><label>Rows</label><processType>Flow</processType>
<variables><name>rows</name><dataType>SObject</dataType><isCollection>true</isCollection><isInput>false</isInput>
 <isOutput>false</isOutput><objectType>Thing__c</objectType></variables>
<variables><name>row</name><dataType>SObject</dataType><isCollection>false</isCollection><isInput>false</isInput>
 <isOutput>false</isOutput><objectType>Thing__c</objectType></variables>
<variables><name>pickedId</name><dataType>String</dataType><isCollection>false</isCollection><isInput>true</isInput>
 <isOutput>false</isOutput></variables>
<formulas><name>fx_count</name><dataType>Number</dataType><expression>{!rows.size}</expression><scale>0</scale></formulas>
<assignments><name>Fill</name><label>Fill</label>
 <assignmentItems><assignToReference>row.Id</assignToReference><operator>Assign</operator>
  <value><elementReference>pickedId</elementReference></value></assignmentItems>
 <assignmentItems><assignToReference>row.Score__c</assignToReference><operator>Assign</operator>
  <value><elementReference>fx_count</elementReference></value></assignmentItems>
 <assignmentItems><assignToReference>rows</assignToReference><operator>Add</operator>
  <value><elementReference>row</elementReference></value></assignmentItems>
 <connector><targetReference>Save_Rows</targetReference></connector></assignments>
<recordCreates><name>Save_Rows</name><label>Save Rows</label><doesUpsert>true</doesUpsert>
 <inputReference>rows</inputReference></recordCreates>
<start><connector><targetReference>Fill</targetReference></connector></start>
<status>Active</status>
"""


def test_upsert_record_id_and_collection_size(tmp_path):
    """doesUpsert gives kind upsert (create and update); an Id assignment is the record_id, never a write; a
    collection's .size is no record field."""
    schema = _schema(fields={("Thing__c", "Score__c"): {"data_type": "Number"}, ("Thing__c", "Id"): {"data_type": "Id"}})
    _, g, _ = _facts(tmp_path, UPSERT_FLOW, schema)
    ef = g["flow"]["effects"][0]
    assert ef["kind"] == "upsert" and ef["record_id"] == {"variable": "pickedId"}
    assert "Id" not in ef["sets"] and ef["sets"]["Score__c"] == {"computed": "formula:fx_count"}
    fields = {f["field"]: f["operations"] for f in g["fields"]}
    assert fields["Thing__c.Score__c"] == ["create", "update"]
    assert "Thing__c.Id" not in fields and not any(f.endswith(".size") for f in fields)
    assert g["objects"][0]["role"] == ["creates", "updates"]


COND_FLOW = """
<apiVersion>65.0</apiVersion><label>Cond</label><processType>AutoLaunchedFlow</processType>
<decisions><name>Which</name><label>Which</label><defaultConnector><targetReference>Set_B</targetReference></defaultConnector>
 <defaultConnectorLabel>B</defaultConnectorLabel>
 <rules><name>A</name><conditionLogic>and</conditionLogic>
  <conditions><leftValueReference>$Record.Status__c</leftValueReference><operator>EqualTo</operator>
   <rightValue><stringValue>A</stringValue></rightValue></conditions>
  <connector><targetReference>Set_A</targetReference></connector><label>A</label></rules>
</decisions>
<assignments><name>Set_A</name><label>Set A</label>
 <assignmentItems><assignToReference>$Record.Score__c</assignToReference><operator>Assign</operator>
  <value><numberValue>1.0</numberValue></value></assignmentItems>
 <connector><targetReference>Post</targetReference></connector></assignments>
<assignments><name>Set_B</name><label>Set B</label>
 <assignmentItems><assignToReference>$Record.Note__c</assignToReference><operator>Assign</operator>
  <value><stringValue>b</stringValue></value></assignmentItems>
 <connector><targetReference>Post</targetReference></connector></assignments>
<actionCalls><name>Post</name><label>Post</label><actionName>slackPostMessage</actionName><actionType>slackPostMessage</actionType>
 <connector><targetReference>Stamp_Ts</targetReference></connector>
 <faultConnector><targetReference>Save</targetReference></faultConnector>
 <outputParameters><assignToReference>$Record.Thread__c</assignToReference><name>slackMessageTimestamp</name></outputParameters>
</actionCalls>
<assignments><name>Stamp_Ts</name><label>Stamp</label>
 <assignmentItems><assignToReference>$Record.Seen__c</assignToReference><operator>Assign</operator>
  <value><booleanValue>true</booleanValue></value></assignmentItems>
 <connector><targetReference>Save</targetReference></connector></assignments>
<recordUpdates><name>Save</name><label>Save</label><inputReference>$Record</inputReference>
 <inputAssignments><field>Thread__c</field><value><elementReference>$Record.Thread__c</elementReference></value></inputAssignments>
</recordUpdates>
<start><connector><targetReference>Which</targetReference></connector><object>Thing__c</object>
 <recordTriggerType>Update</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>
<status>Active</status>
"""


def test_sets_on_some_routes_and_action_outputs(tmp_path):
    """A field set on one branch only is in sets_only_when with that branch's route; a field set after an action
    whose fault path also reaches the save carries 'Element=next'; a value an action output wrote into $Record.F
    comes from the action."""
    schema = _schema(fields={("Thing__c", "Score__c"): {"data_type": "Number"}, ("Thing__c", "Note__c"): {"data_type": "Text"},
                             ("Thing__c", "Seen__c"): {"data_type": "Checkbox"}, ("Thing__c", "Thread__c"): {"data_type": "Text"},
                             ("Thing__c", "Status__c"): {"data_type": "Picklist"}})
    _, g, _ = _facts(tmp_path, COND_FLOW, schema)
    ef = next(e for e in g["flow"]["effects"] if e["element"] == "Save")
    assert ef["sets"]["Thread__c"] == {"from": "Post.slackMessageTimestamp"}
    assert ef["sets_only_when"]["Score__c"] == ["Which=A"]
    assert ef["sets_only_when"]["Note__c"] == ["Which=default"]
    assert ef["sets_only_when"]["Seen__c"] == ["Post=next"]


def test_when_routes_are_capped(tmp_path):
    """More than 16 routes are not listed: when null, when_routes the count, when_all the shared conditions. Five
    decisions in a row, each with two outcomes that go on and a default that ends the run, give 32 routes."""
    n, body = 5, []
    for i in range(n):
        nxt = f"D{i + 1}" if i + 1 < n else "Mail"
        rules = "".join(f"""<rules><name>{o}</name><conditionLogic>and</conditionLogic>
  <conditions><leftValueReference>$Record.Score__c</leftValueReference><operator>EqualTo</operator>
   <rightValue><numberValue>{i * 2 + k}.0</numberValue></rightValue></conditions>
  <connector><targetReference>{nxt}</targetReference></connector><label>{o}</label></rules>"""
                        for k, o in enumerate(("Y", "Z")))
        body.append(f"<decisions><name>D{i}</name><label>D{i}</label><defaultConnectorLabel>no</defaultConnectorLabel>"
                    f"{rules}</decisions>")
    xml = ("<apiVersion>65.0</apiVersion><label>Many</label><processType>AutoLaunchedFlow</processType>" + "".join(body) +
           "<actionCalls><name>Mail</name><label>Mail</label><actionName>emailSimple</actionName>"
           "<actionType>emailSimple</actionType></actionCalls>"
           "<start><connector><targetReference>D0</targetReference></connector><object>Thing__c</object>"
           "<recordTriggerType>Update</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>"
           "<status>Active</status>")
    _, g, _ = _facts(tmp_path, xml)
    ef = g["flow"]["effects"][0]
    assert ef["when"] is None and ef["when_routes"] == 32 and ef["when_all"] == []


def test_parent_hop_and_widened_masks():
    """CaseComment.Parent is the Case; an unknown object's Parent is not guessed. Ids with any key prefix, Slack ids
    with any second digit and plain 10-digit phone numbers are masked. The guard finds lower-case withheld names."""
    s = _schema()
    assert s.hop("CaseComment", "Parent") == ("ParentId", "Case")
    assert s.hop("FeedItem", "Parent") is None
    m = L.Masker([])
    out = m.text("case 5005g00000AbCdE, channel C1ABCDEFGH, call 5550100199")
    assert "5005g00000AbCdE" not in out and "C1ABCDEFGH" not in out and "5550100199" not in out
    schema = _schema(fields={("Thing__c", "Secret_Field__c"): {"data_type": "Text", "label": "Secret Field"}},
                     labels={"Thing__c": "Thing"})
    guard = L.WithheldGuard({"Thing__c.Secret_Field__c"}, schema)
    hits = guard.scan({"caveats": ["reads secret_field__c", "thing__c.secret_field__c"]}, {"Other__c"})
    assert len(hits) == 2


def test_curated_text_names_no_source_document():
    """Review P1 (2026-10-24): merge_flows.py refuses a source document named in curated text (titles and document
    types such as a handbook, the Solution Document or the KB, and ticket keys); generic words stay allowed."""
    import merge_flows as MF

    titles = ["training module solution document"]
    for text in ("the Training Module Solution Document notes it", "(Admin Handbook)", "The KB (dev9) says",
                 "the dev9 KB lists it", "the QuickBooks document says", "Epic KAN-1723 plans", "the Decision Sheet asks",
                 "the Background Check knowledgebase", "(Recruitment KB)", "the rubric document warns",
                 "the Training Module extract says", "the handover says", "(Trainer Handbook)"):
        assert L.attribution_hits(text, titles), text
    for text in ("as documented for dev10", "the documentation lists it", "Copy the candidate's project document",
                 "matches the payment by QuickBooks document number", "creates another Google Doc",
                 "the documents say", "an open review point", "a known gap"):
        assert L.attribution_hits(text, titles) == [], text
    gen = {"label": "Thing roll-up", "state": {"is_active": True}, "flow": {"start": {"entry": None}}}
    cur = {"api_name": "F", "caveats": ["Step dates skip weekends (Solution Document)."], "aliases": []}
    problems = MF.check_curated("F", cur, gen, {}, {}, True)[0]
    assert any("caveats names a source document (Solution Document)" in p for p in problems), problems
    cur["caveats"] = ["Step dates skip weekends, as documented."]
    assert not any("source document" in p for p in MF.check_curated("F", cur, gen, {}, {}, True)[0])
