#!/usr/bin/env python3
"""Run the upgrade evaluation set (eval_set.py, B-02) against an orchestrator: the B-04 baseline runner.

  PY=<orchestrator venv>/bin/python
  # what would be sent, case by case, with no network call at all:
  $PY scripts/aiq/run_evalset.py --dry-run --base http://127.0.0.1:28080
  # a run: loopback only, the password from a file, stdin or AIQ_PASSWORD (never the command line)
  $PY scripts/aiq/run_evalset.py --base http://127.0.0.1:28080 --email <account> --password-file <path> \\
      --repeats 1 --workers 1 --label "<stack, image, topology, load>" [--only EV01,RQ03] [--out DIR] \\
      [--not-before 05:00 --deadline 07:00]
  # re-score a finished run after a check changed, without calling anything:
  $PY scripts/aiq/run_evalset.py --rescore DIR [--force-rescore]

Writes DIR/results.json (schema 1, below; rewritten after every finished
record, so a killed run keeps what it finished) and DIR/summary.json.

ACCOUNT STATE. The orchestrator answers from more than the request: saved
facts are injected verbatim, and cross-chat recall (semantic and keyword)
searches the account's OTHER conversations, its stored answers included
(main.py read_facts / read_cross_chat, memory_semantic.cross_chat_block).
So one fresh account per run directory; the baseline is N run directories
of --repeats 1, each on its own fresh account (devstack.sh seed), so repeats
never see each other; within one run the case order is fixed (workers 1), so
leakage between cases is identical for baseline and candidate. Before the
first case the runner counts the account's conversations (GET
/history/conversations, active plus ?archived=true) and saved facts (GET
/memory/facts) and keeps COUNTS ONLY in conditions.account; it refuses to
start (exit 2) unless both are 0, or --allow-used-account is given
(conditions.account.allowed_used true). The API exposes no per-user memory
or recall switch (fact extraction and recall are deployment settings, and
/auth/preferences is UI storage the chat path never reads), so
conditions.account.memory_switch says that rather than guessing.

WORKERS. Use --workers 1 for a baseline: the dev stack's cap allows two
requests in flight, and Deep Research and fact extraction make parallel
calls of their own, so a second worker changes the load being measured.

STOPPING. Ctrl-C, SIGTERM and SIGHUP (all three raise KeyboardInterrupt in
the main thread; a signal the runner was started with ignored, as nohup
ignores SIGHUP, stays ignored), or any BaseException reaching the main
thread, stop the run: no new case or turn starts, queued cases are
cancelled, results.json is written with "interrupted" true and "finished"
set, THEN POST /chat/stop goes out for every conversation with a case in
flight and results.json is rewritten with each outcome in
results.stopped_on_interrupt ({"pending": true} until it is sent), so a
stop that hangs cannot hide the interruption. interrupted_by names the
signal (or the exception), and the runner exits 128 + the signal number
(130 SIGINT, 143 SIGTERM, 129 SIGHUP) or re-raises. A case in flight
stops reading its stream at the next line it receives (a token, any event,
or the server's 15 s heartbeat), even when the stop fails and the server
keeps streaming, and sends its own stop too (its POST /chat may have
reached the server after main()'s stop did); a stream silent altogether
is bounded by its read timeout (TIMEOUTS below), and only then can the
process exit.

WINDOW. --not-before HH:MM and --deadline HH:MM are TODAY's times on the
host clock (24-hour; the hosts run Asia/Kolkata, so --not-before 05:00
--deadline 07:00 is the 05:00-07:00 IST window). The runner refuses to
start (exit 2, before signing in) when either is given and the host's UTC
offset is not +05:30, unless --tz-ok; when --not-before is still ahead (a
run meant for 05:00-07:00 cannot start at 04:55); when the deadline has
already passed (it never rolls over to tomorrow, so a late start cannot
become a run with no deadline) or is more than 12 h away. No case starts
after the deadline, and it bounds a case already running: each turn reads
for at most min(the case's time left, the time to the deadline), and a
turn the deadline cuts is timed_out with timeout_reason "deadline",
stopped, and its record's error is "DeadlineCut: ...". The run then ends
with "deadline_reached" true. conditions keep deadline, not_before,
utc_offset (always) and tz_ok. A --dry-run checks the HH:MM format only.

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

TIMEOUTS. Sign-in, /auth/me, /health, the account counts, trace reads and
/chat/stop: 30 s each. An upload: 300 s. The stream waits at most
min(time left in the case, 60 s) for any byte (the server heartbeats every
15 s, app/sse.py), so a dead tunnel fails in 60 s. --case-timeout-s
(default 2400) bounds a whole case: the server's own hang guard
GEN_WALL_CLOCK_S is 1800 s (config.py), and the runner's clock starts
earlier (uploads, earlier turns), so it must not cut a full-length server
generation first. A turn cut short (timed_out; timeout_reason
"case_timeout" or "deadline"; a read that times out once the turn's time is
up is such a cut, not a dead stream) is stopped (POST /chat/stop, outcome
in the record's `stop`) BEFORE its trace is read, and the trace is read
once. A case that ends any other way after its stream was opened (an HTTP
or stream error, an exception) is stopped too: the generation is detached.

TIMING, per turn, float seconds from just before the POST (time.perf_counter),
null when it never happened (harness.Client.chat):
  first_event_s   the first SSE event of any kind (an SSE comment such as the
                  `: keep-alive` heartbeat is not an event)
  first_token_s   the first `token` event, even whitespace
  first_answer_s  the first `token` event holding a non-whitespace character:
                  the first meaningful answer token. A step, status, research,
                  reasoning or meta event, or a heartbeat, never is one.
  total_s         the stream closed after its terminal `done`; null for a
                  turn that did not finish (an HTTP error, a timeout, a
                  stream that ended in `error` or with no terminal event), so
                  a failed turn is never a latency sample
The stream is split into lines on "\\n" only (harness.sse_lines): the server
writes U+2028, U+2029 and U+0085 raw, and a splitlines-style reader cuts a
frame holding one in two. `terminal` is "done", "error" or null, and
`bad_frames` counts `data:` lines that were not a JSON object.
`status_events` keeps the `status` texts (a blocked tool is downgraded with
one such line, main.py feature gate), and conditions.features the account's
/auth/me feature map.

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
  `versions` (application, schema, model id). A read that finds the trace
  missing (404) or still "running" is repeated (TRACE_RETRIES x
  TRACE_RETRY_S); summary.headline.turns_trace_not_ok counts the turns whose
  trace_status is not "ok".
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
as run.py does, on a finished turn only. Before the run the sandbox must be
able to run every selected coding language (conditions.code_sandbox records
the backend); the runner refuses (exit 2) otherwise unless --no-code, which
runs no code at all: run_code records the toolchain as unavailable, so
code_runs and code_correct FAIL (code_present still reads the answer). A
case that crashed (an exception, an HTTP error from /chat, a stream that
ended in `error` or without `done`, --case-timeout-s) has `error`
"Type: message", keeps the turns it finished, and scores 0.

RESCORE re-runs every check over the stored answers and records
conditions.rescored {rescored_at, eval_set_sha256, original_eval_set_sha256,
forced}. It refuses (exit 2) when the run's eval_set_sha256 is not the
current set's, unless --force-rescore. A coding turn's stored code_result is
re-scored as it is (the code is not run again), and the record says so in
`rescore_note`.

Safety: --base must be http(s)://127.0.0.1 or localhost WITH an explicit
port (never 8080 or 3000, harness.PROD_PORTS), and no credentials, query or
fragment; at most two requests in flight (--workers <= 2, the dev stack's
inference cap); the password is never printed or written; no credential
reaches results.json; /health is reduced to names and statuses, the account
to counts.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple
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
#: sign-in, /auth/me, /health, account counts, trace reads, /chat/stop
REQUEST_TIMEOUT_S = 30.0
#: the longest the stream may go without a byte (the server heartbeats every 15 s)
STREAM_IDLE_S = 60.0
#: above the server's GEN_WALL_CLOCK_S (1800 s, config.py): the runner's clock starts first
DEFAULT_CASE_TIMEOUT_S = 2400.0
EXIT_REFUSED = 2
EXIT_SIGINT = 130
#: the hosts' clock: --deadline / --not-before are read on it (the 05:00-07:00 IST window)
IST_OFFSET = "+05:30"
#: a --deadline further ahead than this is a typo or the wrong day, not a window
MAX_DEADLINE_AHEAD = dt.timedelta(hours=12)

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
#: a trace not there yet (404) or still being written when the stream closed is read again this often
TRACE_RETRIES, TRACE_RETRY_S = 5, 0.4
#: a /health check name kept in conditions: a service name ("vllm-router",
#: "app_db"), never anything with a dot, colon or slash in it (a host or URL)
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_STATUS = re.compile(r"^[a-z_]{1,32}$")


class ChatFailed(RuntimeError):
    """/chat answered with an HTTP error instead of a stream."""


class CaseTimeout(RuntimeError):
    """The case ran past --case-timeout-s."""


class StreamFailed(RuntimeError):
    """The stream ended in an `error` event, or with no terminal event at all."""


class LoginFailed(RuntimeError):
    """A worker could not sign in (harness.Client raises SystemExit for that)."""


class DeadlineCut(CaseTimeout):
    """--deadline came while the case was running: its turn was cut and stopped."""


class Interrupted(RuntimeError):
    """The run was stopped (Ctrl-C, SIGTERM, SIGHUP) during a case."""


class Refused(Exception):
    """A run that must not start (main() exits EXIT_REFUSED with the message)."""


# ============================================================ guard rails ==

def check_base(base: str) -> str:
    """The base URL as scheme://host:port[/path], or SystemExit when it is not
    a loopback orchestrator on an explicit, non-production port.

    Parsed twice, by urlsplit and by httpx (the client that sends), and
    refused when the two disagree on the host or the port."""
    try:
        parts = urlsplit(base)
        port = parts.port
        sent = httpx.URL(base)
    except (ValueError, httpx.InvalidURL) as exc:
        raise SystemExit(f"refusing --base {base!r}: {exc}")
    if (parts.scheme not in ("http", "https") or parts.username or parts.password or "@" in parts.netloc
            or "\\" in base):
        raise SystemExit(f"refusing --base {base!r}: only http(s)://127.0.0.1 or localhost, no credentials in the URL")
    if parts.query or parts.fragment:
        raise SystemExit(f"refusing --base {base!r}: no query or fragment")
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"refusing --base {base!r}: the runner talks to 127.0.0.1 or localhost only")
    if port is None:
        raise SystemExit(f"refusing --base {base!r}: give the port explicitly (e.g. http://127.0.0.1:28080)")
    if str(port) in H.PROD_PORTS:
        raise SystemExit(f"refusing --base {base!r}: port {port} is a production port")
    if (sent.host or "").lower() != host or (sent.port or {"http": 80, "https": 443}[parts.scheme]) != port:
        raise SystemExit(f"refusing --base {base!r}: the URL parsers disagree on its host or port")
    return f"{parts.scheme}://{host}:{port}{parts.path.rstrip('/')}"


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
        r = client.http.get(f"{client.base}/health", timeout=REQUEST_TIMEOUT_S)
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


#: what conditions.account.memory_switch says: there is nothing per user to read
MEMORY_SWITCH_NOT_EXPOSED = ("not exposed by the API: fact extraction and cross-chat recall are deployment "
                             "settings, and /auth/preferences is UI storage the chat path never reads")


def account_state(client: "EvalClient", allowed_used: bool = False) -> dict:
    """How much the account already holds, as COUNTS ONLY: its conversations
    (active plus archived; history.py list_conversations) and its saved facts
    (memory_api.py list_facts, {"facts": [...]}). checked false when a count
    could not be read; nothing of the content is kept."""
    out: Dict[str, Any] = {"checked": False, "conversations": None, "conversations_archived": None,
                           "facts": None, "allowed_used": bool(allowed_used),
                           "memory_switch": MEMORY_SWITCH_NOT_EXPOSED}

    def count(path: str, key: Optional[str] = None) -> int:
        r = client.http.get(f"{client.base}{path}", timeout=REQUEST_TIMEOUT_S)
        if r.status_code != 200:
            raise ValueError(f"GET {path.split('?')[0]} answered HTTP {r.status_code}")
        body = r.json()
        items = body.get(key) if key and isinstance(body, dict) else body
        if not isinstance(items, list):
            raise ValueError(f"GET {path.split('?')[0]} did not return a list")
        return len(items)

    try:
        active = count("/history/conversations")
        archived = count("/history/conversations?archived=true")
        facts = count("/memory/facts", "facts")
    except (httpx.HTTPError, ValueError) as exc:
        out["why"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    out.update(checked=True, conversations=active + archived, conversations_archived=archived, facts=facts)
    return out


def account_refusal(account: dict) -> Optional[str]:
    """Why the run must not start on this account, or None."""
    if account.get("allowed_used"):
        return None
    if not account.get("checked"):
        return (f"could not count the account's conversations and saved facts ({account.get('why')}); "
                "a used account leaks cross-chat recall and saved facts into the measurements. "
                "Use a fresh account (ops/dev/devstack.sh seed) or pass --allow-used-account")
    if account["conversations"] or account["facts"]:
        return (f"the account already has {account['conversations']} conversation(s) and {account['facts']} "
                "saved fact(s): cross-chat recall and saved facts would leak into the measurements. "
                "Use a fresh account (ops/dev/devstack.sh seed) or pass --allow-used-account")
    return None


def features_summary(me: Any) -> Dict[str, bool]:
    """/auth/me `features` (authn/features.py), names and booleans only."""
    raw = me.get("features") if isinstance(me, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in sorted(raw.items()) if isinstance(k, str) and _NAME.fullmatch(k) and isinstance(v, bool)}


def code_languages(todo: List[dict]) -> List[str]:
    return sorted({t["expect"]["code"]["lang"] for c in todo for t in c["turns"] if t["expect"].get("code")})


def sandbox_conditions(todo: List[dict], toolchain, no_code: bool) -> dict:
    """Can the sandbox run every selected coding language? Asked before the
    first case, so a run never finds out at its coding case."""
    langs = code_languages(todo)
    out: Dict[str, Any] = {"needed": bool(langs), "languages": langs, "no_code": bool(no_code), "backend": None,
                           "available": {}, "why": {}}
    if not langs or no_code:
        if langs:
            out["available"] = {lang: False for lang in langs}
            out["why"] = {lang: "not run: --no-code" for lang in langs}
        return out
    for lang in langs:
        ok, why = toolchain.available(lang)
        out["available"][lang] = bool(ok)
        if why:
            out["why"][lang] = why
    out["backend"] = toolchain.backend
    return out


class _NoCode:
    """The toolchain --no-code hands run_code: it runs nothing, so run_code
    records the turn as unavailable and code_runs / code_correct fail."""

    tsc = node = None

    def isolation(self) -> dict:
        return {"backend": None}

    def available(self, lang: str) -> Tuple[bool, str]:
        return False, "not run: --no-code"


def _today_at(text: str, flag: str, now: dt.datetime) -> dt.datetime:
    """HH:MM (24-hour) as a moment of `now`'s day, or SystemExit for a malformed time."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (text or "").strip())
    if not m:
        raise SystemExit(f"{flag} {text!r}: use HH:MM, 24-hour, the host's local time")
    return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)


