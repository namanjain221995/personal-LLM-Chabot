"""Shared test setup: the SFA_* aliases of the OKEVAL_* settings never reach a test from the developer's shell.

config.py reads an ``SFA_*`` alias (``SFA_RECORDS_TZ``, ``SFA_WAREHOUSE`` ...) when its ``OKEVAL_*`` name is unset or
blank. The notebook folder never read these names, and the ported tests clear only the ``OKEVAL_*`` names, so an
exported alias would change their results. They are removed when pytest starts (before collection and before any
module- or session-scoped fixture opens the records) and put back when it ends; a test that wants one sets it itself
(``monkeypatch.setenv``). The path variables (``SFA_HOME``, ``SFA_KNOWLEDGE_DIR`` ...) are read once at import and are
left alone on purpose. So are the switches ``SFA_RESOLVER``, ``SFA_MERGE``, ``SFA_TABLES`` (the code-built record
rows, latency plan step 3) and ``SFA_CHARTS`` (the charts of 2026-11-03): the suites run with each of them unset and set
from the shell, and a test whose subject depends on one sets it itself.
"""

from __future__ import annotations

import os

from sf_assistant.config import ENV_ALIASES

_saved: dict[str, str] = {}


def pytest_configure(config):
    for name in ENV_ALIASES.values():
        if name in os.environ:
            _saved[name] = os.environ.pop(name)


def pytest_unconfigure(config):
    os.environ.update(_saved)
    _saved.clear()
