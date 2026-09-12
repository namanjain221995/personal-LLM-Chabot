#!/usr/bin/env bash
# Engine failure drills for the two-node vLLM pair (docs/availability/CONTRACT.md).
#
#   scripts/recovery-tests/engine_failure_drills.sh --list
#   scripts/recovery-tests/engine_failure_drills.sh --only 3 --yes
#   scripts/recovery-tests/engine_failure_drills.sh --only 5,14,15 --yes
#   scripts/recovery-tests/engine_failure_drills.sh --only 16 --yes      # 3 restarts, whole budget
#   scripts/recovery-tests/engine_failure_drills.sh --only 17 --yes      # cache clear, cold start
#   scripts/recovery-tests/engine_failure_drills.sh --all --yes
#   scripts/recovery-tests/engine_failure_drills.sh --selftest             # no cluster: the drill-15 sentence round-trip
#
# Each drill breaks one thing on purpose and then PROVES the recovery with a
# deterministic barrier: the controller's /state reaching a named state within
# a timeout, a Prometheus query returning a named value, a container's
# StartedAt moving (or not moving), a REAL completion. Never a bare sleep as
# proof -- a sleep is only ever the interval between two polls.
#
# Every drill writes a record under .runtime/drills/<utc>/ with UTC and
# Asia/Kolkata timestamps on every line, so the record reads next to the
# incident reports (UTC) and next to the people who were there (IST).
#
# DESTRUCTIVE. Drills 3-7 kill a rank, an engine or an API process and cost
# 4-10 minutes of no inference each while the pair reloads; each one also
# spends one of the controller's RECOVERY_BUDGET (3 per hour, contract §6.3)
# -- the script checks the budget before every destructive drill and stops,
# rather than drive the controller into DOWN/budget_exhausted. Run them in an
# ANNOUNCED window, never during someone else's session, never from a deploy.
# --yes is required for anything but --list; the engine recovery lock must be
# free (a recovery or a deploy in progress means: not now).
#
# Drills 8 and 9 (network blips) need a person at the worker's console and
# are printed as manual procedures; 13 (browser closure) is
# scripts/recovery-tests/restart_drill.py against the e2e stack.
#
# Credentials for the orchestrator assertions (drills 5, 14, 15) come from
# DRILL_EMAIL / DRILL_PASSWORD (VIDEO_SMOKE_EMAIL / VIDEO_SMOKE_PASSWORD are
# accepted too, as in README.md). Without them those assertions are SKIPPED
# and say so; the infrastructure half of drill 5 still runs.
#
# --selftest runs drill 15's sentence check against a local fake orchestrator
# (stdlib python3, no cluster, no --yes): the helper's JSON must carry the
# queued sentence byte for byte. On 2026-09-12 the helper printed it with
# json.dumps' default ensure_ascii=True -- the em dash came out as \u2014 and
# the drill's grep -F for the literal sentence failed although the person had
# read it. The self-test feeds the sentence, as app/sse.py puts it on the wire,
# through the same parser and the same assertion function the drill uses.
#
# Drills 16 and 17 restart the pair on purpose, under the engine lock, and
# assert the GDN prefill kernel line on BOTH ranks (candidate B,
# docs/availability/CANDIDATE-B.md): GDN_EXPECT is the exact log text,
# derived from CLUSTER_GDN_PREFILL_BACKEND unless set explicitly.
#
# The drill functions are dispatched by name ("drill_$n"), which shellcheck
# cannot see; hence the SC2317/SC2329 waiver for this file (every line of
# every drill function would otherwise be "unreachable").
# shellcheck disable=SC2317,SC2329
# shellcheck source=../lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/../lib/cluster-common.sh"
# shellcheck source=../lib/engine-lock.sh
. "$CLUSTER_LIB_DIR/engine-lock.sh"

CONTROLLER_URL="${ENGINE_CONTROLLER_URL:-http://127.0.0.1:9838}"
PROM_URL="${PROMETHEUS_URL:-http://127.0.0.1:${PROMETHEUS_PORT:-9090}}"
ORCH_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:${ORCHESTRATOR_PORT:-8080}}"
HEAD_CTR="${ENGINE_HEAD_CONTAINER:-sf-local-ai-vllm-1}"
WORKER_CTR="sf-local-ai-worker-vllm-worker-1"
PROM_CTR="sf-local-ai-prometheus-1"
CONTROLLER_CTR="sf-local-ai-engine-controller-1"
ORCH_CTR="sf-local-ai-orchestrator-1"
COLD_START_BUDGET_S="${ENGINE_COLD_START_BUDGET_S:-900}"
# Detection must beat what it replaces: the old watchdog needed two 120 s
# probe timeouts; the executor took 4 m 51 s on 2026-09-11. 30 s is the
# assignment's bar for a killed rank the sentinel can see directly.
DETECT_BUDGET_S="${DRILL_DETECT_BUDGET_S:-30}"
# How long drill 1 keeps Prometheus down. 150 s > the 2 m absent_over_time
# window of VllmMonitoringUnknown (contract §7.4), so the warning CAN fire.
DRILL1_OUTAGE_S="${DRILL1_OUTAGE_S:-150}"
DRILL_EMAIL="${DRILL_EMAIL:-${VIDEO_SMOKE_EMAIL:-}}"
DRILL_PASSWORD="${DRILL_PASSWORD:-${VIDEO_SMOKE_PASSWORD:-}}"
# The exact status line the orchestrator emits while the primary is not READY
# (contract §8.3, strict one-model mode) -- an em dash, no spaces around it.
QUEUED_SENTENCE='Main model is recovering—your request is safely queued.'
# Drill 16/17: the GDN prefill kernel line each rank logs at start-up
# (vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn.py, both the
# pinned build and candidate B). With CLUSTER_GDN_PREFILL_BACKEND=flashinfer
# the candidate resolves FlashInfer on GB10; the pinned build logs the
# Triton/FLA line even when flashinfer is requested -- which is exactly what
# the drill is there to show. Set GDN_EXPECT to assert another line.
GDN_EXPECT="${GDN_EXPECT:-}"

# ------------------------------------------------------------------ catalogue
# number|slug|kind|one line. kind: auto (runs), manual (printed procedure),
# pointer (another script), companion (runs inside another drill).
DRILLS='1|stop-prometheus-scrape-only|auto|stop Prometheus only: inference continues, /state stays READY, the dashboard shows MONITORING_UNKNOWN, never DOWN
2|restart-monitoring-only|auto|scripts/monitoring.sh restart: no inference container is recreated (StartedAt unchanged on head, worker, controller, orchestrator)
3|kill-worker-rank-process|auto|kill -9 VLLM::Worker_TP1 on the worker: RECOVERING within 30 s, READY within the cold-start budget, no stale worker, real canary
4|kill-head-enginecore|auto|kill -9 VLLM::EngineCore in the head: /health 5xx seen as head_engine_dead, coordinated recovery of the pair
5|kill-head-api|auto|kill -9 the vllm serve API process in the head: recovery, and one orchestrator chat POSTed through the outage is queued (the queued sentence) and resumes on the primary once READY
6|restart-head-only|auto|docker restart the head (as an operator would, without the lock): the controller/sentinel re-pair the worker
7|restart-worker-only|auto|restart the worker container only: the head loses rank 1 and the controller restarts the pair in order
8|management-network-blip|manual|drop the worker management link for 20 s: DEGRADED (sentinel unreachable), never RECOVERING; inference continues
9|roce-blip|manual|drop RoCE rail A for 5 s: NCCL stalls, WEDGED or recovery, evidence captured
13|browser-closure|pointer|the person closes the tab mid-answer: scripts/recovery-tests/restart_drill.py (e2e stack)
14|duplicate-client-retry|companion|two POST /chat with the same intent_id during drill 5: exactly one assistant message
15|queued-continuity|companion|during drill 5: a chat POSTed in the outage is accepted, shows the queued sentence, and the SAME generation completes exactly once after READY (strict one-model mode)
16|tp2-repeated-startup|auto|3 consecutive coordinated restarts via scripts/cluster-recover.sh: each reaches READY and BOTH ranks log the expected GDN prefill kernel line (GDN_EXPECT); spends 3 recoveries of the budget
17|kernel-cache-stale|auto|scripts/cluster-recover.sh --clear-kernel-cache --yes: both kernel-cache volumes emptied, the cold start (full recompile) succeeds within the cold budget'

