#!/usr/bin/env bash
# Install the autopilot runner, its guardrails and its systemd user unit.
# Operator only: it refuses to run inside an autopilot session. Re-run it after
# changing anything under ops/autopilot or ops/deploy/merge_to_dev.sh.
#   ops/autopilot/install.sh                  install and enable (does not start)
#   ops/autopilot/install.sh --start          install, enable and start
#   ops/autopilot/install.sh --restart-after-cycle
#                                             install, then (if the runner is
#                                             active) STOP it, wait for the
#                                             in-flight cycle to finish, and start
#                                             the new code. A plain `systemctl
#                                             restart` would SIGTERM a running
#                                             cycle; use this instead.
#
# NOTE: `systemctl start` on an already-active unit is a no-op, so after a
# re-install the running python runner keeps its OLD code in memory until it is
# restarted. The guard hooks, settings and CYCLE_PROMPT are re-read every call
# or cycle and pick up changes immediately; only autopilot.py itself stays stale
# until a restart. This script warns when that is the case.
set -euo pipefail
if [ "${LLM_AUTOPILOT:-}" = "1" ]; then
    echo "install.sh refuses to run inside an autopilot session" >&2
    exit 1
fi
mode=${1:-}
case "$mode" in
    ""|--start|--restart-after-cycle) ;;
    *) echo "install.sh: unknown option $mode (use --start or --restart-after-cycle)" >&2; exit 2 ;;
esac
SRC=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$SRC/../.." && pwd)
AP=${AP_HOME:-$HOME/.llm-autopilot}
WT=${AP_WORKTREE:-$REPO}
was_active=$(systemctl --user is-active llm-autopilot.service 2>/dev/null || true)
old_runner_hash=$( [ -f "$AP/bin/autopilot.py" ] && sha256sum "$AP/bin/autopilot.py" | cut -d' ' -f1 || true )
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

new_runner_hash=$(sha256sum "$AP/bin/autopilot.py" | cut -d' ' -f1)
runner_changed=0
[ "$was_active" = "active" ] && [ "$old_runner_hash" != "$new_runner_hash" ] && runner_changed=1

graceful_restart() {
    echo "runner is active; letting the current cycle finish before starting the new code (a cycle can run up to its 4 h timeout)..."
    touch "$AP/STOP"
    # the runner finishes its cycle and exits 64; systemd does not restart on 64
    local waited=0 limit=$((5 * 3600))
    while [ "$(systemctl --user is-active llm-autopilot.service 2>/dev/null || true)" = "active" ]; do
        sleep 2
        waited=$((waited + 2))
        if [ "$waited" -ge "$limit" ]; then
            rm -f "$AP/STOP"
            echo "ERROR: the runner did not exit within 5 h after STOP; it is left as-is. Investigate, then start it yourself." >&2
            return 1
        fi
    done
    rm -f "$AP/STOP"
    systemctl --user start llm-autopilot.service
    echo "restarted on the new code; watch with: $AP/bin/status.sh"
}

if [ "$mode" = "--restart-after-cycle" ]; then
    if [ "$was_active" = "active" ]; then
        graceful_restart
    else
        systemctl --user start llm-autopilot.service
        echo "started; watch with: $AP/bin/status.sh"
    fi
elif [ "$mode" = "--start" ]; then
    if [ "$was_active" = "active" ]; then
        echo "WARNING: the runner is already active; 'systemctl start' does nothing." >&2
        [ "$runner_changed" = 1 ] && echo "WARNING: autopilot.py changed but the running process keeps the OLD code. Re-run with --restart-after-cycle to pick it up without killing a cycle." >&2
    else
        systemctl --user start llm-autopilot.service
        echo "started; watch with: $AP/bin/status.sh"
    fi
elif [ "$runner_changed" = 1 ]; then
    echo "WARNING: the runner is active and autopilot.py changed; the running process keeps the OLD code until a restart." >&2
    echo "         Re-run with --restart-after-cycle to pick it up without killing a cycle (a plain 'systemctl restart' SIGTERMs the current cycle)." >&2
fi
