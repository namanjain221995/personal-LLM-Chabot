#!/usr/bin/env bash
# Regenerate every diagram in render/ from src/*.puml.
#
# Toolchain notes (see ../ASSUMPTIONS.md):
#   * plantuml is NOT installed system-wide on this host and apt needs a
#     password, so the audit downloaded plantuml.jar and drives it with the
#     system JRE. Point PLANTUML_JAR at your own copy if it moved.
#   * graphviz (`dot`) is ALSO absent, so layout uses PlantUML's built-in
#     Smetana engine via -Playout=smetana. That flag is passed on the COMMAND
#     LINE, never written into the .puml files, so the sources stay clean for
#     draw.io (which renders them with its own graphviz).
#   * `plantuml -checkonly` exits 0 even when a diagram fails to parse, so
#     correctness is judged by grepping its OUTPUT for "error", not by $?.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/src"
OUT="$HERE/render"
JAR="${PLANTUML_JAR:-/tmp/claude-1000/-home-techsphere-Documents-projects-saleforce-LLM/d87d2556-efbd-4963-bc3a-9b9f13b17475/scratchpad/plantuml.jar}"

if [ ! -f "$JAR" ]; then
  echo "plantuml.jar not found at $JAR" >&2
  echo "Download it:  curl -sSL -o plantuml.jar https://github.com/plantuml/plantuml/releases/download/v1.2024.7/plantuml-1.2024.7.jar" >&2
  exit 1
fi

mkdir -p "$OUT"

echo "== syntax check =="
fail=0
for f in "$SRC"/*.puml; do
  out="$(java -jar "$JAR" -checkonly "$f" 2>&1)"
  if echo "$out" | grep -qi "error"; then
    echo "FAIL $(basename "$f")"
    echo "$out" | sed 's/^/     /'
    fail=1
  else
    echo "ok   $(basename "$f")"
  fi
done
[ "$fail" -eq 0 ] || { echo "syntax errors above — not rendering" >&2; exit 1; }

echo "== render svg + png =="
java -DPLANTUML_LIMIT_SIZE=16384 -jar "$JAR" -Playout=smetana -tsvg -o "$OUT" "$SRC"/*.puml
java -DPLANTUML_LIMIT_SIZE=16384 -jar "$JAR" -Playout=smetana -tpng -o "$OUT" "$SRC"/*.puml

echo "== result =="
echo "svg: $(find "$OUT" -name '*.svg' | wc -l)   png: $(find "$OUT" -name '*.png' | wc -l)   src: $(find "$SRC" -name '*.puml' | wc -l)"