def parse_deadline(text: str, now: dt.datetime) -> dt.datetime:
    """Today's HH:MM. Refused when it has already passed (it never rolls over
    to tomorrow: a late start must not become a run with no deadline) or is
    more than MAX_DEADLINE_AHEAD away."""
    at = _today_at(text, "--deadline", now)
    if at <= now:
        raise Refused(f"--deadline {at:%H:%M} has already passed (now {now:%H:%M})")
    if at - now > MAX_DEADLINE_AHEAD:
        raise Refused(f"--deadline {at:%H:%M} is more than {MAX_DEADLINE_AHEAD.total_seconds() / 3600:g} h away "
                      f"(now {now:%H:%M})")
    return at


def parse_not_before(text: str, now: dt.datetime) -> dt.datetime:
    """Today's HH:MM; refused while it is still ahead, so a run meant for the
    window that opens then cannot start a few minutes early."""
    at = _today_at(text, "--not-before", now)
    if now < at:
        raise Refused(f"--not-before {at:%H:%M} has not come yet (now {now:%H:%M}); start the run inside its window")
    return at


def _local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def utc_offset(now: dt.datetime) -> str:
    """`now`'s UTC offset as +HH:MM."""
    off = int((now.utcoffset() or dt.timedelta(0)).total_seconds()) // 60
    return f"{'-' if off < 0 else '+'}{abs(off) // 60:02d}:{abs(off) % 60:02d}"


