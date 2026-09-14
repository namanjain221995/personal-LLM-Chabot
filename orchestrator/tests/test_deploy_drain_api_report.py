"""scripts/deploy-drain.sh api — the durable /v1 run report (no-timeout /v1, 2026-09-13).

Deploys never wait for /v1 (durable runs suspend and resume), so this mode is
a REPORT: one line with running, suspended, queued and quarantined runs and the
tokens a restart will make them prefill again. What is pinned, with `docker`
replaced by a PATH shim:

- the line, exit 0, well under 2 s;
- exit 0 and a plain sentence when the schema predates V36, when there is no
  database container, and when the answer is not numbers;
- the session is read-only (PGOPTIONS default_transaction_read_only=on) and
  the statements are SELECTs;
- and the report's real SQL, captured from the shim, run against the test
  database with seeded runs, counts what the line says it counts.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy-drain.sh"

_SHIM = r"""#!/usr/bin/env bash
# A fake docker: `inspect` succeeds unless FAKE_NO_CONTAINER=1; `exec … psql -c SQL`
# logs the command and answers from the environment.
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  inspect) [ "${FAKE_NO_CONTAINER:-0}" = 1 ] && exit 1; exit 0 ;;
  exec)
    sql=""
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "-c" ]; then sql="$arg"; fi
      prev="$arg"
    done
    printf '%s\n' "$sql" >> "$FAKE_SQL_LOG"
    case "$sql" in
      *information_schema.columns*) printf ' %s \n' "${FAKE_PRESENT:-t}" ;;
      *"FROM api_responses"*) printf '%s\n' "${FAKE_ROW:-0|0|0|0|0}" ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture()
def shim(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_SHIM)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "root"
    root.mkdir()
    env = dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        TECHSARA_DEPLOY_ROOT=str(root),
        FAKE_LOG=str(tmp_path / "docker.log"),
        FAKE_SQL_LOG=str(tmp_path / "sql.log"),
    )
    return env, tmp_path


def _run(env, **extra):
    started = time.monotonic()
    out = subprocess.run(["bash", str(SCRIPT), "api"], env={**env, **extra}, capture_output=True, text=True,
                         timeout=30)
    return out, time.monotonic() - started


def test_the_report_is_one_line_exit_0_and_fast(shim):
    env, tmp = shim
    out, elapsed = _run(env, FAKE_ROW="2|1|3|1|123456")
    assert out.returncode == 0, out.stderr
    assert "drain api: running=2 suspended=1 queued=3 quarantined=1 re_prefill_tokens=123456" in out.stdout
    assert "never waited for" in out.stdout
    assert elapsed < 2.0, elapsed
    calls = (tmp / "docker.log").read_text()
    assert "PGOPTIONS=-c default_transaction_read_only=on" in calls
    statements = (tmp / "sql.log").read_text().strip().splitlines()
    assert len(statements) == 2 and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert "sleep" not in SCRIPT.read_text().split("do_api()", 1)[1].split("case \"$MODE\"", 1)[0]


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({"FAKE_PRESENT": "f"}, "schema predates V36"),
        ({"FAKE_NO_CONTAINER": "1"}, "cannot read api_responses"),
        ({"FAKE_ROW": "ERROR: relation does not exist"}, "could not be read"),
    ],
)
def test_every_unanswerable_case_is_a_sentence_and_exit_0(shim, extra, expected):
    env, _tmp = shim
    out, elapsed = _run(env, **extra)
    assert out.returncode == 0
    assert expected in (out.stdout + out.stderr)
    assert "running=" not in out.stdout
    assert elapsed < 2.0


def test_the_reports_sql_counts_what_the_line_says_against_a_real_database(shim):
    env, tmp = shim
    _run(env)
    report_sql = (tmp / "sql.log").read_text().strip().splitlines()[-1]
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES ('ws-drain', 'Drain')")
    owner = int(db.create_user("drain-owner", "hash"))
    project = db.create_api_project("ws-drain", "Durable", "live", created_by=owner)
    now = datetime.now(timezone.utc)

    def run(request_id, **fields):
        row = db.create_api_response(project["id"], "ws-drain", "techsara-35b", request_id)
        db.update_api_response(row["id"], project["id"], resumable=True, **fields)

    run("running-1", status="in_progress", lease_owner="proc-a", lease_expires_at=now + timedelta(seconds=60),
        input_tokens=100_000, generated_tokens=5_000)
    run("running-2", status="in_progress", lease_owner="proc-a", lease_expires_at=now + timedelta(seconds=60),
        generated_tokens=700, engine_fault_attempts=1)
    run("suspended", status="in_progress", suspended_at=now, suspend_reason="restart",
        input_tokens=20_000, generated_tokens=300)
    run("queued-bg", status="queued")
    db.update_api_response(
        db.create_api_response(project["id"], "ws-drain", "techsara-35b", "bg", background=True)["id"],
        project["id"], resumable=True, status="queued")
    run("done", status="completed", input_tokens=9_999_999)
    with db.connection() as con:
        row = con.execute(report_sql).fetchone()
    value = next(iter(row.values()))
    # running 2, suspended 1, queued background 1, quarantined 1,
    # re-prefill = (100,000 + 5,000) + 700 + (20,000 + 300)
    assert value == "2|1|1|1|126000"
