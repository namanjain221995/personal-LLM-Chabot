#!/usr/bin/env python3
"""Rehearse scripts/deploy-rollback.sh through its REAL interface, changing nothing.

WHY THIS REPLACED THE `--help` CHECK (2026-09-13)
-------------------------------------------------
Wave 1 (2026-09-12, audit F074) wired the rollback script into CI by running
`deploy-rollback.sh --help` and grepping the text for `--to`, `--dry-run` and
the sentence "NEVER RESTORES A DATABASE". That asserts the script's
DOCUMENTATION. `--help` is an `awk` over the header comment and exits before
the argument loop has looked at anything else, so every one of these edits
left that step green:

  * deleting the `--dry-run)` case — the flag the runbook tells an operator to
    rehearse with at 3 a.m. would then be "unknown option";
  * deleting the `exit 0` after the dry-run banner — a rehearsal would go on
    to take the deploy lock, re-tag images and recreate containers;
  * turning the schema-boundary refusal into a warning — the one refusal the
    script exists to make.

So this runs the real script, with its real argument parsing and its real
schema check, inside a sandbox that cannot touch a machine:

  * TECHSARA_DEPLOY_ROOT points at a temporary directory holding a recorded
    release, so `--list`, bare-stamp resolution and `--to DIR` resolve
    against fixtures, never against a production checkout;
  * `docker` and `curl` on PATH are stubs. `docker` answers only the READ
    verbs the script uses to inspect an image (`image inspect`, `inspect`,
    `create` + `cp` + `rm -f` of the throwaway container it reads db.py out
    of) and logs everything; any other verb — `tag`, `compose`, `stop` — is
    refused with exit 97 AND recorded, so a `|| true` cannot hide it. `curl`
    answers `/health` with the schema version the scenario chose;
  * stdin is closed, so a script that fell through to its `ROLLBACK`
    confirmation prompt fails instead of waiting.

Every scenario then asserts the exit code, the sentence that proves which
branch ran, that no mutating docker verb was seen, and that no deploy lock
was created. Nothing here needs docker, a database or the DGX, so it runs on
a hosted runner on every commit.

Usage:
    rollback_rehearsal.py [--script scripts/deploy-rollback.sh] [--summary FILE]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_SCRIPT = REPO / "scripts" / "deploy-rollback.sh"

ORCH_ID = "sha256:" + "a" * 64
FRONT_ID = "sha256:" + "b" * 64
RELEASE = "20260913-010203Z"
TARGET_SCHEMA = 30

#: Docker verbs the rollback script may use while it only READS. Everything
#: else reaching the stub is a rehearsal that stopped being one.
READ_ONLY_VERBS = frozenset({"image inspect", "inspect", "create", "cp", "rm -f"})

_DOCKER_STUB = r"""#!/usr/bin/env bash
# Rehearsal stub: answers read verbs from fixtures, refuses and records the rest.
printf '%s\n' "$*" >> "$REHEARSAL_DOCKER_LOG"
case "$1" in
  image)
    [ "$2" = inspect ] || exit 97
    grep -qxF -- "$3" "$REHEARSAL_IMAGES" && { printf '%s\n' "$3"; exit 0; }
    exit 1 ;;
  inspect) printf '%s\n' "sha256:running"; exit 0 ;;
  create)  printf '%s\n' "rehearsal-cid"; exit 0 ;;
  cp)      cp "$REHEARSAL_DB_PY" "$3"; exit 0 ;;
  rm)      [ "$2" = -f ] && [ "$3" = rehearsal-cid ] && exit 0; exit 97 ;;
  *)       exit 97 ;;
esac
"""

_CURL_STUB = r"""#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    */health) printf '{"checks":{"app_db":{"schema_version":%s}}}' "$REHEARSAL_LIVE_SCHEMA"; exit 0 ;;
  esac
