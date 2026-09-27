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
    not asked to impersonate a database, only to answer or refuse the two
    inspection calls that tell the script whether a deployed stack is present.

  * the SILENT EXIT, which this script has two places to fall into and which no
    amount of reading finds: under `set -euo pipefail` an assignment whose
    command substitution fails takes that status and ends the script AT the
    assignment, before the `dr_die` or `bad` on the next line can say anything.
    One case is an unreachable server; the other is a baseline image whose
    migration table cannot be read. The second one needs the script to reach its
    UPGRADE phase, so the stub grows an opt-in arm that fakes the calls the
    fresh-install phase makes. That arm proves control flow, never SQL.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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


#: A complete upgrade record: BOTH the baseline image and the schema version
#: that was read out of it. `scripts/deploy-db-rehearsal.sh` always writes both
#: keys, and an empty `from_version` beside a named image is its way of saying
#: the image's migration table could not be read -- so a fixture that stood for
#: "this arm really ran" must carry the version too.
PASSING_UPGRADE = _record(
    "fresh-and-upgrade", "pass", from_image="orchestrator:baseline", from_version="40"
)
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

    def test_a_baseline_whose_migration_table_could_not_be_read_is_not_proved_either(self):
        """The second way for the upgrade phase not to have run.

        The script names the baseline image it was GIVEN before it tries to read
        the image's migration table, so a record can carry `from_image` and still
        describe an arm that never ran; `from_version` is what says it ran. Today
        that state also carries failed>=1, but this lock must not depend on the
        arm's own count being right -- that is the point of a second lock.
        """
        for version in ("", None):
            with self.subTest(from_version=version):
                rec = _record(
                    "fresh-and-upgrade",
                    "pass",
                    from_image="postgres:18-alpine",
                    detail="the upgrade arm did NOT run: the migration table could not be read",
                )
                if version is not None:
                    rec["from_version"] = version
                ok, lines = db_rehearsal.build_verdict([rec, PASSING_REVERSE])
                self.assertFalse(ok, lines)
                line = next(one for one in lines if "fresh-and-upgrade" in one)
                self.assertIn("NOT PROVED", line)
                self.assertIn("migration table could not be read", line)
                self.assertNotIn("**FACT**", line)

    def test_an_extra_arm_is_reported_but_does_not_decide(self):
        ok, lines = db_rehearsal.build_verdict(
            [PASSING_UPGRADE, PASSING_REVERSE, _record("restore", "pass")]
        )
        self.assertTrue(ok)
        self.assertTrue(any("restore" in line and "not required" in line for line in lines), lines)

    def test_a_record_that_is_not_an_object_cannot_satisfy_an_arm(self):
        ok, _ = db_rehearsal.build_verdict(["fresh-and-upgrade", PASSING_REVERSE])  # type: ignore[list-item]
        self.assertFalse(ok)


