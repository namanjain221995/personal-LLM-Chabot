#!/usr/bin/env bash
# The voice archive store: finished recordings' audio on the worker's disk.
#
#   scripts/voice-store.sh up [--plain-http]   build and start the store on the worker, mint its token and TLS
#                                              certificate once, and record VOICE_ARCHIVE_URL (and the pinned
#                                              certificate) in .env; the token goes to .runtime/secrets.env
#   scripts/voice-store.sh up --candidate      a THROWAWAY store for a test: its own project, files (under
#                                              ~/.techsara-cluster/candidates/<project>), data directory and
#                                              port (VOICE_STORE_PORT, required), its token from
#                                              VOICE_STORE_TOKEN_FILE; .env and secrets.env untouched
#   scripts/voice-store.sh down [--candidate]  stop and remove the container (the recordings stay on disk)
#   scripts/voice-store.sh status              container state + the store's own /health
#   scripts/voice-store.sh logs                follow the store's log
#   scripts/voice-store.sh verify              a synthetic PUT / GET / Range / DELETE round trip (sha256
#                                              checked, another owner's DELETE refused), and the guard
#   scripts/voice-store.sh url                 the address the orchestrator should use
#   scripts/voice-store.sh rotate-token        accept a new token beside the old one; then recreate the
#                                              orchestrator; then `rotate-token --finish` drops the old one
#
# WHAT IT IS. compose/voice-store/server.py, a small authenticated object
# store for one kind of object: a finished recording's source file. The
# orchestrator's mover (orchestrator/app/voice_archive.py) copies recordings
# here, reads each one back, and only then releases the head's copy.
# docs/voice-archive.md has the design, the measurements and the runbook.
#
# NOTHING HERE TOUCHES THE MODEL OR THE HEAD. The store is its own Compose
# project (sf-local-ai-voice-store) on its own port (30011) on the WORKER, on
# the worker's efficiency cores, with no GPU; it never comes near
# sf-local-ai-worker, the main model's tensor-parallel rank 1. Nothing new is
# loaded on the head (owner rule of 2026-09-16).
#
# THE HAND-OVER IS THREE STEPS. `up` starts the store and writes its address
# (VOICE_ARCHIVE_URL) and its pinned certificate (VOICE_ARCHIVE_TLS_CERT_B64)
# into .env and its token (VOICE_ARCHIVE_TOKEN) into .runtime/secrets.env
# (0600); `./techsara up` then gives them to the orchestrator; and the mover
# starts only once VOICE_ARCHIVE_ENABLED=true is set in .env too, after
# `verify` has passed. Until then nothing moves.
#
# THE HOST GUARD. `up` refuses to start the production store unless the
# worker's LIVE ruleset (/run/techsara-host-guard/ruleset.nft, readable without
# root) lists the port in both of its sets: 30011 must be closed to the office
# LAN and the tailnet before it opens. That needs root on the worker once
# (docs/voice-archive.md, "Owner actions"). A --candidate store on a test port
# is exempt; it is protected by its token and TLS only, and removed after.
set -euo pipefail

# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

# The production store's compose file and sources live beside the other side
# stacks'. (lib/cluster-common.sh sets WORKER_REMOTE_DIR itself, so the override
# for this script is VOICE_STORE_REMOTE_DIR.)
REMOTE_DIR="${VOICE_STORE_REMOTE_DIR:-$WORKER_REMOTE_DIR}"
VOICE_STORE_MANAGEMENT_IFNAME="${VOICE_STORE_MANAGEMENT_IFNAME:-enP7s7}"
CANDIDATE=0
NO_ENV=0
PLAIN_HTTP=0
FINISH=0

action="${1:-status}"; shift || true
for arg in "$@"; do
  case "$arg" in
    --candidate) CANDIDATE=1; NO_ENV=1 ;;
    --no-env) NO_ENV=1 ;;
    --plain-http) PLAIN_HTTP=1 ;;
    --finish) FINISH=1 ;;
    *) die "unknown argument '$arg'" ;;
  esac
