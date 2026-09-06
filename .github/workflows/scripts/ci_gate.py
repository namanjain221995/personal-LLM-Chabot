#!/usr/bin/env python3
"""The aggregate release gate: assert every required job actually SUCCEEDED.

WHY THIS SCRIPT EXISTS — the `always()` trap
--------------------------------------------
A gate job needs `if: always()`, because a gate that is SKIPPED when an
upstream job fails is worse than no gate at all: GitHub branch protection
treats a *skipped* required check as satisfied, so the PR goes green. The gate
must therefore always run, and always be the thing that says no.

But `always()` only controls whether the gate RUNS. It says nothing about what
the upstream jobs did. The classic broken gate is:

    if: always()
    run: '[ "${{ contains(needs.*.result, 'failure') }}" = "false" ]'

`needs.<job>.result` is one of: success, failure, cancelled, skipped. The
expression above is false — i.e. the gate PASSES — when a job was `skipped` or
`cancelled`, because neither string is 'failure'. A job skips for many reasons
that are not "everything is fine": an `if:` that evaluated false, a dependency
that failed so the job never started, a matrix that expanded to nothing, a
concurrency cancellation, a workflow-level cancel, or a `needs:` entry someone
deleted while refactoring. Every one of those has to be a red gate.

So: run always, and then assert POSITIVELY that each named job reported
exactly `success`. Anything else — including a job that is missing from the
`needs` payload entirely — fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

#: The only upstream result that may pass the gate. Everything else — including
#: "skipped", which the naive `contains(needs.*.result, 'failure')` check lets
#: through — is a refusal.
PASS = "success"


def evaluate(needs: dict, required: list[str]) -> tuple[bool, list[tuple[str, str, str]]]:
    """Return (ok, rows). Each row is (job, result, verdict)."""
    rows: list[tuple[str, str, str]] = []
    ok = True

    for job in required:
        if job not in needs:
            # A required job that is absent from `needs` means the workflow no
            # longer depends on it. That is a silent hole in the gate, not a
            # pass: the suite could have been deleted or renamed and nothing
            # would have gone red.
            rows.append((job, "<not a dependency>", "FAIL"))
            ok = False
            continue
        entry = needs[job] or {}
        result = str(entry.get("result", "")) or "<no result>"
        if result == PASS:
            rows.append((job, result, "ok"))
        else:
            rows.append((job, result, "FAIL"))
            ok = False

    # A dependency that exists but was never declared required is also a
    # problem: someone added a suite to `needs:` and forgot to add it here, so
    # it can fail without blocking anything.
    for job in sorted(set(needs) - set(required)):
        result = str((needs[job] or {}).get("result", "")) or "<no result>"
        rows.append((job, result, "FAIL (not in --require)"))
        ok = False

    return ok, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--needs",
        help="the ${{ toJSON(needs) }} payload; defaults to $NEEDS_JSON",
    )
    parser.add_argument(
        "--require",
        required=True,
        help="comma-separated job ids that must all report success",
    )
    parser.add_argument("--title", default="Release gate")
    args = parser.parse_args(argv)

    raw = args.needs if args.needs is not None else os.environ.get("NEEDS_JSON", "")
    if not raw.strip():
        print("FATAL: no needs payload was supplied", file=sys.stderr)
        return 2
    try:
        needs = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"FATAL: needs payload is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(needs, dict):
        print("FATAL: needs payload is not an object", file=sys.stderr)
        return 2

    required = [j.strip() for j in args.require.split(",") if j.strip()]
    ok, rows = evaluate(needs, required)

    width = max([len(r[0]) for r in rows] + [len("job")])
    lines = [f"{'job'.ljust(width)}  {'result'.ljust(20)}  verdict"]
    lines += [f"{j.ljust(width)}  {r.ljust(20)}  {v}" for j, r, v in rows]
    report = "\n".join(lines)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### {args.title}: {'PASSED' if ok else 'FAILED'}\n\n")
            fh.write("| job | result | verdict |\n| --- | --- | --- |\n")
            for j, r, v in rows:
                mark = "✅" if v == "ok" else "❌"
                fh.write(f"| `{j}` | `{r}` | {mark} {v} |\n")

    if not ok:
        print(
            "\nRefusing to report success: a required job did not report "
            "`success`. `skipped` and `cancelled` are failures here — see the "
            "module docstring for why `always()` alone is not a gate.",
            file=sys.stderr,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
