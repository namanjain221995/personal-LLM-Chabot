"""Step 8: the semantic-IR path, piece by piece and end to end.

The model-facing parts (compiler, grounder) are exercised with scripted
inputs; everything deterministic -- guards, temporal ranges, capability
planning, conversation inheritance, SQL, derived arithmetic -- runs for real,
the SQL against a small fixture warehouse built here.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    import duckdb
except ImportError:                                    # pragma: no cover
    duckdb = None

from pipeline.capabilities import plan as plan_capabilities
from pipeline.compiler import CompileResult, clean
from pipeline.conversation import ConversationStore
from pipeline.ir import from_dict
from pipeline.results import TypedResult, compare, percentage, trend
from salesforce.schema_linking.temporal import parse

TODAY = date(2026, 9, 30)


class _Scripted:
    """Stands in for the main model's HTTP endpoint; answers in order."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def post(self, url, json=None, timeout=None):
        import json as _json
        self.calls.append(json)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer

        class Response:
            status_code = 200

            def json(self_inner):
                return {"choices": [{"message": {"content": None if answer is None
                                                 else _json.dumps(answer)},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        return Response()


# -- temporal ---------------------------------------------------------------
class TemporalTest(unittest.TestCase):
    def test_closed_ranges(self):
        cases = {
            "this month": ("2026-09-01", "2026-10-01"),
            "last month": ("2026-08-01", "2026-09-01"),
            "last six months": ("2026-04-01", "2026-10-01"),
            "Q2 2026": ("2026-04-01", "2026-07-01"),
            "2025": ("2025-01-01", "2026-01-01"),
            "yesterday": ("2026-09-29", "2026-09-30"),
        }
        for text, (start, end) in cases.items():
            with self.subTest(text=text):
                r = parse(text, TODAY)
                self.assertIsNotNone(r)
                self.assertEqual((r.start.isoformat(), r.end_exclusive.isoformat()),
                                 (start, end))

    def test_open_ranges_have_one_bound(self):
        future = parse("upcoming", TODAY)
        self.assertEqual(future.start, TODAY)
        self.assertIsNone(future.end_exclusive)
        before = parse("before 2026-01-01", TODAY)
        self.assertIsNone(before.start)
        self.assertEqual(before.end_exclusive.isoformat(), "2026-01-01")

    def test_latest_is_ordering_not_a_period(self):
        self.assertIsNone(parse("latest", TODAY))


# -- compiler guards -----------------------------------------------------------
def _ir(payload, question="q"):
    return clean(from_dict(payload, question))


class CompilerGuardTest(unittest.TestCase):
    def test_count_is_an_operation_not_a_field(self):
        ir = _ir({"family": "record", "output_mode": "count",
                  "entities": [{"ref": "e0", "concept": "interview"}],
                  "measures": [{"op": "count", "entity": "e0", "concept": "count"}]})
        self.assertIsNone(ir.measures[0].concept)

    def test_record_number_literal_is_the_name(self):
        ir = _ir({"entities": [{"ref": "e0", "concept": "background check"}],
                  "filters": [{"entity": "e0", "concept": "check number",
                               "operator": "equals", "right": "BCN-00028"}]})
        self.assertEqual(ir.filters[0].concept, "name")

    def test_percentage_numerator_does_not_restrict_denominator(self):
        ir = _ir({"output_mode": "percentage",
                  "entities": [{"ref": "e0", "concept": "interview"}],
                  "filters": [{"entity": "e0", "concept": "status", "right": "Completed"}],
                  "derived": [{"kind": "percentage", "numerator": [
                      {"entity": "e0", "concept": "status", "right": "Completed"}]}]})
        self.assertEqual(ir.filters, [])
        self.assertEqual(len(ir.derived[0].numerator), 1)

    def test_time_unit_filter_becomes_temporal(self):
        ir = _ir({"entities": [{"ref": "e0", "concept": "interview"}],
                  "filters": [{"entity": "e0", "concept": "month", "right": "this month"}]})
        self.assertEqual(ir.filters, [])
        self.assertEqual(ir.temporal[0].expression, "this month")

    def test_percentage_without_numerator_asks_for_one_retry(self):
        from pipeline.compiler import _shape, _valid
        bare = _ir({"output_mode": "percentage", "sources": ["RECORD_DATA"],
                    "entities": [{"concept": "interview"}]})
        # A shape problem buys one retry; it never fails the stage, because the
        # planning stage decides the numerator with verified fields in view.
        self.assertIn("numerator", _shape(bare))
        self.assertEqual(_valid(bare), "")

    def test_comparison_needs_two_sides(self):
        from pipeline.compiler import _shape
        one = _ir({"output_mode": "comparison", "entities": [{"concept": "interview"}],
                   "comparison": [{"label": "this month"}]})
        self.assertIn("two", _shape(one))

    def test_duplicate_detection_is_a_generic_grouped_operation(self):
        from pipeline.compiler import _shape, _valid
        ir = _ir({"output_mode": "duplicate_groups", "sources": ["RECORD_DATA"],
                  "entities": [{"ref": "e0", "concept": "invoice"}],
                  "dimensions": [{"entity": "e0", "concept": "external id"}],
                  "duplicate_threshold": 2})
        self.assertEqual((_valid(ir), _shape(ir)), ("", ""))
        self.assertEqual(ir.measures[0].op, "count")
        self.assertEqual(ir.ordering[0].target, "measure:0")
        self.assertEqual(ir.duplicate_threshold, 2)

    def test_duplicate_detection_without_a_key_is_retried(self):
        from pipeline.compiler import _shape
        ir = _ir({"output_mode": "duplicate_groups",
                  "entities": [{"ref": "e0", "concept": "invoice"}]})
        self.assertIn("key", _shape(ir))

    def test_routing_is_required_and_from_the_vocabulary(self):
        from pipeline.compiler import _valid
        ir = _ir({"family": "record", "entities": [{"concept": "interview"}],
                  "sources": ["record_data", "WEB", "RUNTIME_SCHEMA"],
                  "schema": [{"kind": "field_for_concept", "field_concept": "status"}]})
        self.assertEqual(ir.sources, ["RECORD_DATA", "RUNTIME_SCHEMA"])
        unrouted = _ir({"family": "record", "entities": [{"concept": "interview"}]})
        self.assertIn("sources", _valid(unrouted))

    def test_automation_words_are_evidence_not_a_decision(self):
        from pipeline.compiler import lexical_features
        ir = _ir({"family": "record", "sources": ["RECORD_DATA"],
                  "entities": [{"concept": "flow orchestration instance"}]},
                 "Which flows run on Interview__c?")
        self.assertEqual(ir.family, "record")          # the model's reading stands
        self.assertIn("flows", lexical_features("Which flows run on Interview__c?")[0])

    def test_unknown_vocabulary_is_replaced_not_trusted(self):
        ir = _ir({"family": "hack", "output_mode": "drop_table",
                  "entities": [{"ref": "e0", "concept": "interview"}],
                  "filters": [{"entity": "e9", "concept": "status",
                               "operator": "; DROP", "right": "x"}]})
        self.assertEqual((ir.family, ir.output_mode), ("record", "records"))
        self.assertEqual((ir.filters[0].entity, ir.filters[0].operator), ("e0", "equals"))


# -- capabilities ---------------------------------------------------------------
class CapabilityTest(unittest.TestCase):
    def test_record_question(self):
        ir = _ir({"family": "record", "sources": ["RECORD_DATA"],
                  "entities": [{"concept": "interview"}]})
        p = plan_capabilities(ir)
        self.assertEqual(p.route, "DATA_DIRECT")
        self.assertIn("record_query", p.capabilities)
        self.assertEqual(p.unsupported, {})

    def test_metadata_is_honestly_unsupported_when_not_ingested(self):
        ir = _ir({"family": "metadata", "sources": ["METADATA_CONTEXT"],
                  "entities": [{"concept": "interview"}]})
        p = plan_capabilities(ir)
        self.assertIn("metadata_context", p.unsupported)
        self.assertEqual(plan_capabilities(ir, metadata_available=True).unsupported, {})

    def test_mixed_schema_and_records(self):
        ir = _ir({"family": "schema", "sources": ["RUNTIME_SCHEMA", "RECORD_DATA"],
                  "entities": [{"concept": "interview"}],
                  "schema": [{"kind": "field_for_concept", "object_concept": "interview",
                              "field_concept": "outcome"}],
                  "filters": [{"concept": "outcome", "right": "Offer Received"}]})
        p = plan_capabilities(ir)
        self.assertEqual(p.route, "MIXED_DIRECT")
        self.assertTrue({"schema_query", "record_query"} <= set(p.capabilities))

    def test_routing_follows_the_models_sources_not_the_family(self):
        # A "record" family the model routed to sync state is answered from
        # sync state: code finds handlers, it does not overrule the routing.
        ir = _ir({"family": "record", "sources": ["OPERATIONAL_CONTEXT"],
                  "entities": [{"concept": "interview"}]})
        p = plan_capabilities(ir)
        self.assertEqual(p.capabilities, ["operational_context"])
        self.assertEqual(p.route, "SCHEMA_ONLY")


# -- conversation ---------------------------------------------------------------
class ConversationTest(unittest.TestCase):
    def test_follow_up_is_compiled_with_the_previous_meaning(self):
        # What a follow-up inherits is the main model's decision (§30): the
        # compiler is shown the previous question and its meaning.
        model = _Scripted({"family": "record", "output_mode": "grouped",
                           "sources": ["RECORD_DATA", "CONVERSATION_CONTEXT"],
                           "follow_up": True,
                           "entities": [{"ref": "e0", "concept": "interview"},
                                        {"ref": "e1", "concept": "recruiter"}],
                           "filters": [{"entity": "e0", "concept": "status",
                                        "right": "Completed"}],
                           "dimensions": [{"entity": "e1", "concept": "name"}]})
        from pipeline.compiler import IntentCompiler
        compiled = IntentCompiler("http://m", "main", client=model).compile(
            "break them down by recruiter",
            previous={"question": "completed interviews",
                      "ir_summary": {"entities": [{"concept": "interview"}]}})
        sent = model.calls[0]["messages"][1]["content"]
        self.assertIn("Previous question: completed interviews", sent)
        self.assertEqual(compiled.ir.filters[0].right.value, "Completed")
        self.assertEqual(compiled.ir.measures[0].op, "count")

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp) / "c.sqlite")
            ir = _ir({"entities": [{"concept": "interview"}],
                      "filters": [{"concept": "status", "right": "Completed"}]}, "q1")
            store.save("conv-1", ir, {"kind": "records"})
            loaded, result = store.load("conv-1")
            self.assertEqual(loaded.question, "q1")
            self.assertEqual(loaded.filters[0].right.value, "Completed")
            self.assertEqual(result, {"kind": "records"})
            self.assertEqual(store.load("other"), (None, None))
            self.assertEqual(store.load(None), (None, None))


# -- derived arithmetic ------------------------------------------------------
class DerivedMetricTest(unittest.TestCase):
    def test_percentage(self):
        self.assertEqual(percentage(25, 200)["percentage"], 12.5)
        self.assertIsNone(percentage(3, 0)["percentage"])

    def test_compare(self):
        facts = compare([("this month", 120), ("last month", 100)])
        self.assertEqual(facts["difference"], 20)
        self.assertEqual(facts["change_percent"], 20)

    def test_trend(self):
        facts = trend([("2026-07", 10), ("2026-08", 30), ("2026-09", 20)])
        self.assertEqual((facts["first"]["value"], facts["last"]["value"]), (10, 20))


# -- SQL over a fixture warehouse ---------------------------------------------
@unittest.skipIf(duckdb is None, "duckdb not installed")
class AnalyticSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = str(Path(cls.tmp.name) / "w.duckdb")
        c = duckdb.connect(path)
        c.execute('CREATE TABLE "Account" ("Id" VARCHAR, "Name" VARCHAR)')
        c.execute('CREATE TABLE "Recruiter__c" ("Id" VARCHAR, "Name" VARCHAR)')
        c.execute('CREATE TABLE "Invoice__c" ("Id" VARCHAR, "Name" VARCHAR,'
                  ' "External_Id__c" VARCHAR)')
        c.execute('CREATE TABLE "Interview__c" ("Id" VARCHAR, "Name" VARCHAR,'
                  ' "Status__c" VARCHAR, "StartTime__c" VARCHAR, "EndTime__c" VARCHAR,'
                  ' "Candidate__c" VARCHAR, "Recruiter__c" VARCHAR,'
                  ' "Rate__c" DOUBLE, "CreatedDate" TIMESTAMP)')
        c.execute("INSERT INTO \"Account\" VALUES ('a1','Ann'),('a2','Bob'),('a3','Cy_%')")
        c.execute("INSERT INTO \"Recruiter__c\" VALUES ('r1','Rita'),('r2','Raj')")
        c.execute("INSERT INTO \"Invoice__c\" VALUES "
                  "('v1','INV-1','EXT-A'),('v2','INV-2','EXT-A'),"
                  "('v3','INV-3','EXT-B'),('v4','INV-4',NULL),"
                  "('v5','INV-5',NULL)")
        c.execute("""INSERT INTO "Interview__c" VALUES
            ('i1','INT-1','Completed','09:00 AM','10:00 AM','a1','r1',10,'2026-09-05'),
            ('i2','INT-2','Completed','02:00 PM','01:00 PM','a1','r1',20,'2026-09-10'),
            ('i3','INT-3','Scheduled','11:00 AM','11:30 AM','a2','r2',30,'2026-08-15'),
            ('i4','INT-4','Cancelled','08:30 AM','09:00 AM','a1','r1',NULL,'2026-07-01')""")
        c.close()

        from record_query.config import DuckDBSettings, RecordQueryConfig
        from record_query.executor import DuckDBExecutor
        from record_query.freshness import FreshnessTracker
        from record_query.physical_catalog import PhysicalCatalog

        class Service:
            config = RecordQueryConfig(duckdb=DuckDBSettings(path=path, schema="main"))
            executor = DuckDBExecutor(config.duckdb)
            catalog = PhysicalCatalog(executor.connection, config.duckdb)
            freshness = FreshnessTracker(config.freshness)
            schema = None
        cls.service = Service()

    @classmethod
    def tearDownClass(cls):
        cls.service.executor.close()
        cls.tmp.cleanup()

    def _g(self, **kw):
        from record_query.grounded import GroundedQuery
        g = GroundedQuery(base="e0", objects={"e0": "Interview__c"})
        for k, v in kw.items():
            setattr(g, k, v)
        return g

    def _run(self, g):
        from record_query.analytic import AnalyticPlanner, execute
        built = AnalyticPlanner(self.service.catalog).build(g)
        return built, execute(self.service, built)

    def test_count_has_no_limit(self):
        from record_query.grounded import GMeasure
        built, out = self._run(self._g(measures=[GMeasure("count", None, "record_count")]))
        self.assertNotIn("LIMIT", built.statement.sql)
        self.assertEqual(out.rows[0]["record_count"], 4)

    def test_avg_and_count_distinct(self):
        from record_query.grounded import GMeasure, GRef
        g = self._g(measures=[GMeasure("avg", GRef("e0", "Rate__c", "DOUBLE"), "avg_rate"),
                              GMeasure("count_distinct", GRef("e0", "Candidate__c"), "n")])
        _, out = self._run(g)
        self.assertEqual((out.rows[0]["avg_rate"], out.rows[0]["n"]), (20.0, 2))

    def test_values_are_bound_never_inlined(self):
        from record_query.grounded import GFilter, GMeasure, GRef
        g = self._g(measures=[GMeasure("count", None, "c")],
                    filters=[GFilter(GRef("e0", "Status__c"), "equals",
                                     value="Completed' OR 1=1 --")])
        built, out = self._run(g)
        self.assertNotIn("OR 1=1", built.statement.sql)
        self.assertEqual(out.rows[0]["c"], 0)

    def test_group_by_ranking(self):
        from record_query.grounded import GDimension, GMeasure, GOrder, GRef
        from salesforce.schema_linking.graph import Hop
        g = self._g(objects={"e0": "Interview__c", "e1": "Recruiter__c"},
                    paths={"e1": [Hop("Interview__c", "Recruiter__c", "Recruiter__c", "parent")]},
                    measures=[GMeasure("count", None, "interviews")],
                    dimensions=[GDimension(GRef("e1", "Name"), None, "recruiter")],
                    ordering=[GOrder(measure=0)], limit=1)
        built, out = self._run(g)
        self.assertIn("LEFT JOIN", built.statement.sql)
        self.assertTrue(built.statement.sql.endswith("LIMIT 1"))   # top-N: no probe row
        self.assertFalse(out.truncated)
        self.assertEqual(out.rows[0], {"recruiter": "Rita", "interviews": 3})

    def test_duplicate_groups_work_for_any_key_field(self):
        from record_query.grounded import GDimension, GMeasure, GRef
        g = self._g(output_mode="duplicate_groups",
                    measures=[GMeasure("count", None, "duplicate_count")],
                    dimensions=[GDimension(GRef("e0", "Status__c"), None, "status")])
        built, out = self._run(g)
        self.assertIn("HAVING COUNT(*) > ?", built.statement.sql)
        self.assertIn("IS NOT NULL", built.statement.sql)
        self.assertEqual(built.statement.params, [1])
        self.assertEqual(out.rows, [{"status": "Completed", "duplicate_count": 2}])

    def test_duplicate_groups_support_composite_keys_and_thresholds(self):
        from record_query.grounded import GDimension, GMeasure, GRef
        g = self._g(output_mode="duplicate_groups", duplicate_threshold=2,
                    measures=[GMeasure("count", None, "duplicate_count")],
                    dimensions=[
                        GDimension(GRef("e0", "Candidate__c"), None, "candidate"),
                        GDimension(GRef("e0", "Recruiter__c"), None, "recruiter")])
        built, out = self._run(g)
        self.assertEqual(built.statement.params, [2])
        self.assertEqual(out.rows, [{"candidate": "a1", "recruiter": "r1",
                                     "duplicate_count": 3}])

    def test_duplicate_groups_generalize_to_an_unrelated_object(self):
        from record_query.grounded import GDimension, GMeasure, GRef
        g = self._g(base="e2", objects={"e2": "Invoice__c"},
                    output_mode="duplicate_groups",
                    measures=[GMeasure("count", None, "duplicate_count")],
                    dimensions=[GDimension(GRef("e2", "External_Id__c"), None,
                                           "external_id")])
        _, out = self._run(g)
        self.assertEqual(out.rows, [{"external_id": "EXT-A", "duplicate_count": 2}])

    def test_duplicate_result_has_its_own_type(self):
        from pipeline.engines.records import RecordEngine
        from record_query.grounded import GDimension, GMeasure, GRef
        g = self._g(output_mode="duplicate_groups",
                    measures=[GMeasure("count", None, "duplicate_count")],
                    dimensions=[GDimension(GRef("e0", "Status__c"), None, "status")])
        result = RecordEngine(self.service).run(g)
        self.assertEqual(result.kind, "duplicate_groups")
        self.assertEqual(result.returned_count, 1)
        from pipeline.results import to_interpreted
        interpreted = to_interpreted(result, "Find duplicate statuses", g)
        self.assertEqual(interpreted.result_type.value, "DUPLICATE_GROUPS")
        self.assertEqual(interpreted.groups, [{"status": "Completed",
                                               "duplicate count": 2}])

    def test_field_to_field_time_comparison(self):
        from record_query.grounded import GFilter, GRef
        g = self._g(filters=[GFilter(GRef("e0", "EndTime__c"), "less_than", kind="field",
                                     right=GRef("e0", "StartTime__c"), compare_as="time")])
        _, out = self._run(g)
        self.assertEqual([r["Name"] for r in out.rows], ["INT-2"])

    def test_not_exists(self):
        from record_query.grounded import GExists
        g = self._g(base="e1", objects={"e0": "Interview__c", "e1": "Account"},
                    existence=[GExists("not_exists", "Interview__c", "Candidate__c", "e1")])
        _, out = self._run(g)
        self.assertEqual(sorted(r["Name"] for r in out.rows), ["Cy_%"])

    def test_like_escapes_wildcards(self):
        from record_query.grounded import GFilter, GRef
        g = self._g(base="e1", objects={"e1": "Account"},
                    filters=[GFilter(GRef("e1", "Name"), "contains", value="_%")])
        _, out = self._run(g)
        self.assertEqual([r["Name"] for r in out.rows], ["Cy_%"])

    def test_monthly_trend(self):
        from record_query.grounded import GDimension, GMeasure, GRef
        g = self._g(measures=[GMeasure("count", None, "n")],
                    dimensions=[GDimension(GRef("e0", "CreatedDate", "TIMESTAMP"),
                                           "month", "month")])
        _, out = self._run(g)
        self.assertEqual([r["n"] for r in out.rows], [1, 1, 2])

    # -- decomposition through the record engine --------------------------
    def test_percentage_is_two_counts(self):
        from pipeline.engines.records import RecordEngine
        from record_query.grounded import GFilter, GRef
        g = self._g(derived="percentage", output_mode="percentage",
                    numerator=[GFilter(GRef("e0", "Status__c"), "equals", value="Completed")])
        result = RecordEngine(self.service).run(g)
        self.assertEqual(result.values["percentage"], 50)
        self.assertEqual([p["label"] for p in result.subplans], ["numerator", "denominator"])

    def test_comparison_is_one_count_per_segment(self):
        from pipeline.engines.records import RecordEngine
        from record_query.grounded import GFilter, GRef, GSegment
        created = GRef("e0", "CreatedDate", "TIMESTAMP")
        g = self._g(output_mode="comparison", segments=[
            GSegment("September", [GFilter(created, "on_or_after", value="2026-09-01")]),
            GSegment("August", [GFilter(created, "on_or_after", value="2026-08-01"),
                                GFilter(created, "before", value="2026-09-01")])])
        result = RecordEngine(self.service).run(g)
        self.assertEqual(result.kind, "comparison")
        self.assertEqual(result.derived["difference"], 1)

    def test_percentage_without_numerator_fails_instead_of_listing(self):
        from pipeline.engines.records import RecordEngine
        result = RecordEngine(self.service).run(self._g(output_mode="percentage"))
        self.assertFalse(result.success)
        self.assertEqual(result.subplans, [])

    def test_name_search_spans_every_kind_of_record(self):
        from pipeline.engines.search import SearchEngine
        result = SearchEngine(self.service).run("rita")
        self.assertEqual(result.values["kinds_of_record_matched"], 1)
        self.assertEqual(result.rows[0]["Name"], "Rita")
        self.assertEqual(SearchEngine(self.service).run("zzz").rows, [])
        wide = SearchEngine(self.service).run("INT-", contains=True)
        self.assertEqual(wide.total_count, 4)

    def test_exists_is_yes_or_no(self):
        from pipeline.engines.records import RecordEngine
        from record_query.grounded import GFilter, GRef
        g = self._g(output_mode="exists",
                    filters=[GFilter(GRef("e0", "Status__c"), "equals", value="Scheduled")])
        result = RecordEngine(self.service).run(g)
        self.assertEqual(result.values, {"exists": "Yes", "matching_record_count": 1})


# -- the IR path end to end, with scripted ports -------------------------------
def _called(log_to, stage, ok=True, failure=""):
    from pipeline.stage_model import ModelCall
    return log_to.add(ModelCall(stage=stage, model="main", model_called=True, ok=ok,
                                valid=ok, failure=failure))


class _Compiler:
    def __init__(self, payload, failure="invalid"):
        self.payload, self.failure = payload, failure

    def compile(self, question, previous=None, log_to=None):
        if self.payload is None:
            call = _called(log_to, "intent", ok=False, failure=self.failure)
            log_to.combined("routing", call)
            return CompileResult(None, "not_json: finish=length", self.failure)
        call = _called(log_to, "intent")
        ir = _ir(self.payload, question)
        log_to.combined("routing", call, selected=ir.sources)
        return CompileResult(ir)


class _Grounder:
    schema = verifier = None

    def __init__(self, grounded=True):
        self.grounded = grounded

    def ground(self, ir, log_to=None):
        from record_query.grounded import GroundedQuery
        _called(log_to, "discovery")
        _called(log_to, "linking")
        g = GroundedQuery(base="e0", objects={"e0": "Interview__c"}, ir=ir)
        if not self.grounded:
            g.fail("NO_FIELD_CANDIDATES", "nothing matched 'mood'")
        return g


class _Planner:
    def plan(self, question, g, log_to=None):
        call = _called(log_to, "planning")
        return {"operation": "count",
                "measures": [{"op": "count", "binding": None, "entity": "e0"}]}, call


class _Records:
    def run(self, g):
        self.seen = g
        return TypedResult(kind="count", values={"matching_record_count": 7},
                           returned_count=1, total_count=7,
                           subplans=[{"label": "main", "sql": "SELECT 1", "params": 0,
                                      "ms": 1.0, "rows": 1}])


class _Answers:
    def __init__(self):
        self.seen = None

    def answer_interpreted(self, question, interpreted, log_to=None, meaning=None):
        self.seen = interpreted
        from types import SimpleNamespace
        call = _called(log_to, "answer")
        log_to.combined("interpretation", call, selected={"primary_facts": ["count"]})
        return SimpleNamespace(text="There are 7.", trace=[], failure_detail="",
                               failure=None, answer=None)


class IRPathTest(unittest.TestCase):
    def _run(self, payload, grounded=True, failure="invalid"):
        from pipeline import PipelineRequest, SalesforcePipeline
        from pipeline.ir_path import IRComponents
        answers = _Answers()
        pipeline = SalesforcePipeline(
            answers=answers,
            ir=IRComponents(compiler=_Compiler(payload, failure),
                            grounder=_Grounder(grounded),
                            record_engine=_Records(), planner=_Planner()))
        return pipeline.run(PipelineRequest(question="how many interviews")), answers

    def test_record_question_answers_from_typed_facts(self):
        result, answers = self._run({"family": "record", "output_mode": "count",
                                     "sources": ["RECORD_DATA"],
                                     "entities": [{"concept": "interview"}],
                                     "measures": [{"op": "count"}]})
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.answer.text, "There are 7.")
        self.assertEqual(answers.seen.result_type.value, "COUNT")
        stages = [e.stage for e in result.trace]
        for stage in ("IR_COMPILED", "CAPABILITIES_PLANNED", "IR_GROUNDED", "PLAN_CREATED",
                      "QUERY_DECOMPOSED", "SUBPLAN_EXECUTED", "FACTS_FUSED", "MODEL_STAGE"):
            self.assertIn(stage, stages)
        self.assertEqual(result.applicable_stages,
                         ["intent", "routing", "discovery", "linking", "planning",
                          "interpretation", "answer"])
        for stage in result.applicable_stages:
            self.assertTrue(result.model_called(stage), stage)

    def test_compile_failure_is_not_silently_downgraded(self):
        result, _ = self._run(None)
        self.assertFalse(result.success)
        self.assertEqual(result.error.value, "EXTRACTION_FAILED")

    def test_unreachable_model_is_main_model_unavailable(self):
        result, answers = self._run(None, failure="unavailable")
        self.assertEqual(result.error.value, "MAIN_MODEL_UNAVAILABLE")
        self.assertIsNone(answers.seen)

    def test_ungrounded_question_fails_visibly(self):
        result, answers = self._run({"sources": ["RECORD_DATA"],
                                     "entities": [{"concept": "interview"}],
                                     "filters": [{"concept": "mood", "right": "happy"}]},
                                    grounded=False)
        self.assertFalse(result.success)
        self.assertEqual(result.error.value, "SCHEMA_LINKING_FAILED")
        self.assertIsNone(answers.seen)

    def test_unsupported_capability_is_answered_honestly(self):
        result, answers = self._run({"family": "metadata", "sources": ["METADATA_CONTEXT"],
                                     "entities": [{"concept": "interview"}]})
        self.assertTrue(result.success)
        self.assertEqual(answers.seen.result_type.value, "UNSUPPORTED")
        self.assertIn("not ingested", answers.seen.aggregates["unavailable"])
        self.assertEqual(result.applicable_stages,
                         ["intent", "routing", "interpretation", "answer"])

    def test_small_talk_is_left_to_the_caller(self):
        result, answers = self._run({"family": "none"})
        self.assertTrue(result.success)
        self.assertIsNone(result.answer)
        self.assertIsNone(answers.seen)
        self.assertEqual(result.applicable_stages, ["intent", "routing"])


