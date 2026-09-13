#!/usr/bin/env python3
"""Run a `unittest` suite and refuse a run that collected nothing.

WHY THIS SCRIPT EXISTS — the empty-suite trap
---------------------------------------------
`python -m unittest discover -s launcher/tests` reports success when it
discovers ZERO tests on every Python this repository supports except 3.12+:

    $ python3.11 -m unittest discover -s /empty
    Ran 0 tests in 0.000s
    OK                                       # ... and exit code 0

The launcher job runs a MATRIX (3.11 and 3.12), so the same mistake — a
renamed directory, a `PYTHONPATH` that stopped resolving, a `tests/`
package that lost its `__init__.py` — is red on one leg and green on the
other, and a half-green matrix reads as "flaky", not as "the suite is gone".
Python 3.12 added the `NO TESTS RAN` exit code 5, but relying on it would
mean the 3.11 leg silently asserts nothing, which is exactly the shape of
defect this repository has been bitten by before (see the `tail -30` pipe in
pipeline.yml's launcher job: four runs reported success over six real
failures because a pipeline reports the LAST command's status).

So discovery is done HERE, in process, and the count is asserted BEFORE the
suite runs, identically on every interpreter.

THE FLOOR IS NOT JUST "MORE THAN ZERO"
--------------------------------------
`--min-tests` takes a real floor, not 1. Zero is the loud failure; the
quiet one is a single module dropping out of discovery and taking a hundred
tests with it while the remaining four hundred still pass. A floor set a
little under the current count catches that, and the day someone deliberately
deletes tests they have to lower it in the same commit — which is a review,
not an accident.

AND THE FLOOR IS CHECKED AGAINST WHAT RAN, NOT WHAT WAS COLLECTED
-----------------------------------------------------------------
The first version (2026-09-12) compared `countTestCases()` to the floor and
then ran the suite. A module that raises `unittest.SkipTest` at import time —
`if shutil.which("docker") is None: raise SkipTest(...)`, one line — is still
COLLECTED: discovery turns it into a synthetic skipped case, and a class with
`@unittest.skip` keeps every one of its methods in the count. So a floor of
450 was satisfied by 450 tests of which none executed, and the run printed
`OK (skipped=450)` in green (2026-09-13 CI review of the wave-1 gates). The
collected count is still checked first, because it is a cheap early refusal;
the floor that decides is the number of tests that STARTED and were not then
skipped.

Import errors are reported FIRST and on their own. `TestLoader.discover`
turns a module that fails to import into a synthetic `_FailedTest`, which
counts towards `countTestCases()` — so a suite that imports nothing at all
can still clear the floor. Naming them before the count is checked means the
message says "this module does not import", not "too few tests".

Usage:
    unittest_gate.py --start launcher/tests --min-tests 400
"""
from __future__ import annotations

import argparse
import sys
import unittest

#: 0 = the suite passed, 1 = it did not (or there is not enough of it),
#: 2 = this script was asked to do something impossible.
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def discover(start: str, top_level: str | None) -> tuple[unittest.TestLoader, unittest.TestSuite]:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=start, top_level_dir=top_level)
    return loader, suite


class _CountingResult(unittest.TextTestResult):
    """A text result that also knows which tests actually executed.

    `testsRun` alone is not it: a module-level skip increments `testsRun` and
    `skipped` together, but a `setUpClass` that raises SkipTest adds to
    `skipped` WITHOUT a matching `testsRun`, so `testsRun - len(skipped)`
    would under-count. Tracking ids is exact in both shapes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids: set[str] = set()
        self.skipped_ids: set[str] = set()

    def startTest(self, test):  # noqa: N802 - unittest's name
        self.started_ids.add(test.id())
        super().startTest(test)

    def addSkip(self, test, reason):  # noqa: N802 - unittest's name
        self.skipped_ids.add(test.id())
        super().addSkip(test, reason)

    @property
    def executed(self) -> int:
        return len(self.started_ids - self.skipped_ids)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="the directory to discover tests in")
    ap.add_argument(
        "--top-level-dir",
        default=None,
        help="unittest's top-level directory; defaults to --start, as `discover -s` does",
    )
    ap.add_argument(
        "--min-tests",
        type=int,
        required=True,
        help="refuse the run if discovery collected fewer than this many tests",
    )
    ap.add_argument("--label", default=None, help="what to call this suite in the output")
    args = ap.parse_args(argv)

    if args.min_tests < 1:
        print(
            "FATAL: --min-tests must be at least 1. A floor of zero is not a "
            "floor: it is the defect this script exists to catch.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    label = args.label or args.start

    try:
        loader, suite = discover(args.start, args.top_level_dir)
    except (ImportError, OSError) as exc:
        print(f"FATAL: cannot discover tests under {args.start}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Import failures first, and alone: they are why a count is wrong far more
    # often than the count itself is interesting.
    if loader.errors:
        print(
            f"\n{len(loader.errors)} module(s) under {label} could not be imported, "
            "so their tests were never collected:\n",
            file=sys.stderr,
        )
        for err in loader.errors:
            print(err, file=sys.stderr)
        return EXIT_FAIL

    collected = suite.countTestCases()
    print(f"{label}: discovery collected {collected} test(s); the floor is {args.min_tests}")
    if collected < args.min_tests:
        print(
            f"\nRefusing to run: only {collected} test(s) were collected under "
            f"{args.start}, and this suite is expected to have at least "
            f"{args.min_tests}. Either discovery is broken (a renamed directory, "
            "a PYTHONPATH that no longer resolves, a package that lost its "
            "__init__.py) or tests were deleted — and if they were deleted on "
            "purpose, lower --min-tests in the same commit so the decision is "
            "reviewable.",
            file=sys.stderr,
        )
        return EXIT_FAIL

    # verbosity=1: dots, not names. A green launcher run stays about sixty
    # lines, and a failure still carries its whole traceback because nothing
    # here pipes or truncates the runner's output.
    result = unittest.TextTestRunner(verbosity=1, resultclass=_CountingResult).run(suite)
    executed = result.executed
    skipped = len(result.skipped)
    print(
        f"{label}: {executed} test(s) executed, {skipped} skipped, "
        f"of {collected} collected; the floor is {args.min_tests}"
    )
    if executed < args.min_tests:
        print(
            f"\nRefusing the run: only {executed} test(s) actually executed under "
            f"{args.start} ({skipped} were skipped), and this suite is expected "
            f"to execute at least {args.min_tests}. A skip is not a pass: a "
            "module-level `raise unittest.SkipTest`, a skipped class or a "
            "missing tool on the runner has switched these tests off. Either "
            "restore what they need on this runner, or lower --min-tests in the "
            "same commit so the decision is reviewable.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    return EXIT_OK if result.wasSuccessful() else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
