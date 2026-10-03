#!/usr/bin/env python3
"""B-04: freeze an eval-set baseline and compare a candidate run against it.

  # freeze the baseline from one or more finished run_evalset.py run directories
  baseline.py freeze runs/evalset-a runs/evalset-b runs/evalset-c --out baseline.json [--markdown baseline.md]
  # compare a candidate run (or several, pooled) against a frozen baseline; exits 1 when it does not pass
  baseline.py compare baseline.json runs/evalset-candidate [--allow-insufficient] [--out report.json]
  # print a frozen baseline as the tables for docs/ai-platform-upgrade/BENCHMARK_RESULTS.md
  baseline.py show baseline.json

Reads only <run dir>/results.json (kind "evalset", schema 1, the contract that
run_evalset.py writes) and the baseline documents this script writes. Standard
library only; nothing here calls a service or the network. Every rule the
numbers follow (percentiles, tolerances, scores, regressions) is written into
each baseline document under "method", so a number never travels without the
way it was made, and each baseline lists the conditions label of every run it
pools.

Exit codes: 0 done (compare: passed), 1 compare did not pass, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter
from datetime import datetime
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence

RUN_KIND, RUN_SCHEMA = "evalset", 1
BASELINE_KIND, BASELINE_SCHEMA = "evalset-baseline", 1
REPORT_KIND, REPORT_SCHEMA = "evalset-comparison", 1

# MASTER_PROMPT §24 workload classes for the eval set (the set has no large-document job case). The same map
# lives in run_evalset.py, which writes it into every record; a record's own `workload` wins, this is the fallback.
WORKLOAD = {
    "EV01": "direct_fast", "EV02": "direct_fast", "EV03": "direct_fast", "RQ01": "direct_fast",
    "RQ03": "direct_fast", "RQ05": "direct_fast", "RQ06": "direct_fast", "RQ07": "direct_fast",
    "EV05": "evidence_fast", "RQ02": "evidence_fast",
    "EV04": "live_search_fast", "RQ04": "live_search_fast",
    "EV07": "long_context",
    "EV06": "think", "EV08": "think",
    "EV09": "max",
}
CLASS_ORDER = ("direct_fast", "evidence_fast", "live_search_fast", "long_context", "think", "max")
CLASS_ABS_S = {"direct_fast": 0.25, "evidence_fast": 0.5, "live_search_fast": 1.0, "long_context": 1.0,
               "think": 2.0, "max": 10.0}
METRICS = ("first_event_s", "first_token_s", "first_answer_s", "total_s")
GATED_METRICS = ("first_answer_s", "total_s")       # SPEC: gate these two, report the other two
TOTAL_ABS_FACTOR = 2.0                              # total_s abs is 2x the class abs
REL_FLOOR = 0.20
CV_FACTOR = 2.0
MIN_SAMPLES = 3
OVERALL_DROP = 0.05
FLOAT_GUARD = 1e-9                                  # so 0.05 written as a float is not "more than 0.05"
THINKING_CHECK = "thinking_off"
DIGITS = 6
# What a baseline keeps of a run's conditions. The base URL and the health detail stay out: a baseline may be
# committed to a public repository, and an endpoint must never travel with it.
SOURCE_KEYS = ("dir", "started", "finished", "seconds", "label", "harness_commit", "workers", "repeats", "only")


class BaselineError(ValueError):
    """Input that cannot be frozen or compared honestly; the message says why."""


def _abs_text() -> str:
    return ", ".join(f"{w} {CLASS_ABS_S[w]:g} s" for w in CLASS_ORDER)


METHOD = {
    "workload": "Each record's own `workload` field; when absent, the eval-set map in baseline.WORKLOAD "
                "(MASTER_PROMPT §24 classes). A class without a defined tolerance is refused.",
    "case_score": "Score of one record (case x repeat) = checks passed / checks run, over all its turns. A record "
                  "with an error, or with no checks at all, scores 0 and does not pass.",
    "case_pass": "A record passes when every check is ok and it has no error. Case pass_rate = passing records / "
                 "records; mean_score = mean record score.",
    "check_rate": "Per check name: ok / total over every (case, repeat, turn), errored records included.",
    "overall": "Overall mean score = mean over cases of each case's mean_score (every case weighs the same).",
    "fast_thinking": f"Fast thinking turns = turns of effort 'fast' records whose `{THINKING_CHECK}` check failed.",
    "latency_samples": "Per workload class and metric (" + ", ".join(METRICS) + "): every turn of every repeat of "
                       "every pooled run whose timing value is not null; nulls are counted in `missing`.",
    "percentile": "Nearest rank: the sample at 1-based rank ceil(q x n) of the ascending samples, q = 0.50 (p50) "
                  "and 0.95 (p95), computed in integers; no interpolation.",
    "cv": "cv = sample standard deviation (n - 1 denominator) / mean; 0 when n < 2 or the mean is 0.",
    "tolerance": f"allowed_pXX = pXX x (1 + rel) + abs, rel = max({REL_FLOOR:.2f}, {CV_FACTOR:g} x cv); abs by "
                 f"class: {_abs_text()}; total_s uses {TOTAL_ABS_FACTOR:g} x the class abs.",
    "insufficient": f"A class/metric with fewer than {MIN_SAMPLES} samples has status insufficient_samples and no "
                    "allowed values. A comparison never counts it as a pass.",
    "compare_case": "A case regresses when the candidate pass_rate < baseline pass_rate - 1 / baseline repeats "
                    "(exact fractions). A baseline case missing from the candidate fails.",
    "compare_overall": f"The candidate regresses overall when its overall mean score, over the cases both have, "
                       f"is more than {OVERALL_DROP:.2f} below the baseline's over the same cases.",
    "compare_latency": "A candidate fails a gated metric (" + ", ".join(GATED_METRICS) + ") of a class when its "
                       "p50 > allowed_p50 or its p95 > allowed_p95. first_event_s and first_token_s are compared "
                       "and reported but do not gate. Fewer than 3 samples on either side: insufficient_samples.",
    "compare_thinking": "The candidate fails when its Fast thinking turns exceed the baseline's.",
    "compare_passed": "passed = no failure, and no gated insufficient_samples unless --allow-insufficient.",
}


# ------------------------------------------------------------------ reading --

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BaselineError(f"{path}: no such file") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"{path}: not readable JSON ({type(exc).__name__}: {exc})") from None


def read_run(run_dir: str) -> dict:
    """One run directory's results.json, refused unless it is kind 'evalset', schema 1."""
    path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(path):
        raise BaselineError(f"{run_dir}: no results.json (expected a run_evalset.py run directory)")
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise BaselineError(f"{path}: not a JSON object")
    kind, schema = doc.get("kind"), doc.get("schema")
    if kind != RUN_KIND or type(schema) is not int or schema != RUN_SCHEMA:
        raise BaselineError(f"{path}: kind={kind!r} schema={schema!r}; only kind {RUN_KIND!r} schema {RUN_SCHEMA} "
                            "(run_evalset.py results) is accepted")
    if not isinstance(doc.get("conditions"), dict):
        raise BaselineError(f"{path}: `conditions` is missing or not an object")
    if not isinstance(doc.get("cases"), list):
        raise BaselineError(f"{path}: `cases` is missing or not a list")
    if not doc["cases"]:
        raise BaselineError(f"{path}: the run has no case records; it would be listed as a source of no numbers")
    for i, rec in enumerate(doc["cases"]):
        _check_record(rec, f"{path}: cases[{i}]")
    return doc