class TheGreenHeaderCannotCarryANotProvedBullet(unittest.TestCase):
    """`cmd_verdict`'s summary and its exit code have to agree.

    With `--pg-major` empty the body printed
    `- **NOT PROVED** the rehearsal server's PostgreSQL major was not recorded`
    under the header `### Database rehearsal: SAFE AND REVERSIBLE`, and returned
    0. The major is the fact the rehearsal turns on -- the script reads it off
    the running server and refuses a declaration that disagrees with it -- so a
    run that did not record it has not proved what the header claims.

    The pipeline always passes it, so this was not reachable there. It is closed
    anyway: the branch's own thesis is that a green summary cannot be misread,
    and an unreachable hole is one step edit away from being reachable.
    """

    def _verdict(self, *, pg_major: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for rec in (PASSING_UPGRADE, PASSING_REVERSE):
                path = pathlib.Path(tmp) / f"{rec['arm']}.json"
                path.write_text(json.dumps(rec), encoding="utf-8")
                paths.append(str(path))
            summary = pathlib.Path(tmp) / "summary.md"
            env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = db_rehearsal.main(
                    ["verdict", "--results", *paths, "--head", HEAD, "--previous", OTHER,
                     "--pg-major", pg_major]
                )
            return rc, summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_an_unrecorded_postgres_major_is_a_refusal_and_not_a_footnote(self):
        rc, body = self._verdict(pg_major="")
        self.assertEqual(rc, 1, body)
        self.assertIn("**NOT PROVED** the rehearsal server's PostgreSQL major", body)
        self.assertIn("Database rehearsal: REFUSED", body)
        self.assertNotIn("SAFE AND REVERSIBLE", body)

    def test_a_recorded_major_with_two_passing_arms_is_still_green(self):
        """The refusal above must not have turned every verdict red."""
        rc, body = self._verdict(pg_major="18")
        self.assertEqual(rc, 0, body)
        self.assertIn("Database rehearsal: SAFE AND REVERSIBLE", body)
        self.assertIn("PostgreSQL major asserted on the rehearsal server: `18`", body)
        self.assertNotIn("**NOT PROVED** the rehearsal server", body)

    def test_the_by_construction_line_is_still_there_under_a_green_header(self):
        """The one NOT PROVED that IS allowed under a green header: it is out of scope."""
        _rc, body = self._verdict(pg_major="18")
        self.assertIn("**NOT PROVED here, by construction:** that the deploy itself is safe", body)


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


class TheReversibilityArmAssertsWhatItClaims(unittest.TestCase):
    """The arm the job exists for, driven without docker or an image.

    This arm had no unit coverage at all, and it is the one that carries the
    job's whole reason for existing: there are no down migrations, so an image
    rollback leaves the PREVIOUS release's code in front of THIS release's
    schema, and nobody had ever run that combination. Replacing
    `if seen == args.expect_schema:` with `if True:` -- accept any schema_version
    /health cares to report -- left every test green, and so did replacing the
    write smoke's check with `if True:`.

    The arm was verified against the real engine (a real PostgreSQL 18.6, the
    real sf-local-ai-orchestrator:sha-c3ca253d16e5 booted over a V41 database:
    6 passed, 0 failed, with honest negatives on a wrong --expect-schema and on
    an image that will not start). That proof lived in a report and nowhere in
    the repository, so it had to be re-run by hand to be believed again.

    `_run` and `_http` are the arm's only two windows onto the world, so faking
    those two drives the REAL assertion code -- the lines under test are the ones
    that ship, not a copy of them.
    """

    #: A /health body from a previous release that is behaving exactly as a
    #: rollback candidate should: it can read the newer schema and reports the
    #: version it found without touching it.
    @staticmethod
    def _health(schema: int, *, app_db_status: str = "ok") -> str:
        return json.dumps(
            {"checks": {"app_db": {"status": app_db_status, "schema_version": schema}}}
        )

    def _arm(
        self,
        *,
        expect_schema: int = 41,
        reported_schema: int = 41,
        app_db_status: str = "ok",
        health_json: str | None = None,
        start_rc: int = 0,
        metrics_status: int = 200,
        login_status: int = 401,
        write_rc: int = 0,
        write_stdout: str = "ROLLBACK-WRITE-OK 7 1",
    ):
        """Run the real `cmd_reversibility` against a fabricated world."""
        out = pathlib.Path(self.tmp) / "reverse.json"
        args = argparse.Namespace(
            old_image="orch:previous",
            dsn="postgresql://u:p@127.0.0.1:1/reverse",
            expect_schema=expect_schema,
            port=18731,
            dead_port=65535,
            container="db-rehearsal-previous-release",
            boot_timeout=5,
            json=str(out),
        )

        def fake_run(argv, **_kw):
            joined = " ".join(argv)
            if "rm" in argv and "-f" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if "logs" in argv:
                return subprocess.CompletedProcess(argv, 0, "container log line", "")
            if "-d" in argv:  # docker run -d --name ... : booting the old image
                return subprocess.CompletedProcess(
                    argv, start_rc, "", "" if start_rc == 0 else "no such image"
                )
            if "--entrypoint" in argv and "python" in argv:  # the write smoke
                return subprocess.CompletedProcess(
                    argv, write_rc, write_stdout if write_rc == 0 else "", "" if write_rc == 0 else "IntegrityError"
                )
            raise AssertionError("the arm ran a command this test did not expect: " + joined)

        def fake_http(url, **_kw):
            if url.endswith("/health"):
                body = self._health(reported_schema, app_db_status=app_db_status)
                return 200, (health_json if health_json is not None else body)
            if url.endswith("/metrics"):
                return metrics_status, ""
            if url.endswith("/auth/login"):
                return login_status, "{}"
            raise AssertionError("the arm fetched a URL this test did not expect: " + url)

        with mock.patch.object(db_rehearsal, "_run", fake_run), \
             mock.patch.object(db_rehearsal, "_http", fake_http), \
             mock.patch.object(db_rehearsal.time, "sleep", lambda _s: None), \
             contextlib.redirect_stdout(io.StringIO()) as printed:
            rc = db_rehearsal.cmd_reversibility(args)
        record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        return rc, printed.getvalue(), record

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def _texts(self, record: dict, *, ok: bool) -> list[str]:
        return [c["text"] for c in record["checks"] if c["ok"] is ok]

    # ------------------------------------------------------------ the happy arm

    def test_a_clean_rollback_passes_every_check_and_records_them(self):
        rc, printed, record = self._arm()
        self.assertEqual(rc, 0, printed)
        self.assertEqual(record["outcome"], "pass")
        self.assertEqual(record["expect_schema"], 41)
        self.assertEqual(record["old_image"], "orch:previous")
        self.assertEqual(self._texts(record, ok=False), [])
        # Six, and the count is asserted so a check that silently stops running
        # cannot hide behind the other five.
        self.assertEqual(len(record["checks"]), 6, printed)
        self.assertIn("reversibility: 6 passed, 0 failed", printed)

    # ------------------------------- the assertion that is the arm's whole point

    def test_a_previous_release_that_reports_the_wrong_schema_version_fails(self):
        """Replacing `if seen == args.expect_schema:` with `if True:` left the suite green.

        Either the database was not staged at this commit's version, or the
        previous release CHANGED it - and a rollback that migrates is not a
        rollback. Both are the finding; neither is a footnote.
        """
        rc, printed, record = self._arm(expect_schema=41, reported_schema=40)
        self.assertEqual(rc, 1, printed)
        self.assertEqual(record["outcome"], "fail")
        failed = " ".join(self._texts(record, ok=False))
        self.assertIn("schema_version=40", failed)
        self.assertIn("expected 41", failed)
        self.assertIn("a rollback that migrates is not a rollback", failed)

    def test_a_missing_schema_version_is_a_failure_and_not_a_lucky_match(self):
        """`None == 41` must fail rather than being smoothed over."""
        rc, _printed, record = self._arm(health_json=json.dumps({"checks": {"app_db": {"status": "ok"}}}))
        self.assertEqual(rc, 1)
        self.assertIn("schema_version=None", " ".join(self._texts(record, ok=False)))

    def test_a_schema_version_that_is_a_string_does_not_satisfy_the_check(self):
        """A JSON "41" is not the integer this arm compares against.

        The comparison is `seen == args.expect_schema` with expect_schema typed
        `int` by argparse, so a stringified version has to be a finding rather
        than a pass -- otherwise the one number this arm exists to check could
        arrive in a shape nobody notices.
        """
        rc, _printed, record = self._arm(health_json=self._health(41).replace('"schema_version": 41', '"schema_version": "41"'))
        self.assertEqual(rc, 1)
        self.assertIn("schema_version='41'", " ".join(self._texts(record, ok=False)))

    # ------------------------------------------------- reading is half a rollback

    def test_a_previous_release_that_cannot_write_against_the_newer_schema_fails(self):
        """Replacing the write smoke's check with `if True:` left the suite green.

        This is the half that catches a column this commit made NOT NULL without
        a default: the old code can SELECT perfectly well and still not INSERT.
        """
        rc, printed, record = self._arm(write_rc=1)
        self.assertEqual(rc, 1, printed)
        failed = " ".join(self._texts(record, ok=False))
        self.assertIn("could not write against the newer schema", failed)
        self.assertIn("IntegrityError", failed)

    def test_a_write_that_exits_zero_without_the_marker_is_still_a_failure(self):
        """Exit 0 is not the proof; the round-tripped marker is.

        A `python -c` whose assertions were stripped would exit 0 and print
        nothing, and that must not read as a successful write.
        """
        rc, _printed, record = self._arm(write_stdout="")
        self.assertEqual(rc, 1)
        self.assertIn("could not write against the newer schema", " ".join(self._texts(record, ok=False)))

    # ---------------------------------------------------------- honest negatives

    def test_an_image_that_will_not_start_is_a_recorded_failure_not_a_crash(self):
        rc, printed, record = self._arm(start_rc=1)
        self.assertEqual(rc, 1, printed)
        self.assertEqual(record["outcome"], "fail")
        self.assertIn("would not start", " ".join(self._texts(record, ok=False)))
        # It still writes the record, which is what stops the verdict from
        # calling this arm silent.
        self.assertEqual(record["arm"], "reversibility")

    def test_an_unhealthy_app_db_is_a_failure(self):
        rc, _printed, record = self._arm(app_db_status="error")
        self.assertEqual(rc, 1)
        self.assertIn("not status=ok", " ".join(self._texts(record, ok=False)))

    def test_a_login_that_is_not_401_is_a_failure(self):
        """401 is the evidence: an unknown account means the SELECT ran.

        500 is what a `users` table the old code cannot read looks like, and 200
        would mean a fabricated account exists.
        """
        for status in (200, 500):
            with self.subTest(status=status):
                rc, _printed, record = self._arm(login_status=status)
                self.assertEqual(rc, 1)
                self.assertIn(f"returned {status}", " ".join(self._texts(record, ok=False)))

    def test_metrics_is_not_reported_as_a_database_claim(self):
        """/metrics swallows a database error and still serves 200.

        So the PASS line has to say what it is not, or a reader counts it as
        evidence the database is fine.
        """
        _rc, _printed, record = self._arm()
        line = next(t for t in self._texts(record, ok=True) if "/metrics" in t)
        self.assertIn("not a database claim", line)

    def test_every_failure_is_also_a_workflow_annotation(self):
        """A finding that scrolls past 40 lines of container log is a finding nobody reads."""
        _rc, printed, _record = self._arm(reported_schema=40)
        self.assertIn("::error title=db-rehearsal reversibility::", printed)


#: A `docker` that answers the two inspection calls the script uses to decide
#: whether a deployed stack is present, and refuses everything else with an exit
#: code the script cannot mistake for data.
#:
#: `REHEARSAL_FAKE_DB=1` adds a second arm that also answers `create`/`cp`/`rm`
#: and the handful of `docker run` calls the fresh-install phase makes, which is
#: the only way to walk the script as far as the UPGRADE phase. It is OFF by
#: default, so every refusal that must stop at the unreachable server still
#: does, and it is deliberately not a database: it returns fixed strings, so a
#: test built on it can only ever assert control flow.
_DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$REHEARSAL_DOCKER_LOG"
case "$1" in
  exec)
    [ "${REHEARSAL_STACK:-0}" = 1 ] || exit 1
    # The two reads below fail INDEPENDENTLY on a real box - the major needs the
    # container RUNNING, the image id only needs it to exist - so the stub has to
    # be able to fail them one at a time. REHEARSAL_STACK on its own is a whole,
    # healthy stack; these two are the half-readable states.
    [ "${REHEARSAL_STACK_EXEC_FAIL:-0}" = 1 ] && exit 1
    printf 'postgres (PostgreSQL) %s.4\n' "${REHEARSAL_STACK_MAJOR:-18}"; exit 0 ;;
  inspect)
    [ "${REHEARSAL_STACK:-0}" = 1 ] || exit 1
    [ "${REHEARSAL_STACK_INSPECT_FAIL:-0}" = 1 ] && exit 1
    printf '%s\n' "${REHEARSAL_STACK_IMAGE_ID:-sha256:deployed}"; exit 0 ;;
  image)
    [ "$2" = inspect ] || exit 97
    printf '%s\n' "${REHEARSAL_DECLARED_IMAGE_ID:-sha256:declared}"; exit 0 ;;