list_drills() {
  printf '%-3s %-30s %-9s %s\n' "#" "drill" "kind" "what it proves"
  printf '%s\n' "$DRILLS" | while IFS='|' read -r n slug kind line; do printf '%-3s %-30s %-9s %s\n' "$n" "$slug" "$kind" "$line"; done
  echo
  echo "auto      runs here, destructive, needs --yes and an announced window"
  echo "manual    printed as a procedure for a person at the worker's console"
  echo "pointer   another script owns it"
  echo "companion runs inside the drill it names (5); --only 14 or 15 runs drill 5"
  echo
  echo "Budget: drills 3-7 each spend one controller recovery (RECOVERY_BUDGET=3/h) and drill 16 spends three"
  echo "(manual recoveries count, contract §6.3). Plan windows, or raise ENGINE_RECOVERY_BUDGET for the window"
  echo "and recreate engine-controller (cli: techsara up recreates it). Drill 17 restarts by hand under the lock"
  echo "with the controller paused, so it spends none."
}

# ------------------------------------------------------------------- options
ONLY=""; ALL=0; YES=0; DRY=0; SELFTEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --list) list_drills; exit 0 ;;
    --selftest) SELFTEST=1 ;;
    --only) ONLY="${2:?--only needs a drill number (or a,b,c)}"; shift ;;
    --only=*) ONLY="${1#--only=}" ;;
    --all) ALL=1 ;;
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,51p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --list)" ;;
  esac
  shift
done
[ -n "$ONLY" ] || [ "$ALL" = 1 ] || [ "$SELFTEST" = 1 ] || { list_drills; echo; die "nothing selected: --only N[,N...] or --all (and --yes), or --selftest"; }

# ------------------------------------------------------------------ records
stamp() { printf '%s / %s IST' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(TZ=Asia/Kolkata date +%H:%M:%S)"; }
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$RUNTIME_DIR/drills/$RUN_ID"
DRILL_LOG=""      # the current drill's record
DRILL_FAILS=0
DRILL_PASSES=0
SUMMARY=()
rec() { # rec MESSAGE -> stdout + the drill record, both clocks
  local line
  line="$(stamp)  $*"
  printf '%s\n' "$line"
  [ -n "$DRILL_LOG" ] && printf '%s\n' "$line" >>"$DRILL_LOG"
  return 0
}
assert_pass() { DRILL_PASSES=$((DRILL_PASSES+1)); rec "  PASS  $*"; }
assert_fail() { DRILL_FAILS=$((DRILL_FAILS+1)); rec "  FAIL  $*"; }
assert_skip() { rec "  SKIP  $*"; }
assert_that() { # assert_that DESCRIPTION CMD... -> PASS/FAIL on the command's status
  local desc="$1"; shift
  if "$@"; then assert_pass "$desc"; else assert_fail "$desc"; fi
}
begin_drill() { # begin_drill N SLUG
  DRILL_FAILS=0; DRILL_PASSES=0
  mkdir -p "$RUN_DIR"
  DRILL_LOG="$RUN_DIR/drill-$1-$2.log"
  {
    echo "drill=$1 slug=$2"
    echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "started_ist=$(TZ=Asia/Kolkata date +%Y-%m-%dT%H:%M:%S)"
    echo "actor=${SUDO_USER:-${USER:-unknown}} host=$(hostname)"
    echo "head=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
    echo "---"
  } >"$DRILL_LOG"
  section "drill $1: $2"
}
end_drill() { # end_drill N SLUG -> summary line + record footer
  local verdict="PASS"
  [ "$DRILL_FAILS" -eq 0 ] || verdict="FAIL"
  { echo "---"; echo "ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "ended_ist=$(TZ=Asia/Kolkata date +%Y-%m-%dT%H:%M:%S)"; echo "passes=$DRILL_PASSES fails=$DRILL_FAILS verdict=$verdict"; } >>"$DRILL_LOG"
  SUMMARY+=("$verdict  drill $1 $2  ($DRILL_PASSES pass, $DRILL_FAILS fail)  $DRILL_LOG")
  rec "verdict: $verdict ($DRILL_PASSES pass, $DRILL_FAILS fail); record $DRILL_LOG"
  DRILL_LOG=""
}

# ------------------------------------------------------------------ reading
controller_get() { curl -fsS -m 5 "$CONTROLLER_URL$1" 2>/dev/null; }
# state_field PATH -> a dotted path into /state, printed; "" when absent.
state_field() {
  controller_get /state | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
cur = d
for part in sys.argv[1].split("."):
    if not isinstance(cur, dict) or part not in cur:
        print(""); sys.exit(0)
    cur = cur[part]
print("" if cur is None else (str(cur).lower() if isinstance(cur, bool) else cur))
' "$1" 2>/dev/null || printf ''
}
state_name() { state_field state; }
budget_remaining() { python3 -c 'import sys; b, a = sys.argv[1:]; print(int(b) - int(a) if b.isdigit() and a.isdigit() else "?")' "$(state_field recovery.budget)" "$(state_field recovery.attempts_in_window)"; }
prom_query() { curl -fsS --max-time 10 --get "$PROM_URL/api/v1/query" --data-urlencode "query=$1" 2>/dev/null || printf '{}'; }
prom_scalar() { prom_query "$1" | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)["data"]["result"]
except Exception:
    r = []
print(r[0]["value"][1] if r else "-")'; }
prom_range_values() { # prom_range_values QUERY START END -> distinct values seen, space separated ("" when none)
  curl -fsS --max-time 15 --get "$PROM_URL/api/v1/query_range" --data-urlencode "query=$1" \
    --data-urlencode "start=$2" --data-urlencode "end=$3" --data-urlencode "step=5" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)["data"]["result"]
except Exception:
    r = []
seen = sorted({v[1] for s in r for v in s.get("values", [])})
print(" ".join(seen))'
}
container_started_at() { local v; v="$(docker inspect "$1" --format '{{.State.StartedAt}}' 2>/dev/null)" || v=""; printf '%s\n' "${v:-absent}"; }
worker_started_at() { local v; v="$(ssh_worker "docker inspect $WORKER_CTR --format '{{.State.StartedAt}}'" 2>/dev/null)" || v=""; printf '%s\n' "${v:-absent}"; }
# unchanged LABEL OLD NEW: PASS when a container kept its StartedAt; an absent
# container is a FAIL, not a match (two "absent"s are not "unchanged").
unchanged() {
  if [ "$2" = absent ] || [ "$3" = absent ]; then assert_fail "$1: container absent (before=$2 after=$3)"
  elif [ "$2" = "$3" ]; then assert_pass "$1 not recreated (StartedAt $2)"
  else assert_fail "$1 StartedAt changed ($2 -> $3)"; fi
}
iso_to_epoch() { python3 -c 'import sys; from datetime import datetime; s=sys.argv[1]; s=s.split(".")[0]+"+00:00" if "." in s else s.replace("Z","+00:00"); print(int(datetime.fromisoformat(s).timestamp()))' "$1" 2>/dev/null || echo 0; }
real_completion() { # one 4-token completion against the head (contract §5 shape, non-streaming)
  local models mid body reply
  models="$(curl -fsS -m 5 "$(api_url)/v1/models" 2>/dev/null)" || return 1
  mid="$(printf '%s' "$models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)" || return 1
  body="$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":4,"temperature":0,"seed":7,"chat_template_kwargs":{"enable_thinking":false}}' "$mid")"
  reply="$(curl -sS -m 60 -H 'Content-Type: application/json' -d "$body" "$(api_url)/v1/chat/completions" 2>/dev/null \
           | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d["choices"][0]["message"].get("content") or "").strip())' 2>/dev/null)" || return 1
  [ -n "$reply" ]
}

