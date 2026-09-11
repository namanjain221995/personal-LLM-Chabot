#!/usr/bin/env bash
# Prove what the two-node main engine is ACTUALLY running — from the running
# containers on BOTH nodes, never from .env — and that both ranks take part.
#
#   scripts/cluster-verify-engine.sh            # checks, exit 1 on any FAIL
#   scripts/cluster-verify-engine.sh --probe    # also one real completion with
#                                               # GPU sampling on both nodes
#
# Written for the 2026-09-11 GDN/MTP remediation, where the question "is
# --speculative-config gone from the command line of BOTH ranks?" had to be
# answered from `docker inspect`, because generated.env, worker.env and the
# running process can and did disagree in the past (a preserve-mode deploy
# rewrites the file and leaves the engine alone).
# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"
PROBE=0
for a in "$@"; do case "$a" in --probe) PROBE=1 ;; -h|--help) sed -n '2,8p' "$0"; exit 0 ;; *) die "unknown option: $a" ;; esac; done
cluster_load_settings
require_dual_mode

HEAD_CTR="${VLLM_WATCHDOG_TARGET:-sf-local-ai-vllm-1}"
WORKER_CTR="sf-local-ai-worker-vllm-worker-1"

argv_of() { # argv_of <docker inspect json of .Config.Cmd> -> one flag per line
  python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)))'
}
head_argv="$(docker inspect "$HEAD_CTR" --format '{{json .Config.Cmd}}' 2>/dev/null | argv_of)" || die "head container $HEAD_CTR is not running"
worker_argv="$(ssh_worker "docker inspect $WORKER_CTR --format '{{json .Config.Cmd}}'" 2>/dev/null | argv_of)" || die "worker container $WORKER_CTR is not running on $CLUSTER_WORKER_SSH"

section "engine command lines (docker inspect, both nodes)"
for node in head worker; do
  argv="$( [ "$node" = head ] && printf '%s' "$head_argv" || printf '%s' "$worker_argv" )"
  if printf '%s\n' "$argv" | grep -qx -- '--speculative-config'; then
    check_fail "$node: --speculative-config is PRESENT ($(printf '%s\n' "$argv" | grep -A1 -x -- '--speculative-config' | tail -1))"
  else
    check_pass "$node: no --speculative-config (speculative decoding off)"
  fi
  if printf '%s\n' "$argv" | grep -qx -- '--no-enable-prefix-caching'; then
    check_pass "$node: --no-enable-prefix-caching (prefix caching off, stated explicitly)"
  elif printf '%s\n' "$argv" | grep -qx -- '--enable-prefix-caching'; then
    check_warn "$node: --enable-prefix-caching is present (experimental on this hybrid-Mamba model)"
  else
    check_warn "$node: neither prefix-caching flag present; vLLM's default applies"
  fi
  for want in "--tensor-parallel-size 2" "--pipeline-parallel-size 1" "--nnodes 2" "--distributed-executor-backend mp" "--enable-chunked-prefill"; do
    flag="${want%% *}"; val="${want#* }"
    if [ "$flag" = "$val" ]; then
      printf '%s\n' "$argv" | grep -qx -- "$flag" && check_pass "$node: $flag" || check_fail "$node: $flag missing"
    else
      got="$(printf '%s\n' "$argv" | grep -A1 -x -- "$flag" | tail -1)"
      [ "$got" = "$val" ] && check_pass "$node: $flag $val" || check_fail "$node: $flag is '${got:-absent}', expected $val"
    fi
  done
done
# The engine args must be byte-identical on both ranks apart from --node-rank/--headless.
strip() { printf '%s\n' "$1" | grep -vxE -- '--node-rank|[01]|--headless|--host|--port|0\.0\.0\.0|[0-9]{4,5}|--reasoning-parser|qwen3|--tool-call-parser|qwen3_xml|--enable-auto-tool-choice'; }
if [ "$(strip "$head_argv")" = "$(strip "$worker_argv")" ]; then
  check_pass "engine arguments are identical on both ranks (mp executor requirement)"
else
  check_fail "engine arguments DIFFER between head and worker:"; diff <(strip "$head_argv") <(strip "$worker_argv") || true
fi

section "generated.env vs running command (drift)"
gen_args="$(python3 - "$GENERATED_ENV" <<'PY'
import sys, shlex
for line in open(sys.argv[1]):
    if line.startswith("CLUSTER_ENGINE_ARGS="):
        raw = line.rstrip("\n")[len("CLUSTER_ENGINE_ARGS="):]
        # generated.env is rendered by the launcher: a double-quoted value with
        # \" escapes, or a single-quoted literal.
        if raw.startswith('"') and raw.endswith('"'):
            raw = bytes(raw[1:-1], "utf-8").decode("unicode_escape")
        elif raw.startswith("'") and raw.endswith("'"):
            raw = raw[1:-1]
        print("\n".join(shlex.split(raw)))
PY
)"
if [ -n "$gen_args" ] && printf '%s\n' "$head_argv" | grep -qxF -- "$(printf '%s\n' "$gen_args" | head -1)"; then
  missing="$(comm -23 <(printf '%s\n' "$gen_args" | sort -u) <(printf '%s\n' "$head_argv" | sort -u))"
  [ -z "$missing" ] && check_pass "every generated engine argument is on the head's command line" \
    || check_fail "generated.env carries arguments the running head does not (a pending engine change?): $(printf '%s' "$missing" | tr '\n' ' ')"