def run_window(args, now: dt.datetime) -> dict:
    """--deadline and --not-before as today's moments on the host clock, with
    that clock's UTC offset; Refused when the host is not on IST (unless
    --tz-ok), the window has not opened, or the deadline has passed."""
    window: Dict[str, Any] = {"deadline": None, "not_before": None, "utc_offset": utc_offset(now),
                              "tz_ok": bool(args.tz_ok)}
    if not (args.deadline or args.not_before):
        return window
    if window["utc_offset"] != IST_OFFSET and not args.tz_ok:
        raise Refused(f"the host's local UTC offset is {window['utc_offset']}, not {IST_OFFSET} (IST): --deadline and "
                      "--not-before are read on the host clock, so the window would not be the IST one. Pass "
                      "--tz-ok if host-local time is what you mean")
    if args.not_before:
        window["not_before"] = parse_not_before(args.not_before, now)
    if args.deadline:
        window["deadline"] = parse_deadline(args.deadline, now)
    return window


# ================================================================ client ==

class EvalClient(H.Client):
    """harness.Client with bounded sign-in and trace reads, plus Stop, and a
    trace read that waits for its last write."""

    def __init__(self, base: str, email: str, password: str):
        super().__init__(base, email, password, request_timeout_s=REQUEST_TIMEOUT_S)

    def stop(self, conversation_id: str) -> dict:
        """POST /chat/stop; {"http", "stopped"} or {"error"}, never raises."""
        try:
            r = self.http.post(f"{self.base}/chat/stop", json={"conversation_id": conversation_id,
                                                               "session_id": conversation_id},
                               timeout=REQUEST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — recorded, the run goes on
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}
        try:
            stopped = bool((r.json() or {}).get("stopped"))
        except (ValueError, AttributeError):
            stopped = False
        return {"http": r.status_code, "stopped": stopped}

    def settled_trace(self, trace_id: str, retries: int = TRACE_RETRIES) -> Optional[dict]:
        """The trace once it is written: a read that finds nothing (404, an
        empty body) or a trace still "running" is repeated, `retries` reads
        at most, TRACE_RETRY_S apart."""
        trace = None
        for attempt in range(max(1, retries)):
            if attempt:
                time.sleep(TRACE_RETRY_S)
            try:
                trace = self.trace(trace_id)
            except Exception:  # noqa: BLE001 — an unreadable trace is recorded as {}
                return None
            if trace and trace.get("final_status") != "running":
                return trace
        return trace