class SchemaSettledReadingTest(unittest.TestCase):
    """IRGrounder._normalise: readings the schema settles, before grounding."""

    def _grounder(self, objects=(), field_labels=None):
        from types import SimpleNamespace
        from salesforce.schema_linking.ir_grounder import IRGrounder

        class Retriever:
            def object_candidates_by_name(self, term):
                return [o for o in objects if o.lower() == term.lower()]

            def object_candidates(self, term, **kw):
                return [SimpleNamespace(api_name="Job_Submission__c", label="Job Submission")]

            def field_candidates(self, obj, phrase, **kw):
                label = (field_labels or {}).get(phrase)
                return [SimpleNamespace(label=label)] if label else []

        g = IRGrounder.__new__(IRGrounder)
        g.retriever = Retriever()
        return g

    def test_qualifier_is_discovery_evidence_not_a_rewrite(self):
        g = self._grounder(objects=("internal interview",))
        ir = _ir({"entities": [{"concept": "interview"}],
                  "filters": [{"concept": "internal",
                               "right": {"type": "boolean", "value": True}}]})
        normalised = g._normalise(ir)
        self.assertEqual(normalised.entities[0].concept, "interview")
        self.assertEqual(len(normalised.filters), 1)
        self.assertEqual(g._qualifiers(normalised), {"e0": ["internal"]})

    def test_distinct_count_of_an_entity_counts_its_records(self):
        ir = self._grounder()._normalise(_ir({
            "entities": [{"ref": "e0", "concept": "marketing record"},
                         {"ref": "e1", "concept": "recruiter", "role": "returned"}],
            "measures": [{"op": "count_distinct", "entity": "e0", "concept": "recruiter"}]}))
        self.assertEqual((ir.measures[0].entity, ir.measures[0].concept), ("e1", None))

    def test_attribute_named_with_another_entitys_word_is_the_primarys(self):
        g = self._grounder(field_labels={"candidate rate": "Candidate Rate"})
        ir = g._normalise(_ir({
            "entities": [{"ref": "e0", "concept": "job submission"},
                         {"ref": "e1", "concept": "candidate", "role": "related"}],
            "measures": [{"op": "avg", "entity": "e1", "concept": "rate"}]}))
        self.assertEqual((ir.measures[0].entity, ir.measures[0].concept),
                         ("e0", "candidate rate"))

    def test_entity_restating_part_of_a_concept_is_dropped(self):
        ir = self._grounder()._normalise(_ir({
            "entities": [{"ref": "e0", "concept": "job requirement"},
                         {"ref": "e1", "concept": "opening", "role": "related"}],
            "measures": [{"op": "avg", "entity": "e0", "concept": "number of openings"}]}))
        self.assertEqual([e.concept for e in ir.entities], ["job requirement"])


