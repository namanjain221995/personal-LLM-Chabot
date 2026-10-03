"""B-04: baseline.py freezes eval-set runs into a baseline and compares a candidate against it.

  * nearest-rank percentiles; the latency method of fix round 1: units are
    (case, turn), noise is the pooled within-unit log-sd, the statistic is the
    geometric mean of per-unit median ratios, p95 gates only with >= 20
    samples a side, insufficient needs 3 samples a side and a unit of 2;
  * freeze refuses incomplete, interrupted, edited, copied, mixed or
    non-public input; compare refuses an incomparable candidate and a
    baseline whose stored numbers disagree with a recomputation;
  * compare: case, per-check, failed-turn, overall, latency, missing-case and
    Fast-thinking verdicts on each side of their thresholds, exact fractions;
  * the markdown tables and the CLI exit codes (a crash is 2, never 1).

Everything is synthetic results.json written to tmp dirs or records built in
memory. Nothing here calls a model, a service or the network.
"""
from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

AIQ = Path(__file__).resolve().parents[1]
if str(AIQ) not in sys.path:
    sys.path.append(str(AIQ))

import baseline as B  # noqa: E402

FROZEN_AT = "2026-10-04T05:00:00+05:30"
SHA = "e" * 64
FAST = {"first_event_s": 0.1, "first_token_s": 0.3, "first_answer_s": 0.3, "total_s": 1.0}
DIRECT = [c for c, w in B.WORKLOAD.items() if w == "direct_fast"]
EFFORT = {"EV06": "think", "EV08": "think", "EV09": "max"}


# ----------------------------------------------------------------- helpers --

def checks(n_ok: int = 1, fail=(), thinking: bool = True) -> list:
    """n_ok passing checks named c00.., one failing check per name in `fail`, and a passing thinking_off check
    (the harness emits one on every Fast turn) unless thinking is False or thinking_off is in `fail`."""
    out = [{"check": f"c{i:02d}", "dimension": "format", "ok": True, "detail": ""} for i in range(n_ok)]
    out += [{"check": name, "dimension": "format", "ok": False, "detail": "synthetic"} for name in fail]
    if thinking and B.THINKING_CHECK not in fail:
        out.append({"check": B.THINKING_CHECK, "dimension": "effort", "ok": True, "detail": ""})
    return out


def turn(chk=None, timing=None, **result) -> dict:
    res = {"http": 200, "answer": "synthetic answer", "errors": [], "meta": {},
           "timing": dict(FAST if timing is None else timing)}
    res.update(result)
    return {"message": "synthetic", "expect": {}, "checks": checks() if chk is None else chk, "result": res}


def record(cid: str, repeat: int = 1, turns=None, *, effort: str = "fast", workload="map", error=None) -> dict:
    rec = {"id": cid, "category": "synthetic", "section": "11", "effort": effort, "repeat": repeat,
           "conversation_id": f"aiq-{cid}-r{repeat}", "error": error, "attachments": [],
           "turns": [turn()] if turns is None else turns}
    if workload == "map":
        rec["workload"] = B.WORKLOAD[cid]
    elif workload is not None:
        rec["workload"] = workload
    return rec


def run_conditions(**over) -> dict:
    src = {"dir": None, "started": "2026-10-04T05:10:00+0530", "finished": "2026-10-04T05:20:00+0530",
           "seconds": 600, "interrupted": False, "deadline_reached": False, "label": "synthetic stack, cap 2",
           "harness_commit": "0123abc", "workers": 1, "repeats": 1, "only": [], "eval_set_sha256": SHA,
           "account": {"conversations": 0, "facts": 0, "checked": True}}
    src.update(over)
    return src


def pooled(records: list, **over) -> list:
    """Records as load_runs returns them: one run whose repeats is the records per case."""
    per_case = {}
    for r in records:
        per_case[r["id"]] = per_case.get(r["id"], 0) + 1
    over.setdefault("repeats", max(per_case.values()) if per_case else 1)
    src = run_conditions(**over)
    return [dict(r, _run=src) for r in records]


def frozen(records: list, **over) -> dict:
    return B.freeze(pooled(records, **over), frozen_at=FROZEN_AT)


def compare(base: dict, records: list, **kw) -> dict:
    over = kw.pop("cond", {})
    return B.compare(base, pooled(records, **over), compared_at=FROZEN_AT, **kw)


def write_run(root: Path, name: str, records: list, *, label: str = "synthetic stack, cap 2", kind="evalset",
              schema=1, repeats: int = 1, cond=None, **top) -> str:
    d = root / name
    d.mkdir(parents=True)
    conditions = {"base": "http://127.0.0.1:28080", "harness_commit": "0123abc", "label": label, "workers": 1,
                  "repeats": repeats, "only": [], "eval_set_sha256": SHA,
                  "health": {"status": "ok", "checks": {"vllm": "ok"}},
                  "account": {"conversations": 0, "facts": 0, "checked": True}}
    conditions.update(cond or {})
    doc = {"kind": kind, "schema": schema, "started": "2026-10-04T05:10:00+0530",
           "finished": "2026-10-04T05:20:00+0530", "seconds": 600, "conditions": conditions, "cases": records}
    doc.update(top)
    (d / "results.json").write_text(json.dumps(doc), encoding="utf-8")
    return str(d)


def three_repeats(cid: str = "EV01", **kw) -> list:
    return [record(cid, r, **kw) for r in (1, 2, 3)]


def latency(doc: dict, workload: str = "direct_fast", metric: str = "total_s") -> dict:
    return doc["latency"][workload][metric]


def timed(cid: str, totals, *, effort: str = "fast", **timing) -> list:
    return [record(cid, r, [turn(timing=dict(FAST, total_s=float(v), **timing))], effort=effort)
            for r, v in enumerate(totals, start=1)]


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


def test_tolerance_rel_floor_sigma_and_class_abs():
    assert B.tolerance("direct_fast", "first_answer_s", 0.0) == {"rel": 0.2, "abs": 0.25}
    assert B.tolerance("direct_fast", "first_answer_s", 0.09) == {"rel": 0.2, "abs": 0.25}   # 2 x sigma = 0.18
    assert B.tolerance("direct_fast", "first_answer_s", 0.5) == {"rel": 1.0, "abs": 0.25}    # 2 x sigma wins
    expected = {"direct_fast": 0.25, "evidence_fast": 0.5, "live_search_fast": 1.0, "long_context": 1.0,
                "think": 2.0, "max": 10.0}
    for w, abs_s in expected.items():
        assert B.tolerance(w, "first_answer_s")["abs"] == abs_s
        assert B.tolerance(w, "first_event_s")["abs"] == abs_s
        assert B.tolerance(w, "total_s")["abs"] == 2 * abs_s
    with pytest.raises(B.BaselineError):
        B.tolerance("large_document", "total_s")


def test_gated_metrics_first_event_only_for_fast_classes_and_never_first_token():
    for w in ("direct_fast", "evidence_fast", "live_search_fast", "long_context"):
        assert B.gated_metrics(w) == ("first_event_s", "first_answer_s", "total_s")
    for w in ("think", "max"):
        assert B.gated_metrics(w) == ("first_answer_s", "total_s")
    assert all("first_token_s" not in B.gated_metrics(w) for w in B.CLASS_ORDER)


