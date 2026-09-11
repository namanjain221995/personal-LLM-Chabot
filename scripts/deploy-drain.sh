#!/usr/bin/env bash
# Do not cut a request in half to save eight seconds.
#
#   scripts/deploy-drain.sh check   [SERVICE...]
#   scripts/deploy-drain.sh wait    SERVICE [--deadline S] [--quiet-for S] [--max N]
#   scripts/deploy-drain.sh uploads [--deadline S] [--poll S]
#
# WHAT DRAINING CAN AND CANNOT BE HERE
#
# The honest version first, because the dishonest version is easy to write:
# there is no second copy of this stack to shift traffic to. The 35B model
# fills the GPUs, so a "green" stack cannot exist beside the "blue" one, and
# anything calling itself blue-green on this box is a story. What IS available
# is a graceful stop, and it is worth using properly.
#
# `docker compose up -d` recreates a service by sending SIGTERM and waiting
# `stop_grace_period` before SIGKILL. uvicorn treats SIGTERM as "stop accepting
# new connections, finish the ones you have", so the grace period is literally
# how long an in-flight answer is allowed to finish. The default is TEN
# SECONDS, which is shorter than a great many streamed answers.
#
#   check  reads the RENDERED configuration (full four-file chain) and reports
#          the grace period each service will actually get, flagging any
#          traffic-carrying service that is on the 10 s default. This is a
#          configuration audit, and it is the part that catches the problem
#          before a deploy rather than during one.
#
#   wait   watches established connections inside the container and returns
#          once they have been quiet for a while, so the SIGTERM lands in a gap
#          rather than in the middle of a burst. It counts CONNECTIONS, not
#          requests — the orchestrator exposes no in-flight gauge — so it is a
#          best-effort quiet window, not a proof that nothing is in flight.
#          It is deliberately advisory: a busy box must not be able to block a
#          deploy forever, so the deadline expiring is a warning (exit 2), not
#          a failure. The grace period is what actually protects the request.
#
#   uploads waits for chunked upload sessions in status `finalizing` to clear.
#          This is the ONE upload state a recreate cannot leave recoverable on
#          its own timescale: `uploading` resumes from the browser (the parts
#          are on disk and the session row says which arrived) and `complete`
#          is done, but `finalizing` is a process holding a row lock while it
#          concatenates parts, hashes the result and hard-links it into the
#          video store — kill it there and the row sits `finalizing` until the
#          NEXT process's startup hook resets it, with the person watching a
#          spinner in between. Seconds of waiting removes that window.
#
#          WHAT IT GUARANTEES: at the moment it returned 0, no session was in
#          `finalizing`. WHAT IT DOES NOT: nothing stops a new `complete` from
#          arriving one millisecond later — there is no admission control here
#          and adding one would mean refusing uploads during every deploy. It
#          also says nothing about `uploading` sessions, deliberately: those
#          are designed to survive a restart. Like `wait`, it is advisory and
#          bounded (exit 2 on the deadline), and it reads the database through
#          the postgres container with a SELECT and nothing else.
#
# No mode ever stops, kills or recreates anything, and none of them writes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"

#: Services that carry user traffic and must not have their connections cut at
#: the 10 s default. sync-worker is here because a Salesforce sync in flight is
#: worth finishing (it declares 10m for exactly that reason).
DR_TRAFFIC_SERVICES="orchestrator frontend sync-worker"
#: Seconds. Below this a traffic-carrying service is reported as a finding.
DR_MIN_GRACE="${DR_MIN_GRACE:-30}"

