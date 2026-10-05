#!/usr/bin/env python3
"""Refresh the runtime schema from its configured source."""
import argparse, json, logging, sys
from _bootstrap import KNOWLEDGE_ROOT
from salesforce.runtime_schema.service import RuntimeSchemaService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment")
    parser.add_argument("--object", action="append", dest="objects",
                        help="refresh only these objects (repeatable)")
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    with RuntimeSchemaService(root=KNOWLEDGE_ROOT,
                              environment=args.environment) as service:
        result = service.refresh_schema(args.objects, not args.no_index)
        print(json.dumps({k: v for k, v in result.items() if k != "manifest"},
                         indent=2))
        if args.export:
            print(json.dumps({"exports": service.export_schema()}, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