def test_noise_is_the_pooled_within_unit_log_sd_and_allowed_ratio_follows():
    # EV01 total_s 1, 2, 4: var(ln) = (ln 2)^2; RQ01 10, 10, 10: var 0 -> sigma = ln 2 / sqrt 2
    doc = frozen(timed("EV01", (1, 2, 4)) + timed("RQ01", (10, 10, 10)))
    e = latency(doc)
    assert e["units"]["EV01/t1"] == {"n": 3, "median": 2.0, "log_var": pytest.approx(math.log(2) ** 2),
                                     "samples": [1.0, 2.0, 4.0]}
    assert e["units"]["RQ01/t1"]["log_var"] == 0.0
    sigma = math.log(2) / math.sqrt(2)
    scale = math.sqrt(2.0 * 10.0)                                       # geometric mean of the unit medians
    assert e["sigma"] == pytest.approx(sigma) and e["scale"] == pytest.approx(scale)
    assert e["rel"] == pytest.approx(2 * sigma) and e["abs"] == 0.5    # total_s: 2 x 0.25
    assert e["allowed_ratio"] == pytest.approx(1 + 2 * sigma + 0.5 / scale)
    assert (e["n"], e["units_n"], e["noise_units"], e["status"]) == (6, 2, 2, "ok")
    assert (e["p50"], e["p95"], e["min"], e["max"]) == (4.0, 10.0, 1.0, 10.0)
    assert e["mean"] == pytest.approx(37 / 6)
    assert e["p95_status"] == "reported" and e["allowed_p95"] is None     # n < 20
    # a small spread keeps the 0.20 floor
    small = latency(frozen(timed("EV01", (10.0, 10.5, 11.0))))
    assert small["rel"] == 0.2 and small["allowed_ratio"] == pytest.approx(1.2 + 0.5 / 10.5)


def test_ratio_on_each_side_of_the_allowed_value():
    base = frozen(timed("EV01", (10, 10, 10)) + timed("RQ01", (10, 10, 10)))
    assert latency(base)["allowed_ratio"] == pytest.approx(1.25)       # 1 + 0.20 + 0.5 / 10
    below = compare(base, timed("EV01", (12.4,) * 3) + timed("RQ01", (12.4,) * 3))
    assert below["latency"]["direct_fast"]["total_s"]["ratio"] == pytest.approx(1.24)
    assert below["latency"]["direct_fast"]["total_s"]["verdict"] == "pass" and below["passed"] is True
    above = compare(base, timed("EV01", (12.6,) * 3) + timed("RQ01", (12.6,) * 3))
    entry = above["latency"]["direct_fast"]["total_s"]
    assert entry["verdict"] == "fail" and entry["over"] == ["ratio"] and above["passed"] is False
    assert any(f.startswith("latency direct_fast/total_s: median ratio 1.260") for f in above["fails"])


def test_noise_ignores_the_case_mix():
    """QA P2: eight direct_fast cases of very different length; the old class cv let every case run 3x slower."""
    total = {"EV01": 1.2, "EV02": 6.0, "EV03": 12.0, "RQ01": 15.0, "RQ03": 2.0, "RQ05": 25.0, "RQ06": 8.0,
             "RQ07": 5.0}
    rng = random.Random(7)

    def run(scale: float) -> list:
        return [record(cid, r, [turn(timing=dict(FAST, first_answer_s=0.5 * scale * rng.uniform(0.95, 1.05),
                                                 total_s=t * scale * rng.uniform(0.95, 1.05)))])
                for cid, t in total.items() for r in (1, 2, 3)]

    base = frozen(run(1.0))
    assert latency(base)["sigma"] < 0.05 and latency(base)["rel"] == 0.2
    assert compare(base, run(1.0))["passed"] is True
    slow = compare(base, run(1.5))
    assert slow["latency"]["direct_fast"]["total_s"]["verdict"] == "fail" and slow["passed"] is False


def _family_doc() -> dict:
    # fast family: direct_fast EV01 10, 10, 10 (var 0), evidence_fast EV05 1, 2, 4 (var (ln 2)^2);
    # slow family: think EV06 1, 4, 16 (var (ln 4)^2), max EV09 5, 5, 5 (var 0)
    return frozen(timed("EV01", (10, 10, 10)) + timed("EV05", (1, 2, 4)) + timed("EV06", (1, 4, 16), effort="think")
                  + timed("EV09", (5, 5, 5), effort="max"))


def test_sigma_is_pooled_within_the_effort_family_and_never_across_families():
    """Re-QA 1: pooling every class let think/max noise loosen the Fast gates (1.3x Fast slowdowns passed)."""
    doc = _family_doc()
    ln2 = math.log(2)
    quiet, evidence, think, mx = (latency(doc, w) for w in ("direct_fast", "evidence_fast", "think", "max"))
    assert quiet["sigma_family"] == evidence["sigma_family"] == "fast"
    assert think["sigma_family"] == mx["sigma_family"] == "slow"
    # the fast family pools EV01 and EV05 only: sqrt(mean(0, (ln 2)^2)); all four classes would give sqrt(5/4) ln 2
    assert quiet["sigma_class"] == 0.0 and quiet["sigma_pooled"] == pytest.approx(ln2 / math.sqrt(2))
    assert quiet["sigma"] == pytest.approx(ln2 / math.sqrt(2)) and quiet["rel"] == pytest.approx(math.sqrt(2) * ln2)
    assert quiet["allowed_ratio"] == pytest.approx(1 + math.sqrt(2) * ln2 + 0.5 / 10)
    assert evidence["sigma"] == pytest.approx(ln2)                     # its own figure is the larger
    # the slow family pools EV06 and EV09: sqrt(mean((2 ln 2)^2, 0)) = sqrt(2) ln 2
    assert mx["sigma_class"] == 0.0 and mx["sigma_pooled"] == pytest.approx(math.sqrt(2) * ln2)
    assert mx["sigma"] == pytest.approx(math.sqrt(2) * ln2)
    assert think["sigma"] == pytest.approx(2 * ln2) and think["sigma_pooled"] == pytest.approx(math.sqrt(2) * ln2)
    assert B.CONSTANTS["sigma_families"] == {"fast": list(B.FAST_CLASSES), "slow": ["think", "max"]}
    assert "effort family" in doc["method"]["noise"] and "never pooled together" in doc["method"]["noise"]


def test_the_family_sigma_is_part_of_the_baseline_recomputation():
    doc = json.loads(json.dumps(_family_doc()))
    B._verify_baseline(doc)
    tampered = json.loads(json.dumps(doc))
    tampered["latency"]["direct_fast"]["total_s"]["sigma"] = 0.0
    with pytest.raises(B.BaselineError, match="disagree with a recomputation"):
        B._verify_baseline(tampered)
    # rewrite think consistently with new samples: max (its family partner) no longer matches its recomputation,
    # while the fast family is untouched by the change
    rewritten = json.loads(json.dumps(doc))
    think = rewritten["latency"]["think"]["total_s"]
    units = {u: [6.0, 6.0, 6.0] for u in think["units"]}
    slow = B.family_sigmas({"think": units, "max": {u: v["samples"] for u, v in
                                                    rewritten["latency"]["max"]["total_s"]["units"].items()}})
    rewritten["latency"]["think"]["total_s"] = B._describe("think", "total_s", units, think["missing"],
                                                           think["excluded"], slow["think"])
    with pytest.raises(B.BaselineError, match="latency.max.total_s"):
        B._verify_baseline(rewritten)


def test_values_at_or_below_the_floor_are_clamped_before_ln():
    e = latency(frozen(timed("EV01", (0.0, 0.0005, 0.001))))
    assert e["units"]["EV01/t1"]["log_var"] == 0.0 and e["sigma"] == 0.0
    assert e["scale"] == pytest.approx(B.LOG_FLOOR_S)


