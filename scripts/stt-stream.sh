#!/usr/bin/env bash
# Live dictation: the streaming speech-to-text engine on the worker's CPU.
#
#   scripts/stt-stream.sh up [--no-env]    fetch the pinned models onto the worker, build the
#                                          image there and start the engine; record its
#                                          address in .env and its token in .runtime/secrets.env
#   scripts/stt-stream.sh down [--no-env]  stop and remove it (the models stay) and take its
#                                          address out of .env
#   scripts/stt-stream.sh status           container state + the engine's own /health
#   scripts/stt-stream.sh logs             follow the engine log
#   scripts/stt-stream.sh verify           stream two real clips through it and print what it heard
#   scripts/stt-stream.sh url              print the address the orchestrator should use
#
#   --no-env  leave .env and .runtime/secrets.env exactly as they are. For a
#             candidate test: the engine runs, but the production orchestrator
#             is not pointed at it on its next recreate. The token then comes
#             from STT_TOKEN_FILE (a 0600 file holding it) or from a
#             VOICE_LIVE_ENGINE_TOKEN secrets.env already has.
#
# WHAT IT IS. compose/stt-stream/server.py: Nemotron streaming ASR on
# sherpa-onnx, on the CPU, behind the orchestrator's live-dictation WebSocket
# gateway (build spec sections 3 and 7). The stored recording and whisper
# stay the record; this is the preview that shows words while someone speaks.
#
# NOTHING HERE TOUCHES THE LLM OR THE HEAD. The engine is its own Compose
# project (sf-local-ai-stt) on its own port (30009) on the WORKER: nothing new
# is loaded on the head (owner rule of 2026-09-16), and it never comes near
# sf-local-ai-worker, the main model's tensor-parallel rank 1. It uses no GPU:
# chat decode runs at the speed of the slower rank, so the engine is pinned to
# the worker's ten Cortex-X925 cores and capped at eight (the compose file has
# the measurements).
#
# THE HAND-OVER IS TWO STEPS, like scripts/whisper.sh. `up` starts the engine
# and MERGES its address into VOICE_LIVE_ENGINE_URLS in .env (the token goes
# to .runtime/secrets.env, 0600, which compose.yaml hands the orchestrator as
# an env_file); the orchestrator picks both up when `./techsara up` next
# recreates it. `down` takes the address out again, so the orchestrator is
# never left dialling an engine that is gone.
#
# THE HOST GUARD. The worker's packet filter (scripts/host-guard.sh) judges
# only the ports it lists. 30009 is in the repository's list, but the filter
# running on the worker is the copy installed as root; until it is
# reinstalled, 30009 answers the office LAN too (the stream itself still
# demands the token; /health and /metrics do not). `up` checks and prints
# the commands when that is so.
set -euo pipefail

# shellcheck source=lib/cluster-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/cluster-common.sh"

cluster_load_settings

STT_PROJECT="${STT_PROJECT:-sf-local-ai-stt}"
STT_PORT="${STT_PORT:-30009}"
# The worker's management interface; its address is what the engine binds.
STT_MANAGEMENT_IFNAME="${STT_MANAGEMENT_IFNAME:-enP7s7}"
# The worker's model cache (the launcher records it for the cluster); the
# models land in <cache>/repos/ next to the main model's shard.
STT_MODEL_CACHE="${STT_MODEL_CACHE:-${CLUSTER_WORKER_MODEL_CACHE:-${TECHSARA_MODEL_CACHE:-$HOME/Documents/project/Model}}}"
# How long `up` waits for /health to say ready: four recognizers load and warm
# up in a few seconds from a warm page cache, longer after a reboot.
STT_READY_TIMEOUT_S="${STT_READY_TIMEOUT_S:-300}"
REMOTE_DIR="${WORKER_REMOTE_DIR:-\$HOME/.techsara-cluster}"
NO_ENV=0

