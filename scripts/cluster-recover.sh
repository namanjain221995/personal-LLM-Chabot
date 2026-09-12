#!/usr/bin/env bash
# Coordinated manual recovery of the two-node vLLM pair (contract §6.1, §6.3).
#
#   scripts/cluster-recover.sh [--timeout SECONDS]
#       Ask the engine controller to do it: POST /recover on 127.0.0.1:9838,
#       then follow GET /state, printing every step with UTC and IST clocks,
#       until the engine is READY (or the controller gives up). The controller
#       owns the choreography -- diagnostics first, worker first, then the
#       head, then a REAL completion -- and the lock, so a recovery started
#       here and one the controller starts by itself can never overlap.
#
#   scripts/cluster-recover.sh --force [--timeout SECONDS]
#       The controller is down, or has exhausted its budget and stood down
#       (state DOWN, no recovery in progress): take the same lock and do the
#       same choreography by hand with the cluster helpers. Refused while the
#       controller is alive and either recovering or still able to (a manual
#       request queued through the default mode above would otherwise run a
#       SECOND recovery right after this one). Never both: --force never POSTs.
#
#   scripts/cluster-recover.sh --clear-kernel-cache [--yes] [--timeout SECONDS]
#       The compiled-kernel caches are stale or broken (contract §6.6: a
#       cold_start_timeout, or torch._dynamo / flashinfer.jit / nvcc /
#       cuda_nvrtc errors in the head log): under the lock, stop BOTH ranks,
#       empty BOTH kernel-cache volumes (sf-local-ai_vllm-kernel-cache here,
#       sf-local-ai-worker_kernel-cache on the worker), start the pair and
#       prove it. The next start recompiles (~50 s per rank measured, more
#       for a FlashInfer GDN JIT). Asks for confirmation unless --yes.
#
# Exit status: 0 when a real completion succeeded on the recovered pair,
# 1 when it did not, 2 on a usage or precondition error.
#
# WHY worker first. The two ranks are ONE torch.distributed process group. A
# restarted head is a new group; a worker that is still in the old one can
# never join it and waits at the rendezvous until its own healthcheck notices
# (2026-09-11: the worker joined the OLD head's TCP store and was reset). A
# worker restarted FIRST is already waiting at the rendezvous when the new
# head arrives.
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
# shellcheck source=lib/engine-lock.sh
. "$CLUSTER_LIB_DIR/engine-lock.sh"

CONTROLLER_URL="${ENGINE_CONTROLLER_URL:-http://127.0.0.1:9838}"
HEAD_CTR="${ENGINE_HEAD_CONTAINER:-sf-local-ai-vllm-1}"
HEAD_KERNEL_CACHE_VOLUME="sf-local-ai_vllm-kernel-cache"
WORKER_KERNEL_CACHE_VOLUME="sf-local-ai-worker_kernel-cache"
# The controller polls every POLL_S=5 s and acts on a manual request "at
# the next tick": how long to wait for it to have TAKEN the request before
# judging the outcome (three ticks, review round 1).
CONTROLLER_TAKEUP_S="${CONTROLLER_TAKEUP_S:-20}"
# The controller's own cold-start budget (contract §6.3) plus the time it
# takes to detect, capture and stop the pair. Overridable per run.
TIMEOUT=1200
FORCE=0
CLEAR_CACHE=0
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --clear-kernel-cache) CLEAR_CACHE=1 ;;
    --yes) YES=1 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift ;;
    --timeout=*) TIMEOUT="${1#--timeout=}" ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
case "$TIMEOUT" in ''|*[!0-9]*) die "--timeout needs a whole number of seconds" ;; esac
[ "$FORCE" = 1 ] && [ "$CLEAR_CACHE" = 1 ] && die "--force and --clear-kernel-cache are two different procedures; pick one"