def test_first_event_gates_fast_classes_only_and_first_token_never_gates():
    base = frozen(three_repeats() + three_repeats("EV08", effort="think"))
    late_event = [record("EV01", r, [turn(timing=dict(FAST, first_event_s=5.0))]) for r in (1, 2, 3)]
    r = compare(base, late_event + three_repeats("EV08", effort="think"))
    assert r["latency"]["direct_fast"]["first_event_s"]["gated"] is True
    assert r["latency"]["direct_fast"]["first_event_s"]["verdict"] == "fail" and r["passed"] is False
    think_late = [record("EV08", r, [turn(timing=dict(FAST, first_event_s=5.0))], effort="think") for r in (1, 2, 3)]
    r = compare(base, three_repeats() + think_late)
    assert r["latency"]["think"]["first_event_s"]["gated"] is False
    assert r["latency"]["think"]["first_event_s"]["verdict"] == "fail" and r["passed"] is True
    late_token = [record("EV01", r, [turn(timing=dict(FAST, first_token_s=5.0))]) for r in (1, 2, 3)]
    r = compare(base, late_token + three_repeats("EV08", effort="think"))
    assert r["latency"]["direct_fast"]["first_token_s"]["verdict"] == "fail"
    assert r["latency"]["direct_fast"]["first_token_s"]["gated"] is False and r["passed"] is True


def test_p95_gates_only_with_twenty_samples_on_each_side():
    base_recs = [rec for i, cid in enumerate(DIRECT) for rec in timed(cid, (1.0 + i,) * 3)]   # 24 samples
    base = frozen(base_recs)
    e = latency(base)
    assert (e["n"], e["p95"], e["p95_status"]) == (24, 8.0, "gated")
    assert e["allowed_p95"] == pytest.approx(8.0 * 1.2 + 0.5)
    cand = [rec for i, cid in enumerate(DIRECT) for rec in timed(cid, (1.0 + i,) * 3)]
    cand[0]["turns"][0]["result"]["timing"]["total_s"] = 50.0       # EV01 r1: its unit median stays 1.0
    cand[3]["turns"][0]["result"]["timing"]["total_s"] = 50.0       # EV02 r1: median stays 2.0
    r = compare(base, cand)
    entry = r["latency"]["direct_fast"]["total_s"]
    assert entry["ratio"] == pytest.approx(1.0) and entry["p95_status"] == "gated"
    assert entry["over"] == ["p95"] and r["passed"] is False
    # six samples a side: the same two outliers are reported, never gated
    small = frozen(timed("EV01", (1, 1, 1)) + timed("EV02", (2, 2, 2)))
    assert latency(small)["p95_status"] == "reported" and latency(small)["allowed_p95"] is None
    r = compare(small, timed("EV01", (50, 1, 1)) + timed("EV02", (50, 2, 2)))
    entry = r["latency"]["direct_fast"]["total_s"]
    assert entry["candidate_p95"] == 50.0 and entry["p95_status"] == "reported" and entry["verdict"] == "pass"
    assert r["passed"] is True


def test_insufficient_needs_three_samples_and_a_unit_with_two():
    recs = [rec for cid in ("EV01", "RQ01", "RQ03") for rec in three_repeats(cid)]
    for rec in recs:
        if rec["repeat"] > 1:
            rec["turns"][0]["result"]["timing"]["total_s"] = None     # one total_s sample per unit
    e = latency(frozen(recs))
    assert (e["n"], e["units_n"], e["noise_units"], e["missing"]) == (3, 3, 0, 6)
    assert e["status"] == "insufficient_samples" and e["allowed_ratio"] is None and e["rel"] is None
    two = frozen(three_repeats())
    two_e = latency(two)
    assert two_e["status"] == "ok"
    thin = three_repeats()
    thin[1]["turns"][0]["result"]["timing"]["total_s"] = None
    thin[2]["turns"][0]["result"]["timing"]["total_s"] = None
    assert latency(frozen(thin))["status"] == "insufficient_samples"          # n = 1


def test_output_token_rate_is_the_median_per_class_and_never_gates():
    def tok(n_tokens, first, total):
        return turn(timing=dict(FAST, first_answer_s=first, total_s=total), usage={"completion_tokens": n_tokens})

    recs = [record("EV01", 1, [tok(100, 1.0, 3.0)]), record("EV01", 2, [tok(200, 1.0, 3.0)]),
            record("EV01", 3, [tok(200, 1.0, 3.0)]), record("RQ01", 1, [turn(usage={"completion_tokens": None})]),
            record("RQ01", 2, [tok(10, 1.0, 1.0)]), record("RQ01", 3)]      # no tokens / zero span: no sample
    base = frozen(recs)
    assert base["output_tokens_per_s"] == {"direct_fast": {"n": 3, "median": 100.0}}
    slow = [record("EV01", r, [tok(10, 1.0, 3.0)]) for r in (1, 2, 3)] + three_repeats("RQ01")
    r = compare(base, slow)
    assert r["output_tokens_per_s"]["direct_fast"]["candidate"] == {"n": 3, "median": 5.0}
    assert r["passed"] is True


def test_method_states_the_false_block_rate_and_the_tie_break():
    m = frozen(three_repeats())["method"]
    fb = m["false_block"]
    # the Monte Carlo of fix round 2 (uniform: 400 trials a row; heterogeneous: 300 trials a cell)
    assert fb.startswith("Expected false blocks and power of the LATENCY gates only")
    assert "blocked in 3.0 % at sigma 0.10 and 8.5 % at sigma 0.15 without spikes" in fb
    assert "19 % (sigma 0.15) and 24 % (sigma 0.30) with spikes" in fb
    assert ("1.3x is blocked in 99.8 % (sigma 0.10) and 96.5 % (sigma 0.15) without spikes, but only in 72-75 % "
            "with spikes") in fb
    assert "with spikes only in 54-57 %, against a 21-24 % false-block rate" in fb
    assert "a real 1.3x slowdown passes about half the time" in fb
    assert "Pooling sigma within the effort family" in fb
    assert "max(the class's own sigma" in m["noise"] and "effort family" in m["noise"]
    assert "35-58 %" in m["noise"] and "99 % within the family" in m["noise"]
    assert "re-run the BASELINE commit in the same window" in m["false_block"]
    assert "ceil(q x n)" in m["percentile"] and "ln x" in m["noise"] and "geometric mean" in m["ratio"]


# ----------------------------------------------------------------- freeze --

def test_freeze_pools_three_run_directories(tmp_path):
    def recs():
        return [record("EV01", 1), record("RQ01", 1), record("EV08", 1, effort="think")]

    dirs = [write_run(tmp_path, f"run{i}", _ids(recs(), f"run{i}")) for i in (1, 2, 3)]
    records = B.load_runs(dirs)
    assert len(records) == 9 and records[0]["_run"]["dir"] == dirs[0]
    doc = B.freeze(records, frozen_at=FROZEN_AT)
    assert doc["kind"] == "evalset-baseline" and doc["schema"] == 3 and doc["frozen_at"] == FROZEN_AT
    assert doc["repeats_per_case"] == {"EV01": 3, "RQ01": 3, "EV08": 3}
    assert [s["dir"] for s in doc["sources"]] == ["run1", "run2", "run3"]           # basename only
    assert all(s["label"] == "synthetic stack, cap 2" and s["harness_commit"] == "0123abc"
               and s["eval_set_sha256"] == SHA and s["workers"] == 1 for s in doc["sources"])
    assert doc["conditions"]["eval_set_sha256"] == SHA and doc["conditions"]["workers"] == 1
    assert latency(doc)["n"] == 6 and latency(doc, "think")["n"] == 3
    assert doc["quality"]["cases"]["EV01"]["pass_rate"] == 1.0
    assert doc["quality"]["overall_mean_score_exact"] == "1"
    assert json.dumps(B.freeze(B.load_runs(dirs), frozen_at=FROZEN_AT)) == json.dumps(doc)   # deterministic
    assert B.compare(doc, B.load_runs(dirs))["passed"] is True                                # self-compare


