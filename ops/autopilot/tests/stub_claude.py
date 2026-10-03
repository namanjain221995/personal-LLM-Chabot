#!/usr/bin/env python3
"""A stand-in for the `claude` CLI for the runner's acceptance tests.

Each `-p` invocation consumes the next entry of the JSON list in $STUB_SCENARIO
(the last entry repeats) and records its arguments in $STUB_CALLS. It costs no
usage. Behaviours: success, limit (with "text"), crash (with "rc"), auth,
max_turns, sleep (with "seconds", then success), sigterm (the cycle is killed
by SIGTERM, as a service restart or a stray kill would do).
"""
import json
import os
import signal
import sys
import time

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    sys.exit(0)
scenario = json.load(open(os.environ["STUB_SCENARIO"]))
counter = os.environ["STUB_SCENARIO"] + ".n"
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
step = scenario[min(n, len(scenario) - 1)]
with open(os.environ["STUB_CALLS"], "a") as fh:
    fh.write(json.dumps({"n": n, "args": args, "cycle": os.environ.get("LLM_AUTOPILOT_CYCLE"), "test_db": os.environ.get("TEST_DATABASE_ALLOWED_HOSTS"),
                         "bg_wait_ceiling_ms": os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")}) + "\n")


def emit(obj):
    print(json.dumps(obj), flush=True)


def stopfailure(kind):
    with open(os.path.join(os.environ["AP_HOME"], "stopfailure.jsonl"), "a") as fh:
        fh.write(json.dumps({"error_type": kind, "cycle": os.environ.get("LLM_AUTOPILOT_CYCLE")}) + "\n")


emit({"type": "system", "subtype": "init", "session_id": f"stub-{n}"})
kind = step["kind"]
if kind == "badjson":
    # a non-object JSON line must not crash the runner (R12)
    print("[]", flush=True)
    print("12345", flush=True)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3})
    sys.exit(0)
if kind == "multi_results_then_sleep":
    # several intermediate result events, then linger so a SIGTERM interrupts it (R2)
    for t in step.get("turns", [45, 1]):
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "waiting", "num_turns": t})
    time.sleep(step.get("seconds", 30))
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3})
    sys.exit(0)
if kind == "sigterm":
    # killed mid-cycle by SIGTERM, without the runner itself being signalled
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(30)
    sys.exit(0)
if kind == "rate_event_limit":
    # the structured rate_limit_event the stream carries, with a reset epoch (R10)
    emit({"type": "system", "subtype": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": step["resets_at"]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your session limit", "num_turns": 1})
    stopfailure("rate_limit")
    sys.exit(1)
if kind == "sleep":
    time.sleep(step.get("seconds", 1))
    kind = "success"
if kind == "success":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3})
    sys.exit(0)
if kind == "limit":
    text = step.get("text", "Claude AI usage limit reached")
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": text, "num_turns": 1})
    stopfailure("rate_limit")
    sys.exit(1)
if kind == "auth":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login", "num_turns": 0})
    stopfailure("authentication_failed")
    sys.exit(1)
if kind == "max_turns":
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 150})
    sys.exit(1)
print("Segmentation fault (stub)", file=sys.stderr)
sys.exit(step.get("rc", 1))
