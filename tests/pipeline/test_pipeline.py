"""Route dispatch and stage wiring.

No LLM, no bundle, no DuckDB. Every port is a stub, because what is being
tested is which stages a route runs and what happens when one of them is not
there -- neither of which depends on the stages doing real work.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))

from graphrag.routing import Route, route_for
from graphrag.trace_store import TraceStore
from pipeline import (PipelineError, PipelineRequest, SalesforcePipeline,
                      Stage, UnknownRoute, routes, stages_for)


class StubExtraction:
    def __init__(self, *, discovery=False, records=False, metadata=False):
        self.requires_schema_discovery = discovery
        self.requires_record_query = records
        self.requires_metadata_context = metadata
        self.request_type = "DATA"
        self.intent = "record_list"
        self.action = "list"
        self.business_entities = [{"name": "interview", "role": "primary_entity"}]
        self.filters: list = []
        self.requested_attributes: list = []
        self.metadata_types: list = []
        self.mode = "json_object"
        self.duration_ms = 12
        self.model = "stub-model"
        self.endpoint = "http://stub/v1"


class StubPlan:
    def __init__(self, grounded=True):
        self.primary_object = "Internal_Interview__c"
        self.objects = ["Internal_Interview__c"]
        self.entity_mappings: list = []
        self.filter_mappings: list = []
        self.requested_attribute_mappings: list = []
        self.schema_grounded = grounded
        self.failures = [] if grounded else [
            {"code": "NO_OBJECT_CANDIDATES", "detail": "nothing matched"}]
        self.trace: list = []

    def as_dict(self):
        return {"primary_object": self.primary_object, "objects": self.objects,
                "entity_mappings": [], "filter_mappings": [],
                "requested_attribute_mappings": [],
                "schema_grounded": self.schema_grounded,
                "failures": self.failures}


class StubLinker:
    def __init__(self, plan=None):
        self.plan = plan or StubPlan()
        self.calls = 0

    def link(self, intent):
        self.calls += 1
        return self.plan


class StubRecords:
    def __init__(self, success=True):
        self.success = success
        self.calls: list = []

    def query_records(self, plan, **kwargs):
        self.calls.append((plan, kwargs))
        return StubResult(self.success)


class StubError:
    value = "DUCKDB_TABLE_NOT_FOUND"


class StubResult:
    def __init__(self, success=True):
        self.success = success
        self.sql = 'SELECT "t0"."Id" AS "Id"\nFROM "main"."Internal_Interview__c" AS "t0"'
        self.row_count = 3 if success else 0
        self.truncated = False
        self.execution_ms = 4.2
        self.error = None if success else StubError()
        self.error_detail = "" if success else "no such table"
        self.freshness = None
        self.trace = [{"stage": "SQL_GENERATED", "details": {"param_count": 2}}]

    def as_dict(self):
        return {"success": self.success, "row_count": self.row_count}


class StubFinalAnswer:
    def __init__(self, text="2 matching interviews were found.",
                 grounded=True, fallback=False):
        self.text = text
        self.grounded = grounded
        self.fallback_used = fallback
        self.regenerated = False
        self.attempts = 1
        self.model = "Qwen/Qwen3.6-35B-A3B-NVFP4"
        self.model_role = "main"
        self.result_type = "MULTI_RECORD"
        self.failure = None
        self.failure_detail = ""
        self.grounding = None
        self.duration_ms = 120
        self.trace: list = []

    def as_dict(self):
        return {"text": self.text, "grounded": self.grounded,
                "model": self.model, "model_role": self.model_role}


class StubAnswers:
    def __init__(self, final=None):
        self.final = final or StubFinalAnswer()
        self.calls: list = []

    def answer(self, question, query_result, **kwargs):
        self.calls.append((question, query_result, kwargs))
        return self.final


class StubCandidate:
    def __init__(self, api_name):
        self.api_name = api_name
        self.component_id = f"object:{api_name}"


class StubDiscovery:
    def __init__(self):
        self.objects = [StubCandidate("Internal_Interview__c")]
        self.fields: list = []
        self.needs_clarification: list = []
        self.served_from = "compact"
        self.trace: list = []


class DispatchTest(unittest.TestCase):
    def test_every_route_has_a_stage_plan(self):
        for route in Route:
            self.assertIn(route.value, routes())
            stages_for(route)

    def test_a_route_name_and_a_route_object_dispatch_alike(self):
        self.assertEqual(stages_for(Route.MIXED_DIRECT),
                         stages_for("MIXED_DIRECT"))

    def test_none_runs_nothing(self):
        self.assertEqual(stages_for(Route.NONE), ())

    def test_every_data_route_links_before_it_queries(self):
        for name in ("DATA_DIRECT", "DATA_WITH_DISCOVERY",
                     "MIXED_DIRECT", "MIXED_WITH_DISCOVERY"):
            plan = stages_for(name)
            self.assertIn(Stage.SCHEMA_LINKING, plan, name)
            self.assertIn(Stage.RECORD_QUERY, plan, name)
            self.assertLess(plan.index(Stage.SCHEMA_LINKING),
                            plan.index(Stage.RECORD_QUERY), name)
            # The main model answers every one of them, and answers last.
            self.assertEqual(plan[-1], Stage.ANSWER, name)

    def test_only_data_routes_answer(self):
        for name in routes():
            runs_records = Stage.RECORD_QUERY in stages_for(name)
            self.assertEqual(Stage.ANSWER in stages_for(name), runs_records,
                             name)

    def test_only_discovery_routes_run_discovery(self):
        for name in routes():
            expected = name.endswith("_WITH_DISCOVERY") or name == "SCHEMA_ONLY"
            self.assertEqual(Stage.DISCOVERY in stages_for(name), expected, name)

    def test_an_unknown_route_is_refused_not_guessed(self):
        with self.assertRaises(UnknownRoute):
            stages_for("DATA_SOMETIMES")
        with self.assertRaises(UnknownRoute):
            stages_for(None)


class PipelineTest(unittest.TestCase):
    def test_a_data_question_links_queries_then_answers(self):
        linker, records, answers = StubLinker(), StubRecords(), StubAnswers()
        pipe = SalesforcePipeline(linker=linker, records=records,
                                  answers=answers)
        result = pipe.run(PipelineRequest(
            question="list the interviews",
            extraction=StubExtraction(records=True)))
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.route, "DATA_DIRECT")
        self.assertEqual(result.stages_run,
                         ["SCHEMA_LINKING", "RECORD_QUERY", "ANSWER"])
        self.assertEqual(linker.calls, 1)
        self.assertEqual(records.calls[0][0]["primary_object"],
                         "Internal_Interview__c")
        # The main model saw the question and the result, and produced the text.
        self.assertEqual(answers.calls[0][0], "list the interviews")
        self.assertEqual(result.answer.text,
                         "2 matching interviews were found.")

    def test_rows_never_reach_a_user_without_the_answer_stage(self):
        pipe = SalesforcePipeline(linker=StubLinker(), records=StubRecords())
        result = pipe.run(PipelineRequest(
            question="list the interviews",
            extraction=StubExtraction(records=True)))
        self.assertFalse(result.success)
        self.assertEqual(result.error, PipelineError.STAGE_UNAVAILABLE)
        self.assertEqual(result.stages_run,
                         ["SCHEMA_LINKING", "RECORD_QUERY"])
        self.assertIsNone(result.answer)

    def test_a_fallback_answer_still_completes_the_route(self):
        answers = StubAnswers(StubFinalAnswer(
            text="2 matching interviews were found.", grounded=False,
            fallback=True))
        pipe = SalesforcePipeline(linker=StubLinker(), records=StubRecords(),
                                  answers=answers)
        result = pipe.run(PipelineRequest(
            question="list the interviews",
            extraction=StubExtraction(records=True)))
        self.assertTrue(result.success, result.error_detail)
        self.assertTrue(result.answer.fallback_used)
        self.assertFalse(result.answer.grounded)

    def test_none_is_a_success_that_runs_nothing(self):
        pipe = SalesforcePipeline(linker=StubLinker(), records=StubRecords())
        result = pipe.run(PipelineRequest(question="hello",
                                          extraction=StubExtraction()))
        self.assertEqual(result.route, "NONE")
        self.assertEqual(result.stages, [])
        self.assertTrue(result.success)

    def test_a_missing_port_is_reported_not_skipped(self):
        pipe = SalesforcePipeline(linker=StubLinker())        # no records port
        result = pipe.run(PipelineRequest(
            question="list the interviews",
            extraction=StubExtraction(records=True)))
        self.assertFalse(result.success)
        self.assertEqual(result.error, PipelineError.STAGE_UNAVAILABLE)
        self.assertEqual(result.stages_run, ["SCHEMA_LINKING"])
        skipped = [e for e in result.trace if e.stage == "STAGE_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].status, "failed")

    def test_a_failed_grounding_stops_before_any_sql(self):
        records = StubRecords()
        pipe = SalesforcePipeline(linker=StubLinker(StubPlan(grounded=False)),
                                  records=records)
        result = pipe.run(PipelineRequest(
            question="list the widgets",
            extraction=StubExtraction(records=True)))
        self.assertFalse(result.success)
        self.assertEqual(result.error, PipelineError.SCHEMA_LINKING_FAILED)
        self.assertEqual(result.error_detail, "nothing matched")
        self.assertEqual(records.calls, [])

    def test_a_failed_record_query_carries_its_own_error_code(self):
        pipe = SalesforcePipeline(linker=StubLinker(),
                                  records=StubRecords(success=False))
        result = pipe.run(PipelineRequest(
            question="list the interviews",
            extraction=StubExtraction(records=True)))
        self.assertFalse(result.success)
        self.assertEqual(result.error, PipelineError.RECORD_QUERY_FAILED)
        self.assertIn("DUCKDB_TABLE_NOT_FOUND", result.error_detail)

    def test_a_mixed_question_runs_every_stage(self):
        pipe = SalesforcePipeline(
            discover=lambda q, **k: StubDiscovery(),
            linker=StubLinker(), records=StubRecords(), answers=StubAnswers(),
            describe=lambda component_id: {"component_id": component_id})
        result = pipe.run(PipelineRequest(
            question="what is an interview and list them",
            extraction=StubExtraction(discovery=True, records=True,
                                      metadata=True)))
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.route, "MIXED_WITH_DISCOVERY")
        self.assertEqual(result.stages_run,
                         ["DISCOVERY", "SCHEMA_LINKING", "RECORD_QUERY",
                          "METADATA", "ANSWER"])
        self.assertEqual(len(result.metadata), 1)

    def test_a_route_may_be_supplied_without_an_extraction(self):
        pipe = SalesforcePipeline(discover=lambda q, **k: StubDiscovery())
        result = pipe.run(PipelineRequest(question="what objects are there",
                                          route=Route.SCHEMA_ONLY))
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.stages_run, ["DISCOVERY"])

    def test_no_extraction_and_no_route_is_an_explicit_failure(self):
        result = SalesforcePipeline().run("anything")
        self.assertFalse(result.success)
        self.assertEqual(result.error, PipelineError.EXTRACTION_UNAVAILABLE)

    def test_a_raising_stage_does_not_escape_the_pipeline(self):
        class Boom:
            def link(self, intent):
                raise RuntimeError("linker exploded")

        result = SalesforcePipeline(linker=Boom(), records=StubRecords()).run(
            PipelineRequest(question="x", extraction=StubExtraction(records=True)))
        self.assertFalse(result.success)
        self.assertIn("linker exploded", result.error_detail)

    def test_the_route_matches_what_routing_derived(self):
        for discovery, records, metadata in [
                (False, False, False), (True, True, True),
                (False, True, True), (True, False, False)]:
            extraction = StubExtraction(discovery=discovery, records=records,
                                        metadata=metadata)
            pipe = SalesforcePipeline(
                discover=lambda q, **k: StubDiscovery(),
                linker=StubLinker(), records=StubRecords(),
                describe=lambda component_id: {"id": component_id})
            result = pipe.run(PipelineRequest(question="q",
                                              extraction=extraction))
            self.assertEqual(result.route, route_for(extraction).value)


class TraceStoreTest(unittest.TestCase):
    """Steps 4 and 5 reach the store, and an old store is migrated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "traces.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def _pipeline(self, store, **ports):
        return SalesforcePipeline(trace_store=store, **ports)

    def test_a_whole_data_trace_lands_in_one_row(self):
        store = TraceStore(self.path)
        pipe = self._pipeline(store, linker=StubLinker(), records=StubRecords(),
                              answers=StubAnswers())
        result = pipe.run(PipelineRequest(question="list the interviews",
                                          test_case_id="SF-PIPE-001",
                                          extraction=StubExtraction(records=True)))
        self.assertTrue(result.success, result.error_detail)
        row = store.recent(1)[0]
        trace = store.get(row["trace_id"])
        self.assertEqual(trace["route"], "DATA_DIRECT")
        self.assertEqual(trace["extraction_model"], "stub-model")
        self.assertEqual(trace["grounded_ok"], 1)
        self.assertEqual(trace["grounded_primary_object"],
                         "Internal_Interview__c")
        self.assertEqual(trace["record_row_count"], 3)
        self.assertEqual(trace["record_param_count"], 2)
        self.assertIsNone(trace["record_error"])
        # Direct routes run no discovery, so the resolved objects come from
        # the grounded plan instead of from a discovery result.
        self.assertEqual(trace["resolved_objects"], ["Internal_Interview__c"])
        store.close()

    def test_the_answer_and_its_model_land_in_the_trace(self):
        store = TraceStore(self.path)
        answers = StubAnswers()
        pipe = self._pipeline(store, linker=StubLinker(), records=StubRecords(),
                              answers=answers)
        pipe.run(PipelineRequest(question="list the interviews",
                                 extraction=StubExtraction(records=True)))
        trace = store.get(store.recent(1)[0]["trace_id"])
        self.assertEqual(trace["answer_model"], "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(trace["answer_model_role"], "main")
        self.assertEqual(trace["answer_grounded"], 1)
        self.assertEqual(trace["answer_fallback"], 0)
        self.assertEqual(trace["answer_text"],
                         "2 matching interviews were found.")
        stats = store.stats()
        self.assertEqual(stats["answers"], 1)
        self.assertEqual(stats["answers_grounded"], 1)
        self.assertEqual(stats["first_pass_grounding_rate"], 1.0)
        self.assertEqual(stats["safety_fallback_rate"], 0.0)
        self.assertEqual(stats["answer_models"],
                         {"Qwen/Qwen3.6-35B-A3B-NVFP4": 1})
        store.close()

    def test_a_fallback_answer_is_visible_in_the_metrics(self):
        store = TraceStore(self.path)
        answers = StubAnswers(StubFinalAnswer(text="2 matching interviews "
                                                   "were found.",
                                              grounded=False, fallback=True))
        pipe = self._pipeline(store, linker=StubLinker(), records=StubRecords(),
                              answers=answers)
        pipe.run(PipelineRequest(question="q",
                                 extraction=StubExtraction(records=True)))
        stats = store.stats()
        self.assertEqual(stats["answers_fell_back"], 1)
        self.assertEqual(stats["answers_grounded"], 0)
        self.assertEqual(stats["safety_fallback_rate"], 1.0)
        store.close()

    def test_a_failed_grounding_is_recorded_with_its_failure_code(self):
        store = TraceStore(self.path)
        pipe = self._pipeline(store, linker=StubLinker(StubPlan(grounded=False)),
                              records=StubRecords())
        pipe.run(PipelineRequest(question="list the widgets",
                                 extraction=StubExtraction(records=True)))
        trace = store.get(store.recent(1)[0]["trace_id"])
        self.assertEqual(trace["status"], "error")
        self.assertEqual(trace["grounded_ok"], 0)
        self.assertEqual(trace["grounding_failures"][0]["code"],
                         "NO_OBJECT_CANDIDATES")
        self.assertIsNone(trace["record_sql"])
        store.close()

    def test_bound_values_never_reach_the_store(self):
        store = TraceStore(self.path)
        pipe = self._pipeline(store, linker=StubLinker(), records=StubRecords())
        pipe.run(PipelineRequest(question="list the interviews",
                                 extraction=StubExtraction(records=True)))
        trace = store.get(store.recent(1)[0]["trace_id"])
        self.assertIn("SELECT", trace["record_sql"])
        self.assertEqual(trace["record_param_count"], 2)
        self.assertNotIn("record_params", trace)
        store.close()

    def test_stats_answer_whether_the_new_pipeline_is_working(self):
        store = TraceStore(self.path)
        ok = self._pipeline(store, linker=StubLinker(), records=StubRecords())
        bad = self._pipeline(store, linker=StubLinker(),
                             records=StubRecords(success=False))
        ok.run(PipelineRequest(question="a", extraction=StubExtraction(records=True)))
        bad.run(PipelineRequest(question="b", extraction=StubExtraction(records=True)))
        stats = store.stats()
        self.assertEqual(stats["traces"], 2)
        self.assertEqual(stats["grounded"], 2)
        self.assertEqual(stats["with_record_query"], 2)
        self.assertEqual(stats["record_query_failed"], 1)
        self.assertEqual(stats["routes"], {"DATA_DIRECT": 2})
        self.assertEqual(stats["record_errors"],
                         {"DUCKDB_TABLE_NOT_FOUND": 1})
        store.close()

    def test_a_store_written_before_steps_4_and_5_is_migrated_not_rebuilt(self):
        import sqlite3
        first = TraceStore(self.path)
        trace_id = first.begin("an older question", test_case_id="OLD-001")
        first.record_route(trace_id, "SCHEMA_ONLY")
        first.close()
        # Drop the new columns to look like a store built before they existed.
        raw = sqlite3.connect(self.path)
        keep = [r[1] for r in raw.execute("PRAGMA table_info(trace)")
                if not r[1].startswith(("grounded_", "grounding_", "record_",
                                        "data_freshness"))]
        raw.execute(f"CREATE TABLE old AS SELECT {', '.join(keep)} FROM trace")
        raw.execute("DROP TABLE trace")
        raw.execute("ALTER TABLE old RENAME TO trace")
        raw.commit()
        raw.close()

        reopened = TraceStore(self.path)
        columns = {r["name"] for r in
                   reopened.db.execute("PRAGMA table_info(trace)")}
        self.assertIn("grounded_ok", columns)
        self.assertIn("record_sql", columns)
        # The old row survived the migration.
        self.assertEqual(reopened.get(trace_id)["question"],
                         "an older question")
        self.assertEqual(reopened.get(trace_id)["route"], "SCHEMA_ONLY")
        reopened.close()


if __name__ == "__main__":
    unittest.main()
