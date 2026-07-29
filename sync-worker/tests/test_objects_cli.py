"""Editing which Salesforce objects and fields are synced.

The failures this guards against are all SILENT ones: a missing SystemModstamp
turns every incremental sync into a full re-extract, a rag_field that is not
also a field is never fetched so never indexed, and a YAML round-trip quietly
deletes the comment header that documents the file.
"""
import pytest
import yaml

from syncworker import objects as ob

HEADER = "# Salesforce sync configuration.\n# Adding an object needs no code change.\n\n"

BASE = HEADER + """objects:
  - name: Account
    fields:
      - Id
      - Name
      - SystemModstamp
  - name: Case
    fields:
      - Id
      - Subject
      - Description
      - SystemModstamp
    rag_fields:
      - Description
"""


@pytest.fixture()
def config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(BASE)
    return p


def objects_of(path):
    return yaml.safe_load(path.read_text())["objects"]


def run(path, *argv):
    return ob.main([*argv, "--config", str(path)])


# ---------------------------------------------------------------------------
# Adding an object
# ---------------------------------------------------------------------------


def test_add_an_object_with_its_fields(config):
    assert run(config, "add", "Opportunity", "--fields", "Name,StageName,Amount") == 0
    opp = next(o for o in objects_of(config) if o["name"] == "Opportunity")
    assert opp["fields"] == ["Id", "Name", "StageName", "Amount", "SystemModstamp"]


def test_the_required_pair_is_added_for_you(config):
    """Nobody should have to remember these, and forgetting them fails quietly."""
    run(config, "add", "Lead", "--fields", "Company")
    lead = next(o for o in objects_of(config) if o["name"] == "Lead")
    assert "Id" in lead["fields"] and "SystemModstamp" in lead["fields"]


def test_naming_the_required_pair_yourself_does_not_duplicate_it(config):
    run(config, "add", "Lead", "--fields", "Id,Company,SystemModstamp")
    lead = next(o for o in objects_of(config) if o["name"] == "Lead")
    assert lead["fields"].count("Id") == 1
    assert lead["fields"].count("SystemModstamp") == 1


def test_rag_fields_are_recorded(config):
    run(config, "add", "Opportunity", "--fields", "Name,Description",
        "--rag-fields", "Description")
    opp = next(o for o in objects_of(config) if o["name"] == "Opportunity")
    assert opp["rag_fields"] == ["Description"]


def test_a_rag_field_that_is_not_selected_is_rejected(config):
    """It would be fetched by nothing and indexed by nothing — silently."""
    with pytest.raises(ob.ConfigError, match="must also appear in fields"):
        ob.upsert_object([], "Opportunity", ["Name"], ["Description"])


def test_adding_an_existing_object_replaces_its_field_list(config):
    run(config, "add", "Account", "--fields", "Industry")
    acct = next(o for o in objects_of(config) if o["name"] == "Account")
    assert acct["fields"] == ["Id", "Industry", "SystemModstamp"]
    assert "Name" not in acct["fields"]


# ---------------------------------------------------------------------------
# Adding fields to an existing object
# ---------------------------------------------------------------------------


def test_add_fields_merges_rather_than_replaces(config):
    assert run(config, "add-fields", "Account", "--fields", "Industry,Type") == 0
    acct = next(o for o in objects_of(config) if o["name"] == "Account")
    assert acct["fields"] == ["Id", "Name", "Industry", "Type", "SystemModstamp"]


def test_add_fields_keeps_existing_rag_fields(config):
    run(config, "add-fields", "Case", "--fields", "Priority")
    case = next(o for o in objects_of(config) if o["name"] == "Case")
    assert case["rag_fields"] == ["Description"]
    assert "Priority" in case["fields"]


def test_add_fields_does_not_duplicate_one_already_present(config):
    run(config, "add-fields", "Account", "--fields", "Name,Industry")
    acct = next(o for o in objects_of(config) if o["name"] == "Account")
    assert acct["fields"].count("Name") == 1


def test_add_fields_on_an_unknown_object_is_an_error(config, capsys):
    assert run(config, "add-fields", "Nope", "--fields", "X") == 2
    assert "use 'add' first" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Removing