done

if [ "$CANDIDATE" = 1 ]; then
  # A throwaway beside production, never instead of it.
  [ -n "${VOICE_STORE_PORT:-}" ] || die "--candidate needs VOICE_STORE_PORT (a test port, not 30011)"
  [ "$VOICE_STORE_PORT" != 30011 ] || die "--candidate never uses the production port 30011"
  VOICE_STORE_PROJECT="${VOICE_STORE_PROJECT:-voice-store-candidate}"
  VOICE_STORE_FILES="${VOICE_STORE_FILES:-voice-store-candidate}"
  VOICE_STORE_DATA_DIR="${VOICE_STORE_DATA_DIR:-\$HOME/techsara-data/voice-candidate}"
  VOICE_STORE_IMAGE="${VOICE_STORE_IMAGE:-voice-store-candidate:test}"
else
  VOICE_STORE_PORT="${VOICE_STORE_PORT:-30011}"
  VOICE_STORE_PROJECT="${VOICE_STORE_PROJECT:-sf-local-ai-voice-store}"
  VOICE_STORE_FILES="${VOICE_STORE_FILES:-voice-store}"
  VOICE_STORE_DATA_DIR="${VOICE_STORE_DATA_DIR:-\$HOME/techsara-data/voice}"
  VOICE_STORE_IMAGE="${VOICE_STORE_IMAGE:-sf-local-ai-voice-store:1}"
fi
case "$VOICE_STORE_PROJECT$VOICE_STORE_FILES" in
  *[!A-Za-z0-9_.-]*) die "VOICE_STORE_PROJECT and VOICE_STORE_FILES may hold letters, digits, '.', '_' and '-' only" ;;
esac
if [ "$CANDIDATE" = 1 ] && [ -z "${VOICE_STORE_REMOTE_DIR:-}" ]; then
  # A candidate keeps even its compose file apart: it must never rewrite the
  # production store's ~/.techsara-cluster/compose.voice-store.yaml.
  REMOTE_DIR="$REMOTE_DIR/candidates/$VOICE_STORE_PROJECT"
fi
FILES_DIR="$REMOTE_DIR/$VOICE_STORE_FILES"

require_worker() {
  [ "${CLUSTER_MODE:-single}" = "dual" ] \
    || die "the voice archive store runs on the WORKER only (nothing new is loaded on the head), and this deployment is single-node (TECHSARA_CLUSTER_MODE=${CLUSTER_MODE:-single})"
}

# The address the store binds: the worker's MANAGEMENT address (enP7s7,
# 192.168.9.68 today), read over ssh; never a wildcard and never a 10.100.x
# RoCE address. Every consumer (the head's orchestrator) dials the management
# address, and the host guard closes it to everyone else.
voice_store_bind_address() {
  local address
  address="$(ssh_worker "ip -4 -br addr show $VOICE_STORE_MANAGEMENT_IFNAME 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" </dev/null)" || address=""
  case "$address" in
    "") die "could not read the worker's $VOICE_STORE_MANAGEMENT_IFNAME address over ssh ($CLUSTER_WORKER_SSH); set VOICE_STORE_MANAGEMENT_IFNAME if its management interface is named differently" ;;
    0.0.0.0|::|"[::]") die "the worker's $VOICE_STORE_MANAGEMENT_IFNAME address read back as '$address'; the store never binds a wildcard" ;;
    10.100.*) die "the worker's $VOICE_STORE_MANAGEMENT_IFNAME address read back as '$address', a RoCE rail address; the store never binds the fabric" ;;
  esac
  printf '%s' "$address"
}

scheme() { if [ "$PLAIN_HTTP" = 1 ]; then printf 'http'; else printf 'https'; fi; }

# ----------------------------------------------------------------- the guard --

