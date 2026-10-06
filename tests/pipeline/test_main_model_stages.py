"""The main model is the semantic decision-maker at every applicable stage.

Spec "Main Qwen3.6-35B Model Is the Primary Semantic Engine in Every Pipeline
Stage" (§45): these tests run the real compiler, grounder (discovery +
linking), planner, plan compiler and answer service against the real runtime
schema, with the model endpoint replaced by a scripted stand-in that answers
per stage. What is asserted is read from the RUNTIME TRACE -- the ModelCall
records and MODEL_STAGE events a question leaves behind -- not from
configuration.

They also prove the failure policy (§52): a stage whose model cannot be
reached fails the question as MAIN_MODEL_UNAVAILABLE and nothing downstream
runs; and that retrieval rank never decides (§9, §11): the model's choice of a
lower-ranked candidate is the one that is used.

The held-out tests (§47) compile plans on objects and fields that were not
used while the planner was built.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

KNOWLEDGE = ROOT / "salesforce_knowledge"
DB = KNOWLEDGE / "runtime_schema/db/production/salesforce_runtime_schema.db"

STAGE_PROMPTS = {
    "intent": "Read ONE question about a Salesforce org",
    "discovery": "You decide which Salesforce object",
    "linking": "You map ONE Salesforce question's business concepts onto fields",
    "planning": "You write the LOGICAL QUERY PLAN",
}


class StageScript:
    """The main model's endpoint, scripted per stage. Records every call.

    A responder is an answer dict, an Exception (the endpoint is down), or a
    callable(user_payload) -> answer, so a test can choose by candidate ID
    from what the stage was actually shown.
    """

    def __init__(self, **responders):
        self.responders = {k: (v if isinstance(v, list) else [v])
                           for k, v in responders.items()}
        self.calls: dict[str, list[dict]] = {}

    def stage_of(self, body):
        system = body["messages"][0]["content"]
        return next(s for s, prefix in STAGE_PROMPTS.items() if system.startswith(prefix))

    def post(self, url, json=None, timeout=None):
        stage = self.stage_of(json)
        self.calls.setdefault(stage, []).append(json)
        queue = self.responders.get(stage) or [None]
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(responder, Exception):
            raise responder
        try:            # what the stage was shown (a retry appends to it)
            payload = _json.loads(json["messages"][1]["content"])
        except ValueError:
            payload = json["messages"][1]["content"]
        answer = responder(payload) if callable(responder) else responder
        return _Response(answer)


_json = json


class _Response:
    status_code = 200

    def __init__(self, answer):
        self.answer = answer

    def json(self):
        return {"choices": [{"message": {"content": None if self.answer is None
                                         else _json.dumps(self.answer)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 321, "completion_tokens": 42}}


class AnswerScript:
    """The answer stage's client: interpretation first, then the answer."""

    def __init__(self, summary="There are 7 interviews.", down=False):
        self.summary, self.down, self.calls = summary, down, 0
        self.model = "main"

    def complete_json(self, messages):
        self.calls += 1
        if self.down:
            raise ConnectionError("main model down")
        return ({"interpretation": {"answer_type": "count",
                                    "primary_facts": ["matching_record_count"],
                                    "important_context": [], "relation": None},
                 "answer_type": "count", "summary": self.summary, "details": [],
                 "notes": [], "freshness_note": None},
                {"model": "main", "duration_ms": 5, "completion_tokens": 30,
                 "finish_reason": "stop"})


class CountRecords:
    """The record engine, standing in for the warehouse: a count of 7."""

    def __init__(self):
        self.seen = None

    def run(self, g):
        from pipeline.results import TypedResult
        self.seen = g
        return TypedResult(kind="count", values={"matching_record_count": 7},
                           returned_count=1, total_count=7,
                           subplans=[{"label": "main", "sql": "SELECT 1", "params": 0,
                                      "ms": 1.0, "rows": 1}])


# -- responders ---------------------------------------------------------------
def pick_objects(wanted: dict[str, str], fact: str = "e0"):
    """Discovery: choose, per entity concept, the candidate with that API name."""
    def respond(payload):
        out = []
        for e in payload["entities"]:
            api = wanted.get(e["concept"])
            oid = next((c["id"] for c in e["candidates"] if c["api_name"] == api), None)
            out.append({"ref": e["ref"], "object_id": oid, "confidence": 0.9})
        return {"entities": out, "fact_entity": fact}
    return respond