# ---------------------------------------------------------------------------


def test_remove_stops_syncing_an_object(config):
    assert run(config, "remove", "Account") == 0
    assert [o["name"] for o in objects_of(config)] == ["Case"]


def test_removing_something_absent_says_so(config, capsys):
    assert run(config, "remove", "Ghost") == 2
    assert "not in the config" in capsys.readouterr().err


def test_the_last_object_cannot_be_removed(config, capsys):
    run(config, "remove", "Account")
    assert run(config, "remove", "Case") == 2
    assert "nothing to do" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_the_comment_header_survives_an_edit(config):
    """A plain YAML round-trip deletes every comment. The header is the only
    documentation of this file that anyone opening it will see."""
    run(config, "add", "Opportunity", "--fields", "Name")
    text = config.read_text()
    assert text.startswith("# Salesforce sync configuration.")
    assert "needs no code change" in text


def test_the_result_is_still_valid_for_the_real_loader(config):
    """The CLI and the sync worker must agree on what a valid config is."""
    from syncworker.config import load_object_configs

    run(config, "add", "Opportunity", "--fields", "Name,Description",
        "--rag-fields", "Description")
    parsed = load_object_configs(str(config))
    names = [o.name for o in parsed]
    assert "Opportunity" in names


@pytest.mark.parametrize("bad", ["9Lives", "has-dash", "has space"])
def test_invalid_object_names_are_rejected(bad):
    with pytest.raises(ob.ConfigError):
        ob.upsert_object([], bad, ["Name"])


@pytest.mark.parametrize("bad", ["9Lives", "has-dash", "has space"])
def test_invalid_field_names_are_rejected(bad):
    with pytest.raises(ob.ConfigError):
        ob.upsert_object([], "Account", ["Name", bad])


def test_listing_shows_what_is_configured(config, capsys):
    assert run(config, "list") == 0
    out = capsys.readouterr().out
    assert "Account" in out and "Case" in out
    assert "indexed for search: Description" in out


# ---------------------------------------------------------------------------
# Importing an org "Objects, Fields" spreadsheet
# ---------------------------------------------------------------------------

SHEET = """Objects,Fields
Account,Name
,Description
,BillingAddress
,
,Ghost_Field__c
Session__c,Notes__c
Secret__c,Hidden__c
Gone__c,Whatever__c
"""


def field(name, ftype="string"):
    return {"name": name, "type": ftype}


ORG = {
    "Account": {"queryable": True, "fields": [
        field("Id"), field("Name"), field("Description", "textarea"),
        field("BillingAddress", "address"), field("SystemModstamp"),
    ]},
    # Visible object, but every custom field is hidden by field-level security.
    "Secret__c": {"queryable": True, "fields": [field("Id"), field("SystemModstamp")]},
    "Session__c": {"queryable": True, "fields": [
        field("Id"), field("Notes__c", "textarea"), field("SystemModstamp")]},
    # Gone__c is absent entirely -> describe returns None
}


@pytest.fixture()
def sheet(tmp_path):
    p = tmp_path / "org.csv"
    p.write_text(SHEET)
    return p


def describe(name):
    return ORG.get(name)


def test_the_sheet_is_parsed_with_names_carried_down(sheet):
    parsed = ob.parse_sheet(sheet)
    assert list(parsed) == ["Account", "Session__c", "Secret__c", "Gone__c"]
    assert parsed["Account"] == ["Name", "Description", "BillingAddress", "Ghost_Field__c"]


def test_only_fields_this_user_can_read_are_kept(sheet):
    entries, _ = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    account = next(e for e in entries if e["name"] == "Account")
    assert "Name" in account["fields"]
    assert "Ghost_Field__c" not in account["fields"], "not visible to the user"


def test_compound_fields_are_dropped(sheet):
    """SOQL accepts them; the Bulk API rejects the whole query."""
    entries, _ = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    account = next(e for e in entries if e["name"] == "Account")
    assert "BillingAddress" not in account["fields"]


