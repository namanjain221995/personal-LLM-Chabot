"""New Salesforce fields are adopted; new objects are reported, not adopted.

Without this the config is a SNAPSHOT: a field created in Salesforce today
stays invisible to this platform until someone remembers to edit YAML, and
nothing anywhere says it is missing. Fields are additive and safe, so they are
taken automatically. Objects are not — a new object means a full extract of
something nobody asked for.
"""
import pytest

from syncworker.main import COMPOUND_TYPES, adopt_new_fields, report_new_objects


class FakeClient:
    def __init__(self, types=None, objects=None, boom=False):
        self._types, self._objects, self._boom = types or {}, objects or {}, boom

    def describe_field_types(self, name):
        if self._boom:
            raise RuntimeError("describe down")
        return self._types

    def list_objects(self):
        if self._boom:
            raise RuntimeError("down")
        return self._objects


class FakeSettings:
    sync_max_fields = 80


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


def test_new_custom_objects_are_reported():
    client = FakeClient(objects={"Account": "Account", "Shiny__c": "Shiny"})
    assert report_new_objects([Obj("Account")], client) == ["Shiny__c"]


def test_configured_objects_are_not_reported_as_new():
    client = FakeClient(objects={"Account": "Account"})
    assert report_new_objects([Obj("Account")], client) == []


def test_reporting_failure_is_not_fatal():
    assert report_new_objects([Obj("Account")], FakeClient(boom=True)) == []