def _check_record(rec, where: str) -> None:
    if not isinstance(rec, dict):
        raise BaselineError(f"{where}: a case record must be an object")
    if not isinstance(rec.get("id"), str) or not rec["id"]:
        raise BaselineError(f"{where}: a case record needs a non-empty string `id`")
    turns = rec.get("turns", [])
    if not isinstance(turns, list) or not all(isinstance(t, dict) for t in turns):
        raise BaselineError(f"{where} ({rec['id']}): `turns` must be a list of objects")
    for j, t in enumerate(turns):
        checks = t.get("checks", [])
        if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("check"), str)
                                                   for c in checks):
            raise BaselineError(f"{where} ({rec['id']}) turn {j}: `checks` must be a list of objects with a "
                                "string `check`")
        result = t.get("result")
        if result is not None and not isinstance(result, dict):
            raise BaselineError(f"{where} ({rec['id']}) turn {j}: `result` must be an object or null")
        timing = (result or {}).get("timing")
        if timing is not None and not isinstance(timing, dict):
            raise BaselineError(f"{where} ({rec['id']}) turn {j}: `result.timing` must be an object or null")
        for m in METRICS:
            v = (timing or {}).get(m)
            if v is not None and (not _is_number(v) or v < 0):
                raise BaselineError(f"{where} ({rec['id']}) turn {j}: timing {m}={v!r} is not a non-negative "
                                    "number or null")