# Two clocks on every line: the incident reports are written in UTC, the
# people reading them are in Asia/Kolkata.
stamp() { printf '%s / %s IST' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(TZ=Asia/Kolkata date +%H:%M:%S)"; }
say()   { printf '%s  %s\n' "$(stamp)" "$*"; }

controller_get() { curl -fsS -m 5 "$CONTROLLER_URL$1" 2>/dev/null; }
controller_alive() { controller_get /healthz >/dev/null 2>&1; }

# state_line: one-line rendering of a /state document (schema 1).
state_line() {
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("state=? (unreadable /state)"); sys.exit(0)
rec = d.get("recovery") or {}
inc = d.get("incident") or {}
sig = d.get("signals") or {}
can = sig.get("canary") or {}
print("state=%s(%s) step=%s in_progress=%s incident=%s category=%s attempts=%s/%s canary_ok=%s ttft=%s reason=%s" % (
    d.get("state"), d.get("state_code"), rec.get("step"), rec.get("in_progress"),
    inc.get("id") or "-", inc.get("category") or "-", rec.get("attempts_in_window"), rec.get("budget"),
    can.get("ok"), can.get("ttft_s"), (d.get("reason") or "")[:80]))
'
}

# state_fields: "state_code state in_progress manual_pending incident_id
# category" from a /state document ("? ? ? ? ? ?" when unreadable).
state_fields() {
  python3 -c '
import json, sys
d = json.load(sys.stdin); rec = d.get("recovery") or {}; inc = d.get("incident") or {}
print(d.get("state_code"), d.get("state"), str(rec.get("in_progress")).lower(), str(rec.get("manual_pending", False)).lower(), inc.get("id") or "-", inc.get("category") or "-")' 2>/dev/null || echo "? ? ? ? ? ?"
}

# ------------------------------------------------------------ via controller
recover_via_controller() {
  controller_alive || die "the engine controller is not answering at $CONTROLLER_URL. Is the engine-controller container running (docker ps | grep engine-controller)? If it is down, use: $0 --force"
  say "controller reachable at $CONTROLLER_URL; state before:"
  controller_get /state | state_line
  say "POST /recover (reason=manual, category=manual)"
  local reply code body
  reply="$(mktemp)"
  code="$(curl -sS -m 10 -o "$reply" -w '%{http_code}' \
          -H 'Content-Type: application/json' \
          -d '{"reason":"manual","category":"manual"}' "$CONTROLLER_URL/recover" 2>/dev/null || echo 000)"
  body="$(cat "$reply" 2>/dev/null || true)"; rm -f "$reply"
  case "$code" in
    200|202) say "accepted (HTTP $code): ${body:0:200}" ;;
    403) die "the controller refused (HTTP 403): POST /recover is accepted from 127.0.0.1 only; run this on the head" ;;
    409) die "the controller declined (HTTP 409): ${body:0:300}. A recovery is already running; follow it with: curl -s $CONTROLLER_URL/state | python3 -m json.tool" ;;
    429) die "the controller declined (HTTP 429): ${body:0:300}. The budget or the cooldown applies (contract §6.3: manual recoveries count too); the escalation past the budget is $0 --force, which takes the same lock" ;;
    *) die "POST /recover failed (HTTP $code): ${body:0:300}" ;;
  esac

  # THE REQUEST IS QUEUED, NOT DONE. The controller acts at its next tick
  # (POLL_S=5), so the first /state after the 202 still shows the state
  # BEFORE the recovery: READY would have read as "recovered", DOWN as
  # "stood down" (and sent the operator to --force, which then restarted the
  # freshly recovered pair a second time -- review round 1). No verdict is
  # given until recovery.in_progress HAS BEEN true. While the controller
  # publishes recovery.manual_pending (v2: set in the very next /state after
  # the 202, kept while it waits for the engine lock) the request is alive
  # and the wait is bounded by --timeout only; a controller that shows
  # neither within CONTROLLER_TAKEUP_S never took it.
  local started last="" now line state code_ st in_prog pending incident category taken=0
  started="$(date +%s)"
  say "following /state (timeout ${TIMEOUT}s; the controller acts at its next tick)..."
  while :; do
    now="$(date +%s)"
    if [ $((now - started)) -ge "$TIMEOUT" ]; then
      say "TIMEOUT after ${TIMEOUT}s; last: $last"
      return 1
    fi
    if state="$(controller_get /state)"; then
      line="$(printf '%s' "$state" | state_line)"
      if [ "$line" != "$last" ]; then say "$line"; last="$line"; fi
      read -r code_ st in_prog pending incident category < <(printf '%s' "$state" | state_fields)
      if [ "$taken" = 0 ]; then
        if [ "$in_prog" = "true" ]; then
          taken=1; say "the controller took the request (incident $incident, category=$category)"
        elif [ "$pending" = "true" ]; then
          [ $(( (now - started) % 30 )) -lt 2 ] && say "  queued at the controller (manual_pending; it waits for its tick or for the engine lock)"
        elif [ $((now - started)) -ge "$CONTROLLER_TAKEUP_S" ]; then
          say "the controller did not start a recovery within ${CONTROLLER_TAKEUP_S}s of accepting the request (state $st, in_progress=$in_prog, manual_pending=$pending) -- check: curl -s $CONTROLLER_URL/state | python3 -m json.tool ; docker logs --tail 50 sf-local-ai-engine-controller-1"
          return 1
        fi
      elif [ "$in_prog" = "false" ]; then
        case "$code_" in
          2|3) say "READY: the controller reports a real completion on the recovered pair ($st)"; return 0 ;;
          8) say "DOWN: the controller stood down (category=$category). Evidence is under .runtime/incidents/; accepted requests stay queued (strict one-model mode: nothing else answers them). Manual path: $0 --force"; return 1 ;;
        esac
      fi
    else
      line="controller unreachable (restarting?)"
      if [ "$line" != "$last" ]; then say "$line"; last="$line"; fi
    fi
    sleep 2
  done
}

