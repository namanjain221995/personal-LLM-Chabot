#!/usr/bin/env python3
"""Split orchestrator/tests across CI shards, deterministically, and prove it.

WHY THIS SCRIPT EXISTS
----------------------
The orchestrator suite is 13,973 tests (4,661 + 4,756 + 4,556 collected across
the three shards on 2026-09-27; it was 13,787 the last time it ran unsharded).
Unsharded, `pytest tests -q -rs` took 3985 s and 3861 s of pure test time on two
hosted runners (runs 35646527143 and 35653694304, step "Run the orchestrator
suite"), inside jobs of 67 and 65 minutes. A single job cannot be made
meaningfully faster; three jobs can each run a third of the WORK.

A third of the WORK is not a third of the FILES. That distinction is the whole
reason shard_weights.json exists — see HOW THE FILES ARE BALANCED.

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
    split and disappearing from CI without a word;

  * the timing map BALANCES the split and never SELECTS it. `--check` does not
    read shard_weights.json at all, and a file the map has never seen is
    weighted by an estimate rather than dropped. A map that is stale, empty or
    unreadable therefore costs balance and nothing else.

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
longest-processing-time first: the heaviest file still unplaced goes into the
lightest shard.

The map has to be measured from the SHARDED job, and the first one was not. It
was derived from the two unsharded runs above. Over the 420 files the old map
and this one share, it OVERSTATED by up to 36.2 s
(tests/test_api_platform_console.py: 55.9 s then, 19.72 s now) and UNDERSTATED
by up to 31.9 s (tests/test_understanding_followups.py: 44.2 s then, 76.13 s
now) — both pairs are `git show <the previous commit>:` against this file, so
they are checkable rather than quoted. The TOTALS hid it: the old map summed to
3879.1 s against this one's 3597.1 s, 7.8% apart, and it was within 0.5% of the
3861 s unsharded run it was derived from, which is precisely why nobody looked
at it per file. Per file is where it mattered, because LPT places the heaviest
weights first: a file whose weight is OVERSTATED fills its shard on paper and
leaves it idle in fact. `--plan` predicted 21.8 minutes for all three shards
while run 260 (36311424006) ran its jobs in 16m55s, 28m13s and 20m52s — a 55%
spread behind a flat prediction. Shard 2 was 28 minutes under a 40-minute ceiling
because it had been handed more work, not because the ceiling was too low.

shard_weights.json is now measured from runs 36311316007 and 36311424006
(numbers 259 and 260, 2026-09-27), which are both the SHARDED job and both at
tree 6e986cf — byte-identical trees, so the pair is a true repeat measurement
rather than two different suites. pytest's own reported time:

    shard     run 259      run 260     files
      1      1423.41 s     960.00 s     141
      2      1613.41 s    1631.58 s     142
      3      1085.84 s    1188.98 s     142

Shard 1 ran 48% slower on run 259 than on run 260 over the same 141 files. Per
shard RUNNER SPEED, not the split, is therefore the largest single term in a
shard's wall clock, and it is why one run can never settle whether a split is
balanced. Contention is one-sided — a busy runner makes everything on it
slower, and nothing makes a test faster than the machine allows — so each
shard's per-file weights were scaled onto the LOWER of its two observed totals
and the two runs were then averaged.

Scaled ONTO, not TO: the per-file seconds do not sum to those totals, and are
not meant to. Summed over the files each shard actually ran they give 932.09 /
1593.63 / 1071.40 s against observed totals of 960.00 / 1613.41 / 1085.84 s —
short by 27.91 / 19.78 / 14.44 s, or 1.2-2.9%. The deficit is pytest's session
start, which happens before the first progress line this map is derived from
and belongs to no file. It is a one-sided deficit: a per-file map can never sum
ABOVE the run it came from. Both halves of that comparison are recorded in
`_source` (`measured_shards` and `observed_pytest_seconds`), and
test_the_map_reconciles_with_the_runs_it_was_measured_from asserts it — the one
check in the repository that can go red when these numbers are WRONG rather
than merely missing or misshapen. A refresh has to keep it true or change the
band deliberately.

Balance, with the split computed on that map and scored against each run's own
map (i.e. held out): 20.0 / 19.9 / 20.1 minutes on run 259 and 20.0 / 20.1 /
19.8 on run 260, a 1.3% spread. The split CI ran BEFORE this map scores 15.5 /
26.6 / 17.9 on the same weights, a 55% spread. Packing by FILE SIZE instead of
by time gives 21.8 / 19.9 / 22.9 against the old map (13% spread), which is why
a map is committed at all.

WHAT THE PREDICTION IS WORTH
----------------------------
The ~20.0 minutes `--plan` now prints is a PREDICTION COMPUTED FROM RECORDED
TIMINGS, not a measurement of a real CI run. Two things bound it, and neither
is removed by taking more runs of the same shape:

  * `pytest -q` prints 72 outcome characters per log line and timestamps the
    LINE, not the test. A line's elapsed time is divided evenly across its 72
    tests, so a file with fewer tests than that is charged its neighbours'
    rate. Both runs share the same neighbours, so holding one run out cannot
    detect this error. Per-test durations (`--durations`) would close it and
    would mean changing the pytest invocation, which nothing here does;

  * the runner variance above. A 20-minute shard on a runner as contended as
    run 259's shard 1 is ~30 minutes of tests plus the fixed cost of a shard,
    which over all six measured jobs (log start to the first `pytest -q`
    progress line) was 85.2 / 92.3 / 88.2 s on run 260 and 109.8 / 97.2 /
    83.0 s on run 259 — 1.4 to 1.8 minutes, mean 1.5. Against the worst of
    those the projection is ~31.8 minutes, which is what `timeout-minutes: 40`
    in pipeline.yml is sized against. The ceiling is there to kill a WEDGED
    job; it was never the fix for a shard carrying more work than its
    neighbours, and raising it would have hidden exactly the imbalance this map
    corrects.

To refresh the map after the suite has grown a lot, per shard:

    gh api repos/{owner}/{repo}/actions/jobs/<job id>/logs > job.log
    (cd orchestrator && python -m pytest $(python \
        ../.github/workflows/scripts/shard_tests.py --shard N --of 3) \
        --collect-only -q) > collect.txt

then walk the progress lines, accumulating the count of outcome characters as
the test index and the line's timestamp as the clock, and attribute the elapsed
time between a file's first and last test index to that file. Two notes that
cost an hour each to find: a test that PRINTS splits a progress line in two, so
the `[ NN%]` suffix must be optional when matching; and the per-shard collect
must be run over that shard's own file list, in that order, because pytest
collects command-line arguments in the order given.

A stale map costs BALANCE ONLY, never correctness. A file the map has never
seen is weighted by its size at the map's own measured seconds-per-byte, is
still assigned to exactly one shard, and is still run. Over today's 425 files
that estimate has a median relative error of 54% and understates the worst file
by 69 s, so it is a placeholder and not a measurement — which is why `--check`
and `--plan` both print how many files were estimated and what share of the
predicted total they carry. Measured alternatives that did NOT beat it, on the
same 425 files: counting `def test_` (62% median), the larger of the two (69%),
their geometric mean (56%).
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
#: 3, from the 2026-09-27 numbers: 3597 s of test time over the whole suite
#: (shard_weights.json, runs 36311316007 and 36311424006), ~60 s of job setup
#: per shard (container 11 s, checkout 2 s, setup-python 3 s, apt 10 s, pip
#: 17-27 s) and ~20-28 s of pytest session start before its first test
#: (imports, CREATE DATABASE, the migrations). Three shards land at ~21.5
#: minutes; two land at ~31.5 and four at ~16.5. Each extra shard also takes
#: one more concurrent hosted runner away from the nine other jobs in this
#: workflow, and a shard that QUEUES adds wall time rather than removing it.
#: The fixed cost per shard measured 1.4-1.8 minutes over the six jobs above
#: (mean 1.5), so raising this later is cheap — and safe, because the guard
#: refuses a matrix that disagrees with it.
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
#: would run zero tests in three green jobs. 425 files exist today (2026-09-27).
#: Lower it in the SAME commit if test files are deliberately deleted.
MIN_FILES = 400

WEIGHTS_PATH = pathlib.Path(__file__).resolve().parent / "shard_weights.json"

#: The least an ESTIMATED file may weigh. Only a zero-byte test file reaches it,
#: and the point is the invariant, not the number: a weight of exactly zero is
#: free work to the packer and would break "every discovered file has a
#: positive weight", which is what lets `assign` place all of them.
FLOOR_SECONDS = 0.01


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


def measured() -> dict[str, float]:
    """The per-file seconds shard_weights.json actually recorded, or nothing.

    An unreadable, truncated, absent or WRONGLY SHAPED map returns {} rather
    than raising: the map is a BALANCE input, and the coverage proof in
    `--check` does not consult it, so losing it must not be able to fail a
    build. "Wrongly shaped" includes a `seconds` key that is not an object at
    all — that one is a present key, so it survives the lookup and only fails
    when iterated, which is why it is checked separately below.

    Every returned value is a float strictly greater than zero. A non-positive
    entry is DROPPED rather than trusted, so the file falls through to the size
    estimate in `weights` instead of carrying a weight that would REDUCE its
    shard's apparent load.
    """
    try:
        raw = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))["seconds"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        # `{"seconds": null}`, `{"seconds": 3}`, `{"seconds": [1, 2]}`: the key
        # is present, so the lookup above raises nothing, and the iteration
        # below would then raise AttributeError OUT of a function whose whole
        # contract is that it cannot fail a build. Exit 1 on the guard step
        # fails every shard, which is the opposite of "balance only".
        return {}
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            out[name] = seconds
    return out


def weights(root: pathlib.Path, files: list[str]) -> dict[str, float]:
    """Seconds per file: the measured map, size-scaled for anything new.

    The seconds-per-byte rate is computed over the files the map and the
    checkout still AGREE on, so files deleted since the map was taken do not
    skew the estimate for files added since.

    A NEW test file — one the map has never seen — is therefore given
    `size in bytes * that rate` and is assigned like any other file. It is
    never left out of the split, and it never fails the build: `--check` does
    not read the map. The estimate is only a placeholder; over the 425 files
    the map covers today it has a median relative error of 54% and understates
    the worst file by 69 s. `_table` prints how many files got one.

    Every returned weight is strictly positive, FLOOR included. A zero-byte new
    file would otherwise estimate to exactly 0.0, which packs as free work and
    breaks the one thing the rest of this module leans on: every discovered
    file has a weight, so `assign` can place all of them.
    """
    known = measured()

    sizes = {f: (root / ORCHESTRATOR / f).stat().st_size for f in files}
    shared = [f for f in files if f in known]
    total_seconds = sum(known[f] for f in shared)
    total_bytes = sum(sizes[f] for f in shared)
    rate = (total_seconds / total_bytes) if (shared and total_bytes) else 1.0
    return {
        f: known[f] if f in known else max(sizes[f] * rate, FLOOR_SECONDS)
        for f in files
    }


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
    """The per-shard prediction, and how much of it is guesswork.

    The `estimated` column is the point of this function. A predicted split is
    only as good as the map behind it, and the way it silently goes bad is new
    test files: they are weighted by size, land wherever that puts them, and
    the shard that gets them runs long for reasons no one can see in a green
    log. Printing the count and the share here means the next person reading a
    slow shard can tell in one line whether the map needs refreshing, without
    re-deriving anything.

    Two conditions that look interchangeable and are not: whether the map is
    non-empty, and whether a seconds-per-byte RATE could be derived from it for
    THIS checkout. Only the second one licenses a minutes column. See
    `measured_here` below.
    """
    known = measured()
    total = sum(weight.values())

    # Whether a MINUTES column is a real number at all. `weights` derives its
    # seconds-per-byte rate over the files the map and the checkout still
    # SHARE, and falls back to a rate of 1.0 when that intersection is empty —
    # at which point every estimated weight is a raw BYTE COUNT. So the test is
    # the intersection, not merely "is the map non-empty": a map whose every
    # path has been renamed, or taken before the tests directory moved, is
    # non-empty and still buys nothing. Getting this wrong printed a
    # 16,800-byte shard as "93.3 min".
    measured_here = any(f in known for f in weight)
    rows = ["shard  files  predicted   estimated (no measured timing)"]
    for i, group in enumerate(bins, start=1):
        # `.get`, not `[...]`, in this function only: `check` calls it while
        # BUILDING the report it will print alongside its refusals, so a path
        # the split invented but discovery never found must reach the refusal
        # below as a message rather than as a KeyError out of here.
        seconds = sum(weight.get(f, 0.0) for f in group)
        guessed = [f for f in group if f not in known]
        if measured_here:
            predicted = f"{seconds / 60:>7.1f} min"
            note = "-"
            if guessed:
                note = (
                    f"{len(guessed)} file(s), "
                    f"{sum(weight.get(f, 0.0) for f in guessed) / 60:.1f} min"
                )
        else:
            # No measured seconds for anything in this checkout, so the weights
            # are raw byte counts and a minutes column would be a fabricated
            # number. Print the share of the work instead, which is all the
            # packing knew.
            share = (seconds / total * 100) if total else 0.0
            predicted = f"{share:>6.1f}% wt"
            note = f"all {len(group)} file(s)"
        rows.append(f"{i:>5}  {len(group):>5}  {predicted}   {note}")

    guessed_all = sorted(f for f in weight if f not in known)
    if not measured_here:
        rows.append(
            "shard_weights.json was unreadable or empty, or shares no file "
            "with this checkout, so EVERY weight above is a file-size estimate "
            "and the column above is a SHARE OF BYTES, not minutes. The split "
            "is still whole — coverage does not depend on the map — but the "
            "balance is not measured."
        )
    elif guessed_all:
        share = (
            (sum(weight.get(f, 0.0) for f in guessed_all) / total * 100) if total else 0.0
        )
        rows.append(
            f"{len(guessed_all)} of {len(weight)} file(s) have no measured "
            f"timing and were estimated from file size, carrying {share:.0f}% of "
            "the predicted total. Balance only: each one is still assigned to "
            "exactly one shard and still runs. Refresh shard_weights.json when "
            "this share grows — see the module docstring for how."
        )
        rows.extend(f"  estimated: {f}" for f in guessed_all[:10])
        if len(guessed_all) > 10:
            rows.append(f"  ... and {len(guessed_all) - 10} more")
    else:
        rows.append(
            f"every one of the {len(weight)} files has a measured timing in "
            "shard_weights.json. The minutes above are a PREDICTION from those "
            "recordings, not a measurement of this run."
        )
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
