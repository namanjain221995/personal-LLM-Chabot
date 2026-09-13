#!/usr/bin/env python3
"""Prove the PUBLIC developer API's OpenAPI document is sound, and that its
surface is the surface somebody signed off.

Two questions, both of which have to be answered on every commit once
`orchestrator/app/publicapi/` exists:

  1. is the generated document well-formed OpenAPI **3.1**? A document that
     does not parse, or that is quietly still 3.0 (`nullable: true` is the
     giveaway — 3.1 deleted it in favour of `type: [..., "null"]`), breaks
     every client generator a developer will point at it, and it breaks them
     at THEIR build time, not ours;

  2. does its operation set match `public-api-surface.txt`? CONTRACT-3 §7
     names eight endpoints and says what is deliberately NOT exposed
     (Salesforce, RAG and web search, deep research, uploads, artifacts,
     memory, conversation history, admin analytics). A route added to the
     public router by accident — a stray `@router.get` on a debugging
     endpoint, a sub-router mounted at the wrong prefix — is a new piece of
     public attack surface that nobody reviewed. Diffing against a
     checked-in list makes adding one an explicit, reviewable act: the file
     changes in the same commit as the route, or the build is red.

THE SKIP, AND WHY IT IS LOUD
----------------------------
This gate was written (2026-09-12) while `orchestrator/app/publicapi/` was
still ahead of it in the programme's integration order, so it has to cope with
not existing yet. A job that is `if:`-skipped would be WORSE than useless
here: GitHub branch protection counts a skipped required check as satisfied,
and `ci_gate.py` (rightly) treats a skipped dependency as a failure. So the
job always RUNS, and this script decides: with no package it prints a banner,
a `::warning::` annotation and a step-summary block saying in as many words
that the public API contract is NOT being enforced yet, and exits 0.

The moment the package appears, every check below becomes a hard failure with
no edit to the workflow — which is the point: nobody has to remember to switch
it on, and nobody can forget. The corollary, stated so it is not a surprise:
between the package landing and it exposing a document, this gate is RED. That
is the intended reading of "the public surface is half-built", and the failure
message names the exact entry point to add.

Usage:
    api_contract.py [--orchestrator orchestrator]
                    [--expected .github/workflows/scripts/public-api-surface.txt]
                    [--require-package]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

#: Where the public document comes from. Tried in order; the FIRST one that
#: imports and yields a mapping wins. The publicapi wave may expose any one of
#: them — but it must expose one, and this list is the contract. A callable is
#: called with no arguments; a plain mapping is used as it is.
#:
#: Deliberately NOT "import app.main and read app.openapi()": the public
#: document is the PUBLIC schema only (CONTRACT-3 §7 — `GET /v1/openapi.json`
#: serves "the public schema only"), and the full application document carries
#: every internal chat, admin and analytics route. Validating that one would
#: assert nothing about what a developer can see, and would drag the whole
#: application's import graph into a lint job.
ENTRYPOINTS: list[tuple[str, str]] = [
    ("app.publicapi.openapi", "public_openapi"),
    ("app.publicapi.openapi", "build_openapi"),
    ("app.publicapi", "public_openapi"),
]

OPENAPI_VERSION = re.compile(r"^3\.1\.\d+$")

#: The operation keys a Path Item Object may carry, per OpenAPI 3.1. Anything
#: else in a path item that is not one of the documented siblings is either a
#: typo or a 3.0-ism.
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
PATH_ITEM_SIBLINGS = ("$ref", "summary", "description", "servers", "parameters")

_CHILD = r"""
import importlib, json, sys, traceback

entrypoints = json.loads(sys.argv[1])
tried = []
for mod_name, attr in entrypoints:
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        # Only "the entry point itself is not there" is a reason to try the
        # next candidate. A missing THIRD-PARTY module (fastapi, pydantic)
        # means the environment is wrong, and silently falling through to
        # "no entry point found" would blame the wrong team.
        if missing == mod_name or mod_name.startswith(missing + "."):
            tried.append("%s:%s -> no module %s" % (mod_name, attr, missing))
            continue
        print(traceback.format_exc(), file=sys.stderr)
        raise
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        raise
    target = getattr(mod, attr, None)
    if target is None:
        tried.append("%s:%s -> imported, but has no `%s`" % (mod_name, attr, attr))
        continue
    doc = target() if callable(target) else target
    json.dump({"ok": True, "source": "%s" % (mod_name + ":" + attr), "document": doc}, sys.stdout)
    sys.exit(0)