else
  check_warn "could not compare CLUSTER_ENGINE_ARGS with the running command"
fi

section "engine facts from the logs"
for node in head worker; do
  if [ "$node" = head ]; then logs="$(docker logs "$HEAD_CTR" 2>&1 | tail -n 20000)"; else logs="$(ssh_worker "docker logs $WORKER_CTR 2>&1 | tail -n 20000")"; fi
  if [ "$(printf '%s' "$logs" | grep -c "Loading drafter model" || true)" != 0 ]; then check_fail "$node: log shows 'Loading drafter model' (an MTP draft was loaded)"; else check_pass "$node: no MTP drafter loaded (this log window)"; fi
  if [ "$(printf '%s' "$logs" | grep -c "is not supported with spec-decode" || true)" != 0 ]; then check_warn "$node: CUDA graphs were downgraded for spec-decode (this log window)"; fi
  cg="$(printf '%s' "$logs" | grep -oE "Capturing CUDA graphs \([^)]*\)" | sort -u | tr '\n' ';' || true)"
  [ -n "$cg" ] && log_info "$node: CUDA graph passes: $cg"
  kv="$(printf '%s' "$logs" | grep -oE "GPU KV cache size: [0-9,]+ tokens" | tail -1 || true)"; [ -n "$kv" ] && log_info "$node: $kv"
  faults="$(printf '%s' "$logs" | grep -cE "CUDA error|illegal memory access|misaligned address|died unexpectedly" || true)"
  [ "$faults" = 0 ] && check_pass "$node: 0 CUDA fault lines in the retained log" || check_fail "$node: $faults CUDA fault lines in the retained log"
  if [ "$node" = head ]; then xid="$(journalctl -k --no-pager 2>/dev/null | grep -c Xid || true)"; else xid="$(ssh_worker "journalctl -k --no-pager 2>/dev/null | grep -c Xid || true")"; fi
  [ "${xid:-0}" = 0 ] && check_pass "$node: 0 Xid lines in the kernel log since boot" || check_fail "$node: $xid Xid lines in the kernel log since boot"
done
nccl="$(ssh_worker "docker logs $WORKER_CTR 2>&1 | grep -E 'NCCL INFO Using network|NCCL INFO NET/IB : Using|Connected all rings' | tail -3" || true)"
if [ "$(printf '%s' "$nccl" | grep -c "Using network IB" || true)" != 0 ]; then check_pass "worker: NCCL 'Using network IB' (RoCE, not sockets)"; else check_warn "worker: could not confirm NCCL transport from the log window"; fi
[ "$(printf '%s' "$nccl" | grep -c "Socket" || true)" != 0 ] && check_fail "worker: NCCL fell back to sockets"

section "restart counters"
for node in head worker; do
  if [ "$node" = head ]; then st="$(docker inspect -f '{{.RestartCount}} {{.State.StartedAt}} {{.State.Health.Status}}' "$HEAD_CTR")"; else st="$(ssh_worker "docker inspect -f '{{.RestartCount}} {{.State.StartedAt}} {{.State.Health.Status}}' $WORKER_CTR")"; fi
  log_info "$node: RestartCount/StartedAt/Health = $st"
done

if [ "$PROBE" = 1 ]; then
  section "one real completion with both GPUs sampled"
  s1="$(mktemp)"; s2="$(mktemp)"
  nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 250 > "$s1" 2>/dev/null & P1=$!
  ssh_worker "timeout 60 nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -lms 250" > "$s2" 2>/dev/null & P2=$!
  sleep 1
  body='{"model":"'"$MAIN_MODEL"'","messages":[{"role":"user","content":"Write six sentences about tensor parallelism."}],"max_tokens":160,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}'
  t0=$(date +%s.%N)
  out="$(curl -s -m 120 -H 'Content-Type: application/json' -d "$body" "$(api_url)/v1/chat/completions")"
  t1=$(date +%s.%N)
  kill $P1 $P2 2>/dev/null || true; wait 2>/dev/null || true
  toks="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("usage",{}).get("completion_tokens",0))' 2>/dev/null || echo 0)"
  el="$(python3 -c "print(round($t1-$t0,2))")"
  [ "${toks:-0}" -gt 0 ] && check_pass "completion answered: $toks tokens in ${el}s ($(python3 -c "print(round($toks/$el,1))") tok/s incl. prefill)" || check_fail "completion failed: $(printf '%s' "$out" | head -c 200)"
  summ() { awk 'NF{n++; if($1+0>m)m=$1+0; s+=$1; if($1+0>0)b++} END{if(n) printf "max %d%% avg %.0f%% busy %d/%d", m, s/n, b, n; else printf "no samples"}' "$1"; }
  h="$(summ "$s1")"; w="$(summ "$s2")"
  log_info "GPU during the probe: head $h | worker $w"
  hm="$(awk 'NF && $1+0>m{m=$1+0} END{print m+0}' "$s1")"; wm="$(awk 'NF && $1+0>m{m=$1+0} END{print m+0}' "$s2")"
  { [ "${hm:-0}" -ge 30 ] && [ "${wm:-0}" -ge 30 ]; } && check_pass "both GPUs worked on the request (peak head ${hm}%, worker ${wm}%)" || check_fail "a GPU stayed idle during the request (peak head ${hm}%, worker ${wm}%)"
  rm -f "$s1" "$s2"
fi

check_summary