# ------------------------------------------------------------------ models --
#
# PINNED BY HUGGING FACE COMMIT, CHECKED BY SHA-256. The four sherpa-onnx int8
# exports the engine's profiles name (build spec 8E), each at the commit it was
# measured at on 2026-09-29, and the sha256 of every file the engine reads; the
# test clips are what `verify` streams. A moving "main" would change what the
# platform transcribes with, silently, and a truncated download would load as
# a model that decodes garbage.
#
#   key        repository                                                                  commit
STT_MODELS="
en-160     csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25   237e551abd7a411ef92d3595454d9f6ab5fe7d6c
multi-160  csukuangfj2/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11    b3a4dbde84fba1a13cb4270e6730b525ac6a2db6
en-560     csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25   52056fdc070914a48dcd68b31b44d6a6f5b85902
multi-560  csukuangfj2/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11    ab43d895f5985b1bbab8b6eac8607fcdc05343f3
"
#   key        file                sha256
STT_FILES="
en-160     encoder.int8.onnx   71111f61b18e1e65e01e369434a5c0434868d2f44892742ae54240600c681209
en-160     decoder.int8.onnx   0be9702c2f427a2b6bb241d298e0d3836a558de1f5b9fd3018f1cce6e2b3fa98
en-160     joiner.int8.onnx    a35eac38a22ebceb04d230ed7afe0d68f446ba6914a036b97f14fece95967e23
en-160     tokens.txt          dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff
en-160     test_wavs/0.wav     6bc58a4efdf20daac252b6b1502632601a71efe0308f6757dc1eda34891a7e4f
multi-160  encoder.int8.onnx   e1b39e5e16bef578a54ed2fba5f031438e000cc36c3ea2ca49d55699d5baebd4
multi-160  decoder.int8.onnx   19f9c98fc6d0a2c33a65a43b36fdb2e914c26c0aa9764be3aebc502a1e982fb0
multi-160  joiner.int8.onnx    4101c7c679a0bc30483794b27a059e34e79232aa2068d78d51231a22c8b0d7ce
multi-160  tokens.txt          729cc103155bafa785f9cd45746cd41cabe97eab7182fc04d594129587958f8a
multi-160  test_wavs/en.wav    eb1eb008904465b74c304aad8342e8c7d3c6e61ffe9f66adcaca9cf0f76a93f4
en-560     encoder.int8.onnx   7d932213491ad355c6e5576705dc3494731a52af87d7a1b954559340147909d8
en-560     decoder.int8.onnx   0be9702c2f427a2b6bb241d298e0d3836a558de1f5b9fd3018f1cce6e2b3fa98
en-560     joiner.int8.onnx    a35eac38a22ebceb04d230ed7afe0d68f446ba6914a036b97f14fece95967e23
en-560     tokens.txt          dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff
multi-560  encoder.int8.onnx   012e9321373af99021415e0b0eb3ec827b4be3153be6f30d9b448fe65e896e68
multi-560  decoder.int8.onnx   19f9c98fc6d0a2c33a65a43b36fdb2e914c26c0aa9764be3aebc502a1e982fb0
multi-560  joiner.int8.onnx    4101c7c679a0bc30483794b27a059e34e79232aa2068d78d51231a22c8b0d7ce
multi-560  tokens.txt          729cc103155bafa785f9cd45746cd41cabe97eab7182fc04d594129587958f8a
"

# The directory a model lives in, under <cache>/repos/. The launcher's model
# manager slug: org--name--<12 characters of the revision>.
model_dir() { # model_dir <key>
  local key repo rev
  while read -r key repo rev; do
    [ "$key" = "$1" ] || continue
    printf '%s--%s' "${repo//\//--}" "${rev:0:12}"
    return 0
  done <<<"$STT_MODELS"
  die "no pinned model called '$1'"
}