class RunControl:
    """What the worker threads share with the main thread: the stop flag,
    the --deadline, and the conversations with a case in flight."""

    def __init__(self, deadline: Optional[float] = None):
        self.stop = threading.Event()
        self.deadline = deadline  # epoch seconds, or None
        self.deadline_reached = False
        self._lock = threading.Lock()
        self._inflight: Dict[str, EvalClient] = {}
        self._halted: Optional[Dict[str, EvalClient]] = None

    def may_start(self) -> bool:
        if self.stop.is_set():
            return False
        if self.deadline is not None and time.time() >= self.deadline:
            self.deadline_reached = True
            return False
        return True

    def begin(self, conversation_id: str, client: EvalClient) -> None:
        with self._lock:
            self._inflight[conversation_id] = client

    def end(self, conversation_id: str) -> None:
        with self._lock:
            self._inflight.pop(conversation_id, None)

    def seconds_to_deadline(self) -> Optional[float]:
        return None if self.deadline is None else self.deadline - time.time()

    def halt(self) -> List[Tuple[str, EvalClient]]:
        """Set the stop flag; the conversations that had a case in flight when
        it was first set (a worker that then notices it ends its case and
        leaves the in-flight set), plus any still in flight now."""
        with self._lock:
            if self._halted is None:
                self._halted = dict(self._inflight)
                self.stop.set()
            self._halted.update(self._inflight)
            return sorted(self._halted.items(), key=lambda kv: kv[0])


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


