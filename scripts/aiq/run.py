#!/usr/bin/env python3
"""QA runner — "like ChatGPT", measured.

  # the whole thing, in one command (stack up, seed, run, gate, tear down):
  scripts/aiq/aiq-stack.sh all

  # or by hand:
  # 1. an isolated orchestrator (never production): aiq-stack.sh up && aiq-stack.sh seed aiq-eval
  # 2. run (credentials from the environment or the stack's scratch files)
  PY=/home/techsphere/Documents/project/personal-LLM-Chabot/orchestrator/.venv/bin/python
  $PY run.py --base http://127.0.0.1:8082 --container techsara-e2e-aiq-orchestrator \
             [--only F01,P02] [--category csv_plot] [--workers 2] [--out runs/<stamp>] \
             [--gate --baseline runs/worker-baseline-1]
  # 3. re-score a finished run without calling the model again (after editing checks or cases)
  $PY run.py --rescore runs/<stamp>
  # 4. the dev-to-main gate over a finished run, against the worker baseline
  $PY run.py --rescore runs/<stamp> --gate --baseline runs/worker-baseline-1

Writes <out>/results.json (every turn: answer, meta, markdown metrics, file
metrics, chart spec summary, hue profile, the sandbox's build and check steps
for a coding case, and the checks), <out>/summary.json (scores per case, per
category, per dimension, and the headline numbers) and, with --gate,
<out>/gate.json. Downloaded files are kept under <out>/files/<case>/ and the
code the model wrote under <out>/code/<case>/.

--gate exits non-zero when the gate fails, so CI or a shell can branch on it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import code_sandbox  # noqa: E402
import harness as H  # noqa: E402
import make_fixtures  # noqa: E402
from cases import CASES, FAIL_TEXT  # noqa: E402
from gate import gate_report, print_gate  # noqa: E402


def _cred(args):
    email = args.email or os.environ.get("AIQ_EMAIL") or "aiq-eval@test.local"
    pw = os.environ.get("AIQ_PASSWORD")
    if not pw:
        p = os.path.join(HERE, ".runtime", "user.password")
        pw = open(p).read().strip() if os.path.exists(p) else None
    if not pw:
        raise SystemExit("set AIQ_PASSWORD (or run stack/aiq-stack.sh seed)")
    return email, pw


def _trace_thinks(trace):
    if not trace:
        return None
    blob = json.dumps(trace)
    if '"ADAPTIVE_THINKING"' not in blob:
        return False
    for ev in trace.get("events") or trace.get("timeline") or []:
        if isinstance(ev, dict) and "ADAPTIVE_THINKING" in json.dumps(ev):
            if '"think": true' in json.dumps(ev):
                return True
    return '"think": true' in blob


def run_case(client_args, case, out_dir, toolchain=None):
    client = H.Client(*client_args)
    conv = f"aiq-{case['id']}-{int(time.time() * 1000)}"
    upload_path = os.path.join(HERE, "fixtures", case["upload"]) if case.get("upload") else None
    rec = {"id": case["id"], "category": case["category"], "effort": case["effort"], "conversation_id": conv,
           "upload": case.get("upload"), "note": case.get("note", ""), "turns": []}
    try:
        if upload_path:
            rec["upload_result"] = {k: v for k, v in client.upload(conv, upload_path).items() if k in ("upload_id", "filename", "bytes", "files", "notes")}
        history = []
        prev_file = None
        for ti, t in enumerate(case["turns"]):
            res = client.chat(conv, t["message"], history, case["effort"], web_search=case.get("web_search", "off"))
            meta = res.get("meta") or {}
            res["trace"] = client.trace(meta.get("trace_id") or "")
            res["trace_thinks"] = _trace_thinks(res["trace"])
            res["md"] = H.md_metrics(res.get("answer") or "")
            refs = meta.get("artifacts") or []
            art = None
            if refs:
                ref = client.wait_job(refs[0])
                files = []
                fdir = os.path.join(out_dir, "files", case["id"], f"t{ti + 1}")
                os.makedirs(fdir, exist_ok=True)
                for f in ref.get("files") or []:
                    data = client.download(ref, f)
                    if data is None:
                        files.append({"format": f.get("format"), "error": "download failed"})
                        continue
                    with open(os.path.join(fdir, f.get("filename") or f"file.{f.get('format')}"), "wb") as fh:
                        fh.write(data)
                    files.append(H.inspect_file(f.get("format"), data))
                spec = client.spec(ref)
                charts = H.find_charts(spec) if spec is not None else None
                spec_blocks = H.block_counts(spec) if spec is not None else None
                block_seq = H.block_sequence(spec) if spec is not None else None
                hues, hues_soft = [], []
                for f in files:
                    for img in f.pop("images", []) or []:
                        try:
                            hues.append(H.hue_profile(img))
                            # the status tokens can be pale tints, which the
                            # default saturation floor would not see at all
                            hues_soft.append(H.hue_profile(img, min_saturation=0.18))
                        except Exception as exc:  # noqa: BLE001
                            hues.append({"hues": 0, "dominant": None, "error": str(exc)})
                art = {"ref": ref, "files": files, "charts": charts, "hues": hues, "hues_soft": hues_soft,
                       "all_refs": len(refs), "spec_blocks": spec_blocks, "block_seq": block_seq}
            res["artifact"] = art
            if t["expect"].get("code"):
                workdir = os.path.join(out_dir, "code", case["id"], f"t{ti + 1}")
                res["code_result"] = code_sandbox.run_code(t["expect"]["code"], res.get("answer") or "",
                                                           workdir, toolchain)
            checks = H.check_turn(t["expect"], res, effort=case["effort"], fail_text=FAIL_TEXT,
                                  prev_file=prev_file, upload_path=upload_path)
            if art:
                prev_file = art
            rec["turns"].append({"message": t["message"][:300] + ("…" if len(t["message"]) > 300 else ""),
                                 "expect": t["expect"], "result": _slim(res), "checks": checks})
            history += [{"role": "user", "content": t["message"]}, {"role": "assistant", "content": res.get("answer") or ""}]
            print(f"  {case['id']} t{ti + 1}: {sum(c['ok'] for c in checks)}/{len(checks)} in {res.get('seconds')}s "
                  f"route={meta.get('route')} {'FAILED: ' + ', '.join(c['check'] for c in checks if not c['ok']) if not all(c['ok'] for c in checks) else ''}",
                  flush=True)
    except Exception as exc:  # noqa: BLE001 — a crashed case scores 0, with the reason
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()[-2000:]
        print(f"  {case['id']} ERROR {rec['error']}", flush=True)
    return rec


def _slim(res):
    out = {k: v for k, v in res.items() if k not in ("trace",)}
    art = out.get("artifact")
    if art:
        art = dict(art)
        art["files"] = [{k: (v[:4000] if k == "text" else v) for k, v in f.items()} for f in art["files"]]
        if art.get("charts") is not None:
            art["charts"] = [{"type": c.get("type"), "title": c.get("title"), "categories": (c.get("categories") or [])[:30],
                              "series": [{"name": s.get("name"), "values": (s.get("values") or [])[:30], "color": s.get("color")}
                                         for s in (c.get("series") or [])[:6]],
                              "data": c.get("data"), "provenance": c.get("provenance"), "style": c.get("style")}
                             for c in art["charts"]]
        out["artifact"] = art
    if res.get("trace"):
        evs = res["trace"].get("events") or []
        out["trace_events"] = [e.get("stage") or e.get("event") or e.get("name") for e in evs if isinstance(e, dict)][:80]
    return out


def rescore(results, container=None):
    """Re-run the checks over stored answers. With a container, also backfill
    spec block counts (headings/tables) for artifacts that lack them."""
    by_id = {c["id"]: c for c in CASES}
    if container:
        import subprocess
        for rec in results["cases"]:
            for tr in rec.get("turns", []):
                art = tr["result"].get("artifact")
                if art and art.get("spec_blocks") is None and art["ref"].get("status", "").startswith("completed"):
                    uid = results.get("user_id") or 1
                    path = f"/reports/artifacts/{uid}/{art['ref']['artifact_id']}/v{art['ref']['version']}/spec.json"
                    p = subprocess.run(["docker", "exec", container, "cat", path], capture_output=True, timeout=30)
                    if p.returncode == 0:
                        spec = json.loads(p.stdout)
                        art["spec_blocks"] = H.block_counts(spec)
                        art["block_seq"] = H.block_sequence(spec)
    for rec in results["cases"]:
        case = by_id.get(rec["id"])
        if not case or rec.get("error"):
            continue
        upload_path = os.path.join(HERE, "fixtures", case["upload"]) if case.get("upload") else None
        prev_file = None
        for t, tr in zip(case["turns"], rec["turns"]):
            res = tr["result"]
            res["md"] = H.md_metrics(res.get("answer") or "")
            tr["expect"] = t["expect"]
            tr["checks"] = H.check_turn(t["expect"], res, effort=case["effort"], fail_text=FAIL_TEXT,
                                        prev_file=prev_file, upload_path=upload_path)
            if res.get("artifact"):
                prev_file = res["artifact"]
    return results


#: the sentence the owner saw, and the chart warning the dataset conversations
#: carried. The gate requires zero of each, so they are counted over the whole
#: run record — the answer, the step details and the job's warnings.
NO_CHARTS_SENTENCE = "no charts to draw"
TABLE_NOT_AVAILABLE = re.compile(r"the table\s+'?[^'\n]{0,60}'?\s+is not available", re.I)


def _gate_counts(results):
    """The four counts the gate requires to be zero, each with where it happened."""
    no_charts, table_na, false_claims = [], [], []
    for rec in results["cases"]:
        prev_file = None
        for ti, tr in enumerate(rec.get("turns", []), start=1):
            res = tr["result"]
            art = res.get("artifact")
            where = f"{rec['id']} t{ti}" + (f" job {art['ref'].get('job_id', '')[:8]}" if art else "")
            blob = json.dumps(tr, default=str)
            if NO_CHARTS_SENTENCE in blob.lower():
                no_charts.append(where)
            if rec.get("upload") and TABLE_NOT_AVAILABLE.search(blob):
                table_na.append(where)
            if prev_file is not None and H.claims_chart_added(res.get("answer") or ""):
                if H.chart_count(art) <= H.chart_count(prev_file):
                    false_claims.append(where)
            if art:
                prev_file = art
    return no_charts, table_na, false_claims


def summarise(results):
    cases = []
    dims = defaultdict(lambda: [0, 0])
    checks_by_name = defaultdict(lambda: [0, 0])
    for rec in results["cases"]:
        checks = [c for t in rec.get("turns", []) for c in t["checks"]]
        n_turns_expected = len(next(c for c in CASES if c["id"] == rec["id"])["turns"])
        if rec.get("error") or len(rec.get("turns", [])) < n_turns_expected:
            checks = checks + [{"check": "case_completed", "dimension": "deliverable", "ok": False}]
        passed = sum(c["ok"] for c in checks)
        score = passed / len(checks) if checks else 0.0
        for c in checks:
            dims[c["dimension"]][0] += c["ok"]; dims[c["dimension"]][1] += 1
            checks_by_name[c["check"]][0] += c["ok"]; checks_by_name[c["check"]][1] += 1
        cases.append({"id": rec["id"], "category": rec["category"], "score": round(score, 3), "all_pass": passed == len(checks),
                      "failed": [c["check"] for c in checks if not c["ok"]]})
    cats = defaultdict(list)
    for c in cases:
        cats[c["category"]].append(c)
    turns = [t for rec in results["cases"] for t in rec.get("turns", [])]
    fast_turns = [t for rec in results["cases"] if rec["effort"] == "fast" for t in rec.get("turns", [])]
    thinking = [t for t in fast_turns if any(c["check"] == "thinking_off" and not c["ok"] for c in t["checks"])]
    docs = [t["result"]["artifact"] for t in turns if t["result"].get("artifact")]
    doc_pages = [max([f.get("pages") or 0 for f in d["files"]] + [int(d["ref"].get("preview_pages") or 0)]) for d in docs]
    hue_imgs = [h for d in docs for h in d.get("hues") or []]
    headline = {
        "cases": len(cases),
        "overall_score": round(sum(c["score"] for c in cases) / len(cases), 3) if cases else 0,
        "cases_all_pass": sum(c["all_pass"] for c in cases),
        "fast_turns": len(fast_turns),
        "fast_turns_thinking": len(thinking),
        "fast_thinking_case_ids": sorted({rec["id"] for rec in results["cases"] for t in rec.get("turns", []) if t in thinking}),
        "file_turns": len(docs),
        "doc_pages_median": sorted(doc_pages)[len(doc_pages) // 2] if doc_pages else None,
        "doc_pages": doc_pages,
        "chart_images": len(hue_imgs),
        "chart_images_single_hue": sum(1 for h in hue_imgs if h["hues"] <= 1),
        "chart_dominant_hues": sorted({h["dominant"] for h in hue_imgs if h.get("dominant") is not None}),
        "chat_turns_with_markdown_structure": sum(1 for t in turns if not t["result"].get("artifact") and
                                                   (t["result"]["md"]["headings"] + t["result"]["md"]["bullets"] + t["result"]["md"]["numbered"] + t["result"]["md"]["table_max_rows"]) > 0),
        "chat_turns": sum(1 for t in turns if not t["result"].get("artifact")),
    }
    no_charts, table_na, false_claims = _gate_counts(results)
    code_turns = [t for t in turns if t["result"].get("code_result")]
    headline.update({
        "no_charts_to_draw": len(no_charts), "no_charts_to_draw_where": no_charts,
        "table_not_available": len(table_na), "table_not_available_where": table_na,
        "false_chart_claims": len(false_claims), "false_chart_claims_where": false_claims,
        "code_turns": len(code_turns),
        "code_turns_unavailable": [t["result"]["code_result"].get("unavailable") for t in code_turns
                                   if t["result"]["code_result"].get("unavailable")],
        "code_turns_built": sum(1 for t in code_turns
                                if all(s["ok"] for s in t["result"]["code_result"].get("steps") or [{"ok": False}])),
    })
    by_category = {k: {"cases": len(v), "mean_score": round(sum(c["score"] for c in v) / len(v), 3),
                       "all_pass": sum(c["all_pass"] for c in v)} for k, v in sorted(cats.items())}
    # P06 is scored separately (the plan): it asks for a chart without asking
    # for a file, so which route it takes is a judgement call, not a defect.
    plot = [c for c in cats.get("csv_plot", []) if c["id"] != "P06"]
    if plot:
        by_category["csv_plot_without_p06"] = {
            "cases": len(plot), "mean_score": round(sum(c["score"] for c in plot) / len(plot), 3),
            "all_pass": sum(c["all_pass"] for c in plot)}
    return {
        "headline": headline,
        "job_ids": {rec["id"]: [t["result"]["artifact"]["ref"].get("job_id", "") for t in rec.get("turns", [])
                                if t["result"].get("artifact")] for rec in results["cases"]},
        "by_category": by_category,
        "by_dimension": {k: {"passed": p, "total": n, "rate": round(p / n, 3)} for k, (p, n) in sorted(dims.items())},
        "by_check": {k: {"passed": p, "total": n, "rate": round(p / n, 3)} for k, (p, n) in sorted(checks_by_name.items())},
        "cases": cases,
    }


def print_summary(s):
    h = s["headline"]
    print(f"\nOVERALL {h['overall_score']:.3f}  ({h['cases_all_pass']}/{h['cases']} cases pass every check)")
    print(f"Fast turns that thought: {h['fast_turns_thinking']}/{h['fast_turns']} {h['fast_thinking_case_ids']}")
    print(f"file turns {h['file_turns']}, pages median {h['doc_pages_median']} {h['doc_pages']}")
    print(f"chart images {h['chart_images']}, single-hue {h['chart_images_single_hue']}, dominant hues {h['chart_dominant_hues']}")
    print(f"chat answers with any markdown structure: {h['chat_turns_with_markdown_structure']}/{h['chat_turns']}")
    if h.get("code_turns"):
        print(f"coding turns {h['code_turns']}, built and checked clean {h['code_turns_built']}"
              + (f", toolchain missing: {sorted(set(h['code_turns_unavailable']))}" if h.get("code_turns_unavailable") else ""))
    for key, what in (("no_charts_to_draw", "'no charts to draw'"), ("table_not_available", "'the table … is not available'"),
                      ("false_chart_claims", "a chart claimed but not added")):
        if h.get(key):
            print(f"{what}: {h[key]}  {h[key + '_where']}")
    print("\ncategory            cases  score  all-pass")
    for k, v in s["by_category"].items():
        print(f"  {k:18s} {v['cases']:5d}  {v['mean_score']:.3f}  {v['all_pass']}")
    print("\ndimension     passed/total  rate")
    for k, v in s["by_dimension"].items():
        print(f"  {k:12s} {v['passed']:4d}/{v['total']:<4d}   {v['rate']:.3f}")
    print("\ncase  score  failed checks")
    for c in s["cases"]:
        print(f"  {c['id']}  {c['score']:.2f}  {', '.join(c['failed'])}")


def _load_summary(run_dir):
    """A run directory's summary, re-derived from its results so an older run
    is compared with today's checks, not with the ones it was scored by."""
    if not run_dir:
        return None
    path = os.path.join(run_dir, "results.json")
    if os.path.exists(path):
        return summarise(json.load(open(path)))
    return json.load(open(os.path.join(run_dir, "summary.json")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8082")
    ap.add_argument("--container", default="techsara-e2e-aiq-orchestrator")
    ap.add_argument("--email", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("--rescore", default="")
    ap.add_argument("--gate", action="store_true",
                    help="apply the dev-to-main gate to the run and exit non-zero when it fails")
    ap.add_argument("--baseline", default="", help="a run directory to compare every score against")
    ap.add_argument("--code-root", default=os.environ.get("AIQ_RUNTIME", os.path.join(HERE, ".runtime")),
                    help="where the scratch venv for the coding cases lives (outside the repository)")
    args = ap.parse_args()

    if args.rescore:
        path = os.path.join(args.rescore, "results.json")
        results = rescore(json.load(open(path)), args.container)
        json.dump(results, open(path, "w"), indent=1, default=str)
        s = summarise(results)
        json.dump(s, open(os.path.join(args.rescore, "summary.json"), "w"), indent=1)
        print_summary(s)
        return _finish(s, args)

    make_fixtures.ensure()
    email, pw = _cred(args)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    todo = [c for c in CASES if (not only or c["id"] in only) and (not args.category or c["category"] == args.category)]
    out = args.out or os.path.join(HERE, "runs", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    image = open(os.path.join(HERE, ".runtime", "image.id")).read().strip() if os.path.exists(os.path.join(HERE, ".runtime", "image.id")) else ""
    results = {"base": args.base, "image": image, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cases": []}
    toolchain = code_sandbox.Toolchain(os.path.join(args.code_root, "code")) if any(
        t["expect"].get("code") for c in todo for t in c["turns"]) else None
    t0 = time.time()
    print(f"{len(todo)} cases against {args.base} (image {image[:19]}), {args.workers} workers → {out}")
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_case, (args.base, email, pw, args.container), c, out, toolchain): c for c in todo}
        for f in cf.as_completed(futs):
            results["cases"].append(f.result())
            results["cases"].sort(key=lambda r: [c["id"] for c in CASES].index(r["id"]))
            json.dump(results, open(os.path.join(out, "results.json"), "w"), indent=1, default=str)
    results["seconds"] = round(time.time() - t0)
    json.dump(results, open(os.path.join(out, "results.json"), "w"), indent=1, default=str)
    s = summarise(results)
    json.dump(s, open(os.path.join(out, "summary.json"), "w"), indent=1)
    print_summary(s)
    return _finish(s, args, out)


def _finish(summary, args, out_dir=""):
    """Print the gate when it was asked for; its verdict is the exit code."""
    if not args.gate and not args.baseline:
        return 0
    report = gate_report(summary, _load_summary(args.baseline))
    print_gate(report)
    target = out_dir or args.rescore
    if target:
        json.dump(report, open(os.path.join(target, "gate.json"), "w"), indent=1)
    return 0 if (report["passed"] or not args.gate) else 1


if __name__ == "__main__":
    sys.exit(main())
