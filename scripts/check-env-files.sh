#!/usr/bin/env bash
# Fail loudly on an env-file line that a POSIX shell would mis-parse.
#
#     scripts/check-env-files.sh                 # check the standard set
#     scripts/check-env-files.sh path/to/file    # check specific files
#
# Reports the LINE NUMBER, the KEY and the SHAPE of the problem. It never
# prints a value, so its output is safe in CI logs and in a terminal someone
# might paste into a ticket.
#
# The shapes it catches are the ones that actually occurred here:
#
#   a value with a space         -> "<word>: command not found", and under
#                                   `set -e` every later assignment is lost
#   a value with parentheses     -> a syntax error that abandons the whole file
#   a value with quotes in it    -> loads with NO error and the quotes stripped,
#                                   which is the worst case: silent corruption
#   a UTF-8 BOM                  -> welded to the first key, so that key is gone
#   an unterminated quote        -> Compose refuses to read the file at all
#
# The fix for every one of them is to quote the value. Quoting does not change
# it: Compose and the launcher's parser both strip the quotes back off. Prove
# that for a file you have edited with
#
#     python3 -c "import sys; sys.path.insert(0,'launcher'); \
#         from techsara_cli.utils import parse_env_file; \
#         from pathlib import Path; print(len(parse_env_file(Path('.env'))))"
#
# and compare the dicts before and after.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "$#" -gt 0 ]; then
    FILES=("$@")
else
    FILES=(.env .env.example .runtime/secrets.env .runtime/generated.env)
fi

python3 - "${FILES[@]}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "launcher"))
from techsara_cli.utils import check_env_file  # noqa: E402

failed = 0
checked = 0
for name in sys.argv[1:]:
    path = Path(name)
    if not path.is_file():
        continue
    checked += 1
    problems = check_env_file(path)
    if not problems:
        print(f"  ok    {name}")
        continue
    failed += 1
    print(f"  FAIL  {name}: {len(problems)} line(s) a shell would mis-parse")
    for number, key, reason in problems:
        print(f"          line {number}: {key} -- {reason}")

if not checked:
    print("no env files found to check")
    raise SystemExit(0)

if failed:
    print()
    print("Quote the values above. Quoting is value-preserving: Compose and the")
    print("launcher parser both strip the quotes back off. Do NOT work around")
    print("this by sourcing the file differently -- use scripts/lib/env-load.sh.")
    raise SystemExit(1)

print()
print(f"{checked} env file(s) clean: safe for Compose and for a shell.")
PY