# ----------------------------------------------------------------- barriers
# wait_state REGEX TIMEOUT: poll /state until the state name matches. Logs
# every transition it sees, so the record shows the path, not just the end.
wait_state() {
  local want="$1" timeout="$2" t0 now st last=""
  t0="$(date +%s)"
  while :; do
    st="$(state_name)"; [ -n "$st" ] || st="UNREACHABLE"
    if [ "$st" != "$last" ]; then rec "    state=$st step=$(state_field recovery.step) reason=$(state_field reason | cut -c1-70)"; last="$st"; fi
    if printf '%s' "$st" | grep -qE "^($want)$"; then return 0; fi
    now="$(date +%s)"
    [ $((now - t0)) -ge "$timeout" ] && { rec "    barrier TIMEOUT after ${timeout}s waiting for state $want (last: $st)"; return 1; }
    sleep 2
  done
}
# wait_quiescent TIMEOUT: READY|BUSY with no recovery in progress.
wait_quiescent() {
  local timeout="$1" t0 now
  t0="$(date +%s)"
  while :; do
    if printf '%s' "$(state_name)" | grep -qE '^(READY|BUSY)$' && [ "$(state_field recovery.in_progress)" = "false" ]; then return 0; fi
    now="$(date +%s)"; [ $((now - t0)) -ge "$timeout" ] && return 1
    sleep 2
  done
}
wait_http_code() { # wait_http_code URL CODE TIMEOUT
  local url="$1" want="$2" timeout="$3" t0 now code
  t0="$(date +%s)"
  while :; do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    [ "$code" = "$want" ] && return 0
    now="$(date +%s)"; [ $((now - t0)) -ge "$timeout" ] && { rec "    barrier TIMEOUT after ${timeout}s waiting for $url = $want (last $code)"; return 1; }
    sleep 2
  done
}
wait_prom_value() { # wait_prom_value QUERY VALUE TIMEOUT
  local q="$1" want="$2" timeout="$3" t0 now got
  t0="$(date +%s)"
  while :; do
    got="$(prom_scalar "$q")"
    [ "$got" = "$want" ] && return 0
    now="$(date +%s)"; [ $((now - t0)) -ge "$timeout" ] && { rec "    barrier TIMEOUT after ${timeout}s waiting for $q = $want (last $got)"; return 1; }
    sleep 3
  done
}
wait_canary_after() { # wait_canary_after EPOCH TIMEOUT: the controller's own canary succeeded after EPOCH
  local since="$1" timeout="$2" t0 now last
  t0="$(date +%s)"
  while :; do
    last="$(state_field signals.canary.last_success_at | cut -d. -f1)"
    [ -n "$last" ] && [ "$last" != "" ] && [ "${last:-0}" -gt "$since" ] 2>/dev/null && return 0
    now="$(date +%s)"; [ $((now - t0)) -ge "$timeout" ] && { rec "    barrier TIMEOUT after ${timeout}s waiting for a controller canary success after $(date -u -d "@$since" +%H:%M:%SZ)"; return 1; }
    sleep 3
  done
}
wait_started_after() { # wait_started_after LABEL GETTER OLD_ISO TIMEOUT -> the container was (re)started since OLD_ISO
  local label="$1" getter="$2" old="$3" timeout="$4" t0 now cur
  t0="$(date +%s)"
  while :; do
    cur="$("$getter")"
    [ "$cur" != "$old" ] && [ "$cur" != "absent" ] && [ "$(iso_to_epoch "$cur")" -gt "$(iso_to_epoch "$old")" ] && { rec "    $label restarted at $cur"; return 0; }
    now="$(date +%s)"; [ $((now - t0)) -ge "$timeout" ] && { rec "    barrier TIMEOUT after ${timeout}s: $label StartedAt still $cur"; return 1; }
    sleep 2
  done
}

# ---------------------------------------------------------- recovery proof
# The shared second half of every destructive drill: from a break at T0,
# prove detection, coordinated recovery, a real completion and both ranks.
prove_recovery() { # prove_recovery T0 HEAD_OLD WORKER_OLD
  local t0="$1" head_old="$2" worker_old="$3" t_detect t_ready
  if wait_state 'RECOVERING|STARTING|WEDGED|DEGRADED' "$DETECT_BUDGET_S"; then
    t_detect="$(date +%s)"; assert_pass "controller left READY within ${DETECT_BUDGET_S}s (detection latency $((t_detect - t0))s)"
  else
    assert_fail "controller did not react within ${DETECT_BUDGET_S}s"
  fi
  if wait_state 'RECOVERING' 60; then assert_pass "controller entered RECOVERING (incident $(state_field incident.id) category=$(state_field incident.category))"
  else assert_fail "controller never reported RECOVERING (state now $(state_name))"; fi
  if wait_state 'READY|BUSY' $((COLD_START_BUDGET_S + 120)); then
    t_ready="$(date +%s)"; assert_pass "READY again $((t_ready - t0))s after the break (budget ${COLD_START_BUDGET_S}s)"
  else
    assert_fail "not READY within $((COLD_START_BUDGET_S + 120))s of the break; state $(state_name), category $(state_field incident.category)"
  fi
  local head_new worker_new
  head_new="$(container_started_at "$HEAD_CTR")"; worker_new="$(worker_started_at)"
  if [ "$(iso_to_epoch "$head_new")" -gt "$(iso_to_epoch "$head_old")" ]; then assert_pass "head container was restarted ($head_old -> $head_new)"; else assert_fail "head container was NOT restarted (StartedAt $head_new)"; fi
  if [ "$(iso_to_epoch "$worker_new")" -gt "$(iso_to_epoch "$worker_old")" ]; then assert_pass "worker container was restarted ($worker_old -> $worker_new)"; else assert_fail "worker container was NOT restarted (StartedAt $worker_new)"; fi
  # No stale worker: the worker must be the YOUNGER process group member or
  # equal-age, never an old rank waiting at a rendezvous the head has left.
  if [ "$(iso_to_epoch "$worker_new")" -le "$(( $(iso_to_epoch "$head_new") + 60 ))" ]; then assert_pass "worker started before (or with) the head: no stale rank at the rendezvous"; else assert_fail "worker started $(( $(iso_to_epoch "$worker_new") - $(iso_to_epoch "$head_new") ))s AFTER the head: rank 1 rejoined late"; fi
  if [ "$(state_field signals.worker.rank_process_alive)" = "true" ]; then assert_pass "sentinel: VLLM::Worker_TP1 alive"; else assert_fail "sentinel: worker rank not alive"; fi
  assert_that "a real 4-token completion succeeds on the recovered pair" real_completion
  if wait_canary_after "$t0" 120; then assert_pass "controller canary succeeded after the break (ttft $(state_field signals.canary.ttft_s)s)"; else assert_fail "no controller canary success after the break"; fi
  if "$CLUSTER_LIB_DIR/../cluster-verify-engine.sh" --probe >>"$DRILL_LOG" 2>&1; then assert_pass "cluster-verify-engine.sh --probe: both GPUs participated"; else assert_fail "cluster-verify-engine.sh --probe failed (see record)"; fi
  rec "  incident: id=$(state_field incident.id) category=$(state_field incident.category) attempts=$(state_field incident.attempts) budget_remaining=$(budget_remaining)"
}