esac

[ "${REHEARSAL_FAKE_DB:-0}" = 1 ] || exit 97

# Fixed answers, not a database. Enough of them that the script's FRESH phase
# passes its four assertions and control reaches the UPGRADE phase.
fake_latest="${REHEARSAL_FAKE_LATEST:-41}"
case "$1" in
  create)
    # `docker create IMAGE true`. The id carries the image so that `cp` can
    # decide whether THAT image has a readable /app/app/db.py.
    printf 'cid-%s\n' "$2"; exit 0 ;;
  rm) exit 0 ;;
  cp)
    # `docker cp CID:/app/app/db.py FILE`
    img="${2%%:/*}"; img="${img#cid-}"
    [ "$img" = "${REHEARSAL_UNREADABLE_IMAGE:-}" ] && exit 1
    # Every image reports the same schema version unless a test says otherwise,
    # which makes the baseline and the image under test identical -- so the arm
    # record's "the schema did not move in this commit" is the DEFAULT state here
    # and "upgraded from V_n to V_m" was unreachable until this.
    v="$fake_latest"
    if [ -n "${REHEARSAL_OLD_VERSION:-}" ] && [ "$img" = "${REHEARSAL_OLD_IMAGE:-orch:old}" ]; then
      v="$REHEARSAL_OLD_VERSION"
    fi
    printf '_MIGRATIONS = (\n    (%s, _MIGRATION_V%s),\n)\n' "$v" "$v" > "$3"
    exit 0 ;;
  run)
    entry=""; want=0
    for a in "$@"; do
      [ "$want" = 1 ] && { entry="$a"; want=0; continue; }
      [ "$a" = --entrypoint ] && want=1
    done
    case "$entry" in
      python|pg_restore) exit 0 ;;
      pg_dump) printf 'not-a-real-archive\n'; exit 0 ;;
      psql)
        sql=""; for a in "$@"; do sql="$a"; done
        case "$sql" in
          *"SHOW server_version"*) printf '%s.4\n' "${REHEARSAL_FAKE_MAJOR:-18}" ;;
          *"MAX(version)"*)        printf '%s\n' "$fake_latest" ;;
          *"md5("*)                printf 'fake-digest\n' ;;
          *"count(*)"*)            printf '0\n' ;;
          *) : ;;  # CREATE/DROP DATABASE and the pg_database probe say nothing
        esac
        exit 0 ;;
    esac
    exit 97 ;;