def _signed_in(get_client: Callable[[], EvalClient]) -> EvalClient:
    try:
        return get_client()
    except SystemExit as exc:  # harness.Client refuses a failed sign-in with SystemExit
        raise LoginFailed(str(exc)) from None


def _run_code(spec: dict, answer: str, workdir: str, toolchain, no_code: bool) -> dict:
    if no_code:
        return code_sandbox.run_code(spec, answer, workdir, _NoCode())
    if toolchain is None:  # a direct caller; main() passes --code-root's
        toolchain = code_sandbox.Toolchain(os.path.join(os.environ.get("AIQ_RUNTIME", os.path.join(HERE, ".runtime")),
                                                        "code"))
    return code_sandbox.run_code(spec, answer, workdir, toolchain)


def run_one(get_client: Callable[[], EvalClient], case: dict, repeat: int, out_dir: str, toolchain=None,
            case_timeout_s: float = DEFAULT_CASE_TIMEOUT_S, log=print, control: Optional[RunControl] = None,
            no_code: bool = False) -> Optional[dict]:
    """One (case, repeat): uploads, then each turn; never raises for a case
    failure. None when the case was not started (the run was stopped, or
    --deadline passed). The client comes from `get_client` INSIDE the try, so
    a failed sign-in is this record's error, not the run's end."""
    control = control or RunControl()
    if not control.may_start():
        return None
    started = time.perf_counter()
    conv = f"aiq-{case['id']}-r{repeat}-{int(time.time() * 1000)}"
    rec: Dict[str, Any] = {"id": case["id"], "category": case["category"], "section": case["section"],
                           "effort": case["effort"], "workload": WORKLOAD[case["id"]], "repeat": repeat,
                           "conversation_id": conv, "error": None, "stop": None, "attachments": [], "turns": []}
    client: Optional[EvalClient] = None

    def remaining() -> float:
        left = case_timeout_s - (time.perf_counter() - started)
        if left <= 0:
            raise CaseTimeout(f"the case ran past --case-timeout-s {case_timeout_s:g}")
        return left

    def not_stopped(what: str) -> None:
        if control.stop.is_set():
            raise Interrupted(f"the run was stopped before {what}")

    def turn_limit(turn: int) -> Tuple[float, str]:
        """How long this turn may read, and what bounds it: the case's time
        left, or the time to --deadline when that comes first."""
        left = remaining()
        to_deadline = control.seconds_to_deadline()
        if to_deadline is None or to_deadline >= left:
            return left, "case_timeout"
        if to_deadline <= 0:
            control.deadline_reached = True
            raise DeadlineCut(f"--deadline passed before turn {turn}")
        return to_deadline, "deadline"

    #: a /chat stream was opened: its generation is detached, so a case that ends abnormally stops it
    opened = False
    try:
        client = _signed_in(get_client)
        control.begin(conv, client)
        documents = []
        for att in case.get("attachments") or []:
            path = os.path.join(ES.FIXTURES, att["fixture"])
            not_stopped("an upload")
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
            not_stopped(f"turn {ti + 1}")
            limit, bound_by = turn_limit(ti + 1)
            opened = True
            res = client.chat(conv, t["message"], history, case["effort"], web_search=case["web_search"],
                              pdf_uploads=documents if ti == 0 and documents else None,
                              deep_research=bool(case.get("deep_research")), test_case_id=case["id"],
                              extra={"intent_id": uuid.uuid4().hex}, max_seconds=limit,
                              idle_timeout_s=STREAM_IDLE_S, cancel=control.stop)
            if res.get("cancelled"):
                opened = res.get("http") is not None
                raise Interrupted(f"the run was stopped during turn {ti + 1}")
            timed_out = bool(res.get("timed_out"))
            if timed_out:
                # first, before the trace or the sandbox: the generation is
                # detached, and a stream we stopped reading still holds one of
                # the stack's two inference slots
                rec["stop"] = client.stop(conv)
            meta = res.get("meta") or {}
            errors = list(res.get("errors") or [])
            if res.get("http") != 200:
                errors.append({"http": res.get("http"), "body": str(res.get("error") or "")[:400]})
            trace_id = str(meta.get("trace_id") or "")
            # a stopped turn's trace is still open: one read, no waiting for it to settle
            trace = client.settled_trace(trace_id, retries=1 if timed_out else TRACE_RETRIES) if trace_id else None
            result: Dict[str, Any] = {
                "http": res.get("http"), "answer": res.get("answer") or "", "errors": errors,
                "request_id": res.get("request_id"), "meta": meta,
                "reasoning_events": res.get("reasoning_events", 0), "reasoning_chars": res.get("reasoning_chars", 0),
                "timing": res.get("timing") or {}, "timed_out": timed_out,
                "timeout_reason": bound_by if timed_out else None, "terminal": res.get("terminal"),
                "bad_frames": int(res.get("bad_frames") or 0), "status_events": list(res.get("status_events") or []),
                **reduce_trace(trace),
                "trace_thinks": trace_thinks(trace),
                "source_passages": {}, "source_passages_captured": False,
            }
            finished = res.get("http") == 200 and not timed_out and not errors
            if t["expect"].get("code") and finished:
                workdir = os.path.join(out_dir, "code", case["id"], f"r{repeat}", f"t{ti + 1}")
                result["code_result"] = _run_code(t["expect"]["code"], result["answer"], workdir, toolchain, no_code)
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
            if timed_out and bound_by == "deadline":
                control.deadline_reached = True
                raise DeadlineCut(f"--deadline came during turn {ti + 1}; the generation was stopped")
            if timed_out:
                raise CaseTimeout(f"the case ran past --case-timeout-s {case_timeout_s:g}; the generation was stopped")
            if errors:
                raise StreamFailed(f"the stream ended with {json.dumps(errors[0], ensure_ascii=False)[:200]}")
            history += [{"role": "user", "content": t["message"]}, {"role": "assistant", "content": result["answer"]}]
    except Exception as exc:  # noqa: BLE001 — a crashed case scores 0, with the reason
        if rec["stop"] is None and client is not None and (opened or isinstance(exc, (CaseTimeout, httpx.HTTPError))):
            # the generation is detached: a stream we stopped reading would
            # otherwise hold one of the stack's two inference slots. Sent for
            # an interrupted case too, in case its POST /chat reached the
            # server after main()'s stop did.
            rec["stop"] = client.stop(conv)
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()[-2000:]
        log(f"  {case['id']} r{repeat} ERROR {rec['error']}")
    except BaseException:
        # this ends the run (main() handles it when the future yields it);
        # halt first, or this worker takes the next case meanwhile
        control.halt()
        raise
    finally:
        control.end(conv)
    return rec