json.dump({"ok": False, "tried": tried}, sys.stdout)
sys.exit(0)
"""


# --------------------------------------------------------------------------
# Producing the document
# --------------------------------------------------------------------------
def load_document(orchestrator: pathlib.Path) -> tuple[dict | None, str, list[str]]:
    """Return (document, source, problems).

    Run in a CHILD interpreter with cwd = the orchestrator package root, so
    `app.…` resolves the same way it does in the container and so importing
    the application cannot leave this process holding a database pool, a
    logging handler or an asyncio loop.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, json.dumps([list(e) for e in ENTRYPOINTS])],
        cwd=str(orchestrator),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, "", [
            "importing the public API document raised. The child interpreter's "
            f"output follows:\n{detail}"
        ]
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, "", [f"the document generator did not print JSON: {exc}"]

    if not payload.get("ok"):
        tried = "\n".join(f"    {t}" for t in payload.get("tried", []))
        return None, "", [
            "orchestrator/app/publicapi exists, but none of the documented "
            "entry points for the public OpenAPI document is importable:\n"
            f"{tried}\n"
            "    Expose ONE of them (a callable taking no arguments, or a "
            "mapping) and this gate starts asserting the surface."
        ]

    doc = payload.get("document")
    if not isinstance(doc, dict):
        return None, str(payload.get("source", "")), [
            f"{payload.get('source')} produced {type(doc).__name__}, not a JSON object"
        ]
    return doc, str(payload.get("source", "")), []


# --------------------------------------------------------------------------
# Is it OpenAPI 3.1?
# --------------------------------------------------------------------------
def _walk(node, path: str):
    """Yield (json-pointer-ish path, dict) for every mapping in the document."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _walk(value, f"{path}/{idx}")


def _resolve_pointer(doc: dict, ref: str):
    """Resolve a local `#/a/b` JSON pointer, or raise KeyError/IndexError."""
    node = doc
    for raw in ref.lstrip("#/").split("/"):
        if raw == "":
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        else:
            node = node[token]
    return node


def validate_openapi_31(doc) -> list[str]:
    """Structural validation of an OpenAPI 3.1 document. Returns problems."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"the document is {type(doc).__name__}, not a JSON object"]

    if "swagger" in doc:
        problems.append("`swagger:` is Swagger 2.0; this must be an OpenAPI 3.1 document")

    version = doc.get("openapi")
    if not isinstance(version, str) or not OPENAPI_VERSION.match(version):
        problems.append(
            f"`openapi` is {version!r}; it must be a 3.1.x version string. "
            "3.0 and 3.1 differ in ways every client generator cares about."
        )

    info = doc.get("info")
    if not isinstance(info, dict):
        problems.append("`info` is missing or is not an object")
    else:
        for field in ("title", "version"):
            if not str(info.get(field, "")).strip():
                problems.append(f"`info.{field}` is missing or empty")

    for idx, server in enumerate(doc.get("servers") or []):
        if not isinstance(server, dict) or not str(server.get("url", "")).strip():
            problems.append(f"`servers[{idx}]` has no `url`")

    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        problems.append(
            "`paths` is missing, not an object, or empty. A public API document "
            "with no paths describes nothing."
        )
        paths = {}

    seen_operation_ids: dict[str, str] = {}
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            problems.append(f"path key {path!r} does not start with `/`")
            continue
        if not isinstance(item, dict):
            problems.append(f"the path item for `{path}` is not an object")
            continue
        operations = [k for k in item if k in METHODS]
        unknown = [
            k for k in item
            if k not in METHODS and k not in PATH_ITEM_SIBLINGS and not k.startswith("x-")
        ]
        if unknown:
            problems.append(f"`{path}` carries unknown path-item key(s): {', '.join(sorted(unknown))}")
        if not operations and "$ref" not in item:
            problems.append(f"`{path}` declares no HTTP operation")
        for method in operations:
            op = item[method]
            where = f"{method.upper()} {path}"
            if not isinstance(op, dict):
                problems.append(f"`{where}` is not an operation object")
                continue
            responses = op.get("responses")
            if not isinstance(responses, dict) or not responses:
                problems.append(f"`{where}` declares no `responses`")
            op_id = op.get("operationId")
            if op_id is not None:
                if not isinstance(op_id, str) or not op_id.strip():
                    problems.append(f"`{where}` has an empty `operationId`")
                elif op_id in seen_operation_ids:
                    problems.append(
                        f"`operationId` {op_id!r} is used by both "
                        f"`{seen_operation_ids[op_id]}` and `{where}`; generated "
                        "clients collide on duplicates"
                    )
                else:
                    seen_operation_ids[op_id] = where

    for pointer, node in _walk(doc, ""):
        # 3.0's `nullable: true` is not a keyword in 3.1 — a generator reading
        # it as 3.1 silently drops the nullability from the client's types.
        if isinstance(node.get("nullable"), bool):
            problems.append(
                f"`{pointer or '/'}` uses `nullable:`, which OpenAPI 3.1 removed. "
                'Use `type: [..., "null"]` instead.'
            )
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                problems.append(
                    f"`{pointer or '/'}` has a non-local `$ref` ({ref!r}). The "
                    "public document must be self-contained: a developer "
                    "fetching it cannot resolve a reference to our filesystem."
                )
            else:
                try:
                    _resolve_pointer(doc, ref)
                except (KeyError, IndexError, ValueError):
                    problems.append(f"`{pointer or '/'}` references {ref!r}, which does not exist")

    return problems