esac
exit 97
"""


class RunsTheRealScript:
    """Puts the real `scripts/deploy-db-rehearsal.sh` behind a stubbed `docker`.

    The script is copied out of the repository, not reimplemented: a test that
    read a copy of the logic would keep passing after the real one rotted.
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

    #: Every variable that decides what a scenario IS, and therefore none of
    #: which may arrive from the shell that ran `python3 -m unittest`.
    #:
    #: `REHEARSAL_*` is what the `docker` stub reads to choose what to pretend,
    #: and `DR_REHEARSAL_*` is the script's own env interface for `--pg-image`,
    #: `--expect-major` and `--require-upgrade`. Either one, set ambiently, silently
    #: rewrites the scenario a test thought it was running. Measured today on this
    #: branch before this stripping existed: `REHEARSAL_FAKE_DB=1 python3 -m unittest
    #: tests.test_db_rehearsal` -> FAILED (failures=2), and one of the two was
    #: `test_an_unreachable_server_says_so_instead_of_exiting_silently` -- the guard
    #: for the silent exit this whole file exists to pin -- because the stub's fake
    #: database walked the script straight past the unreachable 127.0.0.1:1.
    #:
    #: A PREFIX and not a list of names on purpose: a list would have to be kept in
    #: step with the stub, which is what let three new variables in unnoticed. A
    #: test that wants one passes it through `**env`, which lands after this.
    _SCENARIO_ENV_PREFIXES = ("REHEARSAL_", "DR_REHEARSAL_")

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(self._SCENARIO_ENV_PREFIXES)
        }
        return subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={
                **inherited,
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


class TheHarnessRefusesToInheritAScenario(RunsTheRealScript, unittest.TestCase):
    """The variables that choose a scenario must come from the test, not the shell.

    Both scenarios below are ones a real test in this file depends on, and each
    ambient variable here was measured today to change that scenario when it was
    inherited. The first one is the important one: an inherited `REHEARSAL_FAKE_DB`
    gives the script a fake database, so the test that proves an unreachable server
    is reported instead of exiting silently reported a full green run instead.
    """

    UNREACHABLE = (
        "--image", "orch:new",
        "--server", "postgresql://u:p@127.0.0.1:1/postgres",
        "--pg-image", "postgres@sha256:abc",
        "--expect-major", "18",
    )

    #: Every row below is paired with a scenario that MEASURABLY changes when that
    #: variable is inherited: checked by reverting the stripping and watching each
    #: subtest go red. `DR_REHEARSAL_*` gets its own scenario because `UNREACHABLE`
    #: passes `--pg-image` and `--expect-major` on the command line, which win over
    #: the env -- so pairing those two with THIS scenario would pass either way and
    #: prove nothing.

    def test_an_ambient_variable_cannot_walk_a_scenario_past_the_unreachable_server(self):
        for name, value in (("REHEARSAL_FAKE_DB", "1"), ("REHEARSAL_STACK", "1")):
            with self.subTest(var=name), mock.patch.dict(os.environ, {name: value}):
                proc = self._run(*self.UNREACHABLE)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("cannot reach the rehearsal server", proc.stderr)

    def test_an_ambient_variable_cannot_supply_a_declaration_the_test_withheld(self):
        """`DR_REHEARSAL_*` is the script's env interface for the CLI declaration.

        This scenario passes NEITHER `--pg-image` nor `--expect-major`, so the
        refusal it pins is "no stack and no declaration, so the major is unknown".
        Half a declaration arriving from the shell turns that into the "TOGETHER"
        refusal, which is a different refusal about a different mistake.
        """
        for name, value in (
            ("DR_REHEARSAL_EXPECT_MAJOR", "17"),
            ("DR_REHEARSAL_PG_IMAGE", "postgres@sha256:zzz"),
        ):
            with self.subTest(var=name), mock.patch.dict(os.environ, {name: value}):
                proc = self._run(
                    "--image", "orch:new", "--server", "postgresql://u:p@127.0.0.1:1/postgres"
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("unknown major", proc.stderr)
                self.assertNotIn("TOGETHER", proc.stderr)

    def test_an_ambient_variable_cannot_rewrite_the_stubbed_upgrade_scenario(self):
        for name, value in (
            ("REHEARSAL_FAKE_MAJOR", "17"),
            ("REHEARSAL_FAKE_LATEST", "99"),
            ("REHEARSAL_UNREADABLE_IMAGE", "orch:old"),
            ("REHEARSAL_STACK", "1"),
        ):
            with self.subTest(var=name), mock.patch.dict(os.environ, {name: value}):
                proc = self._run(
                    *self.UNREACHABLE, "--from-image", "orch:old", REHEARSAL_FAKE_DB="1"
                )
                combined = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 0, combined)
                self.assertIn("upgraded from : orch:old (V41)", combined)

    def test_a_variable_the_test_passes_still_reaches_the_script(self):
        """The stripping must not decay into "the stub can never be configured"."""
        proc = self._run(*self.UNREACHABLE, "--from-image", "orch:old", REHEARSAL_FAKE_DB="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("upgrading from orch:old (V41)", proc.stdout)


class TheScriptRefusesTheWrongPostgres(RunsTheRealScript, unittest.TestCase):
    """`scripts/deploy-db-rehearsal.sh`'s identity rules, run for real.

    A rehearsal on the wrong PostgreSQL major proves nothing, and a digest says
    nothing about a version -- which is why the deployed major is read from the
    running server, and why a hosted runner has to DECLARE it. Each case here
    exits before a database exists.
    """

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

    # ------------------------------------------------- half a stack is not no stack
    #
    # "On a machine with a stack, the stack decides" is the script header's rule
    # and the two refusals above are the only things that were pinning it. They
    # both go through the branch that needs BOTH reads to have worked. Neither
    # noticed that the reads fail independently, so a box WITH a stack that could
    # only be read halfway fell through to the declared-identity path and the
    # declaration won -- the exact override the rule forbids.

    def test_a_stack_whose_version_cannot_be_read_is_not_a_bare_runner(self):
        """The container is here; its image id proves it. A declaration must not win.

        `docker exec` needs the container RUNNING and `docker inspect` does not, so a
        STOPPED postgres container is this state on a real box. Measured on the branch
        before this guard, with a stub whose `exec` fails and whose `inspect` succeeds:

            no deployed stack here; rehearsing against the DECLARED PostgreSQL:
            major 11, image postgres@sha256:something-nobody-checked
        """
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:not-the-box",
            "--expect-major", "11",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_EXEC_FAIL="1",
            REHEARSAL_STACK_IMAGE_ID="sha256:deployedpg",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertNotIn("no deployed stack here", combined)
        self.assertNotIn("DECLARED PostgreSQL", combined)
        self.assertIn("Refusing to treat a box with a stack as a bare runner", proc.stderr)
        # The half that FAILED is named, and the half that succeeded is the
        # evidence offered for saying a stack is present.
        self.assertIn("version could not be read", proc.stderr)
        self.assertIn("sha256:deployedpg", proc.stderr)

    def test_the_same_hole_is_not_reachable_through_the_environment(self):
        """`--pg-image`/`--expect-major` also arrive as DR_REHEARSAL_*."""
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_EXEC_FAIL="1",
            DR_REHEARSAL_PG_IMAGE="postgres:15",
            DR_REHEARSAL_EXPECT_MAJOR="11",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertNotIn("DECLARED PostgreSQL", combined)
        self.assertIn("Refusing to treat a box with a stack as a bare runner", proc.stderr)

    def test_an_unreadable_image_id_is_blamed_on_the_image_id_and_not_the_version(self):
        """The diagnosis has to name the read that failed.

        origin/dev said `cannot read the image id of the production postgres
        container`. The rewrite deleted that line, so this state fell through to the
        no-declaration refusal and reported `cannot read the PostgreSQL version from
        sf-local-ai-postgres-1` -- measured on the branch, with a stub whose `exec`
        succeeds and whose `inspect` fails. The version had read perfectly well. On
        the box this is the most confusing thing this script can say.

        Not probed with a third `docker inspect`: that is the call that just failed,
        so it would answer "no container here" in exactly this case. The major that
        came back is the evidence instead -- something answered `postgres --version`
        INSIDE that container.
        """
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_MAJOR="18",
            REHEARSAL_STACK_INSPECT_FAIL="1",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("cannot read the image id of the production postgres container", proc.stderr)
        self.assertIn("major 18", proc.stderr)
        self.assertNotIn("cannot read the PostgreSQL version", proc.stderr)

    def test_an_unreadable_image_id_is_refused_even_when_a_declaration_agrees(self):
        """A declaration cannot fill in the half of the stack that would not read."""
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            REHEARSAL_STACK="1",
            REHEARSAL_STACK_MAJOR="18",
            REHEARSAL_STACK_INSPECT_FAIL="1",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertNotIn("DECLARED PostgreSQL", combined)
        self.assertIn("cannot read the image id", proc.stderr)

    def test_a_runner_with_no_stack_at_all_still_rehearses_against_its_declaration(self):
        """The guard above must not cost the hosted runner its only way to run.

        Both reads failing is the genuine no-stack case and has to fall straight
        through to the declared identity -- the pipeline's PostgreSQL is a
        `services:` container under a name GitHub chooses, never $PG_CONTAINER, so
        the two refusals cannot fire there at all.
        """
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
        )
        combined = proc.stdout + proc.stderr
        self.assertIn("no deployed stack here", proc.stdout)
        self.assertIn("major 18, image postgres@sha256:abc", proc.stdout)
        self.assertNotIn("as a bare runner", combined)
        # And it goes on to the server, which is the next real step.
        self.assertIn("cannot reach the rehearsal server", proc.stderr)

    def test_the_unknown_option_guard_still_holds(self):
        proc = self._run("--not-a-flag")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown option", proc.stderr)


class TheUpgradeArmRefusesABaselineItCannotRead(RunsTheRealScript, unittest.TestCase):
    """A baseline image whose migration table cannot be read is a FAILED arm.

    This is the second silent-exit trap in the script, and the likelier of the
    two to fire on a real Actions run: the baseline image is built from an
    ARBITRARY historical commit, so "its /app/app/db.py cannot be read" is an
    ordinary outcome -- a Dockerfile that has since been renamed, a layout that
    moved, an image that is not an orchestrator at all.

    Measured on this branch before the guard existed, with the real script, a
    real PostgreSQL 18.6 and `--from-image postgres:18-alpine`: EXIT=1 whose last
    line of output was "== UPGRADE (old image -> data -> new image) ==", with no
    ERROR line, no SUMMARY, and no arm JSON written at all -- so
    `db_rehearsal.py verdict` could only say "NOT PROVED `fresh-and-upgrade`:
    this arm reported nothing", which is the wording reserved for a step that was
    deleted or never ran.
    """

    def _rehearse(self, unreadable: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        out = pathlib.Path(self.tmp.name) / "arm.json"
        proc = self._run(
            "--image", "orch:new",
            "--from-image", "orch:old",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            "--json", str(out),
            REHEARSAL_FAKE_DB="1",
            REHEARSAL_UNREADABLE_IMAGE=unreadable,
        )
        return proc, out

    def test_it_names_the_image_and_still_reports_the_arm(self):
        proc, out = self._rehearse("orch:old")
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("cannot read the migration table out of the baseline image orch:old", combined)
        # The half that a bare `dr_die` would NOT have delivered, and the half
        # that decides whether the verdict can name anything: the fresh-install
        # findings survive, the run still summarises, and the record is written.
        self.assertIn("== SUMMARY ==", combined)
        self.assertIn("failed        : 1", combined)
        # And the line a person reads has to say the same thing as the record.
        # `$FROM_IMAGE` is set before its migration table is read, so printing it
        # bare here announced an upgrade in the one state whose whole point is
        # that the upgrade did not happen.
        self.assertIn(
            "upgraded from : <skipped: orch:old, its migration table could not be read>",
            combined,
        )
        self.assertTrue(out.exists(), "no arm JSON was written:\n" + combined)
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "fail")
        self.assertEqual(record["from_image"], "orch:old")
        self.assertEqual(record["from_version"], "")
        self.assertIn("could not be read", record["detail"])

    def test_the_verdict_names_the_failed_arm_instead_of_calling_it_silent(self):
        _proc, out = self._rehearse("orch:old")
        records = [json.loads(out.read_text(encoding="utf-8")), PASSING_REVERSE]
        ok, lines = db_rehearsal.build_verdict(records)
        self.assertFalse(ok)
        line = next(one for one in lines if "fresh-and-upgrade" in one)
        self.assertIn("**FACT**", line)
        self.assertIn("FAILED", line)
        self.assertNotIn("reported nothing", line)

    def test_a_baseline_it_CAN_read_is_not_refused(self):
        """The guard must not degrade into "every baseline is refused"."""
        proc, out = self._rehearse("")
        combined = proc.stdout + proc.stderr
        self.assertNotIn("cannot read the migration table out of the baseline image", combined)
        self.assertIn("upgrading from orch:old (V41)", combined)
        # The other direction of the SUMMARY line: it must not decay into
        # "<skipped>" for a baseline that WAS read.
        self.assertIn("upgraded from : orch:old (V41)", combined)
        self.assertNotIn("upgraded from : <skipped", combined)
        self.assertEqual(proc.returncode, 0, combined)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["outcome"], "pass")