# ------------------------------------------------------------------ by hand
real_completion() { # real_completion -> 0 when the engine answered with content
  local models mid body reply
  models="$(curl -fsS -m 5 "$(api_url)/v1/models" 2>/dev/null)" || return 1
  mid="$(printf '%s' "$models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)" || return 1
  # The controller's canary shape (contract §5), non-streaming: no tools, no
  # thinking, 4 tokens, temperature 0, seed 7. Nothing about the reply is kept
  # but its presence.
  body="$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":4,"temperature":0,"seed":7,"chat_template_kwargs":{"enable_thinking":false}}' "$mid")"
  reply="$(curl -sS -m 60 -H 'Content-Type: application/json' -d "$body" "$(api_url)/v1/chat/completions" 2>/dev/null \
           | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d["choices"][0]["message"].get("content") or "").strip())' 2>/dev/null)" || return 1
  [ -n "$reply" ]
}

# capture_diagnostics MODE -> prints the incident dir; evidence BEFORE anything
# is touched, the way the controller does it (contract §6.3).
capture_diagnostics() {
  local mode="$1" id dir alive
  id="manual-$(date -u +%Y%m%dT%H%M%SZ)"
  dir="$RUNTIME_DIR/incidents/$id"
  # .runtime/incidents is bind-mounted into the controller (root); the launcher
  # creates it as the operator first, but a directory Docker created is root's.
  mkdir -p "$dir" 2>/dev/null || die "cannot create $dir (is .runtime/incidents root-owned? sudo chown -R $(id -un) $RUNTIME_DIR/incidents)"
  docker logs --tail 400 "$HEAD_CTR" >"$dir/head-logs.txt" 2>&1 || say "head logs: not captured (container absent?)"
  worker_compose logs --no-color --tail 400 vllm-worker >"$dir/worker-logs.txt" 2>&1 || say "worker logs: not captured (ssh?)"
  docker inspect "$HEAD_CTR" >"$dir/head-inspect.json" 2>/dev/null || true
  alive=false; controller_alive && alive=true
  controller_get /state >"$dir/controller-state.json" 2>/dev/null || true
  curl -fsS -m 5 "$(api_url)/metrics" 2>/dev/null | grep -E '^vllm:(num_requests_running|num_requests_waiting|generation_tokens_total|prompt_tokens_total)' >"$dir/head-metrics.txt" 2>/dev/null || true
  { echo "id=$id"; echo "started_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "started_at_ist=$(TZ=Asia/Kolkata date +%Y-%m-%dT%H:%M:%S)"; echo "actor=${SUDO_USER:-${USER:-unknown}}"; echo "mode=$mode"; echo "controller_alive=$alive"; } >"$dir/incident.txt"
  printf '%s' "$dir"
}

# refuse_beside_a_live_controller: --force and --clear-kernel-cache do the
# choreography by hand. They may run beside a controller that is DOWN and
# standing down (or unreachable), never beside one that is recovering or has
# a manual request queued: the second actor is exactly what 2026-09-11 was
# made of. --force also refuses a controller that is alive and still able to
# recover -- that is what the default mode is for.
refuse_beside_a_live_controller() {
  local purpose="$1" code_ st in_prog pending incident category
  controller_alive || { say "controller not answering at $CONTROLLER_URL; proceeding by hand"; return 0; }
  read -r code_ st in_prog pending incident category < <(controller_get /state | state_fields)
  say "controller IS answering: state=$st in_progress=$in_prog manual_pending=$pending incident=$incident category=$category"
  if [ "$in_prog" = "true" ] || [ "$pending" = "true" ]; then
    die "the controller is recovering the pair (or has a manual request queued: incident $incident); $purpose would restart it a second time. Follow it: curl -s $CONTROLLER_URL/state | python3 -m json.tool"
  fi
  case "$code_" in
    8) say "the controller has stood down (DOWN, $category); $purpose takes over under the same lock" ;;
    *) [ "$CLEAR_CACHE" = 1 ] && return 0
       die "the controller is alive and not standing down (state $st): use the default mode, which asks it to recover -- $purpose beside a live controller is the two-actor pattern of 2026-09-11." ;;
  esac
}

