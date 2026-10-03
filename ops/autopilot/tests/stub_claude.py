#!/usr/bin/env python3
"""A stand-in for the `claude` CLI for the runner's acceptance tests.

Each `-p` invocation consumes the next entry of the JSON list in $STUB_SCENARIO
(the last entry repeats) and records its arguments in $STUB_CALLS. It costs no
usage. Behaviours: success, limit (with "text"), crash (with "rc"), auth,
max_turns, sleep (with "seconds", then success).
"""
import json
import os
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
    fh.write(json.dumps({"n": n, "args": args, "cycle": os.environ.get("LLM_AUTOPILOT_CYCLE"), "test_db": os.environ.get("TEST_DATABASE_ALLOWED_HOSTS")}) + "\n")


def emit(obj):
    print(json.dumps(obj), flush=True)


def stopfailure(kind):
    with open(os.path.join(os.environ["AP_HOME"], "stopfailure.jsonl"), "a") as fh:
        fh.write(json.dumps({"error_type": kind, "cycle": os.environ.get("LLM_AUTOPILOT_CYCLE")}) + "\n")


emit({"type": "system", "subtype": "init", "session_id": f"stub-{n}"})
kind = step["kind"]
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
