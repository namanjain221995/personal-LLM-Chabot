"""The semantic layer: what the org's measures MEAN.

sf_dictionary stops the model inventing a field name. This stops it inventing
a definition — counting initial calls as interviews, every Account as a
candidate, or a ghosting rate over a denominator that is half empty. Those
answers do not error, they just disagree with the business.
"""
import re

from app.core import org_brief as ob
from app.engines import sql as sql_engine


def test_every_metric_is_well_formed():
    for m in ob.METRICS:
        assert m["name"] and m["table"] and m["definition"], m
        assert m["sql"].upper().startswith("SELECT"), m["name"]
        assert m["chart"], m["name"]


def test_metric_names_are_unique():
    names = [m["name"] for m in ob.METRICS]
    assert len(names) == len(set(names))


def test_a_named_measure_pulls_its_canonical_definition():
    hint = ob.metric_hint("what is our ghosting rate this quarter")
    assert "ghosting rate" in hint
    assert "Interview_Outcome__c = 'Ghosted'" in hint
    # The denominator is the whole reason this metric is defined centrally.
    assert "nullif" in hint


def test_a_bare_common_word_matches_nothing():
    """"rate" appears in half the metrics; matching on it would inject three
    unrelated definitions into every pricing question."""
    assert ob.match_metrics("what is the rate") == []


def test_an_unrelated_question_adds_no_grounding_noise():
    assert ob.metric_hint("write me a python function") == ""


def test_interview_metric_excludes_initial_calls():
    """33,147 interview rows, 5,566 of which are Initial Calls. Counting the
    object is not counting interviews."""
    hint = ob.metric_hint("how many interviews did we run last month")
    assert "rt.Name = 'Interview'" in hint
    assert "Initial Call" in hint


def test_candidate_metric_filters_by_record_type_not_is_person_account():
    """The trap: the 259 Recruiter Accounts ARE person accounts, and every one
    carries a Candidate_Status__c. IsPersonAccount returns 551 active
    candidates where the record type returns 294."""
    hint = ob.metric_hint("how many active candidates do we have")
    assert "rt.Name = 'Person Account'" in hint
    assert "IsPersonAccount = 'true'" not in hint


def test_the_brief_warns_off_the_is_person_account_shortcut():
    assert "IsPersonAccount is NOT the candidate test" in ob.ORG_RULES


def test_the_brief_is_always_present_even_without_a_metric():
    grounding = ob.grounding_for("show me something about accounts")
    assert "Person Account" in grounding
    assert "Recruiter__c" in grounding


def test_grounding_adds_the_metric_when_one_matches():
    plain = ob.grounding_for("show me a list of records")
    with_metric = ob.grounding_for("what is our ghosting rate")
    assert len(with_metric) > len(plain)
    assert "canonical SQL" in with_metric


# ── The mechanics rule that silently corrupts answers ────────────────────────

def test_the_sql_prompt_demands_a_numeric_cast():
    """Every warehouse column is VARCHAR. Uncast, ORDER BY amount sorts as
    text and "top 10 by value" puts 999 above 27000 without erroring."""
    assert "TRY_CAST" in sql_engine._SQL_SYSTEM
    assert "VARCHAR" in sql_engine._SQL_SYSTEM
    assert re.search(r"ORDER BY.*TRY_CAST", sql_engine._SQL_SYSTEM, re.S)


def test_the_sql_prompt_forbids_plain_cast():
    """One unparseable value in 700k rows would abort the whole query."""
    assert "TRY_CAST, never CAST" in sql_engine._SQL_SYSTEM


def test_the_sql_prompt_still_carries_the_older_hard_won_rules():
    """Regression: the casting block is appended, not substituted."""
    assert "DAY-MONTH-YEAR" in sql_engine._SQL_SYSTEM
    assert "lowercase TEXT 'true'" in sql_engine._SQL_SYSTEM
    assert "ONE SELECT statement" in sql_engine._SQL_SYSTEM


def test_the_sql_stage_receives_the_org_brief():
    import inspect

    source = inspect.getsource(sql_engine._ask_sql)
    assert "grounding_for" in source


def test_the_answer_stage_is_told_to_state_its_population():
    import inspect

    source = inspect.getsource(sql_engine._narrative_messages)
    assert "ANSWER_RULES" in source
    assert "population" in ob.ANSWER_RULES


