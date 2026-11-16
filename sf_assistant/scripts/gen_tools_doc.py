#!/usr/bin/env python3
"""Write docs/tools.md from the tool registry (sf_assistant.tools.registry.tools_markdown).

    .venv/bin/python scripts/gen_tools_doc.py           # write docs/tools.md
    .venv/bin/python scripts/gen_tools_doc.py --check   # exit 1 when docs/tools.md is not current

It reads only the tool definitions: no knowledge file, no warehouse, no model.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sf_assistant.tools.registry import tools_markdown

DOC = Path(__file__).resolve().parents[1] / "docs" / "tools.md"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="only check that docs/tools.md is current")
    a = p.parse_args(argv)
    text = tools_markdown()
    current = DOC.read_text(encoding="utf-8") if DOC.is_file() else None
    if a.check:
        if current != text:
            print(f"{DOC} is not current: run scripts/gen_tools_doc.py", file=sys.stderr)
            return 1
        print(f"{DOC} is current")
        return 0
    if current == text:
        print(f"{DOC} is already current")
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {DOC} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