def _sort(cases: List[dict]) -> List[dict]:
    return sorted(cases, key=lambda r: (ORDER.get(r["id"], len(ORDER)), int(r.get("repeat") or 0)))


def _write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def rescore_refusal(results: dict) -> Optional[str]:
    """Why --rescore must not re-score this run without --force-rescore, or None."""
    stored = (results.get("conditions") or {}).get("eval_set_sha256")
    current = eval_set_sha256()
    if stored == current:
        return None
    return (f"the run was made with eval set {str(stored)[:12]}..., the current set is {current[:12]}...: its "
            "answers were given to other questions or checks. Pass --force-rescore to re-score it anyway "
            "(conditions.rescored records that)")


def rescore(results: dict) -> dict:
    """Re-run every check over the stored answers; nothing is called.

    conditions.rescored says when and against which set; a coding turn's
    stored code_result is scored as it is (the code is not run again), and
    the record's `rescore_note` says so."""
    stored = (results.get("conditions") or {}).get("eval_set_sha256")
    current = eval_set_sha256()
    for rec in results.get("cases") or []:
        case = ES.BY_ID.get(rec["id"])
        if case is None:
            continue
        rec.setdefault("workload", WORKLOAD.get(rec["id"]))
        reused = []
        for ti, (t, tr) in enumerate(zip(case["turns"], rec.get("turns") or [])):
            tr["expect"] = t["expect"]
            if t["expect"].get("code") and (tr.get("result") or {}).get("code_result") is not None:
                reused.append(ti + 1)
            tr["checks"] = score_turn(case, t, tr["result"])
        if reused:
            rec["rescore_note"] = (f"turn(s) {reused}: code_result is the original run's, re-scored as stored; "
                                   "the code was not run again")
    results.setdefault("conditions", {})["rescored"] = {
        "rescored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "eval_set_sha256": current,
        "original_eval_set_sha256": stored, "forced": stored != current}
    results["cases"] = _sort(results.get("cases") or [])
    return results


