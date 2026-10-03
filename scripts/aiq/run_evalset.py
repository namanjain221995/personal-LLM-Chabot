#!/usr/bin/env python3
"""Run the upgrade evaluation set (eval_set.py, B-02) against an orchestrator: the B-04 baseline runner.

  PY=<orchestrator venv>/bin/python
  # what would be sent, case by case, with no network call at all:
  $PY scripts/aiq/run_evalset.py --dry-run --base http://127.0.0.1:28080
  # a run: loopback only, the password from a file, stdin or AIQ_PASSWORD (never the command line)
  $PY scripts/aiq/run_evalset.py --base http://127.0.0.1:28080 --email <account> --password-file <path> \\
      --repeats 3 --workers 1 --label "<stack, image, topology, load>" [--only EV01,RQ03] [--out DIR]
  # re-score a finished run after a check changed, without calling anything:
  $PY scripts/aiq/run_evalset.py --rescore DIR

Writes DIR/results.json (schema 1, below; rewritten after every finished
record, so a killed run keeps what it finished) and DIR/summary.json.

ONE (case, repeat). A fresh conversation id; each `attachments` entry is
uploaded first (POST /uploads, form fields file / conversation_id / purpose);
a document is then named on the case's FIRST turn as
`pdf_uploads: [{"upload_id", "name"}]` (the browser's shape,
frontend/lib/orchestrator.ts), `name` being the filename the server stored.
The upload's own time is `attachments[].upload_s`, not part of the
turn's timing (the browser, too, finishes /uploads before it POSTs /chat;
frontend/lib/uploadDocument.ts). The case's
synthetic `history` leads `messages`; every turn sends
mode "assistant", model "smart", the case's effort, web_search and
deep_research, `test_case_id` = the case id (a trace join key only;
main.py ChatRequest) and a fresh `intent_id` per turn, as the browser does
(V29: the first SSE event is then the meta that names the trace). A later turn
carries the earlier turns and their real answers.

TIMING, per turn, float seconds from just before the POST (time.perf_counter),
null when it never happened (harness.Client.chat):
  first_event_s   the first SSE event of any kind (an SSE comment such as the
                  `: keep-alive` heartbeat is not an event)
  first_token_s   the first `token` event, even whitespace
  first_answer_s  the first `token` event holding a non-whitespace character:
                  the first meaningful answer token. A step, status, research,
                  reasoning or meta event, or a heartbeat, never is one.
  total_s         the stream closed

SERVER STAGES, from GET /chat/trace/{meta.trace_id} (db.get_query_trace: the
root row plus its query_trace_events; app/core/tracing.py), integer ms:
  stages          {STAGE: duration_ms} for every event that recorded a
                  duration (the time the stage took, server clock):
                  MODE_RESOLVED routing (decide + gates), KNOWLEDGE_PREPARED
                  the retrieval pre-pass, CONTEXT_ASSEMBLED prompt preparation
                  (facts, recall, documents, history, compaction),
                  MODEL_PROMPT_PREPARED message shaping, MODEL_DISPATCHED the
                  breaker and admission queue, MODEL_FIRST_CHUNK prefill
                  (dispatch to the engine's first chunk), MODEL_STREAM_ENDED
                  generation (first chunk to end), FIRST_ANSWER_TOKEN request
                  start to the first non-blank answer token, RERANK a rerank
                  call, and whatever else the trace timed
  stage_offsets   {STAGE: ms from the trace's started_at to the event's
                  completed_at} for EVERY event, durations or not
  A stage seen more than once (MODEL_* per model call, RERANK per call) is
  keyed STAGE, STAGE#2, STAGE#3 ... in trace order. server_total_ms is the
  root's total_duration_ms, trace_status its final_status, versions its
  `versions` (application, schema, model id).
USAGE: prompt/completion tokens summed over the MODEL_STREAM_ENDED events'
  `details.usage` (llm.stream_chat_events records each streamed main-model
  call's own counts, as the serving runtime reported them; the trace keeps
  the first 8 calls, tracing.MAX_TRACED_MODEL_CALLS). Router, embedding,
  rerank and non-streamed JSON calls are not in it. calls_counted says how
  many calls were summed, calls_total how many the turn made (the root's
  meta.stage_counts.model_call). null when no traced call reported a count.
SOURCE PASSAGES: no endpoint exposes the text a run read. meta.sources rows
  carry n/url/title/read flags only (engines/search.py _meta_sources;
  engines/deep_research.py _sources_meta), the trace stores no evidence text
  (tracing.py: "raw prompts, result rows, answer text ... do not belong
  here"), and research_runs keeps sources_meta only. So source_passages is {}
  and source_passages_captured false, and a `citations.passage_support` check
  FAILS ("no passage captured"), as answer_checks.py requires; nothing is
  filled in from search snippets.
trace_thinks: run.py's _trace_thinks, plus True when a MODEL_PROMPT_PREPARED
  event says the call ran with `thinking` on (the ADAPTIVE_THINKING stage
  run.py looks for no longer exists).

SCORING: harness.check_turn(expect, result + md_metrics(answer), effort=...,
fail_text=eval_set.FAIL_TEXT, prev_file=None, upload_path=None), which also
runs answer_checks.check: the path tests/test_eval_set.py scores the
hand-written answers with. A `code` expect runs code_sandbox.run_code first,
as run.py does. A case that crashed (an exception, an HTTP error from /chat,
--case-timeout-s) has `error` "Type: message", keeps the turns it finished,
and scores 0.

Safety: --base must be http(s)://127.0.0.1 or localhost (and never port 8080 or
3000, harness.PROD_PORTS); at most two requests in flight (--workers <= 2,
the dev stack's inference cap); the password is never printed or written; no
credential reaches results.json; /health is reduced to names and statuses.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import code_sandbox  # noqa: E402
import eval_set as ES  # noqa: E402
import harness as H  # noqa: E402
from run import _trace_thinks as _run_trace_thinks  # noqa: E402

SCHEMA = 1
MAX_WORKERS = 2
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}

#: MASTER_PROMPT §24 latency classes (the eval set has no large-document job).
#: The same map as baseline.py's; written into every record.
WORKLOAD: Dict[str, str] = {
    **{cid: "direct_fast" for cid in ("EV01", "EV02", "EV03", "RQ01", "RQ03", "RQ05", "RQ06", "RQ07")},
    **{cid: "evidence_fast" for cid in ("EV05", "RQ02")},
    **{cid: "live_search_fast" for cid in ("EV04", "RQ04")},
    "EV07": "long_context",
    **{cid: "think" for cid in ("EV06", "EV08")},
    "EV09": "max",
}
assert set(WORKLOAD) == set(ES.BY_ID), sorted(set(WORKLOAD) ^ set(ES.BY_ID))

ORDER = {c["id"]: i for i, c in enumerate(ES.EVAL_SET_CASES)}
#: a trace still being written when the stream closed is read again this often
TRACE_RETRIES, TRACE_RETRY_S = 5, 0.4
#: a /health check name kept in conditions: a service name ("vllm-router",
#: "app_db"), never anything with a dot, colon or slash in it (a host or URL)
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_STATUS = re.compile(r"^[a-z_]{1,32}$")


class ChatFailed(RuntimeError):
    """/chat answered with an HTTP error instead of a stream."""


class CaseTimeout(RuntimeError):
    """The case ran past --case-timeout-s."""


# ============================================================ guard rails ==

def check_base(base: str) -> str:
    """The base URL, or SystemExit when it is not a loopback orchestrator."""
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError as exc:
        raise SystemExit(f"refusing --base {base!r}: {exc}")
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise SystemExit(f"refusing --base {base!r}: only http(s)://127.0.0.1 or localhost, no credentials in the URL")
    if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        raise SystemExit(f"refusing --base {base!r}: the runner talks to 127.0.0.1 or localhost only")
    if str(port or (443 if parts.scheme == "https" else 80)) in H.PROD_PORTS:
        raise SystemExit(f"refusing --base {base!r}: port {port} is a production port")
    return base.rstrip("/")


def read_password(args) -> str:
    """--password-file, --password-stdin or AIQ_PASSWORD, in that order. Never argv."""
    if args.password_file:
        with open(os.path.expanduser(args.password_file), encoding="utf-8") as fh:
            pw = fh.read().strip()
    elif args.password_stdin:
        pw = sys.stdin.readline().strip()
    else:
        pw = os.environ.get("AIQ_PASSWORD", "")
    if not pw:
        raise SystemExit("no password: use --password-file PATH, --password-stdin or AIQ_PASSWORD")
    return pw


def harness_commit() -> Optional[str]:
    try:
        p = subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = p.stdout.strip()
    return sha if p.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def eval_set_sha256() -> str:
    """A fingerprint of the cases as run: a baseline and a candidate with
    different fingerprints did not run the same set."""
    blob = json.dumps(ES.EVAL_SET_CASES, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def health_summary(client: "EvalClient") -> Optional[dict]:
    """GET /health, reduced to the overall status and each check's name and
    status: no URL, host, detail or error body survives."""
    try:
        r = client.http.get(f"{client.base}/health", timeout=30.0)
        body = r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — conditions are best effort
        return None
    if not isinstance(body, dict):
        return None

    def word(v: Any) -> str:
        v = str(v or "").strip().lower()
        return v if _STATUS.fullmatch(v) else "other"

    checks = {}
    raw = body.get("checks") if isinstance(body.get("checks"), dict) else {}
    for name, val in raw.items():
        if isinstance(name, str) and _NAME.fullmatch(name):
            checks[name] = word(val.get("status") if isinstance(val, dict) else val)
    return {"status": word(body.get("status")), "checks": dict(sorted(checks.items()))}


# ================================================================ client ==

class EvalClient(H.Client):
    """harness.Client plus Stop, and a trace read that waits for its last write."""

    def stop(self, conversation_id: str) -> None:
        try:
            self.http.post(f"{self.base}/chat/stop", json={"conversation_id": conversation_id,
                                                           "session_id": conversation_id}, timeout=30.0)
        except Exception:  # noqa: BLE001 — best effort; the record already says it timed out
            pass

    def settled_trace(self, trace_id: str) -> Optional[dict]:
        trace = None
        for _ in range(TRACE_RETRIES):
            try:
                trace = self.trace(trace_id)
            except Exception:  # noqa: BLE001 — a missing trace is recorded as {}
                return None
            if not trace or trace.get("final_status") != "running":
                return trace
            time.sleep(TRACE_RETRY_S)
        return trace


# ================================================================= trace ==

def _when(value: Any) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def reduce_trace(trace: Optional[dict]) -> dict:
    """stages / stage_offsets / usage / versions / status from one trace row."""
    out = {"stages": {}, "stage_offsets": {}, "server_total_ms": None, "trace_status": None, "versions": {},
           "usage": {"prompt_tokens": None, "completion_tokens": None, "calls_counted": 0, "calls_total": None}}
    if not isinstance(trace, dict):
        return out
    started = _when(trace.get("started_at"))
    seen: Dict[str, int] = defaultdict(int)
    prompt = completion = None
    for ev in trace.get("events") or []:
        if not isinstance(ev, dict) or not ev.get("stage"):
            continue
        stage = str(ev["stage"])
        seen[stage] += 1
        key = stage if seen[stage] == 1 else f"{stage}#{seen[stage]}"
        if isinstance(ev.get("duration_ms"), (int, float)):
            out["stages"][key] = ev["duration_ms"]
        done = _when(ev.get("completed_at"))
        if started is not None and done is not None:
            out["stage_offsets"][key] = round((done - started).total_seconds() * 1000)
        usage = (ev.get("details") or {}).get("usage") if stage == "MODEL_STREAM_ENDED" else None
        if isinstance(usage, dict):
            p, c = (v if isinstance(v, int) and not isinstance(v, bool) else None
                    for v in (usage.get("prompt_tokens"), usage.get("completion_tokens")))
            if p is not None or c is not None:
                out["usage"]["calls_counted"] += 1
            if p is not None:
                prompt = (prompt or 0) + p
            if c is not None:
                completion = (completion or 0) + c
    out["usage"]["prompt_tokens"], out["usage"]["completion_tokens"] = prompt, completion
    counts = (trace.get("meta") or {}).get("stage_counts") if isinstance(trace.get("meta"), dict) else None
    if isinstance(counts, dict) and isinstance(counts.get("model_call"), int):
        out["usage"]["calls_total"] = counts["model_call"]
    if isinstance(trace.get("total_duration_ms"), (int, float)):
        out["server_total_ms"] = trace["total_duration_ms"]
    out["trace_status"] = trace.get("final_status")
    versions = trace.get("versions")
    if isinstance(versions, dict):
        out["versions"] = {str(k): v for k, v in versions.items() if isinstance(v, (str, int, float, bool))}
    return out


def trace_thinks(trace: Optional[dict]) -> Optional[bool]:
    legacy = _run_trace_thinks(trace)
    if legacy is None or legacy:
        return legacy
    for ev in trace.get("events") or []:
        if (isinstance(ev, dict) and ev.get("stage") == "MODEL_PROMPT_PREPARED"
                and (ev.get("details") or {}).get("thinking") is True):
            return True
    return False


# ================================================================ scoring ==

def scoring_view(result: dict) -> dict:
    """The turn record check_turn reads: the stored result, its markdown
    metrics, and the artifact ref when the turn produced a file (every
    eval-set case expects none, so a file fails its `artifact` check)."""
    refs = (result.get("meta") or {}).get("artifacts") or []
    art = {"ref": refs[0], "files": [], "all_refs": len(refs)} if refs and isinstance(refs[0], dict) else None
    return {**result, "md": H.md_metrics(result.get("answer") or ""), "artifact": art}


def score_turn(case: dict, turn: dict, result: dict) -> List[dict]:
    return H.check_turn(turn["expect"], scoring_view(result), effort=case["effort"], fail_text=ES.FAIL_TEXT,
                        prev_file=None, upload_path=None)


def record_checks(rec: dict) -> List[dict]:
    """Every check of a record, plus a failing `case_completed` when it
    crashed or stopped short (run.py's rule): an errored case scores 0."""
    checks = [c for t in rec.get("turns") or [] for c in t.get("checks") or []]
    case = ES.BY_ID.get(rec["id"])
    if rec.get("error") or case is None or len(rec.get("turns") or []) < len(case["turns"]):
        checks = checks + [{"check": "case_completed", "dimension": "deliverable", "ok": False,
                            "detail": rec.get("error") or "turns missing"}]
    return checks


def record_score(rec: dict) -> Tuple[bool, float]:
    """(every check passed, fraction of checks passed); an errored case is (False, 0.0)."""
    if rec.get("error"):
        return False, 0.0
    checks = record_checks(rec)
    passed = sum(1 for c in checks if c["ok"])
    return (bool(checks) and passed == len(checks)), (passed / len(checks) if checks else 0.0)


# =================================================================== run ==

def _message_preview(text: str) -> str:
    return text[:300] + ("…" if len(text) > 300 else "")


def run_one(client: EvalClient, case: dict, repeat: int, out_dir: str, toolchain=None,
            case_timeout_s: float = 1800.0, log=print) -> dict:
    """One (case, repeat): uploads, then each turn; never raises for a case failure."""
    started = time.perf_counter()
    conv = f"aiq-{case['id']}-r{repeat}-{int(time.time() * 1000)}"
    rec: Dict[str, Any] = {"id": case["id"], "category": case["category"], "section": case["section"],
                           "effort": case["effort"], "workload": WORKLOAD[case["id"]], "repeat": repeat,
                           "conversation_id": conv, "error": None, "attachments": [], "turns": []}

    def remaining() -> float:
        left = case_timeout_s - (time.perf_counter() - started)
        if left <= 0:
            raise CaseTimeout(f"the case ran past --case-timeout-s {case_timeout_s:g}")
        return left

    try:
        documents = []
        for att in case.get("attachments") or []:
            path = os.path.join(ES.FIXTURES, att["fixture"])
            remaining()
            t_up = time.perf_counter()
            up = client.upload(conv, path, purpose=att["purpose"])
            name = up.get("filename") or os.path.basename(path)
            rec["attachments"].append({"fixture": att["fixture"], "purpose": att["purpose"],
                                       "upload_id": up.get("upload_id"), "name": name, "bytes": up.get("bytes"),
                                       "upload_s": round(time.perf_counter() - t_up, 3)})
            if att["purpose"] == "document":
                documents.append({"upload_id": up["upload_id"], "name": name})
        history = [{"role": m["role"], "content": m["content"]} for m in case.get("history") or []]
        for ti, t in enumerate(case["turns"]):
            res = client.chat(conv, t["message"], history, case["effort"], web_search=case["web_search"],
                              pdf_uploads=documents if ti == 0 and documents else None,
                              deep_research=bool(case.get("deep_research")), test_case_id=case["id"],
                              extra={"intent_id": uuid.uuid4().hex}, max_seconds=remaining())
            meta = res.get("meta") or {}
            errors = list(res.get("errors") or [])
            if res.get("http") != 200:
                errors.append({"http": res.get("http"), "body": str(res.get("error") or "")[:400]})
            trace = client.settled_trace(str(meta.get("trace_id") or "")) if meta.get("trace_id") else None
            result: Dict[str, Any] = {
                "http": res.get("http"), "answer": res.get("answer") or "", "errors": errors,
                "request_id": res.get("request_id"), "meta": meta,
                "reasoning_events": res.get("reasoning_events", 0), "reasoning_chars": res.get("reasoning_chars", 0),
                "timing": res.get("timing") or {}, "timed_out": bool(res.get("timed_out")),
                **reduce_trace(trace),
                "trace_thinks": trace_thinks(trace),
                "source_passages": {}, "source_passages_captured": False,
            }
            if t["expect"].get("code"):
                workdir = os.path.join(out_dir, "code", case["id"], f"r{repeat}", f"t{ti + 1}")
                if toolchain is None:  # a direct caller; main() passes --code-root's
                    toolchain = code_sandbox.Toolchain(
                        os.path.join(os.environ.get("AIQ_RUNTIME", os.path.join(HERE, ".runtime")), "code"))
                result["code_result"] = code_sandbox.run_code(t["expect"]["code"], result["answer"], workdir,
                                                              toolchain)
            checks = score_turn(case, t, result)
            rec["turns"].append({"message": _message_preview(t["message"]), "expect": t["expect"],
                                 "checks": checks, "result": result})
            tm = result["timing"]
            failed = [c["check"] for c in checks if not c["ok"]]
            log(f"  {case['id']} r{repeat} t{ti + 1}: {len(checks) - len(failed)}/{len(checks)} checks, "
                f"first answer {tm.get('first_answer_s')} s, total {tm.get('total_s')} s, route={meta.get('route')}"
                + (f"  FAILED: {', '.join(failed)}" if failed else ""))
            if res.get("http") != 200:
                raise ChatFailed(f"/chat returned HTTP {res.get('http')}: {str(res.get('error') or '')[:200]}")
            if result["timed_out"]:
                raise CaseTimeout(f"the case ran past --case-timeout-s {case_timeout_s:g}; the generation was stopped")
            history += [{"role": "user", "content": t["message"]}, {"role": "assistant", "content": result["answer"]}]
    except Exception as exc:  # noqa: BLE001 — a crashed case scores 0, with the reason
        if isinstance(exc, (CaseTimeout, httpx.HTTPError)):
            # the generation is detached: a stream we stopped reading would
            # otherwise hold one of the stack's two inference slots
            client.stop(conv)
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()[-2000:]
        log(f"  {case['id']} r{repeat} ERROR {rec['error']}")
    return rec


def _sort(cases: List[dict]) -> List[dict]:
    return sorted(cases, key=lambda r: (ORDER.get(r["id"], len(ORDER)), int(r.get("repeat") or 0)))


def _write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def rescore(results: dict) -> dict:
    """Re-run every check over the stored answers; nothing is called."""
    for rec in results.get("cases") or []:
        case = ES.BY_ID.get(rec["id"])
        if case is None:
            continue
        rec.setdefault("workload", WORKLOAD.get(rec["id"]))
        for t, tr in zip(case["turns"], rec.get("turns") or []):
            tr["expect"] = t["expect"]
            tr["checks"] = score_turn(case, t, tr["result"])
    results["cases"] = _sort(results.get("cases") or [])
    return results


# =============================================================== summary ==

def summarise(results: dict) -> dict:
    per_case: Dict[str, dict] = {}
    by_workload: Dict[str, dict] = defaultdict(lambda: {"cases": set(), "records": 0, "records_pass": 0,
                                                         "errors": 0, "turns": 0, "first_answer_s_n": 0,
                                                         "total_s_n": 0})
    by_check: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    fast_turns = thinking = stream_errors = timed_out = no_passages = 0
    thinking_ids = set()
    scores = []
    for rec in _sort(results.get("cases") or []):
        ok, score = record_score(rec)
        scores.append(score)
        failed = sorted({c["check"] for c in record_checks(rec) if not c["ok"]})
        entry = per_case.setdefault(rec["id"], {"id": rec["id"], "workload": rec.get("workload") or WORKLOAD.get(rec["id"]),
                                                "effort": rec.get("effort"), "repeats": []})
        entry["repeats"].append({"repeat": rec.get("repeat"), "pass": ok, "score": round(score, 3), "failed": failed,
                                 "error": rec.get("error")})
        w = by_workload[entry["workload"]]
        w["cases"].add(rec["id"])
        w["records"] += 1
        w["records_pass"] += int(ok)
        w["errors"] += int(bool(rec.get("error")))
        for c in record_checks(rec):
            by_check[c["check"]][0] += int(c["ok"])
            by_check[c["check"]][1] += 1
        for tr in rec.get("turns") or []:
            res = tr.get("result") or {}
            w["turns"] += 1
            w["first_answer_s_n"] += int((res.get("timing") or {}).get("first_answer_s") is not None)
            w["total_s_n"] += int((res.get("timing") or {}).get("total_s") is not None)
            stream_errors += int(bool(res.get("errors")))
            timed_out += int(bool(res.get("timed_out")))
            no_passages += int(res.get("source_passages_captured") is False)
            if rec.get("effort") == "fast":
                fast_turns += 1
                if any(c["check"] == "thinking_off" and not c["ok"] for c in tr.get("checks") or []):
                    thinking += 1
                    thinking_ids.add(rec["id"])
    cases = []
    for entry in per_case.values():
        reps = entry["repeats"]
        entry["pass_rate"] = round(sum(r["pass"] for r in reps) / len(reps), 3)
        entry["mean_score"] = round(sum(r["score"] for r in reps) / len(reps), 3)
        cases.append(entry)
    return {
        "kind": "evalset-summary", "schema": SCHEMA,
        "headline": {
            "records": len(scores), "cases": len(cases),
            "mean_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "records_all_pass": sum(r["pass"] for c in cases for r in c["repeats"]),
            "cases_pass_every_repeat": sum(1 for c in cases if c["pass_rate"] == 1.0),
            "errors": sum(1 for c in cases for r in c["repeats"] if r["error"]),
            "fast_turns": fast_turns, "fast_turns_thinking": thinking,
            "fast_thinking_case_ids": sorted(thinking_ids),
            "turns_with_stream_errors": stream_errors, "turns_timed_out": timed_out,
            "turns_without_source_passages": no_passages,
        },
        "cases": cases,
        "by_workload": {k: {**v, "cases": sorted(v["cases"], key=lambda c: ORDER.get(c, 99))}
                        for k, v in sorted(by_workload.items())},
        "by_check": {k: {"passed": p, "total": n, "rate": round(p / n, 3)} for k, (p, n) in sorted(by_check.items())},
    }


def print_summary(s: dict) -> None:
    h = s["headline"]
    print(f"\nMEAN SCORE {h['mean_score']:.3f}  ({h['records_all_pass']}/{h['records']} records pass every check, "
          f"{h['cases_pass_every_repeat']}/{h['cases']} cases pass every repeat, {h['errors']} errored)")
    print(f"Fast turns that thought: {h['fast_turns_thinking']}/{h['fast_turns']} {h['fast_thinking_case_ids']}")
    print(f"turns with stream errors {h['turns_with_stream_errors']}, timed out {h['turns_timed_out']}, "
          f"without source passages {h['turns_without_source_passages']}")
    print("\ncase  workload          effort  repeats  score  failed checks")
    for c in s["cases"]:
        marks = "".join("P" if r["pass"] else ("E" if r["error"] else "F") for r in c["repeats"])
        failed = sorted({f for r in c["repeats"] for f in r["failed"]})
        print(f"  {c['id']}  {c['workload']:<16s}  {c['effort']:<6s}  {marks:<7s}  {c['mean_score']:.2f}   "
              f"{', '.join(failed)}")
    print("\nworkload          cases  records  pass  errors")
    for k, v in s["by_workload"].items():
        print(f"  {k:<16s}  {len(v['cases']):5d}  {v['records']:7d}  {v['records_pass']:4d}  {v['errors']:6d}")


# =============================================================== dry run ==

def _shape(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 60 else f"<str {len(value)} chars>"
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return value


def dry_run(todo: List[dict], base: str, repeats: int) -> None:
    print(f"DRY RUN: {len(todo)} case(s) x {repeats} repeat(s) against {base}; nothing is sent.")
    for case in todo:
        conv = f"aiq-{case['id']}-r1-<ms>"
        print(f"\n{case['id']}  effort={case['effort']}  workload={WORKLOAD[case['id']]}  "
              f"web_search={case['web_search']}  deep_research={bool(case.get('deep_research'))}")
        documents = []
        for att in case.get("attachments") or []:
            path = os.path.join(ES.FIXTURES, att["fixture"])
            print(f"  POST /uploads  form: conversation_id={conv} purpose={att['purpose']} "
                  f"file={os.path.basename(path)} ({os.path.getsize(path)} bytes)")
            if att["purpose"] == "document":
                documents.append({"upload_id": "<from /uploads>", "name": os.path.basename(path)})
        history = [{"role": m["role"], "content": m["content"]} for m in case.get("history") or []]
        for ti, t in enumerate(case["turns"]):
            body = H.chat_body(conv, t["message"], history, case["effort"], case["web_search"],
                               pdf_uploads=documents if ti == 0 and documents else None,
                               deep_research=bool(case.get("deep_research")), test_case_id=case["id"],
                               extra={"intent_id": "<32 hex per turn>"})
            shown = dict(body)
            msgs = shown.pop("messages")
            shown = _shape(shown)
            shown["messages"] = (f"<{len(msgs)} messages: {len(history)} history + 1 user, "
                                 f"{sum(len(m['content']) for m in msgs)} chars>")
            print(f"  turn {ti + 1}: POST /chat {json.dumps(shown, ensure_ascii=False)}")
            print(f"          expect: {', '.join(sorted(t['expect']))}")
            history += [{"role": "user", "content": t["message"]}, {"role": "assistant", "content": "<answer>"}]


# ================================================================== main ==

def _parse_only(text: str) -> List[str]:
    only = [x.strip() for x in (text or "").split(",") if x.strip()]
    unknown = [x for x in only if x not in ES.BY_ID]
    if unknown:
        raise SystemExit(f"unknown case id(s) {unknown}; known: {', '.join(ES.BY_ID)}")
    return only


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="", help="the orchestrator, http(s)://127.0.0.1 or localhost only")
    ap.add_argument("--email", default="", help="the account (default: AIQ_EMAIL)")
    ap.add_argument("--password-file", default="", help="a file holding the password")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    ap.add_argument("--only", default="", help="comma-separated case ids, e.g. EV01,RQ03")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help=f"requests in flight, 1..{MAX_WORKERS}")
    ap.add_argument("--out", default="", help="the run directory (default: runs/evalset-<stamp>)")
    ap.add_argument("--label", default="", help="free text naming the conditions: stack, image, topology, load")
    ap.add_argument("--rescore", default="", help="a run directory to re-score without calling anything")
    ap.add_argument("--case-timeout-s", type=float, default=1800.0)
    ap.add_argument("--dry-run", action="store_true", help="print each request's shape; no network")
    ap.add_argument("--code-root", default=os.environ.get("AIQ_RUNTIME", os.path.join(HERE, ".runtime")),
                    help="where the coding cases' scratch toolchain lives (as run.py's --code-root)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.rescore:
        path = os.path.join(args.rescore, "results.json")
        with open(path, encoding="utf-8") as fh:
            results = rescore(json.load(fh))
        _write_json(path, results)
        s = summarise(results)
        _write_json(os.path.join(args.rescore, "summary.json"), s)
        print_summary(s)
        return 0

    if not args.base:
        raise SystemExit("--base is required")
    base = check_base(args.base)
    if not 1 <= args.workers <= MAX_WORKERS:
        raise SystemExit(f"refusing --workers {args.workers}: the dev stack's inference cap allows "
                         f"{MAX_WORKERS} requests in flight")
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    if args.case_timeout_s <= 0:
        raise SystemExit("--case-timeout-s must be positive")
    only = _parse_only(args.only)
    todo = [c for c in ES.EVAL_SET_CASES if not only or c["id"] in only]
    problems = ES.validate(todo)
    if problems:
        raise SystemExit("the evaluation set is not valid:\n" + "\n".join(problems))

    if args.dry_run:
        dry_run(todo, base, args.repeats)
        return 0

    email = args.email or os.environ.get("AIQ_EMAIL", "")
    if not email:
        raise SystemExit("no account: use --email or AIQ_EMAIL")
    password = read_password(args)
    out = args.out or os.path.join(HERE, "runs", "evalset-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    toolchain = (code_sandbox.Toolchain(os.path.join(args.code_root, "code"))
                 if any(t["expect"].get("code") for c in todo for t in c["turns"]) else None)

    first = EvalClient(base, email, password)
    results: Dict[str, Any] = {
        "kind": "evalset", "schema": SCHEMA, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finished": None, "seconds": None,
        "conditions": {"base": base, "harness_commit": harness_commit(), "label": args.label,
                       "workers": args.workers, "repeats": args.repeats, "only": only,
                       "health": health_summary(first), "case_timeout_s": args.case_timeout_s,
                       "eval_set_sha256": eval_set_sha256()},
        "cases": [],
    }
    results_path = os.path.join(out, "results.json")
    _write_json(results_path, results)
    lock = threading.Lock()
    local = threading.local()
    spare = [first]

    def client() -> EvalClient:
        if getattr(local, "client", None) is None:
            with lock:
                local.client = spare.pop() if spare else None
            if local.client is None:
                local.client = EvalClient(base, email, password)
        return local.client

    # repeat-major: every case's first repeat before any second, so drift over
    # the run spreads across the cases instead of landing on the last ones
    jobs = [(c, r) for r in range(1, args.repeats + 1) for c in todo]
    t0 = time.perf_counter()
    print(f"{len(todo)} case(s) x {args.repeats} repeat(s) against {base}, {args.workers} worker(s) -> {out}",
          flush=True)

    def log(line: str) -> None:
        print(line, flush=True)

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(lambda c=c, r=r: run_one(client(), c, r, out, toolchain, args.case_timeout_s, log))
                for c, r in jobs]
        for fut in cf.as_completed(futs):
            rec = fut.result()
            with lock:
                results["cases"] = _sort(results["cases"] + [rec])
                _write_json(results_path, results)
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results["seconds"] = round(time.perf_counter() - t0)
    _write_json(results_path, results)
    s = summarise(results)
    _write_json(os.path.join(out, "summary.json"), s)
    print_summary(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
