"""The org field dictionary: what users say → what the API calls it.

Asked about "interview status", a model with no dictionary writes
`Status__c` — which does not error, it returns nothing. A silently empty
result is the failure mode this exists to prevent, so the tests are about
whether the right names reach the prompt, not about formatting.
"""
import pytest

from app.core import sf_dictionary as sd

ROWS = [
    ("Interview__c", "Interview", "Interview_Status__c", "Interview Status", "picklist"),
    ("Interview__c", "Interview", "Candidate__c", "Candidate", "reference"),
    ("Interview__c", "Interview", "Date_of_Interview__c", "Date of Interview", "date"),
    ("Opportunity", "Opportunity", "CloseDate", "Close Date", "date"),
    ("Opportunity", "Opportunity", "StageName", "Stage", "picklist"),
    ("Account", "Account", "AnnualRevenue", "Annual Revenue", "currency"),
]


@pytest.fixture(autouse=True)
def dictionary(tmp_path):
    path = tmp_path / "dict.json"
    sd.save(sd.build_from_rows(ROWS), str(path))
    sd._cache = None
    sd.load(str(path))
    yield
    sd._cache = None


def test_the_export_shape_is_parsed():
    data = sd.build_from_rows(ROWS)
    assert set(data["objects"]) == {"Interview__c", "Opportunity", "Account"}
    assert len(data["objects"]["Interview__c"]["fields"]) == 3


def test_a_question_pulls_the_object_it_names():
    hint = sd.hint_for("what is the interview status for each candidate?")
    assert "Interview_Status__c" in hint
    assert 'Interview__c = "Interview"' in hint


def test_the_label_a_user_would_say_is_shown_next_to_the_api_name():
    """The mapping is the whole point — the model has to see both sides."""
    hint = sd.hint_for("close date of opportunities")
    assert 'CloseDate = "Close Date"' in hint


def test_the_named_object_outranks_one_that_merely_shares_a_field():
    """Naming "interview" must not surface Opportunity just because both have
    a date field."""
    picked = [o["api"] for o in sd.relevant_objects("interview date")]
    assert picked[0] == "Interview__c"


def test_an_unrelated_question_adds_nothing():
    """A question the dictionary cannot help with must leave the prompt alone."""
    assert sd.hint_for("write me a python function") == ""


def test_common_words_do_not_match_everything():
    """Without a stop list, "how many records" scores every object equally."""
    assert sd.hint_for("how many records are there") == ""


def test_a_missing_dictionary_is_not_fatal(tmp_path):
    sd._cache = None
    sd.load(str(tmp_path / "nope.json"))
    assert sd.available() is False
    assert sd.hint_for("interview status") == ""


def test_the_hint_warns_that_a_wrong_name_returns_nothing():
    """The model must know a plausible guess fails SILENTLY, not loudly."""
    assert "returns no rows instead of an error" in sd.hint_for("interview status")


def test_only_a_few_objects_are_injected():
    many = [(f"Obj{i}__c", f"Obj {i}", "Status__c", "Status", "picklist")
            for i in range(50)]
    sd.save(sd.build_from_rows(many), "/tmp/many.json")
    sd._cache = None
    sd.load("/tmp/many.json")
    assert len(sd.relevant_objects("status")) <= sd.MAX_OBJECTS


def test_both_query_paths_consult_the_dictionary():
    import inspect

    from app.engines import live_sf, sql

    assert "hint_for" in inspect.getsource(sql._ask_sql)
    assert "hint_for" in inspect.getsource(live_sf.write_soql)


# ── The AI-friendly export ───────────────────────────────────────────────────
# A second exporter emits a 13-column CSV and a much richer org-schema.json.
# The CSV's third column is ObjectKind, which the positional reader used to
# fold into the field name — silently, which is the whole problem.

AI_FRIENDLY_CSV = (
    '"ObjectApiName","ObjectLabel","ObjectKind","FieldApiName","FieldLabel",'
    '"FieldType","IsFormula","ReferenceTo","PicklistValues","Required",'
    '"Unique","Length","HelpText"\n'
    '"Interview__c","Interview","Custom Object","Interview_Outcome__c",'
    '"Interview Outcome","Picklist","False","","Ghosted;Rejected","False",'
    '"False","",""\n'
)

ORG_SCHEMA_JSON = {
    "org": "test",
    "objects": [
        {
            "apiName": "Interview__c",
            "label": "Interview",
            "fields": [
                {
                    "name": "Interview_Outcome__c",
                    "label": "Interview Outcome",
                    "type": "Picklist",
                    "picklistValues": ["Moved to Next Round", "Rejected", "Ghosted"],
                    "helpText": "How the interview ended.",
                },
                {
                    "name": "Candidate__c",
                    "label": "Candidate",
                    "type": "MasterDetail",
                    "referenceTo": ["Account"],
                },
                {
                    "name": "Preferred_Support_Person__c",
                    "label": "Preferred Support Person",
                    "type": "Lookup",
                    "referenceTo": ["Employee"],
                },
            ],
        }
    ],
}


def _write_json(tmp_path, payload):
    import json as _json

    path = tmp_path / "org-schema.json"
    path.write_text(_json.dumps(payload), encoding="utf-8")
    return str(path)


