#!/usr/bin/env python3
"""Check the runtime schema store is internally consistent.

Every check is read-only. Exit 0 when all pass, 1 when any fail.
"""
import argparse, json, sys
from _bootstrap import KNOWLEDGE_ROOT
from salesforce.runtime_schema.service import RuntimeSchemaService


def validate(service) -> list[dict]:
    connection = service.db.connection
    checks: list[dict] = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "status": "pass" if ok else "FAIL",
                       "detail": detail})

    check("database exists", service.config.database_path.is_file(),
          str(service.config.database_path))

    counts = service.counts()
    check("objects populated", counts["objects"] > 0, f"{counts['objects']} objects")
    check("fields populated", counts["fields"] > 0, f"{counts['fields']} fields")

    orphans = connection.execute(
        "SELECT count(*) FROM fields f WHERE NOT EXISTS"
        " (SELECT 1 FROM objects o WHERE o.api_name = f.object_api_name)"
    ).fetchone()[0]
    check("no orphan fields", orphans == 0, f"{orphans} fields without an object")

    bad_source = connection.execute(
        "SELECT count(*) FROM relationships r WHERE NOT EXISTS"
        " (SELECT 1 FROM fields f WHERE f.object_api_name = r.source_object"
        "   AND f.api_name = r.source_field)").fetchone()[0]
    check("relationship source fields exist", bad_source == 0,
          f"{bad_source} relationships with a missing source field")

    # A lookup may legitimately target an object outside the retrieve, so this
    # reports rather than fails.
    unknown_targets = connection.execute(
        "SELECT count(DISTINCT target_object) FROM relationships r WHERE NOT"
        " EXISTS (SELECT 1 FROM objects o WHERE o.api_name = r.target_object)"
    ).fetchone()[0]
    checks.append({"check": "relationship targets known", "status": "info",
                   "detail": f"{unknown_targets} target objects not in this store"})

    bad_picklist = connection.execute(
        "SELECT count(*) FROM picklist_values p WHERE NOT EXISTS"
        " (SELECT 1 FROM fields f WHERE f.object_api_name = p.object_api_name"
        "   AND f.api_name = p.field_api_name)").fetchone()[0]
    check("picklist fields exist", bad_picklist == 0,
          f"{bad_picklist} picklist values without a field")

    bad_rt = connection.execute(
        "SELECT count(*) FROM record_types rt WHERE NOT EXISTS"
        " (SELECT 1 FROM objects o WHERE o.api_name = rt.object_api_name)"
    ).fetchone()[0]
    check("record type objects exist", bad_rt == 0,
          f"{bad_rt} record types without an object")

    known = set(service.repo.object_names())
    missing_pins = [p for p in service.config.pinned if p not in known]
    check("pinned objects exist", not missing_pins, ", ".join(missing_pins) or "all present")

    fts_objects = connection.execute("SELECT count(*) FROM object_search").fetchone()[0]
    fts_fields = connection.execute("SELECT count(*) FROM field_search").fetchone()[0]
    check("object FTS populated", fts_objects > 0, f"{fts_objects} documents")
    check("field FTS populated", fts_fields > 0, f"{fts_fields} documents")

    manifest_path = service.config.manifest_path
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        agree = (manifest.get("object_count") == counts["objects"]
                 and manifest.get("field_count") == counts["fields"])
        check("manifest matches database", agree,
              f"manifest {manifest.get('object_count')}/{manifest.get('field_count')}"
              f" vs db {counts['objects']}/{counts['fields']}")
    else:
        check("manifest present", False, str(manifest_path))
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    with RuntimeSchemaService(root=KNOWLEDGE_ROOT,
                              environment=args.environment) as service:
        checks = validate(service)
    failed = [c for c in checks if c["status"] == "FAIL"]
    if args.json:
        print(json.dumps({"checks": checks, "failed": len(failed)}, indent=2))
    else:
        for c in checks:
            print(f"  {c['status']:4s}  {c['check']:36s} {c['detail']}")
        print(f"\n  {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
