#!/usr/bin/env python3
"""Turn a dotenv file into shell-quoted `export` lines.

    eval "$(python3 scripts/lib/env_export.py .env)"

This exists so no script ever has to `. ./.env` again. Sourcing a dotenv file
does not read it, it EXECUTES it: a value holding a space becomes a command,
a value holding parentheses is a syntax error that abandons the rest of the
file, and a value holding quotes is silently stripped of them. All three shapes
are present in this repository's own env files.

Values here go through the launcher's canonical parser -- the same function
`techsara` and the Compose wrapper use -- and come back out through
`shlex.quote`, so what a shell ends up with is exactly what Compose puts in the
containers. That equivalence is asserted by
launcher/tests/test_env_file_shapes.py::ComposeParityTests.

Nothing is printed unless every file parsed, and errors carry the key only,
never the value.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "launcher"))

try:
    from techsara_cli.utils import parse_env_file
except ImportError as exc:  # pragma: no cover - a broken checkout
    print(f"env_export: cannot import the canonical parser: {exc}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: env_export.py FILE [FILE...]", file=sys.stderr)
        return 2

    merged: dict[str, str] = {}
    for name in argv:
        path = Path(name)
        if not path.is_file():
            print(f"env_export: no such env file: {name}", file=sys.stderr)
            return 1
        # Later files win, which is the same precedence Compose applies to a
        # repeated --env-file.
        merged.update(parse_env_file(path))

    out = [f"export {key}={shlex.quote(value)}" for key, value in merged.items()]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
