#!/usr/bin/env bash
# The awkward inputs, one after another, against a running deployment.
#
#   VIDEO_SMOKE_EMAIL=... VIDEO_SMOKE_PASSWORD=... scripts/video_edge_cases.sh <dir-with-videos>
#
# Every case runs through scripts/video_smoke.py — the real upload and chat
# routes — and prints the analysis status at the end. A case that fails
# loudly (a corrupt file) is SUPPOSED to: what matters is that the chat says
# why and the process moves on to the next one.
set -uo pipefail
DIR="${1:?directory holding the test videos}"
REPORTS="${2:-$DIR/reports}"
mkdir -p "$REPORTS"

run() { # run <file> [<ask>...]
  local file="$DIR/$1"; shift
  [ -f "$file" ] || { echo "== skip $1 (missing)"; return; }
  echo; echo "================ $(basename "$file") ================"
  local args=()
  for q in "$@"; do args+=(--ask "$q"); done
  python3 "$(dirname "$0")/video_smoke.py" --video "$file" --effort fast --report "$REPORTS/$(basename "$file").smoke.json" "${args[@]}" 2>&1 \
    | grep -vE "^\s*$" | sed 's/^/  /' | tail -60
}

run clip_12s.mp4 "What is this clip about?"
run silent_screen_90s.mp4 "What is on the slides?" "What did the speaker say?"
run talking_head_2min.mp4 "What is on screen?" "What is the reading about?"
run corrupt.mp4
run screencast_vp9.webm "What happens in this recording?"
run hinglish_2min.mp4 "यह tutorial किस बारे में है?" "What is the tutorial about?"
run long_33min.mp4 "Which chapters of the book are read, and in what order?" "What is on screen around 20:00?"