class AnswerFactsTest(unittest.TestCase):
    def test_grouping_without_a_measure_counts(self):
        ir = _ir({"entities": [{"concept": "marketing record"}],
                  "dimensions": [{"concept": "status"}]})
        self.assertEqual((ir.measures[0].op, ir.output_mode), ("count", "grouped"))

    def test_period_and_negative_magnitude_are_supported(self):
        from pipeline.results import to_interpreted
        from record_query.grounded import GroundedQuery
        g = GroundedQuery(base="e0", objects={"e0": "Interview__c"},
                          periods=[{"expression": "Q2 2026", "start": "2026-04-01",
                                    "end_exclusive": "2026-07-01"}])
        typed = TypedResult(kind="aggregate", values={"sum_margin": -1739})
        interpreted = to_interpreted(typed, "q", g)
        self.assertTrue({2.0, 2026.0, 1739.0} <= interpreted.supported_numbers)

    def test_fallback_lists_ranking_groups(self):
        from answer.fallback import build_fallback
        from pipeline.results import to_interpreted
        typed = TypedResult(kind="ranking", rows=[{"name": "Ann", "record_count": 3}],
                            returned_count=1)
        answer = build_fallback(to_interpreted(typed, "q"))
        self.assertEqual(answer.summary, "1 groups were returned.")
        self.assertIn("Ann", answer.details[0].text)