def test_the_thirteen_column_csv_is_read_by_header_not_position():
    """ObjectKind sits where the old exporter put the field name. Read
    positionally, every field in the org came out named "Custom Object"."""
    path = "/tmp/ai_friendly.csv"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(AI_FRIENDLY_CSV)
    data = sd.build_from_csv(path)
    fields = data["objects"]["Interview__c"]["fields"]
    assert [f["api"] for f in fields] == ["Interview_Outcome__c"]
    assert fields[0]["label"] == "Interview Outcome"
    assert fields[0]["type"] == "Picklist"


def test_a_headerless_export_still_reads_positionally():
    """The original five-column export has no recognisable header row."""
    path = "/tmp/legacy.csv"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("a,b,c,d,e\nInterview__c,Interview,Status__c,Status,picklist\n")
    data = sd.build_from_csv(path)
    assert data["objects"]["Interview__c"]["fields"][0]["api"] == "Status__c"


def test_org_schema_json_keeps_lookup_targets_and_picklist_values(tmp_path):
    data = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    fields = {f["api"]: f for f in data["objects"]["Interview__c"]["fields"]}
    assert fields["Candidate__c"]["ref"] == ["Account"]
    assert "Ghosted" in fields["Interview_Outcome__c"]["values"]
    assert fields["Interview_Outcome__c"]["help"] == "How the interview ended."


def test_merge_enriches_without_inventing_fields(tmp_path):
    """The overlay is a preprod export. Preferred_Support_Person__c does not
    exist in production, so it must not reach the prompt."""
    base = sd.build_from_rows(ROWS)
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    merged, stats = sd.merge(base, overlay)

    fields = {f["api"]: f for f in merged["objects"]["Interview__c"]["fields"]}
    assert fields["Candidate__c"]["ref"] == ["Account"]
    assert "Preferred_Support_Person__c" not in fields
    assert "Interview_Outcome__c" not in fields
    assert stats["skipped"] == 2
    assert stats["enriched"] == 1


def test_merge_can_add_new_fields_when_asked(tmp_path):
    base = sd.build_from_rows(ROWS)
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    merged, stats = sd.merge(base, overlay, add_new=True)
    fields = {f["api"] for f in merged["objects"]["Interview__c"]["fields"]}
    assert "Preferred_Support_Person__c" in fields
    assert stats["added"] == 2


def test_merge_leaves_the_base_dictionary_untouched(tmp_path):
    base = sd.build_from_rows(ROWS)
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    sd.merge(base, overlay, add_new=True)
    assert len(base["objects"]["Interview__c"]["fields"]) == 3


def test_the_hint_shows_what_a_lookup_points_at(tmp_path):
    base = sd.build_from_rows(ROWS)
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    merged, _ = sd.merge(base, overlay)
    sd.save(merged, str(tmp_path / "merged.json"))
    assert "Candidate__c" in sd.hint_for("which candidate had the interview")
    assert "→Account" in sd.hint_for("which candidate had the interview")


def test_picklist_values_appear_only_for_the_field_asked_about(tmp_path):
    """256 other fields' enums would drown the one that matters."""
    base = sd.build_from_rows(
        ROWS + [("Interview__c", "Interview", "Interview_Outcome__c",
                 "Interview Outcome", "picklist")]
    )
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    merged, _ = sd.merge(base, overlay)
    sd.save(merged, str(tmp_path / "merged.json"))

    asked = sd.hint_for("how many interviews were ghosted, by outcome")
    assert "Ghosted" in asked
    assert "How the interview ended." in asked

    unasked = sd.hint_for("interview date")
    assert "Ghosted" not in unasked


def test_a_plural_question_finds_the_singular_object():
    """"how many interviews" is how everyone phrases it, and it used to
    retrieve nothing because the object is Interview__c."""
    picked = [o["api"] for o in sd.relevant_objects("how many interviews were held")]
    assert "Interview__c" in picked


def test_shadow_objects_do_not_eat_the_slots(tmp_path):
    """AccountChangeEvent carries Account's name and fields, so it ties with
    Account on every score and displaces a real object."""
    rows = list(ROWS) + [
        ("AccountChangeEvent", "Account Change Event", "AnnualRevenue",
         "Annual Revenue", "currency"),
        ("AccountHistory", "Account History", "AnnualRevenue",
         "Annual Revenue", "currency"),
    ]
    sd.save(sd.build_from_rows(rows), str(tmp_path / "shadow.json"))
    picked = [o["api"] for o in sd.relevant_objects("annual revenue by account")]
    assert picked == ["Account"]


def test_a_question_about_history_still_reaches_the_shadow(tmp_path):
    rows = list(ROWS) + [
        ("AccountHistory", "Account History", "OldValue", "Old Value", "string"),
    ]
    sd.save(sd.build_from_rows(rows), str(tmp_path / "shadow2.json"))
    picked = [o["api"] for o in sd.relevant_objects("account history old value")]
    assert "AccountHistory" in picked


def test_a_question_naming_a_value_surfaces_the_field_that_holds_it(tmp_path):
    """Nobody asks "by outcome" — they ask "how many were ghosted"."""
    base = sd.build_from_rows(
        ROWS + [("Interview__c", "Interview", "Interview_Outcome__c",
                 "Interview Outcome", "picklist")]
    )
    overlay = sd.build_from_org_schema_json(_write_json(tmp_path, ORG_SCHEMA_JSON))
    merged, _ = sd.merge(base, overlay)
    sd.save(merged, str(tmp_path / "values.json"))

    hint = sd.hint_for("how many interviews were ghosted last month")
    assert "Interview_Outcome__c" in hint
    assert "Ghosted" in hint
