"""The production-metadata ingester (scripts/build_dictionary_from_metadata.py).

A metadata retrieve is the one source that knows what a formula computes and
what a roll-up actually counts — the two things behind most silently-wrong
numbers. These pin the parsing: formula gists, roll-up gists WITH their filter
items, picklists, lookup targets, and the sf_dictionary overlay shape.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_dictionary_from_metadata.py"
spec = importlib.util.spec_from_file_location("build_dictionary_from_metadata", _SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

_NS = 'xmlns="http://soap.sforce.com/2006/04/metadata"'


def _write(tmp_path: Path, relative: str, body: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_a_formula_field_gets_a_readonly_gist(tmp_path):
    path = _write(tmp_path, "f.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}>
  <fullName>Outstanding_Amount__c</fullName>
  <formula>Invoice_Amount__c - Amount_Paid__c</formula>
  <label>Outstanding Amount</label>
  <type>Currency</type>
</CustomField>""")
    entry = mod.parse_field(path)
    assert entry["api"] == "Outstanding_Amount__c"
    assert "FORMULA (read-only): = Invoice_Amount__c - Amount_Paid__c" in entry["help"]


def test_a_rollup_gist_keeps_its_filters(tmp_path):
    """The filters ARE the semantics: this roll-up counts ghosted interviews,
    not interviews, and only the metadata knows that."""
    path = _write(tmp_path, "f.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}>
  <fullName>Interviews_Ghosted__c</fullName>
  <label>Interviews Ghosted</label>
  <summaryFilterItems>
    <field>Interview__c.Interview_Status__c</field>
    <operation>equals</operation>
    <value>Ghosted</value>
  </summaryFilterItems>
  <summaryForeignKey>Interview__c.Candidate__c</summaryForeignKey>
  <summaryOperation>count</summaryOperation>
  <type>Summary</type>
</CustomField>""")
    entry = mod.parse_field(path)
    assert "Roll-up (read-only): COUNT(Interview__c rows)" in entry["help"]
    assert "Interview_Status__c equals Ghosted" in entry["help"]


def test_picklists_and_lookup_targets_are_captured(tmp_path):
    path = _write(tmp_path, "f.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}>
  <fullName>Status__c</fullName>
  <label>Status</label>
  <type>Picklist</type>
  <valueSet><valueSetDefinition>
    <value><fullName>Paid</fullName><label>Paid</label></value>
    <value><fullName>Voided</fullName><label>Voided</label></value>
  </valueSetDefinition></valueSet>
</CustomField>""")
    assert mod.parse_field(path)["values"] == ["Paid", "Voided"]

    path2 = _write(tmp_path, "g.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}>
  <fullName>Candidate__c</fullName>
  <label>Candidate</label>
  <type>Lookup</type>
  <referenceTo>Account</referenceTo>
</CustomField>""")
    assert mod.parse_field(path2)["ref"] == ["Account"]


def test_an_inline_help_text_outranks_the_description(tmp_path):
    path = _write(tmp_path, "f.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}>
  <fullName>X__c</fullName>
  <label>X</label>
  <type>Text</type>
  <description>internal dev note</description>
  <inlineHelpText>What the admin wrote for users.</inlineHelpText>
</CustomField>""")
    assert mod.parse_field(path)["help"].startswith("What the admin wrote")


def test_build_walks_the_sfdx_layout_into_dictionary_shape(tmp_path):
    _write(tmp_path, "Invoice__c/Invoice__c.object-meta.xml", f"""<?xml version="1.0"?>
<CustomObject {_NS}><label>Invoice</label></CustomObject>""")
    _write(tmp_path, "Invoice__c/fields/Amount__c.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}><fullName>Amount__c</fullName><label>Amount</label><type>Currency</type></CustomField>""")
    _write(tmp_path, "Invoice__c/validationRules/R1.validationRule-meta.xml", f"""<?xml version="1.0"?>
<ValidationRule {_NS}>
  <fullName>R1</fullName><active>true</active>
  <errorConditionFormula>ISBLANK(Amount__c)</errorConditionFormula>
  <errorMessage>Amount is required</errorMessage>
</ValidationRule>""")
    overlay, validations = mod.build(tmp_path)
    obj = overlay["objects"]["Invoice__c"]
    assert obj["label"] == "Invoice"
    assert obj["fields"][0]["api"] == "Amount__c"
    assert validations["Invoice__c"][0] == {
        "name": "R1", "active": True, "error": "Amount is required",
        "description": "", "condition": "ISBLANK(Amount__c)",
    }


def test_a_broken_field_file_is_skipped_not_fatal(tmp_path):
    _write(tmp_path, "X__c/fields/bad.field-meta.xml", "<not-xml")
    _write(tmp_path, "X__c/fields/ok.field-meta.xml", f"""<?xml version="1.0"?>
<CustomField {_NS}><fullName>Ok__c</fullName><label>Ok</label><type>Text</type></CustomField>""")
    overlay, _ = mod.build(tmp_path)
    assert [f["api"] for f in overlay["objects"]["X__c"]["fields"]] == ["Ok__c"]


def test_the_shipped_validation_rules_pack_retrieves_save_failures(monkeypatch):
    from app.config import settings
    from app.core import brain

    packs_dir = Path(__file__).resolve().parents[2] / "brain" / "packs"
    monkeypatch.setattr(settings, "brain_dir", str(packs_dir))
    monkeypatch.setattr(brain, "_cache", None)
    names = [p["name"] for p in brain.load()]
    assert "prod-validation-rules" in names
    block = brain.knowledge_for("why would saving a background check fail with an error?")
    assert "Background_Check__c" in block
