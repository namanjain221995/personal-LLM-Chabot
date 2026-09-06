#!/usr/bin/env python3
"""Turn a gitleaks report into a verdict, without trusting the exit code alone.

Three things go wrong with a naive secret-scan step, and this script exists to
close all three:

  1. THE SCANNER BROKE AND IT LOOKED CLEAN. gitleaks exits 0 for "no leaks",
     1 for "leaks found" and 2+ for "I could not run". A step that only checks
     for non-zero, or that swallows the code with `|| true`, cannot tell a
     clean repository from a scan that never happened. Measured here on
     2026-09-07: with a read-only bind mount and no `safe.directory`, gitleaks
     scans NOTHING and the naive step passes.

  2. THE REPORT AND THE EXIT CODE DISAGREED. Also measured on 2026-09-07:
     `--report-path /dev/stdout` produced an EMPTY report while the process
     logged "leaks found: 13" and exited 1. Parsed on its own, that empty
     report reads as clean. So exit code and report content are cross-checked
     against each other, and a disagreement is a failure.

  3. HISTORICAL NOISE MADE THE GATE USELESS. A repository with pre-existing
     entropy false positives has a permanently red scan, which very quickly
     becomes a permanently ignored scan. The baseline holds FINGERPRINTS ONLY
     (<commit>:<file>:<rule>:<line> — no secret values), and the comparison is
     two-way: a finding outside the baseline fails, and a baseline entry that
     is no longer reported also fails, so the list cannot rot.

Nothing here ever prints a secret. gitleaks runs with --redact and this script
reports rule, file and line only — a CI log on a public repository is not a
place to reproduce a credential, even one believed to be fake.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys


def load_baseline(path: pathlib.Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    return {e["fingerprint"]: e for e in doc.get("findings", [])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", required=True, type=pathlib.Path)
    ap.add_argument("--baseline", required=True, type=pathlib.Path)
    ap.add_argument("--exit-code", type=int, required=True,
                    help="the exit code gitleaks itself returned")
    args = ap.parse_args(argv)

    # ---- 1. the scanner must have actually run -----------------------------
    if args.exit_code not in (0, 1):
        print(
            f"gitleaks exited {args.exit_code}: it did not complete a scan. "
            "That is not the same as finding nothing, and it will not be "
            "reported as clean.",
            file=sys.stderr,
        )
        return 1
    if not args.report.exists():
        print(f"gitleaks wrote no report at {args.report}", file=sys.stderr)
        return 1
    raw = args.report.read_text().strip() or "[]"
    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"gitleaks report is not parseable JSON: {exc}", file=sys.stderr)
        return 1

    # ---- 2. the report must agree with the exit code -----------------------
    if args.exit_code == 1 and not findings:
        print(
            "gitleaks exited 1 (leaks found) but its report is EMPTY. The two "
            "disagree, so neither can be trusted — most likely the report path "
            "was not writable. Refusing to pass.",
            file=sys.stderr,
        )
        return 1
    if args.exit_code == 0 and findings:
        print(
            f"gitleaks exited 0 (clean) but reported {len(findings)} finding(s). "
            "Refusing to pass on a self-contradictory scan.",
            file=sys.stderr,
        )
        return 1

    # ---- 3. compare against the reviewed baseline --------------------------
    baseline = load_baseline(args.baseline)
    seen = {f.get("Fingerprint"): f for f in findings}
    new = [f for fp, f in seen.items() if fp not in baseline]
    stale = [fp for fp in baseline if fp not in seen]

    def row(rule, file, line):
        return f"| `{rule}` | `{file}` | {line} |"

    lines: list[str] = []
    if new:
        lines += [
            "### ❌ NEW secret-scan findings",
            "",
            "Not in the reviewed baseline. Treat as a live credential until "
            "proven otherwise: rotate first, then decide whether it belongs in "
            "`.github/workflows/gitleaks-baseline.json`.",
            "",
            "| rule | file | line |",
            "| --- | --- | --- |",
        ]
        lines += [row(f.get("RuleID"), f.get("File"), f.get("StartLine")) for f in new]
        for f in new:
            print(
                f"  NEW  {f.get('RuleID')}  {f.get('File')}:{f.get('StartLine')}",
                file=sys.stderr,
            )
    if stale:
        lines += [
            "",
            "### ⚠️ Baseline entries no longer reported",
            "",
            "Delete them from `.github/workflows/gitleaks-baseline.json`.",
            "",
        ]
        lines += [f"- `{fp}`" for fp in stale]
        for fp in stale:
            print(f"  STALE  {fp}", file=sys.stderr)
    if not new and not stale:
        lines += [
            f"### ✅ Secret scan: no new findings",
            "",
            f"{len(findings)} finding(s), all matching the "
            f"{len(baseline)}-entry reviewed baseline "
            f"(placeholders in tests, docs and fixtures).",
        ]
        print(
            f"secret scan: {len(findings)} finding(s), all baselined; "
            "0 new, 0 stale"
        )

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    return 1 if (new or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
