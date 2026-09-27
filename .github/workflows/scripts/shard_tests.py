#!/usr/bin/env python3
"""Split orchestrator/tests across CI shards, deterministically, and prove it.

WHY THIS SCRIPT EXISTS
----------------------
The orchestrator suite is 13,787 tests. `pytest tests -q -rs` took 3985 s and
3861 s of pure test time on two hosted runners (runs 35646527143 and
35653694304, step "Run the orchestrator suite"), inside jobs of 67 and 65
minutes. A single job cannot be made meaningfully faster; three jobs can each
run a third of the files.

The danger of a hand-rolled split is not that it is slow. It is that it is
QUIET. A file that no shard claims still leaves every shard GREEN, and the run
then reports success for tests that never executed — a build that tested less
than it claims, which is worse than one that took an hour. Closing that hole is
most of this file.

The contract:

  * the assignment is a pure function of the checkout. No randomness, no
    environment, no network, no pytest plugin (requirements-dev.txt pins
    `pytest>=8` and nothing else, and this script imports only the standard
    library). Every shard derives the SAME assignment from the same commit, so
    "the union is everything" is a property of the algorithm, not of luck;

  * `--check` re-derives the assignment and asserts POSITIVELY that every
    discovered test file appears in exactly one shard, that no shard is empty,
    and that at least MIN_FILES files were discovered at all. An empty
    discovery satisfies "every file is in exactly one shard" vacuously, so the
    floor is part of the guard and not a nicety;

  * discovery is what PYTEST would collect — `test_*.py` and `*_test.py` at ANY
    depth under orchestrator/tests — not a top-level glob. A nested test
    directory added later is assigned and run, rather than falling outside the
    split and disappearing from CI without a word.

THE SHARD COUNT LIVES HERE, NOT IN THE WORKFLOW
-----------------------------------------------
`SHARDS` below is the single source of truth. The workflow passes the size of
the matrix GitHub ACTUALLY expanded (`strategy.job-total`) as `--of`, and this
script refuses to do anything when the two disagree. Editing
`matrix: shard: [...]` without editing this constant therefore fails every
shard loudly instead of silently dropping one shard's files.

HOW THE FILES ARE BALANCED
--------------------------
By measured wall time per file, from shard_weights.json, packed
longest-processing-time first.

`pytest -q` prints one character per test in collection order, 72 to a line,
and the Actions log timestamps every line. Cumulative character count therefore
maps to wall clock, and `pytest --collect-only -q` at the same commit maps
cumulative test index to file. Both runs above are at e0dc93b, which is this
branch's merge base, so the map is exact rather than estimated. The two runs
were derived independently and averaged.

Balance, with the split computed on the average map and scored against each
run's own map (i.e. held out): 21.8 / 22.0 / 21.8 minutes on run 1 and
21.3 / 21.1 / 21.3 on run 2 — 0.8% spread. Packing by FILE SIZE instead gives
21.8 / 19.9 / 22.9 (13% spread), which is why the map is committed.

To refresh the map after the suite has grown a lot:

    gh api repos/{owner}/{repo}/actions/jobs/<job id>/logs > job.log
    (cd orchestrator && python -m pytest tests --collect-only -q) > collect.txt

then walk the `[ NN%]` progress lines, accumulating len(line minus the
timestamp and the percentage) as the test index and the line's timestamp as the
clock, and attribute the elapsed time between a file's first and last test
index to that file.

A stale map costs BALANCE ONLY, never correctness: a file the map has never
seen is weighted by its size at the map's own measured seconds-per-byte, and
`--check` does not consult the map at all.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

#: How many shards the `orchestrator` matrix expands to. Changing this number
#: means changing `matrix: shard:` in pipeline.yml in the same commit; the
#: `--of` cross-check below is what makes that mandatory rather than hoped for.
#:
#: 3, from the measured numbers: 3985 s and 3861 s of pytest, ~50 s of job
#: setup (container 11 s, checkout 3 s, setup-python 4 s, apt 9 s, pip 23 s)
#: and ~60 s of pytest session start (imports, CREATE DATABASE, the migrations)
#: per shard. Three shards land at ~23 minutes and hit the ~25 minute target;
#: two land at ~34 and miss it. Four would reach ~18, but each extra shard also
#: takes one more concurrent hosted runner away from the nine other jobs in
#: this workflow, and a shard that QUEUES adds wall time rather than removing
#: it. The fixed cost per shard is under two minutes, so raising this later is
#: cheap — and safe, because the guard refuses a matrix that disagrees with it.
SHARDS = 3

#: Paths are relative to the orchestrator directory, because that is the
#: workflow step's `working-directory` and what it hands to pytest.
ORCHESTRATOR = "orchestrator"
TESTS = "tests"

#: pytest's default `python_files`. There is no pytest ini table anywhere in
#: this repository (checked: no pytest.ini, no tox.ini, no setup.cfg, and the
#: root pyproject.toml has no [tool.pytest.ini_options]), so the defaults are
#: what actually applies. If one is ever added with a `python_files` override,
#: this list has to follow it or the split starts missing files.
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")

#: Directories that never hold collectable tests.
SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".venv", ".git"})

#: A floor on discovery, in the spirit of unittest_gate.py's --min-tests: an
#: empty file list satisfies "every file is in exactly one shard" vacuously and
#: would run zero tests in three green jobs. 420 files exist today. Lower it in
#: the SAME commit if test files are deliberately deleted.
MIN_FILES = 400

WEIGHTS_PATH = pathlib.Path(__file__).resolve().parent / "shard_weights.json"


def repo_root() -> pathlib.Path:
    """.github/workflows/scripts/shard_tests.py -> the repository root."""
    return pathlib.Path(__file__).resolve().parents[3]


def discover(root: pathlib.Path) -> list[str]:
    """Every file pytest would collect under orchestrator/tests, sorted.

    Returned relative to the orchestrator directory and POSIX-separated, which
    is exactly the form the workflow passes to pytest.
    """
    base = root / ORCHESTRATOR / TESTS
    found: set[str] = set()
    for pattern in TEST_FILE_PATTERNS:
        for path in base.rglob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root / ORCHESTRATOR)
            if SKIP_DIRS.intersection(rel.parts):
                continue
            found.add(rel.as_posix())
    return sorted(found)


def weights(root: pathlib.Path, files: list[str]) -> dict[str, float]:
    """Seconds per file: the measured map, size-scaled for anything new.

    The seconds-per-byte rate is computed over the files the map and the
    checkout still AGREE on, so files deleted since the map was taken do not
    skew the estimate for files added since.
    """
    try:
        known = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))["seconds"]
    except (OSError, KeyError, json.JSONDecodeError):
        known = {}

    sizes = {f: (root / ORCHESTRATOR / f).stat().st_size for f in files}
    shared = [f for f in files if f in known]
    total_seconds = sum(float(known[f]) for f in shared)
    total_bytes = sum(sizes[f] for f in shared)
    rate = (total_seconds / total_bytes) if (shared and total_bytes) else 1.0
    return {f: float(known[f]) if f in known else sizes[f] * rate for f in files}


def assign(files: list[str], weight: dict[str, float], shards: int) -> list[list[str]]:
    """Longest-processing-time bin packing: heaviest file into the lightest
    shard, ties broken by path and then by shard index.

    Deterministic in every step, so all shards agree without communicating.
    Each shard's list is returned sorted by path: a stable, readable order, and
    one no test may depend on — the suite already TRUNCATEs every table before
    every test, so cross-file ordering is not a thing tests are allowed to rely
    on, and sharding would break it anywhere it had crept in.
    """
    bins: list[list[str]] = [[] for _ in range(shards)]
    load = [0.0] * shards
    for name in sorted(files, key=lambda f: (-weight[f], f)):
        target = min(range(shards), key=lambda i: (load[i], i))
        bins[target].append(name)
        load[target] += weight[name]
    return [sorted(b) for b in bins]


def plan(root: pathlib.Path, shards: int) -> tuple[list[str], list[list[str]], dict[str, float]]:
    files = discover(root)
    weight = weights(root, files)
    return files, assign(files, weight, shards), weight


def _table(bins: list[list[str]], weight: dict[str, float]) -> list[str]:
    rows = ["shard  files  predicted"]
    for i, group in enumerate(bins, start=1):
        seconds = sum(weight[f] for f in group)
        rows.append(f"{i:>5}  {len(group):>5}  {seconds / 60:>7.1f} min")
    return rows


def check(root: pathlib.Path, shards: int) -> tuple[bool, list[str]]:
    """Assert the split covers everything exactly once. Returns (ok, report)."""
    files, bins, weight = plan(root, shards)
    known = set(files)
    counted: dict[str, int] = {}
    for group in bins:
        for name in group:
            counted[name] = counted.get(name, 0) + 1

    problems: list[str] = []
    if len(files) < MIN_FILES:
        problems.append(
            f"discovery found {len(files)} test files under "
            f"{ORCHESTRATOR}/{TESTS}, below the floor of {MIN_FILES}. Three "
            "green shards that ran nothing is the failure this floor exists "
            "for; if files were deleted on purpose, lower MIN_FILES in the "
            "same commit."
        )
    missing = sorted(f for f in files if counted.get(f, 0) == 0)
    if missing:
        problems.append(
            f"{len(missing)} test file(s) are in NO shard, so they would not "
            f"run anywhere: {', '.join(missing[:10])}"
            + (" ..." if len(missing) > 10 else "")
        )
    duplicated = sorted(f for f, n in counted.items() if n > 1)
    if duplicated:
        problems.append(
            f"{len(duplicated)} test file(s) are in MORE than one shard: "
            f"{', '.join(duplicated[:10])}" + (" ..." if len(duplicated) > 10 else "")
        )
    invented = sorted(f for f in counted if f not in known)
    if invented:
        problems.append(
            f"the split produced {len(invented)} path(s) that discovery did "
            f"not find: {', '.join(invented[:10])}"
        )
    empty = [i for i, group in enumerate(bins, start=1) if not group]
    if empty:
        problems.append(
            f"shard(s) {empty} would run no files at all. A shard with nothing "
            "in it is a pytest invocation with no arguments, which collects the "
            "whole repository."
        )

    report = [
        f"{len(files)} test files under {ORCHESTRATOR}/{TESTS}, "
        f"{shards} shard(s), each file in exactly one",
        *_table(bins, weight),
    ]
    return not problems, report + ([""] + [f"REFUSED: {p}" for p in problems] if problems else [])


def _write_summary(title: str, ok: bool, report: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"### {title}: {'PASSED' if ok else 'FAILED'}\n\n```\n")
        fh.write("\n".join(report) + "\n```\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shard", type=int, help=f"which shard to print, 1..{SHARDS}")
    ap.add_argument(
        "--of",
        type=int,
        required=True,
        help="how many shards CI actually expanded (pass ${{ strategy.job-total }}); "
        f"it must equal SHARDS ({SHARDS})",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="assert every discovered test file is in exactly one shard",
    )
    ap.add_argument("--plan", action="store_true", help="print the per-shard table")
    args = ap.parse_args(argv)

    if args.of != SHARDS:
        # The one failure this cross-check exists for: the workflow matrix and
        # this file disagree about how many shards there are. Whichever is
        # right, some files are about to run in no job at all.
        print(
            f"--of {args.of} but shard_tests.SHARDS is {SHARDS}. The matrix in "
            "pipeline.yml and this script disagree on the shard count, so the "
            "split does not cover the suite. Change both in one commit.",
            file=sys.stderr,
        )
        return 2

    root = repo_root()

    if args.check:
        ok, report = check(root, args.of)
        print("\n".join(report))
        _write_summary("Shard coverage", ok, report)
        if not ok:
            print(
                "\nRefusing to pass: a split that does not cover every test "
                "file exactly once makes a GREEN run that tested less than it "
                "claims.",
                file=sys.stderr,
            )
        return 0 if ok else 1

    if args.plan:
        _, bins, weight = plan(root, args.of)
        print("\n".join(_table(bins, weight)))
        return 0

    if args.shard is None:
        ap.error("one of --shard, --check or --plan is required")
    if not 1 <= args.shard <= args.of:
        print(f"--shard {args.shard} is outside 1..{args.of}", file=sys.stderr)
        return 2

    _, bins, _ = plan(root, args.of)
    print("\n".join(bins[args.shard - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