def test_freeze_refuses_fewer_than_three_records_per_case():
    with pytest.raises(B.BaselineError, match="fewer than 3 records"):
        frozen([record("EV01", 1), record("EV01", 2)])


@pytest.mark.parametrize("top,match", [({"finished": None}, "did not finish"), ({"interrupted": True}, "interrupted"),
                                       ({"deadline_reached": True}, "deadline")])
def test_freeze_refuses_unfinished_interrupted_or_deadline_runs(tmp_path, top, match):
    d = write_run(tmp_path, "r", three_repeats(), repeats=3, **top)
    with pytest.raises(B.BaselineError, match=match):
        B.freeze(B.load_runs([d]))


def test_freeze_refuses_record_counts_that_disagree_with_repeats(tmp_path):
    short = write_run(tmp_path, "short", three_repeats()[:2] + three_repeats("RQ01"), repeats=3)
    with pytest.raises(B.BaselineError, match="case EV01 has 2 record"):
        B.freeze(B.load_runs([short]))
    outside = write_run(tmp_path, "outside", three_repeats() + three_repeats("RQ01"), repeats=3,
                        cond={"only": ["EV01"]})
    with pytest.raises(B.BaselineError, match="RQ01 is not in its --only"):
        B.freeze(B.load_runs([outside]))
    # a run of the whole set that lacks a case another pooled run has
    a = write_run(tmp_path, "a", [record("EV01", 1), record("RQ01", 1)])
    b_recs = [record("EV01", 1)]
    b_recs[0]["conversation_id"] = "other"
    b = write_run(tmp_path, "b", b_recs)
    with pytest.raises(B.BaselineError, match="case RQ01 has 0 record"):
        B.freeze(B.load_runs([a, b]))
    # --only runs add exactly their own repeats
    full = write_run(tmp_path, "full", three_repeats() + three_repeats("RQ01"), repeats=3)
    extra = [record("EV01", r) for r in (4, 5)]
    only = write_run(tmp_path, "only", extra, repeats=2, cond={"only": ["EV01"]})
    assert B.freeze(B.load_runs([full, only]))["repeats_per_case"] == {"EV01": 5, "RQ01": 3}


def test_freeze_refuses_duplicate_conversation_ids(tmp_path):
    src = write_run(tmp_path, "orig", three_repeats(), repeats=3)
    shutil.copytree(src, str(tmp_path / "orig-copy"))
    with pytest.raises(B.BaselineError, match="appears twice"):
        B.freeze(B.load_runs([src, str(tmp_path / "orig-copy")]))


def test_mixed_condition_labels_are_refused_without_the_flag(tmp_path):
    a = write_run(tmp_path, "a", three_repeats(), label="dev stack, cap 2", repeats=3)
    b = write_run(tmp_path, "b", [record("EV01", 4)], label="dev stack, cap 4", repeats=1,
                  cond={"only": ["EV01"]})
    with pytest.raises(B.BaselineError, match="different conditions labels"):
        B.load_runs([a, b])
    doc = B.freeze(B.load_runs([a, b], allow_mixed=True))
    assert [s["label"] for s in doc["sources"]] == ["dev stack, cap 2", "dev stack, cap 4"]
    assert doc["conditions"]["labels"] == ["dev stack, cap 2", "dev stack, cap 4"]


@pytest.mark.parametrize("cond", [{"eval_set_sha256": "f" * 64}, {"workers": 2}])
def test_pooled_runs_with_different_eval_set_or_workers_are_refused_even_mixed(tmp_path, cond):
    a = write_run(tmp_path, "a", three_repeats(), repeats=3)
    b = write_run(tmp_path, "b", [record("EV01", 4)], cond=dict(cond, only=["EV01"]))
    with pytest.raises(B.BaselineError, match="differ in conditions"):
        B.freeze(B.load_runs([a, b], allow_mixed=True))


def test_a_run_without_an_eval_set_fingerprint_is_refused(tmp_path):
    d = write_run(tmp_path, "r", three_repeats(), repeats=3, cond={"eval_set_sha256": None})
    with pytest.raises(B.BaselineError, match="eval_set_sha256 is missing"):
        B.load_runs([d])


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


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["turns"][0]["result"]["timing"].update(total_s="slow"), "total_s"),
    (lambda r: r.update(workload=["direct_fast"]), "workload"),
    (lambda r: r.update(effort=None), "effort"),
    (lambda r: r["turns"][0]["checks"][0].update(ok="true"), "boolean `ok`"),
    (lambda r: r["turns"][0]["result"].update(http="200"), "http"),
    (lambda r: r["turns"][0]["result"].update(usage={"completion_tokens": 1.5}), "completion_tokens"),
    (lambda r: r["turns"][0]["result"].update(terminal=1), "terminal"),
    (lambda r: r.update(error=["x"]), "error"),
])
def test_wrong_types_are_refused(tmp_path, mutate, match):
    rec = record("EV01")
    mutate(rec)
    with pytest.raises(B.BaselineError, match=match):
        B.load_runs([write_run(tmp_path, "x", [rec])])


def test_error_records_score_zero_and_do_not_pass():
    recs = three_repeats()
    recs[1]["error"] = "ReadTimeout: stream stalled"                  # its checks all passed, still 0
    case = frozen(recs)["quality"]["cases"]["EV01"]
    assert (case["repeats"], case["passes"], case["errors"]) == (3, 2, 1)
    assert case["mean_score_exact"] == "2/3" and case["pass_rate"] == pytest.approx(2 / 3)
    assert case["check_passes"] == {"c00": 2, "thinking_off": 2}
    recs = three_repeats()
    recs[0] = record("EV01", 1, turns=[], error="ConnectError: refused")
    case = frozen(recs)["quality"]["cases"]["EV01"]
    assert (case["mean_score_exact"], case["failed_turns"], case["turns"]) == ("2/3", 1, 3)
    recs = three_repeats()
    recs[0] = record("EV01", 1, turns=[turn(chk=[])])
    with pytest.raises(B.BaselineError, match="thinking_off"):       # a Fast turn without the check
        frozen(recs)


def test_case_scores_check_rates_and_fast_thinking():
    recs = [record("EV01", 1, [turn(checks(3, fail=["max_chars"])), turn(checks(4))]),     # 9/10, not a pass
            record("EV01", 2, [turn(checks(3, fail=["thinking_off"])), turn(checks(4))]),  # 8/9, Fast reasoning
            record("EV01", 3, [turn(checks(3)), turn(checks(4))]),
            *[record("EV08", r, [turn(checks(1, fail=["thinking_off"]))], effort="think") for r in (1, 2, 3)]]
    q = frozen(recs)["quality"]
    c = q["cases"]["EV01"]
    assert (c["passes"], c["mean_score_exact"]) == (1, str((Fraction(9, 10) + Fraction(8, 9) + 1) / 3))
    assert c["failing_checks"] == {"max_chars": 1, "thinking_off": 1}
    assert c["check_passes"] == {"c00": 3, "c01": 3, "c02": 3, "c03": 3, "max_chars": 0, "thinking_off": 2}
    assert q["checks"]["thinking_off"] == {"passed": 5, "total": 9, "rate": 5 / 9}
    assert (q["fast_turns"], q["fast_thinking_turns"], q["fast_thinking_case_ids"]) == (6, 1, ["EV01"])


def test_workload_comes_from_the_record_then_the_map():
    lat = frozen(three_repeats("EV05", workload=None) + three_repeats("EV01", workload="long_context"))["latency"]
    assert set(lat) == {"evidence_fast", "long_context"}
    with pytest.raises(B.BaselineError, match="not in the eval-set map"):
        frozen(three_repeats("ZZ99", workload=None))
    with pytest.raises(B.BaselineError, match="no tolerance"):
        frozen(three_repeats("EV01", workload="large_document"))


