"""B-04: baseline.py freezes eval-set runs into a baseline and compares a candidate against it.

  * nearest-rank percentiles, cv and the tolerance rule of the B-04 spec
    (rel = max(0.20, 2 x cv), abs per class, total_s abs doubled), with
    fewer than three samples reported as insufficient, never as a pass;
  * freeze pools run directories, refuses foreign or mixed-condition input,
    and scores an errored record 0;
  * compare: case, overall, latency, missing-case and Fast-thinking verdicts
    on each side of their thresholds;
  * the markdown tables and the CLI exit codes.

Everything is synthetic results.json written to tmp dirs. Nothing here calls
a model, a service or the network.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

AIQ = Path(__file__).resolve().parents[1]
if str(AIQ) not in sys.path:
    sys.path.append(str(AIQ))

import baseline as B  # noqa: E402

FROZEN_AT = "2026-10-04T05:00:00+05:30"
FAST = {"first_event_s": 0.1, "first_token_s": 0.3, "first_answer_s": 0.3, "total_s": 1.0}


# ----------------------------------------------------------------- helpers --

def checks(n_ok: int = 1, fail=()) -> list:
    """n_ok passing checks named c00.. plus one failing check per name in `fail`."""
    out = [{"check": f"c{i:02d}", "dimension": "format", "ok": True, "detail": ""} for i in range(n_ok)]
    out += [{"check": name, "dimension": "format", "ok": False, "detail": "synthetic"} for name in fail]
    return out


def turn(chk=None, timing=None) -> dict:
    return {"message": "synthetic", "expect": {}, "checks": checks() if chk is None else chk,
            "result": {"http": 200, "answer": "synthetic answer", "errors": [], "meta": {},
                       "timing": dict(FAST if timing is None else timing)}}


def record(cid: str, repeat: int = 1, turns=None, *, effort: str = "fast", workload="map", error=None) -> dict:
    rec = {"id": cid, "category": "synthetic", "section": "11", "effort": effort, "repeat": repeat,
           "conversation_id": f"aiq-{cid}-r{repeat}", "error": error, "attachments": [],
           "turns": [turn()] if turns is None else turns}
    if workload == "map":
        rec["workload"] = B.WORKLOAD[cid]
    elif workload is not None:
        rec["workload"] = workload
    return rec


def write_run(root: Path, name: str, records: list, *, label: str = "synthetic stack, cap 2", kind="evalset",
              schema=1, repeats: int = 1) -> str:
    d = root / name
    d.mkdir(parents=True)
    doc = {"kind": kind, "schema": schema, "started": "2026-10-04T05:10:00+0530",
           "finished": "2026-10-04T05:20:00+0530", "seconds": 600,
           "conditions": {"base": "http://127.0.0.1:28080", "harness_commit": "0123abc", "label": label,
                          "workers": 1, "repeats": repeats, "only": [], "health": None},
           "cases": records}
    (d / "results.json").write_text(json.dumps(doc), encoding="utf-8")
    return str(d)


def three_repeats(cid: str = "EV01", **kw) -> list:
    return [record(cid, r, **kw) for r in (1, 2, 3)]


def frozen(records: list) -> dict:
    return B.freeze(records, [], frozen_at=FROZEN_AT)


def latency(doc: dict, workload: str = "direct_fast", metric: str = "total_s") -> dict:
    return doc["latency"][workload][metric]


# ------------------------------------------------------------- statistics --

@pytest.mark.parametrize("values,p50,p95", [
    ([5.0], 5.0, 5.0),                                   # n=1: rank 1 for both
    ([2.0, 1.0], 1.0, 2.0),                              # n=2: ceil(1.0)=1, ceil(1.9)=2
    ([3.0, 1.0, 2.0], 2.0, 3.0),                         # n=3: ceil(1.5)=2, ceil(2.85)=3
    ([float(v) for v in range(10, 0, -1)], 5.0, 10.0),   # n=10: rank 5, ceil(9.5)=10
    ([float(v) for v in range(1, 21)], 10.0, 19.0),      # n=20: rank 10, rank 19 (exact, no float drift)
])
def test_nearest_rank_percentiles(values, p50, p95):
    assert B.nearest_rank(values, 50) == p50
    assert B.nearest_rank(values, 95) == p95
    assert B.nearest_rank(values, 100) == max(values)


def test_nearest_rank_refuses_empty_and_bad_pct():
    with pytest.raises(ValueError):
        B.nearest_rank([], 50)
    with pytest.raises(ValueError):
        B.nearest_rank([1.0], 0.5)


def test_cv_uses_the_sample_stdev_and_is_zero_below_two_samples():
    assert B.coefficient_of_variation([4.0]) == 0.0
    assert B.coefficient_of_variation([]) == 0.0
    assert B.coefficient_of_variation([0.0, 0.0, 0.0]) == 0.0
    assert B.coefficient_of_variation([2.0, 2.0, 2.0]) == 0.0
    assert B.coefficient_of_variation([1.0, 2.0, 3.0]) == pytest.approx(0.5)   # stdev 1 (n-1), mean 2


def test_tolerance_rel_floor_cv_and_class_abs():
    assert B.tolerance("direct_fast", "first_answer_s", 0.0) == {"rel": 0.2, "abs": 0.25}
    assert B.tolerance("direct_fast", "first_answer_s", 0.09) == {"rel": 0.2, "abs": 0.25}   # 2 x cv = 0.18
    assert B.tolerance("direct_fast", "first_answer_s", 0.5) == {"rel": 1.0, "abs": 0.25}    # 2 x cv wins
    expected = {"direct_fast": 0.25, "evidence_fast": 0.5, "live_search_fast": 1.0, "long_context": 1.0,
                "think": 2.0, "max": 10.0}
    for w, abs_s in expected.items():
        assert B.tolerance(w, "first_answer_s", 0)["abs"] == abs_s
        assert B.tolerance(w, "first_event_s", 0)["abs"] == abs_s
        assert B.tolerance(w, "total_s", 0)["abs"] == 2 * abs_s
    with pytest.raises(B.BaselineError):
        B.tolerance("large_document", "total_s", 0)


def test_allowed_values_follow_the_rule():
    # equal samples: cv 0 -> rel 0.20; first_answer abs 0.25, total abs 0.50
    doc = frozen(three_repeats())
    fa, tot = latency(doc, metric="first_answer_s"), latency(doc)
    assert (fa["n"], fa["p50"], fa["p95"], fa["cv"], fa["status"]) == (3, 0.3, 0.3, 0.0, "ok")
    assert fa["allowed_p50"] == pytest.approx(0.3 * 1.2 + 0.25)
    assert tot["allowed_p95"] == pytest.approx(1.0 * 1.2 + 0.5)
    # spread samples: total_s 1, 2, 3 -> cv 0.5 -> rel 1.0; think abs 2.0, total_s abs 4.0
    recs = [record("EV08", r, [turn(timing=dict(FAST, total_s=float(r)))], effort="think") for r in (1, 2, 3)]
    t = latency(frozen(recs), "think")
    assert (t["p50"], t["p95"], t["min"], t["max"], t["mean"]) == (2.0, 3.0, 1.0, 3.0, 2.0)
    assert t["cv"] == pytest.approx(0.5) and t["tolerance"] == {"rel": 1.0, "abs": 4.0}
    assert t["allowed_p50"] == pytest.approx(2.0 * 2 + 4.0)
    assert t["allowed_p95"] == pytest.approx(3.0 * 2 + 4.0)
    # small spread: the 0.20 floor holds
    recs = [record("EV01", r, [turn(timing=dict(FAST, total_s=v))]) for r, v in ((1, 10.0), (2, 10.5), (3, 11.0))]
    s = latency(frozen(recs))
    assert s["cv"] == pytest.approx(0.5 / 10.5, abs=1e-6) and s["tolerance"]["rel"] == 0.2
    assert s["allowed_p95"] == pytest.approx(11.0 * 1.2 + 0.5)


def test_fewer_than_three_samples_is_insufficient_and_has_no_allowed_values():
    recs = three_repeats()
    recs[2]["turns"][0]["result"]["timing"]["total_s"] = None      # 2 total_s samples, 3 of the rest
    doc = frozen(recs)
    tot = latency(doc)
    assert (tot["n"], tot["missing"], tot["status"]) == (2, 1, "insufficient_samples")
    assert tot["allowed_p50"] is None and tot["allowed_p95"] is None and tot["tolerance"] is None
    assert tot["p50"] == 1.0                                         # still described, never gated
    assert latency(doc, metric="first_answer_s")["status"] == "ok"
    one = latency(frozen([record("EV09", effort="max")]), "max")
    assert (one["n"], one["status"], one["allowed_p95"]) == (1, "insufficient_samples", None)


# ----------------------------------------------------------------- freeze --

def test_freeze_pools_three_run_directories(tmp_path):
    dirs = [write_run(tmp_path, f"run{i}", [record("EV01", 1), record("RQ01", 1),
                                            record("EV08", 1, effort="think")]) for i in (1, 2, 3)]
    records = B.load_runs(dirs)
    assert len(records) == 9 and records[0]["_run"]["dir"] == dirs[0]
    doc = B.freeze(records, frozen_at=FROZEN_AT)
    assert doc["kind"] == "evalset-baseline" and doc["schema"] == 1 and doc["frozen_at"] == FROZEN_AT
    assert doc["repeats_per_case"] == {"EV01": 3, "RQ01": 3, "EV08": 3}
    assert [s["dir"] for s in doc["sources"]] == dirs
    assert all(s["label"] == "synthetic stack, cap 2" and s["harness_commit"] == "0123abc" for s in doc["sources"])
    assert all("base" not in s and "health" not in s for s in doc["sources"])     # no endpoint in a baseline
    assert latency(doc)["n"] == 6 and latency(doc, "think")["n"] == 3
    assert doc["quality"]["cases"]["EV01"]["pass_rate"] == 1.0
    assert doc["quality"]["overall_mean_score"] == 1.0
    assert set(doc["method"]) >= {"percentile", "tolerance", "cv", "insufficient", "case_score"}
    assert "ceil(q x n)" in doc["method"]["percentile"]
    # deterministic: the same input freezes to the same document
    assert json.dumps(B.freeze(B.load_runs(dirs), frozen_at=FROZEN_AT)) == json.dumps(doc)


def test_mixed_condition_labels_are_refused_without_the_flag(tmp_path):
    a = write_run(tmp_path, "a", [record("EV01")], label="dev stack, cap 2")
    b = write_run(tmp_path, "b", [record("EV01")], label="dev stack, cap 4")
    with pytest.raises(B.BaselineError, match="different conditions labels"):
        B.load_runs([a, b])
    records = B.load_runs([a, b], allow_mixed=True)
    assert [s["label"] for s in B.freeze(records)["sources"]] == ["dev stack, cap 2", "dev stack, cap 4"]


def test_the_same_run_directory_twice_is_refused(tmp_path):
    a = write_run(tmp_path, "a", [record("EV01")])
    with pytest.raises(B.BaselineError, match="twice"):
        B.load_runs([a, a])


@pytest.mark.parametrize("kind,schema", [("aiq", 1), ("evalset", 2), ("evalset", "1"), ("evalset", True),
                                         ("evalset-baseline", 1)])
def test_foreign_kind_or_schema_is_refused(tmp_path, kind, schema):
    d = write_run(tmp_path, "x", [record("EV01")], kind=kind, schema=schema)
    with pytest.raises(B.BaselineError, match="only kind 'evalset' schema 1"):
        B.load_runs([d])


def test_a_directory_without_results_or_with_broken_json_is_refused(tmp_path):
    with pytest.raises(B.BaselineError, match="no results.json"):
        B.load_runs([str(tmp_path)])
    (tmp_path / "results.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(B.BaselineError, match="not readable JSON"):
        B.load_runs([str(tmp_path)])
    with pytest.raises(B.BaselineError, match="no case records"):
        B.load_runs([write_run(tmp_path, "empty", [])])


def test_bad_timing_values_are_refused(tmp_path):
    rec = record("EV01", turns=[turn(timing=dict(FAST, total_s="slow"))])
    with pytest.raises(B.BaselineError, match="total_s"):
        B.load_runs([write_run(tmp_path, "x", [rec])])


def test_error_records_score_zero_and_do_not_pass():
    recs = three_repeats()
    recs[1]["error"] = "ReadTimeout: stream stalled"                  # its checks all passed, still 0
    case = frozen(recs)["quality"]["cases"]["EV01"]
    assert (case["repeats"], case["passes"], case["errors"]) == (3, 2, 1)
    assert case["pass_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert case["mean_score"] == pytest.approx(2 / 3, abs=1e-6)
    empty = frozen([record("EV01", turns=[], error="ConnectError: refused")])["quality"]
    assert (empty["cases"]["EV01"]["mean_score"], empty["overall_mean_score"]) == (0.0, 0.0)
    no_checks = frozen([record("EV01", turns=[turn(chk=[])])])["quality"]["cases"]["EV01"]
    assert (no_checks["passes"], no_checks["mean_score"], no_checks["no_checks"]) == (0, 0.0, 1)


def test_case_scores_check_rates_and_fast_thinking():
    recs = [record("EV01", 1, [turn(checks(3, fail=["max_chars"])), turn(checks(4))]),     # 7/8, not a pass
            record("EV01", 2, [turn(checks(3, fail=["thinking_off"])), turn(checks(4))]),  # Fast reasoning
            record("EV08", 1, [turn(checks(1, fail=["thinking_off"]))], effort="think")]   # not a Fast turn
    q = frozen(recs)["quality"]
    assert q["cases"]["EV01"]["pass_rate"] == 0.0
    assert q["cases"]["EV01"]["mean_score"] == pytest.approx(7 / 8)
    assert q["cases"]["EV01"]["failing_checks"] == {"max_chars": 1, "thinking_off": 1}
    assert q["checks"]["c00"] == {"passed": 5, "total": 5, "rate": 1.0}                  # 4 Fast turns + EV08
    assert q["checks"]["thinking_off"] == {"passed": 0, "total": 2, "rate": 0.0}
    assert (q["fast_turns"], q["fast_thinking_turns"], q["fast_thinking_case_ids"]) == (4, 1, ["EV01"])
    assert q["overall_mean_score"] == pytest.approx((7 / 8 + 0.5) / 2)              # each case weighs the same


def test_workload_comes_from_the_record_then_the_map():
    recs = [record("EV05", workload=None), record("EV01", workload="long_context")]
    lat = frozen(recs)["latency"]
    assert set(lat) == {"evidence_fast", "long_context"}
    with pytest.raises(B.BaselineError, match="not in the eval-set map"):
        frozen([record("ZZ99", workload=None)])
    with pytest.raises(B.BaselineError, match="no tolerance"):
        frozen([record("EV01", workload="large_document")])


# ---------------------------------------------------------------- compare --

def base_and_records(cases=("EV01", "RQ01"), n_ok: int = 1) -> tuple:
    recs = [record(cid, r, [turn(checks(n_ok))]) for cid in cases for r in (1, 2, 3)]
    return frozen(recs), recs


def failing_repeat(cid: str, repeat: int, n_ok: int, fail) -> dict:
    return record(cid, repeat, [turn(checks(n_ok, fail=fail))])


def test_identical_candidate_passes():
    base, recs = base_and_records()
    report = B.compare(base, recs)
    assert report["passed"] is True and report["fails"] == [] and report["insufficient"] == []
    assert report["kind"] == "evalset-comparison"
    assert {v["verdict"] for v in report["cases"].values()} == {"pass"}
    assert report["latency"]["direct_fast"]["total_s"]["verdict"] == "pass"
    assert report["overall"]["verdict"] == "pass" and report["fast_thinking"]["verdict"] == "pass"


def test_case_regresses_only_when_more_than_one_repeat_worse():
    base, _ = base_and_records(cases=("EV01",), n_ok=50)
    two_of_three = [record("EV01", 1, [turn(checks(50))]), record("EV01", 2, [turn(checks(50))]),
                    failing_repeat("EV01", 3, 49, ["format"])]
    r = B.compare(base, two_of_three)
    assert r["cases"]["EV01"]["verdict"] == "pass" and r["passed"] is True       # 3/3 -> 2/3 is within one repeat
    one_of_three = [record("EV01", 1, [turn(checks(50))]), failing_repeat("EV01", 2, 49, ["format"]),
                    failing_repeat("EV01", 3, 49, ["format"])]
    r = B.compare(base, one_of_three)
    assert r["cases"]["EV01"]["verdict"] == "fail" and r["passed"] is False
    assert any(f.startswith("case EV01: pass rate 0.333 (1/3)") for f in r["fails"])
    assert r["overall"]["verdict"] == "pass"                                     # 49/50 twice: a small drop


def test_overall_drop_of_006_fails_and_004_passes():
    base, _ = base_and_records(cases=("EV01",), n_ok=50)
    full = [record("EV01", r, [turn(checks(50))]) for r in (1, 2)]
    drop6 = B.compare(base, full + [failing_repeat("EV01", 3, 41, ["f"] * 9)])   # (1 + 1 + 0.82) / 3 = 0.94
    assert drop6["overall"]["drop"] == pytest.approx(0.06) and drop6["overall"]["verdict"] == "fail"
    assert drop6["cases"]["EV01"]["verdict"] == "pass"                           # only the overall rule fires
    assert drop6["passed"] is False and len(drop6["fails"]) == 1 and drop6["fails"][0].startswith("overall")
    drop4 = B.compare(base, full + [failing_repeat("EV01", 3, 44, ["f"] * 6)])   # (1 + 1 + 0.88) / 3 = 0.96
    assert drop4["overall"]["drop"] == pytest.approx(0.04) and drop4["overall"]["verdict"] == "pass"
    assert drop4["passed"] is True


def test_latency_p95_just_above_allowed_fails_and_just_below_passes():
    base, _ = base_and_records(cases=("EV01",))
    allowed = latency(base)["allowed_p95"]
    assert allowed == pytest.approx(1.7)                                          # 1.0 x 1.2 + 0.5

    def cand(last_total: float) -> list:
        return [record("EV01", 1), record("EV01", 2), record("EV01", 3, [turn(timing=dict(FAST, total_s=last_total))])]

    above = B.compare(base, cand(allowed + 0.01))
    entry = above["latency"]["direct_fast"]["total_s"]
    assert entry["verdict"] == "fail" and entry["over"] == ["p95"] and entry["candidate_p50"] == 1.0
    assert above["passed"] is False and any(f.startswith("latency direct_fast/total_s") for f in above["fails"])
    below = B.compare(base, cand(allowed - 0.01))
    assert below["latency"]["direct_fast"]["total_s"]["verdict"] == "pass" and below["passed"] is True


def test_p50_over_allowed_fails():
    base, _ = base_and_records(cases=("EV01",))
    slow = [record("EV01", r, [turn(timing=dict(FAST, first_answer_s=0.62))]) for r in (1, 2, 3)]   # > 0.61
    r = B.compare(base, slow)
    assert r["latency"]["direct_fast"]["first_answer_s"]["over"] == ["p50", "p95"] and r["passed"] is False


def test_reported_metrics_do_not_gate():
    base, _ = base_and_records(cases=("EV01",))
    late_event = [record("EV01", r, [turn(timing=dict(FAST, first_event_s=5.0))]) for r in (1, 2, 3)]
    r = B.compare(base, late_event)
    assert r["latency"]["direct_fast"]["first_event_s"]["verdict"] == "fail"
    assert r["latency"]["direct_fast"]["first_event_s"]["gated"] is False
    assert r["passed"] is True and r["fails"] == []


def test_a_missing_case_fails():
    base, recs = base_and_records()
    r = B.compare(base, [x for x in recs if x["id"] == "EV01"])
    assert r["missing_cases"] == ["RQ01"] and r["cases"]["RQ01"]["verdict"] == "fail"
    assert r["passed"] is False and "case RQ01: missing from the candidate run(s)" in r["fails"]
    assert r["overall"]["cases_compared"] == 1


def test_new_candidate_cases_are_listed_not_failed():
    base, recs = base_and_records(cases=("EV01",))
    r = B.compare(base, recs + three_repeats("RQ03"))
    assert r["new_cases"] == ["RQ03"] and r["passed"] is True


def test_fast_thinking_leak_increase_fails():
    base, _ = base_and_records(cases=("EV01",), n_ok=50)
    leak = [record("EV01", 1, [turn(checks(50))]), record("EV01", 2, [turn(checks(50))]),
            failing_repeat("EV01", 3, 49, ["thinking_off"])]
    r = B.compare(base, leak)
    assert r["fast_thinking"] == {"baseline": 0, "candidate": 1, "candidate_case_ids": ["EV01"], "verdict": "fail"}
    assert r["passed"] is False and len(r["fails"]) == 1 and r["fails"][0].startswith("fast thinking leak")
    # equal to the baseline's count is not a leak
    base_leak = frozen(leak)
    assert B.compare(base_leak, leak)["passed"] is True


def test_insufficient_candidate_samples_fail_unless_allowed():
    base, _ = base_and_records(cases=("EV01",))
    thin = three_repeats()
    for rec in thin[1:]:
        rec["turns"][0]["result"]["timing"]["total_s"] = None
    r = B.compare(base, thin)
    assert r["latency"]["direct_fast"]["total_s"]["verdict"] == "insufficient_samples"
    assert r["insufficient"] == [{"workload": "direct_fast", "metric": "total_s", "baseline_n": 3, "candidate_n": 1}]
    assert r["fails"] == [] and r["passed"] is False
    allowed = B.compare(base, thin, allow_insufficient=True)
    assert allowed["passed"] is True and allowed["insufficient"] == r["insufficient"]


def test_insufficient_baseline_samples_never_pass_silently():
    base = frozen([record("EV09", 1, effort="max"), record("EV09", 2, effort="max")])
    r = B.compare(base, [record("EV09", r, effort="max") for r in (1, 2, 3)])
    assert r["latency"]["max"]["total_s"]["verdict"] == "insufficient_samples" and r["passed"] is False


def test_compare_refuses_a_document_that_is_not_a_baseline():
    _, recs = base_and_records()
    with pytest.raises(B.BaselineError, match="evalset-baseline"):
        B.compare({"kind": "evalset", "schema": 1, "cases": recs}, recs)


# --------------------------------------------------------------- markdown --

def test_render_markdown_has_every_table_and_a_workload_row(tmp_path):
    recs = three_repeats() + [record("EV08", r, [turn(checks(1, fail=["required_sections"]))], effort="think")
                              for r in (1, 2)]
    doc = B.freeze(B.load_runs([write_run(tmp_path, "run1", recs, repeats=3)]), frozen_at=FROZEN_AT)
    md = B.render_markdown(doc)
    assert "| Case | Workload | Effort | Repeats | Pass rate | Mean score | Failing checks (most often) |" in md
    assert "| Class | Metric | n | Missing | p50 | p95 | min | max | Allowed p50 | Allowed p95 | Tolerance |" in md
    assert "| Check | Passed | Total | Rate |" in md
    assert "### Conditions" in md and 'label "synthetic stack, cap 2"' in md and "`0123abc`" in md
    assert "run `run1`" in md and str(tmp_path) not in md                     # the run's name, not the path
    assert "| direct_fast | total_s | 3 | 0 | 1.00 | 1.00 | 1.00 | 1.00 | 1.70 | 1.70 | rel 0.200, abs 0.50 s |" in md
    assert "| think | total_s | 2 | 0 |" in md and "insufficient samples (n < 3)" in md
    assert "| EV08 | think | think | 2 | 0.000 | 0.500 | required_sections x2 |" in md
    assert "| EV01 | direct_fast | fast | 3 | 1.000 | 1.000 | none |" in md
    assert "| required_sections | 0 | 2 | 0.000 |" in md


def test_render_markdown_escapes_pipes_in_labels(tmp_path):
    doc = B.freeze(B.load_runs([write_run(tmp_path, "r", [record("EV01")], label="a | b\nc")]),
                   frozen_at=FROZEN_AT)
    assert 'label "a \\| b c"' in B.render_markdown(doc)


# -------------------------------------------------------------------- CLI --

def test_cli_freeze_compare_show_exit_codes(tmp_path, capsys):
    dirs = [write_run(tmp_path, f"base{i}", [record("EV01", 1), record("RQ01", 1)]) for i in (1, 2, 3)]
    out, md = tmp_path / "baseline.json", tmp_path / "baseline.md"
    assert B.main(["freeze", *dirs, "--out", str(out), "--markdown", str(md)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "evalset-baseline" and doc["repeats_per_case"] == {"EV01": 3, "RQ01": 3}
    assert "| Case | Workload |" in md.read_text(encoding="utf-8")
    assert B.main(["compare", str(out), *dirs]) == 0
    assert "compare: PASSED" in capsys.readouterr().out

    worse = [write_run(tmp_path, f"cand{i}", [record("EV01", 1, [turn(checks(1, fail=["format"]))]),
                                               record("RQ01", 1)]) for i in (1, 2, 3)]
    report = tmp_path / "report.json"
    assert B.main(["compare", str(out), *worse, "--out", str(report)]) == 1
    printed = capsys.readouterr().out
    assert "compare: NOT PASSED" in printed and "FAIL case EV01" in printed
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is False

    thin = [write_run(tmp_path, "thin", [record("EV01", 1), record("RQ01", 1)])]   # 2 samples per metric
    assert B.main(["compare", str(out), *thin]) == 1
    assert "INSUFFICIENT latency direct_fast/total_s" in capsys.readouterr().out
    assert B.main(["compare", str(out), *thin, "--allow-insufficient"]) == 0
    capsys.readouterr()

    assert B.main(["show", str(out)]) == 0
    assert "| Class | Metric |" in capsys.readouterr().out


def test_cli_mixed_labels_and_bad_input_exit_2(tmp_path, capsys):
    a = write_run(tmp_path, "a", [record("EV01")], label="one")
    b = write_run(tmp_path, "b", [record("EV01")], label="two")
    out = tmp_path / "b.json"
    assert B.main(["freeze", a, b, "--out", str(out)]) == 2
    assert "--allow-mixed-conditions" in capsys.readouterr().err and not out.exists()
    assert B.main(["freeze", a, b, "--out", str(out), "--allow-mixed-conditions"]) == 0
    assert B.main(["show", a + "/results.json"]) == 2
    assert "only kind 'evalset-baseline'" in capsys.readouterr().err


def test_cli_runs_as_a_script(tmp_path):
    dirs = [write_run(tmp_path, f"r{i}", [record("EV01", 1)]) for i in (1, 2, 3)]
    out = tmp_path / "baseline.json"
    script = str(AIQ / "baseline.py")
    run = subprocess.run([sys.executable, script, "freeze", *dirs, "--out", str(out)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    shown = subprocess.run([sys.executable, script, "show", str(out)], capture_output=True, text=True, timeout=60)
    assert shown.returncode == 0 and "| direct_fast | total_s | 3 |" in shown.stdout
