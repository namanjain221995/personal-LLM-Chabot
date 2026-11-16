"""sf_assistant.engine.values: grounding of model values against stored and knowledge values (synthetic warehouse)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine.catalog import RecordError  # noqa: E402
from sf_assistant.engine.query_builder import BuildCtx, Scope, filter_frag  # noqa: E402
from sf_assistant.engine.values import (escape_like, ground_value, is_record_number, looks_like_id, normalize,  # noqa: E402
                          parse_bool)
from warehouse_fixture import open_fixture  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")


@pytest.fixture(scope="module")
def cat(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_fixture(tmp_path_factory.mktemp("v"))
    yield t.catalog
    t.close()


def _f(cat, obj, fld):
    return cat.objects[obj].fields[fld]


def test_normalize():
    assert normalize("Full-Time") == normalize("full time") == "full time"
    assert normalize("  Offer   Received. ") == "offer received"
    assert normalize("H1B/Visa (new)") == "h1b visa new"


def test_full_time_binds_the_stored_spelling(cat):
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Employment_Type__c"), "full-time")
    assert g.values == ["Full Time"] and not g.zero
    assert any("knowledge spells it 'Full-Time'" in n for n in g.notes)


def test_offer_received_case_insensitive_with_cross_field_note(cat):
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Outcome__c"), "offer received")
    assert g.values == ["Offer Received"]
    assert "'Offer Received' is also stored in Interview_Status__c (2 records, legacy value)" in g.notes


def test_no_cross_field_note_when_both_fields_list_the_value(cat):
    """Two live picklists sharing a word (e.g. two 'Pending' statuses) mean different things: no note."""
    other = _f(cat, "Interview__c", "Interview_Status__c")
    saved = list(other.picklist)
    try:
        other.picklist.append(("Offer Received", "Offer Received", "Offer stage."))
        g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Outcome__c"), "Offer Received")
        assert g.values == ["Offer Received"]
        assert not any("also stored in" in n for n in g.notes)
    finally:
        other.picklist[:] = saved
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Outcome__c"), "Offer Received")
    assert any("also stored in Interview_Status__c" in n for n in g.notes)


def test_unknown_value_lists_stored_counts_and_cross_field_hint(cat):
    with pytest.raises(RecordError) as exc:
        ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Status__c"), "Offer")
    msg = str(exc.value)
    assert exc.value.code == "E4"
    assert msg.startswith("'Offer' is not a value of Interview__c.Interview_Status__c. Stored values (records): Completed 5")
    assert "Offer Received 2" in msg and "Close: 'Offer Received' (2)" in msg
    assert "'Offer Received' is stored in Interview__c.Interview_Outcome__c (2 records)" in msg
    assert "Knowledge values with no records: Rescheduled" in msg
    assert msg.endswith("Use one of them exactly, or op contains.")


def test_knowledge_only_value_binds_with_zero_note(cat):
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Outcome__c"), "reference check")
    assert g.values == ["Reference Check"] and g.zero
    assert any("no stored records have it" in n for n in g.notes)


def test_legacy_note(cat):
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Status__c"), "Offer Received")
    assert "'Offer Received' is a legacy value of Interview_Status__c (not in the current picklist)" in g.notes


def test_boolean_words():
    for w in ("true", "Yes", "y", "checked", "ticked", "active", "enabled", "on", "1", True, 1):
        assert parse_bool(w) is True
    for w in ("false", "No", "n", "unchecked", "inactive", "disabled", "off", "0", False, 0):
        assert parse_bool(w) is False
    assert parse_bool("maybe") is None


def test_boolean_filter_and_error(cat):
    ctx = BuildCtx(cat, "America/New_York", dt.date(2026, 10, 13))
    sc = Scope(cat, cat.objects["Interview__c"])
    out = filter_frag(sc, {"field": "IsDeleted", "op": "eq", "value": "no"}, ctx)
    assert out.frag.sql == 't0."IsDeleted" = ?' and out.frag.params == (False,)
    out = filter_frag(sc, {"field": "IsDeleted", "op": "ne", "value": True}, ctx)
    assert out.frag.sql == 't0."IsDeleted" IS DISTINCT FROM ?'
    with pytest.raises(RecordError) as exc:
        filter_frag(sc, {"field": "IsDeleted", "op": "eq", "value": "perhaps"}, ctx)
    assert exc.value.code == "E4d" and "true or false" in str(exc.value)


def test_multipicklist_grounds_split_values(cat):
    g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Evaluation_Parameter__c"), "coding")
    assert g.values == ["Coding"]
    with pytest.raises(RecordError):
        ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Evaluation_Parameter__c"), "Communication;Coding")


def test_reference_by_name_and_by_id(cat):
    ctx = BuildCtx(cat, "America/New_York", dt.date(2026, 10, 13))
    sc = Scope(cat, cat.objects["Interview__c"])
    by_name = filter_frag(sc, {"field": "Recruiter__c", "op": "in", "values": ["Recruiter A", "Recruiter C"]}, ctx)
    assert "chr(160), ' '), '\\s+', ' ', 'g'))) = ANY(?)" in by_name.frag.sql
    assert by_name.frag.params == (["recruiter a", "recruiter c"],)
    assert by_name.checks and by_name.checks[0].target == "Recruiter__c"
    by_id = filter_frag(sc, {"field": "Recruiter__c", "op": "eq", "value": "a06000000000002AAA"}, ctx)
    assert by_id.frag.params == ("a06000000000002AAA",)


def test_record_type_by_name_and_miss(cat):
    ctx = BuildCtx(cat, "America/New_York", dt.date(2026, 10, 13))
    sc = Scope(cat, cat.objects["Account"])
    out = filter_frag(sc, {"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}, ctx)
    assert out.frag.params == (["012000000000001AAA"],) and out.text == "RecordTypeId = 'Person Account'"
    with pytest.raises(RecordError) as exc:
        filter_frag(sc, {"field": "RecordTypeId", "op": "eq", "value": "Vendor"}, ctx)
    assert "Person Account (PersonAccount) 3" in str(exc.value) and "B2B Client (B2B_Client) 1" in str(exc.value)


def test_text_without_index_is_not_grounded(cat):
    assert ground_value(cat, "Recruiter__c", cat.objects["Recruiter__c"].fields["Name"], "Recruiter A") is None


def test_helpers():
    assert is_record_number("REC-00024") and is_record_number("INT0042") and not is_record_number("Recruiter A")
    assert escape_like("5%_\\") == "5\\%\\_\\\\"
    assert looks_like_id("a06000000000001AAA") and looks_like_id("a06000000000001") and not looks_like_id("a06")
