"""Step 7: semantic linking on the main model, against the real org schema.

The model is a scripted client -- `post()` returns whatever a test hands it --
because what is under test is what the LINKER does with an answer: accept it,
refuse it, verify it, or never ask at all. The live 35B is exercised by
scripts/ask.sh; a unit test that called it would be slow, and would pass or
fail with the model's mood instead of the code's behaviour.

Every expected value below was read from the runtime schema, not assumed.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))

from graphrag.extract import _validate
from salesforce.runtime_schema.business_knowledge import load_business_notes
from salesforce.runtime_schema.service import RuntimeSchemaService
from salesforce.schema_linking.config import load_schema_linking_config
from salesforce.schema_linking.linker import SchemaLinker
from salesforce.schema_linking.models import FailureCode
from salesforce.schema_linking.temporal import parse

KNOWLEDGE = ROOT / "salesforce_knowledge"
DB = KNOWLEDGE / "runtime_schema/db/production/salesforce_runtime_schema.db"
CURATED = ROOT / "brain/lexicon/curated.yaml"


class ScriptedModel:
    """Stands in for the main model's HTTP endpoint. Counts every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)


class _Response:
    def __init__(self, answer):
        self.status_code = 200
        self._answer = answer

    def json(self):
        content = None if self._answer is None else json.dumps(self._answer)
        return {"choices": [{"message": {"content": content},
                             "finish_reason": "stop"}]}


def intent(entities, filters=(), attributes=(), temporal=None, return_mode=None):
    return {"business_entities": [{"name": n, "role": r} for n, r in entities],
            "filters": list(filters), "requested_attributes": list(attributes),
            "temporal": temporal, "return_mode": return_mode}


