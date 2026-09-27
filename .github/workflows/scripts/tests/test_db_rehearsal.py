"""The database rehearsal's two refusals: a guessed baseline, and a silent arm.

Everything expensive about the `db-rehearsal` job -- two image builds, a real
PostgreSQL, a booted orchestrator -- is proved by running it, and has been. What
is proved HERE is the part that decides, and the part that can rot without
anybody noticing:

  * the BASELINE resolver. "The previous release" is not a thing a hosted runner
    can look up: this repository has no release tags and records deploys in
    `.runtime/releases` on the box. So the resolver reads the event, and every
    way that can go wrong -- a first push, a shallow clone, a baseline that is
    HEAD itself, an event nobody taught it about -- has to be a refusal rather
    than a fallback to something that looks like a number.

  * the VERDICT. A green rehearsal has to mean "we ran this and it held". An arm
    whose step was deleted, renamed or never reached reports nothing, and an
    upgrade arm that skipped itself reports `pass`. Both must be refusals, for
    the same reason ci_gate.py refuses a job that is missing from `needs:`.

  * the rehearsal SCRIPT's refusals to rehearse against the wrong PostgreSQL.
    These run the real `scripts/deploy-db-rehearsal.sh` with a `docker` stub, and
    every one of them exits before the script creates anything -- the stub is
    never asked to impersonate a database, only to answer or refuse the two
    inspection calls that tell the script whether a deployed stack is present.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db_rehearsal  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[4]
REAL_SCRIPT = REPO / "scripts" / "deploy-db-rehearsal.sh"

HEAD = "a" * 40
OTHER = "b" * 40


def _exists_always(_sha: str) -> bool:
    return True


def _exists_never(_sha: str) -> bool:
    return False


class BaselineResolution(unittest.TestCase):
    """Which commit counts as the previous release, and when to refuse to say."""

    def test_a_declared_baseline_wins_over_everything(self):
        sha, how = db_rehearsal.resolve_baseline(
            repo=".",
            head=HEAD,
            event_name="pull_request",
            event={"pull_request": {"base": {"sha": OTHER}}},
            default_branch="main",
            declared="c" * 40,
            commit_exists=_exists_always,
        )
        self.assertEqual(sha, "c" * 40)
        self.assertIn("declared", how)

    def test_a_pull_request_rehearses_against_its_base(self):
        sha, how = db_rehearsal.resolve_baseline(
            repo=".",
            head=HEAD,
            event_name="pull_request",
            event={"pull_request": {"base": {"sha": OTHER}}},
            default_branch="main",
            commit_exists=_exists_always,
        )
        self.assertEqual(sha, OTHER)
        self.assertIn("base commit", how)

    def test_a_push_to_the_default_branch_rehearses_against_what_it_replaced(self):
        sha, how = db_rehearsal.resolve_baseline(
            repo=".",
            head=HEAD,
            event_name="push",
            event={"ref": "refs/heads/main", "before": OTHER},
            default_branch="main",
            commit_exists=_exists_always,
        )
        self.assertEqual(sha, OTHER)
        self.assertIn("before this push", how)

    def test_a_baseline_equal_to_head_is_refused_not_rehearsed(self):
        with self.assertRaises(db_rehearsal.BaselineError) as caught:
            db_rehearsal.resolve_baseline(
                repo=".",
                head=HEAD,
                event_name="pull_request",
                event={"pull_request": {"base": {"sha": HEAD}}},
                default_branch="main",
                commit_exists=_exists_always,
            )
        self.assertIn("proves nothing", str(caught.exception))

    def test_a_shallow_clone_is_named_as_the_cause_not_reported_as_a_bad_sha(self):
        """The default actions/checkout depth is 1, so this is the FIRST failure."""
        with self.assertRaises(db_rehearsal.BaselineError) as caught:
            db_rehearsal.resolve_baseline(
                repo=".",
                head=HEAD,
                event_name="pull_request",
                event={"pull_request": {"base": {"sha": OTHER}}},
                default_branch="main",
                commit_exists=_exists_never,
            )
        self.assertIn("fetch-depth: 0", str(caught.exception))

    def test_an_event_nobody_taught_it_about_is_a_refusal(self):
        with self.assertRaises(db_rehearsal.BaselineError) as caught:
            db_rehearsal.resolve_baseline(
                repo=".",
                head=HEAD,
                event_name="schedule",
                event={},
                default_branch="main",
                commit_exists=_exists_always,
            )
        self.assertIn("--previous", str(caught.exception))

    def test_a_missing_pull_request_base_is_a_refusal_and_not_an_empty_sha(self):
        with self.assertRaises(db_rehearsal.BaselineError):
            db_rehearsal.resolve_baseline(
                repo=".",
                head=HEAD,
                event_name="pull_request",
                event={"pull_request": {}},
                default_branch="main",
                commit_exists=_exists_always,
            )


class BaselineResolutionAgainstRealGit(unittest.TestCase):
    """The fallbacks that need a repository: merge-base, and HEAD's first parent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = cls._tmp.name
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "rehearsal",
            "GIT_AUTHOR_EMAIL": "rehearsal@invalid.example",
            "GIT_COMMITTER_NAME": "rehearsal",
            "GIT_COMMITTER_EMAIL": "rehearsal@invalid.example",
        }

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", cls.repo, *args],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        (pathlib.Path(cls.repo) / "one").write_text("1", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "root")
        cls.root = git("rev-parse", "HEAD")
        (pathlib.Path(cls.repo) / "two").write_text("2", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "second")
        cls.second = git("rev-parse", "HEAD")
        # A remote-tracking ref for origin/main, which is what the merge-base
        # rule actually looks at on a runner.
        git("update-ref", "refs/remotes/origin/main", cls.second)
        (pathlib.Path(cls.repo) / "three").write_text("3", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "third, on a branch")
        cls.third = git("rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_a_push_to_a_branch_rehearses_against_where_it_left_the_default_branch(self):
        sha, how = db_rehearsal.resolve_baseline(
            repo=self.repo,
            head=self.third,
            event_name="push",
            event={"ref": "refs/heads/some/branch"},
            default_branch="main",
        )
        self.assertEqual(sha, self.second)
        self.assertIn("merge-base", how)

    def test_a_branch_sitting_at_the_default_branch_tip_falls_back_to_the_first_parent(self):
        """Every branch on the day it is cut. A red build here would be bookkeeping."""
        sha, how = db_rehearsal.resolve_baseline(
            repo=self.repo,
            head=self.second,
            event_name="push",
            event={"ref": "refs/heads/some/branch"},
            default_branch="main",
        )
        self.assertEqual(sha, self.root)
        self.assertIn("first parent", how)

    def test_the_first_push_to_a_branch_falls_back_to_the_first_parent(self):
        sha, how = db_rehearsal.resolve_baseline(
            repo=self.repo,
            head=self.second,
            event_name="push",
            event={"ref": "refs/heads/main", "before": db_rehearsal.NULL_SHA},
            default_branch="main",
        )
        self.assertEqual(sha, self.root)
        self.assertIn("first parent", how)

    def test_a_root_commit_has_no_earlier_schema_and_is_a_refusal(self):
        with self.assertRaises(db_rehearsal.BaselineError) as caught:
            db_rehearsal.resolve_baseline(
                repo=self.repo,
                head=self.root,
                event_name="push",
                event={"ref": "refs/heads/main", "before": db_rehearsal.NULL_SHA},
                default_branch="main",
            )
        self.assertIn("root commit", str(caught.exception))


def _record(arm: str, outcome: str, **extra) -> dict:
    rec = {"arm": arm, "outcome": outcome}
    rec.update(extra)
    return rec


PASSING_UPGRADE = _record("fresh-and-upgrade", "pass", from_image="orchestrator:baseline")
PASSING_REVERSE = _record("reversibility", "pass")


class TheVerdictRefusesSilence(unittest.TestCase):
    def test_both_arms_passing_is_the_only_green(self):
        ok, lines = db_rehearsal.build_verdict([PASSING_UPGRADE, PASSING_REVERSE])
        self.assertTrue(ok)
        self.assertTrue(all("**FACT**" in line for line in lines), lines)

    def test_an_arm_that_reported_nothing_is_not_proved(self):
        ok, lines = db_rehearsal.build_verdict([PASSING_UPGRADE])
        self.assertFalse(ok)
        self.assertTrue(any("NOT PROVED" in line and "reversibility" in line for line in lines), lines)

    def test_an_arm_that_failed_is_a_fact_and_a_refusal(self):
        ok, lines = db_rehearsal.build_verdict(
            [PASSING_UPGRADE, _record("reversibility", "fail", detail="the old image would not boot")]
        )
        self.assertFalse(ok)
        self.assertTrue(any("**FACT**" in line and "FAILED" in line for line in lines), lines)

    def test_an_outcome_that_is_neither_pass_nor_fail_is_not_proved(self):
        for outcome in ("", "skipped", "cancelled", "probably fine"):
            with self.subTest(outcome=outcome):
                ok, lines = db_rehearsal.build_verdict(
                    [PASSING_UPGRADE, _record("reversibility", outcome)]
                )
                self.assertFalse(ok)
                self.assertTrue(any("NOT PROVED" in line for line in lines), lines)

    def test_an_upgrade_arm_that_upgraded_from_nothing_is_not_proved_even_when_it_passed(self):
        """The script skips its upgrade phase with no baseline image and exits 0."""
        ok, lines = db_rehearsal.build_verdict(
            [_record("fresh-and-upgrade", "pass", from_image=""), PASSING_REVERSE]
        )
        self.assertFalse(ok)
        self.assertTrue(
            any("NOT PROVED" in line and "UPGRADE phase was skipped" in line for line in lines),
            lines,
        )

    def test_a_missing_from_image_key_is_treated_the_same_as_an_empty_one(self):
        ok, _ = db_rehearsal.build_verdict([_record("fresh-and-upgrade", "pass"), PASSING_REVERSE])
        self.assertFalse(ok)

    def test_an_extra_arm_is_reported_but_does_not_decide(self):
        ok, lines = db_rehearsal.build_verdict(
            [PASSING_UPGRADE, PASSING_REVERSE, _record("restore", "pass")]
        )
        self.assertTrue(ok)
        self.assertTrue(any("restore" in line and "not required" in line for line in lines), lines)

    def test_a_record_that_is_not_an_object_cannot_satisfy_an_arm(self):
        ok, _ = db_rehearsal.build_verdict(["fresh-and-upgrade", PASSING_REVERSE])  # type: ignore[list-item]
        self.assertFalse(ok)


class TheWriteSmokeCannotCollideWithItself(unittest.TestCase):
    """`users.username` and the conversation id are UNIQUE.

    A fixed name made a second run against the same database raise
    UniqueViolation, which reads exactly like "the previous release cannot
    write" -- the one thing this smoke must not be able to say wrongly. Measured:
    re-running the reversibility arm against an already-smoked database failed
    on the fixed names and passes on these.
    """

    def test_two_runs_produce_different_rows(self):
        first = db_rehearsal.write_smoke_source("111-222")
        second = db_rehearsal.write_smoke_source("333-444")
        self.assertIn("'111-222'", first)
        self.assertIn("'333-444'", second)
        self.assertNotEqual(first, second)

    def test_the_source_is_valid_python_and_still_asserts_the_read_back(self):
        source = db_rehearsal.write_smoke_source("555-666")
        compile(source, "<write_smoke>", "exec")
        self.assertIn("ROLLBACK-WRITE-OK", source)
        self.assertIn("list_messages", source)
        self.assertIn("assert rows", source)


#: A `docker` that answers ONLY the two inspection calls the script uses to
#: decide whether a deployed stack is present, and refuses everything else with
#: an exit code the script cannot mistake for data. It is deliberately not a
#: database: every scenario below exits before the script creates anything, and
#: a stub that pretended to be PostgreSQL would be testing the stub.
_DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$REHEARSAL_DOCKER_LOG"
case "$1 $2" in
  "exec $2")
    [ "${REHEARSAL_STACK:-0}" = 1 ] || exit 1
    printf 'postgres (PostgreSQL) %s.4\n' "${REHEARSAL_STACK_MAJOR:-18}"; exit 0 ;;
