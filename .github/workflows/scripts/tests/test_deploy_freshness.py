"""A queued deploy is not a fresh deploy.

MEASURED, on this repository: a gate that reported success at
2026-09-19T07:33:29Z rolled out at 2026-09-21T14:11:26Z - 2 days, 6 hours,
37 minutes and 57 seconds later, unattended, because the self-hosted runner
had wedged after a DNS blip and the run sat queued until it came back. The
normal hand-off between the same two points is seconds: 12 s and 10 m 46 s are
the other two samples. The 90-minute default window sits between them on
purpose.

Nothing else in the pipeline looks at that gap. `concurrency: {group:
deploy-dgx-spark, cancel-in-progress: false}` supersedes a PENDING deploy when
a newer one queues, which fixes ORDERING; a single queued deploy with nothing
behind it is never superseded and is exactly the case above.

Every test here is on the pure decision. The workflow step that feeds it (a
`git ls-remote` and one Actions API read) cannot be exercised without a real
Actions run, which is why the script takes both facts as plain strings.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import deploy_freshness  # noqa: E402

TIP = "a" * 40
OTHER = "b" * 40
#: The instant every test measures from, so a case reads as "N minutes old".
NOW = "2026-09-23T12:00:00Z"


def minutes_ago(minutes: float, *, now: str = NOW) -> str:
    base = deploy_freshness.parse_timestamp(now)
    assert base is not None
    return (base - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(**kwargs) -> tuple[int, str]:
    """Drive main() exactly as the workflow step does, capturing its output.

    Every option the script can also read from the environment is passed
    EXPLICITLY here, including an empty `--summary`. These tests run inside the
    `policy` job, where GITHUB_STEP_SUMMARY is a real file: a test that left
    that default in place would write fixture tables into the run summary of
    the job that is testing it.
    """
    argv = ["--now", kwargs.pop("now", NOW)]
    for key, default in (
        ("event_name", "push"),
        ("branch", "main"),
        ("workflow", "pipeline.yml"),
        ("window_minutes", 90),
        ("summary", ""),
    ):
        kwargs.setdefault(key, default)
    for key, value in kwargs.items():
        if value is None:
            continue
        argv += ["--" + key.replace("_", "-"), str(value)]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = deploy_freshness.main(argv)
    return rc, out.getvalue()


class AFreshReleaseDeploys(unittest.TestCase):
    def test_the_tip_and_a_five_minute_old_gate_pass(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 0, output)
        self.assertIn("is still the tip of origin/main", output)
        self.assertIn("inside the 90-minute window", output)
        self.assertNotIn("REFUSE", output)

    def test_the_measured_twelve_second_hand_off_passes(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(0.2))
        self.assertEqual(rc, 0, output)

    def test_the_measured_ten_minute_forty_six_second_hand_off_passes(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(10.767))
        self.assertEqual(rc, 0, output)


class ACommitTheBranchHasMovedPastIsRefused(unittest.TestCase):
    def test_a_sha_that_is_not_the_tip_refuses(self):
        rc, output = run(sha=OTHER, origin_tip=TIP, gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 1, output)
        self.assertIn("no longer the tip of origin/main", output)

    def test_an_unresolvable_tip_refuses_rather_than_assuming(self):
        rc, output = run(sha=TIP, origin_tip="", gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 1, output)
        self.assertIn("could not be resolved", output)

    def test_a_short_or_missing_sha_refuses(self):
        for sha in ("", "abc1234"):
            with self.subTest(sha=sha):
                rc, output = run(sha=sha, origin_tip=TIP, gate_completed_at=minutes_ago(5))
                self.assertEqual(rc, 1, output)
                self.assertIn("not a full commit id", output)

    def test_the_tip_check_still_applies_to_a_dispatch(self):
        rc, output = run(
            sha=OTHER, origin_tip=TIP, gate_completed_at=minutes_ago(5),
            event_name="workflow_dispatch",
        )
        self.assertEqual(rc, 1, output)
        self.assertIn("no longer the tip", output)


class TheWindowBoundary(unittest.TestCase):
    """Both sides of it, because an off-by-one here is a bricked release path."""

    def test_eighty_nine_minutes_passes(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(89))
        self.assertEqual(rc, 0, output)
        self.assertIn("inside the 90-minute window", output)

    def test_exactly_ninety_minutes_passes(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(90))
        self.assertEqual(rc, 0, output)

    def test_ninety_one_minutes_refuses(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(91))
        self.assertEqual(rc, 1, output)
        self.assertIn("91.0 minutes old", output)
        self.assertIn("the window is 90 minutes", output)

    def test_the_measured_two_day_gap_refuses(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP,
            gate_completed_at="2026-09-19T07:33:29Z",
            now="2026-09-21T14:11:26Z",
        )
        self.assertEqual(rc, 1, output)
        self.assertIn("3277.9 minutes old", output)

    def test_the_window_is_configurable(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(120), window_minutes=180
        )
        self.assertEqual(rc, 0, output)


class AnUnknownAgeIsARefusal(unittest.TestCase):
    """Fail closed: this step exists because a stale run looks fresh from inside."""

    def test_no_timestamp_at_all_refuses(self):
        rc, output = run(sha=TIP, origin_tip=TIP)
        self.assertEqual(rc, 1, output)
        self.assertIn("the age of this release is unknown", output)

    def test_an_empty_timestamp_refuses(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at="   ")
        self.assertEqual(rc, 1, output)
        self.assertIn("the age of this release is unknown", output)

    def test_an_unparseable_timestamp_refuses(self):
        for raw in ("yesterday", "2026-13-45T99:99:99Z", "1758547200"):
            with self.subTest(raw=raw):
                rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=raw)
                self.assertEqual(rc, 1, output)
                self.assertIn("the age of this release is unknown", output)

    def test_an_unparseable_gate_time_falls_back_to_the_commit_timestamp(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP,
            gate_completed_at="not-a-time",
            fallback_timestamp=minutes_ago(40),
        )
        self.assertEqual(rc, 0, output)
        self.assertIn("the gate's own time was unavailable", output)

    def test_the_fallback_is_measured_too_and_can_refuse(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at="", fallback_timestamp=minutes_ago(200)
        )
        self.assertEqual(rc, 1, output)
        self.assertIn("200.0 minutes old", output)

    def test_the_commit_timestamp_fallback_can_refuse_a_release_that_is_not_stale(self):
        """The known false-refusal path, recorded on purpose rather than hidden.

        The fallback is the COMMITTER's clock. A commit written three hours
        before it is pushed carries that hour, so with the Actions API read
        unavailable a perfectly fresh release measures as three hours old and is
        refused. The first version of this guard described the fallback as
        "older than the gate's completion by roughly the length of CI", which is
        not true and made it look safer than it is.

        It is kept because measuring TOO LARGE fails closed and having no
        measurement at all would not, and because nothing better exists
        offline - the run's own start time is not a `github` context property.
        What the refusal owes the operator instead is (a) the source it used,
        so the reading can be recognised as the weak one, and (b) an escape
        hatch that works. Both are asserted here. If someone later loosens this
        into a pass, this test says out loud what was traded away.
        """
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at="", fallback_timestamp=minutes_ago(180)
        )
        self.assertEqual(rc, 1, output)
        self.assertIn("the deployed commit's timestamp", output)
        self.assertIn("the gate's own time was unavailable", output)
        self.assertIn("gh workflow run pipeline.yml --ref main -f deploy=true", output)

    def test_the_gate_time_wins_when_both_are_present(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP,
            gate_completed_at=minutes_ago(5),
            fallback_timestamp=minutes_ago(500),
        )
        self.assertEqual(rc, 0, output)
        self.assertIn("the gate's completion time", output)


class TheRefusalSaysHowToProceed(unittest.TestCase):
    """The advice has to be advice that works, and it is not the same advice.

    CHANGED 2026-09-27, and deliberately so. This class used to assert that
    ALL FOUR refusals print the literal `gh workflow run ... --ref main -f
    deploy=true`, i.e. it asserted the defect: for three of the four that
    command cannot do what the sentence above it promises.

      * `sha != origin_tip` - a dispatch on the branch deploys the TIP, which
        is the other commit. It never puts the refused commit anywhere, so
        "to deploy this deliberately" is a false sentence.
      * an unresolvable tip - a dispatch runs the same tip check against the
        same unreachable remote and refuses identically.
      * a malformed github.sha - a dispatch arrives with the same value.

    Telling an operator at 3 a.m. to run a command that cannot work is the
    exact class of defect this file was added to remove, so the assertion is
    now per reason: the AGE refusals (where the refused commit IS the tip, so
    a dispatch really does roll out this same commit) keep the command, and the
    other three say what actually moves the deploy forward.
    """

    def test_the_two_age_refusals_print_the_literal_gh_workflow_run_command(self):
        cases = (
            dict(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(91)),   # stale
            dict(sha=TIP, origin_tip=TIP),                                     # unknown age
        )
        for case in cases:
            with self.subTest(**case):
                rc, output = run(**case)
                self.assertEqual(rc, 1, output)
                self.assertIn("gh workflow run pipeline.yml --ref main -f deploy=true", output)
                self.assertIn("with the age on your name", output)

    def test_every_refusal_says_nothing_on_the_box_was_touched(self):
        cases = (
            dict(sha=OTHER, origin_tip=TIP, gate_completed_at=minutes_ago(5)),
            dict(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(91)),
            dict(sha=TIP, origin_tip=TIP),
            dict(sha=TIP, origin_tip=""),
            dict(sha="abc", origin_tip=TIP),
        )
        for case in cases:
            with self.subTest(**case):
                rc, output = run(**case)
                self.assertEqual(rc, 1, output)
                self.assertIn("Nothing on the box has been touched", output)

    def test_a_commit_the_branch_moved_past_is_not_sent_to_a_dispatch(self):
        rc, output = run(sha=OTHER, origin_tip=TIP, gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 1, output)
        self.assertIn("refused because", output)
        # The command is named only to say it is the WRONG one, with the reason.
        self.assertIn("is NOT the command for this", output)
        self.assertIn(f"it would deploy {TIP[:12]}, not {OTHER[:12]}", output)
        # And the thing that does work, on the box, with a person present.
        self.assertIn(f"scripts/deploy.sh --ref {OTHER}", output)

    def test_an_unresolvable_tip_is_a_runner_fault_not_an_override(self):
        rc, output = run(sha=TIP, origin_tip="", gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 1, output)
        self.assertIn("refuse identically", output)
        self.assertIn("Re-run this job once the runner can reach the remote again", output)
        self.assertNotIn("To deploy this same commit deliberately", output)

    def test_a_malformed_sha_is_reported_as_wiring_not_as_an_override(self):
        rc, output = run(sha="abc", origin_tip=TIP)
        self.assertEqual(rc, 1, output)
        self.assertIn("wiring fault in the workflow", output)
        self.assertIn("Fix how github.sha is passed to this step", output)
        self.assertNotIn("To deploy this same commit deliberately", output)

    def test_the_reason_is_named_in_the_facts_table(self):
        for case, expected in (
            (dict(sha="abc", origin_tip=TIP), "wiring"),
            (dict(sha=TIP, origin_tip=""), "tip-unresolved"),
            (dict(sha=OTHER, origin_tip=TIP, gate_completed_at=minutes_ago(5)), "tip-moved"),
            (dict(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(91)), "age-stale"),
            (dict(sha=TIP, origin_tip=TIP), "age-unknown"),
        ):
            with self.subTest(expected=expected):
                rc, output = run(**case)
                self.assertEqual(rc, 1, output)
                self.assertIn(f"refused because        {expected}", output)

    def test_the_earliest_refusal_owns_the_advice(self):
        # Both the tip check and the age check refuse here. A malformed commit
        # id is not made better by also knowing the release is old, so the
        # advice is the wiring one and the age one must not also appear.
        rc, output = run(sha="abc", origin_tip=TIP, gate_completed_at=minutes_ago(500))
        self.assertEqual(rc, 1, output)
        self.assertIn("wiring fault in the workflow", output)
        self.assertNotIn("with the age on your name", output)

    def test_a_pass_does_not_print_the_escape_hatch(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5))
        self.assertEqual(rc, 0, output)
        self.assertNotIn("gh workflow run", output)

    def test_the_command_follows_the_branch_and_workflow_it_was_given(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(500),
            branch="release", workflow="other.yml",
        )
        self.assertEqual(rc, 1, output)
        self.assertIn("gh workflow run other.yml --ref release -f deploy=true", output)


class ADispatchIsAHumanTakingResponsibility(unittest.TestCase):
    def test_a_stale_dispatch_is_reported_and_not_enforced(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5000),
            event_name="workflow_dispatch",
        )
        self.assertEqual(rc, 0, output)
        self.assertIn("dispatched by hand", output)
        self.assertIn("Not enforced", output)

    def test_a_dispatch_with_no_timestamp_at_all_still_deploys(self):
        # A dispatch has no head_commit.timestamp. If the Actions API read
        # also fails there is nothing left to measure - and refusing here
        # would refuse the very command every other refusal recommends,
        # leaving the release path with no escape hatch at all.
        rc, output = run(sha=TIP, origin_tip=TIP, event_name="workflow_dispatch")
        self.assertEqual(rc, 0, output)
        self.assertIn("Not enforced", output)

    def test_a_push_with_no_timestamp_at_all_is_still_refused(self):
        rc, output = run(sha=TIP, origin_tip=TIP, event_name="push")
        self.assertEqual(rc, 1, output)
        self.assertIn("the age of this release is unknown", output)

    def test_a_dispatch_of_a_commit_main_moved_past_is_still_refused(self):
        rc, output = run(sha=OTHER, origin_tip=TIP, event_name="workflow_dispatch")
        self.assertEqual(rc, 1, output)
        self.assertIn("no longer the tip", output)

    def test_a_stale_push_is_enforced(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5000), event_name="push"
        )
        self.assertEqual(rc, 1, output)


class ClockSkewIsNotStaleness(unittest.TestCase):
    def test_a_timestamp_slightly_in_the_future_passes_quietly(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(-2))
        self.assertEqual(rc, 0, output)
        self.assertNotIn("FUTURE", output)

    def test_a_timestamp_far_in_the_future_passes_but_says_so(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(-600))
        self.assertEqual(rc, 0, output)
        self.assertIn("FUTURE", output)


class TheReportItWrites(unittest.TestCase):
    def test_the_step_summary_records_the_verdict_and_the_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / "summary.md"
            rc, _ = run(
                sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(91), summary=str(summary)
            )
            self.assertEqual(rc, 1)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("### Freshness: REFUSED", text)
            self.assertIn("gh workflow run", text)

    def test_a_summary_that_cannot_be_written_does_not_change_the_verdict(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5),
            summary="/proc/definitely/not/writable/summary.md",
        )
        self.assertEqual(rc, 0, output)

    def test_a_nonsense_window_is_a_usage_error_not_a_pass(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP, gate_completed_at=minutes_ago(5), window_minutes=0
        )
        self.assertEqual(rc, 2, output)


class TheGateTimeComesOutOfTheActionsJobsPayload(unittest.TestCase):
    """The workflow curls the API to a file; only the file is parsed here.

    That split is deliberate. The network call cannot be exercised without a
    real Actions run, so it is kept out of the thing that decides, and every
    way it can go wrong - no file, a 403 body, a job still running - is a case
    below rather than an untested branch on the release path.
    """

    def _file(self, tmp: str, payload) -> str:
        path = pathlib.Path(tmp) / "jobs.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_it_finds_the_ci_passed_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp, {"jobs": [
                {"name": "Policy validation", "completed_at": "2026-09-23T10:00:00Z"},
                {"name": "CI passed", "completed_at": "2026-09-23T11:55:00Z"},
            ]})
            self.assertEqual(
                deploy_freshness.gate_time_from_jobs_file(path, "CI passed"),
                "2026-09-23T11:55:00Z",
            )

    def test_a_job_that_has_not_finished_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp, {"jobs": [{"name": "CI passed", "completed_at": None}]})
            self.assertIsNone(deploy_freshness.gate_time_from_jobs_file(path, "CI passed"))

    def test_a_missing_job_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp, {"jobs": [{"name": "Verify production",
                                              "completed_at": "2026-09-23T11:55:00Z"}]})
            self.assertIsNone(deploy_freshness.gate_time_from_jobs_file(path, "CI passed"))

    def test_a_403_body_or_any_other_shape_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload in ({"message": "Resource not accessible by integration"}, [], "no"):
                with self.subTest(payload=payload):
                    path = self._file(tmp, payload)
                    self.assertIsNone(
                        deploy_freshness.gate_time_from_jobs_file(path, "CI passed")
                    )

    def test_a_missing_or_unreadable_file_yields_nothing(self):
        self.assertIsNone(deploy_freshness.gate_time_from_jobs_file(None, "CI passed"))
        self.assertIsNone(deploy_freshness.gate_time_from_jobs_file("", "CI passed"))
        self.assertIsNone(
            deploy_freshness.gate_time_from_jobs_file("/no/such/file.json", "CI passed")
        )

    def test_malformed_json_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "jobs.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(deploy_freshness.gate_time_from_jobs_file(str(path), "CI passed"))

    def test_the_jobs_file_drives_the_verdict_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp, {"jobs": [
                {"name": "CI passed", "completed_at": minutes_ago(91)},
            ]})
            rc, output = run(sha=TIP, origin_tip=TIP, gate_jobs_file=path)
            self.assertEqual(rc, 1, output)
            self.assertIn("91.0 minutes old", output)
            self.assertIn("the gate's completion time", output)

    def test_an_unusable_jobs_file_falls_back_to_the_commit_timestamp(self):
        rc, output = run(
            sha=TIP, origin_tip=TIP,
            gate_jobs_file="/no/such/file.json",
            fallback_timestamp=minutes_ago(30),
        )
        self.assertEqual(rc, 0, output)
        self.assertIn("the gate's own time was unavailable", output)

    def test_with_no_jobs_file_and_no_fallback_the_deploy_is_refused(self):
        rc, output = run(sha=TIP, origin_tip=TIP, gate_jobs_file="/no/such/file.json")
        self.assertEqual(rc, 1, output)
        self.assertIn("the age of this release is unknown", output)


class TimestampParsing(unittest.TestCase):
    def test_it_accepts_the_shapes_github_actually_emits(self):
        for raw in (
            "2026-09-19T07:33:29Z",
            "2026-09-19T07:33:29z",
            "2026-09-19T07:33:29+00:00",
            "2026-09-19T09:33:29+02:00",
            "2026-09-19 07:33:29",
        ):
            with self.subTest(raw=raw):
                value = deploy_freshness.parse_timestamp(raw)
                self.assertIsNotNone(value)
                self.assertEqual(value.tzinfo, dt.timezone.utc)

    def test_it_returns_none_rather_than_raising(self):
        for raw in (None, "", "   ", "later", "2026-09-19T07:33:29+99:00"):
            with self.subTest(raw=raw):
                self.assertIsNone(deploy_freshness.parse_timestamp(raw))


if __name__ == "__main__":
    unittest.main()
