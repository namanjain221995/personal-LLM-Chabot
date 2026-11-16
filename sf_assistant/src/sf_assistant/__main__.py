"""``python -m sf_assistant <command> ...``.

Commands:

* ``ask`` - the question cli (``sf_assistant.cli``): the same flags and output as ``ask.sh``, which runs it.
  ``python -m sf_assistant ask --help`` lists the flags.
"""
from __future__ import annotations

import sys

USAGE = """usage: python -m sf_assistant <command> [arguments]

commands:
  ask    ask the Salesforce assistant a question (the flags of ./ask.sh; see: python -m sf_assistant ask --help)
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "ask":
        from sf_assistant.cli import main as ask_main

        return ask_main(rest, prog="python -m sf_assistant ask")
    sys.stderr.write(f"unknown command {command!r}\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
