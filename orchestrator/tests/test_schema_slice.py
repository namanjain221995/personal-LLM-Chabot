"""How much schema the model is asked to read.

Measured before this existed: "how many candidates completed the training from
slot 128 and how many failed the mock in that" shipped 146 tables / 5,090
columns / 141,978 characters — about 41,000 tokens — for a question needing
five tables and 11,357 characters. On a 6,000-token answer budget the model
intermittently spent the whole budget reasoning and returned NOTHING, which the
engine read as "not in the warehouse" and sent to live Salesforce, which
answered off the wrong object. Every wrong answer of that shape starts here.
"""
from app.core import org_brief as ob
from app.core.schema_cache import format_schema, relevant_schema

# A miniature warehouse with the shapes that matter: a wide standard object,
# the tables a training question needs, a shadow, and unrelated noise.
def _cols(*names):
    return [(n, "VARCHAR") for n in names]


SCHEMA = {
    "Account": _cols("Id", "Name", "RecordTypeId", *[f"Filler_{i}__c" for i in range(90)]),
    "RecordType": _cols("Id", "Name"),
    "Candidate_Training__c": _cols("Id", "Candidate__c", "Cohort__c", "Status__c"),
    "Cohort__c": _cols("Id", "Name", "Start_Date__c"),
    "Internal_Interview__c": _cols("Id", "Candidate_Training__c", "Human_Decision__c"),
    "Interview_Type__c": _cols("Id", "Name"),
    "Interview__c": _cols("Id", "RecordTypeId", "Interview_Outcome__c"),
    "Candidate_Training__History": _cols("Id", "Field", "OldValue"),
    "Invoice__c": _cols("Id", "Invoice_Amount__c"),
    **{f"Noise_{i}__c": _cols("Id", "Name") for i in range(60)},
}

SLOT_Q = ("how many candidates completed the training from slot 128 "
          "and how many failed the mock in that")


def test_the_prompt_is_not_the_whole_warehouse():
    kept = relevant_schema(SCHEMA, SLOT_Q, must_include=ob.tables_for(SLOT_Q))
    assert len(kept) <= 24, f"{len(kept)} tables is back to burying the question"
    assert len(kept) < len(SCHEMA)


def test_a_slot_question_keeps_the_tables_it_cannot_be_answered_without():
    """"Slot" is Cohort__c. Word overlap alone ranked Cohort__c out of a
    question about slot 128, making the question unanswerable."""
    kept = relevant_schema(SCHEMA, SLOT_Q, must_include=ob.tables_for(SLOT_Q))
    for table in ("Candidate_Training__c", "Cohort__c", "Internal_Interview__c",
                  "Account", "RecordType"):
        assert table in kept, f"{table} was dropped from its own question"


def test_record_type_is_always_present():
    """Nearly every correct query joins it, and no question says the words."""
    assert "RecordType" in relevant_schema(SCHEMA, "how much have we invoiced")


def test_a_plural_question_still_finds_the_singular_table():
    """"interviews" scored Interview__c at zero, so ranking fell back to
    alphabetical and handed the model unrelated tables."""
    kept = relevant_schema(SCHEMA, "how many interviews have we run",
                           must_include=ob.tables_for("how many interviews have we run"))
    assert "Interview__c" in kept


def test_shadow_tables_stay_out_unless_asked_for():
    q = "how many trainings are active"
    assert "Candidate_Training__History" not in relevant_schema(SCHEMA, q)
    assert "Candidate_Training__History" in relevant_schema(
        SCHEMA, "show me the training history changes"
    )


def test_a_very_wide_table_is_trimmed_but_keeps_its_keys():
    """Account has 269 real columns; sending all of them for a question that
    touches three is what made the prompt unreadable."""
    kept = relevant_schema(SCHEMA, SLOT_Q, must_include=ob.tables_for(SLOT_Q))
    account = dict.fromkeys(name for name, _ in kept["Account"])
    assert len(account) <= 70
    for key in ("Id", "Name", "RecordTypeId"):
        assert key in account, f"{key} must survive trimming"


def test_a_question_matching_nothing_still_gets_a_usable_schema():
    """An empty schema raises NoSuchTable and diverts to live Salesforce."""
    kept = relevant_schema(SCHEMA, "zzz qqq")
    assert kept and "RecordType" in kept


def test_vocabulary_aliases_resolve_to_tables():
    assert "Cohort__c" in ob.tables_for("candidates in slot 128")
    assert "Internal_Interview__c" in ob.tables_for("how many oot mocks today")
    assert "Recruiter__c" in ob.tables_for("trainer workload")
    assert ob.tables_for("hello there") == []


def test_a_long_question_does_not_inject_three_unrelated_metrics():
    """This question pulled `active candidates`, `training enrolment` AND
    `assessment hire decision` — three definitions of three different
    measures — into one prompt."""
    assert len(ob.match_metrics(SLOT_Q)) <= 2


def test_the_model_is_told_how_the_tables_join():
    """Column types cannot say where a lookup points. Asked which candidates
    completed training in slot 128, the model wrote
    `JOIN Cohort__c c ON ii.Session__c = c.Id` — that column points at
    Session__c, so the join matched zero rows and the answer was "0", with a
    note explaining that slot 128 was not in use yet."""
    from app.core import sf_dictionary as sd

    sd.save({"objects": {
        "Internal_Interview__c": {"api": "Internal_Interview__c", "label": "Mock",
            "fields": [{"api": "Session__c", "label": "Session", "type": "reference",
                        "ref": ["Session__c"]},
                       {"api": "Candidate_Training__c", "label": "Training",
                        "type": "reference", "ref": ["Candidate_Training__c"]}]},
        "Session__c": {"api": "Session__c", "label": "Session", "fields": []},
        "Candidate_Training__c": {"api": "Candidate_Training__c", "label": "Training",
            "fields": [{"api": "Cohort__c", "label": "Slot", "type": "reference",
                        "ref": ["Cohort__c"]}]},
        "Cohort__c": {"api": "Cohort__c", "label": "Slot", "fields": []},
    }}, "/tmp/joinmap.json")

    m = sd.join_map(["Internal_Interview__c", "Candidate_Training__c",
                     "Cohort__c", "Session__c"])
    assert "Internal_Interview__c.Session__c = Session__c.Id" in m
    assert "Candidate_Training__c.Cohort__c = Cohort__c.Id" in m
    # The invented edge must not be offered.
    assert "Internal_Interview__c.Session__c = Cohort__c.Id" not in m
    assert "silently match zero rows" in m


def test_the_join_map_only_lists_edges_between_tables_actually_sent():
    from app.core import sf_dictionary as sd

    assert "Cohort__c" not in sd.join_map(["Internal_Interview__c", "Session__c"])


def test_sql_generation_does_not_run_the_reasoning_pass():
    """Reasoning shares the answer's token budget. On an 11,500-token SQL
    prompt the model thought for 121 seconds and returned zero characters;
    empty SQL then routed the question to live Salesforce."""
    import inspect

    from app.engines import sql as sql_engine

    assert "thinking=False" in inspect.getsource(sql_engine._ask_sql)