# port_in_set PORT "elements = { a, b-c, ... }" -> 0 when listed.
port_in_set() {
  local port="$1" line="$2" element
  line="${line#*\{}"; line="${line%\}*}"
  local IFS=,
  for element in $line; do
    element="${element// /}"
    [ -n "$element" ] || continue
    if [[ "$element" == *-* ]]; then
      (( port >= ${element%-*} && port <= ${element#*-} )) && return 0
    else
      [ "$element" = "$port" ] && return 0
    fi
  done
  return 1
}

# Whether the filter RUNNING on the worker guards the port in both sets: it
# is judged at all (guarded_ports) and accepted from the head only
# (head_lan_ports). The applied ruleset is world-readable; the nft table is not.
guard_lists_port() {
  local ruleset guarded head_lan
  ruleset="$(ssh_worker "cat /run/techsara-host-guard/ruleset.nft" </dev/null 2>/dev/null)" || return 1
  guarded="$(awk '/set guarded_ports/ {f=1} f && /elements/ {print; exit}' <<<"$ruleset")"
  head_lan="$(awk '/set head_lan_ports/ {f=1} f && /elements/ {print; exit}' <<<"$ruleset")"
  [ -n "$guarded" ] && [ -n "$head_lan" ] || return 1
  port_in_set "$VOICE_STORE_PORT" "$guarded" && port_in_set "$VOICE_STORE_PORT" "$head_lan"
}

require_guard() {
  if guard_lists_port; then
    check_pass "the worker's host guard judges $VOICE_STORE_PORT and admits only the head"
    return 0
  fi
  check_fail "the host guard RUNNING on the worker does not list $VOICE_STORE_PORT in both of its sets"
  # This checkout's guard (which lists the port) goes to the worker now, so
  # the root steps below apply the right rules.
  local remote_dir
  remote_dir="$(ssh_worker "mkdir -p $REMOTE_DIR && echo $REMOTE_DIR" </dev/null)" \
    && scp_to_worker "$ROOT/scripts/host-guard.sh" "$remote_dir" \
    && check_pass "copied this checkout's scripts/host-guard.sh to the worker's ~/.techsara-cluster/"
  printf '  Nothing was started. As root on the worker (ssh -t %s), then run up again:\n' "$CLUSTER_WORKER_SSH"
  printf '      sudo bash ~/.techsara-cluster/host-guard.sh plan --role worker\n'
  printf '      sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker\n'
  printf '      sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker\n'
  printf '      bash ~/.techsara-cluster/host-guard.sh verify --role worker\n'
  exit 2
}

# ----------------------------------------------------------------- the token --

# The token, printed on stdout (every message goes to stderr). Production: the
# one .runtime/secrets.env holds, or a new one minted ONCE (32 random bytes)
# and appended there (0600). A candidate: VOICE_STORE_TOKEN_FILE only.
store_token() {
  local token
  if [ "$NO_ENV" = 1 ]; then
    [ -n "${VOICE_STORE_TOKEN_FILE:-}" ] || die "a --candidate or --no-env store takes its token from VOICE_STORE_TOKEN_FILE (a 0600 file); secrets.env is never written for it"
    token="$(tr -d '[:space:]' <"$VOICE_STORE_TOKEN_FILE")" || die "could not read VOICE_STORE_TOKEN_FILE"
    [ "${#token}" -ge 32 ] || die "the token in VOICE_STORE_TOKEN_FILE is shorter than 32 characters"
    printf '%s' "$token"
    return 0
  fi
  token="$(env_get "$SECRETS_ENV" VOICE_ARCHIVE_TOKEN 2>/dev/null || true)"
  if [ -n "$token" ]; then
    printf '%s' "$token"
    return 0
  fi
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  ( umask 077; mkdir -p "$(dirname "$SECRETS_ENV")"; touch "$SECRETS_ENV" )
  chmod 0600 "$SECRETS_ENV"
  printf '\n# Voice archive store token (generated %s by scripts/voice-store.sh).\nVOICE_ARCHIVE_TOKEN=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$token" >>"$SECRETS_ENV"
  log_info "generated VOICE_ARCHIVE_TOKEN into .runtime/secrets.env" >&2
  printf '%s' "$token"
}

# store.env on the worker, 0600 under umask 077, over ssh STDIN: a token on a
# command line is visible to every user of both nodes in `ps`.
write_worker_tokens() { # write_worker_tokens <comma-separated tokens>
  printf '# The voice archive store token(s). Written by scripts/voice-store.sh on %s; keep 0600.\nVOICE_STORE_TOKENS=%s\n' \
    "$(hostname)" "$1" \
    | ssh_worker "umask 077 && mkdir -p $FILES_DIR && cat >$FILES_DIR/store.env.tmp && chmod 0600 $FILES_DIR/store.env.tmp && mv -f $FILES_DIR/store.env.tmp $FILES_DIR/store.env" \
    || die "could not write the store token on the worker"
  check_pass "token in place on the worker (0600)"
}

current_worker_tokens() {
  ssh_worker "sed -n 's/^VOICE_STORE_TOKENS=//p' $FILES_DIR/store.env 2>/dev/null" </dev/null
}

# set_secret <file> <key>: set KEY=<value> in a secrets file, the value read
# from STDIN. Never an argument: /proc/<pid>/cmdline is readable by every
# user of the head (no hidepid), which is why the token goes to the worker
# over ssh stdin too. Every KEY= line gets the value (or one is added), and
# the file is replaced atomically, still 0600.
set_secret() {
  python3 -c '
import os, sys, tempfile
path, key = sys.argv[1], sys.argv[2]
value = sys.stdin.read().strip()
if not value or "\n" in value:
    sys.exit("set_secret: the value is empty or more than one line")
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
out = [key + "=" + value if line.startswith(key + "=") else line for line in lines]
if out == lines:
    out.append(key + "=" + value)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".secrets.")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$1" "$2"
}

# ------------------------------------------------------------------- the TLS --

# A P-256 key and a self-signed certificate for the bind address, made ON the
# worker (the key never leaves it; 0600) and kept until the address changes.
ensure_tls() { # ensure_tls <bind>
  [ "$PLAIN_HTTP" = 0 ] || { ssh_worker "umask 077 && mkdir -p $FILES_DIR/tls" </dev/null; return 0; }
  ssh_worker "bash -s" <<EOS || die "could not make the store's TLS certificate on the worker"
set -euo pipefail
umask 077
mkdir -p $FILES_DIR/tls
cd $FILES_DIR/tls
if [ -s cert.pem ] && [ -s key.pem ] && openssl x509 -in cert.pem -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$1\$"; then
  exit 0
fi
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout key.pem.tmp -out cert.pem.tmp -days 3650 -subj /CN=techsara-voice-store \
  -addext subjectAltName=IP:$1 2>/dev/null
chmod 0600 key.pem.tmp
chmod 0644 cert.pem.tmp
mv -f key.pem.tmp key.pem
mv -f cert.pem.tmp cert.pem
EOS
  check_pass "TLS certificate for $1 in place on the worker (key 0600, never copied off it)"
}

worker_certificate() {
  ssh_worker "cat $FILES_DIR/tls/cert.pem" </dev/null
}

# ------------------------------------------------------------------- the rest --

sync_files() {
  local remote_dir
  log_info "syncing the store to $CLUSTER_WORKER_SSH"
  ssh_worker "umask 077 && mkdir -p $FILES_DIR" </dev/null
  remote_dir="$(ssh_worker "echo $REMOTE_DIR" </dev/null)"
  scp_to_worker "$ROOT/compose/compose.voice-store.yaml" "$remote_dir" \
    || die "could not copy the compose file to the worker"
  scp_to_worker "$ROOT/compose/voice-store/Dockerfile" "$ROOT/compose/voice-store/server.py" \
    "$ROOT/compose/voice-store/requirements.txt" "$remote_dir/$VOICE_STORE_FILES" \
    || die "could not copy the store sources to the worker"
  check_pass "store synced"
}

ensure_data_dir() {
  ssh_worker "umask 077 && mkdir -p $VOICE_STORE_DATA_DIR && chmod 0700 $VOICE_STORE_DATA_DIR \$(dirname $VOICE_STORE_DATA_DIR)" </dev/null \
    || die "could not make the data directory on the worker"
}

store_compose() { # store_compose <bind> ARGS...
  local bind="$1" tls_env=""; shift
  [ "$PLAIN_HTTP" = 1 ] && tls_env="VOICE_STORE_TLS_CERT= VOICE_STORE_TLS_KEY="
  ssh_worker "cd $REMOTE_DIR && VOICE_STORE_BIND=$bind VOICE_STORE_PORT=$VOICE_STORE_PORT \
VOICE_STORE_FILES=$VOICE_STORE_FILES VOICE_STORE_DATA_DIR=$VOICE_STORE_DATA_DIR \
VOICE_STORE_IMAGE=$VOICE_STORE_IMAGE VOICE_STORE_UID=\$(id -u) VOICE_STORE_GID=\$(id -g) $tls_env \
docker compose --project-name $VOICE_STORE_PROJECT -f compose.voice-store.yaml $*" </dev/null
}

# Compose renders the project for every command and the env_file is required;
# `down` and `status` must still work after someone removed it.
ensure_worker_files() {
  ssh_worker "umask 077 && mkdir -p $FILES_DIR/tls && touch $FILES_DIR/store.env" </dev/null
}

store_url() { printf '%s://%s:%s' "$(scheme)" "$1" "$VOICE_STORE_PORT"; }

# curl from the head against the store, pinned when TLS. The token goes in a
# 0600 header FILE (-H @file), never on the command line.
store_curl() { # store_curl <bind> <token-header-file|-> ARGS...
  local bind="$1" header="$2"; shift 2
  local tls=()
  if [ "$PLAIN_HTTP" = 0 ]; then tls=(--cacert "$WORK/cert.pem"); fi
  if [ "$header" = - ]; then
    curl -sS -m 30 "${tls[@]}" "$@"
  else
    curl -sS -m 30 "${tls[@]}" -H @"$header" "$@"
  fi
}

wait_ready() { # wait_ready <bind>
  local deadline body
  deadline=$(( $(date +%s) + ${VOICE_STORE_READY_TIMEOUT_S:-180} ))
  while :; do
    body="$(store_curl "$1" - "$(store_url "$1")/health" 2>/dev/null || true)"
    case "$body" in *'"ready":true'*) check_pass "store ready at $(store_url "$1")"; return 0 ;; esac
    [ "$(date +%s)" -lt "$deadline" ] || die "the store was not ready in time (scripts/voice-store.sh logs)"
    sleep 3
  done
}

# .env: MERGED keys, idempotent.
_set_env() { # _set_env KEY VALUE
  local key="$1" value="$2" env_file="$ROOT/.env"
  touch "$env_file"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

prepare_work() {
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/voice-store.XXXXXX")"
  chmod 0700 "$WORK"
  trap 'rm -rf "$WORK"' EXIT
}

token_header() { # token_header <token> -> path of a 0600 header file
  ( umask 077; printf 'Authorization: Bearer %s\n' "$1" >"$WORK/auth" )
  printf '%s' "$WORK/auth"
}

fetch_certificate() {
  [ "$PLAIN_HTTP" = 1 ] && return 0
  worker_certificate >"$WORK/cert.pem" || die "could not read the store's certificate from the worker"
  grep -q "BEGIN CERTIFICATE" "$WORK/cert.pem" || die "the worker's cert.pem is not a certificate"
}

# expect WANT GOT PASS-MESSAGE FAIL-MESSAGE
expect() {
  if [ "$1" = "$2" ]; then check_pass "$3"; else check_fail "$4"; fi
}

# The round trip `verify` runs: 64 KiB under user 0 (reserved, no account
# has it) and a fresh session id, removed at the end whatever happens. It
# speaks as a throwaway owner of its own (X-Archive-Owner): every object on
# the store belongs to one deployment, and only that one may delete it.
round_trip() { # round_trip <bind> <header-file>
  local bind="$1" header="$2" sid sum url code got owner other
  sid="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  owner="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  other="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  head -c 65536 /dev/urandom >"$WORK/probe.bin"
  sum="$(sha256sum "$WORK/probe.bin" | cut -d' ' -f1)"
  url="$(store_url "$bind")/v1/recordings/0/$sid/source.webm"
  # shellcheck disable=SC2064
  trap "store_curl '$bind' '$header' -o /dev/null -X DELETE -H 'X-Archive-Owner: $owner' '$(store_url "$bind")/v1/recordings/0/$sid' >/dev/null 2>&1 || true; rm -rf '$WORK'" EXIT
  code="$(store_curl "$bind" "$header" -o /dev/null -w '%{http_code}' -X PUT --data-binary @"$WORK/probe.bin" \
    -H "X-Content-SHA256: $sum" -H "X-Archive-Owner: $owner" -H 'Content-Type: application/octet-stream' "$url")"
  expect "201" "$code" "PUT stored 64 KiB (201)" "PUT answered $code, not 201"
  code="$(store_curl "$bind" "$header" -o /dev/null -w '%{http_code}' -X PUT --data-binary @"$WORK/probe.bin" \
    -H "X-Content-SHA256: $sum" -H "X-Archive-Owner: $owner" -H 'Content-Type: application/octet-stream' "$url")"
  expect "200" "$code" "the same PUT again is 200 (already stored)" "a repeated PUT answered $code, not 200"
  got="$(store_curl "$bind" "$header" "$url" | sha256sum | cut -d' ' -f1)"
  expect "$sum" "$got" "GET returns the same sha256" "GET returned different bytes"
  code="$(store_curl "$bind" "$header" -o "$WORK/range.bin" -w '%{http_code}' -H 'Range: bytes=10-19' "$url")"
  if [ "$code" = 206 ] && cmp -s "$WORK/range.bin" <(tail -c +11 "$WORK/probe.bin" | head -c 10); then
    check_pass "a Range read is 206 with the right bytes"
  else
    check_fail "a Range read answered $code or the wrong bytes"
  fi
  code="$(store_curl "$bind" - -o /dev/null -w '%{http_code}' "$url")"
  expect "401" "$code" "without the token: 401" "without the token the store answered $code"
  code="$(store_curl "$bind" "$header" -o /dev/null -w '%{http_code}' -X DELETE -H "X-Archive-Owner: $other" \
    "$(store_url "$bind")/v1/recordings/0/$sid")"
  expect "409" "$code" "DELETE by another deployment: 409, nothing deleted" "a DELETE by another deployment answered $code, not 409"
  code="$(store_curl "$bind" "$header" -o /dev/null -w '%{http_code}' -X DELETE -H "X-Archive-Owner: $owner" \
    "$(store_url "$bind")/v1/recordings/0/$sid")"
  expect "204" "$code" "DELETE by its owner: 204" "DELETE answered $code"
  code="$(store_curl "$bind" "$header" -o /dev/null -w '%{http_code}' "$url")"
  expect "404" "$code" "gone after the DELETE (404)" "after the DELETE the store answered $code"
}

# ------------------------------------------------------------------ commands --

case "$action" in
  up)
    require_worker
    bind="$(voice_store_bind_address)"
    if [ "$CANDIDATE" = 0 ]; then
      require_guard
    else
      check_warn "--candidate: port $VOICE_STORE_PORT is not in the host guard; the token and TLS alone protect it until 'down --candidate'"
    fi
    prepare_work
    token="$(store_token)"
    log_info "bringing the voice archive store up on the worker ($CLUSTER_WORKER_SSH) at $(store_url "$bind")"
    sync_files
    ensure_data_dir
    ensure_tls "$bind"
    existing="$(current_worker_tokens || true)"
    case ",$existing," in
      *",$token,"*) write_worker_tokens "$existing" ;;  # a rotation in progress keeps both
      *) write_worker_tokens "$token" ;;
    esac
    store_compose "$bind" up -d --build
    fetch_certificate
    wait_ready "$bind"
    if [ "$NO_ENV" = 1 ]; then
      log_info "--candidate/--no-env: .env and .runtime/secrets.env untouched; the store is at $(store_url "$bind")"
    else
      _set_env VOICE_ARCHIVE_URL "$(store_url "$bind")"
      if [ "$PLAIN_HTTP" = 1 ]; then
        _set_env VOICE_ARCHIVE_TLS_CERT_B64 ""
      else
        _set_env VOICE_ARCHIVE_TLS_CERT_B64 "$(base64 -w0 <"$WORK/cert.pem")"
      fi
      check_pass "recorded VOICE_ARCHIVE_URL=$(store_url "$bind") and the pinned certificate in .env"
      log_info "next: scripts/voice-store.sh verify; ./techsara up; then VOICE_ARCHIVE_ENABLED=true in .env and ./techsara up again"
    fi
    ;;
  down|stop)
    require_worker
    bind="$(voice_store_bind_address)"
    ensure_worker_files
    store_compose "$bind" "$([ "$action" = down ] && echo down || echo stop)"
    if [ "$CANDIDATE" = 0 ]; then
      check_warn "the recordings stay in $VOICE_STORE_DATA_DIR on the worker. While any recording is archived the orchestrator needs this store: to retire it, set VOICE_ARCHIVE_ENABLED=false and run 'docker exec sf-local-ai-orchestrator-1 python -m app.voice_archive recall-all' FIRST"
    fi
    ;;
  status)
    require_worker
    bind="$(voice_store_bind_address)"
    ensure_worker_files
    prepare_work
    printf '\n== worker (%s), %s ==\n' "$CLUSTER_WORKER_SSH" "$(store_url "$bind")"
    store_compose "$bind" ps || true
    ( fetch_certificate ) 2>/dev/null || true
    store_curl "$bind" - "$(store_url "$bind")/health" || echo "  the store is not answering"
    echo
    ;;
  logs)
    require_worker
    bind="$(voice_store_bind_address)"
    ensure_worker_files
    store_compose "$bind" logs -f
    ;;
  url)
    require_worker
    store_url "$(voice_store_bind_address)"
    echo
    ;;
  verify)
    require_worker
    bind="$(voice_store_bind_address)"
    prepare_work
    fetch_certificate
    token="$(store_token)"
    section "the store at $(store_url "$bind")"
    body="$(store_curl "$bind" - "$(store_url "$bind")/health" 2>/dev/null || true)"
    case "$body" in *'"ready":true'*) check_pass "health: ready" ;; *) check_fail "health: not ready ($body)" ;; esac
    round_trip "$bind" "$(token_header "$token")"
    if [ "$CANDIDATE" = 0 ]; then
      section "the host guard"
      if guard_lists_port; then check_pass "30011 is judged by the live guard and admitted from the head only"; else check_fail "the live guard does not list $VOICE_STORE_PORT"; fi
    fi
    check_summary
    ;;
  rotate-token)
    require_worker
    [ "$NO_ENV" = 0 ] || die "rotate-token is for the production store"
    bind="$(voice_store_bind_address)"
    old="$(env_get "$SECRETS_ENV" VOICE_ARCHIVE_TOKEN 2>/dev/null || true)"
    [ -n "$old" ] || die "no VOICE_ARCHIVE_TOKEN in .runtime/secrets.env to rotate"
    if [ "$FINISH" = 1 ]; then
      write_worker_tokens "$old"
      store_compose "$bind" up -d
      check_pass "the store now accepts only the current token"
    else
      new="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
      write_worker_tokens "$new,$old"
      store_compose "$bind" up -d
      printf '%s' "$new" | set_secret "$SECRETS_ENV" VOICE_ARCHIVE_TOKEN
      check_pass "the store accepts the new token and the old one; .runtime/secrets.env holds the new one"
      log_info "next: ./techsara up (the orchestrator picks up the new token), then scripts/voice-store.sh rotate-token --finish"
    fi
    ;;
  *)
    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