def _source(run_dir: str, conditions: dict, doc: dict) -> dict:
    return {"dir": run_dir, "started": doc.get("started"), "finished": doc.get("finished"),
            "seconds": doc.get("seconds"), "label": conditions.get("label"),
            "harness_commit": conditions.get("harness_commit"), "workers": conditions.get("workers"),
            "repeats": conditions.get("repeats"), "only": conditions.get("only")}


def load_runs(dirs: Sequence[str], allow_mixed: bool = False) -> List[dict]:
    """Pool the case records of one or more run directories.

    Each returned record is a shallow copy carrying `_run`: the conditions of the run it came from (the same
    dict for every record of a run). Runs whose conditions.label differ are refused unless allow_mixed, because
    numbers measured under different conditions must not be pooled silently.
    """
    if isinstance(dirs, (str, os.PathLike)):
        dirs = [dirs]
    dirs = [os.fspath(d) for d in dirs]
    if not dirs:
        raise BaselineError("no run directory given")
    seen: Dict[str, str] = {}
    for d in dirs:
        real = os.path.realpath(d)
        if real in seen:
            raise BaselineError(f"{d}: the same run directory is given twice (also as {seen[real]}); pooling it "
                                "twice would double-count its samples")
        seen[real] = d
    records: List[dict] = []
    sources: List[dict] = []
    for d in dirs:
        doc = read_run(d)
        src = _source(d, doc["conditions"], doc)
        sources.append(src)
        for rec in doc["cases"]:
            out = dict(rec)
            out["_run"] = src
            records.append(out)
    labels = []
    for s in sources:
        if s["label"] not in labels:
            labels.append(s["label"])
    if len(labels) > 1 and not allow_mixed:
        listing = "; ".join(f"{s['dir']}: {s['label']!r}" for s in sources)
        raise BaselineError(f"the runs have different conditions labels ({listing}); numbers from different "
                            "conditions must not be pooled silently. Pass --allow-mixed-conditions (allow_mixed=True) "
                            "to pool them anyway; the baseline then lists every label")
    return records


def sources_of(records: Iterable[dict]) -> List[dict]:
    """The distinct run conditions behind a list of pooled records, in first-seen order."""
    out: List[dict] = []
    for rec in records:
        src = rec.get("_run")
        if isinstance(src, dict) and not any(src is s for s in out):
            out.append(src)
    return out


def load_baseline(path: str) -> dict:
    doc = _read_json(path)
    _check_baseline(doc, path)
    return doc


def _check_baseline(doc, where: str = "baseline") -> None:
    if not isinstance(doc, dict):
        raise BaselineError(f"{where}: not a JSON object")
    kind, schema = doc.get("kind"), doc.get("schema")
    if kind != BASELINE_KIND or type(schema) is not int or schema != BASELINE_SCHEMA:
        raise BaselineError(f"{where}: kind={kind!r} schema={schema!r}; only kind {BASELINE_KIND!r} schema "
                            f"{BASELINE_SCHEMA} (baseline.py freeze output) is accepted")
    quality, latency = doc.get("quality"), doc.get("latency")
    if not isinstance(quality, dict) or not isinstance(quality.get("cases"), dict) \
            or not isinstance(latency, dict):
        raise BaselineError(f"{where}: `quality.cases` or `latency` is missing")
    for cid, c in quality["cases"].items():
        if not (isinstance(c, dict) and type(c.get("passes")) is int and type(c.get("repeats")) is int
                and c["repeats"] > 0 and _is_number(c.get("mean_score"))):
            raise BaselineError(f"{where}: quality.cases.{cid} lacks integer passes/repeats or mean_score")
    if type(quality.get("fast_thinking_turns")) is not int:
        raise BaselineError(f"{where}: quality.fast_thinking_turns is missing")


# --------------------------------------------------------------- statistics --

