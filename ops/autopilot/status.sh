#!/usr/bin/env bash
# Show the autopilot runner's heartbeat, recent events and the integration branch.
AP=${AP_HOME:-$HOME/.llm-autopilot}
WT=${AP_WORKTREE:-$HOME/work/llm-dev}
python3 - "$AP/heartbeat.json" <<'PY'
import datetime, json, sys
try:
    hb = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"no heartbeat ({type(exc).__name__})"); sys.exit(0)
age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(hb["ts"])).total_seconds()
print(f"state        {hb.get('state')}   (heartbeat {age:.0f}s ago, pid {hb.get('pid')})")
for k in ("cycle", "current_task", "last_outcome", "last_commit", "next_wake", "last_error", "log_file", "started_at", "limit_wait_hours_total"):
    print(f"{k:13s}{hb.get(k)}")
PY
printf 'service      %s\n' "$(systemctl --user is-active llm-autopilot.service 2>/dev/null)"
for f in STOP PAUSE; do [ -e "$AP/$f" ] && printf 'flag         %s is set\n' "$f"; done
echo "== last events"; tail -n 6 "$AP/events.jsonl" 2>/dev/null
echo "== autopilot/dev (git log -5)"; git -C "$WT" log -5 --oneline --decorate autopilot/dev 2>/dev/null
if [ -s "$AP/NEEDS_HUMAN.runtime.md" ]; then echo "== runtime NEEDS_HUMAN (tail)"; tail -n 15 "$AP/NEEDS_HUMAN.runtime.md"; fi