done
exit 7
"""


@dataclasses.dataclass
class Outcome:
    rc: int
    out: str
    docker_calls: list[str]
    lock_created: bool


@dataclasses.dataclass
class Scenario:
    name: str
    args: list[str]
    expect_rc: int  # 0, or any non-zero when given as 1
    expect_text: str
    live_schema: int = TARGET_SCHEMA
    images: tuple[str, ...] = (ORCH_ID, FRONT_ID)


def _sandbox(root: pathlib.Path) -> None:
    release = root / ".runtime" / "releases" / RELEASE
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "techsara.release-manifest",
                "images": {"orchestrator": {"id": ORCH_ID}, "frontend": {"id": FRONT_ID}},
                "schema": {"code_version": TARGET_SCHEMA},
                "git": {"head": "0123456789abcdef0123"},
            }
        ),
        encoding="utf-8",
    )
    migrations = "".join(f"    ({v}, _MIGRATION_V{v}),\n" for v in range(1, TARGET_SCHEMA + 1))
    (root / "db.py").write_text(f"_MIGRATIONS = [\n{migrations}]\n", encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _DOCKER_STUB), ("curl", _CURL_STUB)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run(script: pathlib.Path, scenario: Scenario) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="rollback-rehearsal-") as tmp:
        root = pathlib.Path(tmp)
        _sandbox(root)
        (root / "images").write_text("\n".join(scenario.images) + "\n", encoding="utf-8")
        log = root / "docker.log"
        log.touch()
        env = {
            key: value
            for key, value in os.environ.items()
            # The deploy lock's reentrancy escape hatch must not leak in.
            if key not in ("DEPLOY_LOCK_HELD_BY", "DR_LOG")
        }
        env.update(
            PATH=f"{root / 'bin'}{os.pathsep}{env.get('PATH', '')}",
            TECHSARA_DEPLOY_ROOT=str(root),
            REHEARSAL_DOCKER_LOG=str(log),
            REHEARSAL_IMAGES=str(root / "images"),
            REHEARSAL_DB_PY=str(root / "db.py"),
            REHEARSAL_LIVE_SCHEMA=str(scenario.live_schema),
            DEPLOY_LOCK_WAIT="1",
        )
        args = [a.replace("{release_dir}", str(root / ".runtime" / "releases" / RELEASE)) for a in scenario.args]
        try:
            proc = subprocess.run(
                ["bash", str(script), *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
            rc, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc = 124
            out = f"timed out after 60 s: {exc}"
        calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        lock_created = (root / ".runtime" / "locks").exists()
    return Outcome(rc=rc, out=out, docker_calls=calls, lock_created=lock_created)


def _verb(call: str) -> str:
    words = call.split()
    if not words:
        return ""
    if words[0] in ("image", "rm") and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("--help prints the interface", ["--help"], 0, "--dry-run"),
    Scenario("an unknown option is refused", ["--definitely-not-a-flag"], 1, "unknown option"),
    Scenario("--to without a value is refused", ["--to"], 1, "--to needs"),
    Scenario("no target is refused", ["--dry-run"], 1, "nothing to roll back to"),
    Scenario("--list shows the recorded release", ["--list"], 0, RELEASE),
    Scenario(
        "a compatible dry run by release stamp changes nothing",
        ["--to", RELEASE, "--dry-run"],
        0,
        "dry run: nothing was tagged, stopped or recreated",
    ),
    Scenario(
        "a dry run across a schema boundary is refused",
        ["--to", "{release_dir}", "--dry-run"],
        1,
        "refusing the rollback (no --i-accept-schema-drift)",
        live_schema=TARGET_SCHEMA + 1,
    ),
    Scenario(
        "a frontend-only dry run is outside the schema check",
        ["--to", "{release_dir}", "--services", "frontend", "--dry-run"],
        0,
        "not applicable",
        live_schema=TARGET_SCHEMA + 1,
    ),
    Scenario(
        "a target whose image was pruned is refused",
        ["--to", RELEASE, "--dry-run"],
        1,
        "names images that are not on this machine",
        images=(FRONT_ID,),
    ),
)


def check(script: pathlib.Path, scenario: Scenario) -> list[str]:
    outcome = run(script, scenario)
    problems = []
    if scenario.expect_rc == 0 and outcome.rc != 0:
        problems.append(f"exited {outcome.rc}, expected success")
    if scenario.expect_rc != 0 and outcome.rc == 0:
        problems.append("exited 0, expected a refusal")
    if scenario.expect_text not in outcome.out:
        problems.append(f"output does not contain {scenario.expect_text!r}")
    mutating = [c for c in outcome.docker_calls if _verb(c) not in READ_ONLY_VERBS]
    if mutating:
        problems.append(f"reached mutating docker call(s): {mutating}")
    if outcome.lock_created:
        problems.append("created the deploy lock, so it went past the point a rehearsal must stop")
    if problems:
        tail = "\n".join("      | " + line for line in outcome.out.strip().splitlines()[-15:])
        problems.append("last output:\n" + tail)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", default=str(DEFAULT_SCRIPT))
    ap.add_argument("--summary", default=None, help="append a Markdown table here (GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)

    script = pathlib.Path(args.script)
    if not script.is_file():
        print(f"FATAL: {script} does not exist", file=sys.stderr)
        return 2
    if shutil.which("bash") is None or shutil.which("python3") is None:
        print("FATAL: the rehearsal needs bash and python3 on PATH", file=sys.stderr)
        return 2

    failed = 0
    rows = []
    for scenario in SCENARIOS:
        problems = check(script, scenario)
        rows.append((scenario, problems))
        if problems:
            failed += 1
            print(f"  FAIL {scenario.name}")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"  ok   {scenario.name}")

    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("### Rollback rehearsal (sandboxed, nothing changed)\n\n")
            fh.write("| scenario | command | result |\n| --- | --- | --- |\n")
            for scenario, problems in rows:
                command = "deploy-rollback.sh " + " ".join(scenario.args)
                fh.write(f"| {scenario.name} | `{command}` | {'FAIL' if problems else 'ok'} |\n")
            fh.write("\n")

    if failed:
        print(f"\nrollback rehearsal: {failed} of {len(SCENARIOS)} scenario(s) failed", file=sys.stderr)
        return 1
    print(f"rollback rehearsal: all {len(SCENARIOS)} scenarios behaved, and nothing was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