def nearest_rank(values: Iterable[float], pct: int) -> float:
    """The nearest-rank percentile: the value at 1-based rank ceil(pct/100 x n) of the sorted values."""
    vals = sorted(float(v) for v in values)
    if not vals:
        raise ValueError("no values")
    if type(pct) is not int or not 0 < pct <= 100:
        raise ValueError(f"pct must be an integer in 1..100, not {pct!r}")
    rank = -(-pct * len(vals) // 100)          # ceil(pct * n / 100), exact in integers
    return vals[max(rank, 1) - 1]


def coefficient_of_variation(values: Sequence[float]) -> float:
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return 0.0
    mean = statistics.fmean(vals)
    if mean == 0:
        return 0.0
    return statistics.stdev(vals) / mean


def tolerance(workload: str, metric: str, cv: float) -> dict:
    if workload not in CLASS_ABS_S:
        raise BaselineError(f"no latency tolerance is defined for workload class {workload!r} "
                            f"(known: {', '.join(CLASS_ORDER)})")
    if metric not in METRICS:
        raise BaselineError(f"unknown latency metric {metric!r}")
    rel = max(REL_FLOOR, CV_FACTOR * cv)
    abs_s = CLASS_ABS_S[workload] * (TOTAL_ABS_FACTOR if metric == "total_s" else 1.0)
    return {"rel": round(rel, DIGITS), "abs": round(abs_s, DIGITS)}


def allowed(value: float, tol: dict) -> float:
    return round(value * (1 + tol["rel"]) + tol["abs"], DIGITS)


def workload_of(rec: dict) -> str:
    w = rec.get("workload") or WORKLOAD.get(rec["id"])
    if not w:
        raise BaselineError(f"case {rec['id']}: the record has no `workload` and the id is not in the eval-set map")
    if w not in CLASS_ABS_S:
        raise BaselineError(f"case {rec['id']}: workload class {w!r} has no tolerance "
                            f"(known: {', '.join(CLASS_ORDER)})")
    return w


def _describe(workload: str, metric: str, values: List[float], missing: int) -> dict:
    n = len(values)
    entry = {"n": n, "missing": missing, "p50": None, "p95": None, "min": None, "max": None, "mean": None,
             "cv": None, "gated": metric in GATED_METRICS}
    if n:
        cv = coefficient_of_variation(values)
        entry.update({"p50": nearest_rank(values, 50), "p95": nearest_rank(values, 95), "min": float(min(values)),
                      "max": float(max(values)), "mean": round(statistics.fmean(values), DIGITS),
                      "cv": round(cv, DIGITS)})
    if n >= MIN_SAMPLES:
        tol = tolerance(workload, metric, entry["cv"])
        entry.update({"status": "ok", "tolerance": tol, "allowed_p50": allowed(entry["p50"], tol),
                      "allowed_p95": allowed(entry["p95"], tol)})
    else:
        entry.update({"status": "insufficient_samples", "tolerance": None, "allowed_p50": None,
                      "allowed_p95": None})
    return entry


def latency_stats(records: Sequence[dict]) -> dict:
    samples: Dict[str, Dict[str, List[float]]] = {}
    missing: Dict[str, Counter] = {}
    for rec in records:
        w = workload_of(rec)
        per = samples.setdefault(w, {m: [] for m in METRICS})
        miss = missing.setdefault(w, Counter())
        for t in rec.get("turns") or []:
            timing = ((t.get("result") or {}).get("timing")) or {}
            for m in METRICS:
                v = timing.get(m)
                if v is None:
                    miss[m] += 1
                else:
                    per[m].append(float(v))
    return {w: {m: _describe(w, m, samples[w][m], missing[w][m]) for m in METRICS}
            for w in CLASS_ORDER if w in samples}


def _record_checks(rec: dict) -> List[dict]:
    return [c for t in rec.get("turns") or [] for c in t.get("checks") or []]


def _failed_thinking(turn: dict) -> bool:
    return any(c.get("check") == THINKING_CHECK and c.get("ok") is not True for c in turn.get("checks") or [])


def quality_stats(records: Sequence[dict]) -> dict:
    acc: Dict[str, dict] = {}
    checks: Dict[str, List[int]] = {}
    fast_turns = fast_thinking = 0
    thinking_ids: List[str] = []
    for rec in records:
        cid = rec["id"]
        w, effort = workload_of(rec), rec.get("effort")
        a = acc.get(cid)
        if a is None:
            a = acc[cid] = {"workload": w, "effort": effort, "category": rec.get("category"), "repeats": 0,
                            "passes": 0, "score": Fraction(0), "errors": 0, "no_checks": 0, "failing": Counter()}
        elif (a["workload"], a["effort"]) != (w, effort):
            raise BaselineError(f"case {cid}: records disagree on workload/effort ({a['workload']}/{a['effort']} "
                                f"vs {w}/{effort}); the runs used different eval-set versions")
        rec_checks = _record_checks(rec)
        ok = sum(1 for c in rec_checks if c.get("ok") is True)
        for c in rec_checks:
            tally = checks.setdefault(c["check"], [0, 0])
            tally[0] += c.get("ok") is True
            tally[1] += 1
            if c.get("ok") is not True:
                a["failing"][c["check"]] += 1
        a["repeats"] += 1
        if rec.get("error"):
            a["errors"] += 1
        elif not rec_checks:
            a["no_checks"] += 1
        else:
            a["score"] += Fraction(ok, len(rec_checks))
            a["passes"] += ok == len(rec_checks)
        if effort == "fast":
            for t in rec.get("turns") or []:
                fast_turns += 1
                if _failed_thinking(t):
                    fast_thinking += 1
                    if cid not in thinking_ids:
                        thinking_ids.append(cid)
    cases = {}
    for cid, a in acc.items():
        cases[cid] = {"workload": a["workload"], "effort": a["effort"], "category": a["category"],
                      "repeats": a["repeats"], "passes": a["passes"],
                      "pass_rate": round(a["passes"] / a["repeats"], DIGITS),
                      "mean_score": round(float(a["score"] / a["repeats"]), DIGITS),
                      "errors": a["errors"], "no_checks": a["no_checks"],
                      "failing_checks": dict(sorted(a["failing"].items(), key=lambda kv: (-kv[1], kv[0])))}
    overall = (round(float(sum(a["score"] / a["repeats"] for a in acc.values()) / len(acc)), DIGITS)
               if acc else None)
    return {"cases": cases,
            "checks": {name: {"passed": p, "total": t, "rate": round(p / t, DIGITS)}
                       for name, (p, t) in sorted(checks.items())},
            "overall_mean_score": overall, "fast_turns": fast_turns, "fast_thinking_turns": fast_thinking,
            "fast_thinking_case_ids": thinking_ids}


# ------------------------------------------------------------ freeze/compare --

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _public_sources(conditions) -> List[dict]:
    if conditions is None:
        return []
    if isinstance(conditions, dict):
        conditions = [conditions]
    return [{k: s.get(k) for k in SOURCE_KEYS} for s in conditions]


def freeze(records: Sequence[dict], conditions=None, *, frozen_at: Optional[str] = None) -> dict:
    """The baseline document for pooled case records.

    `conditions` is the list of run conditions the records came from (default: the `_run` of each record, as
    load_runs attaches them); a single conditions dict is accepted too. Only SOURCE_KEYS of each are kept.
    """
    records = list(records)
    if not records:
        raise BaselineError("no case records to freeze")
    for i, rec in enumerate(records):
        _check_record(rec, f"records[{i}]")
    quality = quality_stats(records)
    return {
        "kind": BASELINE_KIND, "schema": BASELINE_SCHEMA,
        "frozen_at": frozen_at or _now(),
        "sources": _public_sources(sources_of(records) if conditions is None else conditions),
        "repeats_per_case": {cid: c["repeats"] for cid, c in quality["cases"].items()},
        "quality": quality,
        "latency": latency_stats(records),
        "method": dict(METHOD),
    }


def _fmt_s(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _fmt_rate(v) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _compare_latency(base_lat: dict, cand_lat: dict, fails: List[str], insufficient: List[dict]) -> dict:
    out: Dict[str, dict] = {}
    classes = [w for w in CLASS_ORDER if w in base_lat or w in cand_lat]
    classes += sorted(w for w in set(base_lat) | set(cand_lat) if w not in CLASS_ORDER)
    for w in classes:
        out[w] = {}
        for m in METRICS:
            b = (base_lat.get(w) or {}).get(m) or {}
            c = (cand_lat.get(w) or {}).get(m) or {}
            b_n, c_n = b.get("n", 0), c.get("n", 0)
            entry = {"gated": m in GATED_METRICS, "baseline_n": b_n, "candidate_n": c_n,
                     "baseline_p50": b.get("p50"), "baseline_p95": b.get("p95"),
                     "candidate_p50": c.get("p50"), "candidate_p95": c.get("p95"),
                     "allowed_p50": b.get("allowed_p50"), "allowed_p95": b.get("allowed_p95"), "over": []}
            if b.get("status") != "ok" or b_n < MIN_SAMPLES or c_n < MIN_SAMPLES \
                    or entry["allowed_p50"] is None or entry["allowed_p95"] is None:
                entry["verdict"] = "insufficient_samples"
                if entry["gated"]:
                    insufficient.append({"workload": w, "metric": m, "baseline_n": b_n, "candidate_n": c_n})
            else:
                for q in ("p50", "p95"):
                    if c[q] > entry[f"allowed_{q}"]:
                        entry["over"].append(q)
                entry["verdict"] = "fail" if entry["over"] else "pass"
                if entry["over"] and entry["gated"]:
                    fails.append(f"latency {w}/{m}: " + ", ".join(
                        f"candidate {q} {_fmt_s(c[q])} s > allowed {_fmt_s(entry[f'allowed_{q}'])} s"
                        for q in entry["over"]))
            out[w][m] = entry
    return out


def compare(baseline: dict, candidate_records: Sequence[dict], *, allow_insufficient: bool = False,
            compared_at: Optional[str] = None) -> dict:
    """Compare pooled candidate records with a frozen baseline; `passed` is False on any failure and, unless
    allow_insufficient, on any gated class/metric with too few samples on either side."""
    _check_baseline(baseline)
    records = list(candidate_records)
    if not records:
        raise BaselineError("no candidate case records to compare")
    for i, rec in enumerate(records):
        _check_record(rec, f"candidate records[{i}]")
    cand = quality_stats(records)
    fails: List[str] = []
    insufficient: List[dict] = []

    cases: Dict[str, dict] = {}
    missing: List[str] = []
    common: List[str] = []
    for cid, b in baseline["quality"]["cases"].items():
        c = cand["cases"].get(cid)
        if c is None:
            missing.append(cid)
            cases[cid] = {"verdict": "fail", "reason": "missing from the candidate", "baseline_pass_rate":
                          b.get("pass_rate"), "baseline_repeats": b["repeats"]}
            fails.append(f"case {cid}: missing from the candidate run(s)")
            continue
        common.append(cid)
        floor = Fraction(b["passes"] - 1, b["repeats"])
        regressed = Fraction(c["passes"], c["repeats"]) < floor
        cases[cid] = {"verdict": "fail" if regressed else "pass",
                      "baseline_pass_rate": b.get("pass_rate"), "baseline_repeats": b["repeats"],
                      "candidate_pass_rate": c["pass_rate"], "candidate_repeats": c["repeats"],
                      "floor": round(float(floor), DIGITS),
                      "baseline_mean_score": b["mean_score"], "candidate_mean_score": c["mean_score"],
                      "candidate_failing_checks": c["failing_checks"], "candidate_errors": c["errors"]}
        if regressed:
            fails.append(f"case {cid}: pass rate {_fmt_rate(c['pass_rate'])} ({c['passes']}/{c['repeats']}) < "
                         f"baseline {_fmt_rate(b.get('pass_rate'))} ({b['passes']}/{b['repeats']}) - 1/"
                         f"{b['repeats']}")
    new_cases = [cid for cid in cand["cases"] if cid not in baseline["quality"]["cases"]]

    overall = {"cases_compared": len(common), "threshold": OVERALL_DROP, "baseline": None, "candidate": None,
               "drop": None, "verdict": "insufficient_samples"}
    if common:
        b_mean = statistics.fmean(baseline["quality"]["cases"][cid]["mean_score"] for cid in common)
        c_mean = statistics.fmean(cand["cases"][cid]["mean_score"] for cid in common)
        drop = b_mean - c_mean
        regressed = drop > OVERALL_DROP + FLOAT_GUARD
        overall.update({"baseline": round(b_mean, DIGITS), "candidate": round(c_mean, DIGITS),
                        "drop": round(drop, DIGITS), "verdict": "fail" if regressed else "pass"})
        if regressed:
            fails.append(f"overall mean score {_fmt_rate(c_mean)} is {_fmt_rate(drop)} below the baseline "
                         f"{_fmt_rate(b_mean)} over {len(common)} cases (allowed drop {OVERALL_DROP:.2f})")
    else:
        fails.append("overall: the candidate has none of the baseline's cases")
        overall["verdict"] = "fail"

    b_think, c_think = baseline["quality"]["fast_thinking_turns"], cand["fast_thinking_turns"]
    thinking = {"baseline": b_think, "candidate": c_think, "candidate_case_ids": cand["fast_thinking_case_ids"],
                "verdict": "fail" if c_think > b_think else "pass"}
    if c_think > b_think:
        fails.append(f"fast thinking leak: {c_think} Fast turns with reasoning > baseline {b_think} "
                     f"(cases {', '.join(cand['fast_thinking_case_ids'])})")

    latency = _compare_latency(baseline["latency"], latency_stats(records), fails, insufficient)
    passed = not fails and (allow_insufficient or not insufficient)
    return {
        "kind": REPORT_KIND, "schema": REPORT_SCHEMA, "compared_at": compared_at or _now(),
        "passed": passed, "allow_insufficient": allow_insufficient,
        "fails": fails, "insufficient": insufficient,
        "baseline": {"frozen_at": baseline.get("frozen_at"), "sources": baseline.get("sources")},
        "candidate": {"sources": _public_sources(sources_of(records)), "records": len(records)},
        "missing_cases": missing, "new_cases": new_cases,
        "cases": cases, "overall": overall, "fast_thinking": thinking, "latency": latency,
        "method": {k: METHOD[k] for k in METHOD if k.startswith("compare_")},
    }


# ----------------------------------------------------------------- markdown --

def _cell(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _row(cells: Iterable) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _table(header: Sequence[str], rows: Iterable[Sequence]) -> List[str]:
    return [_row(header), _row(["---"] * len(header))] + [_row(r) for r in rows]


def _failing_text(case: dict, top: int = 3) -> str:
    parts = []
    if case.get("errors"):
        parts.append(f"error x{case['errors']}")
    if case.get("no_checks"):
        parts.append(f"no checks x{case['no_checks']}")
    parts += [f"{name} x{n}" for name, n in list((case.get("failing_checks") or {}).items())[:top]]
    return ", ".join(parts) or "none"


def render_markdown(baseline: dict) -> str:
    _check_baseline(baseline)
    q, sources = baseline["quality"], baseline.get("sources") or []
    out = ["## Eval-set baseline", "",
           f"Frozen {baseline.get('frozen_at')} by `scripts/aiq/baseline.py` from {len(sources)} run(s). Every "
           "number below holds only under the conditions listed here.", "", "### Conditions", ""]
    for s in sources:
        only = s.get("only")
        out.append(f"- run `{os.path.basename(os.path.normpath(str(s.get('dir') or '')))}`: label "
                   f"\"{_cell(s.get('label'))}\"; started {s.get('started')}; finished {s.get('finished')}; "
                   f"harness commit `{s.get('harness_commit')}`; repeats {s.get('repeats')}; workers "
                   f"{s.get('workers')}; cases {', '.join(only) if only else 'all'}")
    if not sources:
        out.append("- no source run recorded")
    n_records = sum(c["repeats"] for c in q["cases"].values())
    out += ["", "### Quality", "",
            f"Overall mean case score {_fmt_rate(q.get('overall_mean_score'))} over {len(q['cases'])} cases "
            f"({n_records} records). Fast turns with reasoning (`{THINKING_CHECK}` failed): "
            f"{q.get('fast_thinking_turns')} of {q.get('fast_turns')}.", ""]
    out += _table(["Case", "Workload", "Effort", "Repeats", "Pass rate", "Mean score", "Failing checks (most often)"],
                  [[cid, c.get("workload"), c.get("effort"), c["repeats"], _fmt_rate(c.get("pass_rate")),
                    _fmt_rate(c.get("mean_score")), _failing_text(c)] for cid, c in q["cases"].items()])
    out += ["", "### Latency by workload class", "",
            "Seconds. " + METHOD["percentile"] + " " + METHOD["tolerance"] + " Gated: "
            + ", ".join(GATED_METRICS) + "; first_event_s and first_token_s are reported only. "
            + METHOD["insufficient"], ""]
    rows = []
    for w, per in baseline["latency"].items():
        for m in METRICS:
            e = per.get(m)
            if not e:
                continue
            if e.get("status") == "ok":
                tol = e["tolerance"]
                allowed_cells = [_fmt_s(e["allowed_p50"]), _fmt_s(e["allowed_p95"]),
                                 f"rel {tol['rel']:.3f}, abs {tol['abs']:.2f} s"]
            else:
                allowed_cells = [f"insufficient samples (n < {MIN_SAMPLES})"] * 2 + ["n/a"]
            rows.append([w, m, e["n"], e.get("missing", 0), _fmt_s(e.get("p50")), _fmt_s(e.get("p95")),
                         _fmt_s(e.get("min")), _fmt_s(e.get("max"))] + allowed_cells)
    out += _table(["Class", "Metric", "n", "Missing", "p50", "p95", "min", "max", "Allowed p50", "Allowed p95",
                   "Tolerance"], rows)
    out += ["", "### Check pass rate", "", METHOD["check_rate"], ""]
    out += _table(["Check", "Passed", "Total", "Rate"],
                  [[name, c["passed"], c["total"], _fmt_rate(c["rate"])] for name, c in q["checks"].items()])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------- CLI --

def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_json(path: str, doc: dict) -> None:
    _write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _summary(report: dict) -> str:
    lines = [f"compare: {'PASSED' if report['passed'] else 'NOT PASSED'} "
             f"({len(report['fails'])} failure(s), {len(report['insufficient'])} gated class/metric(s) with "
             f"insufficient samples{', allowed' if report['allow_insufficient'] else ''})"]
    lines += [f"  FAIL {f}" for f in report["fails"]]
    lines += [f"  INSUFFICIENT latency {i['workload']}/{i['metric']}: baseline n={i['baseline_n']}, "
              f"candidate n={i['candidate_n']} (need {MIN_SAMPLES})" for i in report["insufficient"]]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="baseline.py", description="Freeze an eval-set baseline and compare a "
                                 "candidate run against it (B-04).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="freeze a baseline from run_evalset.py run directories")
    f.add_argument("run_dirs", nargs="+")
    f.add_argument("--out", required=True, help="baseline JSON to write")
    f.add_argument("--markdown", help="also write the BENCHMARK_RESULTS.md tables here")
    f.add_argument("--allow-mixed-conditions", action="store_true",
                   help="pool runs whose conditions.label differ (every label is listed in the baseline)")
    c = sub.add_parser("compare", help="compare candidate run directories against a frozen baseline")
    c.add_argument("baseline")
    c.add_argument("run_dirs", nargs="+")
    c.add_argument("--allow-insufficient", action="store_true",
                   help="do not fail on class/metrics with fewer than 3 samples (they are still listed)")
    c.add_argument("--allow-mixed-conditions", action="store_true",
                   help="pool candidate runs whose conditions.label differ")
    c.add_argument("--out", help="comparison report JSON to write")
    s = sub.add_parser("show", help="print a frozen baseline as markdown tables")
    s.add_argument("baseline")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "freeze":
            records = load_runs(args.run_dirs, allow_mixed=args.allow_mixed_conditions)
            doc = freeze(records)
            _write_json(args.out, doc)
            if args.markdown:
                _write_text(args.markdown, render_markdown(doc))
            print(f"froze {len(records)} records of {len(doc['quality']['cases'])} cases from "
                  f"{len(doc['sources'])} run(s) -> {args.out}")
            return 0
        if args.cmd == "compare":
            base = load_baseline(args.baseline)
            records = load_runs(args.run_dirs, allow_mixed=args.allow_mixed_conditions)
            report = compare(base, records, allow_insufficient=args.allow_insufficient)
            if args.out:
                _write_json(args.out, report)
            print(_summary(report))
            return 0 if report["passed"] else 1
        sys.stdout.write(render_markdown(load_baseline(args.baseline)))
        return 0
    except BaselineError as exc:
        print(f"baseline.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