def test_the_answer_rules_refuse_to_read_out_credentials():
    assert "passwords" in ob.ANSWER_RULES
    assert "SSN" in ob.ANSWER_RULES


# ── The "oot mocks taken today" failure ──────────────────────────────────────
# One question exposed four defects at once: the dictionary offered a setup
# object, no metric matched, the type lookup was never joined, and CURRENT_DATE
# was the wrong day. Each is pinned here.

def test_oot_pulls_the_internal_assessment_metric():
    hint = ob.metric_hint("give me the list of the total oot mocks taken today")
    assert "Internal_Interview__c" in hint
    assert "Interview_Type__c" in hint
    assert "it.Name = 'OOT'" in hint


def test_the_assessment_metric_uses_the_column_that_has_data():
    """Date__c is null on every row; filtering it returns a convincing zero."""
    hint = ob.metric_hint("how many oot mocks today")
    assert "Scheduled_Date__c" in hint
    assert "Date__c is empty" in hint


def test_the_assessment_metric_warns_off_mock_status():
    hint = ob.metric_hint("oot mocks")
    assert "Mock_Status__c" in hint and "not the" in hint


def test_today_means_the_business_day_not_the_server_day():
    """The server is UTC and the business is IST, so for several hours every
    evening CURRENT_DATE is yesterday's work."""
    assert ob.BUSINESS_TIMEZONE == "Asia/Kolkata"
    assert "Asia/Kolkata" in ob.SQL_HARD_RULES
    assert "CURRENT_DATE is" in ob.SQL_HARD_RULES


def test_the_brief_explains_that_assessment_type_is_a_lookup():
    assert "LOOKUP to the Interview_Type__c table" in ob.ORG_RULES
    assert "OOT" in ob.ORG_RULES


def test_a_request_for_a_list_must_return_rows_not_a_count():
    """The warehouse answered `12` and the live API answered `SELECT count()`.
    The rule has to reach BOTH dialects, so it is defined once and shared."""
    assert "ask for ROWS" in ob.LIST_NOT_COUNT
    assert "ask for ROWS" in ob.SQL_HARD_RULES
    assert "ask for ROWS" in ob.SOQL_TRANSLATION
    assert "ask for ROWS" in ob.grounding_for("list the oot mocks", dialect="soql")


# ── The live-Salesforce fallback ─────────────────────────────────────────────
# The warehouse is write-locked by the sync worker for roughly half of all
# read attempts, so the "fallback" path carries a large share of real traffic.
# Ungrounded, it answered "0 OOT mocks today" from Program_Version__c.

def test_the_live_path_gets_the_same_org_brief():
    grounding = ob.grounding_for("oot mocks taken today", dialect="soql")
    assert "Internal_Interview__c" in grounding
    assert "OOT" in grounding


def test_the_soql_dialect_translates_joins_to_relationships():
    grounding = ob.grounding_for("oot mocks today", dialect="soql")
    assert "Interview_Type__r.Name = 'OOT'" in grounding
    assert "There is no JOIN" in grounding


def test_the_soql_dialect_forbids_the_warehouse_only_casting():
    """TRY_CAST and `now() AT TIME ZONE` are syntax errors over the API."""
    grounding = ob.grounding_for("oot mocks today", dialect="soql")
    assert "Do NOT cast" in grounding
    assert "Do NOT use the literal TODAY" in grounding


def test_the_sql_dialect_does_not_carry_the_soql_translation():
    assert "There is no JOIN" not in ob.grounding_for("oot mocks today")


def test_the_live_engine_actually_injects_the_grounding():
    import inspect

    from app.engines import live_sf

    source = inspect.getsource(live_sf.write_soql)
    assert 'grounding_for(question, dialect="soql")' in source


def test_the_agent_falls_back_to_live_instead_of_leaking_a_lock_error():
    """High mode surfaced `Could not set lock on file ... PID 0` verbatim."""
    import inspect

    from app.engines import agent

    source = inspect.getsource(agent)
    assert "WarehouseBusy" in source
    assert "fetch_live" in source


def test_the_schema_read_waits_for_the_lock():
    """It connected once and gave up, which is what sent traffic to live."""
    import inspect

    from app.core import schema_cache

    source = inspect.getsource(schema_cache.SchemaCache._load)
    assert "LOCK_WAIT_SECONDS" in source
    assert schema_cache.LOCK_WAIT_SECONDS >= 4.0


