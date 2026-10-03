#!/usr/bin/env bash
# Install the autopilot runner, its guardrails and its systemd user unit.
# Operator only: it refuses to run inside an autopilot session. Re-run it after
# changing anything under ops/autopilot or ops/deploy/merge_to_dev.sh.
#   ops/autopilot/install.sh            install and enable (does not start)
#   ops/autopilot/install.sh --start    install, enable and start
set -euo pipefail
if [ "${LLM_AUTOPILOT:-}" = "1" ]; then
    echo "install.sh refuses to run inside an autopilot session" >&2
    exit 1
fi
SRC=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$SRC/../.." && pwd)
AP=${AP_HOME:-$HOME/.llm-autopilot}
WT=${AP_WORKTREE:-$REPO}
[ -f "$AP/host.json" ] || { echo "missing $AP/host.json: copy ops/autopilot/host.example.json there (mode 600) and fill it in" >&2; exit 1; }
[ -f "$AP/MASTER_PROMPT.md" ] || { echo "missing $AP/MASTER_PROMPT.md" >&2; exit 1; }
install -d -m 700 "$AP" "$AP/agent" "$AP/agent/private"
install -d -m 755 "$AP/logs"
for d in bin guard; do
    [ -d "$AP/$d" ] && chmod u+w "$AP/$d" "$AP/$d"/* 2>/dev/null || true
    install -d -m 755 "$AP/$d"
done
install -m 0555 "$SRC/guard/guard_hook.py" "$AP/guard/guard_hook.py"
install -m 0555 "$SRC/guard/stop_failure_hook.py" "$AP/guard/stop_failure_hook.py"
install -m 0555 "$SRC/autopilot.py" "$AP/bin/autopilot.py"
install -m 0444 "$SRC/CYCLE_PROMPT.md" "$AP/bin/CYCLE_PROMPT.md"
install -m 0555 "$SRC/status.sh" "$AP/bin/status.sh"
install -m 0555 "$REPO/ops/deploy/merge_to_dev.sh" "$AP/bin/merge_to_dev.sh"
[ -f "$AP/settings.autopilot.json" ] && chmod u+w "$AP/settings.autopilot.json"
python3 -I - "$SRC/settings.autopilot.template.json" "$AP/host.json" "$AP" "$AP/settings.autopilot.json" <<'PY'
import json, sys
template, host_path, ap_home, out = sys.argv[1:5]
text = open(template, encoding="utf-8").read().replace("{{AP_HOME}}", ap_home)
settings = json.loads(text)
host = json.load(open(host_path, encoding="utf-8"))
env = [e for e in settings["autoMode"]["environment"] if e != "{{HOST_DETAILS}}"]
env += host.get("automode_environment_private") or []
settings["autoMode"]["environment"] = env
assert "{{" not in json.dumps(settings), "unrendered placeholder"
json.dump(settings, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"rendered {out}: {len(settings['permissions']['deny'])} deny rules, {len(env)} environment entries")
PY
chmod 0444 "$AP/settings.autopilot.json"
chmod 0555 "$AP/bin" "$AP/guard"
UNIT="$HOME/.config/systemd/user/llm-autopilot.service"
install -d "$(dirname "$UNIT")"
sed -e "s#@AP_HOME@#$AP#g" -e "s#@WORKTREE@#$WT#g" -e "s#@HOME@#$HOME#g" "$SRC/llm-autopilot.service" > "$UNIT"
systemd-analyze --user verify "$UNIT"
systemctl --user daemon-reload
systemctl --user enable llm-autopilot.service
echo "installed: $UNIT (enabled)"
if [ "${1:-}" = "--start" ]; then
    systemctl --user start llm-autopilot.service
    echo "started; watch with: $AP/bin/status.sh"
fi