# --------------------------------------------------------- public safety --

@pytest.mark.parametrize("label", [
    "dev stack at http://box:28080", "worker 192.0.2.7 cap 2", "head 2001:db8::1 cap 2", "loopback [::1]:28080",
    "node host-a.internal", "box.local stack", "runs on example.com", "two\nlines", "tab\there",
    "evil ‮gnp.exe", "isolate ⁦x⁩", "bell \x07", "line sep  ",
])
def test_freeze_refuses_a_label_that_is_not_public(label):
    with pytest.raises(B.BaselineError, match="conditions.label holds"):
        frozen(three_repeats(), label=label)


@pytest.mark.parametrize("label", [
    "dev stack llmdev, CPU image, router shared on main, no embed/rerank/search, cap 2",
    "Qwen3.6-35B-A3B-NVFP4, vLLM 0.11.1rc2, e.g. no search", "window 05:10-06:40 IST, workers 1",
])
def test_freeze_accepts_a_plain_label(label):
    assert frozen(three_repeats(), label=label)["sources"][0]["label"] == label


def test_freeze_refuses_an_empty_label():
    with pytest.raises(B.BaselineError, match="label is empty"):
        frozen(three_repeats(), label="")


def test_freeze_keeps_no_endpoint_health_or_account_detail(tmp_path):
    recs = three_repeats()
    recs[0]["traceback"] = "Traceback ... http://192.0.2.7:1234/chat"
    recs[0]["turns"][0]["result"].update(request_id="req-123", meta={"url": "http://192.0.2.7:1234/secret"},
                                         versions={"model": "Qwen3.6-35B-A3B-NVFP4", "application": "9.9"})
    d = write_run(tmp_path, "pub", recs, repeats=3,
                  cond={"health": {"status": "ok", "checks": {"192.0.2.7": "ok"}},
                        "account": {"conversations": 0, "facts": 0, "checked": True, "email": "someone@example.org"},
                        "features": {"memory": True}, "code_sandbox": "docker"})
    doc = B.freeze(B.load_runs([d]))
    blob = json.dumps(doc) + B.render_markdown(doc)
    for needle in ("127.0.0.1", "28080", "192.0.2.7", "req-123", "/secret", "someone", "health", str(tmp_path)):
        assert needle not in blob, needle
    src = doc["sources"][0]
    assert src["dir"] == "pub" and src["model_ids"] == ["Qwen3.6-35B-A3B-NVFP4"]
    assert src["account"] == {"conversations": 0, "facts": 0, "checked": True, "allowed_used": None}
    assert "base" not in src and "features" not in src


# ---------------------------------------------------------------- compare --

def base_and_records(cases=("EV01", "RQ01"), n_ok: int = 1) -> tuple:
    recs = [record(cid, r, [turn(checks(n_ok))]) for cid in cases for r in (1, 2, 3)]
    return frozen(recs), recs


def failing_repeat(cid: str, repeat: int, n_ok: int, fail) -> dict:
    return record(cid, repeat, [turn(checks(n_ok, fail=fail))])


def test_identical_candidate_passes():
    base, recs = base_and_records()
    report = compare(base, recs)
    assert report["passed"] is True and report["fails"] == [] and report["insufficient"] == []
    assert report["kind"] == "evalset-comparison" and report["condition_changes"] == []
    assert {v["verdict"] for v in report["cases"].values()} == {"pass"}
    assert report["latency"]["direct_fast"]["total_s"]["verdict"] == "pass"
    assert report["overall"]["verdict"] == "pass" and report["fast_thinking"]["verdict"] == "pass"


def test_case_regresses_only_when_more_than_one_repeat_worse():
    base, _ = base_and_records(cases=("EV01",), n_ok=50)
    two_of_three = [record("EV01", 1, [turn(checks(50))]), record("EV01", 2, [turn(checks(50))]),
                    failing_repeat("EV01", 3, 49, ["format"])]
    r = compare(base, two_of_three)
    assert r["cases"]["EV01"]["verdict"] == "pass" and r["passed"] is True       # 3/3 -> 2/3 is within one repeat
    one_of_three = [record("EV01", 1, [turn(checks(50))]), failing_repeat("EV01", 2, 49, ["format"]),
                    failing_repeat("EV01", 3, 49, ["format"])]
    r = compare(base, one_of_three)
    assert r["cases"]["EV01"]["pass_rate_verdict"] == "fail" and r["passed"] is False
    assert "case EV01: pass rate 1/3 < baseline 3/3 - 1/3" in r["fails"]


def test_case_floor_uses_the_smaller_repeat_count():
    """QA 4a: a 9-repeat baseline and a 3-repeat candidate with one flaky repeat."""
    base = frozen([record("EV01", r) for r in range(1, 10)])
    flaky = [record("EV01", 1, [turn(checks(0, fail=["max_chars"]))]), record("EV01", 2), record("EV01", 3)]
    r = compare(base, flaky)
    assert r["cases"]["EV01"]["verdict"] == "pass" and r["cases"]["EV01"]["floor"] == pytest.approx(2 / 3)
    two_bad = [record("EV01", 1, [turn(checks(0, fail=["max_chars"]))]),
               record("EV01", 2, [turn(checks(0, fail=["max_chars"]))]), record("EV01", 3)]
    assert compare(base, two_bad)["cases"]["EV01"]["verdict"] == "fail"


def test_a_case_that_never_passes_is_gated_per_check():
    """QA P7: EV09 can never pass (citation_passages); its other checks must still not decay."""
    def ev09(n_ok):
        return [record("EV09", r, [turn(checks(n_ok, fail=["citation_passages"]
                                               + ["required_sections"] * (9 - n_ok), thinking=False))],
                       effort="max") for r in (1, 2, 3)]

    base = frozen(ev09(9))
    assert base["quality"]["cases"]["EV09"]["pass_rate"] == 0
    assert compare(base, ev09(9))["passed"] is True
    r = compare(base, ev09(8))
    assert r["cases"]["EV09"]["pass_rate_verdict"] == "pass" and r["cases"]["EV09"]["verdict"] == "fail"
    assert r["cases"]["EV09"]["check_fails"] == [{"check": "c08", "rule": "passes", "baseline": "3/3",
                                                  "candidate": "0/3"}]
    assert "case EV09 check c08: 0/3 records pass < baseline 3/3 - 1/3" in r["fails"]
    # an errored record fails every check of its case; one of three is within one repeat
    crashed = ev09(9)
    crashed[0] = record("EV09", 1, turns=[], error="ReadTimeout: stalled", effort="max")
    assert compare(base, crashed)["cases"]["EV09"]["check_fails"] == []


def _made_a_file(cid: str, repeat: int, done: bool = True) -> dict:
    """A turn that made a file it should not have: `artifact` fails, and `job_completed` (the harness emits it only
    when a turn made a file) runs."""
    chk = checks(1, fail=["artifact"]) + [{"check": "job_completed", "dimension": "deliverable", "ok": done,
                                           "detail": ""}]
    return record(cid, repeat, [turn(chk)])


def _no_file(cid: str, repeat: int) -> dict:
    return record(cid, repeat, [turn(checks(1) + [{"check": "artifact", "dimension": "deliverable", "ok": True,
                                                    "detail": ""}])])