# =============================================================== summary ==

def summarise(results: dict) -> dict:
    per_case: Dict[str, dict] = {}
    by_workload: Dict[str, dict] = defaultdict(lambda: {"cases": set(), "records": 0, "records_pass": 0,
                                                         "errors": 0, "turns": 0, "first_answer_s_n": 0,
                                                         "total_s_n": 0})
    by_check: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    fast_turns = thinking = stream_errors = timed_out = no_passages = trace_not_ok = bad_frames = 0
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
            trace_not_ok += int(res.get("trace_status") != "ok")
            bad_frames += int(bool(res.get("bad_frames")))
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
            "turns_trace_not_ok": trace_not_ok, "turns_with_bad_frames": bad_frames,
            "interrupted": bool(results.get("interrupted")), "deadline_reached": bool(results.get("deadline_reached")),
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
          f"without source passages {h['turns_without_source_passages']}, trace not ok {h['turns_trace_not_ok']}, "
          f"with bad frames {h['turns_with_bad_frames']}")
    if h["interrupted"] or h["deadline_reached"]:
        print("the run did not finish: " + ("interrupted" if h["interrupted"] else "--deadline reached"))
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
    ap.add_argument("--base", default="", help="the orchestrator, http(s)://127.0.0.1:PORT or localhost:PORT only")
    ap.add_argument("--email", default="", help="the account (default: AIQ_EMAIL)")
    ap.add_argument("--password-file", default="", help="a file holding the password")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    ap.add_argument("--only", default="", help="comma-separated case ids, e.g. EV01,RQ03")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1,
                    help=f"requests in flight, 1..{MAX_WORKERS}; use 1 for a baseline")
    ap.add_argument("--out", default="", help="the run directory (default: runs/evalset-<stamp>)")
    ap.add_argument("--label", default="", help="free text naming the conditions: stack, image, topology, load")
    ap.add_argument("--rescore", default="", help="a run directory to re-score without calling anything")
    ap.add_argument("--force-rescore", action="store_true",
                    help="re-score even when the run was made with a different eval set")
    ap.add_argument("--case-timeout-s", type=float, default=DEFAULT_CASE_TIMEOUT_S,
                    help="the longest one case may take (default 2400: above the server's GEN_WALL_CLOCK_S 1800)")
    ap.add_argument("--deadline", default="", help="HH:MM today, host local time (IST): no case starts after it, "
                    "and a case running then is cut; refused once it has passed or when more than 12 h away")
    ap.add_argument("--not-before", default="", help="HH:MM today, host local time (IST): refuse to start earlier")
    ap.add_argument("--tz-ok", action="store_true",
                    help="allow --deadline / --not-before on a host whose UTC offset is not +05:30 (recorded)")
    ap.add_argument("--allow-used-account", action="store_true",
                    help="run on an account that already has conversations or saved facts (recorded)")
    ap.add_argument("--no-code", action="store_true",
                    help="run no model-written code; the coding turns' code_runs and code_correct then fail")
    ap.add_argument("--dry-run", action="store_true", help="print each request's shape; no network")
    ap.add_argument("--code-root", default=os.environ.get("AIQ_RUNTIME", os.path.join(HERE, ".runtime")),
                    help="where the coding cases' scratch toolchain lives (as run.py's --code-root)")
    return ap


def _refuse(message: str) -> int:
    print(f"refusing to run: {message}", file=sys.stderr, flush=True)
    return EXIT_REFUSED