class TrendDateTest(unittest.TestCase):
    def test_unit_only_bucket_uses_the_period_date(self):
        g = SchemaSettledReadingTest()._grounder()
        ir = g._normalise(_ir({"entities": [{"ref": "e0", "concept": "interview"}],
                               "dimensions": [{"entity": "e0", "concept": "quarter",
                                               "grain": "quarter"}],
                               "temporal": [{"entity": "e0", "expression": "2026"}]}))
        self.assertEqual(ir.dimensions[0].concept, "created")

    def test_named_date_is_kept(self):
        g = SchemaSettledReadingTest()._grounder()
        ir = g._normalise(_ir({"entities": [{"ref": "e0", "concept": "interview"}],
                               "dimensions": [{"entity": "e0", "concept": "interview date",
                                               "grain": "month"}]}))
        self.assertEqual(ir.dimensions[0].concept, "interview date")


class ModelHabitTest(unittest.TestCase):
    """Readings a different main model produced live (35B, 2026-09-30)."""

    def _n(self, payload, question):
        return SchemaSettledReadingTest()._grounder()._normalise(_ir(payload, question))

    def test_bare_word_takes_the_questions_qualified_phrase(self):
        ir = self._n({"entities": [{"ref": "e0", "concept": "interview"}],
                      "dimensions": [{"entity": "e0", "concept": "status"}]},
                     "Break down interviews by interview status.")
        self.assertEqual(ir.dimensions[0].concept, "interview status")

    def test_created_flag_is_the_periods_date(self):
        ir = self._n({"entities": [{"ref": "e0", "concept": "interview"}],
                      "filters": [{"entity": "e0", "concept": "created",
                                   "right": {"type": "boolean", "value": True}}],
                      "temporal": [{"entity": "e0", "expression": "this month"}]},
                     "interviews created this month")
        self.assertEqual((ir.filters, ir.temporal[0].concept), ([], "created"))

    def test_join_written_as_filter_is_dropped(self):
        ir = self._n({"entities": [{"ref": "e0", "concept": "candidate"},
                                   {"ref": "e1", "concept": "interview", "role": "related"}],
                      "filters": [{"entity": "e0", "concept": "name", "right": "John Smith"},
                                  {"entity": "e1", "concept": "candidate", "right": {
                                      "type": "field_reference", "entity": "e0",
                                      "concept": "name"}}]},
                     "Does candidate John Smith have any interviews?")
        self.assertEqual([f.right.value for f in ir.filters], ["John Smith"])

    def test_comparison_periods_live_in_segments_only(self):
        ir = self._n({"entities": [{"ref": "e0", "concept": "interview"}],
                      "temporal": [{"expression": "this month"}, {"expression": "last month"}],
                      "comparison": [
                          {"label": "this month", "temporal": {"expression": "this month"},
                           "filters": [{"concept": "created",
                                        "right": {"type": "boolean", "value": True}}]},
                          {"label": "last month", "temporal": {"expression": "last month"}}]},
                     "compare interviews created this month with last month")
        self.assertEqual(ir.temporal, [])
        self.assertEqual(ir.comparison[0].filters, [])
        self.assertEqual(ir.comparison[0].temporal.concept, "created")