# Fetch whatever is missing or wrong, on the worker, straight from the Hugging
# Face mirror at the pinned commit. A file is downloaded to a .part name,
# checked, and only then moved into place; one whose sha256 does not match
# stops everything, because the engine would load it without complaint.
ensure_models() {
  local key k repo rev file sum script=""
  log_info "checking the four pinned models in $STT_MODEL_CACHE/repos on the worker"
  while read -r key file sum; do
    [ -n "$key" ] || continue
    while read -r k repo rev; do
      [ "$k" = "$key" ] && break
    done <<<"$STT_MODELS"
    script+="fetch '$STT_MODEL_CACHE/repos/$(model_dir "$key")' '$repo' '$rev' '$file' '$sum'"$'\n'
  done <<<"$STT_FILES"
  ssh_worker "bash -s" <<EOS || die "could not fetch the models onto the worker (see above)"
set -euo pipefail
fetch() { # fetch <dir> <repo> <commit> <file> <sha256>
  local dir="\$1" repo="\$2" rev="\$3" file="\$4" sum="\$5" have
  mkdir -p "\$(dirname "\$dir/\$file")"
  if [ -f "\$dir/\$file" ]; then
    have="\$(sha256sum "\$dir/\$file" | cut -d' ' -f1)"
    [ "\$have" = "\$sum" ] && return 0
    echo "  \$dir/\$file does not match its pin; fetching it again" >&2
  fi
  echo "  fetching \$repo@\${rev:0:12} \$file" >&2
  curl -fsSL --retry 3 --retry-delay 5 -o "\$dir/\$file.part" "https://huggingface.co/\$repo/resolve/\$rev/\$file"
  have="\$(sha256sum "\$dir/\$file.part" | cut -d' ' -f1)"
  if [ "\$have" != "\$sum" ]; then
    rm -f "\$dir/\$file.part"
    echo "error: \$repo@\$rev \$file has sha256 \$have, pinned \$sum" >&2
    exit 3
  fi
  chmod 0644 "\$dir/\$file.part"
  mv -f "\$dir/\$file.part" "\$dir/\$file"
}
$script
EOS
  check_pass "models present and matching their pins"
}

# --------------------------------------------------------------- the worker --

require_worker() {
  [ "${CLUSTER_MODE:-single}" = "dual" ] \
    || die "the live dictation engine runs on the WORKER only (nothing new is loaded on the head), and this deployment is single-node (TECHSARA_CLUSTER_MODE=${CLUSTER_MODE:-single})"
}

# The address the engine binds: the worker's MANAGEMENT address (enP7s7,
# 192.168.9.68 today), read over ssh; never a wildcard and never a 10.100.x
# RoCE address, which belongs to the main model's tensor-parallel fabric. The
# reasons are scripts/whisper.sh's (whisper_bind_address): every consumer on
# the head dials the management address, and the host guard closes it to
# everyone else.
stt_bind_address() {
  local address
  address="$(ssh_worker "ip -4 -br addr show $STT_MANAGEMENT_IFNAME 2>/dev/null | awk '{print \$3}' | cut -d/ -f1" </dev/null)" || address=""
  case "$address" in
    "") die "could not read the worker's $STT_MANAGEMENT_IFNAME address over ssh ($CLUSTER_WORKER_SSH); set STT_MANAGEMENT_IFNAME if its management interface is named differently" ;;
    0.0.0.0|::|"[::]") die "the worker's $STT_MANAGEMENT_IFNAME address read back as '$address'; the engine never binds a wildcard" ;;
    10.100.*) die "the worker's $STT_MANAGEMENT_IFNAME address read back as '$address', a RoCE rail address; the engine never binds the fabric" ;;
  esac
  printf '%s' "$address"
}

sync_files() {
  local remote_dir
  log_info "syncing the engine to $CLUSTER_WORKER_SSH"
  ssh_worker "mkdir -p $REMOTE_DIR/stt-stream" </dev/null
  remote_dir="$(ssh_worker "echo $REMOTE_DIR" </dev/null)"
  # The same layout as compose.whisper.yaml: the compose file in
  # ~/.techsara-cluster, the image sources (and the token file) beside it.
  scp_to_worker "$ROOT/compose/compose.stt-stream.yaml" "$remote_dir" \
    || die "could not copy the compose file to the worker"
  scp_to_worker "$ROOT/compose/stt-stream/Dockerfile" "$ROOT/compose/stt-stream/server.py" \
    "$ROOT/compose/stt-stream/requirements.txt" "$remote_dir/stt-stream" \
    || die "could not copy the engine sources to the worker"
  check_pass "engine synced"
}