def test_a_conditional_check_is_gated_on_its_failure_rate():
    """Re-QA 2: a candidate that stopped making an unwanted file lost job_completed (0/3 pass) and failed."""
    base = frozen([_made_a_file("EV01", 1), _made_a_file("EV01", 2), _no_file("EV01", 3)])
    case = base["quality"]["cases"]["EV01"]
    assert case["check_runs"] == {"artifact": 3, "c00": 3, "job_completed": 2, "thinking_off": 3}
    assert B.conditional_checks(case) == ["job_completed"] and B.check_failures(case, "job_completed") == 0
    fixed = compare(base, [_no_file("EV01", r) for r in (1, 2, 3)])
    assert fixed["passed"] is True and fixed["cases"]["EV01"]["check_fails"] == []
    assert fixed["cases"]["EV01"]["conditional_checks"] == ["job_completed"]
    # failing in one more record than the baseline is within one repeat; in every record it is not
    one = compare(base, [_made_a_file("EV01", 1, done=False), _made_a_file("EV01", 2), _no_file("EV01", 3)])
    assert one["cases"]["EV01"]["check_fails"] == []
    broken = compare(base, [_made_a_file("EV01", r, done=False) for r in (1, 2, 3)])
    assert {"check": "job_completed", "rule": "failures", "baseline": "0/3", "candidate": "3/3"} \
        in broken["cases"]["EV01"]["check_fails"]
    assert ("case EV01 check job_completed (conditional: absent from some baseline records): 3/3 records fail it > "
            "baseline 0/3 + 1/3") in broken["fails"]
    # an errored record fails a conditional check too
    crashed = [record("EV01", r, turns=[], error="ReadTimeout: stalled") for r in (1, 2)] + [_no_file("EV01", 3)]
    entry = compare(base, crashed)["cases"]["EV01"]["check_fails"]
    assert {"check": "job_completed", "rule": "failures", "baseline": "0/3", "candidate": "2/3"} in entry


def test_a_check_the_baseline_ran_in_every_complete_record_fails_when_absent():
    base = frozen([_made_a_file("EV01", r) for r in (1, 2, 3)])          # job_completed in every record
    r = compare(base, [_no_file("EV01", r) for r in (1, 2, 3)])
    assert {"check": "job_completed", "rule": "passes", "baseline": "3/3", "candidate": "0/3"} \
        in r["cases"]["EV01"]["check_fails"]
    # an errored baseline record, or one without checks, does not make a check conditional
    recs = [_made_a_file("EV01", r) for r in (1, 2)] + [record("EV01", 3, turns=[], error="Boom")]
    assert B.conditional_checks(frozen(recs)["quality"]["cases"]["EV01"]) == []
    recs = [_made_a_file("EV09", r) for r in (1, 2)] + [record("EV09", 3, [turn(chk=[])], effort="max")]
    for rec in recs:
        rec["effort"] = "max"
    case = frozen(recs)["quality"]["cases"]["EV09"]
    assert case["no_checks"] == 1 and B.conditional_checks(case) == []


def test_overall_drop_of_exactly_one_twentieth_passes_and_more_fails():
    """QA P7: 16 cases x 3 repeats x 5 checks; k failed checks drop the mean by exactly k/240."""
    def full():
        return [record(cid, r, [turn(checks(4))], effort=EFFORT.get(cid, "fast"))
                for cid in B.WORKLOAD for r in (1, 2, 3)]

    base = frozen(full())
    for k, verdict in ((11, "pass"), (12, "pass"), (13, "fail")):
        cand = full()
        for i in range(k):
            rec = cand[i * 4 % 48]
            again = any(c["check"] == "x" for c in rec["turns"][0]["checks"])
            rec["turns"][0]["checks"] = checks(2, fail=["x", "y"]) if again else checks(3, fail=["x"])
        r = compare(base, cand)
        drop = Fraction(k, 240)
        assert r["overall"]["drop_exact"] == f"{drop.numerator}/{drop.denominator}"
        assert r["overall"]["verdict"] == verdict, k


def test_overall_drop_of_006_fails_and_004_passes():
    base, _ = base_and_records(cases=("EV01",), n_ok=49)                     # 50 checks a record
    full = [record("EV01", r, [turn(checks(49))]) for r in (1, 2)]
    drop6 = compare(base, full + [failing_repeat("EV01", 3, 40, ["f"] * 9)])   # (1 + 1 + 41/50) / 3
    assert drop6["overall"]["drop_exact"] == "3/50" and drop6["overall"]["verdict"] == "fail"
    assert drop6["cases"]["EV01"]["pass_rate_verdict"] == "pass"
    drop4 = compare(base, full + [failing_repeat("EV01", 3, 43, ["f"] * 6)])   # (1 + 1 + 44/50) / 3
    assert drop4["overall"]["drop_exact"] == "1/25" and drop4["overall"]["verdict"] == "pass"


def test_failed_turns_are_excluded_from_latency_and_counted():
    """QA 4h: a quick HTTP 503 must not become a fast latency sample."""
    def think(total, **res):
        return turn(timing=dict(FAST, first_answer_s=1.0, total_s=total), **res)

    base = frozen([record(c, r, [think(10.0)], effort="think") for c in ("EV06", "EV08") for r in (1, 2, 3)])
    cand = [record(c, r, [think(14.5)], effort="think") for c in ("EV06", "EV08") for r in (1, 2)]
    cand += [record(c, 3, [think(0.02, http=503)], effort="think", error="ChatFailed: HTTP 503")
             for c in ("EV06", "EV08")]
    r = compare(base, cand)
    e = r["latency"]["think"]["total_s"]
    assert (e["candidate_n"], e["candidate_excluded"], e["candidate_p50"]) == (4, 2, 14.5)
    assert e["ratio"] == pytest.approx(1.45) and e["verdict"] == "pass"      # 1.45 <= 1 + 0.20 + 4/10
    assert r["cases"]["EV06"]["candidate_failed_turns"] == "1/3"
    # each kind of failed turn is excluded
    for res in ({"http": 503}, {"timed_out": True}, {"errors": [{"error": "stream ended without done"}]},
                {"terminal": "error"}, {"terminal": None}):
        recs = three_repeats()
        recs[0]["turns"][0]["result"].update(res)
        e = latency(frozen(recs))
        assert (e["n"], e["excluded"]) == (2, 1), res
        assert frozen(recs)["quality"]["cases"]["EV01"]["failed_turns"] == 1
    ok_terminal = three_repeats()
    ok_terminal[0]["turns"][0]["result"]["terminal"] = "done"
    assert latency(frozen(ok_terminal))["excluded"] == 0


def test_failed_turn_rate_gate():
    base = frozen(three_repeats())
    one = three_repeats()
    one[0]["turns"][0]["result"]["errors"] = [{"error": "stream ended without done"}]
    r = compare(base, one)
    assert r["cases"]["EV01"]["failed_turns_verdict"] == "pass"            # 1/3 is not > 0 + 1/3
    two = three_repeats()
    for rec in two[:2]:
        rec["turns"][0]["result"]["terminal"] = "error"
    r = compare(base, two)
    assert r["cases"]["EV01"]["failed_turns_verdict"] == "fail" and r["passed"] is False
    assert "case EV01: failed turns 2/3 > baseline 0/3 + 1/3" in r["fails"]


def test_latency_compares_only_the_baselines_units():
    """QA 4g: new fast cases in the same class must not mask a regression of the old ones."""
    base = frozen(three_repeats())
    slow = timed("EV01", (1.9, 1.9, 1.9))
    alone = compare(base, slow)
    assert alone["latency"]["direct_fast"]["total_s"]["verdict"] == "fail"
    masked = compare(base, slow + [rec for c in ("EV02", "EV03", "RQ01") for rec in timed(c, (0.3,) * 3)])
    assert masked["new_cases"] == ["EV02", "EV03", "RQ01"]
    entry = masked["latency"]["direct_fast"]["total_s"]
    assert entry["candidate_n"] == 3 and entry["verdict"] == "fail" and masked["passed"] is False
    two_turns = [record("EV01", r, [turn(), turn()]) for r in (1, 2, 3)]
    assert compare(base, two_turns)["new_units"] == ["EV01/t2"]