@unittest.skipUnless(DB.is_file(), "runtime schema not built; run refresh first")
class SemanticLinkingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = RuntimeSchemaService(root=KNOWLEDGE)
        cls.svc.start()
        cls.config = load_schema_linking_config(KNOWLEDGE)
        cls.notes = load_business_notes(CURATED)

    def linker(self, model):
        linker = SchemaLinker(self.svc, self.config, business_notes=self.notes)
        linker.semantic.client = model
        return linker

    # -- fast path (§11, §37, §45) -------------------------------------------
    def test_an_exact_match_question_never_calls_the_model(self):
        model = ScriptedModel()
        plan = self.linker(model).link(intent([("interview", "primary_entity")]),
                                       "how many interviews we have in our org?")
        self.assertTrue(plan.schema_grounded)
        self.assertEqual(plan.primary_object, "Interview__c")
        self.assertEqual(model.calls, [])
        self.assertEqual(plan.semantic["mode"], "fast_path")

    def test_internal_interviews_still_resolve_without_a_model(self):
        model = ScriptedModel()
        plan = self.linker(model).link(
            intent([("internal interviews", "primary_entity")]),
            "how many internal interviews we have currently?")
        self.assertEqual(plan.primary_object, "Internal_Interview__c")
        self.assertEqual(model.calls, [])

    def test_a_named_record_and_a_hand_reviewed_boolean_alias_need_no_model(self):
        model = ScriptedModel()
        plan = self.linker(model).link(intent(
            [("employee", "primary_entity")],
            filters=[{"concept": "name", "operator": "equals",
                      "value": "Jayesh Prajapati"}],
            attributes=[{"entity": "employee",
                         "attribute": "available to take mock interviews"}]), "q")
        self.assertTrue(plan.schema_grounded, plan.failures)
        self.assertEqual(plan.primary_object, "Recruiter__c")
        self.assertEqual(plan.filter_mappings[0].field, "Name")
        self.assertEqual(plan.filter_mappings[0].value, "Jayesh Prajapati")
        self.assertEqual(plan.requested_attribute_mappings[0].target_field,
                         "Active_For_Mock_Interview__c")
        self.assertEqual(model.calls, [])

    # -- one call for the whole question (§36) ------------------------------
    def test_an_ambiguous_question_costs_exactly_one_model_call(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Interview__c", "confidence": 0.95}],
            "primary_object": "Interview__c",
            "filters": [{"index": 0, "object": "Interview__c",
                         "field": "Interview_Outcome__c", "value": "Offer Received",
                         "confidence": 0.95}]})
        plan = self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]),
            "How many interviews have offer received status?")
        self.assertTrue(plan.schema_grounded, plan.failures)
        self.assertEqual(len(model.calls), 1)
        mapping = plan.filter_mappings[0]
        self.assertEqual((mapping.field, mapping.value),
                         ("Interview_Outcome__c", "Offer Received"))

    def test_the_whole_question_and_business_knowledge_reach_the_model(self):
        model = ScriptedModel({"entities": [], "primary_object": None})
        self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]),
            "How many interviews have offer received status?")
        prompt = json.loads(model.calls[0]["messages"][1]["content"])
        self.assertEqual(prompt["question"],
                         "How many interviews have offer received status?")
        outcome = next(f for f in prompt["filters"][0]["candidate_fields"]["Interview__c"]
                       if f["api_name"] == "Interview_Outcome__c")
        self.assertIn("business_knowledge", outcome)
        self.assertIn("manual_alias_match", outcome["evidence"])
        # Both picklists list "Offer Received": the model must see that.
        status = next(f for f in prompt["filters"][0]["candidate_fields"]["Interview__c"]
                      if f["api_name"] == "Interview_Status__c")
        self.assertIn("Offer Received", status["picklist_values"])
        # Never the whole schema: a compact package.
        self.assertLess(len(model.calls[0]["messages"][1]["content"]), 20000)

    def test_the_model_uses_the_main_role_with_thinking_off(self):
        model = ScriptedModel({"entities": [], "primary_object": None})
        self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]), "q")
        body = model.calls[0]
        self.assertEqual(body["model"], "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["temperature"], 0)

    # -- the model may not invent schema (§18, §44) -------------------------
    def test_an_object_that_was_never_offered_is_rejected(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Ghost__c", "confidence": 0.99}],
            "primary_object": "Ghost__c",
            "filters": [{"index": 0, "object": "Ghost__c", "field": "Status__c",
                         "value": "x", "confidence": 0.99}]})
        plan = self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]), "q")
        self.assertFalse(plan.schema_grounded)
        self.assertTrue(any("Ghost__c" in r for r in plan.semantic["rejected"]))

    def test_a_field_that_was_never_offered_is_rejected(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Interview__c", "confidence": 0.99}],
            "primary_object": "Interview__c",
            "filters": [{"index": 0, "object": "Interview__c",
                         "field": "Made_Up_Field__c", "value": "x",
                         "confidence": 0.99}]})
        plan = self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]), "q")
        self.assertFalse(plan.schema_grounded)
        self.assertEqual(plan.failures[0]["code"], FailureCode.AMBIGUOUS_FIELD.value)

    def test_an_invalid_picklist_value_is_rejected_not_passed_through(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Interview__c", "confidence": 0.99}],
            "primary_object": "Interview__c",
            "filters": [{"index": 0, "object": "Interview__c",
                         "field": "Interview_Outcome__c", "value": "Hired Instantly",
                         "confidence": 0.99}]})
        plan = self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Hired Instantly"}]), "q")
        self.assertFalse(plan.schema_grounded)
        self.assertEqual(plan.failures[0]["code"],
                         FailureCode.FIELD_VERIFICATION_FAILED.value)

    def test_a_non_boolean_for_a_checkbox_is_rejected(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Recruiter__c", "confidence": 0.99}],
            "primary_object": "Recruiter__c",
            "filters": [{"index": 0, "object": "Recruiter__c",
                         "field": "Active_For_Interview_Support__c",
                         "value": "sometimes", "confidence": 0.99}]})
        plan = self.linker(model).link(intent(
            [("employee", "primary_entity")],
            filters=[{"concept": "support", "operator": "equals",
                      "value": "sometimes"}]), "q")
        self.assertFalse(plan.schema_grounded)

    def test_a_low_confidence_answer_is_refused(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Account", "confidence": 0.3}],
            "primary_object": "Account", "filters": []})
        plan = self.linker(model).link(intent(
            [("candidate", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "true"}]), "q")
        self.assertFalse(plan.schema_grounded)

    # -- booleans (§16, §42) -----------------------------------------------
    def test_available_to_provide_support_is_a_true_checkbox_filter(self):
        plan = self.linker(ScriptedModel()).link(intent(
            [("employee", "primary_entity")],
            filters=[{"concept": "available to provide support",
                      "operator": "equals", "value": True}]), "q")
        self.assertTrue(plan.schema_grounded, plan.failures)
        mapping = plan.filter_mappings[0]
        self.assertEqual((mapping.field, mapping.value),
                         ("Active_For_Interview_Support__c", True))

    # -- cross-object: the records are not the entity the user named -------
    def test_candidates_who_got_an_offer_are_found_through_their_interviews(self):
        model = ScriptedModel({
            "entities": [{"index": 0, "object": "Account", "confidence": 0.95}],
            "primary_object": "Interview__c",
            "filters": [{"index": 0, "object": "Interview__c",
                         "field": "Interview_Outcome__c", "value": "Offer Received",
                         "confidence": 0.95}]})
        plan = self.linker(model).link(intent(
            [("candidate", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "true"}]), "List candidates who got an offer")
        self.assertTrue(plan.schema_grounded, plan.failures)
        self.assertEqual(plan.primary_object, "Interview__c")
        joined = [m for m in plan.entity_mappings if m.source_field]
        self.assertEqual([(m.source_field, m.target_object) for m in joined],
                         [("Candidate__c", "Account")])
        # The answer shows WHICH candidate, not just interview numbers.
        self.assertIn("Candidate__r.Name",
                      [m.field_path for m in plan.requested_attribute_mappings])

    def test_the_fact_object_is_retrievable_from_the_filter_value(self):
        # "Candidate who got an offer" names no interview; Interview__c must
        # still be in the candidate set or no model could choose it (§43).
        linker = self.linker(ScriptedModel())
        package = linker.semantic.gather(intent(
            [("candidate", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "true"}]), "q")
        self.assertIn("Interview__c", [c.api_name for c in package.anchored])

    # -- business knowledge in retrieval (§17, §43) -----------------------
    def test_candidate_retrieves_account_through_business_knowledge(self):
        linker = self.linker(ScriptedModel())
        top = linker.retriever.object_candidates("candidate")[0]
        self.assertEqual(top.api_name, "Account")
        self.assertIn("manual_alias_match", top.evidence)

    def test_employee_and_background_check_are_retrievable(self):
        retriever = self.linker(ScriptedModel()).retriever
        self.assertEqual(retriever.object_candidates("employee")[0].api_name,
                         "Recruiter__c")
        self.assertEqual(retriever.object_candidates("background check")[0].api_name,
                         "Background_Check__c")

    # -- temporal (§5) ------------------------------------------------------
    def test_a_month_becomes_a_half_open_range_on_the_default_date_field(self):
        plan = self.linker(ScriptedModel()).link(intent(
            [("background check", "primary_entity")],
            temporal={"expression": "May 2026", "concept": None}), "q")
        self.assertTrue(plan.schema_grounded, plan.failures)
        bounds = [(m.field, m.operator, m.value) for m in plan.filter_mappings]
        self.assertEqual(bounds, [("CreatedDate", "on_or_after", "2026-05-01"),
                                  ("CreatedDate", "before", "2026-06-01")])

    def test_an_unreadable_date_fails_instead_of_being_dropped(self):
        plan = self.linker(ScriptedModel()).link(intent(
            [("background check", "primary_entity")],
            temporal={"expression": "sometime soonish", "concept": None}), "q")
        self.assertFalse(plan.schema_grounded)

    # -- model failure (§46) -------------------------------------------------
    def test_an_unreachable_main_model_declines_with_no_small_model_fallback(self):
        model = ScriptedModel(ConnectionError("refused"))
        plan = self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]), "q")
        self.assertFalse(plan.schema_grounded)
        self.assertEqual(plan.semantic["mode"], "model_unavailable")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("unreachable", plan.failures[0]["reason"])

    # -- the linker reads its own config ---------------------------------------
    def test_the_configured_token_budget_is_what_the_model_receives(self):
        model = ScriptedModel({"entities": [], "primary_object": None})
        self.linker(model).link(intent(
            [("interview", "primary_entity")],
            filters=[{"concept": "offer received status", "operator": "equals",
                      "value": "Offer Received"}]), "q")
        self.assertEqual(model.calls[0]["max_tokens"], self.config.models.max_tokens)
        self.assertGreaterEqual(model.calls[0]["max_tokens"], 900)