MODE="${1:-check}"; [ $# -gt 0 ] && shift || true

# ---------------------------------------------------------------------- check
seconds_of() {  # "2m0s" / "90s" / "1m30s" / "" -> integer seconds ("" -> "")
  python3 - "$1" <<'PY'
import re, sys
raw = (sys.argv[1] or "").strip()
if not raw:
    print(""); raise SystemExit
if raw.isdigit():
    print(int(raw)); raise SystemExit
total, seen = 0.0, False
for value, unit in re.findall(r"([0-9.]+)\s*(h|m|s|ms|us|ns)", raw):
    seen = True
    total += float(value) * {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}[unit]
print(int(total) if seen else "")
PY
}

do_check() {
  local prefix; prefix="$(dr_compose_prefix)"
  local rendered
  rendered="$(cd "$DR_ROOT" && eval "$prefix" config --format json)" \
    || dr_die "docker compose config failed"

  # Which services would compose actually recreate? Only those need draining,
  # and saying so out loud is half of "recreate ONLY required services".
  local hashes
  hashes="$(cd "$DR_ROOT" && eval "$prefix" config --hash='*')" || dr_die "config --hash failed"
  local -A WOULD=()
  local svc want have container
  while read -r svc want; do
    container="$(dr_container_for "$svc")"
    have="$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' 2>/dev/null || true)"
    if [ -z "$have" ]; then WOULD[$svc]=create
    elif [ "$have" != "$want" ]; then WOULD[$svc]=recreate
    else WOULD[$svc]=keep; fi
  done <<<"$hashes"

  local wanted=("$@")
  if [ "${#wanted[@]}" -eq 0 ]; then
    mapfile -t wanted < <(printf '%s\n' "${!WOULD[@]}" | sort)
  fi

  printf '%-16s %-9s %-12s %s\n' SERVICE ACTION GRACE VERDICT
  printf '%-16s %-9s %-12s %s\n' ------- ------ ----- -------
  local findings=0 raw secs verdict action
  for svc in "${wanted[@]}"; do
    action="${WOULD[$svc]:-unknown}"
    raw="$(DR_SVC="$svc" python3 -c '
import json, os, sys
d = json.loads(sys.stdin.read())
s = (d.get("services") or {}).get(os.environ["DR_SVC"]) or {}
print(s.get("stop_grace_period") or "")' <<<"$rendered")"
    secs="$(seconds_of "$raw")"
    if [ -z "$secs" ]; then
      raw="(unset)"; secs=10          # Docker's documented default.
      verdict="DEFAULT 10s"
    else
      verdict="explicit"
    fi
    case " $DR_TRAFFIC_SERVICES " in
      *" $svc "*)
        if [ "$secs" -lt "$DR_MIN_GRACE" ]; then
          verdict="TOO SHORT for live traffic (<${DR_MIN_GRACE}s)"
          [ "$action" = keep ] || findings=$((findings + 1))
        else
          verdict="ok"
        fi ;;
      *) verdict="$verdict (not a traffic service)" ;;
    esac
    printf '%-16s %-9s %-12s %s\n' "$svc" "$action" "${raw}" "$verdict"
  done

  if [ "$findings" -gt 0 ]; then
    printf '\n'
    dr_warn "$findings service(s) that this deploy WOULD recreate will have their"
    dr_warn "connections killed after Docker's 10 s default. An answer still being"
    dr_warn "streamed at that moment is cut off mid-sentence."
    dr_warn "Fix is one line per service in the compose file that owns it:"
    dr_warn "    stop_grace_period: 2m"
    dr_warn "(This script does not edit compose files.)"
    return 1
  fi
  dr_say "drain check: every service this deploy would recreate has an adequate grace period"
  return 0
}

# ----------------------------------------------------------------------- wait
# Established connections to the service's own listening port, counted from
# inside its network namespace. /proc/net/tcp is always present and needs no
# tools in the image, which matters: these images carry no `ss` and no `netstat`.
conn_count() {
  local container="$1" port="$2" hexport
  hexport="$(printf '%04X' "$port")"
  docker exec "$container" sh -c '
    port="$1"
    total=0
    for f in /proc/net/tcp /proc/net/tcp6; do
      [ -r "$f" ] || continue
      # $2 = local address:port, $3 = remote, $4 = state. 01 is ESTABLISHED.
      n=$(awk -v p=":$port" '"'"'NR>1 && $4=="01" && index($2,p)==length($2)-length(p)+1 {c++} END{print c+0}'"'"' "$f")
      total=$((total + n))
    done
    echo "$total"
  ' sh "$hexport" 2>/dev/null || echo ""
}