def test_a_missing_case_fails():
    base, recs = base_and_records()
    r = compare(base, [x for x in recs if x["id"] == "EV01"])
    assert r["missing_cases"] == ["RQ01"] and r["cases"]["RQ01"]["verdict"] == "fail"
    assert r["passed"] is False and "case RQ01: missing from the candidate run(s)" in r["fails"]
    assert r["overall"]["cases_compared"] == 1


def test_fast_thinking_leak_compares_rates():
    leak = [record("EV01", 1, [turn(checks(1, fail=["thinking_off"]))])]
    base = frozen(leak + [record("EV01", r) for r in range(2, 10)])          # 1 / 9
    r = compare(base, [record("EV01", 1, [turn(checks(1, fail=["thinking_off"]))]), record("EV01", 2),
                       record("EV01", 3)])                                   # 1 / 3 > 1 / 9
    assert r["fast_thinking"]["verdict"] == "fail" and r["fast_thinking"]["candidate"] == "1/3"
    assert any(f.startswith("fast thinking leak") for f in r["fails"])
    lower = [record("EV01", r, [turn(checks(1, fail=["thinking_off"]))]) for r in (1, 2)]
    lower += [record("EV01", r) for r in range(3, 19)]                       # 2 / 18 = the baseline's rate
    assert compare(base, lower)["fast_thinking"]["verdict"] == "pass"
    clean = frozen(three_repeats())                                          # baseline 0: any leak fails
    one = three_repeats()
    one[0]["turns"][0]["checks"] = checks(1, fail=["thinking_off"])
    assert compare(clean, one)["fast_thinking"]["verdict"] == "fail"


def test_a_fast_turn_without_the_thinking_check_fails():
    base = frozen(three_repeats())
    cand = three_repeats()
    for rec in cand:
        rec["turns"][0]["checks"] = checks(1, thinking=False)
    r = compare(base, cand)
    assert r["fast_thinking"]["candidate_unchecked_turns"] == 3 and r["fast_thinking"]["verdict"] == "fail"
    assert r["passed"] is False and any("carry no `thinking_off` check" in f for f in r["fails"])


def test_a_candidate_on_a_different_eval_set_or_workers_is_refused():
    base = frozen(three_repeats())
    with pytest.raises(B.BaselineError, match="eval_set_sha256"):
        compare(base, three_repeats(), cond={"eval_set_sha256": "f" * 64})
    with pytest.raises(B.BaselineError, match="workers"):
        compare(base, three_repeats(), cond={"workers": 2})


def test_model_label_and_commit_changes_are_listed_not_blocking():
    def with_model(recs, model):
        for rec in recs:
            rec["turns"][0]["result"]["versions"] = {"model": model}
        return recs

    base = frozen(with_model(three_repeats(), "model-a"), label="dev stack, cap 2")
    r = compare(base, with_model(three_repeats(), "model-b"),
                cond={"label": "dev stack, cap 4", "harness_commit": "fedcba9"})
    assert r["passed"] is True
    changes = {c["field"]: c for c in r["condition_changes"]}
    assert changes["model_ids"]["baseline"] == ["model-a"] and changes["model_ids"]["candidate"] == ["model-b"]
    assert changes["model_ids"]["baseline_labels"] == ["dev stack, cap 2"]
    assert changes["model_ids"]["candidate_labels"] == ["dev stack, cap 4"]
    assert changes["labels"]["candidate"] == ["dev stack, cap 4"]
    assert (changes["harness_commits"]["baseline"], changes["harness_commits"]["candidate"]) == (["0123abc"],
                                                                                                 ["fedcba9"])
    text = B._summary(r)
    assert "baseline labels: 'dev stack, cap 2'" in text and "candidate labels: 'dev stack, cap 4'" in text
    assert "CHANGED model_ids: baseline ['model-a'] -> candidate ['model-b']" in text


def test_candidate_side_insufficiency_always_fails():
    """QA 4j: --allow-insufficient waived a class where the candidate had no samples at all."""
    base, _ = base_and_records(cases=("EV01",))
    thin = three_repeats()
    for rec in thin:
        rec["turns"][0]["result"]["timing"]["first_answer_s"] = None
    r = compare(base, thin, allow_insufficient=True)
    entry = r["latency"]["direct_fast"]["first_answer_s"]
    assert entry["verdict"] == "insufficient_samples" and entry["insufficient_side"] == "candidate"
    assert r["insufficient"] == [] and r["passed"] is False
    assert any(f.startswith("latency direct_fast/first_answer_s: the candidate has 0 sample") for f in r["fails"])


def test_baseline_side_insufficiency_fails_unless_allowed():
    recs = [record("EV09", r, effort="max") for r in (1, 2, 3)]
    for rec in recs[1:]:
        rec["turns"][0]["result"]["timing"]["total_s"] = None
    base = frozen(recs)
    cand = [record("EV09", r, effort="max") for r in (1, 2, 3)]
    r = compare(base, cand)
    assert r["latency"]["max"]["total_s"]["insufficient_side"] == "baseline"
    assert r["insufficient"] == [{"workload": "max", "metric": "total_s", "baseline_n": 1, "noise_units": 0,
                                  "candidate_n": 3}]
    assert r["fails"] == [] and r["passed"] is False
    assert compare(base, cand, allow_insufficient=True)["passed"] is True


# -------------------------------------------------------------- integrity --

@pytest.mark.parametrize("mutate", [
    lambda d: d["latency"]["direct_fast"]["total_s"].update(allowed_ratio=9.0),
    lambda d: d["latency"]["direct_fast"]["total_s"].update(allowed_ratio="1.25"),
    lambda d: d["latency"]["direct_fast"]["total_s"].update(n=4),
    lambda d: d["latency"]["direct_fast"]["total_s"]["units"]["EV01/t1"]["samples"].append(1.0),
    lambda d: d["latency"]["direct_fast"]["total_s"]["units"]["EV01/t1"].update(median=0.5),
    lambda d: d["quality"]["cases"]["EV01"].update(passes=-5),
    lambda d: d["quality"]["cases"]["EV01"].update(passes=4),
    lambda d: d["quality"]["cases"]["EV01"].update(mean_score_exact="1/2"),
    lambda d: d["quality"]["cases"]["EV01"]["check_passes"].update(c00=7),
    lambda d: d["quality"].update(fast_thinking_turns=99),
    lambda d: d["repeats_per_case"].update(EV01=2),
    lambda d: d["sources"][0].update(repeats=4),
    lambda d: d["sources"][0].update(label="moved to box.internal"),
    lambda d: d["conditions"].update(workers=2),
    lambda d: d["constants"].update(rel_floor=0.5),
    lambda d: d["latency"].pop("direct_fast"),
    lambda d: d["quality"]["checks"]["c00"].update(passed=9),
    lambda d: d.update(schema=1),
    lambda d: d.update(schema=2),
])
def test_compare_refuses_a_baseline_whose_numbers_disagree(mutate):
    base = json.loads(json.dumps(frozen(three_repeats())))
    B.compare(base, pooled(three_repeats()))                                 # the untouched copy is accepted
    mutate(base)
    with pytest.raises(B.BaselineError):
        B.compare(base, pooled(three_repeats()))


# --------------------------------------------------------------- markdown --