class ExtractionContractTest(unittest.TestCase):
    """Stage 1's structured output, validated without a model (§40)."""

    BASE = {"request_type": "DATA", "intent": "record_list", "action": "retrieve",
            "requires_schema_discovery": True, "requires_record_query": True,
            "requires_metadata_context": False}

    def test_a_person_is_a_filter_value_not_an_entity(self):
        r = _validate({**self.BASE,
                       "business_entities": [{"name": "employee", "role": "primary_entity"}],
                       "filters": [{"concept": "name", "operator": "equals",
                                    "value": "Jayesh Prajapati"}]})
        self.assertEqual([e["name"] for e in r.business_entities], ["employee"])
        self.assertEqual(r.filters[0]["value"], "Jayesh Prajapati")

    def test_a_month_filed_as_a_filter_is_moved_to_temporal(self):
        r = _validate({**self.BASE,
                       "business_entities": [{"name": "background check",
                                              "role": "primary_entity"}],
                       "filters": [{"concept": "month", "operator": "equals",
                                    "value": "May 2026"}]})
        self.assertEqual(r.filters, [])
        self.assertEqual(r.temporal, {"expression": "May 2026", "concept": None})

    def test_the_same_period_written_twice_is_one_restriction(self):
        r = _validate({**self.BASE,
                       "business_entities": [{"name": "background check",
                                              "role": "primary_entity"}],
                       "filters": [{"concept": "created date", "operator": "between",
                                    "value": "May 2026"}],
                       "temporal": {"expression": "May 2026", "concept": "created"}})
        self.assertEqual(r.filters, [])
        self.assertEqual(r.temporal["concept"], "created")

    def test_scope_words_are_never_requested_attributes(self):
        r = _validate({**self.BASE,
                       "business_entities": [{"name": "background check",
                                              "role": "primary_entity"}],
                       "requested_attributes": [{"entity": "background check",
                                                 "attribute": "all"},
                                                {"entity": "background check",
                                                 "attribute": "details"}],
                       "return_mode": "records"})
        self.assertEqual(r.requested_attributes, [])
        self.assertEqual(r.return_mode, "records")

    def test_currently_restricts_no_dates(self):
        r = _validate({**self.BASE, "intent": "record_count", "action": "count",
                       "business_entities": [{"name": "internal interviews",
                                              "role": "primary_entity"}],
                       "temporal": {"expression": "currently", "concept": None}})
        self.assertIsNone(r.temporal)

    def test_a_filter_that_only_repeats_the_entity_name_is_dropped(self):
        r = _validate({**self.BASE, "intent": "record_count", "action": "count",
                       "business_entities": [{"name": "internal interviews",
                                              "role": "primary_entity"}],
                       "filters": [{"concept": "type", "operator": "equals",
                                    "value": "internal"}]})
        self.assertEqual(r.filters, [])

    def test_missing_routing_flags_are_derived_not_left_as_none(self):
        payload = {k: v for k, v in self.BASE.items() if not k.startswith("requires_")}
        r = _validate({**payload, "business_entities": [
            {"name": "employee", "role": "primary_entity"}]})
        self.assertTrue(r.requires_record_query)
        self.assertTrue(r.requires_schema_discovery)
        self.assertFalse(r.requires_metadata_context)

    def test_business_qualifiers_survive_as_filters(self):
        r = _validate({**self.BASE,
                       "business_entities": [{"name": "employee", "role": "primary_entity"}],
                       "filters": [{"concept": "available to provide support",
                                    "operator": "equals", "value": True}]})
        self.assertEqual(r.filters[0]["concept"], "available to provide support")
        self.assertIs(r.filters[0]["value"], True)