esac
case "$1" in
  exec)
    [ "${REHEARSAL_STACK:-0}" = 1 ] || exit 1
    printf 'postgres (PostgreSQL) %s.4\n' "${REHEARSAL_STACK_MAJOR:-18}"; exit 0 ;;
  inspect)
    [ "${REHEARSAL_STACK:-0}" = 1 ] || exit 1
    printf '%s\n' "${REHEARSAL_STACK_IMAGE_ID:-sha256:deployed}"; exit 0 ;;
  image)
    [ "$2" = inspect ] || exit 97
    printf '%s\n' "${REHEARSAL_DECLARED_IMAGE_ID:-sha256:declared}"; exit 0 ;;
esac
exit 97
"""


class TheScriptRefusesTheWrongPostgres(unittest.TestCase):
    """`scripts/deploy-db-rehearsal.sh`'s identity rules, run for real.

    A rehearsal on the wrong PostgreSQL major proves nothing, and a digest says
    nothing about a version -- which is why the deployed major is read from the
    running server, and why a hosted runner has to DECLARE it. Each case here
    exits before a database exists.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        (root / "scripts" / "lib").mkdir(parents=True)
        shutil.copy(REAL_SCRIPT, root / "scripts" / "deploy-db-rehearsal.sh")
        shutil.copy(
            REAL_SCRIPT.parent / "lib" / "deploy-common.sh",
            root / "scripts" / "lib" / "deploy-common.sh",
        )
        self.script = root / "scripts" / "deploy-db-rehearsal.sh"
        bindir = root / "bin"
        bindir.mkdir()
        stub = bindir / "docker"
        stub.write_text(_DOCKER_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.log = root / "docker.log"
        self.bindir = bindir

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.bindir}:{os.environ.get('PATH', '')}",
                "REHEARSAL_DOCKER_LOG": str(self.log),
                "TECHSARA_DEPLOY_ROOT": str(self.script.parent.parent),
                # Never inherited from the developer's shell: it would silently
                # supply a server and let a scenario get further than intended.
                "TEST_DATABASE_URL": "",
                **env,
            },
            stdin=subprocess.DEVNULL,
        )

    def test_the_script_still_parses(self):
        proc = subprocess.run(["bash", "-n", str(REAL_SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_stack_and_no_declaration_refuses_rather_than_guessing_a_major(self):
        proc = self._run("--image", "orch:new", "--server", "postgresql://u:p@127.0.0.1:1/postgres")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("unknown major", proc.stderr)

    def test_half_a_declaration_is_refused(self):
        for args in (("--expect-major", "18"), ("--pg-image", "postgres@sha256:abc")):
            with self.subTest(args=args):
                proc = self._run(
                    "--image", "orch:new", "--server", "postgresql://u:p@127.0.0.1:1/postgres", *args
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("TOGETHER", proc.stderr)

    def test_a_major_that_is_not_a_whole_number_is_refused_before_anything_runs(self):
        for bad in ("18.4", "eighteen", "18-alpine"):
            with self.subTest(major=bad):
                proc = self._run(
                    "--image", "orch:new", "--pg-image", "postgres@sha256:abc", "--expect-major", bad
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("whole PostgreSQL major", proc.stderr)

    def test_on_a_box_with_a_stack_a_contradicting_major_is_refused(self):
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "17",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_MAJOR="18",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the stack decides", proc.stderr)
        self.assertIn("reports major 18", proc.stderr)

    def test_on_a_box_with_a_stack_a_declared_image_that_is_a_different_image_is_refused(self):
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_MAJOR="18",
            REHEARSAL_STACK_IMAGE_ID="sha256:whatisrunning",
            REHEARSAL_DECLARED_IMAGE_ID="sha256:whatwasasked",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the stack decides", proc.stderr)

    def test_an_unreachable_server_says_so_instead_of_exiting_silently(self):
        """Before this was fixed the script exited 2 with no diagnostic.

        `set -e` plus `pipefail` end an assignment whose command substitution
        fails AT that assignment, so the `dr_die` that names the host never ran -
        and "the server is not there" is the failure a hosted runner is most
        likely to produce.
        """
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("cannot reach the rehearsal server", proc.stderr)
        self.assertIn("127.0.0.1:1", proc.stderr)

    def test_a_declaration_that_agrees_with_the_stack_is_not_refused(self):
        """The refusals above must not be "any declaration is refused"."""
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_MAJOR="18",
            REHEARSAL_STACK_IMAGE_ID="sha256:same",
            REHEARSAL_DECLARED_IMAGE_ID="sha256:same",
        )
        combined = proc.stdout + proc.stderr
        self.assertNotIn("the stack decides", combined)
        # It gets as far as trying to reach the server, which the stub is not.
        self.assertIn("cannot reach the rehearsal server", proc.stderr)

    def test_the_unknown_option_guard_still_holds(self):
        proc = self._run("--not-a-flag")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown option", proc.stderr)


class TheJobIsWiredIntoThePipelineButNotYetIntoTheGate(unittest.TestCase):
    """Two facts about pipeline.yml that a later commit deliberately changes.

    The promotion of `db-rehearsal` to a required check is its own commit, and
    ci_gate.py fails a dependency that is in `needs:` without being in
    `--require`. So the wiring has to be all-or-nothing, and these pin which
    half this commit is.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (REPO / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")

    def test_the_job_exists(self):
        self.assertIn("\n  db-rehearsal:\n", self.text)

    def test_it_uses_the_same_postgres_digest_as_the_other_two_jobs(self):
        digests = set(
            line.split("postgres@", 1)[1].strip().strip("\"'")
            for line in self.text.splitlines()
            if "postgres@sha256:" in line
        )
        self.assertEqual(
            len(digests), 1, f"the postgres pin has drifted between jobs: {sorted(digests)}"
        )
        # Three jobs declare it plus the one env: entry this job reads it from.
        self.assertGreaterEqual(self.text.count("postgres@sha256:"), 4)

    def test_it_checks_out_full_history_because_a_baseline_needs_it(self):
        job = self.text.split("\n  db-rehearsal:\n", 1)[1].split("\n  security:", 1)[0]
        self.assertIn("fetch-depth: 0", job)

    def test_it_is_not_a_required_check_in_this_commit(self):
        gate = self.text.split("\n  ci-ok:\n", 1)[1].split("\n  deploy:", 1)[0]
        self.assertNotIn("db-rehearsal", gate)


if __name__ == "__main__":
    unittest.main()
