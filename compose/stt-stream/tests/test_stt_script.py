"""scripts/stt-stream.sh's token handling, run for real in bash.

The functions are cut out of the script and run with the repository's own
scripts/lib/cluster-common.sh (die, env_get, log_info) against a secrets.env
under tmp_path: no ssh, no Docker, nothing outside the test's directory.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "stt-stream.sh"
COMMON = REPO / "scripts" / "lib" / "cluster-common.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

#: Low entropy on purpose: the secret scanner blocks random-looking fixtures.
CANDIDATE = "cand-key-1-cand-key-1"
PRODUCTION = "prod-key-1-prod-key-1"


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match is not None, f"stt-stream.sh has no function {name}"
    return match.group(0)


def stt_token(tmp_path: Path, *, no_env: bool, token_file: Optional[Path] = None):
    """(the finished bash, the secrets.env it used)."""
    secrets = tmp_path / ".runtime" / "secrets.env"
    program = "\n".join([
        f'. "{COMMON}"',
        f'SECRETS_ENV="{secrets}"',
        f"NO_ENV={1 if no_env else 0}",
        _function("refuse_token_file_without_no_env"),
        _function("stt_token"),
        "stt_token",
    ])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "NO_COLOR": "1"}
    if token_file is not None:
        env["STT_TOKEN_FILE"] = str(token_file)
    return subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, timeout=30), secrets


def token_file(tmp_path: Path, value: str = CANDIDATE) -> Path:
    path = tmp_path / "candidate.token"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def production_secret(tmp_path: Path) -> Path:
    secrets = tmp_path / ".runtime" / "secrets.env"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text(f"VOICE_LIVE_ENGINE_TOKEN={PRODUCTION}\n", encoding="utf-8")
    return secrets


def test_a_token_file_without_no_env_is_refused_and_nothing_is_written(tmp_path):
    # Review finding L2: the file's token went to the worker while
    # secrets.env kept another and .env pointed the orchestrator at the
    # engine -- every live stream refused after the next `./techsara up`.
    secrets = production_secret(tmp_path)
    before = secrets.read_text(encoding="utf-8")
    result, _ = stt_token(tmp_path, no_env=False, token_file=token_file(tmp_path))
    assert result.returncode != 0 and result.stdout == ""
    assert "STT_TOKEN_FILE" in result.stderr and "--no-env" in result.stderr
    assert CANDIDATE not in result.stderr and PRODUCTION not in result.stderr
    assert secrets.read_text(encoding="utf-8") == before


def test_a_token_file_is_the_candidates_token_under_no_env(tmp_path):
    production_secret(tmp_path)
    result, _ = stt_token(tmp_path, no_env=True, token_file=token_file(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == CANDIDATE


def test_a_short_token_file_is_refused(tmp_path):
    result, _ = stt_token(tmp_path, no_env=True, token_file=token_file(tmp_path, "short"))
    assert result.returncode != 0 and result.stdout == "" and "shorter than 16" in result.stderr


def test_without_a_file_the_secret_is_used_or_minted_once(tmp_path):
    first, secrets = stt_token(tmp_path, no_env=False)
    assert first.returncode == 0, first.stderr
    assert len(first.stdout) >= 43 and "\n" not in first.stdout
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert f"VOICE_LIVE_ENGINE_TOKEN={first.stdout}" in secrets.read_text(encoding="utf-8")
    again, _ = stt_token(tmp_path, no_env=False)
    assert again.returncode == 0 and again.stdout == first.stdout
    assert secrets.read_text(encoding="utf-8").count("VOICE_LIVE_ENGINE_TOKEN=") == 1


def test_no_env_without_any_token_refuses(tmp_path):
    result, secrets = stt_token(tmp_path, no_env=True)
    assert result.returncode != 0 and result.stdout == "" and not secrets.exists()


def test_up_refuses_a_token_file_before_it_fetches_or_starts_anything():
    text = SCRIPT.read_text(encoding="utf-8")
    up = re.search(r"^  up\)\n(.*?)^    ;;\n", text, re.S | re.M)
    assert up is not None
    steps = [line.strip() for line in up.group(1).splitlines() if line.strip()]
    first_remote = min(i for i, step in enumerate(steps) if step.startswith(("bind=", "ensure_models")))
    assert steps.index("refuse_token_file_without_no_env") < first_remote
