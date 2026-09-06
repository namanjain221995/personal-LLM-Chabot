#!/usr/bin/env bash
# Post-rollout invariants: the questions "is it up?" does not ask.
#
#   scripts/deploy-smoke.sh [--manifest FILE] [--baseline RECORD] [--with-model]
#
# The existing verify stage checks that things ANSWER — /health returns 200, the
# frontend serves /login, Prometheus is alive. Those are necessary and they are
# not sufficient. Every check below is one that passes a 200 and still means the
# deploy is wrong:
#
#   DIGEST      The containers are running the images that were promoted. A
#               healthy container running last week's image is healthy and
#               wrong. (--manifest; without it, this falls back to checking the
#               containers against whatever their tags point at NOW, which
#               catches a tag that moved under a running container.)
#   SCHEMA      The running orchestrator's CODE knows the schema version the
#               database has APPLIED. Old code on a newer schema answers
#               /health perfectly — app_db is reachable, which is all that check
#               tests — and then fails on the one route that touches the column
#               it does not know about.
#   NOT LOOPING No container is restarting. A crash-looping container spends
#               part of every minute healthy, so a single poll can miss it
#               entirely; RestartCount is what actually shows it.
#   MODEL CLOCK The main model container did not restart. A routine deploy must
#               leave it alone (TECHSARA_PRESERVE_MAIN_MODEL) because reloading
#               it is 15-25 minutes of the product answering nothing. With
#               --baseline <a record.json from before the deploy> this is an
#               assertion rather than an observation.
#   MODEL       Optional (--with-model), because it costs a real generation:
#               a completion that actually returns text. /v1/models answers
#               while the engine behind it is dead, so it proves nothing.
#
# Everything except --with-model is read-only and adds no measurable load, so
# this is safe to run at any time, not only after a deploy.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

MANIFEST=""; BASELINE=""; WITH_MODEL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest) MANIFEST="${2:?}"; shift ;;
    --baseline) BASELINE="${2:?}"; shift ;;
    --with-model) WITH_MODEL=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0"; exit 0 ;;
    *) dr_die "unknown option: $1" ;;
  esac
  shift
done

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip(){ printf '  \033[33mSKIP\033[0m %s\n' "$*"; }

printf '== DIGEST ==\n'
if [ -n "$MANIFEST" ]; then
  if "$HERE/deploy-preflight.sh" verify "$MANIFEST" >/dev/null 2>&1; then
    ok "every application container is running its promoted image id"
  else
    bad "a container is NOT running its promoted image id"
    "$HERE/deploy-preflight.sh" verify "$MANIFEST" 2>&1 | sed -n 's/^/      /p' | tail -8
  fi
else
  # No manifest: still worth asking whether the tag has moved out from under a
  # running container, which is the same failure seen from the other side.
  for svc in "${DR_APP_SERVICES[@]}"; do
    container="$(dr_container_for "$svc")"
    running="$(dr_container_image_id "$container")"
    ref="$(dr_container_image_ref "$container")"
    [ -n "$running" ] || { bad "$svc: no container $container"; continue; }
    tagged="$(dr_image_id "$ref")"
    if [ "$running" = "$tagged" ]; then ok "$svc: container and tag $ref agree on ${running:7:12}"
    else bad "$svc: running ${running:7:12} but $ref now points at ${tagged:7:12} - the tag moved"; fi
  done
fi

printf '\n== SCHEMA ==\n'
live="$(dr_live_schema_version 2>/dev/null || true)"
running_image="$(dr_container_image_id "$(dr_container_for orchestrator)")"
code=""
[ -n "$running_image" ] && code="$(dr_code_schema_version_from_image "$running_image" 2>/dev/null || true)"
if [ -z "$live" ] || [ -z "$code" ]; then
  bad "could not read both versions (database=${live:-?}, running code=${code:-?})"
elif [ "$code" = "$live" ]; then
  ok "the running orchestrator knows V$code and the database has applied V$live"
elif [ "$code" -lt "$live" ]; then
  bad "the running orchestrator knows only V$code but the database is at V$live - old code on a newer schema"
else
  bad "the running orchestrator knows V$code but the database is only at V$live - migrations did not run on start"
fi

printf '\n== HEALTH ==\n'
port="$(dr_env_value ORCHESTRATOR_PORT)"; port="${port:-8080}"
front="$(dr_env_value FRONTEND_PORT)"; front="${front:-3000}"
payload="$(curl -fsS -m 20 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
if [ -z "$payload" ]; then
  bad "the orchestrator did not answer /health on $port"
else
  read -r status checks <<<"$(printf '%s' "$payload" | python3 -c '
import json, sys
d = json.load(sys.stdin)
bad = [k for k, v in (d.get("checks") or {}).items() if v.get("status") != "ok"]
print(d.get("status", "?"), ",".join(bad) or "-")')"
  case ",$checks," in
    *,app_db,*) bad "/health says app_db is down (status=$status)" ;;
    *,vllm,*)   bad "/health says the main model is not answering (status=$status)" ;;
    *)          ok "/health status=$status failing=${checks}" ;;
  esac
fi
curl -fsS -m 20 -o /dev/null "http://127.0.0.1:${front}/" \
  && ok "the frontend answers on $front" || bad "the frontend did not answer on $front"

printf '\n== NOT LOOPING ==\n'
looping=0
while read -r name; do
  [ -n "$name" ] || continue
  read -r state restarts <<<"$(docker inspect "$name" --format '{{.State.Status}} {{.RestartCount}}' 2>/dev/null || echo '? 0')"
  if [ "$state" = restarting ]; then bad "$name is restarting"; looping=1; fi
done < <(dr_project_containers)
[ "$looping" = 0 ] && ok "no container in the project is in the restarting state"

printf '\n== MODEL CLOCK ==\n'
vllm="$(dr_container_for vllm)"
started="$(docker inspect "$vllm" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
if [ -z "$started" ]; then
  skip "no $vllm container to check"
elif [ -n "$BASELINE" ] && [ -f "$BASELINE" ]; then
  was="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(((d.get("containers") or {}).get(sys.argv[2]) or {}).get("started_at", ""))' "$BASELINE" "$vllm")"
  if [ -z "$was" ]; then skip "the baseline record does not mention $vllm"
  elif [ "$was" = "$started" ]; then ok "the main model was NOT restarted (started $started, unchanged)"
  else bad "the main model RESTARTED: $was -> $started. A routine deploy must not reset this clock."; fi
else
  skip "no --baseline record; $vllm started at $started (nothing to compare it to)"
fi

printf '\n== MODEL ==\n'
if [ "$WITH_MODEL" != 1 ]; then
  skip "--with-model not given (a real generation competes with whatever else is on the GPU)"
else
  model="$(grep -m1 '^MAIN_MODEL=' "$DR_ROOT/.runtime/generated.env" | cut -d= -f2)"
  vport="$(grep -m1 '^VLLM_PORT=' "$DR_ROOT/.runtime/generated.env" | cut -d= -f2)"
  reply="$(curl -fsS -m 180 -H 'Content-Type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: READY.\"}],\"max_tokens\":8,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
      "http://127.0.0.1:${vport}/v1/chat/completions" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip()[:20])' 2>/dev/null || true)"
  [ -n "$reply" ] && ok "the model completed a request: ${reply}" \
                  || bad "the model did not complete a request (a 200 from /v1/models would not have caught this)"
fi

printf '\n== SUMMARY ==\n  passed: %d\n  failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