# The token, printed on stdout (every message goes to stderr). In order: the
# file an operator names in STT_TOKEN_FILE, the one .runtime/secrets.env
# already holds, a new one -- minted ONCE, 32 random bytes, and appended to
# secrets.env (0600), which is not allowed under --no-env.
stt_token() {
  local token
  if [ -n "${STT_TOKEN_FILE:-}" ]; then
    token="$(tr -d '[:space:]' <"$STT_TOKEN_FILE")" || die "could not read STT_TOKEN_FILE ($STT_TOKEN_FILE)"
    [ "${#token}" -ge 16 ] || die "the token in STT_TOKEN_FILE is shorter than 16 characters"
    printf '%s' "$token"
    return 0
  fi
  token="$(env_get "$SECRETS_ENV" VOICE_LIVE_ENGINE_TOKEN 2>/dev/null || true)"
  if [ -n "$token" ]; then
    printf '%s' "$token"
    return 0
  fi
  [ "$NO_ENV" = 0 ] || die "--no-env writes no secret and .runtime/secrets.env has no VOICE_LIVE_ENGINE_TOKEN: put a token (32 or more random characters) in a 0600 file and pass STT_TOKEN_FILE=<that file>"
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  ( umask 077; mkdir -p "$(dirname "$SECRETS_ENV")"; touch "$SECRETS_ENV" )
  chmod 0600 "$SECRETS_ENV"
  printf '\n# Live dictation engine token (generated %s by scripts/stt-stream.sh).\nVOICE_LIVE_ENGINE_TOKEN=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$token" >>"$SECRETS_ENV"
  log_info "generated VOICE_LIVE_ENGINE_TOKEN into .runtime/secrets.env" >&2
  printf '%s' "$token"
}

# The token on the worker: $REMOTE_DIR/stt-stream/stt.env, the compose file's
# env_file, created 0600 under umask 077. It travels on stdin, never on a
# command line (ps shows those to every user on both nodes).
write_worker_token() { # write_worker_token <token>
  printf '# The live dictation engine token. Written by scripts/stt-stream.sh on %s; keep 0600.\nSTT_TOKEN=%s\n' \
    "$(hostname)" "$1" \
    | ssh_worker "umask 077 && mkdir -p $REMOTE_DIR/stt-stream && cat >$REMOTE_DIR/stt-stream/stt.env.tmp && chmod 0600 $REMOTE_DIR/stt-stream/stt.env.tmp && mv -f $REMOTE_DIR/stt-stream/stt.env.tmp $REMOTE_DIR/stt-stream/stt.env" \
    || die "could not write the engine token on the worker"
  check_pass "token in place on the worker (0600)"
}

# Compose renders the project for every command, and the env_file is
# required; `down` and `status` must still work after someone removed it, so
# an EMPTY one stands in (an engine started from it refuses: no token).
ensure_worker_env_file() {
  ssh_worker "umask 077 && mkdir -p $REMOTE_DIR/stt-stream && touch $REMOTE_DIR/stt-stream/stt.env" </dev/null
}

stt_compose() { # stt_compose <bind> ARGS...
  local bind="$1"; shift
  ssh_worker "cd $REMOTE_DIR && STT_BIND=$bind STT_PORT=$STT_PORT STT_MODEL_CACHE='$STT_MODEL_CACHE' \
STT_EN_160_DIR='$(model_dir en-160)' STT_MULTI_160_DIR='$(model_dir multi-160)' \
STT_EN_560_DIR='$(model_dir en-560)' STT_MULTI_560_DIR='$(model_dir multi-560)' \
docker compose --project-name $STT_PROJECT -f compose.stt-stream.yaml $*" </dev/null
}

health() { # health <bind> -> the engine's /health, from the head or, failing that, from the worker
  curl -fsS -m 5 "http://$1:$STT_PORT/health" 2>/dev/null \
    || ssh_worker "curl -fsS -m 5 http://$1:$STT_PORT/health" </dev/null 2>/dev/null
}

