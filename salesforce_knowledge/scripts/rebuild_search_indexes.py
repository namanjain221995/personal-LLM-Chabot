#!/usr/bin/env python3
"""Rebuild the FTS5 search documents without re-fetching schema."""
import argparse, json, sys
from _bootstrap import KNOWLEDGE_ROOT
from salesforce.runtime_schema.service import RuntimeSchemaService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment")
    parser.add_argument("--object", action="append", dest="objects")
    args = parser.parse_args(argv)
    with RuntimeSchemaService(root=KNOWLEDGE_ROOT,
                              environment=args.environment) as service:
        print(json.dumps(service.search_index.rebuild(args.objects), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
