"""Runtime schema subsystem: the 20 behaviours the spec asks to be covered."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from salesforce.runtime_schema.cache import SchemaCache
from salesforce.runtime_schema.config import AutoPromotion, SchemaConfig
from salesforce.runtime_schema.database import Database
from salesforce.runtime_schema.fetcher import FetchError, MirrorFetcher
from salesforce.runtime_schema.models import (Alias, ChildRelationship, PicklistValue,
                                              RecordType, Relationship, SField, SObject)
from salesforce.runtime_schema.normalizer import (invert_to_child_relationships,
                                                  name_tokens, stable_hash)
from salesforce.runtime_schema.repository import SchemaRepository
from salesforce.runtime_schema.search import SchemaSearch, _fts_query
from salesforce.runtime_schema.service import RuntimeSchemaService
from salesforce.runtime_schema.usage import UsageTracker

MIRROR = ROOT / "brain/Salesforce-Org-Data-main/Prod Org Data/force-app/main/default"


def temp_db() -> Database:
    return Database(Path(tempfile.mkdtemp()) / "t.db")


class DatabaseTest(unittest.TestCase):
    def test_initialisation_creates_every_table_and_index(self):
        db = temp_db()
        tables = {r[0] for r in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("org_info", "objects", "fields", "relationships",
                         "child_relationships", "picklist_values", "record_types",
                         "object_aliases", "field_aliases", "object_usage_stats",
                         "schema_refresh_runs", "object_search", "field_search"):
            self.assertIn(expected, tables)
        indexes = {r[0] for r in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_fields_object", indexes)
        self.assertIn("idx_object_alias", indexes)
        db.close()

    def test_describe_only_columns_default_to_null_not_false(self):
        # A NULL means "this source cannot know". Defaulting to 0 would tell a
        # planner a field is NOT filterable, which is a different claim.
        db = temp_db()
        repo = SchemaRepository(db)
        repo.upsert_objects([SObject(api_name="A__c")], source="mirror")
        row = repo.get_object("A__c")
        for column in ("key_prefix", "is_queryable", "is_filterable" if False else "is_createable"):
            self.assertIsNone(row[column], column)
        db.close()


class UpsertTest(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.repo = SchemaRepository(self.db)

    def tearDown(self):
        self.db.close()

    def test_object_upsert_is_idempotent_and_updates(self):
        self.repo.upsert_objects([SObject(api_name="X__c", label="One")],
                                 source="mirror")
        self.repo.upsert_objects([SObject(api_name="X__c", label="Two")],
                                 source="mirror")
        self.assertEqual(self.repo.counts()["objects"], 1)
        self.assertEqual(self.repo.get_object("X__c")["label"], "Two")

    def test_field_upsert_round_trips_polymorphic_reference(self):
        self.repo.upsert_objects([SObject(api_name="X__c")], source="mirror")
        self.repo.upsert_fields([SField(object_api_name="X__c", api_name="Who__c",
                                        data_type="Lookup",
                                        reference_to=["Account", "Lead"])],
                                source="mirror")
        field = self.repo.get_field("X__c", "Who__c")
        self.assertEqual(field["reference_to"], ["Account", "Lead"])

    def test_describe_values_survive_a_later_mirror_refresh(self):
        # The mirror cannot know is_filterable. A mirror refresh passing None
        # must not erase what a live describe previously found.
        self.repo.upsert_objects([SObject(api_name="X__c")], source="mirror")
        self.repo.upsert_fields([SField(object_api_name="X__c", api_name="F__c",
                                        is_filterable=True)], source="describe")
        self.repo.upsert_fields([SField(object_api_name="X__c", api_name="F__c",
                                        is_filterable=None)], source="mirror")
        self.assertEqual(self.repo.get_field("X__c", "F__c")["is_filterable"], 1)

    def test_relationship_and_child_relationship_normalisation(self):
        rels = [Relationship(source_object="Interview__c", source_field="Candidate__c",
                             target_object="Account", relationship_type="MasterDetail",
                             cascade_delete=True)]
        self.repo.upsert_relationships(rels)
        children = invert_to_child_relationships(rels)
        self.repo.upsert_child_relationships(children)
        self.assertEqual(children[0].parent_object, "Account")
        self.assertEqual(children[0].child_object, "Interview__c")
        self.assertTrue(children[0].cascade_delete)
        self.assertEqual(len(self.repo.get_child_relationships("Account")), 1)

    def test_picklist_and_record_type_normalisation(self):
        self.repo.upsert_picklist_values([
            PicklistValue(object_api_name="X__c", field_api_name="S__c",
                          value="Open", label="Open", sort_order=0)])
        self.repo.upsert_record_types([
            RecordType(object_api_name="X__c", developer_name="B2B", name="B2B")])
        self.assertEqual(self.repo.get_picklist_values("X__c", "S__c")[0]["value"], "Open")
        rt = self.repo.get_record_types("X__c")[0]
        self.assertEqual(rt["developer_name"], "B2B")
        # The mirror has no runtime ids and must not invent one.
        self.assertIsNone(rt["record_type_id"])


class NormalizerTest(unittest.TestCase):
    def test_name_tokens_split_underscore_and_camel(self):
        self.assertEqual(name_tokens("Date_of_Interview__c"), ["date", "of", "interview"])
        self.assertEqual(name_tokens("StartTime__c"), ["start", "time"])

    def test_hash_ignores_nothing_schema_bearing_but_is_stable(self):
        a = stable_hash({"label": "One", "type": "Text"})
        b = stable_hash({"type": "Text", "label": "One"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, stable_hash({"label": "Two", "type": "Text"}))


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.repo = SchemaRepository(self.db)
        self.search = SchemaSearch(self.db, self.repo)
        self.repo.upsert_objects([
            SObject(api_name="Internal_Interview__c", label="Internal Interview",
                    plural_label="Internal Interviews")], source="mirror")
        self.repo.upsert_fields([
            SField(object_api_name="Internal_Interview__c", api_name="Mock_Status__c",
                   label="Mock Status", data_type="Picklist")], source="mirror")
        self.repo.upsert_picklist_values([
            PicklistValue(object_api_name="Internal_Interview__c",
                          field_api_name="Mock_Status__c", value="Unassigned")])
        self.repo.upsert_aliases([Alias(object_api_name="Internal_Interview__c",
                                        alias="mock interview", is_manual=True)],
                                 field_level=False)
        self.search.rebuild()

    def tearDown(self):
        self.db.close()

    def test_exact_api_name_outranks_everything(self):
        results = self.search.search_objects("Internal_Interview__c")
        self.assertEqual(results[0]["api_name"], "Internal_Interview__c")
        self.assertEqual(results[0]["matched_by"], "exact_api_name")

    def test_alias_lookup(self):
        results = self.search.search_objects("mock interview")
        self.assertEqual(results[0]["api_name"], "Internal_Interview__c")
        self.assertEqual(results[0]["matched_by"], "alias")

    def test_object_fts_search(self):
        self.assertTrue(self.search.search_objects("interview"))

    def test_field_fts_search_and_picklist_text(self):
        self.assertTrue(self.search.search_fields("mock status"))
        self.assertTrue(self.search.search_fields("Unassigned"))

    def test_user_text_cannot_break_the_fts_query(self):
        # An apostrophe or a bare '*' is FTS5 syntax and would otherwise raise.
        for hostile in ("candidate's email", "inter*", '"quoted"', "a AND OR b", "()"):
            self.search.search_objects(hostile)
            self.search.search_fields(hostile)
        self.assertEqual(_fts_query("candidate's email"), '"candidate" OR "s" OR "email"')


class HotObjectTest(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.repo = SchemaRepository(self.db)
        self.usage = UsageTracker(self.db)
        self.cache = SchemaCache(self.repo, self.usage)
        self.repo.upsert_objects([SObject(api_name=n) for n in
                                  ("Account", "Interview__c", "Cold__c")],
                                 source="mirror")

    def tearDown(self):
        self.db.close()

    def test_manual_pinning_marks_and_loads(self):
        self.usage.set_pinned(["Account"])
        report = self.cache.load_hot_objects(["Account"], [], 75)
        self.assertEqual(report["pinned_loaded"], ["Account"])
        self.assertTrue(self.cache.is_hot("Account"))
        self.assertEqual(self.usage.stats("Account")["is_pinned"], 1)

    def test_auto_promotion_after_threshold(self):
        for _ in range(20):
            self.usage.record_access("Interview__c")
        promoted = self.usage.promotion_candidates(min_access_count=20,
                                                   max_hot_objects=75)
        self.assertIn("Interview__c", promoted)

    def test_below_threshold_is_not_promoted(self):
        for _ in range(5):
            self.usage.record_access("Cold__c")
        self.assertNotIn("Cold__c", self.usage.promotion_candidates(
            min_access_count=20, max_hot_objects=75))

    def test_pinned_object_is_never_an_auto_demotion_candidate(self):
        self.usage.set_pinned(["Account"])
        self.usage.mark_auto_hot(["Account"], True)
        self.assertNotIn("Account", self.usage.demotion_candidates(min_access_count=20))

    def test_new_object_starts_cold(self):
        self.repo.upsert_objects([SObject(api_name="Brand_New__c")], source="mirror")
        self.assertEqual(self.repo.get_object("Brand_New__c")["is_hot_object"], 0)
        self.assertFalse(self.cache.is_hot("Brand_New__c"))

    def test_missing_pin_is_reported_not_silently_dropped(self):
        report = self.cache.load_hot_objects(["Does_Not_Exist__c"], [], 75)
        self.assertEqual(report["missing"], ["Does_Not_Exist__c"])

    def test_cap_never_displaces_a_pinned_object(self):
        report = self.cache.load_hot_objects(["Account"], ["Interview__c"], 1)
        self.assertEqual(report["pinned_loaded"], ["Account"])
        self.assertEqual(report["skipped_over_cap"], ["Interview__c"])


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.repo = SchemaRepository(self.db)
        self.cache = SchemaCache(self.repo, UsageTracker(self.db))
        self.repo.upsert_objects([SObject(api_name="Account", label="Account")],
                                 source="mirror")
        self.repo.upsert_fields([SField(object_api_name="Account", api_name="Name")],
                                source="mirror")

    def tearDown(self):
        self.db.close()

    def test_catalog_loads_and_detail_comes_from_l1_then_l2(self):
        self.assertEqual(self.cache.load_catalog(), 1)
        _, level = self.cache.get_detailed("Account")
        self.assertEqual(level, "L2")
        self.cache.load_hot_objects(["Account"], [], 75)
        _, level = self.cache.get_detailed("Account")
        self.assertEqual(level, "L1")

    def test_invalidation_sends_the_next_read_back_to_sqlite(self):
        self.cache.load_hot_objects(["Account"], [], 75)
        self.cache.invalidate(["Account"])
        _, level = self.cache.get_detailed("Account")
        self.assertEqual(level, "L2")


@unittest.skipUnless(MIRROR.is_dir(), "metadata mirror not present")
class EndToEndTest(unittest.TestCase):
    """Against the real mirror, so the numbers are the org's own."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.config = SchemaConfig(
            root=cls.tmp, environment="test", mirror_path=str(MIRROR),
            pinned=["Account", "Interview__c"],
            auto_promotion=AutoPromotion(enabled=True, min_access_count=3,
                                         window_hours=24, max_hot_objects=10),
            snapshots_enabled=False)
        cls.service = RuntimeSchemaService(cls.config)
        cls.result = cls.service.refresh_schema()
        cls.service.start()

    @classmethod
    def tearDownClass(cls):
        cls.service.close()

    def test_refresh_populated_everything(self):
        counts = self.service.counts()
        self.assertEqual(counts["objects"], 419)
        # 4,592 fields read from the mirror, plus the standard fields every
        # object carries and the mirror omits (Id, CreatedDate, Name, ...).
        # Counted apart so a change in either is visible on its own.
        connection = self.service.db.connection
        mirror = connection.execute(
            "SELECT count(*) FROM fields WHERE source='mirror'").fetchone()[0]
        platform = connection.execute(
            "SELECT count(*) FROM fields WHERE source='platform_standard'"
        ).fetchone()[0]
        self.assertEqual(mirror, 4592)
        self.assertEqual(platform, 2994)
        self.assertEqual(counts["fields"], mirror + platform)
        self.assertGreater(counts["picklist_values"], 2000)
        self.assertEqual(self.result["status"], "success")

    def test_manifest_written_and_matches(self):
        manifest = json.loads(self.config.manifest_path.read_text())
        self.assertEqual(manifest["object_count"], 419)
        self.assertEqual(manifest["source"], "mirror")

    def test_compact_catalog_excludes_field_detail(self):
        entry = next(e for e in self.service.get_object_catalog()
                     if e["api_name"] == "Interview__c")
        self.assertIn("field_count", entry)
        self.assertNotIn("fields", entry)

    def test_detailed_schema_after_candidate_selection(self):
        detail = self.service.get_object_schema("Interview__c")
        # 257 from the mirror + Name and 7 platform fields.
        self.assertEqual(len(detail["fields"]), 265)
        names = {f["api_name"] for f in detail["fields"]}
        self.assertTrue({"Id", "Name", "CreatedDate"} <= names)
        self.assertEqual(detail["served_from"], "L1")   # pinned
        self.assertTrue(detail["record_types"])

    def test_cold_object_served_from_sqlite(self):
        cold = next(e["api_name"] for e in self.service.get_object_catalog()
                    if e["api_name"] not in ("Account", "Interview__c")
                    and e["field_count"] > 0)
        self.assertEqual(self.service.get_object_schema(cold)["served_from"], "L2")

    def test_usage_recorded_on_access(self):
        before = (self.service.usage.stats("Session__c") or {}).get("access_count", 0)
        self.service.get_object_schema("Session__c")
        after = self.service.usage.stats("Session__c")["access_count"]
        self.assertGreater(after, before)

    def test_hot_flags_written_to_objects_table(self):
        row = self.service.repo.get_object("Account")
        self.assertEqual(row["is_hot_object"], 1)
        self.assertEqual(row["hot_source"], "manual")

    def test_search_finds_a_real_object(self):
        results = self.service.search_objects("interview")
        self.assertTrue(any(r["api_name"] == "Interview__c" for r in results))

    def test_unchanged_refresh_reports_no_changed_objects(self):
        again = self.service.refresh_schema(rebuild_index=False)
        self.assertEqual(again["changed_objects"], 0)


if __name__ == "__main__":
    unittest.main()
