"""Tracing against production-shaped objects.

Every stub here is either the production class itself or deliberately POORER
than it. `extraction_model` and `extraction_endpoint` were NULL in production
for weeks while the tests passed, because the test stub carried `model` and
`endpoint` attributes that the real `Extraction` dataclass did not. A test
whose double is richer than the real thing proves the double works.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import fields as dataclass_fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))

from graphrag import extract as extract_module
from graphrag.extract import Extraction, safe_endpoint
from graphrag.trace_store import TraceStore
from observability.config import LEGACY_DATABASE, load_tracing_config
from observability.export import export_traces
from pipeline import PipelineRequest, SalesforcePipeline

KNOWLEDGE = ROOT / "salesforce_knowledge"
SCHEMA_DB = KNOWLEDGE / "runtime_schema/db/production/salesforce_runtime_schema.db"


def production_extraction() -> Extraction:
    """The real dataclass, with the flags that route to DATA_DIRECT."""
    return Extraction(
        request_type="DATA", intent="record_count", action="count",
        business_entities=[{"name": "internal interview",
                            "role": "primary_entity"}],
        requires_schema_discovery=False, requires_record_query=True,
        requires_metadata_context=False,
        mode="json_object", duration_ms=4213, completion_tokens=96,
        model="Qwen/Qwen3-VL-8B-Instruct-FP8",
        endpoint="http://vllm-router:30002")


def stage_of(event) -> str:
    """Trace entries are TraceEvent objects or plain dicts, by design.

    `add_events` accepts both, so a test that reads only one shape is testing
    less than the store supports.
    """
    return getattr(event, "stage", None) or event.get("stage", "")


class StubPlan:
    def __init__(self):
        self.primary_object = "Internal_Interview__c"
        self.objects = ["Internal_Interview__c"]
        self.entity_mappings: list = []
        self.filter_mappings: list = []
        self.requested_attribute_mappings: list = []
        self.schema_grounded = True
        self.failures: list = []
        self.trace: list = []

    def as_dict(self):
        return {"primary_object": self.primary_object, "objects": self.objects,
                "entity_mappings": [], "filter_mappings": [],
                "requested_attribute_mappings": [], "schema_grounded": True,
                "failures": []}


class StubLinker:
    def __init__(self, schema=None):
        self.schema = schema
        self.plan = StubPlan()

    def link(self, intent):
        return self.plan


class StubQueryResult:
    def __init__(self, success=True):
        self.success = success
        self.sql = 'SELECT COUNT(*) AS "record_count" FROM "main"."x" AS "t0"'
        self.row_count = 1
        self.total_count = 1
        self.truncated = False
        self.execution_ms = 0.75
        self.error = None if success else type("E", (), {"value": "DUCKDB_EXECUTION_FAILED"})()
        self.error_detail = "" if success else "no such table"
        self.freshness = None
        self.plan = None
        self.trace = [{"stage": "SQL_GENERATED", "details": {"param_count": 0}}]

    def as_dict(self):
        return {"success": self.success, "row_count": self.row_count}


class StubRecords:
    def __init__(self, success=True):
        self.success = success

    def query_records(self, plan, **kwargs):
        return StubQueryResult(self.success)


class StubAnswer:
    text = "There are 491 internal interviews."
    grounded = True
    fallback_used = False
    regenerated = False
    attempts = 1
    model = "Qwen/Qwen3.6-35B-A3B-NVFP4"
    model_role = "main"
    result_type = "COUNT"
    failure = None
    failure_detail = ""
    grounding = None
    duration_ms = 580
    trace: list = []

    def as_dict(self):
        return {"text": self.text}


class StubAnswers:
    def answer(self, question, query_result, **kwargs):
        return StubAnswer()


class StubSchemaService:
    """Only what the pipeline asks of it. Poorer than the real service."""

    def __init__(self, tier="L1"):
        self.tier = tier
        self.asked: list[str] = []

    def served_from(self, object_api_name):
        self.asked.append(object_api_name)
        return self.tier


# -- the gap that escaped ---------------------------------------------------
class ExtractionProvenanceTest(unittest.TestCase):
    def test_the_production_extraction_carries_model_and_endpoint(self):
        names = {f.name for f in dataclass_fields(Extraction)}
        self.assertIn("model", names)
        self.assertIn("endpoint", names)

    def test_extract_sets_them_from_the_call_it_actually_made(self):
        captured: dict = {}

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": json.dumps({
                    "request_type": "DATA", "intent": "record_count",
                    "action": "count",
                    "business_entities": [{"name": "interview",
                                           "role": "primary_entity"}],
                    "filters": [], "requested_attributes": [],
                    "metadata_types": [],
                    "requires_schema_discovery": False,
                    "requires_record_query": True,
                    "requires_metadata_context": False})}}],
                    "usage": {"completion_tokens": 42}}

        class Client:
            def __enter__(self): return self
            def __exit__(self, *_): return False

            def post(self, url, json=None, timeout=None):
                captured["url"] = url
                captured["model"] = (json or {}).get("model")
                return Response()

        original = extract_module.httpx if hasattr(extract_module, "httpx") else None
        module = type(sys)("httpx")
        module.Client = Client
        sys.modules["httpx"] = module
        try:
            result = extract_module.extract(
                "how many interviews", endpoint="http://vllm-router:30002/v1",
                model="Qwen/Qwen3-VL-8B-Instruct-FP8")
        finally:
            if original is None:
                sys.modules.pop("httpx", None)
        self.assertIsNotNone(result)
        self.assertEqual(result.model, "Qwen/Qwen3-VL-8B-Instruct-FP8")
        self.assertEqual(result.endpoint, "http://vllm-router:30002")
        self.assertEqual(captured["model"], "Qwen/Qwen3-VL-8B-Instruct-FP8")

    def test_an_endpoint_never_carries_a_credential_into_a_trace(self):
        self.assertEqual(
            safe_endpoint("https://user:secret@host.example.com:8443/v1?api_key=abc"),
            "https://host.example.com:8443")
        self.assertEqual(safe_endpoint("http://vllm:8000/v1"), "http://vllm:8000")
        self.assertEqual(safe_endpoint(""), "")


class TraceContentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "traces.sqlite")
        self.store = TraceStore(self.path)
        self.schema = StubSchemaService("L1")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _run(self, *, records=None, environment="production"):
        pipe = SalesforcePipeline(
            environment=environment,
            linker=StubLinker(self.schema), schema=self.schema,
            records=records or StubRecords(), answers=StubAnswers(),
            trace_store=self.store)
        result = pipe.run(PipelineRequest(
            question="How many internal interviews are scheduled?",
            extraction=production_extraction(), test_case_id="TRACE-001"))
        return result, self.store.get(self.store.recent(1)[0]["trace_id"])

    # -- stage 1 ------------------------------------------------------------
    def test_the_model_that_read_the_question_is_recorded(self):
        _, trace = self._run()
        self.assertEqual(trace["extraction_model"],
                         "Qwen/Qwen3-VL-8B-Instruct-FP8")
        self.assertEqual(trace["extraction_endpoint"],
                         "http://vllm-router:30002")
        self.assertEqual(trace["extraction_mode"], "json_object")
        self.assertEqual(trace["extraction_ms"], 4213)
        self.assertIsNotNone(trace["extraction_json"])

    # -- stage 2 ------------------------------------------------------------
    def test_the_route_is_recorded(self):
        result, trace = self._run()
        self.assertEqual(trace["route"], "DATA_DIRECT")
        dispatched = [e for e in result.trace
                      if stage_of(e) == "ROUTE_DISPATCHED"]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].details["route"], "DATA_DIRECT")

    # -- stage 3 ------------------------------------------------------------
    def test_the_runtime_schema_tier_is_recorded(self):
        _, trace = self._run()
        self.assertEqual(trace["schema_served_from"], "L1")
        self.assertEqual(self.schema.asked, ["Internal_Interview__c"])

    def test_a_cold_object_records_the_sqlite_tier(self):
        self.schema.tier = "L2"
        _, trace = self._run()
        self.assertEqual(trace["schema_served_from"], "L2")

    def test_no_schema_service_leaves_the_tier_null_rather_than_guessing(self):
        pipe = SalesforcePipeline(linker=StubLinker(), schema=None,
                                  records=StubRecords(), answers=StubAnswers(),
                                  trace_store=self.store)
        pipe.run(PipelineRequest(question="q",
                                 extraction=production_extraction()))
        trace = self.store.get(self.store.recent(1)[0]["trace_id"])
        self.assertIsNone(trace["schema_served_from"])

    # -- stage 5 ------------------------------------------------------------
    def test_a_successful_query_leaves_record_error_null(self):
        _, trace = self._run()
        self.assertIsNone(trace["record_error"])
        self.assertEqual(trace["record_row_count"], 1)

    def test_a_failed_query_records_a_safe_error_code(self):
        _, trace = self._run(records=StubRecords(success=False))
        self.assertEqual(trace["record_error"], "DUCKDB_EXECUTION_FAILED")
        self.assertEqual(trace["status"], "error")

    # -- stage 6 ------------------------------------------------------------
    def test_the_answer_model_and_grounding_state_are_recorded(self):
        _, trace = self._run()
        self.assertEqual(trace["answer_model"], "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(trace["answer_model_role"], "main")
        self.assertEqual(trace["answer_grounded"], 1)
        self.assertEqual(trace["answer_fallback"], 0)

    # -- envelope -----------------------------------------------------------
    def test_the_environment_comes_from_configuration_not_a_path(self):
        _, trace = self._run(environment="preprod")
        self.assertEqual(trace["environment"], "preprod")

    def test_the_persisted_structure_is_versioned(self):
        _, trace = self._run()
        self.assertEqual(trace["trace_schema_version"], 1)

    def test_the_trace_is_finalised(self):
        _, trace = self._run()
        self.assertEqual(trace["status"], "ok")
        self.assertIsNotNone(trace["completed_at"])
        self.assertIsNotNone(trace["total_duration_ms"])

    def test_a_failure_still_finalises_the_trace(self):
        _, trace = self._run(records=StubRecords(success=False))
        self.assertEqual(trace["status"], "error")
        self.assertIsNotNone(trace["completed_at"])

    # -- durations ----------------------------------------------------------
    def test_stage_durations_are_derived_and_non_negative(self):
        _, trace = self._run()
        self.assertEqual(trace["extraction_duration_ms"], 4213.0)
        for column in ("linking_duration_ms", "record_query_duration_ms",
                       "answer_duration_ms"):
            self.assertIsNotNone(trace[column], column)
            self.assertGreaterEqual(trace[column], 0.0, column)
        # Not run on this route, so absent rather than zero.
        self.assertIsNone(trace["discovery_duration_ms"])

    def test_total_duration_is_the_end_to_end_timer_not_a_sum_of_stages(self):
        _, trace = self._run()
        stages = sum(trace[c] or 0 for c in
                     ("linking_duration_ms", "record_query_duration_ms",
                      "answer_duration_ms"))
        # The end-to-end timer covers dispatch and persistence too, so it is
        # at least the sum -- and it is emphatically not the extraction column
        # added in, which happened before this trace opened.
        self.assertGreaterEqual(trace["total_duration_ms"], stages)
        self.assertLess(trace["total_duration_ms"],
                        trace["extraction_duration_ms"])

    # -- events -------------------------------------------------------------
    def test_events_belong_to_their_trace_and_keep_execution_order(self):
        result, trace = self._run()
        self.assertEqual(len(trace["events"]), len(result.trace))
        numbers = [e["sequence_number"] for e in trace["events"]]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual([e["stage"] for e in trace["events"]],
                         [stage_of(e) for e in result.trace])

    def test_one_request_makes_exactly_one_trace_row(self):
        self._run()
        self._run()
        count = self.store.db.execute("SELECT count(*) FROM trace").fetchone()[0]
        self.assertEqual(count, 2)

    def test_bound_values_are_never_written(self):
        _, trace = self._run()
        self.assertEqual(trace["record_param_count"], 0)
        self.assertNotIn("record_params", trace)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = str(self.root / "traces.sqlite")
        store = TraceStore(self.path)
        SalesforcePipeline(environment="production", linker=StubLinker(),
                           schema=StubSchemaService(), records=StubRecords(),
                           answers=StubAnswers(), trace_store=store).run(
            PipelineRequest(question="How many internal interviews?",
                            extraction=production_extraction()))
        store.close()
        self.config = load_tracing_config(str(KNOWLEDGE))
        self.config.exports_directory = str(self.root / "exports")
        self.config.duckdb_export_path = str(self.root / "exports/traces.duckdb")

    def tearDown(self):
        self._tmp.cleanup()

    def test_csv_keeps_the_documented_filenames(self):
        result = export_traces(self.config, database=self.path,
                               formats=("csv",))
        names = {Path(f).name for f in result.files}
        self.assertEqual(names, {"query_traces.csv", "query_trace_events.csv"})
        self.assertEqual(result.traces, 1)
        self.assertGreater(result.events, 0)

    def test_the_csv_carries_the_same_logical_trace(self):
        import csv
        export_traces(self.config, database=self.path, formats=("csv",))
        with (Path(self.config.exports_directory) / "query_traces.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "DATA_DIRECT")
        self.assertEqual(rows[0]["extraction_model"],
                         "Qwen/Qwen3-VL-8B-Instruct-FP8")
        with (Path(self.config.exports_directory) / "query_trace_events.csv").open() as f:
            events = list(csv.DictReader(f))
        # Exported under the documented column names.
        self.assertIn("event_index", events[0])
        self.assertIn("event_name", events[0])
        self.assertIn("payload", events[0])
        self.assertEqual(events[0]["trace_id"], rows[0]["trace_id"])

    def test_the_duckdb_export_holds_both_tables_and_joins(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb is not installed")
        export_traces(self.config, database=self.path, formats=("duckdb",))
        connection = duckdb.connect(self.config.duckdb_export_path,
                                    read_only=True)
        try:
            joined = connection.execute(
                'SELECT t.route, count(*) FROM "query_traces" t'
                ' JOIN "query_trace_events" e ON e.trace_id = t.trace_id'
                ' GROUP BY t.route').fetchall()
        finally:
            connection.close()
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0][0], "DATA_DIRECT")
        self.assertGreater(joined[0][1], 0)

    def test_an_export_cannot_modify_the_store(self):
        before = Path(self.path).stat().st_mtime_ns
        export_traces(self.config, database=self.path, formats=("csv",))
        self.assertEqual(Path(self.path).stat().st_mtime_ns, before)

    def test_a_missing_store_is_an_error_not_an_empty_export(self):
        with self.assertRaises(FileNotFoundError):
            export_traces(self.config, database=str(self.root / "absent.sqlite"))


class ConfigTest(unittest.TestCase):
    def test_the_configured_location_is_under_observability(self):
        config = load_tracing_config(str(KNOWLEDGE))
        self.assertTrue(config.enabled)
        self.assertIn("observability/tracing", config.database_path)
        self.assertIn("observability/tracing", config.exports_directory)

    def test_trace_data_is_not_stored_under_salesforce_knowledge(self):
        config = load_tracing_config(str(KNOWLEDGE))
        self.assertNotIn("salesforce_knowledge", config.database_path)
        self.assertNotIn("salesforce_knowledge", config.exports_directory)

    def test_the_previous_location_stays_readable(self):
        config = load_tracing_config(str(KNOWLEDGE))
        candidates = [str(p) for p in config.candidate_databases()]
        self.assertEqual(candidates[0], config.database_path)
        self.assertIn(LEGACY_DATABASE, candidates)

    def test_a_deployment_override_wins_over_the_yaml(self):
        import os
        os.environ["TRACE_DB"] = "/somewhere/else/traces.sqlite"
        try:
            self.assertEqual(load_tracing_config(str(KNOWLEDGE)).database_path,
                             "/somewhere/else/traces.sqlite")
        finally:
            del os.environ["TRACE_DB"]


class MigrationTest(unittest.TestCase):
    def test_a_store_without_the_new_columns_is_migrated_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite")
            first = TraceStore(path)
            trace_id = first.begin("an older question")
            first.record_route(trace_id, "SCHEMA_ONLY")
            first.close()

            new_columns = ("environment", "trace_schema_version",
                           "extraction_duration_ms", "answer_duration_ms")
            raw = sqlite3.connect(path)
            keep = [r[1] for r in raw.execute("PRAGMA table_info(trace)")
                    if r[1] not in new_columns]
            raw.execute(f"CREATE TABLE old AS SELECT {', '.join(keep)} FROM trace")
            raw.execute("DROP TABLE trace")
            raw.execute("ALTER TABLE old RENAME TO trace")
            raw.commit()
            raw.close()

            reopened = TraceStore(path)
            present = {r["name"] for r in
                       reopened.db.execute("PRAGMA table_info(trace)")}
            for column in new_columns:
                self.assertIn(column, present)
            self.assertEqual(reopened.get(trace_id)["question"],
                             "an older question")
            self.assertEqual(reopened.get(trace_id)["route"], "SCHEMA_ONLY")
            reopened.close()


@unittest.skipUnless(SCHEMA_DB.is_file(),
                     "production runtime schema not built; run refresh first")
class RealSchemaProvenanceTest(unittest.TestCase):
    """The tier comes from the real cache, not from a double."""

    def test_the_real_service_reports_its_own_cache_level(self):
        from salesforce.runtime_schema.service import RuntimeSchemaService
        service = RuntimeSchemaService(root=KNOWLEDGE)
        service.start()
        try:
            # Whichever tier holds it, the answer is a real level and not a
            # guess. Internal_Interview__c is hot in this build, so L1.
            tier = service.served_from("Internal_Interview__c")
            self.assertIn(tier, ("L1", "L2"))
            self.assertEqual(tier, "L1" if service.cache.is_hot(
                "Internal_Interview__c") else "L2")
            # A cold object still resolves, and resolves to SQLite.
            cold = next(name for name in
                        (row["api_name"] for row in service.get_object_catalog())
                        if not service.cache.is_hot(name))
            self.assertEqual(service.served_from(cold), "L2")
            # Not in the org at all is a different answer from "cold".
            self.assertIsNone(service.served_from("Not_An_Object__c"))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