def pick_fields(wanted: dict[str, str]):
    """Linking: choose, per slot concept, the candidate with that API name."""
    def respond(payload):
        slots = []
        for s in payload["slots"]:
            api = wanted.get((s["concept"] or "").lower())
            fid = next((c["id"] for c in s["candidates"] if c["api_name"] == api), None)
            slots.append({"id": s["id"], "field_id": fid,
                          "values": {str(v): v for v in s.get("values", [])}})
        relations = [{"ref": r["ref"], "path_id": r["options"][0]["id"]}
                     for r in payload.get("relations", [])]
        return {"slots": slots, "relations": relations}
    return respond


def by_concept(payload, concept):
    return next(b["binding"] for b in payload["bindings"]
                if (b["concept"] or "").lower() == concept)


COUNT_INTENT = {"family": "record", "output_mode": "count", "sources": ["RECORD_DATA"],
                "entities": [{"ref": "e0", "concept": "interview", "role": "primary"}],
                "measures": [{"op": "count", "entity": "e0"}]}
FILTERED_INTENT = {
    "family": "record", "output_mode": "count", "sources": ["RECORD_DATA"],
    "entities": [{"ref": "e0", "concept": "interview", "role": "primary"}],
    "filters": [{"entity": "e0", "concept": "status", "operator": "equals",
                 "right": {"type": "literal", "value": "Completed"}}],
    "measures": [{"op": "count", "entity": "e0"}],
    "temporal": [{"expression": "this month", "entity": "e0", "concept": None}]}


def filtered_plan(payload):
    return {"operation": "count",
            "filters": [{"binding": by_concept(payload, "status"), "operator": "equals",
                         "value": "Completed"}],
            "periods": [{"binding": next(b["binding"] for b in payload["bindings"]
                                         if b["data_type"].lower() in ("date", "datetime")),
                         "period": {"kind": "relative", "unit": "month", "offset": 0}}],
            "measures": [{"op": "count", "binding": None, "entity": "e0"}]}


