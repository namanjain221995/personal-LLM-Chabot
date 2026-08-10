"""Selective SQL grounding over a whole-org warehouse.

The owner syncs EVERYTHING readable (2026-08-06): business tables plus
hundreds of Share/History/Feed shadows and setup objects. The SQL prompt must
stay grounded on the business slice, pulling plumbing tables in only when the
question actually mentions them.
"""
from app.core.schema_cache import relevant_schema

COLS = [("Id", "VARCHAR")]

SCHEMA = {
    "Interview__c": COLS,
    "Account": COLS,
    "Recruiter__c": COLS,
    "_sync_meta": COLS,
    "Interview__Share": COLS,
    "AccountHistory": COLS,
    "PermissionSet": COLS,
    "ApexClass": COLS,
    "ZVC__Zoom_Meeting__c": COLS,
}


def test_business_tables_are_always_grounded():
    keep = relevant_schema(SCHEMA, "how many interviews did we run last week?")
    assert "Interview__c" in keep and "Account" in keep
    assert "ZVC__Zoom_Meeting__c" in keep, "packaged custom objects are business data"


def test_shadow_and_system_tables_stay_out_of_unrelated_questions():
    keep = relevant_schema(SCHEMA, "total invoice revenue this month")
    assert "Interview__Share" not in keep
    assert "AccountHistory" not in keep
    assert "PermissionSet" not in keep
    assert "ApexClass" not in keep


def test_internal_bookkeeping_is_never_grounded():
    keep = relevant_schema(SCHEMA, "anything at all")
    assert "_sync_meta" not in keep


def test_a_system_table_joins_when_the_question_names_it():
    keep = relevant_schema(SCHEMA, "list every permissionset assigned to admins")
    assert "PermissionSet" in keep


def test_a_shadow_joins_when_the_question_names_it():
    keep = relevant_schema(SCHEMA, "show the accounthistory changes for Acme")
    assert "AccountHistory" in keep


def test_short_stopwords_do_not_match_tables():
    keep = relevant_schema(SCHEMA, "who is on the apex team")
    # "apex" is 4 chars and DOES match ApexClass — but "the"/"is"/"on" never
    # match anything; verify no unrelated shadows slipped in alongside.
    assert "Interview__Share" not in keep
    assert "AccountHistory" not in keep


def test_matched_extras_are_capped():
    big = {f"Whatever{i}Share": COLS for i in range(100)}
    big["Interview__c"] = COLS
    keep = relevant_schema(big, "who did we share whatever with?")
    extras = [t for t in keep if t != "Interview__c"]
    assert len(extras) <= 40