wait_ready() { # wait_ready <bind>
  local deadline body
  deadline=$(( $(date +%s) + STT_READY_TIMEOUT_S ))
  log_info "waiting up to ${STT_READY_TIMEOUT_S}s for the engine to load its four models"
  while :; do
    body="$(health "$1" || true)"
    case "$body" in *'"ready":true'*) check_pass "engine ready: $body"; return 0 ;; esac
    case "$body" in *'"error":"'*) die "the engine could not load: $body" ;; esac
    [ "$(date +%s)" -lt "$deadline" ] || die "the engine was not ready after ${STT_READY_TIMEOUT_S}s (scripts/stt-stream.sh logs)"
    sleep 5
  done
}

# Whether the filter RUNNING on the worker guards the port: its applied
# ruleset is world-readable, the nft table is not.
check_host_guard() {
  if ssh_worker "grep -qw '$STT_PORT' /run/techsara-host-guard/ruleset.nft" </dev/null 2>/dev/null; then
    check_pass "the worker's host guard already judges $STT_PORT"
    return 0
  fi
  check_warn "the host guard running on the worker does not list $STT_PORT yet, so the engine's /health and /metrics answer the office LAN (the stream itself demands the token). As root on the worker, with this checkout's scripts/host-guard.sh copied to ~/.techsara-cluster/host-guard.sh:"
  printf '      sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker\n'
  printf '      sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker\n'
  printf '      bash ~/.techsara-cluster/host-guard.sh verify --role worker\n'
}

# ----------------------------------------------------------------- endpoint --