# pause_controller / resume_controller: NO controller is alive while the pair
# is restarted by hand (the same rule `techsara up` follows): a live one would
# read the worker's fresh start as worker_rank_dead, queue a recovery behind
# our lock and run it -- a second restart -- the moment the lock is released.
# Stopped, not removed; started again on every exit path, so the controller
# observes the restarted pair as a cold start and proves it itself.
CONTROLLER_PAUSED=0
pause_controller() {
  docker inspect -f '{{.State.Running}}' sf-local-ai-engine-controller-1 2>/dev/null | grep -qx true || return 0
  say "pausing the engine controller for the by-hand restart (started again afterwards)"
  head_compose stop --timeout 10 engine-controller >/dev/null 2>&1 || die "could not stop engine-controller; not touching the pair beside a live controller"
  CONTROLLER_PAUSED=1
}
resume_controller() {
  [ "$CONTROLLER_PAUSED" = 1 ] || return 0
  CONTROLLER_PAUSED=0
  if head_compose start engine-controller >/dev/null 2>&1; then
    say "engine controller started again (it will prove the pair with its own readiness sequence)"
  else
    say "WARNING: could not start engine-controller again; run ./techsara up (routine) or: docker start sf-local-ai-engine-controller-1"
  fi
}

# start_pair_and_prove DIR T0: shared tail of the by-hand procedures --
# wait for the model, a REAL completion, both ranks.
start_pair_and_prove() {
  local dir="$1" t0="$2" now code n
  section "wait for the model to load"
  while :; do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$(api_url)/health" 2>/dev/null || echo 000)"
    now="$(date +%s)"
    [ "$code" = 200 ] && { say "/health 200 after $((now - t0))s"; break; }
    if [ $((now - t0)) -ge "${COLD_START_BUDGET_S:-900}" ]; then
      say "cold start budget exceeded (/health=$code after $((now - t0))s); see scripts/cluster-logs.sh"
      { echo "ended_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "result=cold_start_timeout"; } >>"$dir/incident.txt"
      return 1
    fi
    [ $(( (now - t0) % 20 )) -lt 3 ] && say "  loading... /health=$code ($((now - t0))s)"
    sleep 3
  done

  section "a REAL completion (a 200 on /health proves nothing, contract §2)"
  for n in 1 2 3 4 5 6; do
    if real_completion; then say "completion ok (attempt $n)"; break; fi
    [ "$n" = 6 ] && { say "the engine did not complete a request in 6 attempts"; return 1; }
    sleep 10
  done

  section "both ranks participating (scripts/cluster-verify-engine.sh --probe)"
  "$CLUSTER_LIB_DIR/../cluster-verify-engine.sh" --probe || { say "verify-engine reported a failure"; return 1; }
  { echo "ended_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "result=recovered"; } >>"$dir/incident.txt"
  say "RECOVERED in $(( $(date +%s) - t0 ))s from the head restart; evidence in $dir"
  return 0
}

recover_by_hand() {
  cluster_load_settings
  require_dual_mode
  refuse_beside_a_live_controller "--force"
  trap resume_controller EXIT
  engine_lock_acquire "${ENGINE_LOCK_WAIT:-600}" "cluster-recover.sh --force" || exit 2
  pause_controller

  local dir
  section "1/6 capture diagnostics BEFORE touching anything"
  dir="$(capture_diagnostics force)"
  say "captured -> $dir"

  section "2/6 restart the WORKER first (it must be waiting at the rendezvous)"
  # -t 5: SIGKILL after 5 s interrupts a core dump in progress (contract §6.4).
  worker_compose restart --timeout 5 vllm-worker || die "worker restart failed; nothing else was touched"
  say "worker restarted"

  section "3/6 restart the head"
  docker restart -t 10 "$HEAD_CTR" || die "head restart failed"
  say "head restarted; waiting for /health (budget ${COLD_START_BUDGET_S:-900}s; measured 3.5-5.5 min)"

  # 4/6 wait, 5/6 real completion, 6/6 both ranks -- shared with --clear-kernel-cache.
  local rc=0
  start_pair_and_prove "$dir" "$(date +%s)" || rc=1
  resume_controller
  engine_lock_release
  return "$rc"
}

# ------------------------------------------------------- clear kernel cache
# empty_volume LABEL VOLUME [worker]: delete everything INSIDE the named
# volume through a throwaway container (the volume itself, its name and its
# mounts in the compose files stay). The engine that used it must be stopped
# first. The throwaway runs the digest-pinned python image both nodes already
# hold for the controller/sentinel -- nothing new is pulled for this.
empty_volume() {
  local label="$1" volume="$2" where="${3:-local}" image
  if [ "$where" = worker ]; then
    image="$(ssh_worker "grep -m1 '^CLUSTER_SENTINEL_IMAGE=' $WORKER_REMOTE_DIR/worker.env | cut -d= -f2-" 2>/dev/null || true)"
    [ -n "$image" ] || die "cannot read CLUSTER_SENTINEL_IMAGE from the worker's worker.env (run scripts/cluster-sync.sh --env-only first)"
    ssh_worker "if docker volume inspect '$volume' >/dev/null 2>&1; then docker run --rm -v '$volume:/cache' '$image' sh -c 'find /cache -mindepth 1 -delete && echo emptied'; else echo 'absent (nothing to empty)'; fi" | sed "s/^/  $label: /"
  else
    image="$(head_controller_image)" || die "cannot resolve the engine-controller image from the rendered head config"
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      docker run --rm -v "$volume:/cache" "$image" sh -c 'find /cache -mindepth 1 -delete && echo emptied' | sed "s/^/  $label: /"
    else
      echo "  $label: absent (nothing to empty)"
    fi
  fi
}

# The image the head's engine-controller service renders to (digest-pinned in
# compose.dgx-spark.yaml), read back from the rendered config.
head_controller_image() {
  head_compose config --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["engine-controller"]["image"])'
}

clear_kernel_cache() {
  cluster_load_settings
  require_dual_mode
  echo
  echo "THIS EMPTIES THE COMPILED-KERNEL CACHES OF BOTH RANKS (contract §6.6):"
  echo "  head   volume $HEAD_KERNEL_CACHE_VOLUME   (torch.compile / Inductor / Triton / FlashInfer JIT)"
  echo "  worker volume $WORKER_KERNEL_CACHE_VOLUME on $CLUSTER_WORKER_SSH"
  echo "Both ranks are STOPPED first (no inference for the whole procedure), the"
  echo "volumes are emptied, the pair is started and proven. The next start"
  echo "recompiles every kernel: ~50 s per rank measured, more for a FlashInfer"
  echo "GDN JIT. Do this only for a stale or broken cache (a cold_start_timeout,"
  echo "or torch._dynamo / flashinfer.jit / nvcc / cuda_nvrtc errors in the head"
  echo "log) -- a recovery never needs it, and the controller never does it."
  echo
  if [ "$YES" != 1 ]; then
    [ -t 0 ] || die "refusing without --yes when not interactive"
    printf 'Type yes to continue: '
    read -r answer
    [ "$answer" = yes ] || die "not confirmed; nothing was touched"
  fi
  refuse_beside_a_live_controller "--clear-kernel-cache"
  trap resume_controller EXIT
  engine_lock_acquire "${ENGINE_LOCK_WAIT:-600}" "cluster-recover.sh --clear-kernel-cache" || exit 2
  pause_controller

  local dir
  section "1/7 capture diagnostics BEFORE touching anything"
  dir="$(capture_diagnostics clear-kernel-cache)"
  say "captured -> $dir"

  section "2/7 stop both ranks (head first: nothing may compile into the caches while they are emptied)"
  docker stop -t 30 "$HEAD_CTR" >/dev/null || die "could not stop the head"
  worker_compose stop --timeout 30 vllm-worker || die "could not stop the worker; the head is stopped -- start it again with scripts/cluster-recover.sh --force"
  say "both ranks stopped"

  section "3/7 empty the head's kernel cache ($HEAD_KERNEL_CACHE_VOLUME)"
  empty_volume head "$HEAD_KERNEL_CACHE_VOLUME"
  section "4/7 empty the worker's kernel cache ($WORKER_KERNEL_CACHE_VOLUME)"
  empty_volume worker "$WORKER_KERNEL_CACHE_VOLUME" worker
  echo "kernel_cache_cleared=head:$HEAD_KERNEL_CACHE_VOLUME worker:$WORKER_KERNEL_CACHE_VOLUME" >>"$dir/incident.txt"

  section "5/7 start the WORKER first (it must be waiting at the rendezvous)"
  worker_compose up -d vllm-worker || die "worker start failed; the head is still stopped -- scripts/cluster-recover.sh --force once the worker is up"
  section "6/7 start the head"
  docker start "$HEAD_CTR" >/dev/null || die "head start failed"
  say "head started; the caches are cold, so expect the full compile on top of the load (budget ${COLD_START_BUDGET_S:-900}s)"

  # 7/7 wait, real completion, both ranks.
  local rc=0
  start_pair_and_prove "$dir" "$(date +%s)" || rc=1
  resume_controller
  engine_lock_release
  return "$rc"
}

echo "========================================"
echo "vLLM PAIR RECOVERY   ($(stamp))"
echo "========================================"
if [ "$CLEAR_CACHE" = 1 ]; then
  clear_kernel_cache
elif [ "$FORCE" = 1 ]; then
  recover_by_hand
else
  recover_via_controller
fi