class TheArmRecordSaysWhichUpgradeItActuallyProved(RunsTheRealScript, unittest.TestCase):
    """The four things the `detail` sentence can say, each driven for real.

    The record's outcome was asserted and its sentence was not, so replacing
    `elif from_v == latest:` with `elif False:` left all 37 tests green. That
    sentence is the only thing standing between a V41-baseline-against-a-V41-image
    arm and a reader taking it for a proved migration: it is the difference
    between "nothing runs and nothing is lost" and "a migration is safe", and the
    stub's images all reported the same version, so that branch was also the one
    every other test in this file happened to be exercising.
    """

    def _detail(self, *extra: str, **env: str) -> tuple[int, str, dict]:
        out = pathlib.Path(self.tmp.name) / "arm.json"
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            "--json", str(out),
            *extra,
            REHEARSAL_FAKE_DB="1",
            **env,
        )
        record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        return proc.returncode, proc.stdout + proc.stderr, record

    def test_a_real_migration_says_which_version_it_came_from_and_went_to(self):
        rc, combined, record = self._detail(
            "--from-image", "orch:old",
            REHEARSAL_FAKE_LATEST="41",
            REHEARSAL_OLD_VERSION="40",
        )
        self.assertEqual(rc, 0, combined)
        self.assertEqual(record["from_version"], "40")
        self.assertEqual(record["latest"], "41")
        self.assertIn("upgraded from orch:old (V40) to V41", record["detail"])
        self.assertNotIn("did not move", record["detail"])
        # And the SUMMARY a person reads agrees with it.
        self.assertIn("upgraded from : orch:old (V40)", combined)

    def test_a_baseline_at_the_same_version_is_not_reported_as_a_proved_migration(self):
        """A green arm, and the sentence that stops it being read as a migration."""
        rc, combined, record = self._detail(
            "--from-image", "orch:old",
            REHEARSAL_FAKE_LATEST="41",
            REHEARSAL_OLD_VERSION="41",
        )
        self.assertEqual(rc, 0, combined)
        self.assertEqual(record["outcome"], "pass")
        self.assertIn("the schema did not move in this commit", record["detail"])
        self.assertIn("both V41", record["detail"])
        self.assertIn("NOT that a migration is safe", record["detail"])
        self.assertNotIn("upgraded from orch:old (V41) to V41", record["detail"])

    def test_no_baseline_at_all_says_the_arm_did_not_run(self):
        rc, combined, record = self._detail()
        self.assertEqual(rc, 0, combined)
        self.assertIn("the upgrade arm did NOT run: no baseline image was supplied", record["detail"])

    def test_an_unreadable_baseline_says_which_image_and_why(self):
        rc, combined, record = self._detail(
            "--from-image", "orch:old", REHEARSAL_UNREADABLE_IMAGE="orch:old"
        )
        self.assertEqual(rc, 1, combined)
        self.assertIn("the migration table could not be read out of the baseline image orch:old", record["detail"])
        self.assertIn("no old schema to migrate forward", record["detail"])

    def test_the_four_sentences_are_all_different(self):
        """A branch that collapsed into its neighbour would satisfy each test above.

        `assertIn` on four separate records cannot see two branches producing the
        same string, so the four are compared against each other here.
        """
        details = {
            "moved": self._detail("--from-image", "orch:old", REHEARSAL_FAKE_LATEST="41", REHEARSAL_OLD_VERSION="40")[2]["detail"],
            "did-not-move": self._detail("--from-image", "orch:old", REHEARSAL_FAKE_LATEST="41", REHEARSAL_OLD_VERSION="41")[2]["detail"],
            "no-baseline": self._detail()[2]["detail"],
            "unreadable": self._detail("--from-image", "orch:old", REHEARSAL_UNREADABLE_IMAGE="orch:old")[2]["detail"],
        }
        self.assertEqual(len(set(details.values())), 4, details)


