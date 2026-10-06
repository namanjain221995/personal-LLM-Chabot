#!/usr/bin/env bash
# Ask the new Salesforce pipeline a question from the host.
#
#   scripts/ask.sh "How many internal interviews are scheduled?"
#   scripts/ask.sh                  # interactive
#   scripts/ask.sh -v "..."         # with every trace event
#   scripts/ask.sh --file scripts/questions_50_objects.txt              # whole list, one run
#   scripts/ask.sh --file scripts/questions_50_objects.txt --out my.txt # choose the output file
#
# Stages this checkout's code into the running orchestrator container under
# /tmp (read-only use; the running app is not touched), then runs scripts/ask.py
# there, against the real models and the real warehouse. Re-stages only when
# the container has been restarted and /tmp was cleared, or with --restage.
set -euo pipefail
cd "$(dirname "$0")/.."
C=sf-local-ai-orchestrator-1

if [[ "${1:-}" == "--restage" ]]; then
  shift
  docker exec "$C" rm -rf /tmp/appcheck /tmp/sfsrc /tmp/vendor /tmp/sfk /tmp/knowledge
fi

if ! docker exec "$C" test -d /tmp/appcheck/app 2>/dev/null; then
  echo "staging code into $C ..." >&2
  docker exec "$C" sh -c 'cp -r /app /tmp/appcheck && mkdir -p /tmp/sfsrc /tmp/vendor /tmp/sfk/runtime_schema/db /tmp/knowledge'
  docker cp brain/Salesforce-Org-Data-main/src/graphrag "$C":/tmp/vendor/graphrag
  docker cp salesforce_knowledge/runtime_schema/db/production "$C":/tmp/sfk/runtime_schema/db/production
  docker cp brain/knowledge/v1/. "$C":/tmp/knowledge/
fi
# Code, config, schema and vocabulary always refreshed, so an edit here -- or a
# runtime-schema rebuild -- is picked up immediately.
docker cp orchestrator/app/engines/sfk_bridge.py "$C":/tmp/appcheck/app/engines/sfk_bridge.py
docker cp src/. "$C":/tmp/sfsrc/
docker exec "$C" rm -rf /tmp/vendor/graphrag
docker cp brain/Salesforce-Org-Data-main/src/graphrag "$C":/tmp/vendor/graphrag
docker cp salesforce_knowledge/config "$C":/tmp/sfk/
docker cp salesforce_knowledge/runtime_schema/db/production/. "$C":/tmp/sfk/runtime_schema/db/production/
docker exec "$C" mkdir -p /tmp/brain/lexicon
docker cp brain/lexicon/curated.yaml "$C":/tmp/brain/lexicon/curated.yaml
docker cp scripts/ask.py "$C":/tmp/appcheck/ask.py

# --file <questions.txt> [--out <results.txt>]
#   Runs every question in the file in ONE process (one staging, one pipeline
#   build), in the same per-question format as a single question, then a
#   summary table. Everything is saved to a .txt file on the host; its location
#   is printed at the start and at the end.
if [[ "${1:-}" == "--file" ]]; then
  QFILE="${2:-}"
  [[ -f "$QFILE" ]] || { echo "no such question file: ${QFILE:-<missing>}" >&2; exit 1; }
  OUT=""
  if [[ "${3:-}" == "--out" ]]; then
    OUT="${4:-}"
    set -- "${@:1:2}" "${@:5}"
  fi
  if [[ -z "$OUT" ]]; then
    mkdir -p ask_results
    OUT="ask_results/$(basename "${QFILE%.*}")_$(date +%Y%m%d-%H%M%S).txt"
  fi
  mkdir -p "$(dirname "$OUT")"
  OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
  COUNT=$(grep -v '^[[:space:]]*#' "$QFILE" | grep -c '[^[:space:]]' || true)
  docker cp "$QFILE" "$C":/tmp/appcheck/questions.txt

  echo "questions : $COUNT  (from $QFILE)"
  echo "results   : $OUT"
  echo "started   : $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  {
    echo "questions : $COUNT  (from $QFILE)"
    echo "started   : $(date '+%Y-%m-%d %H:%M:%S')"
  } > "$OUT"
  # No TTY: output goes to a file, and a TTY would write \r\n line endings.
  docker exec -w /tmp/appcheck \
    -e PYTHONUNBUFFERED=1 \
    -e SFK_PIPELINE_ENABLED=true \
    -e SFK_SOURCE_ROOTS=/tmp/sfsrc:/tmp/vendor \
    -e SALESFORCE_KNOWLEDGE_ROOT=/tmp/sfk \
    -e SFK_BUNDLE_DIR=/tmp/knowledge \
    -e SFK_TRACE_DB=/tmp/ask_traces.sqlite \
    "$C" python ask.py --file /tmp/appcheck/questions.txt "${@:3}" 2>&1 | tee -a "$OUT"
  STATUS=${PIPESTATUS[0]}
  echo "finished  : $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$OUT"
  echo
  echo "All answers saved to: $OUT"
  exit "$STATUS"
fi

TTY=()
[[ -t 0 ]] && TTY=(-it)
exec docker exec "${TTY[@]}" -w /tmp/appcheck \
  -e SFK_PIPELINE_ENABLED=true \
  -e SFK_SOURCE_ROOTS=/tmp/sfsrc:/tmp/vendor \
  -e SALESFORCE_KNOWLEDGE_ROOT=/tmp/sfk \
  -e SFK_BUNDLE_DIR=/tmp/knowledge \
  -e SFK_TRACE_DB=/tmp/ask_traces.sqlite \
  "$C" python ask.py "$@"