service_port() {
  local svc="$1" prefix rendered
  prefix="$(dr_compose_prefix)"
  rendered="$(cd "$DR_ROOT" && eval "$prefix" config --format json)"
  DR_SVC="$svc" python3 -c '
import json, os, sys
d = json.loads(sys.stdin.read())
s = (d.get("services") or {}).get(os.environ["DR_SVC"]) or {}
for spec in s.get("ports") or []:
    target = spec.get("target") if isinstance(spec, dict) else None
    if target:
        print(int(target)); break
else:
    print("")' <<<"$rendered"
}

do_wait() {
  local svc="${1:?usage: $0 wait SERVICE [--deadline S] [--quiet-for S] [--max N]}"; shift
  local deadline=120 quiet_for=5 maxconn=0 port=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --deadline) deadline="${2:?}"; shift ;;
      --quiet-for) quiet_for="${2:?}"; shift ;;
      --max) maxconn="${2:?}"; shift ;;
      --port) port="${2:?}"; shift ;;
      *) dr_die "unknown option: $1" ;;
    esac
    shift
  done

  local container; container="$(dr_container_for "$svc")"
  docker inspect "$container" >/dev/null 2>&1 || dr_die "no container $container to drain"
  [ -n "$port" ] || port="$(service_port "$svc")"
  [ -n "$port" ] || dr_die "cannot determine $svc's listening port from the rendered config; pass --port"

  dr_say "drain wait: $svc ($container) port $port, quiet<=$maxconn for ${quiet_for}s, deadline ${deadline}s"
  local started; started="$(date +%s)"
  local quiet=0 count now
  while :; do
    count="$(conn_count "$container" "$port")"
    if [ -z "$count" ]; then
      dr_warn "cannot read /proc/net/tcp inside $container; skipping the quiet window"
      return 2
    fi
    if [ "$count" -le "$maxconn" ]; then
      quiet=$((quiet + 1))
      if [ "$quiet" -ge "$quiet_for" ]; then
        dr_say "drain wait: $svc quiet ($count established) for ${quiet}s - safe to recreate"
        return 0
      fi
    else
      [ "$quiet" -gt 0 ] && dr_say "drain wait: $svc busy again ($count established)"
      quiet=0
    fi
    now="$(date +%s)"
    if [ $((now - started)) -ge "$deadline" ]; then
      # Each sample costs a `docker exec`, so a short deadline can expire
      # before quiet_for CONSECUTIVE samples accumulate even though the service
      # is quiet. Reporting that as "still busy" would be a lie, and a lie that
      # makes an operator distrust the tool the one time it matters.
      if [ "$count" -le "$maxconn" ]; then
        dr_say "drain wait: $svc is quiet ($count established) but the ${quiet_for}s"
        dr_say "quiet window did not complete before the ${deadline}s deadline. Treating"
        dr_say "it as drained: the last sample found nothing in flight."
        return 0
      fi
      dr_warn "drain wait: $svc still has $count established connection(s) after ${deadline}s."
      dr_warn "Proceeding anyway - a busy service must not be able to block a deploy forever."
      dr_warn "The stop_grace_period is what protects those requests now; run '$0 check' to see it."
      return 2
    fi
    sleep 1
  done
}

# -------------------------------------------------------------------- uploads
# One SELECT as the application user, inside the database's own container.
#
# READ-ONLY BY CONSTRUCTION: PGOPTIONS makes the whole session read-only, so a
# statement that tried to write would be refused by PostgreSQL itself ("cannot
# execute ... in a read-only transaction") rather than by this script's good
# intentions. It is passed as a connection option rather than as a leading
# `SET`, because a `SET` costs an extra "SET" line on stdout that the caller
# would then have to parse around.
dr_psql_ro() {
  local pg user db
  pg="$(dr_container_for postgres)"
  docker inspect "$pg" >/dev/null 2>&1 || return 1
  user="$(dr_env_value POSTGRES_USER)"; user="${user:-techsara}"
  db="$(dr_env_value POSTGRES_DB)"; db="${db:-techsara}"
  docker exec -e PGOPTIONS="-c default_transaction_read_only=on" "$pg" \
    psql -U "$user" -d "$db" -tA -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null \
    | tr -d '[:space:]'
}