def test_the_live_path_is_given_the_business_date_explicitly():
    """Salesforce's TODAY resolves in the integration user's timezone. Asked
    for today's OOT mocks it returned the previous day's records while the
    warehouse returned the current day's — the same question, two days."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    expected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    assert ob.business_today() == expected
    grounding = ob.grounding_for("oot mocks today", dialect="soql")
    assert expected in grounding
    assert "rather than TODAY" in grounding


# ── Training domain ──────────────────────────────────────────────────────────
# Sourced from the Training Module Handbook and then checked against the
# warehouse, which disagreed with the handbook in three places.

def test_training_rules_only_load_for_training_questions():
    """ORG_RULES is on every question; domain rules must not be."""
    assert "Slot" in ob.domain_rules_for("how many candidates in slot 128")
    assert ob.domain_rules_for("how much have we invoiced this month") == ""


def test_slot_is_the_word_for_cohort():
    """Nobody in this org says "cohort" — every Cohort__c.Name is "Slot NNN"."""
    rules = ob.domain_rules_for("show me the last 10 slots")
    assert "Slot\" IS Cohort__c" in rules or '"Slot" IS Cohort__c' in rules


def test_slots_must_sort_numerically():
    """"Slot 11" sorts before "Slot 117" as text, so "last 10 slots" silently
    returns the wrong ten."""
    rules = ob.domain_rules_for("last 10 slots")
    assert "regexp_extract" in rules
    hint = ob.metric_hint("candidates per slot")
    assert "regexp_extract" in hint


def test_training_sessions_must_filter_purpose():
    """Session__c also holds Internal Interview and Resume Understanding rows —
    2,437 training of 2,790 total."""
    hint = ob.metric_hint("how many training sessions did we hold")
    assert "Purpose__c = 'Training'" in hint
    rules = ob.domain_rules_for("training sessions")
    assert "Purpose__c" in rules


def test_attendance_unions_the_group_session_riders():
    """Group sessions put the master on Session__c and everyone else on
    Session_Attendee__c."""
    hint = ob.metric_hint("what was the attendance")
    assert "Session_Attendee__c" in hint
    assert "UNION ALL" in hint


def test_a_mock_is_identified_by_type_not_by_training_link():
    """The handbook says Candidate_Training__c populated = Mock. In the data
    every OOT and Intake record has it empty, so that rule would drop the
    entire OOT population the user asks about by name."""
    rules = ob.domain_rules_for("how many mocks today")
    assert "NOT by" in rules and "Candidate_Training__c" in rules
    assert "OOT" in rules


def test_absent_and_skipped_are_not_the_same_miss():
    rules = ob.domain_rules_for("how many training modules were missed")
    assert "Absent" in rules and "Skipped" in rules
    assert "TRAINER" in rules


def test_a_dropped_training_keeps_its_earlier_history():
    rules = ob.domain_rules_for("dropped trainings")
    assert "Drop_Date__c" in rules


def test_published_is_not_a_valid_program_version_filter():
    """One PV is Published; the programme actually being delivered is Draft."""
    rules = ob.domain_rules_for("which training programs are running")
    assert "Published" in rules


def test_the_training_report_uses_the_dashboard_the_team_already_reads():
    template = ob.report_template_for("build me a training report with charts")
    assert "Retention vs drop" in template
    assert "Trainer workload" in template
    assert template.count('"chart": true') >= 1
    assert template.count("Render as a") == 9


def test_an_unrelated_report_gets_no_template():
    assert ob.report_template_for("write me a poem") == ""


def test_the_report_planner_receives_the_template():
    import inspect

    from app.engines import report

    assert "report_template_for" in inspect.getsource(report.run_report_engine)


def test_every_new_training_metric_is_reachable_by_its_business_word():
    for phrase, expected in [
        ("training sessions", "training sessions delivered"),
        ("attendance", "session attendance"),
        ("deliverables", "deliverable status mix"),
        ("pass rate", "deliverable pass rate"),
        ("mock outcomes", "mock outcomes"),
        ("trainer workload", "trainer workload"),
        ("retention rate", "training retention"),
        ("retraining", "retraining ratio"),
        ("module", "module progress"),
    ]:
        names = [m["name"] for m in ob.match_metrics(phrase)]
        assert expected in names, f"{phrase!r} did not match {expected!r}: {names}"


# ── "give me details for <person>'s training" ────────────────────────────────
# Routed to rag, text-searched record bodies, and reported that a candidate
# with five training enrolments had no training details.

def test_a_person_is_never_matched_by_an_equals():
    """Stored as "Rakshith n/a Bodakuntla"; typed as "Rakshit Bodakuntla".
    Name = '...' returns 0 rows; ILIKE on the surname returns the person."""
    assert "NEVER match a person by an equals" in ob.ORG_RULES
    assert "ILIKE" in ob.ORG_RULES
    assert "n/a" in ob.ORG_RULES


def test_a_name_miss_is_reported_as_a_name_miss():
    """"No records for this person" and "no such person" are different
    answers, and only one of them was true."""
    assert "no candidate matches that name" in ob.ORG_RULES
    assert "report their records as absent" in ob.ORG_RULES


def test_asking_for_a_candidates_training_pulls_the_profile():
    hint = ob.metric_hint("give me details for Rakshith's training")
    assert "candidate training profile" in hint
    assert "ILIKE" in hint


def test_the_profile_expects_several_enrolments():
    """This candidate has five — dropped, retrained and current. Answering
    with one of them is a wrong answer."""
    hint = ob.metric_hint("training details for this candidate")
    assert "SEVERAL (dropped, retrained, current)" in hint
    assert "say which is current" in hint


# ── "give me a report for the above one data" ────────────────────────────────
# Produced a day-by-day report for ONE of five enrolments, off the first 30 of
# 227 rows, after a query failed on a mistyped object name.

def test_the_module_object_name_breaks_the_convention():
    """Candidate_TrainingStep__c does not exist. The real name has no
    underscore between Candidate and Training, and getting it wrong failed the
    query, which was then reported to the user as missing data."""
    rules = ob.domain_rules_for("training modules for this candidate")
    assert "CandidateTrainingStep__c" in rules
    assert "NO underscore" in rules


def test_a_profile_must_aggregate_rather_than_return_hundreds_of_rows():
    """Only 30 rows reach the answer stage. A 227-row profile query gets
    summarised from the first 30 — one enrolment presented as the whole
    history."""
    assert "only the first 30 rows reach" in ob.LIST_NOT_COUNT
    assert "aggregate" in ob.LIST_NOT_COUNT


def test_the_profile_metric_is_one_row_per_enrolment():
    hint = ob.metric_hint("give me details for this candidate's training")
    assert "ONE ROW PER ENROLMENT" in hint
    assert "GROUP BY" in hint
    assert "CandidateTrainingStep__c" in hint


def test_the_profile_metric_refuses_to_report_only_the_first_enrolment():
    hint = ob.metric_hint("training details for the candidate")
    assert "reporting the first is a wrong answer" in hint


def test_a_named_person_gets_the_candidate_report_template():
    template = ob.report_template_for("give me a training report for Rakshith")
    assert "Enrolments" in template
    assert "Mocks" in template


def test_the_person_template_is_not_hardcoded_to_one_name():
    """It must fire on any name, not a list of the ones we have seen."""
    assert ob.names_a_person("report for Priya") is True
    assert ob.report_template_for("training report for Priya Sharma") != ""
    assert "Enrolments" in ob.report_template_for("dashboard for Arjun's training")


def test_object_words_are_not_mistaken_for_people():
    assert ob.names_a_person("how many Training sessions in Slot 134") is False
    assert ob.names_a_person("give me a report") is False


def test_an_org_wide_training_report_still_gets_the_dashboard_template():
    """The person template must not swallow the org-wide one."""
    template = ob.report_template_for("build me a training report with charts")
    assert "Trainer workload" in template


def test_lookups_are_resolved_to_names_not_left_as_ids():
    """A candidate report came back with Program, Cohort and Trainer as raw
    18-character Ids. The reader cannot tell which programme that is."""
    assert "NEVER put a raw 18-character Salesforce Id" in ob.SQL_HARD_RULES
    for lookup in ("Program__c", "Cohort__c", "Interview_Type__c",
                   "Assigned_Trainer__c", "RecordTypeId"):
        assert lookup in ob.SQL_HARD_RULES, lookup


# ── Reports and dashboards must DRAW, not just describe ──────────────────────

def test_asking_for_a_dashboard_counts_as_asking_for_charts():
    """"dashboard" was not in the trigger list, so a dashboard request came
    back as a table of numbers."""
    from app.core import chart_decision

    assert chart_decision.explicit_chart_request("give me a dashboard for Rakshith")
    assert chart_decision.explicit_chart_request("training dashboard")
    assert chart_decision.explicit_chart_request("show me a graphical view")


def test_ordinary_prose_still_does_not_trigger_a_chart():
    from app.core import chart_decision

    assert not chart_decision.explicit_chart_request("how many mocks today")
    assert not chart_decision.explicit_chart_request("list the active candidates")


def test_report_templates_mandate_charts_on_every_section():
    for question in ("training report with charts",
                     "training report for Rakshith"):
        template = ob.report_template_for(question)
        assert template, question
        assert '"chart": true' in template
        assert "bare table is a failure" in template


def test_report_sections_ask_for_a_chartable_shape():
    """A section returning five wide rows has nothing to plot. Each must yield
    a category column and a numeric count."""
    template = ob.report_template_for("training report for Rakshith")
    assert "as the category" in template
    assert "as the value" in template


def test_every_templated_chart_type_can_actually_be_rendered():
    """funnel is table-only in reports; a template asking for one would
    silently degrade every time."""
    from app.core.charts_png import PNG_SUPPORTED

    for template in ob.REPORT_TEMPLATES:
        for title, _instruction, chart in template["sections"]:
            assert chart in PNG_SUPPORTED, f"{template['name']}/{title}: {chart}"


def test_a_canonical_definition_does_not_override_the_questions_own_filter():
    """A per-candidate step copied the org-wide metric SQL verbatim and
    charted 1,402 modules — the whole org — under one candidate's name."""
    hint = ob.metric_hint("module progress for this candidate")
    assert "must be added ON TOP" in hint
    assert "worse than an error because it looks right" in hint