# VOICE_LIVE_ENGINE_URLS in .env, MERGED like scripts/whisper.sh merges
# ASR_BASE_URLS (record_endpoints): everything already listed stays, in its
# order, and this engine is appended if it is new. compose.yaml passes the key
# to the orchestrator; empty means the live path is off.
record_endpoint() { # record_endpoint <url>
  local merged=() url have seen urls
  for url in $(grep -E '^VOICE_LIVE_ENGINE_URLS=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr ',' ' ') "$1"; do
    seen=no
    for have in "${merged[@]-}"; do [ "$have" = "$url" ] && seen=yes; done
    [ "$seen" = no ] && merged+=("$url")
  done
  urls="$(printf '%s,' "${merged[@]}")"; urls="${urls%,}"
  touch "$ROOT/.env"
  _set_env VOICE_LIVE_ENGINE_URLS "$urls"
  check_pass "recorded VOICE_LIVE_ENGINE_URLS=$urls in .env"
  log_info "restart the orchestrator to pick it up:  ./techsara up"
}

# The mirror of record_endpoint: an address left in .env after the engine is
# gone would have the gateway dial a refused port for every stream.
forget_endpoint() { # forget_endpoint <url>
  local remaining=() url urls
  for url in $(grep -E '^VOICE_LIVE_ENGINE_URLS=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr ',' ' '); do
    [ "$url" = "$1" ] || remaining+=("$url")
  done
  touch "$ROOT/.env"
  if [ ${#remaining[@]} -eq 0 ]; then
    _set_env VOICE_LIVE_ENGINE_URLS ""
    check_pass "no live engine left: recorded VOICE_LIVE_ENGINE_URLS= (live transcripts off) in .env"
  else
    urls="$(printf '%s,' "${remaining[@]}")"; urls="${urls%,}"
    _set_env VOICE_LIVE_ENGINE_URLS "$urls"
    check_pass "recorded VOICE_LIVE_ENGINE_URLS=$urls in .env"
  fi
  log_info "restart the orchestrator to pick it up:  ./techsara up"
}

_set_env() { # _set_env KEY VALUE -- idempotent, in .env
  local key="$1" value="$2" env_file="$ROOT/.env"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

# ------------------------------------------------------------------- verify --

# Two REAL clips, streamed at real-time pace in 40 ms frames the way the
# gateway sends them, from INSIDE the engine's container (it has the client
# library, the token and the clips, and nothing leaves the worker): the
# multilingual model's English test clip with the model's own language ID,
# then the English model's LibriSpeech clip, whose reference is printed with
# it. A health probe proves the process is up; this proves it can hear.
VERIFY_CLIENT='
import json, os, threading, time, wave
from websockets.sync.client import connect

def stream(path, language):
    with wave.open(path, "rb") as clip:
        assert clip.getframerate() == 16000 and clip.getnchannels() == 1 and clip.getsampwidth() == 2
        pcm = clip.readframes(clip.getnframes()) + bytes(2 * 16000)
    url = "ws://%s:%s/v1/stream" % (os.environ["STT_BIND"], os.environ.get("STT_PORT", "30009"))
    events = []
    with connect(url, additional_headers={"Authorization": "Bearer " + os.environ["STT_TOKEN"]},
                 compression=None, open_timeout=10) as ws:
        ws.send(json.dumps({"type": "start", "sample_rate": 16000, "encoding": "pcm_s16le", "first_sample": 0,
                            "first_u": 0, "mode": "dictation", "language": language}))
        ready = json.loads(ws.recv(timeout=10))
        started = time.monotonic()

        def read():
            for message in ws:
                event = json.loads(message)
                events.append((time.monotonic() - started, event))
                if event["type"] in ("done", "error"):
                    return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        for index in range(0, len(pcm), 1280):
            ws.send(pcm[index:index + 1280])
            time.sleep(max(0.0, started + (index // 1280 + 1) * 0.04 - time.monotonic()))
        ws.send(json.dumps({"type": "flush"}))
        reader.join(20)
    partials = [t for t, e in events if e["type"] == "partial"]
    finals = [e["text"] for _, e in events if e["type"] == "final"]
    ended = [e for _, e in events if e["type"] in ("done", "error")]
    print(json.dumps({"language": language, "profile": ready.get("profile"),
                      "first_partial_s": round(partials[0], 2) if partials else None,
                      "finals": finals, "ended": ended[-1] if ended else None}, ensure_ascii=False))

stream("/models/multi-160/test_wavs/en.wav", "auto")
stream("/models/en-160/test_wavs/0.wav", "en")
print("reference for the second: AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE THE SQUALID QUARTER OF THE BROTHELS")
'

# ----------------------------------------------------------------- commands --

action="${1:-status}"; shift || true
for arg in "$@"; do
  case "$arg" in
    --no-env) NO_ENV=1 ;;
    *) die "unknown argument '$arg' (the only flag is --no-env)" ;;
  esac
done

case "$action" in
  up)
    require_worker
    bind="$(stt_bind_address)"
    log_info "bringing the live dictation engine up on the worker ($CLUSTER_WORKER_SSH) at $bind:$STT_PORT"
    ensure_models
    token="$(stt_token)"
    sync_files
    write_worker_token "$token"
    stt_compose "$bind" up -d --build
    wait_ready "$bind"
    check_host_guard
    if [ "$NO_ENV" = 1 ]; then
      log_info "--no-env: .env and .runtime/secrets.env untouched; the engine is at ws://$bind:$STT_PORT"
    else
      record_endpoint "ws://$bind:$STT_PORT"
    fi
    ;;
  down|stop)
    require_worker
    bind="$(stt_bind_address)"
    ensure_worker_env_file
    stt_compose "$bind" "$([ "$action" = down ] && echo down || echo stop)"
    if [ "$NO_ENV" = 1 ]; then
      log_info "--no-env: .env untouched"
    else
      forget_endpoint "ws://$bind:$STT_PORT"
    fi
    ;;
  status)
    require_worker
    bind="$(stt_bind_address)"
    printf '\n== worker (%s), %s:%s ==\n' "$CLUSTER_WORKER_SSH" "$bind" "$STT_PORT"
    ensure_worker_env_file
    stt_compose "$bind" ps || true
    health "$bind" || echo "  engine not answering"
    echo
    ;;
  logs)
    require_worker
    bind="$(stt_bind_address)"
    ensure_worker_env_file
    stt_compose "$bind" logs -f
    ;;
  url)
    require_worker
    printf 'ws://%s:%s\n' "$(stt_bind_address)" "$STT_PORT"
    ;;
  verify)
    require_worker
    printf '%s' "$VERIFY_CLIENT" \
      | ssh_worker "docker exec -i \$(docker ps -q --filter label=com.docker.compose.project=$STT_PROJECT --filter label=com.docker.compose.service=stt-stream | head -n 1) python3 -" \
      || die "verify failed: is the engine running? (scripts/stt-stream.sh status)"
    ;;
  *)
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
