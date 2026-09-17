"""The gate for the dev-to-main pull request.

The thresholds are the round's QA acceptance, written once here so the PR
description and the console print the same numbers:

  * overall at least 0.88, and no category below the WORKER baseline,
  * csv_plot without P06 at least 0.80, followup_edit at least 0.80,
    big_report at least 0.80, format_rewrite at least 0.90, howto and tables
    still 1.00,
  * charts at least 0.85, structure at least 0.90, length at least 0.80, and
    thinking off on 100% of Fast turns,
  * and four counts that must be exactly zero:
      - "The artifact has no charts to draw as images",
      - a chart warning "the table … is not available" in a conversation that
        uploaded a dataset,
      - a Fast turn carrying reasoning,
      - an edit that says it added a chart when the new version has none more.

P06 is scored separately (it asks for a chart without asking for a file, so
the route it takes is a judgement call, not a defect), and the coding cases
are reported but do not block: they arrived with this harness, after the
round's acceptance was agreed.
"""
from __future__ import annotations

from typing import Dict, List, Optional

#: (metric, floor, blocking). "category:x" and "dimension:x" read the summary.
GATE: List[tuple] = [
    ("overall", 0.88, True),
    ("category:csv_plot_without_p06", 0.80, True),
    ("category:followup_edit", 0.80, True),
    ("category:big_report", 0.80, True),
    ("category:format_rewrite", 0.90, True),
    ("category:howto", 1.00, True),
    ("category:tables", 1.00, True),
    ("dimension:charts", 0.85, True),
    ("dimension:structure", 0.90, True),
    ("dimension:length", 0.80, True),
    ("dimension:thinking", 1.00, True),
    ("category:coding", 0.80, False),
    ("dimension:code", 0.80, False),
]

#: (count in the headline, what it means, the headline key that lists WHERE)
ZERO_COUNTS = [
    ("no_charts_to_draw", "'The artifact has no charts to draw as images'", "no_charts_to_draw_where"),
    ("table_not_available", "a chart warning 'the table … is not available' in a dataset conversation",
     "table_not_available_where"),
    ("fast_turns_thinking", "a Fast turn that thought", "fast_thinking_case_ids"),
    ("false_chart_claims", "an edit that said it added a chart when none was added", "false_chart_claims_where"),
]


def _value(summary: dict, metric: str) -> Optional[float]:
    if metric == "overall":
        return summary["headline"]["overall_score"]
    kind, _, name = metric.partition(":")
    if kind == "category":
        row = (summary.get("by_category") or {}).get(name)
        return None if row is None else row["mean_score"]
    if kind == "dimension":
        row = (summary.get("by_dimension") or {}).get(name)
        return None if row is None else row["rate"]
    raise KeyError(metric)


def gate_report(summary: dict, baseline: Optional[dict] = None) -> dict:
    """Every gate row with its value, its floor and its baseline."""
    rows = []
    for metric, floor, blocking in GATE:
        got = _value(summary, metric)
        base = _value(baseline, metric) if baseline else None
        # "no category below its worker baseline": the baseline is a floor too,
        # with a 0.03 tolerance — two re-baseline runs of the same image differ
        # by that much, so a smaller drop is noise, not a regression.
        base_floor = None if base is None else round(base - 0.03, 3)
        ok = got is not None and got >= floor and (base_floor is None or got >= base_floor)
        rows.append({"metric": metric, "value": got, "floor": floor, "baseline": base,
                     "baseline_floor": base_floor, "ok": bool(ok), "blocking": blocking})
    counts = []
    for key, what, where_key in ZERO_COUNTS:
        n = summary["headline"].get(key)
        n = len(n) if isinstance(n, list) else int(n or 0)
        where = summary["headline"].get(where_key) or []
        counts.append({"count": key, "what": what, "value": n, "ok": n == 0, "blocking": True,
                       "where": where if isinstance(where, list) else [where]})
    failing = failing_cases(summary, baseline)
    return {"rows": rows, "counts": counts, "failing_cases": failing,
            "p06": next(({"score": c["score"], "failed": c["failed"]} for c in summary["cases"] if c["id"] == "P06"), None),
            "passed": all(r["ok"] for r in rows if r["blocking"]) and all(c["ok"] for c in counts)}


def failing_cases(summary: dict, baseline: Optional[dict] = None) -> List[dict]:
    """Every case that did not pass every check, with its score against the baseline."""
    base_by_id: Dict[str, dict] = {c["id"]: c for c in (baseline or {}).get("cases", [])}
    out = []
    for c in summary["cases"]:
        if c["all_pass"]:
            continue
        b = base_by_id.get(c["id"])
        out.append({"id": c["id"], "category": c["category"], "score": c["score"], "failed": c["failed"],
                    "baseline_score": None if b is None else b["score"],
                    "baseline_failed": None if b is None else b["failed"],
                    "job_ids": (summary.get("job_ids") or {}).get(c["id"], []),
                    "regression": bool(b and c["score"] < b["score"])})
    return out


def print_gate(report: dict) -> None:
    print("\nGATE (dev -> main)" + ("  PASSED" if report["passed"] else "  FAILED"))
    print(f"  {'metric':34s} {'value':>7s} {'floor':>7s} {'baseline':>9s}  ")
    for r in report["rows"]:
        mark = "ok  " if r["ok"] else ("FAIL" if r["blocking"] else "warn")
        value = "  n/a" if r["value"] is None else f"{r['value']:.3f}"
        base = "      -" if r["baseline"] is None else f"{r['baseline']:.3f}"
        print(f"  {mark} {r['metric']:32s} {value:>7s} {r['floor']:7.2f} {base:>9s}"
              + ("" if r["blocking"] else "   (reported, not blocking)"))
    for c in report["counts"]:
        print(f"  {'ok  ' if c['ok'] else 'FAIL'} must be zero: {c['what']:<70s} {c['value']}")
        if not c["ok"] and c["where"]:
            print(f"       {c['where']}")
    if report["p06"]:
        print(f"  P06 (scored separately): {report['p06']['score']:.2f} {', '.join(report['p06']['failed'])}")
    if report["failing_cases"]:
        print("\n  failing cases (score, baseline, checks):")
        for c in report["failing_cases"]:
            base = "   -" if c["baseline_score"] is None else f"{c['baseline_score']:.2f}"
            jobs = (" jobs " + ",".join(j[:8] for j in c["job_ids"])) if c["job_ids"] else ""
            print(f"    {c['id']:5s} {c['category']:14s} {c['score']:.2f} (baseline {base})"
                  f"{'  REGRESSION' if c['regression'] else ''}{jobs}: {', '.join(c['failed'])}")