@unittest.skipUnless(DB.is_file(), "runtime schema not built; run refresh first")
class MainModelStageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from salesforce.runtime_schema.business_knowledge import load_business_notes
        from salesforce.runtime_schema.service import RuntimeSchemaService
        from salesforce.schema_linking.config import load_schema_linking_config
        from salesforce.schema_linking.linker import SchemaLinker
        cls.svc = RuntimeSchemaService(root=KNOWLEDGE)
        cls.svc.start()
        cls.linker = SchemaLinker(cls.svc, load_schema_linking_config(KNOWLEDGE),
                                  offline=True,
                                  business_notes=load_business_notes(
                                      ROOT / "brain/lexicon/curated.yaml"))

    def pipeline(self, script, answers=None, records=None):
        from answer.config import load_answer_config
        from answer.service import AnswerService
        from pipeline import SalesforcePipeline
        from pipeline.compiler import IntentCompiler
        from pipeline.engines.schema import SchemaEngine
        from pipeline.ir_path import IRComponents
        from pipeline.planner import LogicalPlanner
        from salesforce.schema_linking.ir_grounder import IRGrounder
        grounder = IRGrounder(self.svc, self.linker.retriever, self.linker.verifier,
                              endpoint="http://main", model="main", client=script)
        self.records = records or CountRecords()
        self.answer_client = answers or AnswerScript()
        return SalesforcePipeline(
            answers=AnswerService(config=load_answer_config(KNOWLEDGE),
                                  client=self.answer_client),
            ir=IRComponents(
                compiler=IntentCompiler("http://main", "main", client=script),
                grounder=grounder,
                planner=LogicalPlanner("http://main", "main", self.svc,
                                       self.linker.verifier, client=script),
                record_engine=self.records,
                schema_engine=SchemaEngine(self.svc, self.linker.retriever, grounder.graph)))

    def ask(self, script, question, **kw):
        from pipeline import PipelineRequest
        return self.pipeline(script, **kw).run(PipelineRequest(question=question))

    def assert_every_applicable_stage_called_the_main_model(self, result):
        from model_roles import REQUIRED_MODEL_STAGES
        self.assertTrue(result.applicable_stages)
        for stage in result.applicable_stages:
            self.assertIn(stage, REQUIRED_MODEL_STAGES)
            self.assertTrue(result.model_called(stage), f"{stage} bypassed the main model")
        traced = {e.details["stage"] for e in result.trace if e.stage == "MODEL_STAGE"
                  and e.details["model_called"]}
        self.assertTrue(set(result.applicable_stages) <= traced, traced)

    # -- §45: participation, from the runtime trace ---------------------------
    def test_every_stage_of_a_record_question_calls_the_main_model(self):
        script = StageScript(
            intent=FILTERED_INTENT,
            discovery=pick_objects({"interview": "Interview__c"}),
            linking=pick_fields({"status": "Interview_Status__c", "date": "CreatedDate",
                                 None: "CreatedDate", "": "CreatedDate"}),
            planning=filtered_plan)
        result = self.ask(script, "How many interviews have status Completed this month?")
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.applicable_stages,
                         ["intent", "routing", "discovery", "linking", "planning",
                          "interpretation", "answer"])
        self.assert_every_applicable_stage_called_the_main_model(result)
        by_stage = {c.stage: c for c in result.model_stages}
        self.assertEqual(by_stage["routing"].combined_with, "intent")
        self.assertEqual(by_stage["interpretation"].combined_with, "answer")
        self.assertEqual(by_stage["interpretation"].selected["primary_facts"],
                         ["matching_record_count"])
        for stage in ("intent", "discovery", "linking", "planning"):
            self.assertEqual(by_stage[stage].prompt_tokens, 321)
            self.assertTrue(by_stage[stage].valid)
        self.assertGreater(by_stage["discovery"].candidate_count, 1)
        self.assertGreater(by_stage["linking"].candidate_count, 1)
        g = self.records.seen
        self.assertEqual(g.base_object, "Interview__c")
        self.assertEqual([(f.left.field, f.value) for f in g.filters][0],
                         ("Interview_Status__c", "Completed"))
        self.assertTrue(any(f.left.field == "CreatedDate" and f.operator == "on_or_after"
                            for f in g.filters))

    def test_an_exact_match_question_still_asks_the_model(self):
        # "interview" is an exact label: the old fast path decided it without
        # the model. Now discovery and planning are model calls regardless.
        script = StageScript(
            intent=COUNT_INTENT, discovery=pick_objects({"interview": "Interview__c"}),
            planning={"operation": "count",
                      "measures": [{"op": "count", "binding": None, "entity": "e0"}]})
        result = self.ask(script, "How many interviews do we have?")
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(len(script.calls["discovery"]), 1)
        self.assertEqual(len(script.calls["planning"]), 1)
        # Nothing names a field and no other entity needs a path: linking does
        # not apply -- reported as not applicable, not as decided by code.
        self.assertNotIn("linking", result.applicable_stages)
        self.assertNotIn("linking", script.calls)
        self.assert_every_applicable_stage_called_the_main_model(result)

    # -- §9, §11: the model decides, retrieval rank does not -----------------
    def test_the_models_choice_beats_retrieval_rank(self):
        top = self.linker.retriever.field_candidates("Interview__c", "status",
                                                     value="Completed", operator="equals")
        self.assertNotEqual(top[0].api_name, "Interview_Status__c")   # rank 1 is another field
        script = StageScript(
            intent=FILTERED_INTENT,
            discovery=pick_objects({"interview": "Interview__c"}),
            linking=pick_fields({"status": "Interview_Status__c", None: "CreatedDate",
                                 "": "CreatedDate"}),
            planning=filtered_plan)
        result = self.ask(script, "How many interviews are Completed this month?")
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.grounded_plan.bindings["S0"].field, "Interview_Status__c")

    def test_an_id_that_was_not_offered_is_sent_back_once(self):
        script = StageScript(
            intent=COUNT_INTENT,
            discovery=[{"entities": [{"ref": "e0", "object_id": "O999"}], "fact_entity": "e0"},
                       pick_objects({"interview": "Interview__c"})],
            planning={"operation": "count",
                      "measures": [{"op": "count", "binding": None, "entity": "e0"}]})
        result = self.ask(script, "How many interviews do we have?")
        self.assertTrue(result.success, result.error_detail)
        discovery = next(c for c in result.model_stages if c.stage == "discovery")
        self.assertEqual(discovery.retries, 1)
        retry = script.calls["discovery"][1]["messages"][-1]["content"]
        self.assertIn("O999", retry)

    def test_a_plan_that_does_not_compile_is_returned_to_the_model(self):
        script = StageScript(
            intent=FILTERED_INTENT,
            discovery=pick_objects({"interview": "Interview__c"}),
            linking=pick_fields({"status": "Interview_Status__c", None: "CreatedDate",
                                 "": "CreatedDate"}),
            planning=[lambda p: {**filtered_plan(p), "filters": [
                          {"binding": by_concept(p, "status"), "operator": "equals",
                           "value": "Finished"}]},
                      filtered_plan])
        result = self.ask(script, "How many interviews are Completed this month?")
        self.assertTrue(result.success, result.error_detail)
        retry = script.calls["planning"][1]["messages"][-1]["content"]
        self.assertIn("'Finished' is not a value of Interview Status", retry)

    # -- §52: failure policy ---------------------------------------------------
    def test_each_stage_outage_is_main_model_unavailable(self):
        down = ConnectionError("connection refused")
        good = dict(intent=FILTERED_INTENT,
                    discovery=pick_objects({"interview": "Interview__c"}),
                    linking=pick_fields({"status": "Interview_Status__c", None: "CreatedDate",
                                         "": "CreatedDate"}),
                    planning=filtered_plan)
        for stage in ("intent", "discovery", "linking", "planning"):
            with self.subTest(stage=stage):
                script = StageScript(**{**good, stage: down})
                result = self.ask(script, "How many interviews are Completed this month?")
                self.assertFalse(result.success)
                self.assertEqual(result.error.value, "MAIN_MODEL_UNAVAILABLE")
                self.assertIsNone(self.records.seen)          # nothing executed
                self.assertEqual(self.answer_client.calls, 0)  # nothing answered
                failed = [c for c in result.model_stages if c.stage == stage]
                self.assertEqual(failed[-1].failure, "unavailable")

    def test_answer_outage_is_not_replaced_by_a_deterministic_answer(self):
        script = StageScript(
            intent=COUNT_INTENT, discovery=pick_objects({"interview": "Interview__c"}),
            planning={"operation": "count",
                      "measures": [{"op": "count", "binding": None, "entity": "e0"}]})
        result = self.ask(script, "How many interviews do we have?",
                          answers=AnswerScript(down=True))
        self.assertFalse(result.success)
        self.assertEqual(result.error.value, "MAIN_MODEL_UNAVAILABLE")
        self.assertIsNone(result.answer)

    # -- §26: schema questions are decided by the model too ----------------------
    def test_schema_question_uses_the_models_object_and_field(self):
        script = StageScript(
            intent={"family": "schema", "output_mode": "schema_facts",
                    "sources": ["RUNTIME_SCHEMA"],
                    "schema": [{"kind": "field_for_concept", "object_concept": "interview",
                                "field_concept": "status"}]},
            discovery=pick_objects({"interview": "Interview__c"}),
            linking=pick_fields({"status": "Interview_Status__c"}))
        result = self.ask(script, "Which field stores the interview status?",
                          answers=AnswerScript(summary="Interview Status (Interview_Status__c) "
                                                       "on Interview__c stores it."))
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.records.rows[0]["field"], "Interview_Status__c")
        self.assertEqual(result.applicable_stages,
                         ["intent", "routing", "discovery", "linking", "interpretation",
                          "answer"])
        self.assert_every_applicable_stage_called_the_main_model(result)

    # -- §10, §38: high-recall candidates ----------------------------------------
    def test_platform_fields_are_evidence_not_the_only_option(self):
        from salesforce.schema_linking.ir_grounder import IRGrounder, Slot
        grounder = IRGrounder(self.svc, self.linker.retriever, self.linker.verifier,
                              endpoint="http://main", model="main", client=StageScript())
        dates = grounder._field_pool("Job_Submission__c",
                                     Slot("S0", "e0", None, date_only=True), None)
        names = [c.api_name for c in dates]
        self.assertEqual(names[0], "CreatedDate")
        self.assertIn("Client_Submission_Date_Time__c", names)
        self.assertIn("LastModifiedDate", names)
        amounts = grounder._field_pool("Job_Submission__c",
                                       Slot("S1", "e0", "rate", numeric_only=True), None)
        self.assertTrue(amounts)
        self.assertTrue(all(c.data_type.lower() in ("currency", "number", "double", "percent")
                            for c in amounts))