class TemporalParserTest(unittest.TestCase):
    TODAY = date(2026, 9, 29)

    def check(self, expression, start, end):
        r = parse(expression, self.TODAY)
        self.assertIsNotNone(r, expression)
        self.assertEqual((r.start.isoformat(), r.end_exclusive.isoformat()),
                         (start, end), expression)

    def test_months_are_half_open(self):
        self.check("May 2026", "2026-05-01", "2026-06-01")
        self.check("December 2025", "2025-12-01", "2026-01-01")
        self.check("May 2026 month", "2026-05-01", "2026-06-01")

    def test_relative_expressions(self):
        self.check("today", "2026-09-29", "2026-09-30")
        self.check("tomorrow", "2026-09-30", "2026-10-01")
        self.check("last month", "2026-08-01", "2026-09-01")
        self.check("this week", "2026-09-28", "2026-10-05")
        self.check("last 7 days", "2026-09-23", "2026-09-30")

    def test_a_bare_month_is_the_most_recent_one(self):
        self.check("May", "2026-05-01", "2026-06-01")
        self.check("October", "2025-10-01", "2025-11-01")

    def test_other_forms(self):
        self.check("Q4 2025", "2025-10-01", "2026-01-01")
        self.check("2025", "2025-01-01", "2026-01-01")
        self.check("2026-05-14", "2026-05-14", "2026-05-15")

    def test_nonsense_is_none_not_a_guess(self):
        self.assertIsNone(parse("banana", self.TODAY))
        self.assertIsNone(parse("2026-13", self.TODAY))


