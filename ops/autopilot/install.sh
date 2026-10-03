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
#                                             cycle; use this instead. Ctrl-C
#                                             during the wait removes the STOP
#                                             file it made and says whether the
#                                             runner is still up.
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
SRC=$(dirname -- "$0")
SRC=$(cd "$SRC" && pwd)
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
# The renderer writes a new read-only file next to the installed settings and
# renames it into place, so a cycle starting meanwhile reads the old settings or
# the new ones, never a half-written file; a failed render changes nothing.
python3 -I - "$SRC/settings.autopilot.template.json" "$AP/host.json" "$AP" "$AP/settings.autopilot.json" <<'PY'
import json, os, sys, tempfile
template, host_path, ap_home, out = sys.argv[1:5]
with open(template, encoding="utf-8") as fh:
    settings = json.loads(fh.read().replace("{{AP_HOME}}", ap_home))
with open(host_path, encoding="utf-8") as fh:
    host = json.load(fh)
env = [e for e in settings["autoMode"]["environment"] if e != "{{HOST_DETAILS}}"]
env += host.get("automode_environment_private") or []
settings["autoMode"]["environment"] = env
if "{{" in json.dumps(settings):
    sys.exit(f"install.sh: unrendered placeholder in the settings; {out} is unchanged")
fd, tmp = tempfile.mkstemp(prefix=".settings.autopilot.", suffix=".tmp", dir=os.path.dirname(os.path.abspath(out)))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o444)
    os.replace(tmp, out)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(f"rendered {out}: {len(settings['permissions']['deny'])} deny rules, {len(env)} environment entries")
PY
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
    # Ctrl-C, TERM or a closed terminal during the wait must not leave the
    # runner to exit on our STOP file and stay down without a word. With the
    # terminal gone, a write to it (the Hangup line bash prints itself
    # included) fails instead of killing this shell with SIGPIPE before the
    # handler runs.
    restart_stop_created=0
    trap '' PIPE
    trap 'restart_wait_interrupted 130' INT
    trap 'restart_wait_interrupted 143' TERM
    trap 'restart_wait_interrupted 129' HUP
    if [ ! -e "$AP/STOP" ]; then
        restart_stop_created=1
        touch "$AP/STOP"
    fi
    # the runner finishes its cycle and exits 64; systemd does not restart on 64
    local waited=0 limit=$((5 * 3600))
    while [ "$(systemctl --user is-active llm-autopilot.service 2>/dev/null || true)" = "active" ]; do
        sleep 2
        waited=$((waited + 2))
        if [ "$waited" -ge "$limit" ]; then
            if [ "$restart_stop_created" = 1 ]; then rm -f "$AP/STOP"; fi
            trap - INT TERM HUP PIPE
            echo "ERROR: the runner did not exit within 5 h after STOP; it is left as-is. Investigate, then start it yourself." >&2
            return 1
        fi
    done
    # From here until the runner is started, INT, TERM and HUP are ignored (by
    # systemctl too, which inherits that), so an interrupt cannot leave STOP
    # removed but the runner not started. One that came just before this line
    # still finds the handler, which removes STOP itself and says the runner
    # is down.
    trap '' INT TERM HUP
    rm -f "$AP/STOP"
    systemctl --user start llm-autopilot.service
    trap - INT TERM HUP PIPE
    echo "restarted on the new code; watch with: $AP/bin/status.sh"
}

restart_wait_interrupted() {
    # Further INT, TERM and HUP are ignored from here on (and by the commands
    # below, which inherit that): Ctrl-C pressed twice, or a closing terminal
    # that delivers a second hangup, must not kill the shell before STOP is gone.
    trap '' INT TERM HUP
    local code=$1 state
    set +e  # a message that cannot be written must not stop the cleanup or change the exit code
    if [ "${restart_stop_created:-0}" = 1 ]; then
        rm -f "$AP/STOP"
        echo "install.sh: interrupted while waiting for the cycle to finish; removed the STOP file it created." >&2
    else
        echo "install.sh: interrupted while waiting for the cycle to finish; $AP/STOP was there before this run and is left in place." >&2
    fi
    state=$(systemctl --user is-active llm-autopilot.service 2>/dev/null || true)
    if [ "$state" = active ]; then
        if [ "${restart_stop_created:-0}" = 1 ]; then
            echo "The runner is still active and keeps running its OLD code; re-run install.sh --restart-after-cycle to restart it on the new code." >&2
        else
            echo "The runner is still active; because of that STOP file it exits after its current cycle and stays down until you remove STOP and start it." >&2
        fi
    else
        echo "The runner is not active (${state:-unknown}); if it exited on STOP it stays down. Start it with: systemctl --user start llm-autopilot.service" >&2
        if [ -e "$AP/STOP" ]; then echo "(remove $AP/STOP first, or it exits again at once)" >&2; fi
    fi
    exit "$code"
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