# =====================================================================
# §47: held-out plan compilation, on objects and fields the planner was not
# built against.
@unittest.skipUnless(DB.is_file(), "runtime schema not built; run refresh first")
class HeldOutPlanCompileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from salesforce.runtime_schema.service import RuntimeSchemaService
        from salesforce.schema_linking.verifier import SchemaVerifier
        cls.svc = RuntimeSchemaService(root=KNOWLEDGE)
        cls.svc.start()
        cls.verifier = SchemaVerifier(cls.svc)

    def grounded(self, base_obj, bindings, others=None, paths=None):
        from record_query.grounded import GroundedQuery
        from salesforce.schema_linking.ir_grounder import Binding
        objects = {"e0": base_obj, **(others or {})}
        g = GroundedQuery(base="e0", objects=objects, paths=paths or {})
        for i, (entity, field) in enumerate(bindings):
            row = self.svc.repo.get_field(objects[entity], field)
            g.bindings[f"S{i}"] = Binding(
                id=f"S{i}", entity=entity, concept=row["label"].lower(),
                object=objects[entity], field=field, data_type=row["data_type"],
                label=row["label"],
                picklist_values=[p["value"] for p in
                                 self.svc.repo.get_picklist_values(objects[entity], field)])
        g.entities = [{"ref": ref, "concept": obj, "role": "primary" if ref == "e0"
                       else "related"} for ref, obj in objects.items()]
        return g

    def compile(self, plan, g):
        from pipeline.planner import compile_plan
        return compile_plan(plan, g, self.svc, self.verifier)

    def test_average_grouped_by_a_picklist(self):
        g = self.grounded("Company__c", [("e0", "Typical_Rounds__c"), ("e0", "Tier__c")])
        errors = self.compile({"operation": "grouped",
                               "measures": [{"op": "avg", "binding": "S0"}],
                               "dimensions": [{"binding": "S1"}]}, g)
        self.assertEqual(errors, [])
        self.assertEqual((g.measures[0].op, g.measures[0].ref.field),
                         ("avg", "Typical_Rounds__c"))
        self.assertEqual(g.dimensions[0].ref.field, "Tier__c")

    def test_a_related_record_is_shown_by_entity_ref(self):
        from salesforce.schema_linking.graph import Hop
        g = self.grounded("Background_Check__c", [("e0", "Name")], others={"e1": "Account"},
                          paths={"e1": [Hop("Background_Check__c", "Candidate__c", "Account",
                                            "parent")]})
        errors = self.compile({"operation": "single_record",
                               "filters": [{"binding": "S0", "value": "BCN-00028"}],
                               "attributes": [{"entity": "e1"}]}, g)
        self.assertEqual(errors, [])
        self.assertEqual([(r.entity, r.field) for r, _ in g.attributes], [("e1", "Name")])

    def test_a_missing_binding_names_the_entity_form_in_its_error(self):
        g = self.grounded("Background_Check__c", [("e0", "Name")])
        errors = self.compile({"operation": "records", "dimensions": [{"binding": "S9"}]}, g)
        self.assertIn('{"entity"', errors[0])

    def test_a_period_written_as_a_date_filter_is_sent_back(self):
        g = self.grounded("Interview__c", [("e0", "CreatedDate")])
        for bad in ("2026", "September 2026", True, "2026-09-01/2026-09-30"):
            errors = self.compile({"operation": "count", "measures": [{"op": "count"}],
                                   "filters": [{"binding": "S0", "operator": "equals",
                                                "value": bad}]}, self.grounded(
                "Interview__c", [("e0", "CreatedDate")]))
            self.assertTrue(errors and "periods" in errors[0], bad)
        ok = self.compile({"operation": "count", "measures": [{"op": "count"}],
                           "filters": [{"binding": "S0", "operator": "greater_than",
                                        "value": "2026-09-01"}]}, g)
        self.assertEqual(ok, [])

    def test_checkbox_filter_reads_checked_as_true(self):
        g = self.grounded("Template__c", [("e0", "Is_Active__c")])
        self.assertEqual(self.compile({"operation": "count",
                                       "filters": [{"binding": "S0", "value": "checked"}],
                                       "measures": [{"op": "count", "entity": "e0"}]}, g), [])
        self.assertEqual((g.filters[0].kind, g.filters[0].value), ("boolean", True))

    def test_period_on_a_custom_date_field(self):
        g = self.grounded("Cohort__c", [("e0", "End_Date__c")])
        errors = self.compile({"operation": "count",
                               "periods": [{"binding": "S0",
                                            "period": {"kind": "calendar", "year": 2026}}],
                               "measures": [{"op": "count", "entity": "e0"}]}, g)
        self.assertEqual(errors, [])
        self.assertEqual([(f.left.field, f.operator, f.value) for f in g.filters],
                         [("End_Date__c", "on_or_after", "2026-01-01"),
                          ("End_Date__c", "before", "2027-01-01")])

    def test_distinct_related_records_and_a_percentage(self):
        from salesforce.schema_linking.graph import Hop
        hop = Hop("Job_Submission__c", "Candidate__c", "Account", "parent", "Candidate")
        g = self.grounded("Job_Submission__c", [("e0", "Offer_Received__c")],
                          others={"e1": "Account"}, paths={"e1": [hop]})
        errors = self.compile({"operation": "count",
                               "measures": [{"op": "count_distinct", "entity": "e1"}]}, g)
        self.assertEqual(errors, [])
        self.assertEqual((g.measures[0].op, g.measures[0].ref.entity), ("count_distinct", "e1"))
        g = self.grounded("Job_Submission__c", [("e0", "Offer_Received__c")])
        errors = self.compile({"operation": "percentage",
                               "derived": {"kind": "percentage", "numerator": [
                                   {"binding": "S0", "value": True}], "denominator": []},
                               "measures": [{"op": "count", "entity": "e0"}]}, g)
        self.assertEqual(errors, [])
        self.assertEqual((g.derived, g.numerator[0].value), ("percentage", True))

    def test_technical_violations_are_named_for_the_retry(self):
        g = self.grounded("Company__c", [("e0", "Interview_Difficulty__c"), ("e0", "Tier__c")])
        errors = self.compile({"operation": "grouped",
                               "measures": [{"op": "avg", "binding": "S0"}],
                               "dimensions": [{"binding": "S1", "grain": "month"}],
                               "filters": [{"binding": "S1", "value": "Unicorn"}]}, g)
        joined = " | ".join(errors)
        self.assertIn("avg needs a number", joined)
        self.assertIn("not a date", joined)
        self.assertIn("'Unicorn' is not a value of Tier", joined)

    def test_unknown_binding_is_rejected_not_guessed(self):
        g = self.grounded("Company__c", [("e0", "Tier__c")])
        errors = self.compile({"operation": "grouped",
                               "measures": [{"op": "count", "entity": "e0"}],
                               "dimensions": [{"binding": "S7"}]}, g)
        self.assertTrue(any("'S7' is not a binding id" in e for e in errors), errors)


class StructuredPeriodTest(unittest.TestCase):
    def test_the_model_names_the_period_code_does_the_calendar(self):
        from datetime import date
        from salesforce.schema_linking.temporal import from_semantics
        today = date(2026, 3, 31)
        cases = [({"kind": "relative", "unit": "month", "offset": -1}, ("2026-02-01", "2026-03-01")),
                 ({"kind": "relative", "unit": "quarter", "offset": -1}, ("2025-10-01", "2026-01-01")),
                 ({"kind": "last_n", "unit": "month", "n": 6}, ("2025-10-01", "2026-04-01")),
                 ({"kind": "calendar", "year": 2024, "month": 2, "day": 29}, ("2024-02-29", "2024-03-01")),
                 ({"kind": "before", "anchor": {"kind": "calendar", "year": 2026, "month": 5}},
                  (None, "2026-05-01"))]
        for period, (start, end) in cases:
            found = from_semantics(period, today)
            self.assertEqual((found.start.isoformat() if found.start else None,
                              found.end_exclusive.isoformat()), (start, end), period)
        self.assertIsNone(from_semantics({"kind": "fortnightish"}, today))


if __name__ == "__main__":
    unittest.main()