def _finish(results: dict, t0: float, out: str, control: RunControl) -> dict:
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results["seconds"] = round(time.perf_counter() - t0)
    results["deadline_reached"] = bool(control.deadline_reached)
    _write_json(os.path.join(out, "results.json"), results)
    s = summarise(results)
    _write_json(os.path.join(out, "summary.json"), s)
    return s


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.rescore:
        path = os.path.join(args.rescore, "results.json")
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        why = rescore_refusal(stored)
        if why and not args.force_rescore:
            return _refuse(why)
        results = rescore(stored)
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
    for flag, text in (("--deadline", args.deadline), ("--not-before", args.not_before)):
        if text:
            _today_at(text, flag, _local_now())  # the format; a dry run checks no more of the window

    if args.dry_run:
        dry_run(todo, base, args.repeats)
        return 0

    try:
        window = run_window(args, _local_now())
    except Refused as exc:
        return _refuse(str(exc))
    deadline = window["deadline"]

    email = args.email or os.environ.get("AIQ_EMAIL", "")
    if not email:
        raise SystemExit("no account: use --email or AIQ_EMAIL")
    password = read_password(args)
    toolchain = code_sandbox.Toolchain(os.path.join(args.code_root, "code")) if code_languages(todo) else None
    sandbox = sandbox_conditions(todo, toolchain, args.no_code)
    if sandbox["needed"] and not args.no_code and not all(sandbox["available"].values()):
        return _refuse(f"the code sandbox cannot run {sorted(k for k, v in sandbox['available'].items() if not v)}: "
                       f"{'; '.join(sandbox['why'].values())}. Fix the sandbox, or pass --no-code (the coding "
                       "turns' code_runs and code_correct then fail)")

    first = EvalClient(base, email, password)
    account = account_state(first, allowed_used=args.allow_used_account)
    print(f"account: {account['conversations']} conversation(s), {account['facts']} saved fact(s)"
          + ("" if account["checked"] else f" (not counted: {account.get('why')})"), flush=True)
    why = account_refusal(account)
    if why:
        return _refuse(why)

    out = args.out or os.path.join(HERE, "runs", "evalset-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    results: Dict[str, Any] = {
        "kind": "evalset", "schema": SCHEMA, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finished": None, "seconds": None, "interrupted": False, "deadline_reached": False,
        "conditions": {"base": base, "harness_commit": harness_commit(), "label": args.label,
                       "workers": args.workers, "repeats": args.repeats, "only": only,
                       "health": health_summary(first), "case_timeout_s": args.case_timeout_s,
                       "eval_set_sha256": eval_set_sha256(), "account": account,
                       "features": features_summary(first.me), "code_sandbox": sandbox,
                       "deadline": deadline.isoformat(timespec="minutes") if deadline else None,
                       "not_before": (window["not_before"].isoformat(timespec="minutes")
                                      if window["not_before"] else None),
                       "utc_offset": window["utc_offset"], "tz_ok": window["tz_ok"]},
        "cases": [],
    }
    results_path = os.path.join(out, "results.json")
    _write_json(results_path, results)
    lock = threading.Lock()
    local = threading.local()
    spare = [first]
    control = RunControl(deadline.timestamp() if deadline else None)

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

    pool = cf.ThreadPoolExecutor(max_workers=args.workers)
    try:
        futs = [pool.submit(run_one, client, c, r, out, toolchain, args.case_timeout_s, log, control, args.no_code)
                for c, r in jobs]
        for fut in cf.as_completed(futs):
            rec = fut.result()
            if rec is None:  # not started: --deadline passed
                continue
            with lock:
                results["cases"] = _sort(results["cases"] + [rec])
                _write_json(results_path, results)
    except BaseException as exc:
        # Ctrl-C / SIGTERM / SIGHUP, or a BaseException out of a worker: start
        # nothing more, keep what finished, stop what is generating, then let
        # it propagate. results.json says "interrupted" BEFORE the first stop
        # goes out, so a stop that hangs cannot hide the interruption.
        in_flight = control.halt()
        pool.shutdown(wait=False, cancel_futures=True)
        results["interrupted"] = True
        results["interrupted_by"] = signal.Signals(_SIGNALLED[-1]).name if _SIGNALLED else type(exc).__name__
        stops = results["stopped_on_interrupt"] = [{"conversation_id": conv, "pending": True} for conv, _ in in_flight]
        _finish(results, t0, out, control)
        sending = -1
        try:
            for sending, (conv, cl) in enumerate(in_flight):
                stops[sending] = {"conversation_id": conv, **cl.stop(conv)}
        finally:  # a second Ctrl-C during the stops still leaves an honest file
            for i, entry in enumerate(stops):
                if entry.pop("pending", None):
                    entry["error"] = ("outcome unknown: interrupted again while this stop was being sent"
                                      if i == sending else "not sent: interrupted again before this stop went out")
            _write_json(results_path, results)
        try:
            print(f"\nINTERRUPTED ({results['interrupted_by']}): {len(results['cases'])} record(s) kept in "
                  f"{results_path}; /chat/stop sent for {len(stops)} conversation(s) in flight",
                  file=sys.stderr, flush=True)
        except (OSError, ValueError):  # SIGHUP: the terminal may be gone
            pass
        raise
    pool.shutdown(wait=True)
    s = _finish(results, t0, out, control)
    print_summary(s)
    return 0


#: the signals received, in order (_on_signal); the last one names the interruption
_SIGNALLED: List[int] = []


def _on_signal(signum, frame) -> None:
    """SIGINT, SIGTERM and SIGHUP all take Ctrl-C's path: KeyboardInterrupt in the main thread."""
    _SIGNALLED.append(signum)
    signal.default_int_handler(signum, frame)


def install_signal_handlers() -> None:
    """For the command line only (main() called in-process changes no
    handler). A signal the runner was started with ignored stays ignored:
    nohup's SIGHUP is the operator asking the run to outlive the terminal."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, _on_signal)


if __name__ == "__main__":
    install_signal_handlers()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(128 + _SIGNALLED[-1] if _SIGNALLED else EXIT_SIGINT)
