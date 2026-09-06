#!/usr/bin/env python3
"""Run ruff's CORRECTNESS rules and compare the result to a known baseline.

Only E9 (syntax errors), F63 (comparisons that can never do what they look
like), F7 (misplaced statements) and F82 (names that do not exist) are
selected. This is deliberately not a style gate: whether a line is 88 or 100
characters wide must never be able to block a security fix from shipping.

THE BASELINE, AND WHY IT EXPIRES
--------------------------------
Turning this on found one real pre-existing defect, in a file this workstream
does not own. Rather than weaken the rule, the exact finding is listed below.
The comparison is two-way:

  * a finding that is NOT in the baseline fails the build — the gate does its
    job from the first run;
  * a baseline entry that NO LONGER APPEARS also fails the build, with an
    instruction to delete it. A baseline that cannot rot is a to-do list; one
    that can is a place bugs go to hide.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

#: (finding, why it is tolerated). Delete an entry the moment it is fixed —
#: the gate will tell you to.
BASELINE: dict[str, str] = {
    # Empty, and that is the point. The one entry that lived here --
    # `sf_dictionary.py:279 F821 Undefined name \`Sequence\`` -- was a real
    # latent bug this gate found on its first run: `from __future__ import
    # annotations` made the annotation a lazy string, so it never raised on
    # import or call, but typing.get_type_hints(), pydantic or FastAPI
    # introspecting `join_map` would have. Fixed 2026-09-07 by adding
    # `Sequence` to the typing import, verified with a real get_type_hints()
    # call, and the entry deleted the same day -- which is the discipline this
    # dict exists to enforce.
}

SELECT = "E9,F63,F7,F82"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)

    proc = subprocess.run(
        ["ruff", "check", "--select", SELECT, "--output-format", "concise",
         "--exit-zero", *args.paths],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, file=sys.stderr)
        print("ruff itself failed to run", file=sys.stderr)
        return 2

    findings = [
        line.strip() for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith("Found ") and ": " in line
    ]

    new = [f for f in findings if f not in BASELINE]
    stale = [b for b in BASELINE if b not in findings]

    for f in findings:
        if f in BASELINE:
            print(f"  known  {f}\n         ({BASELINE[f].split('.')[0]}.)")

    if not new and not stale:
        print(f"\nruff ({SELECT}): clean apart from {len(BASELINE)} documented finding(s)")
        return 0

    if new:
        print(f"\n{len(new)} NEW correctness finding(s):", file=sys.stderr)
        for f in new:
            print(f"  {f}", file=sys.stderr)
    if stale:
        print(
            f"\n{len(stale)} baseline entry(ies) no longer reported — the bug was "
            "fixed. Delete them from BASELINE in "
            ".github/workflows/scripts/ruff_gate.py:",
            file=sys.stderr,
        )
        for b in stale:
            print(f"  {b}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
