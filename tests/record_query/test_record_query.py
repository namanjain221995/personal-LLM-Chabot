"""Record-query layer, end to end over a fixture warehouse.

The Salesforce side is the real production runtime schema, so grounding is
checked against the org's actual objects and fields. The DuckDB side is a small
warehouse built here: the real one is 361 MB, lives only inside the container,
and its row counts change with every sync, so a test that asserted against it
would be neither portable nor stable.

The split is the point. A field that exists in the org but not in the warehouse
is a different failure from a field that exists in neither, and only a fixture
where the two deliberately disagree can prove the layer tells them apart.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    import duckdb
except ImportError:                                    # pragma: no cover
    duckdb = None

from record_query.config import (DuckDBSettings, FreshnessSettings,
                                 RecordQueryConfig, ResultSettings)
from record_query.models import (DuckDBQueryPlan, Operation, QueryError,
                                 SelectItem, SQLStatement, TableRef)
from record_query.service import RecordQueryService
from record_query.sql_builder import build_sql
from record_query.validator import validate_query_plan, validate_sql
from salesforce.runtime_schema.service import RuntimeSchemaService

KNOWLEDGE = ROOT / "salesforce_knowledge"
SCHEMA_DB = KNOWLEDGE / "runtime_schema/db/production/salesforce_runtime_schema.db"

PRIMARY = "Internal_Interview__c"
RELATED = "Recruiter__c"

# In the org but deliberately absent from the fixture warehouse, so the
# "schema has it, the replica does not" path has something to fail on.
OBJECT_NOT_REPLICATED = "Niche__c"
FIELD_NOT_REPLICATED = "Deadline__c"


def _build_warehouse(path: str) -> None:
    connection = duckdb.connect(path)
    connection.execute("""
        CREATE TABLE "Internal_Interview__c" (
            "Id"                 VARCHAR,
            "Name"               VARCHAR,
            "Status__c"          VARCHAR,
            "Round__c"           VARCHAR,
            "Scheduled_Date__c"  DATE,
            "Week_Number__c"     INTEGER,
            "Human_Total_Score__c" DOUBLE,
            "Postion__c"         VARCHAR,
            "Interviewer__c"     VARCHAR
        )""")
    connection.execute("""
        CREATE TABLE "Recruiter__c" (
            "Id"            VARCHAR,
            "Name"          VARCHAR,
            "Email__c"      VARCHAR,
            "Designation__c" VARCHAR
        )""")
    connection.execute("CREATE TABLE sync_runs (completed_at TIMESTAMP)")

    connection.executemany(
        'INSERT INTO "Recruiter__c" VALUES (?, ?, ?, ?)',
        [("r1", "Asha Menon", "asha@example.com", "Senior Recruiter"),
         ("r2", "Dev Kapoor", "dev@example.com", "Recruiter")])
    connection.executemany(
        'INSERT INTO "Internal_Interview__c" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [("i1", "INT-0001", "Completed", "Round 1", "2026-01-15", 1, 8.0,
          "Backend Engineer", "r1"),
         ("i2", "INT-0002", "Completed", "Round 1", "2026-02-20", 2, 6.0,
          "Backend Engineer", "r1"),
         ("i3", "INT-0003", "Scheduled", "Round 2", "2026-03-05", 3, 9.0,
          "Data Engineer", "r2"),
         ("i4", "INT-0004", "Cancelled", "Round 2", "2025-11-01", 4, 4.0,
          "Data Engineer", "r2"),
         # "50%" and "5010" together: an unescaped % matches both, a correctly
         # escaped one matches only the first.
         ("i5", "INT-0005", "Scheduled", "Round 1", "2026-03-30", 5, 7.0,
          "50% Support Engineer", None),
         ("i6", "INT-0006", "Scheduled", "Round 1", "2026-03-31", 6, 5.0,
          "5010 Support Engineer", None)])
    # Naive UTC. A tz-aware value handed to a DuckDB TIMESTAMP column is
    # converted to local time and stored without the offset, which would make
    # the age read back wrong by the machine's UTC offset.
    connection.execute("INSERT INTO sync_runs VALUES (?)",
                       [(datetime.now(timezone.utc)
                         - timedelta(minutes=5)).replace(tzinfo=None)])
    connection.close()


def grounded(primary: str = PRIMARY, **overrides: object) -> dict:
    plan = {"schema_grounded": True, "primary_object": primary,
            "entity_mappings": [], "requested_attribute_mappings": [],
            "filter_mappings": []}
    plan.update(overrides)
    return plan


@unittest.skipIf(duckdb is None, "duckdb is not installed")
@unittest.skipUnless(SCHEMA_DB.is_file(),
                     "production runtime schema not built; run refresh first")
class RecordQueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.warehouse = str(Path(cls._tmp.name) / "fixture.duckdb")
        _build_warehouse(cls.warehouse)

        cls.schema = RuntimeSchemaService(root=KNOWLEDGE)
        cls.schema.start()
        cls.config = RecordQueryConfig(
            duckdb=DuckDBSettings(path=cls.warehouse, schema="main",
                                  read_only=True),
            result=ResultSettings(default_limit=100, max_limit=1000,
                                  max_response_bytes=2_000_000),
            freshness=FreshnessSettings(reject_when_stale=False))
        cls.service = RecordQueryService(cls.schema, config=cls.config,
                                         root=KNOWLEDGE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.close()
        cls.schema.stop() if hasattr(cls.schema, "stop") else None
        cls._tmp.cleanup()

    # -- 1. simple retrieval ---------------------------------------------
    def test_simple_retrieval_returns_rows_with_identity(self):
        result = self.service.query_records(grounded())
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.row_count, 6)
        # Id first so every row can be cited back to a record.
        self.assertEqual(result.columns[0], "Id")
        self.assertIn("Name", result.columns)

    # -- 2. related field through a join ---------------------------------
    def test_related_field_joins_only_on_a_schema_relationship(self):
        plan = grounded(
            entity_mappings=[{"source_field": "Interviewer__c",
                              "target_object": RELATED}],
            requested_attribute_mappings=[
                {"target_object": RELATED, "target_field": "Email__c",
                 "business_attribute": "interviewer email"}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        self.assertIn("interviewer_email", result.columns)
        self.assertIn('LEFT JOIN "main"."Recruiter__c" AS "t1"', result.sql)
        emails = {row["interviewer_email"] for row in result.rows}
        self.assertIn("asha@example.com", emails)
        # LEFT, not INNER: the row with no interviewer must survive.
        self.assertIn(None, emails)

    # -- 3. multiple filters ---------------------------------------------
    def test_multiple_filters_are_combined_with_and(self):
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "equals", "value": "Completed"},
            {"field": "Round__c", "operator": "equals", "value": "Round 1"}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.row_count, 2)
        self.assertIn("AND", result.sql)

    # -- 4. date filter ---------------------------------------------------
    def test_date_filter_coerces_the_string_to_a_date(self):
        plan = grounded(filter_mappings=[
            {"field": "Scheduled_Date__c", "operator": "on_or_after",
             "value": "2026-02-01"}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        # i2, i3, i5, i6 -- the 2025 row and the January row are excluded.
        self.assertEqual(result.row_count, 4)

    # -- 5. IN filter -----------------------------------------------------
    def test_in_filter_binds_one_parameter_per_value(self):
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "in",
             "value": ["Completed", "Cancelled"]}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.row_count, 3)
        self.assertIn("IN (?, ?)", result.sql)

    # -- 6. contains ------------------------------------------------------
    def test_contains_escapes_wildcards_in_the_user_text(self):
        plan = grounded(filter_mappings=[
            {"field": "Postion__c", "operator": "contains", "value": "50%"}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        # Exactly the row whose text contains "50%". Unescaped, the % would
        # have been a wildcard and "5010 Support Engineer" would match too.
        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.rows[0]["Id"], "i5")
        self.assertIn("ESCAPE '\\'", result.sql)

    # -- 7. count ---------------------------------------------------------
    def test_count_returns_one_number_and_is_never_limited(self):
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "equals", "value": "Completed"}])
        result = self.service.query_records(plan, operation="count")
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.rows, [{"record_count": 2}])
        # A LIMIT on a COUNT would make the number wrong, not short -- and the
        # probe row must not put one back.
        self.assertNotIn("LIMIT", result.sql)

    # -- 8. aggregation ---------------------------------------------------
    def test_aggregation_groups_and_averages(self):
        result = self.service.query_records(
            grounded(), operation="aggregate", group_by_fields=["Round__c"],
            aggregations=[{"function": "avg", "field": "Human_Total_Score__c",
                           "alias": "average_score"}])
        self.assertTrue(result.success, result.error_detail)
        scores = {row["Round__c"]: row["average_score"] for row in result.rows}
        self.assertEqual(scores["Round 1"], 6.5)     # 8.0, 6.0, 7.0, 5.0
        self.assertEqual(scores["Round 2"], 6.5)     # 9.0, 4.0
        self.assertIn("GROUP BY", result.sql)

    def test_average_of_a_text_column_is_refused(self):
        result = self.service.query_records(
            grounded(), operation="aggregate",
            aggregations=[{"function": "avg", "field": "Status__c"}])
        self.assertFalse(result.success)
        self.assertEqual(result.error, QueryError.UNSUPPORTED_AGGREGATION)

    # -- 9. invalid table -------------------------------------------------
    def test_object_in_the_org_but_not_in_the_warehouse_fails_explicitly(self):
        result = self.service.query_records(grounded(OBJECT_NOT_REPLICATED))
        self.assertFalse(result.success)
        self.assertEqual(result.error,
                         QueryError.PHYSICAL_OBJECT_MAPPING_NOT_FOUND)
        self.assertIn(OBJECT_NOT_REPLICATED, result.error_detail)

    def test_object_in_neither_fails_at_grounding_not_at_duckdb(self):
        result = self.service.query_records(grounded("Not_An_Object__c"))
        self.assertFalse(result.success)
        self.assertEqual(result.error, QueryError.GROUNDING_VALIDATION_FAILED)

    # -- 10. invalid field ------------------------------------------------
    def test_field_in_the_org_but_not_in_the_warehouse_fails_explicitly(self):
        plan = grounded(filter_mappings=[
            {"field": FIELD_NOT_REPLICATED, "operator": "is_not_null",
             "value": None}])
        result = self.service.query_records(plan)
        self.assertFalse(result.success)
        self.assertEqual(result.error,
                         QueryError.PHYSICAL_FIELD_MAPPING_NOT_FOUND)

    def test_field_in_neither_fails_at_grounding(self):
        plan = grounded(filter_mappings=[
            {"field": "Not_A_Field__c", "operator": "equals", "value": "x"}])
        result = self.service.query_records(plan)
        self.assertFalse(result.success)
        self.assertEqual(result.error, QueryError.GROUNDING_VALIDATION_FAILED)

    # -- 11. injection value ----------------------------------------------
    def test_a_sql_payload_in_a_value_stays_a_value(self):
        payload = "'; DROP TABLE \"Recruiter__c\"; --"
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "equals", "value": payload}])
        result = self.service.query_records(plan)
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.row_count, 0)
        # The payload never reaches the statement text.
        self.assertNotIn("DROP", result.sql)
        self.assertNotIn("--", result.sql)
        # And the table it named is still there.
        rows = self.service.query_records(
            grounded(RELATED), operation="count").rows
        self.assertEqual(rows, [{"record_count": 2}])

    def test_an_identifier_shaped_payload_cannot_reach_the_catalog(self):
        plan = grounded(filter_mappings=[
            {"field": 'Status__c" OR 1=1 --', "operator": "equals",
             "value": "x"}])
        result = self.service.query_records(plan)
        self.assertFalse(result.success)
        self.assertEqual(result.error, QueryError.GROUNDING_VALIDATION_FAILED)

    # -- 12. forbidden SQL -------------------------------------------------
    def test_a_mutation_is_refused_by_the_sql_validator(self):
        for sql in ('DELETE FROM "Recruiter__c"',
                    'SELECT 1; DROP TABLE "Recruiter__c"',
                    'SELECT * FROM "x" -- comment'):
            with self.subTest(sql=sql):
                check = validate_sql(SQLStatement(sql=sql))
                self.assertFalse(check)
                self.assertEqual(check.error, QueryError.SQL_VALIDATION_FAILED)

    def test_a_keyword_inside_a_quoted_identifier_is_not_a_mutation(self):
        # Salesforce really does have fields like Created_Date__c and objects
        # named Order; quoting is what keeps them from tripping the check.
        statement = SQLStatement(
            sql='SELECT "t0"."Created_Date__c" AS "Created_Date__c"\n'
                'FROM "main"."Order" AS "t0"')
        self.assertTrue(validate_sql(statement))

    # -- 13. limit enforcement ---------------------------------------------
    def test_a_requested_limit_above_the_maximum_is_clamped(self):
        result = self.service.query_records(grounded(), limit=999_999)
        self.assertTrue(result.success, result.error_detail)
        # The plan is clamped to the maximum; the statement carries the probe
        # row on top of it.
        self.assertIn(f"LIMIT {self.config.result.max_limit + 1}", result.sql)
        self.assertFalse(result.truncated)

    def test_a_plan_that_bypasses_the_planner_is_still_rejected(self):
        plan = DuckDBQueryPlan(
            operation=Operation.RETRIEVE,
            primary_table=TableRef(PRIMARY, PRIMARY, "t0"),
            select=[SelectItem("t0", "Id", "Id")],
            limit=self.config.result.max_limit + 1)
        check = validate_query_plan(plan, self.service.catalog,
                                    self.config.result.max_limit)
        self.assertFalse(check)
        self.assertEqual(check.error, QueryError.QUERY_LIMIT_EXCEEDED)

    def test_a_limit_returns_that_many_rows_and_reports_the_rest(self):
        result = self.service.query_records(grounded(), limit=2)
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.row_count, 2)
        # The statement asks for one row more than the caller wants, so the
        # extra row proves more existed. Without it a short answer would be
        # indistinguishable from a complete one.
        self.assertIn("LIMIT 3", result.sql)
        self.assertTrue(result.truncated)
        # And a second statement establishes how many actually matched.
        self.assertEqual(result.total_count, 6)

    def test_a_complete_result_needs_no_count_to_know_its_total(self):
        result = self.service.query_records(grounded())
        self.assertFalse(result.truncated)
        self.assertEqual(result.total_count, result.row_count)
        self.assertEqual(result.total_count, 6)

    def test_the_total_is_left_unknown_when_counting_is_switched_off(self):
        config = RecordQueryConfig(
            duckdb=DuckDBSettings(path=self.warehouse, schema="main"),
            result=ResultSettings(count_total_when_truncated=False))
        with RecordQueryService(self.schema, config=config,
                                root=KNOWLEDGE) as service:
            result = service.query_records(grounded(), limit=2)
        self.assertTrue(result.truncated)
        # None, never a guess. "Not established" is a different answer from a
        # number and the answer layer must be able to tell them apart.
        self.assertIsNone(result.total_count)

    def test_the_total_counts_matching_rows_not_every_row(self):
        plan = grounded(filter_mappings=[
            {"field": "Round__c", "operator": "equals", "value": "Round 1"}])
        result = self.service.query_records(plan, limit=1)
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.total_count, 4)     # i1, i2, i5, i6

    def test_an_oversized_result_is_cut_and_the_cut_is_reported(self):
        config = RecordQueryConfig(
            duckdb=DuckDBSettings(path=self.warehouse, schema="main"),
            result=ResultSettings(default_limit=100, max_limit=1000,
                                  max_response_bytes=40))
        with RecordQueryService(self.schema, config=config,
                                root=KNOWLEDGE) as service:
            result = service.query_records(grounded())
        self.assertTrue(result.success, result.error_detail)
        self.assertTrue(result.truncated)
        self.assertLess(result.row_count, 6)

    # -- 14. freshness ------------------------------------------------------
    def test_freshness_is_read_from_the_warehouses_own_sync_state(self):
        result = self.service.query_records(grounded())
        self.assertIsNotNone(result.freshness)
        self.assertEqual(result.freshness.status, "fresh")
        self.assertIsNotNone(result.freshness.last_sync_at)
        self.assertLess(result.freshness.age_minutes, 45)

    def test_a_stale_replica_is_refused_when_configured_to_refuse(self):
        config = RecordQueryConfig(
            duckdb=DuckDBSettings(path=self.warehouse, schema="main"),
            freshness=FreshnessSettings(reject_after_minutes=0,
                                        reject_when_stale=True))
        with RecordQueryService(self.schema, config=config,
                                root=KNOWLEDGE) as service:
            result = service.query_records(grounded())
        self.assertFalse(result.success)
        self.assertEqual(result.error, QueryError.DATA_STALE)
        self.assertEqual(result.freshness.status, "very_stale")

    def test_the_same_staleness_is_only_reported_when_not_configured_to_refuse(self):
        config = RecordQueryConfig(
            duckdb=DuckDBSettings(path=self.warehouse, schema="main"),
            freshness=FreshnessSettings(reject_after_minutes=0,
                                        reject_when_stale=False))
        with RecordQueryService(self.schema, config=config,
                                root=KNOWLEDGE) as service:
            result = service.query_records(grounded())
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(result.freshness.status, "very_stale")

    # -- determinism and tracing -------------------------------------------
    def test_the_same_question_produces_the_same_sql(self):
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "equals", "value": "Completed"}])
        first = self.service.query_records(plan)
        second = self.service.query_records(plan)
        self.assertEqual(first.sql, second.sql)

    def test_bound_values_are_counted_in_the_trace_never_written(self):
        plan = grounded(filter_mappings=[
            {"field": "Status__c", "operator": "equals",
             "value": "Completed"}])
        result = self.service.query_records(plan)
        generated = [event for event in result.trace
                     if event.stage == "SQL_GENERATED"]
        self.assertEqual(len(generated), 1)
        details = generated[0].details
        self.assertEqual(details["param_count"], 1)
        self.assertNotIn("Completed", details["sql"])
        self.assertNotIn("params", details)

    def test_a_failure_still_reaches_the_final_trace_event(self):
        result = self.service.query_records(grounded("Not_An_Object__c"))
        stages = [event.stage for event in result.trace]
        self.assertEqual(stages[-1], "QUERY_RESULT_CREATED")
        self.assertEqual(result.trace[-1].status, "failed")

    # -- drift ---------------------------------------------------------------
    def test_drift_names_the_columns_the_warehouse_is_missing(self):
        drift = self.service.schema_drift(PRIMARY)
        self.assertEqual(drift["status"], "MISSING_COLUMN")
        self.assertIn(FIELD_NOT_REPLICATED.lower(), drift["missing_columns"])

    def test_drift_reports_a_whole_object_that_is_not_replicated(self):
        drift = self.service.schema_drift(OBJECT_NOT_REPLICATED)
        self.assertEqual(drift["status"], "MISSING_TABLE")

    # -- read-only ------------------------------------------------------------
    def test_the_warehouse_connection_is_opened_read_only(self):
        self.assertTrue(self.config.duckdb.read_only)
        with self.assertRaises(Exception):
            self.service.executor.connection.execute(
                'DELETE FROM "Recruiter__c"')


class SQLBuilderTest(unittest.TestCase):
    """The builder alone, with no database behind it."""

    def test_every_identifier_is_quoted(self):
        plan = DuckDBQueryPlan(
            primary_table=TableRef("Order", "Order", "t0"),
            select=[SelectItem("t0", "Group", "Group")])
        statement = build_sql(plan)
        self.assertIn('"main"."Order" AS "t0"', statement.sql)
        self.assertIn('"t0"."Group" AS "Group"', statement.sql)

    def test_an_identifier_carrying_a_quote_is_refused_not_escaped(self):
        plan = DuckDBQueryPlan(
            primary_table=TableRef('a"b', 'a"b', "t0"),
            select=[SelectItem("t0", "Id", "Id")])
        with self.assertRaises(Exception):
            build_sql(plan)

    def test_a_plan_selecting_nothing_is_refused(self):
        plan = DuckDBQueryPlan(primary_table=TableRef("X", "X", "t0"))
        with self.assertRaises(Exception):
            build_sql(plan)


if __name__ == "__main__":
    unittest.main()


class FreshnessThresholdTest(unittest.TestCase):
    """Thresholds follow the measured ~37-39 minute sync cadence."""

    def test_defaults_match_the_real_cadence(self):
        settings = FreshnessSettings()
        self.assertEqual(settings.expected_sync_minutes, 40)
        self.assertEqual(settings.warning_after_minutes, 80)
        self.assertEqual(settings.reject_after_minutes, 180)

    def test_an_ordinary_gap_between_syncs_is_not_stale(self):
        from record_query.freshness import FreshnessTracker

        class Conn:
            def __init__(self, age):
                self.age = age

            def execute(self, sql):
                stamp = (datetime.now(timezone.utc) - timedelta(minutes=self.age)
                         ).replace(tzinfo=None)
                return type("C", (), {"fetchone": lambda s: (stamp,)})()

        tracker = FreshnessTracker(FreshnessSettings())
        # 45.4 minutes was reported "stale" live on a healthy sync.
        self.assertEqual(tracker.check(Conn(45.4), "main").status, "fresh")
        self.assertEqual(tracker.check(Conn(85), "main").status, "stale")
        self.assertEqual(tracker.check(Conn(200), "main").status, "very_stale")

    def test_the_yaml_carries_the_same_values(self):
        from record_query.config import load_record_query_config
        freshness = load_record_query_config(KNOWLEDGE).freshness
        self.assertEqual((freshness.expected_sync_minutes,
                          freshness.warning_after_minutes,
                          freshness.reject_after_minutes), (40, 80, 180))