def test_render_markdown_has_every_table_and_a_workload_row(tmp_path):
    recs = three_repeats() + [record("EV08", r, [turn(checks(1, fail=["required_sections"]))], effort="think")
                              for r in (1, 2, 3)]
    doc = B.freeze(B.load_runs([write_run(tmp_path, "run1", recs, repeats=3)]), frozen_at=FROZEN_AT)
    md = B.render_markdown(doc)
    assert ("| Case | Workload | Effort | Repeats | Pass rate | Mean score | Failed turns | "
            "Failing checks (most often) |") in md
    assert ("| Class | Metric | Gated | n | Units | Missing | Excluded | p50 | p95 | min | max | Sigma (ln) | "
            "Sigma from | Scale | Allowed ratio | Allowed p95 |") in md
    assert "| Check | Passed | Total | Rate |" in md and "### Output tokens per second" in md
    assert "### Conditions" in md and 'label "synthetic stack, cap 2"' in md and "`0123abc`" in md
    assert f"Eval set sha256 `{SHA}`" in md and "account 0 other conversations, 0 saved facts" in md
    assert "run `run1`" in md and str(tmp_path) not in md                     # the run's name, not the path
    assert ("| direct_fast | total_s | yes | 3 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 | 1.00 | 0.000 | class | 1.00 | "
            "1.700 | reported (n < 20) |") in md
    assert "| think | first_event_s | no | 3 |" in md and "| direct_fast | first_token_s | no | 3 |" in md
    assert "| EV08 | think | think | 3 | 0.000 | 0.667 | 0/3 | required_sections x3 |" in md
    assert "| EV01 | direct_fast | fast | 3 | 1.000 | 1.000 | 0/3 | none |" in md
    assert "| required_sections | 0 | 3 | 0.000 |" in md
    assert "### Method" in md and "re-run the BASELINE commit" in md


def test_render_markdown_escapes_pipes_in_labels():
    assert 'label "a \\| b"' in B.render_markdown(frozen(three_repeats(), label="a | b"))


# -------------------------------------------------------------------- CLI --

def _ids(records: list, tag: str) -> list:
    for rec in records:
        rec["conversation_id"] += f"-{tag}"
    return records


def test_cli_freeze_compare_show_exit_codes(tmp_path, capsys):
    dirs = [write_run(tmp_path, f"base{i}", _ids([record("EV01", 1), record("RQ01", 1)], f"b{i}")) for i in (1, 2, 3)]
    out, md = tmp_path / "baseline.json", tmp_path / "baseline.md"
    assert B.main(["freeze", *dirs, "--out", str(out), "--markdown", str(md)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "evalset-baseline" and doc["repeats_per_case"] == {"EV01": 3, "RQ01": 3}
    assert "| Case | Workload |" in md.read_text(encoding="utf-8")
    assert B.main(["compare", str(out), *dirs]) == 0
    printed = capsys.readouterr().out
    assert "compare: PASSED" in printed and "baseline labels: 'synthetic stack, cap 2'" in printed

    worse = [write_run(tmp_path, f"cand{i}", _ids([record("EV01", 1, [turn(checks(1, fail=["format"]))]),
                                                   record("RQ01", 1)], f"c{i}")) for i in (1, 2, 3)]
    report = tmp_path / "report.json"
    assert B.main(["compare", str(out), *worse, "--out", str(report)]) == 1
    printed = capsys.readouterr().out
    assert "compare: NOT PASSED" in printed and "FAIL case EV01" in printed
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is False

    thin = [write_run(tmp_path, "thin", [record("EV01", 1), record("RQ01", 1)])]   # 2 samples per metric
    assert B.main(["compare", str(out), *thin]) == 1
    assert "FAIL latency direct_fast/total_s: the candidate has 2 sample(s)" in capsys.readouterr().out
    assert B.main(["compare", str(out), *thin, "--allow-insufficient"]) == 1      # candidate side: never waived
    capsys.readouterr()

    assert B.main(["show", str(out)]) == 0
    assert "| Class | Metric |" in capsys.readouterr().out


def test_cli_mixed_labels_and_bad_input_exit_2(tmp_path, capsys):
    a = write_run(tmp_path, "a", three_repeats(), label="one", repeats=3)
    b = write_run(tmp_path, "b", _ids(three_repeats(), "b"), label="two", repeats=3)
    out = tmp_path / "b.json"
    assert B.main(["freeze", a, b, "--out", str(out)]) == 2
    assert "--allow-mixed-conditions" in capsys.readouterr().err and not out.exists()
    assert B.main(["freeze", a, b, "--out", str(out), "--allow-mixed-conditions"]) == 0
    assert B.main(["show", a + "/results.json"]) == 2
    assert "only kind 'evalset-baseline'" in capsys.readouterr().err
    assert B.main(["compare", str(tmp_path), a]) == 2                          # a directory as the baseline
    bad = write_run(tmp_path, "bad", [dict(record("EV01"), workload=["direct_fast"])])
    assert B.main(["freeze", bad, "--out", str(tmp_path / "x.json")]) == 2


def test_cli_compare_refuses_an_incomparable_candidate_with_exit_2(tmp_path, capsys):
    base_dir = write_run(tmp_path, "base", three_repeats(), repeats=3)
    out = tmp_path / "baseline.json"
    assert B.main(["freeze", base_dir, "--out", str(out)]) == 0
    other = write_run(tmp_path, "other", three_repeats(), repeats=3, cond={"eval_set_sha256": "f" * 64})
    assert B.main(["compare", str(out), other]) == 2
    assert "eval_set_sha256" in capsys.readouterr().err
    tampered = json.loads(out.read_text())
    tampered["latency"]["direct_fast"]["total_s"]["allowed_ratio"] = 1e9
    (tmp_path / "tampered.json").write_text(json.dumps(tampered))
    assert B.main(["compare", str(tmp_path / "tampered.json"), base_dir]) == 2
    assert "disagree" in capsys.readouterr().err


def test_cli_maps_an_internal_error_to_exit_2(tmp_path, monkeypatch, capsys):
    d = write_run(tmp_path, "r", three_repeats(), repeats=3)

    def boom(*_a, **_k):
        raise TypeError("synthetic")

    monkeypatch.setattr(B, "quality_stats", boom)
    assert B.main(["freeze", d, "--out", str(tmp_path / "x.json")]) == 2
    assert "TypeError: synthetic" in capsys.readouterr().err


def test_cli_refuses_an_output_path_that_is_an_input(tmp_path, capsys):
    victim = write_run(tmp_path, "victim", three_repeats(), repeats=3)
    results = Path(victim, "results.json")
    before = results.read_text()
    assert B.main(["freeze", victim, "--out", str(results)]) == 2
    assert results.read_text() == before and "one of the inputs" in capsys.readouterr().err
    out = tmp_path / "baseline.json"
    assert B.main(["freeze", victim, "--out", str(out), "--markdown", str(out)]) == 2
    assert not out.exists()
    assert B.main(["freeze", victim, "--out", str(out), "--markdown", str(Path(victim, "summary.json"))]) == 2
    assert B.main(["freeze", victim, "--out", str(out)]) == 0
    frozen_text = out.read_text()
    assert B.main(["compare", str(out), victim, "--out", str(out)]) == 2
    assert out.read_text() == frozen_text
    assert B.main(["compare", str(out), victim, "--out", str(results)]) == 2 and results.read_text() == before


def test_cli_runs_as_a_script(tmp_path):
    dirs = [write_run(tmp_path, f"r{i}", _ids([record("EV01", 1)], f"r{i}")) for i in (1, 2, 3)]
    out = tmp_path / "baseline.json"
    script = str(AIQ / "baseline.py")
    run = subprocess.run([sys.executable, script, "freeze", *dirs, "--out", str(out)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    shown = subprocess.run([sys.executable, script, "show", str(out)], capture_output=True, text=True, timeout=60)
    assert shown.returncode == 0 and "| direct_fast | total_s | yes | 3 |" in shown.stdout