def test_every_candidate_report_section_repeats_the_person_filter():
    template = ob.report_template_for("training report for Rakshith")
    # One per section; the filter is the thing that gets dropped.
    assert template.count("ILIKE") == 5


def test_empty_model_output_is_a_retry_not_a_routing_decision():
    """A 41k-token prompt made the model spend its whole budget reasoning and
    emit no statement. That read as "no FROM" -> NoSuchTable -> ask live
    Salesforce, which answered off whatever object the dictionary suggested."""
    import inspect

    from app.engines import sql as sql_engine

    assert hasattr(sql_engine, "EmptySql")
    source = inspect.getsource(sql_engine.generate_and_run_sql)
    assert "EmptySql" in source
    assert "no SQL statement" in source
    # And it must not be answered from live Salesforce.
    run = inspect.getsource(sql_engine.run_sql_engine)
    assert "except EmptySql" in run


def test_the_session_host_is_the_trainer():
    """Session__c.Host_User__c is named for User and points at Recruiter__c —
    2,781 rows match Recruiter__c and none match User."""
    hint = ob.metric_hint("trainer workload")
    assert "Host_User__c" in hint
    assert "Purpose__c = 'Training'" in hint
    rules = ob.domain_rules_for("who hosted the most sessions")
    assert "Host_User__c" in rules and "Recruiter__c" in rules


def test_sessions_hosted_and_candidates_assigned_are_different_measures():
    names = [m["name"] for m in ob.METRICS]
    assert "trainer workload" in names
    assert "trainings assigned per trainer" in names


def test_a_named_programme_must_be_filtered_on():
    """"Sessions for the interview readiness training yesterday" returned all
    43 training sessions that day instead of the 24 in that programme."""
    rules = ob.domain_rules_for("sessions for the interview readiness training")
    assert "Program__c.Name ILIKE" in rules
    assert "Interview Readiness Training" in rules
    tables = ob.tables_for("interview readiness training sessions yesterday")
    assert "Program__c" in tables and "Session__c" in tables


def test_optional_lookups_must_be_left_joined():
    """"Sessions for the interview readiness training yesterday" returned the
    right 24 rows until `JOIN Cohort__c` was added to show the slot name.
    Those trainings have no slot, so the answer silently became 0."""
    assert "LEFT JOIN for any" in ob.SQL_HARD_RULES
    assert "silently deletes every" in ob.SQL_HARD_RULES