# --------------------------------------------------------------------------
# Is it the surface we signed off?
# --------------------------------------------------------------------------
def parse_expectation(text: str) -> tuple[set[str], list[str]]:
    """Parse `public-api-surface.txt` into a set of `METHOD /path` operations."""
    wanted: set[str] = set()
    problems: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            problems.append(f"line {lineno}: expected `METHOD /path`, got {raw.strip()!r}")
            continue
        method, path = parts
        if method.lower() not in METHODS:
            problems.append(f"line {lineno}: {method!r} is not an HTTP method")
            continue
        if not path.startswith("/"):
            problems.append(f"line {lineno}: {path!r} does not start with `/`")
            continue
        wanted.add(f"{method.upper()} {path}")
    return wanted, problems


def document_operations(doc: dict) -> set[str]:
    found: set[str] = set()
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        # validate_openapi_31 has already reported this; returning nothing here
        # keeps the surface diff from raising on top of a problem it did not
        # find and cannot explain.
        return found
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            if method in item:
                found.add(f"{method.upper()} {path}")
    return found


def compare_surface(doc: dict, expected: set[str], expected_path: str) -> list[str]:
    found = document_operations(doc)
    added = sorted(found - expected)
    removed = sorted(expected - found)
    if not added and not removed:
        return []

    lines = [
        "the public API surface is not the one that was signed off. Every "
        "difference below is a deliberate decision or a mistake, and the way "
        f"to say which is to edit {expected_path} in the SAME commit as the "
        "route change, with a comment saying why."
    ]
    if added:
        lines.append(
            "  NEW, and not in the expectation file (each one is public attack "
            "surface a reviewer has not seen):"
        )
        lines += [f"    + {op}" for op in added]
    if removed:
        lines.append(
            "  EXPECTED, but the document no longer describes them (a removed "
            "public endpoint breaks every developer already calling it):"
        )
        lines += [f"    - {op}" for op in removed]
    return ["\n".join(lines)]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def _summary(block: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(block)


def _not_yet(package: pathlib.Path, expected_path: pathlib.Path, count: int) -> int:
    banner = "=" * 74
    print(banner)
    print("PUBLIC API CONTRACT: NOT ENFORCED YET")
    print(banner)
    print(f"  {package} does not exist, so there is no public OpenAPI document")
    print("  to validate. This job is a placeholder that PASSES, and it says so")
    print("  in the run summary rather than going quietly green.")
    print("")
    print(f"  The expectation file is already checked in ({expected_path}) and")
    print(f"  lists {count} operation(s) taken from CONTRACT-3 §7. The moment the")
    print("  package appears, this becomes a hard failure on any mismatch — no")
    print("  workflow edit, no switch to remember to flip.")
    print(banner)
    # An annotation as well as the log: a passing job's log is not read, and
    # "we are not checking this yet" has to be visible from the run's front page.
    print(
        "::warning title=Public API contract not enforced yet::"
        f"{package} does not exist, so the public OpenAPI document was not "
        "validated. This check turns into a hard failure automatically once "
        "the package lands."
    )
    _summary(
        "### API contract\n\n"
        f"**Not enforced yet.** `{package}` does not exist, so no public OpenAPI "
        "document was generated or validated.\n\n"
        f"The expectation file `{expected_path}` is checked in and lists "
        f"{count} operation(s) from CONTRACT-3 §7. This check becomes blocking "
        "on its own the moment the package lands.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orchestrator", default="orchestrator", help="the orchestrator package root")
    ap.add_argument(
        "--expected",
        default=".github/workflows/scripts/public-api-surface.txt",
        help="the checked-in operation list the document must match",
    )
    ap.add_argument(
        "--require-package",
        action="store_true",
        help="fail instead of skipping when the publicapi package is absent",
    )
    args = ap.parse_args(argv)

    orchestrator = pathlib.Path(args.orchestrator)
    package = orchestrator / "app" / "publicapi"
    expected_path = pathlib.Path(args.expected)

    if not expected_path.is_file():
        print(
            f"FATAL: the expectation file {expected_path} is missing. It is "
            "checked in on purpose: without it this gate cannot tell a new "
            "public endpoint from an intended one, and deleting it would be a "
            "way to make an unreviewed surface change go green.",
            file=sys.stderr,
        )
        return 2
    expected, expectation_problems = parse_expectation(expected_path.read_text(encoding="utf-8"))
    if expectation_problems:
        print(f"FATAL: {expected_path} does not parse:", file=sys.stderr)
        for problem in expectation_problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    if not package.is_dir():
        if args.require_package:
            print(f"FATAL: {package} does not exist and --require-package was given", file=sys.stderr)
            return 1
        return _not_yet(package, expected_path, len(expected))

    doc, source, problems = load_document(orchestrator)
    if doc is None:
        print("Public API contract: FAILED\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        _summary(
            "### API contract\n\n"
            f"**The public OpenAPI document could not be generated** from `{package}`.\n\n"
            "This gate turned blocking by itself the moment that package appeared, "
            "which is the design: an incomplete public surface should be visible, "
            "not silent. It goes green as soon as the package exposes one of:\n\n"
            + "".join(f"- `{mod}:{attr}`\n" for mod, attr in ENTRYPOINTS)
            + "\nEach is a callable taking no arguments (or a plain mapping) that "
            "returns the PUBLIC document — the `/v1` schema only, not the whole "
            "application's. See the job log for what was tried.\n"
        )
        return 1

    print(f"public OpenAPI document generated by {source}")
    problems = validate_openapi_31(doc)
    problems += compare_surface(doc, expected, str(expected_path))

    operations = sorted(document_operations(doc))
    if not problems:
        print(f"OpenAPI {doc.get('openapi')}: well-formed, {len(operations)} operation(s), "
              f"all matching {expected_path}")
        for op in operations:
            print(f"  ok  {op}")
        _summary(
            "### API contract\n\n"
            f"`{source}` produced a well-formed OpenAPI **{doc.get('openapi')}** "
            f"document with **{len(operations)}** operation(s), exactly matching "
            f"`{expected_path}`.\n\n"
            + "".join(f"- `{op}`\n" for op in operations)
        )
        return 0

    print(f"\nPublic API contract: {len(problems)} problem(s)\n", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}\n", file=sys.stderr)
    _summary(
        "### API contract\n\n"
        f"**{len(problems)} problem(s)** with the public OpenAPI document — see the job log.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
