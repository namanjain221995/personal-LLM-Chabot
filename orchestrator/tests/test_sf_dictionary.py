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