class RequireUpgradeIsARefusalAndNotADecoration(RunsTheRealScript, unittest.TestCase):
    """`--require-upgrade` was one of the commit's "three refusals" and unpinned.

    Deleting the whole `if [ -z "$FROM_IMAGE" ] && [ "$REQUIRE_UPGRADE" = 1 ]`
    branch left all 37 tests green, and `bash -n` still passed, so
    `test_the_script_still_parses` did not see it either. A gate that cannot tell
    "we could not test the upgrade" from "the upgrade is safe" is the one thing
    this flag exists to prevent, so it gets the run the commit message says was
    done by hand.

    No `--from-image`, and the stub's `docker images` prints nothing, so the
    script's own search for an older image on the box finds none.
    """

    def _rehearse(self, *extra: str, **env: str):
        out = pathlib.Path(self.tmp.name) / "arm.json"
        proc = self._run(
            "--image", "orch:new",
            "--server", "postgresql://u:p@127.0.0.1:1/postgres",
            "--pg-image", "postgres@sha256:abc",
            "--expect-major", "18",
            "--json", str(out),
            *extra,
            REHEARSAL_FAKE_DB="1",
            **env,
        )
        return proc, out

    def test_a_promised_baseline_that_is_not_there_is_a_failure_not_a_skip(self):
        proc, out = self._rehearse("--require-upgrade")
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("no older orchestrator image to upgrade FROM", combined)
        # A FAILURE and deliberately not a `dr_die`: the fresh-install phase has
        # already produced real findings and throwing them away would make a
        # missing baseline indistinguishable from a broken migration.
        self.assertIn("== SUMMARY ==", combined)
        self.assertIn("upgraded from : <skipped: no older image on this box>", combined)
        self.assertTrue(out.exists(), "no arm JSON was written:\n" + combined)
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "fail")
        self.assertEqual(record["from_image"], "")
        self.assertEqual(record["from_version"], "")

    def test_the_verdict_refuses_that_arm_too(self):
        _proc, out = self._rehearse("--require-upgrade")
        records = [json.loads(out.read_text(encoding="utf-8")), PASSING_REVERSE]
        ok, _lines = db_rehearsal.build_verdict(records)
        self.assertFalse(ok)

    def test_the_environment_form_of_the_flag_refuses_the_same_way(self):
        """DR_REHEARSAL_REQUIRE_UPGRADE is the CI-friendly spelling of it."""
        proc, _out = self._rehearse(DR_REHEARSAL_REQUIRE_UPGRADE="1")
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("no older orchestrator image to upgrade FROM", combined)

    def test_without_the_flag_a_missing_baseline_is_still_an_honest_skip(self):
        """On a laptop with no history the skip is legitimate and must stay green.

        This is the half that makes the refusal above a CHOICE rather than a new
        unconditional failure.
        """
        proc, out = self._rehearse()
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Skipping it rather than faking an old schema", combined)
        self.assertNotIn("no older orchestrator image to upgrade FROM", combined)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["outcome"], "pass")

    def test_the_verdict_still_refuses_that_pass_because_nothing_was_upgraded(self):
        """An honest skip is a green RUN and must not be a proved upgrade.

        The two halves together are the point: the script may exit 0, and the
        verdict may still not say the upgrade is safe.
        """
        _proc, out = self._rehearse()
        records = [json.loads(out.read_text(encoding="utf-8")), PASSING_REVERSE]
        ok, lines = db_rehearsal.build_verdict(records)
        self.assertFalse(ok)
        line = next(one for one in lines if "fresh-and-upgrade" in one)
        self.assertIn("NOT PROVED", line)


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

    def _job(self) -> str:
        """The db-rehearsal job's body, and nothing of the job after it."""
        self.assertIn("\n  db-rehearsal:\n", self.text)
        return self.text.split("\n  db-rehearsal:\n", 1)[1].split("\n  security:", 1)[0]

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
        """The SETTING, not the comment above it that happens to quote the setting.

        `assertIn("fetch-depth: 0", job)` was satisfied by the job's own
        explanatory line `# Required. See "WHY fetch-depth: 0" above.`, so deleting
        the real setting left this green. Measured: 37 tests, OK, with the setting
        gone. Anchored on the newlines and the indentation so only a YAML key can
        satisfy it.
        """
        job = self._job()
        self.assertIn("\n          fetch-depth: 0\n", job)

    # -------------------------------------------- the lines the arms are read from
    #
    # Each of the four below was deleted, one at a time, with the whole suite
    # still green. They are not cosmetic: the first two are the only channel
    # between an arm and the step that judges it, `if: always()` is what stops a
    # failed arm from skipping its own verdict, and the .gitignore entry is what
    # keeps the arm records and the security job's gitleaks.json out of a commit.

    def test_each_arm_writes_a_record_the_verdict_reads(self):
        """A step that reports nothing can only be judged if its file is expected.

        Removing `--json .ci-reports/arm-fresh-upgrade.json` left all 37 tests
        green. The arm then reports nothing at all and the verdict can say no more
        than "NOT PROVED `fresh-and-upgrade`: this arm reported nothing" -- a
        refusal, so not a false green, but an unexplained red on every run.
        """
        job = self._job()
        for arm in ("arm-fresh-upgrade", "arm-reversibility"):
            with self.subTest(arm=arm):
                self.assertIn(f"--json .ci-reports/{arm}.json", job)
                # and the verdict has to be reading the same two paths it writes
                self.assertIn(f".ci-reports/{arm}.json", job.split("name: The verdict", 1)[1])

    def test_the_verdict_runs_even_when_an_arm_failed(self):
        """Without `if: always()` a failed arm skips the step that decides.

        Removing it left all 37 tests green. GitHub's default is to skip a step
        once a previous one failed, and a skipped step reports no reason at all -
        the trap this repository has already been caught by once.
        """
        verdict = self._job().split("- name: The verdict", 1)[1]
        self.assertIn("if: always()", verdict.split("run:", 1)[0])

    def test_the_arm_records_are_not_committable(self):
        """Removing `/.ci-reports/` from .gitignore left all 37 tests green."""
        ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.ci-reports/", ignored)

    def test_the_upgrade_arm_is_required_to_have_actually_run(self):
        """The ARGUMENT, not the comment four lines above it that explains it.

        `assertIn("--require-upgrade", job)` is the same vacuous assert that let
        `fetch-depth: 0` be deleted: the job's own comment says "--require-upgrade
        turns ... into a failure", which satisfies it on its own. Caught by
        deleting the real argument line and watching the suite stay green.
        Anchored on the continuation line so only a real argument can satisfy it.
        """
        self.assertIn("\n            --require-upgrade \\\n", self._job())

    def test_it_is_not_a_required_check_in_this_commit(self):
        gate = self.text.split("\n  ci-ok:\n", 1)[1].split("\n  deploy:", 1)[0]
        self.assertNotIn("db-rehearsal", gate)


if __name__ == "__main__":
    unittest.main()
