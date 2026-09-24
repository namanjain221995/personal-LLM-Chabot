from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .cards import CardError, build_cards
from .catalog import CatalogError, build_catalog, write_catalog
from .embed import DEFAULT_ENDPOINT, DEFAULT_MODEL, EmbedError, build_vectors
from .graph import build_graph, read_graph, write_graph
from .lexicon import LexiconError, build_lexicon
from .resolver import Bundle, ResolverError
from .graphdb import GraphDbError, build_graphdb
from .obsidian import export_obsidian
from .queries import (
    QueryError,
    dependencies,
    fields_for_object,
    find_nodes,
    picklist_values,
    required_fields,
    traverse_dependencies,
)
from .scanner import ScanError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphrag")
    subparsers = parser.add_subparsers(dest="command", required=True)
    index = subparsers.add_parser("index", help="build a local Salesforce graph")
    index.add_argument("--source", required=True)
    index.add_argument("--output", required=True)
    query = subparsers.add_parser("query", help="query a graph artifact")
    query.add_argument("--graph", required=True)
    query.add_argument("--match", required=True, help="exact node id or name")
    query.add_argument("--kind")
    query.add_argument("--mode", choices=("find", "dependencies", "traverse"), default="dependencies")
    query.add_argument("--depth", type=int, default=1)
    fields = subparsers.add_parser("fields", help="list fields defined on a Salesforce object")
    fields.add_argument("--graph", required=True)
    fields.add_argument("--object", required=True, dest="object_name")
    picklist = subparsers.add_parser("picklist-values", help="list options for a picklist field")
    picklist.add_argument("--graph", required=True)
    picklist.add_argument("--field", required=True, dest="field_name")
    required = subparsers.add_parser("required-fields", help="list required fields")
    required.add_argument("--graph", required=True)
    required.add_argument("--object", dest="object_name")
    required.add_argument(
        "--latest-commit",
        action="store_true",
        help="reserved for a Git-backed graph; current graph artifacts have no commit metadata",
    )
    catalog = subparsers.add_parser(
        "catalog", help="build the component catalog from the DX mirror"
    )
    catalog.add_argument("--source", required=True)
    catalog.add_argument("--output", required=True)
    graphdb = subparsers.add_parser(
        "graphdb", help="load graph.jsonl into an indexed store, reconciled to the catalog"
    )
    graphdb.add_argument("--graph", required=True)
    graphdb.add_argument("--catalog", required=True)
    graphdb.add_argument("--output", required=True)
    cards = subparsers.add_parser(
        "cards", help="render deterministic fact cards for every component"
    )
    cards.add_argument("--catalog", required=True)
    cards.add_argument("--graph-db", required=True, dest="graph_db")
    cards.add_argument("--output", required=True)
    lexicon = subparsers.add_parser(
        "lexicon", help="map business vocabulary to catalog components"
    )
    lexicon.add_argument("--catalog", required=True)
    lexicon.add_argument("--output", required=True)
    lexicon.add_argument("--packs", help="brain/packs directory (optional)")
    lexicon.add_argument("--curated", help="curated.yaml (optional)")
    lexicon.add_argument("--org-schema", dest="org_schema",
                         help="org-schema.json, for standard-object labels (optional)")
    discover = subparsers.add_parser(
        "discover", help="rank the components a question is about"
    )
    discover.add_argument("--bundle", required=True)
    discover.add_argument("--query", required=True)
    discover.add_argument("--limit", type=int, default=10)
    discover.add_argument("--semantic", action="store_true",
                          help="also score by meaning (needs `graphrag embed`)")
    discover.add_argument("--rerank", action="store_true",
                          help="reorder the shortlist with the cross-encoder")
    describe = subparsers.add_parser(
        "describe", help="full detail for one component"
    )
    describe.add_argument("--bundle", required=True)
    describe.add_argument("--name", required=True)
    describe.add_argument("--text", action="store_true",
                          help="print the detail card instead of JSON")
    embed = subparsers.add_parser(
        "embed", help="embed the embeddable fact cards for semantic search"
    )
    embed.add_argument("--cards", required=True)
    embed.add_argument("--output", required=True)
    embed.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    embed.add_argument("--model", default=DEFAULT_MODEL)
    embed.add_argument("--batch-size", type=int, default=32, dest="batch_size")
    embed.add_argument("--limit", type=int)
    obsidian = subparsers.add_parser("export-obsidian", help="export a graph as an Obsidian vault")
    obsidian.add_argument("--graph", required=True)
    obsidian.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "index":
        try:
            graph = build_graph(args.source)
            write_graph(graph, args.output)
        except (OSError, UnicodeError, ScanError, ValueError) as exc:
            parser.error(str(exc))
        print(f"wrote {len(graph.nodes)} nodes and {len(graph.edges)} edges to {args.output}")
    elif args.command == "query":
        try:
            graph = read_graph(args.graph)
            if args.mode == "find":
                result = [node.to_dict() for node in find_nodes(graph, args.match, args.kind)]
            elif args.mode == "dependencies":
                result = [match.to_dict() for match in dependencies(graph, args.match, kind=args.kind)]
            else:
                result = [match.to_dict() for match in traverse_dependencies(
                    graph, args.match, depth=args.depth, kind=args.kind
                )]
        except (OSError, UnicodeError, ValueError, QueryError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "fields":
        try:
            graph = read_graph(args.graph)
            result = [field.to_dict() for field in fields_for_object(graph, args.object_name)]
        except (OSError, UnicodeError, ValueError, QueryError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "required-fields":
        if args.latest_commit:
            parser.error(
                "--latest-commit requires a graph generated from a Git checkout; "
                "the current graph format does not store commit metadata"
            )
        try:
            graph = read_graph(args.graph)
            result = [field.to_dict() for field in required_fields(graph, args.object_name)]
        except (OSError, UnicodeError, ValueError, QueryError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "picklist-values":
        try:
            graph = read_graph(args.graph)
            result = [value.to_dict() for value in picklist_values(graph, args.field_name)]
        except (OSError, UnicodeError, ValueError, QueryError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "catalog":
        try:
            components, picklists, stats = build_catalog(args.source)
            write_catalog(components, picklists, stats, args.output, args.source)
        except (OSError, UnicodeError, CatalogError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(stats, indent=2, sort_keys=True))
        print(f"wrote {stats['component_count']} components to {args.output}")
    elif args.command == "graphdb":
        try:
            stats = build_graphdb(args.graph, args.catalog, args.output)
        except (OSError, UnicodeError, GraphDbError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
        print(f"wrote {args.output}")
    elif args.command == "cards":
        try:
            stats = build_cards(args.catalog, args.graph_db, args.output)
        except (OSError, UnicodeError, CardError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
        print(f"wrote {stats.total} cards to {args.output}")
    elif args.command == "lexicon":
        try:
            stats = build_lexicon(args.catalog, args.output, args.packs,
                                  args.curated, args.org_schema)
        except (OSError, UnicodeError, LexiconError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
        print(f"wrote {stats.total} entries to {args.output}")
    elif args.command == "discover":
        try:
            with Bundle(args.bundle) as bundle:
                result = bundle.discover_schema(args.query, args.limit,
                                                semantic=args.semantic,
                                                rerank=args.rerank)
        except (OSError, ResolverError, sqlite3.Error) as exc:
            parser.error(str(exc))
        print(json.dumps(result.as_dict(), indent=2))
    elif args.command == "describe":
        try:
            with Bundle(args.bundle) as bundle:
                result = bundle.describe_object(args.name)
        except (OSError, ResolverError, sqlite3.Error) as exc:
            parser.error(str(exc))
        if args.text and result.get("detail"):
            print(result["detail"])
        else:
            print(json.dumps(result, indent=2))
    elif args.command == "embed":
        try:
            stats = build_vectors(args.cards, args.output,
                                  endpoint=args.endpoint, model=args.model,
                                  batch_size=args.batch_size, limit=args.limit)
        except (OSError, EmbedError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
    elif args.command == "export-obsidian":
        try:
            graph = read_graph(args.graph)
            count = export_obsidian(graph, args.output)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(f"wrote {count} Obsidian notes to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