class ModelRoleTest(unittest.TestCase):
    """§27-§29, §47: one main model, and divergence is reported."""

    def setUp(self):
        import os
        self.saved = {k: os.environ.pop(k, None) for k in (
            "SFK_EXTRACT_URL", "SFK_EXTRACT_MODEL", "OPENAI_BASE_URL", "MAIN_MODEL")}

    def tearDown(self):
        import os
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_every_semantic_stage_resolves_to_main(self):
        import model_roles
        report = model_roles.validate(KNOWLEDGE)
        self.assertTrue(report["ok"], report)
        for stage in ("intent", "routing", "discovery", "linking", "planning",
                      "interpretation", "answer", "discovery_rerank"):
            self.assertEqual(report["stages"][stage]["role"], "main")
            self.assertEqual(report["stages"][stage]["model"],
                             "Qwen/Qwen3.6-35B-A3B-NVFP4")
        for stage in ("record_query",):
            self.assertEqual(report["stages"][stage]["mode"], "deterministic")

    def test_a_legacy_stage_one_override_is_honoured_and_reported(self):
        import os, model_roles
        os.environ["SFK_EXTRACT_MODEL"] = "Qwen/Qwen3-VL-8B-Instruct-FP8"
        os.environ["SFK_EXTRACT_URL"] = "http://vllm-router:30002/v1"
        report = model_roles.validate(KNOWLEDGE)
        self.assertFalse(report["ok"])
        self.assertEqual(report["divergent_semantic_stages"], ["intent"])
        self.assertEqual(report["stages"]["intent"]["source"], "override")

    def test_the_deployment_variable_moves_every_semantic_stage_together(self):
        import os, model_roles
        os.environ["MAIN_MODEL"] = "Some/Other-Model"
        report = model_roles.validate(KNOWLEDGE)
        self.assertTrue(report["ok"])
        self.assertEqual({report["stages"][s]["model"] for s in
                          ("intent", "linking", "answer")}, {"Some/Other-Model"})

    def test_endpoints_in_reports_never_carry_credentials(self):
        import os, model_roles
        os.environ["OPENAI_BASE_URL"] = "https://user:secret@host.example:8443/v1?key=abc"
        shown = model_roles.validate(KNOWLEDGE)["main"]["endpoint"]
        self.assertEqual(shown, "https://host.example:8443")


@unittest.skipUnless(DB.is_file(), "runtime schema not built; run refresh first")
class RuntimeSchemaAdditionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = RuntimeSchemaService(root=KNOWLEDGE)
        cls.svc.start()

    def test_platform_fields_exist_and_say_where_they_came_from(self):
        for name in ("Id", "Name", "CreatedDate"):
            row = self.svc.repo.get_field("Recruiter__c", name)
            self.assertIsNotNone(row, name)
        created = self.svc.repo.get_field("Background_Check__c", "CreatedDate")
        self.assertEqual(created["data_type"], "DateTime")
        self.assertEqual(created["source"], "platform_standard")

    def test_the_name_field_carries_the_orgs_own_label(self):
        self.assertEqual(self.svc.repo.get_field("Recruiter__c", "Name")["label"],
                         "Employee Name")

    def test_business_knowledge_became_manual_aliases(self):
        rows = self.svc.db.connection.execute(
            "SELECT object_api_name FROM object_aliases"
            " WHERE is_manual = 1 AND alias = 'candidate'").fetchall()
        self.assertEqual([r[0] for r in rows], ["Account"])
        fields = self.svc.db.connection.execute(
            "SELECT field_api_name FROM field_aliases"
            " WHERE is_manual = 1 AND alias = 'offer status'").fetchall()
        self.assertEqual([r[0] for r in fields], ["Interview_Outcome__c"])


class BusinessKnowledgeLoaderTest(unittest.TestCase):
    def test_a_term_pointing_at_nothing_is_rejected_and_reported(self):
        import tempfile
        from salesforce.runtime_schema.business_knowledge import load_business_aliases
        from salesforce.runtime_schema.models import SchemaBundle, SField, SObject
        bundle = SchemaBundle()
        bundle.objects.append(SObject(api_name="Account"))
        bundle.fields.append(SField(object_api_name="Account", api_name="Status__c"))
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("terms:\n"
                    "  client: {target: 'object:Account'}\n"
                    "  ghost: {target: 'object:Ghost__c'}\n"
                    "  state: {target: 'field:Account.Status__c'}\n"
                    "  pending: {target: 'object:Account', needs_review: true}\n")
        report = load_business_aliases(f.name, bundle)
        self.assertEqual(report.loaded, 2)
        self.assertEqual(report.rejected, ["ghost -> object:Ghost__c"])
        self.assertEqual(report.skipped_review, ["pending"])
        self.assertTrue(all(a.is_manual for a in bundle.object_aliases))


if __name__ == "__main__":
    unittest.main()
