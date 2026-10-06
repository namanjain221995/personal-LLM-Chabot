#!/usr/bin/env python3
"""Export normalized schema to JSON for inspection. SQLite stays primary."""
import argparse, json, sys
from _bootstrap import KNOWLEDGE_ROOT
from salesforce.runtime_schema.service import RuntimeSchemaService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment")
    args = parser.parse_args(argv)
    with RuntimeSchemaService(root=KNOWLEDGE_ROOT,
                              environment=args.environment) as service:
        print(json.dumps(service.export_schema(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
