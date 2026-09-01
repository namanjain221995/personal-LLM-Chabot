"""Every module must parse on the OLDEST Python this code runs on.

WHY (2026-09-01). `app/metrics.py` used a backslash inside an f-string
expression. That is legal from Python 3.12 (PEP 701) and a SyntaxError before
it — so it parsed on the dev box (3.12), imported fine in the running
container (3.12), passed the whole local suite, and then failed to import in
CI, which provisions 3.11. The version skew is real and permanent:
`orchestrator/requirements.txt` says "Python 3.11 in-container" and
`requirements-dev.txt` says the host runs 3.12 as an accepted difference.

A syntax-only check catches this class of bug in milliseconds without needing
an old interpreter installed, which is the only reason it can live in the
ordinary suite.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

#: The floor. Raise this only when every deployment target has moved.
MIN_PYTHON = (3, 11)

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_MODULES = sorted(_APP.rglob("*.py"))


def test_there_are_modules_to_check():
    """A glob that silently matches nothing would make this suite a no-op."""
    assert len(_MODULES) > 20, f"only found {len(_MODULES)} modules under {_APP}"


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: str(p.name))
def test_module_parses_on_the_oldest_supported_python(path: pathlib.Path):
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(path), feature_version=MIN_PYTHON)
    except SyntaxError as exc:  # pragma: no cover — the failure IS the message
        pytest.fail(
            f"{path.relative_to(_APP.parent)} needs Python "
            f"{'.'.join(map(str, MIN_PYTHON))}+ syntax: line {exc.lineno}: {exc.msg}\n"
            "The containers and CI run the older interpreter; a dev box on a "
            "newer one will not catch this."
        )
