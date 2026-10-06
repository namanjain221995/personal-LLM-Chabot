"""Result interpretation and grounded answer generation.

The model is a stub that returns whatever a test hands it. That is the point:
what is under test is whether an invented name, a changed number or a
misstated total gets through, and a real model would produce a correct answer
most of the time and hide exactly the case that matters.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from answer import (AnswerConfig, AnswerService, FreshnessPolicy,
                    GroundingCode, ResultType, build_context, build_fallback,
                    classify_result, extract_facts, render,
                    validate_grounded_answer)
from answer.models import AnswerDetail, AnswerFailure, GroundedAnswer


# -- fixtures ---------------------------------------------------------------
class Freshness:
    def __init__(self, status="fresh", age_minutes=17.0,
                 last_sync_at="2026-09-25T16:07:06+00:00"):
        self.status = status
        self.age_minutes = age_minutes
        self.last_sync_at = last_sync_at


class Result:
    def __init__(self, rows=None, *, success=True, total_count=None,
                 truncated=False, freshness=None):
        self.success = success
        self.rows = rows or []
        self.row_count = len(self.rows)
        self.total_count = (total_count if total_count is not None
                            else (self.row_count if not truncated else None))
        self.truncated = truncated
        self.freshness = freshness if freshness is not None else Freshness()
        self.error = None
        self.error_detail = ""
        self.sql = "SELECT 1"


class SelectItem:
    def __init__(self, output_alias, aggregate=None):
        self.output_alias = output_alias
        self.aggregate = aggregate


class Operation:
    def __init__(self, value):
        self.value = value


class Plan:
    def __init__(self, *, operation="retrieve", select=None, group_by=None):
        self.operation = Operation(operation)
        self.select = select or []
        self.group_by = group_by or []


class Grounded:
    def __init__(self, primary_object="Internal_Interview__c"):
        self.primary_object = primary_object


class StubModel:
    """Returns a queue of payloads, so a regeneration can differ from the first."""

    def __init__(self, *payloads, model="Qwen/Qwen3.6-35B-A3B-NVFP4"):
        self.model = model
        self.endpoint = "http://vllm:8000/v1"
        self.payloads = list(payloads)
        self.calls: list[list[dict[str, str]]] = []

    def complete_json(self, messages):
        self.calls.append(messages)
        payload = self.payloads.pop(0) if self.payloads else None
        if isinstance(payload, Exception):
            raise payload
        return payload, {"model": self.model, "endpoint": self.endpoint,
                         "duration_ms": 42, "completion_tokens": 80,
                         "finish_reason": "stop"}


TWO_CANDIDATES = [
    {"candidate": "John Smith", "email": "john@example.com"},
    {"candidate": "Sarah Lee", "email": "sarah@example.com"},
]

GOOD_ANSWER = {
    "answer_type": "record_list",
    "summary": "2 matching candidates were found.",
    "details": [{"text": "John Smith — john@example.com"},
                {"text": "Sarah Lee — sarah@example.com"}],
    "notes": [],
    "freshness_note": None,
}


def service(*payloads, config=None):
    return AnswerService(config=config or AnswerConfig(),
                         client=StubModel(*payloads))


def interpret(result, plan=None, grounded=None, question="a question"):
    plan = plan or Plan()
    result_type = classify_result(result, grounded, plan)
    return extract_facts(result, result_type, question=question,
                         grounded_plan=grounded or Grounded(), query_plan=plan)


# -- classification (spec §5) -----------------------------------------------
class PlanPropagationTest(unittest.TestCase):
    def test_the_result_carries_the_plan_the_classifier_needs(self):
        # A COUNT returns exactly one row. Without the plan it is
        # indistinguishable from a one-record SELECT, and the answer would
        # describe a number as if it were a record.
        plan = Plan(operation="count",
                    select=[SelectItem("record_count", "COUNT")])
        one_row = Result([{"record_count": 491}])
        self.assertIs(classify_result(one_row, None, plan), ResultType.COUNT)
        self.assertIs(classify_result(one_row, None, None),
                      ResultType.SINGLE_RECORD)


class ClassificationTest(unittest.TestCase):
    def test_a_failed_query_is_an_error_result(self):
        self.assertIs(classify_result(Result(success=False)),
                      ResultType.ERROR_RESULT)

    def test_no_rows_is_empty(self):
        self.assertIs(classify_result(Result([]), None, Plan()),
                      ResultType.EMPTY_RESULT)

    def test_one_row_is_a_single_record(self):
        self.assertIs(classify_result(Result(TWO_CANDIDATES[:1]), None, Plan()),
                      ResultType.SINGLE_RECORD)

    def test_several_rows_are_a_multi_record(self):
        self.assertIs(classify_result(Result(TWO_CANDIDATES), None, Plan()),
                      ResultType.MULTI_RECORD)

    def test_a_count_of_zero_is_still_a_count(self):
        plan = Plan(operation="count",
                    select=[SelectItem("record_count", "COUNT")])
        self.assertIs(classify_result(Result([{"record_count": 0}]), None, plan),
                      ResultType.COUNT)

    def test_an_aggregate_without_groups_is_a_single_aggregate(self):
        plan = Plan(operation="aggregate",
                    select=[SelectItem("average_score", "AVG")])
        self.assertIs(classify_result(Result([{"average_score": 6.5}]),
                                      None, plan),
                      ResultType.SINGLE_AGGREGATE)

    def test_groups_make_a_grouped_aggregate(self):
        plan = Plan(operation="aggregate", group_by=[SelectItem("Status__c")],
                    select=[SelectItem("Status__c"),
                            SelectItem("record_count", "COUNT")])
        self.assertIs(classify_result(Result([{"Status__c": "Assigned",
                                               "record_count": 12}]),
                                      None, plan),
                      ResultType.GROUPED_AGGREGATE)

    def test_truncation_outranks_the_row_count(self):
        result = Result(TWO_CANDIDATES, truncated=True)
        self.assertIs(classify_result(result, None, Plan()),
                      ResultType.TRUNCATED_RESULT)


# -- deterministic facts (spec §7, §26) -------------------------------------
class FactsTest(unittest.TestCase):
    def test_a_count_becomes_a_verified_number(self):
        plan = Plan(operation="count",
                    select=[SelectItem("record_count", "COUNT")])
        interpreted = interpret(Result([{"record_count": 8}]), plan)
        self.assertEqual(interpreted.aggregates["matching_record_count"], 8)
        self.assertEqual(interpreted.total_count, 8)
        self.assertIn(8.0, interpreted.supported_numbers)

    def test_group_arithmetic_happens_before_the_model(self):
        plan = Plan(operation="aggregate", group_by=[SelectItem("status")],
                    select=[SelectItem("status"),
                            SelectItem("record_count", "COUNT")])
        rows = [{"status": "Assigned", "record_count": 12},
                {"status": "Unassigned", "record_count": 8}]
        interpreted = interpret(Result(rows), plan)
        derived = interpreted.derived_facts
        self.assertEqual(derived["difference"], 4)
        self.assertEqual(derived["total"], 20)
        self.assertEqual(derived["largest"], {"group": "Assigned", "value": 12})
        self.assertEqual(derived["smallest"],
                         {"group": "Unassigned", "value": 8})
        shares = {s["group"]: s["percentage"] for s in derived["shares"]}
        self.assertEqual(shares, {"Assigned": 60.0, "Unassigned": 40.0})

    def test_two_measures_derive_no_single_difference(self):
        plan = Plan(operation="aggregate", group_by=[SelectItem("status")],
                    select=[SelectItem("status"),
                            SelectItem("record_count", "COUNT"),
                            SelectItem("average_score", "AVG")])
        rows = [{"status": "A", "record_count": 3, "average_score": 7.0}]
        interpreted = interpret(Result(rows), plan)
        # No honest meaning for "the difference" with two measures, so none
        # is invented.
        self.assertEqual(interpreted.derived_facts, {})

    def test_nulls_dates_and_booleans_are_normalised_for_the_model(self):
        rows = [{"candidate": "John Smith", "email": None,
                 "scheduled_date": date(2026, 9, 26), "active": True,
                 "created": datetime(2026, 9, 26, 14, 30)}]
        interpreted = interpret(Result(rows))
        record = interpreted.records[0]
        self.assertEqual(record["email"],
                         {"raw": None, "display": "unavailable",
                          "available": False})
        self.assertEqual(record["scheduled date"]["display"],
                         "September 26, 2026")
        self.assertEqual(record["active"]["display"], "Yes")
        self.assertEqual(record["created"]["display"],
                         "September 26, 2026 at 2:30 PM")

    def test_the_filters_that_ran_are_evidence_and_may_be_restated(self):
        # The value came from the question, but it is supported because the
        # query actually filtered on it. Without this an empty answer cannot
        # name what it searched for.
        grounded = Grounded()
        grounded.filter_mappings = [
            {"field": "Round__c", "operator": "equals", "value": "Round 99",
             "business_concept": "round"}]
        interpreted = interpret(Result([]), grounded=grounded)
        self.assertEqual(interpreted.applied_filters,
                         [{"attribute": "round", "condition": "is",
                           "value": "Round 99"}])
        answer = GroundedAnswer(
            answer_type="empty",
            summary="No internal interviews were found where round is "
                    "Round 99.")
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_a_filter_value_that_never_ran_is_still_unsupported(self):
        interpreted = interpret(Result([]), grounded=Grounded())
        answer = GroundedAnswer(
            answer_type="empty",
            summary="No internal interviews were found for Round 99.")
        report = validate_grounded_answer(answer, interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.UNSUPPORTED_NUMBER.value, report.codes())

    def test_a_column_name_reaches_the_model_as_business_words(self):
        rows = [{"Scheduled_Date__c": date(2026, 9, 26)}]
        interpreted = interpret(Result(rows))
        self.assertIn("Scheduled Date", interpreted.records[0])


# -- context (spec §8, §36, §37) ---------------------------------------------
class ContextTest(unittest.TestCase):
    def test_the_context_carries_no_sql_and_no_table_aliases(self):
        interpreted = interpret(Result(TWO_CANDIDATES))
        payload = build_context(interpreted, AnswerConfig()).as_dict()
        text = str(payload)
        self.assertNotIn("SELECT", text)
        self.assertNotIn('"t0"', text)
        self.assertIn("question", payload)
        self.assertIn("verified_facts", payload)

    def test_database_truncation_and_context_truncation_stay_separate(self):
        rows = [{"n": i} for i in range(40)]
        interpreted = interpret(Result(rows, truncated=True, total_count=284))
        config = AnswerConfig(max_rows_to_model=10)
        context = build_context(interpreted, config)
        self.assertTrue(context.model_context_truncated)
        self.assertTrue(context.database_result_truncated)
        self.assertEqual(context.rows_sent_to_model, 10)
        self.assertEqual(context.database_returned_rows, 40)
        self.assertEqual(context.payload["model_context"]["rows_shown"], 10)

    def test_an_unknown_total_is_absent_rather_than_null(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=None))
        facts = build_context(interpreted, AnswerConfig()).payload["verified_facts"]
        self.assertNotIn("matching_records", facts)
        self.assertTrue(facts["truncated"])

    def test_the_model_is_always_told_the_real_age_even_when_hidden(self):
        interpreted = interpret(Result(TWO_CANDIDATES))
        config = AnswerConfig(freshness=FreshnessPolicy(show_when_fresh=False))
        freshness = build_context(interpreted, config).payload["freshness"]
        self.assertEqual(freshness["age_minutes"], 17.0)
        self.assertFalse(freshness["include_in_answer"])
        self.assertEqual(freshness["source"],
                         "synchronised copy of Salesforce")

    def test_a_huge_result_is_cut_to_the_character_budget(self):
        rows = [{"candidate": f"Person Number {i}", "note": "x" * 200}
                for i in range(200)]
        interpreted = interpret(Result(rows))
        config = AnswerConfig(max_rows_to_model=200, max_context_characters=4000)
        context = build_context(interpreted, config)
        self.assertLessEqual(context.characters, 4000)
        self.assertTrue(context.model_context_truncated)


# -- grounding validation (spec §27-§32, §54-§57) ---------------------------
class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.interpreted = interpret(
            Result(TWO_CANDIDATES),
            question="Show candidate emails for unassigned mock interviews.")

    def _answer(self, **kwargs):
        base = {"answer_type": "record_list",
                "summary": "2 matching candidates were found.",
                "details": [AnswerDetail("John Smith — john@example.com"),
                            AnswerDetail("Sarah Lee — sarah@example.com")]}
        base.update(kwargs)
        return GroundedAnswer(**base)

    def test_a_faithful_answer_passes(self):
        self.assertTrue(validate_grounded_answer(self._answer(),
                                                 self.interpreted))

    def test_an_invented_person_fails(self):
        answer = self._answer(
            summary="John Smith and Robert Brown matched the request.",
            details=[])
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.UNSUPPORTED_RECORD.value, report.codes())
        self.assertIn("Robert Brown", str(report.as_dict()))

    def test_a_changed_number_fails(self):
        plan = Plan(operation="count",
                    select=[SelectItem("record_count", "COUNT")])
        interpreted = interpret(Result([{"record_count": 8}]), plan)
        answer = GroundedAnswer(answer_type="count",
                                summary="There are 9 matching interviews.")
        report = validate_grounded_answer(answer, interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.UNSUPPORTED_NUMBER.value, report.codes())

    def test_the_exact_count_passes(self):
        plan = Plan(operation="count",
                    select=[SelectItem("record_count", "COUNT")])
        interpreted = interpret(Result([{"record_count": 8}]), plan)
        answer = GroundedAnswer(
            answer_type="count",
            summary="There are 8 unassigned mock interviews scheduled tomorrow.")
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_a_fabricated_email_fails(self):
        answer = self._answer(details=[
            AnswerDetail("John Smith — john@example.com"),
            AnswerDetail("Sarah Lee — sarah.lee@corporate.example.org")])
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.UNSUPPORTED_EMAIL.value, report.codes())

    def test_an_invented_date_fails(self):
        answer = self._answer(
            notes=["All interviews are scheduled for October 3, 2026."])
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.UNSUPPORTED_DATE.value, report.codes())

    def test_a_date_that_is_in_the_result_passes(self):
        interpreted = interpret(Result(
            [{"candidate": "John Smith", "scheduled": date(2026, 9, 26)}]))
        answer = GroundedAnswer(
            answer_type="summary",
            summary="John Smith is scheduled for September 26, 2026.")
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_more_detail_lines_than_records_fails(self):
        answer = self._answer(details=[
            AnswerDetail("John Smith — john@example.com"),
            AnswerDetail("Sarah Lee — sarah@example.com"),
            AnswerDetail("John Smith — john@example.com")])
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.INVENTED_RECORD_COUNT.value, report.codes())

    def test_asserting_a_total_on_an_unknown_truncated_result_fails(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=None))
        answer = GroundedAnswer(answer_type="record_list",
                                summary="There are 2 matching records.")
        report = validate_grounded_answer(answer, interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.TRUNCATION_MISSTATED.value, report.codes())

    def test_describing_a_truncated_result_as_partial_passes(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=None))
        answer = GroundedAnswer(
            answer_type="record_list",
            summary="Showing 2 matching records. Further records may exist.",
            details=[AnswerDetail("John Smith — john@example.com"),
                     AnswerDetail("Sarah Lee — sarah@example.com")])
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_a_known_total_may_be_stated(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=284))
        answer = GroundedAnswer(
            answer_type="record_list",
            summary="There are 284 matching records. Showing the first 2.",
            details=[AnswerDetail("John Smith — john@example.com"),
                     AnswerDetail("Sarah Lee — sarah@example.com")])
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_a_wrong_data_age_fails(self):
        answer = self._answer(
            freshness_note="Data was last synchronised 5 minutes ago.")
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.FRESHNESS_MISSTATED.value, report.codes())

    def test_the_measured_age_passes(self):
        answer = self._answer(
            freshness_note="Data was last synchronised 17 minutes ago.")
        self.assertTrue(validate_grounded_answer(answer, self.interpreted))

    def test_calling_the_replica_live_salesforce_fails(self):
        answer = self._answer(
            notes=["Salesforce currently shows these two candidates."])
        report = validate_grounded_answer(answer, self.interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.LIVE_DATA_CLAIMED.value, report.codes())

    def test_explaining_an_empty_result_fails(self):
        interpreted = interpret(Result([]),
                                question="Show unassigned mock interviews.")
        answer = GroundedAnswer(
            answer_type="empty",
            summary="No matching records were found because no recruiters "
                    "are available.")
        report = validate_grounded_answer(answer, interpreted)
        self.assertFalse(report)
        self.assertIn(GroundingCode.EMPTY_RESULT_EXPLAINED.value,
                      report.codes())

    def test_stating_an_empty_result_plainly_passes(self):
        interpreted = interpret(Result([]))
        answer = GroundedAnswer(
            answer_type="empty",
            summary="No matching unassigned mock interviews were found for "
                    "tomorrow.")
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_an_empty_answer_is_refused(self):
        report = validate_grounded_answer(GroundedAnswer(summary=""),
                                          self.interpreted)
        self.assertFalse(report)


# -- the whole path (spec §33, §34, §58) -------------------------------------
class ServiceTest(unittest.TestCase):
    def test_a_grounded_first_answer_is_returned_as_written(self):
        svc = service(GOOD_ANSWER)
        final = svc.answer("Show candidate emails.", Result(TWO_CANDIDATES),
                           grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.grounded)
        self.assertFalse(final.regenerated)
        self.assertFalse(final.fallback_used)
        self.assertEqual(final.attempts, 1)
        self.assertEqual(final.model, "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(final.model_role, "main")
        self.assertIn("John Smith — john@example.com", final.text)

    def test_every_result_type_reaches_the_main_model(self):
        cases = [
            (Result([]), Plan()),
            (Result(TWO_CANDIDATES[:1]), Plan()),
            (Result(TWO_CANDIDATES), Plan()),
            (Result([{"record_count": 8}]),
             Plan(operation="count",
                  select=[SelectItem("record_count", "COUNT")])),
            (Result([{"average_score": 6.5}]),
             Plan(operation="aggregate",
                  select=[SelectItem("average_score", "AVG")])),
            (Result([{"status": "Assigned", "record_count": 12}]),
             Plan(operation="aggregate", group_by=[SelectItem("status")],
                  select=[SelectItem("status"),
                          SelectItem("record_count", "COUNT")])),
            (Result(TWO_CANDIDATES, truncated=True), Plan()),
        ]
        for result, plan in cases:
            with self.subTest(rows=result.row_count):
                model = StubModel({"answer_type": "summary",
                                   "summary": "A result was returned."})
                svc = AnswerService(config=AnswerConfig(), client=model)
                svc.answer("q", result, grounded_plan=Grounded(),
                           query_plan=plan)
                # No template shortcut for any type: the model was called.
                self.assertEqual(len(model.calls), 1)

    def test_an_ungrounded_answer_is_regenerated_once(self):
        bad = {"answer_type": "record_list",
               "summary": "3 matching candidates were found.",
               "details": []}
        svc = service(bad, GOOD_ANSWER)
        final = svc.answer("q", Result(TWO_CANDIDATES),
                           grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.regenerated)
        self.assertTrue(final.grounded)
        self.assertFalse(final.fallback_used)
        self.assertEqual(final.attempts, 2)

    def test_the_second_prompt_names_the_unsupported_claims(self):
        bad = {"answer_type": "record_list",
               "summary": "3 matching candidates were found.",
               "details": []}
        model = StubModel(bad, GOOD_ANSWER)
        AnswerService(config=AnswerConfig(), client=model).answer(
            "q", Result(TWO_CANDIDATES), grounded_plan=Grounded(),
            query_plan=Plan())
        second = model.calls[1][-1]["content"]
        self.assertIn("UNSUPPORTED_NUMBER", second)
        self.assertIn("3 is not a value in the result", second)

    def test_two_failures_fall_back_and_never_return_the_invalid_answer(self):
        bad = {"answer_type": "record_list",
               "summary": "3 matching candidates were found, including "
                          "Robert Brown.",
               "details": []}
        svc = service(bad, bad)
        final = svc.answer("q", Result(TWO_CANDIDATES),
                           grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.fallback_used)
        self.assertFalse(final.grounded)
        self.assertNotIn("Robert Brown", final.text)
        self.assertNotIn("3 matching", final.text)
        self.assertIn("2 matching", final.text)
        fallbacks = [e for e in final.trace
                     if e.stage == "DETERMINISTIC_SAFETY_FALLBACK_USED"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0].status, "failed")

    def test_a_model_that_cannot_be_reached_falls_back(self):
        svc = AnswerService(config=AnswerConfig(),
                            client=StubModel(RuntimeError("connection refused")))
        final = svc.answer("q", Result(TWO_CANDIDATES),
                           grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.fallback_used)
        self.assertIn("2 matching", final.text)

    def test_a_failed_query_is_not_sent_to_the_model(self):
        model = StubModel(GOOD_ANSWER)
        final = AnswerService(config=AnswerConfig(), client=model).answer(
            "q", Result(success=False))
        self.assertEqual(model.calls, [])
        self.assertEqual(final.result_type, "ERROR_RESULT")
        self.assertEqual(final.text, "")

    def test_the_freshness_note_is_dropped_when_policy_hides_it(self):
        payload = dict(GOOD_ANSWER,
                       freshness_note="Data was last synchronised 17 minutes ago.")
        config = AnswerConfig(freshness=FreshnessPolicy(show_when_fresh=False))
        final = service(payload, config=config).answer(
            "q", Result(TWO_CANDIDATES), grounded_plan=Grounded(),
            query_plan=Plan())
        self.assertTrue(final.grounded)
        self.assertNotIn("synchronised", final.text)

    def test_the_freshness_note_survives_when_the_data_is_stale(self):
        payload = dict(GOOD_ANSWER,
                       freshness_note="Data was last synchronised 90 minutes ago.")
        result = Result(TWO_CANDIDATES,
                        freshness=Freshness(status="stale", age_minutes=90.0))
        final = service(payload).answer("q", result, grounded_plan=Grounded(),
                                        query_plan=Plan())
        self.assertTrue(final.grounded)
        self.assertIn("90 minutes ago", final.text)

    def test_the_trace_names_the_main_model_on_every_generation(self):
        final = service(GOOD_ANSWER).answer(
            "q", Result(TWO_CANDIDATES), grounded_plan=Grounded(),
            query_plan=Plan())
        stages = [e.stage for e in final.trace]
        for expected in ("RESULT_INTERPRETATION_STARTED", "RESULT_CLASSIFIED",
                         "RESULT_FACTS_EXTRACTED", "ANSWER_CONTEXT_CREATED",
                         "MAIN_ANSWER_MODEL_STARTED",
                         "MAIN_ANSWER_MODEL_COMPLETED",
                         "ANSWER_GROUNDING_VALIDATION_STARTED",
                         "ANSWER_GROUNDING_VALIDATION_COMPLETED",
                         "FINAL_RESPONSE_CREATED"):
            self.assertIn(expected, stages)
        completed = next(e for e in final.trace
                         if e.stage == "MAIN_ANSWER_MODEL_COMPLETED")
        self.assertEqual(completed.details["model"],
                         "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(completed.details["model_role"], "main")

    def test_a_grounding_failure_traces_reason_codes_not_the_records(self):
        bad = {"answer_type": "record_list",
               "summary": "3 matching candidates were found.", "details": []}
        final = service(bad, bad).answer("q", Result(TWO_CANDIDATES),
                                         grounded_plan=Grounded(),
                                         query_plan=Plan())
        validation = [e for e in final.trace
                      if e.stage == "ANSWER_GROUNDING_VALIDATION_COMPLETED"]
        self.assertEqual(len(validation), 2)
        self.assertIn("UNSUPPORTED_NUMBER", validation[0].details["codes"])
        self.assertNotIn("john@example.com", str(validation[0].details))


# -- rendering and fallback (spec §34, §46) ----------------------------------
class RenderTest(unittest.TestCase):
    def test_details_render_as_a_numbered_list(self):
        answer = GroundedAnswer(
            answer_type="record_list",
            summary="2 matching candidates were found.",
            details=[AnswerDetail("John Smith — john@example.com"),
                     AnswerDetail("Sarah Lee — sarah@example.com")])
        self.assertEqual(render(answer),
                         "2 matching candidates were found.\n\n"
                         "1. John Smith — john@example.com\n"
                         "2. Sarah Lee — sarah@example.com")

    def test_a_fallback_for_an_unknown_total_never_asserts_one(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=None))
        answer = build_fallback(interpreted)
        self.assertIn("Further records may exist", answer.summary)
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_a_fallback_for_a_known_total_states_both_numbers(self):
        interpreted = interpret(Result(TWO_CANDIDATES, truncated=True,
                                       total_count=284))
        answer = build_fallback(interpreted)
        self.assertIn("284", answer.summary)
        self.assertIn("first 2", answer.summary)
        self.assertTrue(validate_grounded_answer(answer, interpreted))

    def test_every_fallback_passes_its_own_grounding_check(self):
        cases = [
            interpret(Result([]), Plan()),
            interpret(Result(TWO_CANDIDATES), Plan()),
            interpret(Result([{"record_count": 8}]),
                      Plan(operation="count",
                           select=[SelectItem("record_count", "COUNT")])),
            interpret(Result([{"status": "Assigned", "record_count": 12},
                              {"status": "Unassigned", "record_count": 8}]),
                      Plan(operation="aggregate",
                           group_by=[SelectItem("status")],
                           select=[SelectItem("status"),
                                   SelectItem("record_count", "COUNT")])),
        ]
        for interpreted in cases:
            with self.subTest(result_type=interpreted.result_type.value):
                answer = build_fallback(interpreted)
                report = validate_grounded_answer(answer, interpreted)
                self.assertTrue(report, report.as_dict())


if __name__ == "__main__":
    unittest.main()


class DedupeTest(unittest.TestCase):
    """Seen live: the model put the data-age sentence in both notes and freshness_note."""

    NOTE = "The data is from a synchronised copy of Salesforce and is considered stale (last synced 45.4 minutes ago)."

    def test_a_note_repeating_the_freshness_note_is_dropped(self):
        payload = dict(GOOD_ANSWER, notes=[self.NOTE], freshness_note=self.NOTE)
        result = Result(TWO_CANDIDATES,
                        freshness=Freshness(status="stale", age_minutes=45.4))
        final = service(payload).answer("q", result, grounded_plan=Grounded(),
                                        query_plan=Plan())
        self.assertEqual(final.text.count("synchronised copy"), 1)

    def test_the_copy_goes_too_when_policy_hides_freshness(self):
        # Hiding freshness_note must not leave its duplicate behind in notes.
        payload = dict(GOOD_ANSWER, notes=[self.NOTE], freshness_note=self.NOTE)
        final = service(payload).answer("q", Result(TWO_CANDIDATES),
                                        grounded_plan=Grounded(), query_plan=Plan())
        self.assertNotIn("synchronised copy", final.text)

    def test_case_spacing_and_end_punctuation_do_not_hide_a_repeat(self):
        from answer.render import dedupe
        answer = GroundedAnswer(summary="2 candidates were found.",
                                notes=["2  candidates were found", "Other note."])
        self.assertEqual(dedupe(answer).notes, ["Other note."])

    def test_different_sentences_are_kept(self):
        from answer.render import dedupe
        answer = GroundedAnswer(summary="s", notes=["One.", "Two."],
                                freshness_note="Three.")
        self.assertEqual(dedupe(answer).notes, ["One.", "Two."])


class ContextRowCountTest(unittest.TestCase):
    """Seen live: "showing 50" rejected although the context said rows_shown: 50."""

    def test_the_shown_row_count_is_a_supported_number_when_the_context_was_cut(self):
        rows = [{"candidate": f"Person {i}"} for i in range(81)]
        payload = {"answer_type": "record_list",
                   "summary": "81 matching records. Showing the first 50.",
                   "details": [{"text": f"Person {i}"} for i in range(50)]}
        config = AnswerConfig(max_rows_to_model=50)
        final = service(payload, config=config).answer(
            "q", Result(rows), grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.grounded, final.grounding.as_dict() if final.grounding else None)
        self.assertFalse(final.fallback_used)

    def test_an_invented_count_is_still_rejected(self):
        rows = [{"candidate": f"Person {i}"} for i in range(81)]
        payload = {"answer_type": "record_list",
                   "summary": "Showing 37 of the records.", "details": []}
        final = service(payload, payload, config=AnswerConfig(max_rows_to_model=50)).answer(
            "q", Result(rows), grounded_plan=Grounded(), query_plan=Plan())
        self.assertTrue(final.fallback_used)


class AuditFixesTest(unittest.TestCase):
    """Found verifying the main-model-every-stage refactor, 2026-10-03."""

    def test_groups_are_capped_like_records(self):
        from pipeline.results import TypedResult, to_interpreted
        typed = TypedResult(kind="grouped", returned_count=2744,
                            rows=[{"name": f"c{i}", "record_count": 1} for i in range(2744)])
        context = build_context(to_interpreted(typed, "q"), AnswerConfig(max_rows_to_model=50))
        self.assertEqual(len(context.payload["groups"]), 50)
        self.assertTrue(context.model_context_truncated)

    def test_a_cut_off_answer_is_malformed_not_unavailable(self):
        class Cut(StubModel):
            def complete_json(self, messages):
                return None, {"model": self.model, "duration_ms": 9,
                              "completion_tokens": 3000, "finish_reason": "length"}
        svc = AnswerService(config=AnswerConfig(), client=Cut())
        final = svc.answer(*_count_question())
        self.assertEqual(final.failure, AnswerFailure.MODEL_OUTPUT_MALFORMED)
        self.assertTrue(final.fallback_used and final.text)

    def test_no_response_is_unavailable(self):
        svc = service(ConnectionError("refused"), ConnectionError("refused"))
        final = svc.answer(*_count_question())
        self.assertEqual(final.failure, AnswerFailure.MODEL_UNAVAILABLE)


def _count_question():
    return "how many", Result([{"record_count": 7}])