require_budget() { # require_budget -> 0 when the controller can still recover once
  local left; left="$(budget_remaining)"
  if [ "$left" = "?" ]; then assert_skip "cannot read the recovery budget from /state"; return 1; fi
  if [ "$left" -lt 1 ]; then assert_fail "recovery budget exhausted ($left left of $(state_field recovery.budget) per $(state_field recovery.window_s)s); this drill would drive the controller to DOWN. Wait for the window or raise ENGINE_RECOVERY_BUDGET for the drill window."; return 1; fi
  rec "  recovery budget: $left left"
  return 0
}
precondition_ready() {
  if wait_quiescent 30; then assert_pass "precondition: READY/BUSY and no recovery in progress"; return 0; fi
  assert_fail "precondition: the engine is $(state_name) (recovery in_progress=$(state_field recovery.in_progress)); refusing to break it further"
  return 1
}

# ------------------------------------------------------------ orchestrator
# One POST /chat as a signed-in person, read as SSE, summarised as one JSON
# line: status, answer length, the engine named on the meta, status lines,
# error text. Stdlib only, so it runs on the host python3. The session
# cookie is Secure and pinned by hand for plain http (README.md recipe).
# The line is UTF-8 with ensure_ascii=False, written to the raw stdout
# buffer: the shell greps it for the literal queued sentence (an em dash),
# and json.dumps' default would print that as \u2014 (the 2026-09-12 miss).
orch_chat() { # orch_chat CONVERSATION INTENT MESSAGE TIMEOUT -> json line
  python3 - "$ORCH_URL" "$1" "$2" "$3" "$4" <<'PY'
import http.client, json, os, sys, time, urllib.parse
base, conv, intent, message, timeout = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
u = urllib.parse.urlsplit(base)
out = {"status": 0, "answer_len": 0, "engine": None, "status_lines": [], "error": None, "events": 0, "elapsed_s": 0.0, "first_token_at": None}
def emit(o):
    sys.stdout.buffer.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8")); sys.stdout.buffer.flush()
def conn():
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
try:
    c = conn()
    c.request("POST", "/auth/login", body=json.dumps({"email": os.environ["DRILL_EMAIL"], "password": os.environ["DRILL_PASSWORD"], "remember": True}),
              headers={"Content-Type": "application/json"})
    r = c.getresponse(); r.read()
    if r.status != 200:
        out["error"] = "login HTTP %d" % r.status; emit(out); sys.exit(0)
    cookie = (r.getheader("set-cookie") or "").split(";", 1)[0].strip()
    c.close()
    body = {"message": message, "messages": [{"role": "user", "content": message}], "session_id": conv, "conversation_id": conv,
            "mode": "assistant", "model": "smart", "effort": "fast", "web_search": "off", "intent_id": intent}
    t0 = time.time()
    c = conn()
    c.request("POST", "/chat", body=json.dumps(body), headers={"Content-Type": "application/json", "Cookie": cookie, "Accept": "text/event-stream"})
    r = c.getresponse()
    out["status"] = r.status
    if r.status != 200:
        out["error"] = r.read(400).decode("utf-8", "replace"); out["elapsed_s"] = round(time.time() - t0, 1); emit(out); sys.exit(0)
    kind = None; answer = 0
    for raw in r:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"): kind = line[6:].strip(); continue
        if not line.startswith("data:"): continue
        try: data = json.loads(line[5:].strip() or "{}")
        except ValueError: continue
        out["events"] += 1
        if kind == "token":
            if answer == 0: out["first_token_at"] = round(time.time(), 3)
            answer += len(str(data.get("text") or ""))
        elif kind == "status": out["status_lines"].append(str(data.get("text") or "")[:120])
        elif kind == "meta":
            out["engine"] = data.get("engine") or out["engine"]
        elif kind == "error": out["error"] = str(data.get("text") or data.get("error") or data)[:300]
        elif kind == "done": break
    out["answer_len"] = answer
    out["elapsed_s"] = round(time.time() - t0, 1)
except Exception as exc:  # noqa: BLE001 - the summary IS the point
    out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
emit(out)
PY
}
orch_assistant_count() { # orch_assistant_count CONVERSATION -> number of assistant rows (or "?")
  python3 - "$ORCH_URL" "$1" <<'PY'
import http.client, json, os, sys, urllib.parse
base, conv = sys.argv[1], sys.argv[2]
u = urllib.parse.urlsplit(base)
try:
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    c.request("POST", "/auth/login", body=json.dumps({"email": os.environ["DRILL_EMAIL"], "password": os.environ["DRILL_PASSWORD"], "remember": True}), headers={"Content-Type": "application/json"})
    r = c.getresponse(); r.read()
    cookie = (r.getheader("set-cookie") or "").split(";", 1)[0].strip()
    c.request("GET", "/history/conversations/" + conv, headers={"Cookie": cookie})
    r = c.getresponse(); payload = json.loads(r.read().decode("utf-8", "replace")) if r.status == 200 else {}
    rows = [m for m in payload.get("messages", []) if m.get("role") == "assistant"]
    engines = sorted({str(((m.get("meta") or {}).get("engine")) or "-") for m in rows})
    print("%d %s" % (len(rows), ",".join(engines) or "-"))
except Exception as exc:  # noqa: BLE001
    print("? %s" % type(exc).__name__)
PY
}
json_field() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); v=d.get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }
# assert_queued_sentence CHAT_JSON: drill 15's sentence check -- the exact
# sentence, byte for byte, in orch_chat's JSON line. ONE function, so the
# self-test below exercises the very grep the drill runs.
assert_queued_sentence() {
  if printf '%s' "$1" | grep -qF -- "$QUEUED_SENTENCE"; then assert_pass "15: the person read the queued sentence"
  else assert_fail "15: the queued sentence was not shown (status lines: $(json_field "$1" status_lines))"; fi
}

# --selftest: orch_chat against a local fake orchestrator that answers the
# login and streams one chat exactly as app/sse.py does on the wire (UTF-8,
# the em dash unescaped, a keep-alive comment between frames), then the
# drill's own assertion on the result. Exit 0 when the sentence round-trips.
selftest() {
  local dir pid port out t0 n
  dir="$(mktemp -d)"
  python3 - "$QUEUED_SENTENCE" >"$dir/port" 2>"$dir/server.err" <<'PY' &
import http.server, json, sys
sentence = sys.argv[1]
class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def _send(self, status, body, ctype, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/auth/login":
            self._send(200, b"{}", "application/json", [("Set-Cookie", "ts_session=selftest; Path=/; HttpOnly; Secure")])
            return
        if self.path == "/chat":
            def frame(event, data):
                return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))  # app/sse.py's shape
            body = "".join([
                frame("meta", {"generation_id": "gen-selftest", "intent_id": "int-selftest", "attempt": 1}),
                frame("status", {"text": sentence}),
                ": keep-alive\n\n",
                frame("meta", {"generation_id": "gen-selftest", "intent_id": "int-selftest", "attempt": 2, "engine": "primary"}),
                frame("token", {"text": "ok"}),
                frame("done", {}),
            ]).encode("utf-8")
            self._send(200, body, "text/event-stream")
            return
        self._send(404, b"", "text/plain")
srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PY
  pid=$!
  t0="$(date +%s)"; port=""
  while [ -z "$port" ]; do
    port="$(head -n 1 "$dir/port" 2>/dev/null || true)"
    [ -n "$port" ] && break
    kill -0 "$pid" 2>/dev/null || { cat "$dir/server.err" >&2; die "selftest: the fake orchestrator did not start"; }
    [ $(( $(date +%s) - t0 )) -lt 10 ] || { kill "$pid" 2>/dev/null || true; die "selftest: no port from the fake orchestrator in 10s"; }
    sleep 0.2
  done
  section "selftest: drill 15's queued-sentence round-trip (fake orchestrator on 127.0.0.1:$port)"
  ORCH_URL="http://127.0.0.1:$port"
  out="$(DRILL_EMAIL=selftest DRILL_PASSWORD=selftest orch_chat conv-selftest int-selftest "hello" 20)"
  kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
  rm -rf "$dir"
  rec "  orch_chat: $out"
  if [ "$(json_field "$out" status)" = 200 ]; then assert_pass "the fake chat was accepted (HTTP 200)"; else assert_fail "orch_chat did not get HTTP 200: $(json_field "$out" error)"; fi
  # THE assertion drill 15 runs, on the helper's real output.
  assert_queued_sentence "$out"
  if printf '%s' "$out" | grep -qF -- '\u2014'; then assert_fail "the helper still escapes the em dash as \\u2014 (ensure_ascii)"; else assert_pass "the helper emits the sentence as UTF-8, not as a \\u2014 escape"; fi
  n="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_lines"].count(sys.argv[1]))' "$QUEUED_SENTENCE")"
  if [ "$n" = 1 ]; then assert_pass "status_lines carries the sentence exactly once, decoded"; else assert_fail "status_lines carries the sentence $n times: $(json_field "$out" status_lines)"; fi
  if [ "$(json_field "$out" answer_len)" = 2 ] && [ "$(json_field "$out" engine)" = primary ] && [ -n "$(json_field "$out" first_token_at)" ]; then assert_pass "tokens, engine and first_token_at parsed through the keep-alive comment"; else assert_fail "the parser lost a frame: $out"; fi
  # The negative control: the pre-fix shape (json.dumps with its default
  # ensure_ascii=True) must NOT satisfy the grep -- so the pass above is a
  # proof of the round-trip, not a tautology.
  if python3 -c 'import json,sys; print(json.dumps({"status_lines": [sys.argv[1]]}))' "$QUEUED_SENTENCE" | grep -qF -- "$QUEUED_SENTENCE"; then
    assert_fail "negative control: the ASCII-escaped shape matched the grep, so this self-test proves nothing"
  else assert_pass "negative control: the ASCII-escaped shape (the 2026-09-12 miss) does not match the grep"; fi
  printf '\nselftest: %d passed, %d failed\n' "$DRILL_PASSES" "$DRILL_FAILS"
  [ "$DRILL_FAILS" -eq 0 ]
}

# ==================================================================== drills
drill_1() {
  begin_drill 1 stop-prometheus-scrape-only
  precondition_ready || { end_drill 1 stop-prometheus-scrape-only; return; }
  docker inspect "$PROM_CTR" >/dev/null 2>&1 || { assert_skip "$PROM_CTR is not running here (scripts/monitoring.sh up)"; end_drill 1 stop-prometheus-scrape-only; return; }
  local t_stop t_start values alerts head_old
  head_old="$(container_started_at "$HEAD_CTR")"
  rec "stopping $PROM_CTR (monitoring only; no inference container is touched)"
  [ "$DRY" = 1 ] || docker stop "$PROM_CTR" >/dev/null
  t_stop="$(date +%s)"
  if wait_http_code "$PROM_URL/-/ready" 000 30; then assert_pass "Prometheus is down (connection refused)"; else assert_fail "Prometheus still answers"; fi
  assert_that "inference continues: a real completion succeeds while Prometheus is down" real_completion
  # /state is the controller's own view; it must not depend on Prometheus.
  local n ok=0 total=0 st
  for n in $(seq 1 $((DRILL1_OUTAGE_S / 10))); do
    st="$(state_name)"; total=$((total+1))
    printf '%s' "$st" | grep -qE '^(READY|BUSY)$' && ok=$((ok+1))
    [ "$n" = 1 ] && rec "    /state during the outage: $st (sampling every 10 s for ${DRILL1_OUTAGE_S}s)"
    sleep 10
  done
  if [ "$ok" = "$total" ]; then assert_pass "/state stayed READY/BUSY for all $total samples"; else assert_fail "/state left READY/BUSY in $((total-ok)) of $total samples"; fi
  rec "starting $PROM_CTR"
  [ "$DRY" = 1 ] || docker start "$PROM_CTR" >/dev/null
  t_start="$(date +%s)"
  if wait_http_code "$PROM_URL/-/ready" 200 90; then assert_pass "Prometheus is back"; else assert_fail "Prometheus did not come back"; fi
  if wait_prom_value 'up{job="engine-controller"}' 1 90; then assert_pass "engine-controller scrape target is up again"; else assert_skip "no engine-controller target in Prometheus yet (monitoring workstream: prometheus.yml)"; fi
  values="$(prom_range_values 'cluster:vllm_service_state:code' "$t_stop" "$t_start")"
  rec "    cluster:vllm_service_state:code values inside the outage window: '${values:-<none>}'"
  if printf ' %s ' "$values" | grep -q ' 8 '; then assert_fail "the derived state rendered DOWN (8) while only monitoring was away"; else assert_pass "the outage window carries no DOWN (8): absence, i.e. MONITORING_UNKNOWN"; fi
  alerts="$(prom_range_values 'ALERTS{alertname=~"VllmPrimaryDown|VllmEngineWedged|VllmWorkerRankAbsent"}' "$t_stop" "$((t_start + 60))")"
  if [ -z "$alerts" ]; then assert_pass "no critical inference alert fired for a monitoring-only outage"; else assert_fail "a critical inference alert fired: $alerts"; fi
  rec "    VllmMonitoringUnknown after restart: $(prom_scalar 'ALERTS{alertname="VllmMonitoringUnknown"}') (1=firing/pending, -=absent; informational)"
  unchanged "head" "$head_old" "$(container_started_at "$HEAD_CTR")"
  end_drill 1 stop-prometheus-scrape-only
}

drill_2() {
  begin_drill 2 restart-monitoring-only
  precondition_ready || { end_drill 2 restart-monitoring-only; return; }
  local head_old worker_old ctl_old orch_old
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  ctl_old="$(container_started_at "$CONTROLLER_CTR")"; orch_old="$(container_started_at "$ORCH_CTR")"
  rec "StartedAt before: head=$head_old worker=$worker_old controller=$ctl_old orchestrator=$orch_old"
  rec "scripts/monitoring.sh restart"
  if [ "$DRY" = 1 ] || "$CLUSTER_LIB_DIR/../monitoring.sh" restart >>"$DRILL_LOG" 2>&1; then assert_pass "monitoring.sh restart returned 0"; else assert_fail "monitoring.sh restart failed (see record)"; fi
  if wait_http_code "$PROM_URL/-/ready" 200 120; then assert_pass "Prometheus ready again"; else assert_fail "Prometheus not ready after restart"; fi
  if wait_prom_value 'up{job="vllm-main"}' 1 90; then assert_pass "vllm-main scrape target up again"; else assert_fail "vllm-main target not up after restart"; fi
  unchanged "head" "$head_old" "$(container_started_at "$HEAD_CTR")"
  unchanged "worker" "$worker_old" "$(worker_started_at)"
  unchanged "engine-controller" "$ctl_old" "$(container_started_at "$CONTROLLER_CTR")"
  unchanged "orchestrator" "$orch_old" "$(container_started_at "$ORCH_CTR")"
  if wait_quiescent 30; then assert_pass "/state READY/BUSY throughout"; else assert_fail "/state is $(state_name) after a monitoring restart"; fi
  end_drill 2 restart-monitoring-only
}

drill_3() {
  begin_drill 3 kill-worker-rank-process
  { precondition_ready && require_budget; } || { end_drill 3 kill-worker-rank-process; return; }
  local head_old worker_old t0 pid
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  pid="$(ssh_worker "docker exec $WORKER_CTR pgrep -f 'VLLM::Worker_TP[1]' | head -n 1" 2>/dev/null || true)"
  [ -n "$pid" ] || { assert_fail "no VLLM::Worker_TP1 process in $WORKER_CTR"; end_drill 3 kill-worker-rank-process; return; }
  rec "kill -9 VLLM::Worker_TP1 (pid $pid inside $WORKER_CTR) on the worker"
  t0="$(date +%s)"
  [ "$DRY" = 1 ] || ssh_worker "docker exec $WORKER_CTR kill -9 $pid" || assert_fail "kill failed"
  prove_recovery "$t0" "$head_old" "$worker_old"
  if [ "$(state_field incident.category)" = "worker_rank_dead" ]; then assert_pass "category worker_rank_dead"; else assert_fail "category is '$(state_field incident.category)', expected worker_rank_dead"; fi
  end_drill 3 kill-worker-rank-process
}

