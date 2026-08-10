"""New Salesforce fields are adopted; new objects are reported, not adopted.

Without this the config is a SNAPSHOT: a field created in Salesforce today
stays invisible to this platform until someone remembers to edit YAML, and
nothing anywhere says it is missing. Fields are additive and safe, so they are
taken automatically. Objects are not — a new object means a full extract of
something nobody asked for.
"""
import pytest

from syncworker.main import (COMPOUND_TYPES, adopt_new_fields,
                             discover_new_objects, report_new_objects)


class FakeClient:
    def __init__(self, types=None, objects=None, boom=False, types_by_object=None):
        self._types, self._objects, self._boom = types or {}, objects or {}, boom
        self._types_by_object = types_by_object

    def describe_field_types(self, name):
        if self._boom:
            raise RuntimeError("describe down")
        if self._types_by_object is not None:
            if name not in self._types_by_object:
                raise RuntimeError(f"no describe for {name}")
            return self._types_by_object[name]
        return self._types

    def list_objects(self):
        if self._boom:
            raise RuntimeError("down")
        return self._objects


class FakeSettings:
    sync_max_fields = 80
    sync_auto_objects = True


class Obj:
    def __init__(self, name):
        self.name = name


def test_a_new_field_is_picked_up_without_editing_the_config():
    client = FakeClient({"Id": "id", "Name": "string", "Brand_New__c": "string"})
    fields, rag = adopt_new_fields("Account", ["Id", "Name"], [], client, FakeSettings())
    assert "Brand_New__c" in fields


def test_a_new_long_text_field_becomes_searchable_automatically():
    """A notes field created today should be answerable tomorrow."""
    client = FakeClient({"Id": "id", "Notes__c": "textarea"})
    fields, rag = adopt_new_fields("Account", ["Id"], [], client, FakeSettings())
    assert "Notes__c" in fields and "Notes__c" in rag


@pytest.mark.parametrize("ctype", COMPOUND_TYPES)
def test_compound_fields_are_never_adopted(ctype):
    """The Bulk API rejects the whole query — adopting one breaks the object."""
    client = FakeClient({"Id": "id", "BillingAddress": ctype})
    fields, _ = adopt_new_fields("Account", ["Id"], [], client, FakeSettings())
    assert "BillingAddress" not in fields


def test_base64_blobs_are_never_adopted():
    client = FakeClient({"Id": "id", "VersionData": "base64"})
    fields, _ = adopt_new_fields("ContentVersion", ["Id"], [], client, FakeSettings())
    assert "VersionData" not in fields


def test_encrypted_credential_fields_are_never_adopted():
    """Candidate passwords must not drift into the warehouse via adoption."""
    client = FakeClient({"Id": "id", "LinkedIn_Password__c": "encryptedstring"})
    fields, _ = adopt_new_fields("Account", ["Id"], [], client, FakeSettings())
    assert "LinkedIn_Password__c" not in fields


def test_plain_string_password_fields_are_never_adopted():
    """Password__c on the candidate portal is typed `string`; only its NAME
    gives it away. Removing it from the config must stick."""
    client = FakeClient({"Id": "id", "Password__c": "string",
                         "Username__c": "string"})
    fields, _ = adopt_new_fields(
        "Candidate_Portal_Credential__c", ["Id"], [], client, FakeSettings())
    assert "Password__c" not in fields and "Username__c" in fields


def test_already_configured_fields_are_not_duplicated():
    client = FakeClient({"Id": "id", "Name": "string"})
    fields, _ = adopt_new_fields("Account", ["Id", "Name"], [], client, FakeSettings())
    assert fields.count("Name") == 1


def test_adoption_is_capped_so_a_huge_object_stays_workable():
    class Small(FakeSettings):
        sync_max_fields = 5

    client = FakeClient({f"F{i}__c": "string" for i in range(100)})
    fields, _ = adopt_new_fields("Big__c", ["Id"], [], client, Small())
    assert len(fields) <= 5


def test_a_describe_failure_leaves_the_configured_fields_untouched():
    """A transient API blip must not silently shrink what gets synced."""
    client = FakeClient(boom=True)
    fields, rag = adopt_new_fields("Account", ["Id", "Name"], ["Name"], client, FakeSettings())
    assert fields == ["Id", "Name"] and rag == ["Name"]


BASE_FIELDS = {"Id": "id", "Name": "string", "SystemModstamp": "datetime"}


def test_a_new_custom_object_is_adopted_with_its_fields():
    client = FakeClient(
        objects={"Account": "Account", "Shiny__c": "Shiny"},
        types_by_object={"Shiny__c": {**BASE_FIELDS, "Notes__c": "textarea"}},
    )
    adopted = discover_new_objects([Obj("Account")], client, FakeSettings())
    assert [o.name for o in adopted] == ["Shiny__c"]
    assert "Notes__c" in adopted[0].fields
    assert adopted[0].rag_fields == ("Notes__c",)


def test_adoption_is_off_unless_enabled():
    class Off(FakeSettings):
        sync_auto_objects = False

    client = FakeClient(objects={"Shiny__c": "Shiny"},
                        types_by_object={"Shiny__c": BASE_FIELDS})
    assert discover_new_objects([], client, Off()) == []


def test_wanted_standard_objects_are_adopted_once_readable():
    """The org admin grants Read on Tasks/Emails later — they must flow in
    without anyone editing config.yaml."""
    client = FakeClient(objects={"Task": "Task", "EmailMessage": "Email Message"},
                        types_by_object={"Task": dict(BASE_FIELDS),
                                         "EmailMessage": dict(BASE_FIELDS)})
    adopted = discover_new_objects([], client, FakeSettings())
    assert {o.name for o in adopted} == {"Task", "EmailMessage"}


def test_random_standard_and_companion_objects_are_not_adopted():
    client = FakeClient(
        objects={"ApexClass": "Apex Class", "AccountChangeEvent": "shadow",
                 "Interview__Share": "shadow", "Interview__History": "shadow"},
        types_by_object={},
    )
    assert discover_new_objects([], client, FakeSettings()) == []


def test_an_object_without_systemmodstamp_is_not_adopted():
    client = FakeClient(objects={"Odd__c": "Odd"},
                        types_by_object={"Odd__c": {"Id": "id", "Name": "string"}})
    assert discover_new_objects([], client, FakeSettings()) == []


def test_adopted_objects_respect_credential_and_type_filters():
    client = FakeClient(objects={"Portal__c": "Portal"}, types_by_object={
        "Portal__c": {**BASE_FIELDS, "Password__c": "string",
                      "Secret__c": "encryptedstring", "Blob__c": "base64"}})
    adopted = discover_new_objects([], client, FakeSettings())
    assert "Password__c" not in adopted[0].fields
    assert "Secret__c" not in adopted[0].fields
    assert "Blob__c" not in adopted[0].fields


def test_object_discovery_failure_is_not_fatal():
    assert discover_new_objects([], FakeClient(boom=True), FakeSettings()) == []


def test_new_custom_objects_are_reported():
    client = FakeClient(objects={"Account": "Account", "Shiny__c": "Shiny"})
    assert report_new_objects([Obj("Account")], client) == ["Shiny__c"]


def test_configured_objects_are_not_reported_as_new():
    client = FakeClient(objects={"Account": "Account"})
    assert report_new_objects([Obj("Account")], client) == []


def test_reporting_failure_is_not_fatal():
    assert report_new_objects([Obj("Account")], FakeClient(boom=True)) == []
