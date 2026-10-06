"""The /chat cutover seam: exactly one pipeline runs, and the flag really gates it.

These tests import `sfk_bridge` by file rather than through `app.engines`,
because the orchestrator package needs psycopg and a database that a unit test
has no business requiring. What is under test is the decision logic and the
meta contract, neither of which touches the rest of the application.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "orchestrator/app/engines/sfk_bridge.py"


def load_bridge(*, enabled: bool):
    """Import the bridge with a stubbed `sf_intel.Outcome` and a chosen flag."""
    package = types.ModuleType("_sfk_pkg")
    package.__path__ = [str(BRIDGE.parent)]
    sys.modules["_sfk_pkg"] = package

    outcome_module = types.ModuleType("_sfk_pkg.sf_intel")

    class Outcome:
        def __init__(self, handled=False, answer="", resolved_text="",
                     clarification=None, meta_extras=None):
            self.handled = handled
            self.answer = answer
            self.resolved_text = resolved_text
            self.clarification = clarification
            self.meta_extras = meta_extras or {}

    outcome_module.Outcome = Outcome
    sys.modules["_sfk_pkg.sf_intel"] = outcome_module

    spec = importlib.util.spec_from_file_location("_sfk_pkg.sfk_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_sfk_pkg.sfk_bridge"] = module
    spec.loader.exec_module(module)
    module.ENABLED = enabled
    return module


class Request:
    def __init__(self, text="how many interviews", *, sf_live=False,
                 pdf_data=None, image_data=None):
        self.text = text
        self.sf_live = sf_live
        self.pdf_data = pdf_data
        self.image_data = image_data
        self.test_case_id = None


class FakeFreshness:
    status, age_minutes = "fresh", 17.9


class FakeRecords:
    sql = 'SELECT COUNT(*) AS "record_count" FROM "main"."Internal_Interview__c" AS "t0"'
    row_count, total_count, truncated = 1, 491, False
    freshness = FakeFreshness()


class FakeAnswer:
    text = "There are 491 internal interviews scheduled."
    model = "Qwen/Qwen3.6-35B-A3B-NVFP4"
    grounded = True


class FakeResult:
    def __init__(self, *, success=True, route="DATA_DIRECT", answer=FakeAnswer(),
                 records=FakeRecords()):
        self.success = success
        self.route = route
        self.stages_run = ["SCHEMA_LINKING", "RECORD_QUERY", "ANSWER"]
        self.error_detail = "" if success else "AMBIGUOUS_OBJECT"
        self.answer = answer
        self.records = records


class FakePipeline:
    def __init__(self, result=None):
        self.result = result if result is not None else FakeResult()
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return self.result


def answer(module, request, *, pipeline=None):
    emitted: list = []

    async def emit(kind, data):
        emitted.append((kind, data))

    if pipeline is None:
        return asyncio.run(module.try_answer(request.text, emit=emit,
                                             request=request)), emitted
    module._state.update({"pipeline": pipeline, "built": True, "error": None})
    # A stand-in `pipeline` module for this call only. Patching the real one
    # in place leaked a dict-returning PipelineRequest into every later test.
    stub = types.ModuleType("pipeline")
    stub.PipelineRequest = lambda **kw: kw
    from unittest import mock
    with mock.patch.dict(sys.modules, {"pipeline": stub}):
        out = asyncio.run(module.try_answer(request.text, emit=emit,
                                           request=request))
    return out, emitted


class FlagTest(unittest.TestCase):
    def test_off_means_the_bridge_does_nothing_at_all(self):
        module = load_bridge(enabled=False)
        pipeline = FakePipeline()
        out, emitted = answer(module, Request(), pipeline=pipeline)
        self.assertIsNone(out)
        self.assertEqual(emitted, [])
        # The decisive assertion: the new pipeline was never asked.
        self.assertEqual(pipeline.calls, 0)

    def test_off_never_constructs_the_pipeline(self):
        module = load_bridge(enabled=False)
        self.assertFalse(module.available())
        self.assertFalse(module._state["built"])


class DeclineTest(unittest.TestCase):
    """A decline is decided from the request, before any model call."""

    def setUp(self):
        self.module = load_bridge(enabled=True)

    def _declines(self, request, clarification=None) -> bool:
        return self.module._declines(request, clarification) is not None

    def test_a_pending_clarification_belongs_to_the_existing_engine(self):
        self.assertTrue(self._declines(Request(), clarification=object()))

    def test_the_live_toggle_belongs_to_the_existing_engine(self):
        self.assertTrue(self._declines(Request(sf_live=True)))

    def test_an_attachment_belongs_to_the_existing_engine(self):
        self.assertTrue(self._declines(Request(pdf_data=b"x")))
        self.assertTrue(self._declines(Request(image_data=b"x")))

    def test_an_empty_question_is_declined(self):
        self.assertTrue(self._declines(Request("   ")))

    def test_a_plain_record_question_is_accepted(self):
        self.assertFalse(self._declines(Request("how many interviews")))

    def test_a_failed_run_declines_rather_than_answering_badly(self):
        out, emitted = answer(self.module, Request(),
                              pipeline=FakePipeline(FakeResult(success=False)))
        self.assertIsNone(out)
        self.assertEqual(emitted, [])

    def test_an_empty_answer_declines(self):
        class Blank:
            text, model, grounded = "   ", "m", True

        out, _ = answer(self.module, Request(),
                        pipeline=FakePipeline(FakeResult(answer=Blank())))
        self.assertIsNone(out)

    def test_a_raising_pipeline_declines_instead_of_failing_the_request(self):
        class Boom:
            def run(self, request):
                raise RuntimeError("linker exploded")

        out, emitted = answer(self.module, Request(), pipeline=Boom())
        self.assertIsNone(out)
        self.assertEqual(emitted, [])


class ServedTest(unittest.TestCase):
    def setUp(self):
        self.module = load_bridge(enabled=True)
        self.pipeline = FakePipeline()
        self.out, self.emitted = answer(self.module, Request(),
                                        pipeline=self.pipeline)
        self.meta = next(d for k, d in self.emitted if k == "meta")

    def test_the_pipeline_ran_exactly_once(self):
        self.assertEqual(self.pipeline.calls, 1)

    def test_the_outcome_tells_the_caller_to_skip_the_existing_engine(self):
        self.assertTrue(self.out.handled)
        self.assertEqual(self.out.answer,
                         "There are 491 internal interviews scheduled.")

    def test_the_answer_is_emitted_once_as_a_token(self):
        tokens = [d for k, d in self.emitted if k == "token"]
        self.assertEqual(len(tokens), 1)
        self.assertIn("491", tokens[0]["text"])

    def test_the_existing_route_value_is_preserved_for_the_client(self):
        # The frontend already understands "sql" for a records answer. The new
        # eight-value route rides alongside rather than replacing a value a
        # client parses.
        self.assertEqual(self.meta["route"], "sql")
        self.assertEqual(self.meta["sfk_route"], "DATA_DIRECT")

    def test_provenance_says_synchronised_and_never_live(self):
        provenance = self.meta["provenance"]
        self.assertEqual(provenance["source"], "synchronised_salesforce_records")
        self.assertNotEqual(provenance["freshness"], "live")
        self.assertEqual(provenance["age_minutes"], 17.9)
        self.assertNotIn("live", str(provenance).replace("_records", ""))

    def test_the_main_model_is_named(self):
        self.assertEqual(self.meta["model"], "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertTrue(self.meta["grounded"])

    def test_returned_and_total_counts_stay_distinct(self):
        self.assertEqual(self.meta["row_count"], 1)
        self.assertEqual(self.meta["total_count"], 491)
        self.assertFalse(self.meta["truncated"])

    def test_the_traced_sql_reads_main_and_never_raw(self):
        self.assertIn('"main".', self.meta["sql"])
        self.assertNotIn('"raw".', self.meta["sql"])


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.module = load_bridge(enabled=True)

    def _environment(self, **env) -> str:
        import os
        for key in ("SALESFORCE_ENVIRONMENT", "SF_ENVIRONMENT"):
            os.environ.pop(key, None)
        os.environ.update(env)
        try:
            return self.module._environment()
        finally:
            for key in env:
                os.environ.pop(key, None)

    def test_the_old_variable_set_to_unknown_never_reaches_a_trace(self):
        self.assertEqual(self._environment(SF_ENVIRONMENT="unknown"),
                         "production")

    def test_the_old_variable_is_honoured_as_an_alias(self):
        self.assertEqual(self._environment(SF_ENVIRONMENT="preprod"), "preprod")

    def test_the_canonical_variable_wins(self):
        self.assertEqual(
            self._environment(SF_ENVIRONMENT="preprod",
                              SALESFORCE_ENVIRONMENT="production"),
            "production")


if __name__ == "__main__":
    unittest.main()


class RecordOriginTest(unittest.TestCase):
    """The label comes from the records' own Organization row."""

    def setUp(self):
        self.module = load_bridge(enabled=True)

    def _records(self, row=None, *, fails=False):
        class Cursor:
            def fetchone(self_inner):
                return row

        class Connection:
            def execute(self_inner, sql):
                if fails:
                    raise RuntimeError("Catalog Error: Table Organization does not exist")
                return Cursor()

        class Executor:
            connection = Connection()

        class Records:
            executor = Executor()

        return Records()

    def _origin(self, records, login_url=""):
        import os
        previous = os.environ.pop("SF_LOGIN_URL", None)
        if login_url:
            os.environ["SF_LOGIN_URL"] = login_url
        try:
            return self.module._record_origin(records)
        finally:
            os.environ.pop("SF_LOGIN_URL", None)
            if previous is not None:
                os.environ["SF_LOGIN_URL"] = previous

    def test_a_sandbox_is_named_from_the_login_url(self):
        origin = self._origin(
            self._records(("00DOy00000P7XiPMAV", True, "USA250S")),
            "https://techsara--preprod.sandbox.my.salesforce.com")
        self.assertEqual(origin["environment"], "preprod")
        self.assertTrue(origin["is_sandbox"])
        self.assertEqual(origin["environment_source"], "warehouse")
        self.assertEqual(origin["org_id"], "00DOy00000P7XiPMAV")

    def test_a_sandbox_without_a_readable_url_is_still_a_sandbox(self):
        origin = self._origin(self._records(("00D1", True, "USA250S")))
        self.assertEqual(origin["environment"], "sandbox")

    def test_production_data_is_production_whatever_the_url_says(self):
        # The URL may name a sandbox; the data says otherwise, and the data wins.
        origin = self._origin(
            self._records(("00D2", False, "NA123")),
            "https://techsara--preprod.sandbox.my.salesforce.com")
        self.assertEqual(origin["environment"], "production")
        self.assertFalse(origin["is_sandbox"])

    def test_the_warehouse_stores_booleans_as_text_too(self):
        origin = self._origin(self._records(("00D3", "true", "X")), "")
        self.assertTrue(origin["is_sandbox"])

    def test_an_unreadable_organization_falls_back_to_config_and_says_so(self):
        origin = self._origin(self._records(fails=True))
        self.assertEqual(origin["environment_source"], "config")
        self.assertIsNone(origin["is_sandbox"])

    def test_the_answer_provenance_carries_the_records_origin(self):
        self.module._state["origin"] = {
            "environment": "preprod", "environment_source": "warehouse",
            "is_sandbox": True, "org_id": "00DOy00000P7XiPMAV",
            "instance": "USA250S"}
        try:
            _, emitted = answer(self.module, Request(), pipeline=FakePipeline())
        finally:
            self.module._state["origin"] = None
        provenance = next(d for k, d in emitted if k == "meta")["provenance"]
        self.assertEqual(provenance["environment"], "preprod")
        self.assertTrue(provenance["is_sandbox"])
        self.assertEqual(provenance["environment_source"], "warehouse")