drill_4() {
  begin_drill 4 kill-head-enginecore
  { precondition_ready && require_budget; } || { end_drill 4 kill-head-enginecore; return; }
  local head_old worker_old t0 pid
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  pid="$(docker exec "$HEAD_CTR" pgrep -f 'VLLM::EngineCor[e]' 2>/dev/null | head -n 1 || true)"
  [ -n "$pid" ] || { assert_fail "no VLLM::EngineCore process in $HEAD_CTR"; end_drill 4 kill-head-enginecore; return; }
  rec "kill -9 VLLM::EngineCore (pid $pid inside $HEAD_CTR)"
  t0="$(date +%s)"
  [ "$DRY" = 1 ] || docker exec "$HEAD_CTR" kill -9 "$pid" || assert_fail "kill failed"
  prove_recovery "$t0" "$head_old" "$worker_old"
  if printf '%s' "$(state_field incident.category)" | grep -qE '^(head_engine_dead|head_api_dead|canary_timeout|wedged_frozen_tokens)$'; then
    assert_pass "category $(state_field incident.category)"
  else assert_fail "category is '$(state_field incident.category)'"; fi
  end_drill 4 kill-head-enginecore
}

drill_5() {
  begin_drill 5 kill-head-api
  { precondition_ready && require_budget; } || { end_drill 5 kill-head-api; return; }
  local head_old worker_old t0 pid conv intent chat_a chat_b
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  pid="$(docker exec "$HEAD_CTR" pgrep -o -f '/usr/local/bin/vl[l]m serve' 2>/dev/null | head -n 1 || true)"
  [ -n "$pid" ] || { assert_fail "no vllm serve API process in $HEAD_CTR"; end_drill 5 kill-head-api; return; }
  rec "kill -9 vllm serve API (pid $pid inside $HEAD_CTR); strict one-model mode: the chat below must be QUEUED, never answered elsewhere"
  t0="$(date +%s)"
  [ "$DRY" = 1 ] || docker exec "$HEAD_CTR" kill -9 "$pid" || assert_fail "kill failed"
  # Drills 14 + 15 ride on this outage: one intent, sent twice.
  if [ -n "$DRILL_EMAIL" ] && [ -n "$DRILL_PASSWORD" ] && [ "$DRY" != 1 ]; then
    conv="drill-$(date -u +%H%M%S)-$RANDOM"; intent="drill$(date -u +%H%M%S)$RANDOM"
    rec "  orchestrator: POST /chat conv=$conv intent=$intent (twice, 5 s apart) through the outage"
    DRILL_EMAIL="$DRILL_EMAIL" DRILL_PASSWORD="$DRILL_PASSWORD" orch_chat "$conv" "$intent" "Reply with one short sentence about tensor parallelism." 900 >"$RUN_DIR/drill-5-chat-a.json" &
    local pa=$!
    sleep 5
    DRILL_EMAIL="$DRILL_EMAIL" DRILL_PASSWORD="$DRILL_PASSWORD" orch_chat "$conv" "$intent" "Reply with one short sentence about tensor parallelism." 900 >"$RUN_DIR/drill-5-chat-b.json" &
    local pb=$!
  fi
  prove_recovery "$t0" "$head_old" "$worker_old"
  local t_ready; t_ready="$(state_field since | cut -d. -f1)"
  if [ -n "${pa:-}" ]; then
    wait "$pa" || true; wait "${pb:-$pa}" || true
    chat_a="$(cat "$RUN_DIR/drill-5-chat-a.json")"; chat_b="$(cat "$RUN_DIR/drill-5-chat-b.json")"
    rec "  chat A: $chat_a"; rec "  chat B: $chat_b"
    local sa la ea err_a first_a
    sa="$(json_field "$chat_a" status)"; la="$(json_field "$chat_a" answer_len)"; ea="$(json_field "$chat_a" engine)"; err_a="$(json_field "$chat_a" error)"; first_a="$(json_field "$chat_a" first_token_at | cut -d. -f1)"
    # STRICT ONE-MODEL MODE (contract §8.3): the request is ACCEPTED (200),
    # the person reads exactly the queued sentence, nothing else answers,
    # and the same generation resumes when READY returns. A 200 with nothing
    # in it is the blank bubble (RC-2); an error is a broken promise.
    if [ "$sa" = 200 ]; then assert_pass "15: the chat POSTed during the outage was accepted (HTTP 200)"; else assert_fail "15: the chat was refused (HTTP $sa${err_a:+: $err_a})"; fi
    assert_queued_sentence "$chat_a"
    if [ "${la:-0}" -gt 0 ] && [ -z "$err_a" ]; then assert_pass "15: the SAME generation completed after the recovery (${la} chars)";
    elif [ -n "$err_a" ]; then assert_fail "15: the queued chat ended in an error instead of resuming: $err_a";
    else assert_fail "15: the queued chat never completed (HTTP $sa, 0 chars, no error text)"; fi
    if [ "${ea:-primary}" = "primary" ] || [ -z "$ea" ]; then assert_pass "15: the answer came from the primary (engine=${ea:-primary}); no other model answered"; else assert_fail "15: the answer names engine=$ea -- v2 allows no fallback answer"; fi
    if [ -n "$first_a" ] && [ -n "$t_ready" ] && [ "$first_a" -ge "$((t_ready - 30))" ]; then assert_pass "15: the first token arrived after READY returned (queued, then resumed)"; else rec "  15: first token at ${first_a:--}, READY since ${t_ready:--} (informational)"; fi
    local seen; seen="$(prom_range_values 'llm_queued_generations' "$t0" "$(date +%s)")"
    if printf ' %s ' "$seen" | grep -qE ' [1-9][0-9]* '; then assert_pass "15: llm_queued_generations rose above 0 during the outage"; else assert_fail "15: llm_queued_generations never rose above 0 (values: '${seen:-<none>}')"; fi
    # A barrier, not an instant read: the orchestrator drops the gauge at the
    # resume (both halves, app/continuity.py Hold.resume) but Prometheus
    # scrapes it every 15 s (monitoring/prometheus/prometheus.yml, job
    # orchestrator), so the value can lag the completed chat by a scrape.
    if wait_prom_value 'llm_queued_generations' 0 60; then assert_pass "15: the queue drained after recovery (llm_queued_generations back to 0 within 60s)"; else assert_fail "15: llm_queued_generations still $(prom_scalar 'llm_queued_generations') 60s after the queued chat completed"; fi
    # 14: exactly one assistant message for the intent, whichever POST won --
    # and exactly one, not two, after the resume (durability §8.4).
    local count
    count="$(DRILL_EMAIL="$DRILL_EMAIL" DRILL_PASSWORD="$DRILL_PASSWORD" orch_assistant_count "$conv")"
    rec "  14 duplicate-client-retry: assistant rows for $conv = $count"
    if [ "${count%% *}" = 1 ]; then assert_pass "14: one assistant message for one intent sent twice and resumed once"
    else assert_fail "14: ${count%% *} assistant messages for one intent (expected exactly 1)"; fi
  else
    assert_skip "14/15: no DRILL_EMAIL/DRILL_PASSWORD (or --dry-run); the orchestrator assertions were not run"
  fi
  end_drill 5 kill-head-api
}

