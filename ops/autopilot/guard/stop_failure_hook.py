#!/usr/bin/python3 -I
"""Autopilot StopFailure hook: record why a cycle's turn failed.

Claude Code runs this when a turn ends on an API error (rate_limit,
authentication_failed, overloaded, server_error, ...). Its output and exit code
are ignored, so it only appends one JSON line to ~/.llm-autopilot/stopfailure.jsonl,
which the runner reads to choose between waiting for a usage reset, backing off,
and asking the operator to log in again. The payload carries no reset time; the
runner reads that from the cycle's result text.
"""

import datetime
import json
import os
import pwd
import sys

HOME = pwd.getpwuid(os.getuid()).pw_dir
OUT = os.path.join(HOME, ".llm-autopilot", "stopfailure.jsonl")
KEEP = ("session_id", "hook_event_name", "error_type", "agent_id", "agent_type", "cwd")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    rec = {k: payload.get(k) for k in KEEP if k in payload}
    rec["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rec["cycle"] = os.environ.get("LLM_AUTOPILOT_CYCLE")
    try:
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
