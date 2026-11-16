"""``python -m sf_assistant ask ...`` and ``./ask.sh``: the same flags and output as ``sf_assistant.cli`` (offline:
mock LLM, the fake knowledge fixture, records off)."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

import sf_assistant.cli as cli
import sf_assistant.pipeline
from sf_assistant import __main__ as entry
from sf_assistant.agent.llm import LLM
from sf_assistant.agent.mock_llm import MockClient
from sf_assistant.config import Config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"


def _run(fn, argv, monkeypatch) -> tuple[int, str]:
    monkeypatch.setattr(sf_assistant.pipeline, "LLM", lambda cfg: LLM(cfg, client=MockClient()))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fn(argv)
    return code, out.getvalue()


def _norm(text: str) -> str:
    return re.sub(r"\d+\.\d+ s\b", "X s", text)


@pytest.mark.parametrize("flags", [[], ["-v"]])
def test_module_ask_prints_what_the_cli_prints(monkeypatch, flags):
    monkeypatch.setenv("OKEVAL_BASE_URL", "http://127.0.0.1:9/v1")
    argv = ["--no-records", "--knowledge", str(FIXTURE_JSON), *flags, "What values can Status__c take?"]
    code_cli, out_cli = _run(cli.main, argv, monkeypatch)
    code_mod, out_mod = _run(entry.main, ["ask", *argv], monkeypatch)
    assert code_cli == code_mod == 0
    assert "Based on the object knowledge" in out_mod
    assert _norm(out_mod) == _norm(out_cli)


def test_module_usage_and_unknown_command(capsys):
    assert entry.main([]) == 2 and "usage: python -m sf_assistant <command>" in capsys.readouterr().out
    assert entry.main(["--help"]) == 0 and "ask" in capsys.readouterr().out
    assert entry.main(["nope"]) == 2 and "unknown command 'nope'" in capsys.readouterr().err


def test_module_ask_has_the_cli_flags():
    out = subprocess.run([sys.executable, "-m", "sf_assistant", "ask", "--help"], capture_output=True, text=True,
                         check=True).stdout
    assert out.startswith("usage: python -m sf_assistant ask ")
    for flag in ("--knowledge", "--verbose", "--no-records", "--records-only", "--full-knowledge", "--max-rows",
                 "--warehouse", "--refresh-warehouse", "--today", "--tz", "--file", "--out"):
        assert flag in out, flag
    with pytest.raises(SystemExit, match="either --no-records or --records-only"):
        entry.main(["ask", "--no-records", "--records-only", "q"])


def test_ask_sh_runs_the_module_with_the_old_defaults():
    text = (ROOT / "ask.sh").read_text(encoding="utf-8")
    assert 'exec .venv/bin/python -m sf_assistant ask "$@"' in text
    assert f'OKEVAL_BASE_URL="${{OKEVAL_BASE_URL:-${{SFA_BASE_URL:-{Config.base_url}}}}}"' in text
    assert f'OKEVAL_MODEL="${{OKEVAL_MODEL:-${{SFA_MODEL:-{Config.model}}}}}"' in text
    assert 'OKEVAL_API_KEY="${OKEVAL_API_KEY:-${SFA_API_KEY:-EMPTY}}"' in text
    if not (ROOT / ".venv" / "bin" / "python").exists():
        pytest.skip("no .venv next to ask.sh")
    out = subprocess.run(["bash", str(ROOT / "ask.sh"), "--help"], capture_output=True, text=True, check=True).stdout
    assert out.startswith("usage: python -m sf_assistant ask ")