drill_6() {
  begin_drill 6 restart-head-only
  { precondition_ready && require_budget; } || { end_drill 6 restart-head-only; return; }
  local head_old worker_old t0
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  rec "docker restart -t 10 $HEAD_CTR (an operator's bare restart, deliberately WITHOUT the engine lock)"
  t0="$(date +%s)"
  [ "$DRY" = 1 ] || docker restart -t 10 "$HEAD_CTR" >/dev/null || assert_fail "docker restart failed"
  wait_started_after "head" "container_started_at_head" "$head_old" 60 || true
  # A restarted head is a new process group: the old worker MUST be re-paired
  # (restarted) by the controller/sentinel, else it waits at a rendezvous the
  # head has left. STARTING is acceptable as the first observation.
  if wait_state 'STARTING|RECOVERING|DEGRADED|WEDGED' "$DETECT_BUDGET_S"; then assert_pass "controller saw the head restart within ${DETECT_BUDGET_S}s"; else assert_fail "controller did not notice the head restart"; fi
  if wait_started_after "worker" "worker_started_at" "$worker_old" $((COLD_START_BUDGET_S)); then assert_pass "worker was re-paired (restarted)"; else assert_fail "worker was never restarted: a stale rank"; fi
  if wait_state 'READY|BUSY' $((COLD_START_BUDGET_S + 120)); then assert_pass "READY $(( $(date +%s) - t0 ))s after the restart"; else assert_fail "not READY within the budget (state $(state_name))"; fi
  assert_that "a real completion succeeds" real_completion
  if "$CLUSTER_LIB_DIR/../cluster-verify-engine.sh" --probe >>"$DRILL_LOG" 2>&1; then assert_pass "both GPUs participated"; else assert_fail "cluster-verify-engine.sh --probe failed"; fi
  end_drill 6 restart-head-only
}
container_started_at_head() { container_started_at "$HEAD_CTR"; }

drill_7() {
  begin_drill 7 restart-worker-only
  { precondition_ready && require_budget; } || { end_drill 7 restart-worker-only; return; }
  local head_old worker_old t0
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  rec "scripts/cluster-worker.sh restart --timeout 5 (worker only; the head keeps its old process group)"
  t0="$(date +%s)"
  [ "$DRY" = 1 ] || worker_compose restart --timeout 5 vllm-worker >>"$DRILL_LOG" 2>&1 || assert_fail "worker restart failed"
  prove_recovery "$t0" "$head_old" "$worker_old"
  end_drill 7 restart-worker-only
}

drill_8() {
  begin_drill 8 management-network-blip
  assert_skip "manual: needs a person at the worker's console (a blip that lasts is a lockout). Procedure recorded."
  rec "  PROCEDURE (worker console, announced window):"
  rec "    1. on the head: watch  curl -s $CONTROLLER_URL/state | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[\"state\"], d[\"signals\"][\"worker\"][\"reachable\"])'  every 5 s"
  rec "    2. on the worker: sudo ip link set enP7s7 down; sleep 20; sudo ip link set enP7s7 up   (management LAN only; the RoCE rails stay up)"
  rec "    3. EXPECT: inference continues (NCCL is on RoCE); Prometheus up{node=\"spark-2\"} drops to 0 then returns;"
  rec "       the controller stays READY/BUSY (the sentinel is on the RoCE address, so worker.reachable stays true);"
  rec "       NEVER RECOVERING. If the sentinel were reached over the management LAN this drill would show DEGRADED - that is the point of §6.4's binding rule."
  rec "    4. record: the /state samples and  scripts/monitoring.sh verify  before and after"
  end_drill 8 management-network-blip
}

drill_9() {
  begin_drill 9 roce-blip
  assert_skip "manual: dropping a RoCE rail stalls NCCL for every request in flight; needs a window and a console. Procedure recorded."
  rec "  PROCEDURE (worker console, announced window):"
  rec "    1. rail A interface on the worker: the one carrying CLUSTER_WORKER_IP (${CLUSTER_WORKER_IP:-?}) - scripts/cluster-status.sh prints it"
  rec "    2. sudo ip link set <rail-A ifname> down; sleep 5; sudo ip link set <rail-A ifname> up"
  rec "    3. EXPECT (5 s): NCCL retries on rail B or stalls briefly; the canary TTFT rises; state DEGRADED or a WEDGED->RECOVERING cycle if the"
  rec "       collective did not survive; evidence under .runtime/incidents/. The sentinel is ALSO on rail A, so worker.reachable=false is expected"
  rec "       for the blip and must clear by itself. A 30 s+ blip is an NCCL timeout: expect one recovery, one budget unit."
  rec "    4. record: the /state path, techsara_vllm_generation_frozen_seconds, and whether both rails carried bytes during the next canary"
  end_drill 9 roce-blip
}

# ------------------------------------------------- candidate B: 16 and 17
# gdn_expect: the exact GDN prefill kernel line both ranks must log; from
# GDN_EXPECT, else from the launcher's CLUSTER_GDN_PREFILL_BACKEND.
gdn_expect() {
  if [ -n "$GDN_EXPECT" ]; then printf '%s' "$GDN_EXPECT"; return; fi
  case "$(env_get "$GENERATED_ENV" CLUSTER_GDN_PREFILL_BACKEND 2>/dev/null || true)" in
    flashinfer) printf '%s' 'Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128)' ;;
    *) printf '%s' 'Using Triton/FLA GDN prefill kernel' ;;
  esac
}
# rank_logged_since LABEL SINCE_ISO TEXT: the container logged TEXT after it
# was (re)started at SINCE_ISO (docker logs --since takes RFC3339). The
# worker is read over ssh; neither grep prints more than the matching line.
rank_logged_since() {
  local label="$1" since="$2" text="$3" line
  if [ "$label" = worker ]; then
    line="$(ssh_worker "docker logs --since '$since' $WORKER_CTR 2>&1 | grep -F -m1 -- '$text'" 2>/dev/null || true)"
  else
    line="$(docker logs --since "$since" "$HEAD_CTR" 2>&1 | grep -F -m1 -- "$text" || true)"
  fi
  [ -n "$line" ] && { rec "    $label: $(printf '%s' "$line" | cut -c1-160)"; return 0; }
  return 1
}
wait_cooldown() { # wait_cooldown TIMEOUT: the controller's cooldown_until has passed (a manual recovery is refused during it)
  local timeout="$1" t0 now until
  t0="$(date +%s)"
  while :; do
    until="$(state_field recovery.cooldown_until | cut -d. -f1)"
    now="$(date +%s)"
    { [ -z "$until" ] || [ "$until" = None ] || [ "$until" -le "$now" ]; } 2>/dev/null && return 0
    [ $((now - t0)) -ge "$timeout" ] && return 1
    sleep 5
  done
}

drill_16() {
  begin_drill 16 tp2-repeated-startup
  precondition_ready || { end_drill 16 tp2-repeated-startup; return; }
  local expect left; expect="$(gdn_expect)"
  left="$(budget_remaining)"
  if [ "$left" = "?" ] || [ "$left" -lt 3 ]; then
    assert_fail "this drill spends THREE recoveries (manual ones count, contract §6.3) and the budget has $left left of $(state_field recovery.budget); wait for the window or raise ENGINE_RECOVERY_BUDGET"
    end_drill 16 tp2-repeated-startup; return
  fi
  rec "expected GDN prefill kernel line on both ranks: '$expect'"
  rec "main engine image: $(docker inspect "$HEAD_CTR" --format '{{.Config.Image}}' 2>/dev/null || echo '?')"
  local i head_old worker_old t0 head_new worker_new
  for i in 1 2 3; do
    rec "restart $i/3: waiting for the controller's cooldown, then scripts/cluster-recover.sh (POST /recover, coordinated, under the lock)"
    if ! wait_cooldown 300; then assert_fail "restart $i: the controller's cooldown did not pass in 300s"; break; fi
    head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
    t0="$(date +%s)"
    if [ "$DRY" = 1 ]; then rec "  (dry run) scripts/cluster-recover.sh"; continue; fi
    if "$CLUSTER_LIB_DIR/../cluster-recover.sh" --timeout $((COLD_START_BUDGET_S + 300)) >>"$DRILL_LOG" 2>&1; then
      assert_pass "restart $i: cluster-recover.sh returned 0 (READY with a real completion) $(( $(date +%s) - t0 ))s after the request"
    else
      assert_fail "restart $i: cluster-recover.sh failed (see record); state $(state_name) category $(state_field incident.category)"
      break
    fi
    head_new="$(container_started_at "$HEAD_CTR")"; worker_new="$(worker_started_at)"
    if [ "$(iso_to_epoch "$head_new")" -gt "$(iso_to_epoch "$head_old")" ] && [ "$(iso_to_epoch "$worker_new")" -gt "$(iso_to_epoch "$worker_old")" ]; then
      assert_pass "restart $i: both containers were restarted (head $head_new, worker $worker_new)"
    else
      assert_fail "restart $i: a rank was not restarted (head $head_old -> $head_new, worker $worker_old -> $worker_new)"
    fi
    if rank_logged_since head "$head_new" "$expect"; then assert_pass "restart $i: rank 0 logged the expected GDN prefill kernel line"; else assert_fail "restart $i: rank 0 did not log '$expect' since $head_new"; fi
    if rank_logged_since worker "$worker_new" "$expect"; then assert_pass "restart $i: rank 1 logged the expected GDN prefill kernel line"; else assert_fail "restart $i: rank 1 did not log '$expect' since $worker_new"; fi
    if wait_state 'READY|BUSY' 60; then assert_pass "restart $i: controller READY"; else assert_fail "restart $i: controller state $(state_name)"; fi
    assert_that "restart $i: a real 4-token completion succeeds" real_completion
  done
  rec "  budget remaining after the drill: $(budget_remaining)"
  end_drill 16 tp2-repeated-startup
}

