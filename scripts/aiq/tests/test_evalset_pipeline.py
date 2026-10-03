"""B-04: the runner's results.json feeds baseline.py unchanged.

The two halves were built against one written contract (schema 1). This
proves the contract holds end to end: three repeats of the whole set through
run_evalset.py against the loopback fake, frozen by baseline.py, then the same
runs compared with their own baseline (must pass) and a run with one case
broken (must fail on that case). No network beyond 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AIQ = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for p in (str(AIQ), str(HERE)):
    if p not in sys.path:
        sys.path.append(p)

import baseline as B  # noqa: E402
import eval_set as ES  # noqa: E402
import eval_set_answers as EA  # noqa: E402
import run_evalset as RE  # noqa: E402
from test_run_evalset import EMAIL, fake, nothing_lands_under_runs, pwfile  # noqa: E402,F401


def _sandbox_ok(spec, answer, workdir, tc):
    return {"lang": "python", "blocks": 1, "source": answer, "ok": True,
            "steps": [{"stage": "build", "name": "compile", "ok": True, "rc": 0, "tail": ""},
                      {"stage": "check", "name": "check.py", "ok": True, "rc": 0, "tail": "ok"}]}


def _runs(fake, tmp_path, pwfile, name, repeats=3, label="fake loopback orchestrator"):
    out = tmp_path / name
    argv = ["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(out),
            "--code-root", str(tmp_path / "code-root"), "--repeats", str(repeats), "--label", label]
    assert RE.main(argv) == 0
    return str(out)


def test_three_repeats_of_the_whole_set_freeze_and_compare(fake, tmp_path, pwfile, monkeypatch):
    monkeypatch.setattr(RE.code_sandbox, "run_code", _sandbox_ok)
    run_dir = _runs(fake, tmp_path, pwfile, "base")
    results = json.loads(Path(run_dir, "results.json").read_text())
    assert len(results["cases"]) == 3 * len(ES.EVAL_SET_CASES)

    records = B.load_runs([run_dir])
    frozen = B.freeze(records)
    assert set(frozen["quality"]["cases"]) == set(ES.BY_ID)
    assert all(v["repeats"] == 3 for v in frozen["quality"]["cases"].values())
    # every class the set covers has samples; each has at least three (one case x three repeats)
    for workload in ("direct_fast", "evidence_fast", "live_search_fast", "long_context", "think", "max"):
        metric = frozen["latency"][workload]["first_answer_s"]
        assert metric["n"] >= 3 and metric["status"] == "ok" and metric["allowed_ratio"] is not None, (workload, metric)
        # below 20 samples the nearest-rank p95 is the maximum: reported, never gated
        assert metric["p95_status"] == ("gated" if metric["n"] >= 20 else "reported"), (workload, metric)
    # EV09 cannot pass: no endpoint exposes the passages a run read (source_passages_captured false)
    assert frozen["quality"]["cases"]["EV09"]["pass_rate"] == 0
    assert frozen["quality"]["checks"]["citation_passages"]["rate"] == 0

    path = tmp_path / "baseline.json"
    assert B.main(["freeze", run_dir, "--out", str(path), "--markdown", str(tmp_path / "b.md")]) == 0
    assert B.main(["compare", str(path), run_dir]) == 0
    same = B.compare(B.load_baseline(str(path)), B.load_runs([run_dir]))
    assert same["passed"] is True and same["fails"] == [], same["fails"]
    md = (tmp_path / "b.md").read_text()
    assert "direct_fast" in md and "first_answer_s" in md
    assert fake.base not in md and fake.base not in path.read_text(), "a baseline must not carry the endpoint"

    # a candidate whose RQ03 answer is now a sentence instead of names only
    bad = next(b for b in EA.BAD["RQ03"] if b["label"] == "sentence")
    fake.scripts["RQ03"] = {"answer": bad["answer"], "sources": []}
    cand = _runs(fake, tmp_path, pwfile, "cand")
    report = B.compare(B.load_baseline(str(path)), B.load_runs([cand]))
    assert report["passed"] is False
    assert B.main(["compare", str(path), cand]) == 1
    # exactly one failure reason family: RQ03, whose names_only check failed on every repeat
    assert report["fails"] and all("RQ03" in f for f in report["fails"]), report["fails"]
    assert any("names_only" in f for f in report["fails"]), report["fails"]
    assert os.path.isdir(run_dir)