def test_long_text_fields_become_searchable(sheet):
    entries, _ = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    account = next(e for e in entries if e["name"] == "Account")
    assert account["rag_fields"] == ["Description"]


def test_an_object_with_no_readable_fields_is_skipped_with_a_reason(sheet):
    entries, notes = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    assert not any(e["name"] == "Secret__c" for e in entries)
    assert any("field-level security" in n and "Secret__c" in n for n in notes)


def test_an_object_that_does_not_exist_is_skipped_with_a_reason(sheet):
    entries, notes = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    assert not any(e["name"] == "Gone__c" for e in entries)
    assert any("Gone__c" in n and "not readable" in n for n in notes)


def test_every_imported_entry_is_valid_for_the_real_loader(sheet, tmp_path):
    from syncworker.config import load_object_configs

    entries, _ = ob.plan_from_sheet(ob.parse_sheet(sheet), describe)
    out = tmp_path / "config.yaml"
    ob.save("# header\n\n", entries, out)
    names = [o.name for o in load_object_configs(str(out))]
    assert names == ["Account", "Session__c"]


def test_a_huge_object_is_trimmed_rather_than_left_unwieldy():
    many = [f"F{i}__c" for i in range(200)]
    org = {"Big__c": {"queryable": True,
                      "fields": [field("Id"), field("SystemModstamp"),
                                 *[field(f) for f in many]]}}
    entries, notes = ob.plan_from_sheet({"Big__c": many}, lambda n: org.get(n))
    assert len(entries[0]["fields"]) == ob.MAX_FIELDS_PER_OBJECT + 2  # + Id/SystemModstamp
    assert any("trimmed" in n for n in notes)


def test_importing_nothing_readable_leaves_the_config_alone(config, tmp_path, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("Objects,Fields\nGone__c,X__c\n")
    before = config.read_text()
    import syncworker.objects as mod
    orig, mod._live_describe = mod._live_describe, lambda: (lambda n: None)
    try:
        assert run(config, "import-sheet", str(empty)) == 2
    finally:
        mod._live_describe = orig
    assert config.read_text() == before


def test_importing_a_sheet_keeps_fields_that_were_already_configured():
    """An org export is usually a PARTIAL list. The first real import dropped
    Name, StageName, CloseDate, Status and Email because the sheet did not
    mention them — and nothing broke visibly, because DuckDB keeps old columns.
    The data would simply have stopped refreshing."""
    org = {"Opportunity": {"queryable": True, "fields": [
        field("Id"), field("SystemModstamp"), field("StageName"),
        field("CloseDate"), field("Amount"), field("Description", "textarea")]}}
    existing = [{"name": "Opportunity",
                 "fields": ["Id", "StageName", "CloseDate", "SystemModstamp"]}]
    entries, _ = ob.plan_from_sheet(
        {"Opportunity": ["Amount", "Description"]}, lambda n: org.get(n), existing
    )
    fields = entries[0]["fields"]
    assert "StageName" in fields and "CloseDate" in fields, "curated fields survive"
    assert "Amount" in fields and "Description" in fields, "sheet fields are added"


def test_an_object_configured_before_but_absent_from_the_sheet_is_kept():
    org = {"Case": {"queryable": True,
                    "fields": [field("Id"), field("SystemModstamp"), field("Subject")]}}
    existing = [{"name": "Case", "fields": ["Id", "Subject", "SystemModstamp"]}]
    entries, _ = ob.plan_from_sheet({}, lambda n: org.get(n), existing)
    assert [e["name"] for e in entries] == ["Case"]


def test_a_deliberate_rag_field_is_not_lost_by_reimporting():
    org = {"Case": {"queryable": True, "fields": [
        field("Id"), field("SystemModstamp"), field("Subject"), field("Notes")]}}
    existing = [{"name": "Case", "fields": ["Id", "Subject", "SystemModstamp"],
                 "rag_fields": ["Subject"]}]
    entries, _ = ob.plan_from_sheet({"Case": ["Notes"]}, lambda n: org.get(n), existing)
    assert "Subject" in entries[0]["rag_fields"]