# How many chunked sessions are mid-finalisation right now, or "" when the
# question cannot be answered (no postgres container, psql unhappy). "" and
# "0" are deliberately different: one is "nothing in flight", the other is
# "I do not know", and a deploy script must be able to tell them apart before
# it decides to say something reassuring.
#
# The table's existence is asked FIRST and separately. `to_regclass(...) IS
# NULL` inside the counting query would not have helped: PostgreSQL parses the
# whole statement before evaluating any of it, so naming a table that is not
# there is an error at parse time whatever the CASE says. A database that
# predates V29 (upload_sessions arrives with it) therefore answers 0 —
# truthfully, because a schema without the table has no finalising sessions.
finalizing_count() {
  local present out
  present="$(dr_psql_ro "SELECT to_regclass('public.upload_sessions') IS NOT NULL")" || {
    printf '\n'; return 0; }
  case "$present" in
    t) : ;;
    f) printf '0\n'; return 0 ;;
    *) printf '\n'; return 0 ;;
  esac
  out="$(dr_psql_ro "SELECT count(*) FROM upload_sessions WHERE status = 'finalizing'")" || true
  case "$out" in
    ''|*[!0-9]*) printf '\n' ;;
    *) printf '%s\n' "$out" ;;
  esac
}

do_uploads() {
  local deadline="${DEPLOY_FINALIZE_DEADLINE:-90}" poll=2
  while [ $# -gt 0 ]; do
    case "$1" in
      --deadline) deadline="${2:?}"; shift ;;
      --poll) poll="${2:?}"; shift ;;
      *) dr_die "unknown option: $1" ;;
    esac
    shift
  done

  local count; count="$(finalizing_count)"
  if [ -z "$count" ]; then
    dr_warn "drain uploads: cannot read upload_sessions (no postgres container, or"
    dr_warn "the schema predates V29). Proceeding without the check."
    return 2
  fi
  if [ "$count" = 0 ]; then
    dr_say "drain uploads: no chunked upload is being finalised"
    return 0
  fi

  # 90 s is sized on the work, not on taste: finalisation concatenates the
  # parts, sha256s the result and hard-links it into the video store. A 4 GB
  # video (VIDEO_MAX_UPLOAD_MB) is the worst case on this box's NVMe and lands
  # inside a minute; anything still going after 90 s is a finaliser in trouble
  # and must not be allowed to hold a deploy.
  dr_say "drain uploads: $count session(s) finalising; waiting up to ${deadline}s"
  local started; started="$(date +%s)" now
  while :; do
    sleep "$poll"
    count="$(finalizing_count)"
    if [ -z "$count" ]; then
      dr_warn "drain uploads: lost the ability to read upload_sessions mid-wait"
      return 2
    fi
    if [ "$count" = 0 ]; then
      now="$(date +%s)"
      dr_say "drain uploads: clear after $((now - started))s"
      return 0
    fi
    now="$(date +%s)"
    if [ $((now - started)) -ge "$deadline" ]; then
      dr_warn "drain uploads: $count session(s) still finalising after ${deadline}s."
      dr_warn "Proceeding anyway - an upload must not be able to block a deploy."
      dr_warn "The orchestrator's startup hook returns a session abandoned mid-finalise"
      dr_warn "to 'uploading', so the browser can resume it; nothing is lost but time."
      return 2
    fi
  done
}

case "$MODE" in
  check)   do_check "$@" ;;
  wait)    do_wait "$@" ;;
  uploads) do_uploads "$@" ;;
  -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$0" ;;
  *) dr_die "usage: $0 {check|wait|uploads} ..." ;;
esac
