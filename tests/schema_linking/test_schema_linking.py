"""Schema linking, against the real org schema.

The fixture is the production metadata mirror, so these cases are as hard as
the real thing: seven date fields on one object, three status fields, six
email fields on Account.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from salesforce.runtime_schema.service import RuntimeSchemaService
from salesforce.schema_linking import tracing
from salesforce.schema_linking.config import load_schema_linking_config
from salesforce.schema_linking.confidence import resolve
from salesforce.schema_linking.linker import SchemaLinker, link_salesforce_schema
from salesforce.schema_linking.models import FailureCode, Selection
from salesforce.schema_linking.reranker import RetrievalOnlyModel, SchemaLinkingModel
from salesforce.schema_linking.retriever import CandidateRetriever, expected_types

KNOWLEDGE = ROOT / "salesforce_knowledge"
DB = KNOWLEDGE / "runtime_schema/db/production/salesforce_runtime_schema.db"


class StubModel(SchemaLinkingModel):
    """Returns whatever a test tells it to, so escalation can be exercised."""

    def __init__(self, name: str, selected, confidence: float,
                 resolved_value=None):
        self.name = name
        self._selected = selected
        self._confidence = confidence
        self._resolved = resolved_value
        self.calls: list[str] = []

    def _make(self, kind: str) -> Selection:
        self.calls.append(kind)
        return Selection(selected=self._selected, confidence=self._confidence,
                         model=self.name, resolved_value=self._resolved,
                         reason_codes=["stub"])

    def select_object(self, *a, **k): return self._make("object")
    def select_field(self, *a, **k): return self._make("field")
    def select_relationship(self, *a, **k): return self._make("relationship")


@unittest.skipUnless(DB.is_file(), "runtime schema not built; run refresh first")
class SchemaLinkingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = RuntimeSchemaService(root=KNOWLEDGE)
        cls.svc.start()
        cls.config = load_schema_linking_config(KNOWLEDGE)
        cls.retriever = CandidateRetriever(cls.svc, cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.svc.close()

    # -- retrieval ---------------------------------------------------------
    def test_object_discovery_finds_the_object_by_label(self):
        names = [c.api_name for c in
                 self.retriever.object_candidates("internal interview")]
        self.assertIn("Internal_Interview__c", names)

    def test_object_discovery_returns_a_small_candidate_set(self):
        candidates = self.retriever.object_candidates("interview")
        self.assertLessEqual(len(candidates), self.config.candidate_limits.objects)

    def test_exact_api_name_outscores_lexical_hits(self):
        candidates = self.retriever.object_candidates("Interview__c")
        self.assertEqual(candidates[0].api_name, "Interview__c")
        self.assertIn("exact_api_match", candidates[0].evidence)

    def test_picklist_value_is_recorded_as_evidence(self):
        candidates = self.retriever.field_candidates(
            "Internal_Interview__c", "status", value="unassigned")
        mock = next(c for c in candidates if c.api_name == "Mock_Status__c")
        self.assertIn("picklist_value_match", mock.evidence)
        self.assertEqual(mock.matched_picklist_value, "Unassigned")

    def test_field_search_is_scoped_to_one_object(self):
        for candidate in self.retriever.field_candidates(
                "Internal_Interview__c", "status"):
            self.assertEqual(candidate.object_api_name, "Internal_Interview__c")

    def test_datatype_evidence_follows_the_value_shape(self):
        self.assertIn("date", expected_types("tomorrow"))
        self.assertIn("checkbox", expected_types(True))
        self.assertIn("currency", expected_types(100))
        candidates = self.retriever.field_candidates(
            "Internal_Interview__c", "scheduled date", value="tomorrow")
        scheduled = next(c for c in candidates if c.api_name == "Scheduled_Date__c")
        self.assertIn("data_type_match", scheduled.evidence)

    # -- relationships -----------------------------------------------------
    def test_traversal_name_is_derived_from_the_field_not_the_stored_name(self):
        # relationship_name holds the CHILD-side name ('Internal_Interviews');
        # using it parent-ward produces SOQL that runs and returns the wrong
        # thing.
        candidates = self.retriever.relationship_candidates(
            "Internal_Interview__c", "candidate")
        chosen = next(c for c in candidates if c.source_field == "Candidate__c")
        self.assertEqual(chosen.traversal_name, "Candidate__r")
        self.assertEqual(chosen.child_relationship_name, "Internal_Interviews")
        self.assertEqual(chosen.target_object, "Account")

    def test_standard_reference_field_drops_the_id_suffix(self):
        self.assertEqual(
            CandidateRetriever.traversal_name("OwnerId", None), "Owner")
        self.assertEqual(
            CandidateRetriever.traversal_name("Candidate__c", "Whatever"),
            "Candidate__r")

    # -- verification ------------------------------------------------------
    def test_a_name_outside_the_candidate_set_is_rejected(self):
        candidates = self.retriever.object_candidates("internal interview")
        result = self.svc and SchemaLinker(self.svc, self.config, offline=True)
        verdict = result.verifier.verify_object("Totally_Invented__c", candidates)
        self.assertFalse(verdict)
        self.assertEqual(verdict.code, FailureCode.OBJECT_VERIFICATION_FAILED)

    def test_an_injection_shaped_name_is_rejected(self):
        linker = SchemaLinker(self.svc, self.config, offline=True)
        self.assertFalse(linker.verifier.verify_object("Account; DROP TABLE", []))

    def test_picklist_value_is_normalised_to_the_stored_spelling(self):
        linker = SchemaLinker(self.svc, self.config, offline=True)
        self.assertEqual(
            linker.verifier.verify_picklist_value(
                "Internal_Interview__c", "Mock_Status__c", "unassigned"),
            "Unassigned")

    # -- confidence --------------------------------------------------------
    def test_low_confidence_escalates_to_the_fallback(self):
        primary = StubModel("small", "Internal_Interview__c", 0.60)
        fallback = StubModel("main", "Internal_Interview__c", 0.95)
        decision = resolve(primary.select_object(), fallback.select_object,
                           self.config.confidence,
                           ambiguous_code=FailureCode.AMBIGUOUS_OBJECT)
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.escalated)
        self.assertEqual(decision.selection.model, "main")

    def test_high_confidence_does_not_escalate(self):
        fallback = StubModel("main", "X__c", 0.99)
        decision = resolve(Selection(selected="Interview__c", confidence=0.95),
                           fallback.select_object, self.config.confidence,
                           ambiguous_code=FailureCode.AMBIGUOUS_OBJECT)
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.escalated)
        self.assertEqual(fallback.calls, [])

    def test_both_tiers_uncertain_refuses_rather_than_guessing(self):
        decision = resolve(Selection(selected="A__c", confidence=0.2),
                           lambda: Selection(selected="A__c", confidence=0.3),
                           self.config.confidence,
                           ambiguous_code=FailureCode.AMBIGUOUS_OBJECT)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, FailureCode.AMBIGUOUS_OBJECT)

    def test_confidence_outside_the_range_is_clamped(self):
        from salesforce.schema_linking.reranker import _clamp
        self.assertEqual(_clamp(1.7), 1.0)
        self.assertEqual(_clamp(-2), 0.0)
        self.assertEqual(_clamp("nonsense"), 0.0)

    # -- end to end --------------------------------------------------------
    def test_grounded_plan_links_object_and_relationship(self):
        intent = {"business_entities": [
                      {"name": "internal interview", "role": "primary_entity"},
                      {"name": "candidate", "role": "related_entity"}],
                  "filters": [], "requested_attributes": []}
        plan = link_salesforce_schema(intent, self.svc, offline=True)
        self.assertEqual(plan.primary_object, "Internal_Interview__c")
        self.assertIn("Account", plan.objects)
        mapping = next(m for m in plan.entity_mappings
                       if m.business_entity == "candidate")
        self.assertEqual(mapping.relationship_name, "Candidate__r")
        self.assertEqual(mapping.target_object, "Account")

    def test_model_selecting_an_unoffered_name_fails_verification(self):
        linker = SchemaLinker(self.svc, self.config,
                              primary_model=StubModel("bad", "Ghost__c", 0.99),
                              fallback_model=StubModel("bad", "Ghost__c", 0.99))
        plan = linker.link({"business_entities": [
            {"name": "internal interview", "role": "primary_entity"}]})
        self.assertFalse(plan.schema_grounded)
        self.assertEqual(plan.failures[0]["code"],
                         FailureCode.OBJECT_VERIFICATION_FAILED.value)

    def test_picklist_aware_model_resolves_the_filter_value(self):
        linker = SchemaLinker(
            self.svc, self.config,
            primary_model=StubModel("m", "Mock_Status__c", 0.99,
                                    resolved_value="Unassigned"),
            fallback_model=StubModel("m", "Mock_Status__c", 0.99))
        # Object selection uses the same stub, so drive the field step directly.
        plan_holder = link_salesforce_schema(
            {"business_entities": [{"name": "internal interview",
                                    "role": "primary_entity"}]},
            self.svc, offline=True)
        field, selection, resolved = linker._link_field(
            "Internal_Interview__c", "status", "unassigned", "equals", plan_holder)
        self.assertEqual(field, "Mock_Status__c")
        self.assertEqual(resolved, "Unassigned")

    def test_intent_is_never_mutated(self):
        intent = {"business_entities": [{"name": "internal interview",
                                         "role": "primary_entity"}],
                  "filters": [{"concept": "status", "operator": "equals",
                               "value": "unassigned"}],
                  "requested_attributes": []}
        before = repr(intent)
        link_salesforce_schema(intent, self.svc, offline=True)
        self.assertEqual(repr(intent), before)

    def test_no_entities_fails_explicitly(self):
        plan = link_salesforce_schema({"business_entities": []}, self.svc,
                                      offline=True)
        self.assertFalse(plan.schema_grounded)
        self.assertEqual(plan.failures[0]["code"],
                         FailureCode.NO_OBJECT_CANDIDATES.value)

    def test_object_access_is_recorded_for_the_hot_cache(self):
        before = (self.svc.usage.stats("Internal_Interview__c") or {}
                  ).get("access_count", 0)
        link_salesforce_schema(
            {"business_entities": [{"name": "internal interview",
                                    "role": "primary_entity"}]},
            self.svc, offline=True)
        after = self.svc.usage.stats("Internal_Interview__c")["access_count"]
        self.assertGreater(after, before)

    # -- tracing -----------------------------------------------------------
    def test_every_stage_emits_a_trace_event(self):
        plan = link_salesforce_schema(
            {"business_entities": [{"name": "internal interview",
                                    "role": "primary_entity"},
                                   {"name": "candidate", "role": "related_entity"}],
             "filters": [{"concept": "status", "operator": "equals",
                          "value": "unassigned"}]},
            self.svc, offline=True)
        stages = [e.stage for e in plan.trace]
        for expected in (tracing.SCHEMA_LINKING_STARTED,
                         tracing.OBJECT_CANDIDATES_RETRIEVED,
                         tracing.OBJECT_SELECTED, tracing.OBJECT_VERIFIED,
                         tracing.RELATIONSHIP_CANDIDATES_RETRIEVED,
                         tracing.FIELD_CANDIDATES_RETRIEVED,
                         tracing.GROUNDED_SCHEMA_PLAN_CREATED):
            self.assertIn(expected, stages)

    def test_candidate_evidence_is_recorded_in_the_trace(self):
        plan = link_salesforce_schema(
            {"business_entities": [{"name": "Interview__c",
                                    "role": "primary_entity"}]},
            self.svc, offline=True)
        event = next(e for e in plan.trace
                     if e.stage == tracing.OBJECT_CANDIDATES_RETRIEVED)
        self.assertIn("exact_api_match", event.details["candidates"][0]["evidence"])

    def test_retrieval_only_baseline_needs_no_network(self):
        model = RetrievalOnlyModel()
        candidates = self.retriever.object_candidates("Interview__c")
        selection = model.select_object("Interview__c", "primary_entity", candidates)
        self.assertEqual(selection.selected, "Interview__c")
        self.assertGreater(selection.confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