drill_17() {
  begin_drill 17 kernel-cache-stale
  precondition_ready || { end_drill 17 kernel-cache-stale; return; }
  local expect head_old worker_old t0 head_new worker_new incident
  expect="$(gdn_expect)"
  head_old="$(container_started_at "$HEAD_CTR")"; worker_old="$(worker_started_at)"
  rec "scripts/cluster-recover.sh --clear-kernel-cache --yes (both ranks stopped, both kernel-cache volumes emptied, cold start under the lock; the controller is paused for the duration and spends no budget)"
  t0="$(date +%s)"
  if [ "$DRY" = 1 ]; then rec "  (dry run)"; end_drill 17 kernel-cache-stale; return; fi
  if "$CLUSTER_LIB_DIR/../cluster-recover.sh" --clear-kernel-cache --yes --timeout $((COLD_START_BUDGET_S + 300)) >>"$DRILL_LOG" 2>&1; then
    assert_pass "--clear-kernel-cache returned 0: READY with a real completion $(( $(date +%s) - t0 ))s after the start (cold budget ${COLD_START_BUDGET_S}s)"
  else
    assert_fail "--clear-kernel-cache failed (see record); state $(state_name)"
  fi
  if [ $(( $(date +%s) - t0 )) -le "$COLD_START_BUDGET_S" ]; then assert_pass "the cold start (full recompile) fitted the cold budget"; else assert_fail "the cold start took $(( $(date +%s) - t0 ))s, over the ${COLD_START_BUDGET_S}s budget"; fi
  head_new="$(container_started_at "$HEAD_CTR")"; worker_new="$(worker_started_at)"
  if [ "$(iso_to_epoch "$head_new")" -gt "$(iso_to_epoch "$head_old")" ] && [ "$(iso_to_epoch "$worker_new")" -gt "$(iso_to_epoch "$worker_old")" ]; then assert_pass "both ranks were restarted"; else assert_fail "a rank was not restarted"; fi
  incident="$(find "$RUNTIME_DIR/incidents" -maxdepth 1 -type d -name 'manual-*' -newermt "@$t0" 2>/dev/null | sort | tail -n 1)"
  if [ -n "$incident" ] && grep -q '^kernel_cache_cleared=' "$incident/incident.txt" 2>/dev/null; then assert_pass "the incident record names both emptied volumes ($(grep '^kernel_cache_cleared=' "$incident/incident.txt" | cut -d= -f2-))"; else assert_fail "no incident record with kernel_cache_cleared under .runtime/incidents/"; fi
  # A cold cache means a real compile on this start; the head logs its duration.
  if rank_logged_since head "$head_new" "torch.compile took"; then assert_pass "rank 0 recompiled (torch.compile ran on the cold cache)"; else assert_fail "rank 0 logged no torch.compile after the restart: was the cache really emptied?"; fi
  if rank_logged_since head "$head_new" "$expect"; then assert_pass "rank 0 logged the expected GDN prefill kernel line"; else assert_fail "rank 0 did not log '$expect'"; fi
  if rank_logged_since worker "$worker_new" "$expect"; then assert_pass "rank 1 logged the expected GDN prefill kernel line"; else assert_fail "rank 1 did not log '$expect'"; fi
  if wait_state 'READY|BUSY' $((COLD_START_BUDGET_S)); then assert_pass "controller proved the pair after being resumed (READY)"; else assert_fail "controller state $(state_name) after the cache clear"; fi
  assert_that "a real 4-token completion succeeds" real_completion
  end_drill 17 kernel-cache-stale
}

drill_13() {
  begin_drill 13 browser-closure
  assert_skip "pointer: scripts/recovery-tests/restart_drill.py owns this (the tab closes mid-answer, the orchestrator restarts, exactly one answer is stored). Run it against the ISOLATED e2e stack: scripts/e2e-stack.sh, then QA_EMAIL/QA_PASSWORD/QA_FIXTURES scripts/recovery-tests/restart_drill.py"
  end_drill 13 browser-closure
}

# ------------------------------------------------------------------- main
if [ "$SELFTEST" = 1 ]; then selftest && exit 0; exit 1; fi
echo "============================================================"
echo "ENGINE FAILURE DRILLS   ($(stamp))"
echo "============================================================"
echo "THESE ARE DESTRUCTIVE. Drills 3-7 kill an engine process or restart a"
echo "container on purpose and cost minutes of no inference each; each spends"
echo "one of the controller's three recoveries per hour. Announce the window."
echo "Records: $RUN_DIR/"
echo
[ "$YES" = 1 ] || die "refusing without --yes (use --list to read the catalogue)"
cluster_load_settings
require_dual_mode
if engine_lock_is_held; then
  echo "the engine recovery lock is HELD by another actor:" >&2
  engine_lock_holder >&2
  die "a recovery or a deploy is in progress; not now"
fi
controller_get /healthz >/dev/null || die "the engine controller is not answering at $CONTROLLER_URL; the drills prove ITS behaviour, so it must be up (./techsara up)"

selected=()
if [ "$ALL" = 1 ]; then
  # 16 and 17 last: 16 spends the whole recovery budget, 17 needs none.
  selected=(1 2 13 8 9 3 4 5 6 7 17 16)
else
  IFS=', ' read -r -a selected <<<"$ONLY"
fi
# 14 and 15 are companions of 5: selecting them selects 5, once.
want5=0; run=()
for n in "${selected[@]}"; do
  case "$n" in
    14|15) want5=1 ;;
    5) want5=1 ;;
    1|2|3|4|6|7|8|9|13|16|17) run+=("$n") ;;
    *) die "unknown drill '$n' (see --list)" ;;
  esac
done
[ "$want5" = 1 ] && run+=(5)
mkdir -p "$RUN_DIR"
for n in "${run[@]}"; do
  "drill_$n"
  # Let the pair settle and the controller's cooldown pass before the next
  # break, so one drill's recovery is never counted as the next one's detection.
  case "$n" in 3|4|5|6|7|16|17) wait_quiescent 300 || rec "warning: the engine is $(state_name) after drill $n; the next drill's precondition will refuse" ;; esac
done

echo
echo "== summary ($RUN_ID) =="
printf '%s\n' "${SUMMARY[@]}" | tee "$RUN_DIR/summary.txt"
grep -q '^FAIL' "$RUN_DIR/summary.txt" && exit 1
exit 0