class LiveClaimTest(unittest.TestCase):
    def test_denial_is_not_a_claim(self):
        from answer.validator import GroundingReport, _check_live_claim
        report = GroundingReport()
        _check_live_claim("Data is a synchronised copy, not live Salesforce.", report)
        self.assertTrue(report.ok)
        _check_live_claim("This is live Salesforce data.", report)
        self.assertFalse(report.ok)

    def test_date_filter_with_period_value_becomes_the_period(self):
        ir = SchemaSettledReadingTest()._grounder()._normalise(_ir({
            "entities": [{"ref": "e0", "concept": "interview"}],
            "comparison": [{"label": "this month", "filters": [
                {"concept": "created date", "right": "this month"}]},
                {"label": "last month", "filters": [
                    {"concept": "created date", "right": "last month"}]}]}, "q"))
        self.assertEqual([(s.filters, s.temporal.expression, s.temporal.concept)
                          for s in ir.comparison],
                         [([], "this month", "created date"), ([], "last month", "created date")])


class TraceColumnsTest(unittest.TestCase):
    def test_ir_shape_is_stored_as_columns(self):
        sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))
        from graphrag.trace_store import TraceStore
        from pipeline.capabilities import plan as plan_caps
        ir = _ir({"family": "record", "output_mode": "count", "sources": ["RECORD_DATA"],
                  "entities": [{"concept": "interview"}], "measures": [{"op": "count"}]}, "q")
        plan_caps(ir)
        with tempfile.TemporaryDirectory() as tmp:
            store = TraceStore(str(Path(tmp) / "t.sqlite"))
            tid = store.begin("q")
            store.record_extraction(tid, ir, model="m", endpoint="e")
            row = store.get(tid)
            self.assertEqual((row["ir_family"], row["ir_output_mode"], row["ir_retried"]),
                             ("record", "count", 0))
            self.assertEqual(row["ir_capabilities"], ["schema_grounding", "record_query"])
            self.assertEqual(store.stats()["ir_families"], {"record/count": 1})
            store.close()


class NameLookupShapeTest(unittest.TestCase):
    def test_who_is_x_is_a_name_lookup(self):
        from pipeline.ir_path import IRPath
        ir = _ir({"entities": [{"concept": "person"}],
                  "filters": [{"concept": "name", "right": "Naman Jain"}]}, "who is naman jain")
        self.assertTrue(IRPath._is_name_lookup(ir))

    def test_anything_more_is_not(self):
        from pipeline.ir_path import IRPath
        ir = _ir({"entities": [{"ref": "e0", "concept": "interview"},
                               {"ref": "e1", "concept": "candidate", "role": "related"}],
                  "filters": [{"entity": "e1", "concept": "name", "right": "John Smith"}]}, "q")
        self.assertFalse(IRPath._is_name_lookup(ir))


if __name__ == "__main__":
    unittest.main()
