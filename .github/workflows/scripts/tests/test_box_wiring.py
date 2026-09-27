"""How `box-readiness` is WIRED INTO pipeline.yml, asserted from the file.

test_box_probes.py and test_box_readiness.py prove the probes behave. Nothing
proved the wiring, and the wiring is where this job's safety actually lives: a
self-hosted job on a PUBLIC repository is one edited `if:` away from running a
fork's code on the production box, one edited `concurrency:` away from evicting
a pending entry of the release path's group, and one `continue-on-error: true`
away from being a green tick that means nothing.

The `policy` job's workflow_policy.py already enforces the GENERAL rules (P4
self-hosted, P5 permissions, P7 timeouts). These cases assert the SPECIFIC
shape this job was reviewed in, including the two things that a general rule
cannot see:

  * that the guard carries the deploy job's DEPLOY-INTENT clause and not just
    the branch guard, so the DEPLOY_ON_PUSH kill switch silences the box half
    of the pipeline too;
  * that the declared `timeout-minutes` is actually big enough for the probes
    to finish and PRINT THEIR REMEDIES on a slow box, measured rather than
    asserted from a sum somebody typed.

Nothing here touches a machine, a network or the GPU. It reads one YAML file
and runs the probes against the same fake runner the other cases use.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import box_probes  # noqa: E402
import workflow_policy  # noqa: E402
from _box_fixtures import FakeRunner, make_box, make_env  # noqa: E402

PIPELINE = HERE.parents[1] / "pipeline.yml"

#: The job under test, and the release job that consumes its verdict.
JOB = "box-readiness"
DEPLOY = "deploy"


def _flat(text: str) -> str:
    """Whitespace-insensitive form, so an `if:` can be reindented safely."""
    return re.sub(r"\s+", " ", str(text)).strip()


class PipelineCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
        cls.jobs = cls.doc["jobs"]
        cls.job = cls.jobs[JOB]
        cls.text = PIPELINE.read_text(encoding="utf-8")


class TheJobCannotBeReachedByAForkPullRequest(PipelineCase):
    """P4's rule, asserted on THIS job rather than on the class of jobs."""

    def test_it_is_a_self_hosted_job_so_the_rest_of_this_class_is_load_bearing(self):
        self.assertIn("self-hosted", [str(x) for x in self.job["runs-on"]])
        self.assertTrue(workflow_policy._could_be_self_hosted(self.job))

    def test_the_guard_is_the_positive_ref_equality_P4_accepts(self):
        cond = str(self.job["if"])
        self.assertTrue(
            workflow_policy._implies_ref_guard(cond, "main"),
            f"the ref guard is not a conjunct of every path through: {cond!r}",
        )

    def test_the_guard_is_present_as_the_exact_literal_the_guard_regex_fullmatches(self):
        """`_implies_ref_guard` splits on && and `fullmatch`es each conjunct, so
        `github.ref == 'refs/heads/main'` has to survive as a conjunct in its
        own right. A reworded equivalent (`contains(github.ref, 'main')`, or the
        guard folded inside a larger sub-expression) passes no gate at all."""
        guard = workflow_policy._positive_ref_guard("main")
        conjuncts = workflow_policy._split_top_level(str(self.job["if"]), "&&")
        self.assertTrue(
            any(guard.fullmatch(c.strip()) for c in conjuncts),
            f"no top-level conjunct fullmatches the guard: {conjuncts!r}",
        )

    def test_the_guard_is_never_the_inverted_form(self):
        self.assertIsNone(workflow_policy.NEGATED_REF_GUARD.search(str(self.job["if"])))

    def test_the_workflow_does_not_accept_pull_request_target_at_all(self):
        triggers = workflow_policy._triggers(self.doc)
        self.assertNotIn("pull_request_target", triggers)

    def test_the_guard_never_admits_a_pull_request(self):
        self.assertNotIn("pull_request", _flat(self.job["if"]))


class TheKillSwitchSilencesTheBoxHalfToo(PipelineCase):
    """The defect this class exists for.

    The first version of the job guarded on `refs/heads/main` alone. So with
    DEPLOY_ON_PUSH thrown - the switch whose stated purpose is "the box is
    mid-incident and nobody wants to wait on a commit and a CI run to stop the
    bleeding" - every push to main still landed a self-hosted job on that box,
    took its only runner, asked the engine for a generation and reddened the run
    for box state nobody had asked about. A dispatch with `deploy` OFF, which
    means "tests only", did the same.
    """

    #: The clause, lifted from the deploy job. `format('{0}', ...)` is
    #: load-bearing: an unset repository variable is null, and GitHub coerces
    #: it to NaN before comparing, so every comparison against it is false.
    INTENT = _flat(
        """
        ( (github.event_name == 'workflow_dispatch' && inputs.deploy) ||
          (github.event_name == 'push' &&
           !contains(fromJSON('["false","0","no","off"]'),
                     format('{0}', vars.DEPLOY_ON_PUSH))) )
        """
    )

    def test_the_deploy_job_still_carries_the_clause_this_one_is_copied_from(self):
        self.assertIn(self.INTENT, _flat(self.jobs[DEPLOY]["if"]))

    def test_readiness_carries_the_same_clause(self):
        self.assertIn(
            self.INTENT,
            _flat(self.job["if"]),
            "box-readiness must not run on the box when no deploy was asked for",
        )

    def test_a_readiness_run_therefore_implies_the_deploy_was_asked_for(self):
        """Both jobs' guards reduce to the same deploy-intent clause, so there
        is no event that reads the box without also intending a release."""
        self.assertEqual(
            _flat(self.job["if"]).count(self.INTENT),
            _flat(self.jobs[DEPLOY]["if"]).count(self.INTENT),
        )


class ItCanNeverTakeOrLoseTheReleasePathsLock(PipelineCase):
    def test_the_group_is_per_commit(self):
        group = str(self.job["concurrency"]["group"])
        self.assertIn("github.sha", group)

    def test_the_group_is_not_the_release_paths(self):
        """`deploy-dgx-spark` holds at most one running and one PENDING entry;
        a newly queued entry cancels the pending one. A readiness job in that
        group could be cancelled by a rollout, `needs.box-readiness.result`
        would read `cancelled`, and the deploy would be SKIPPED with no reason
        printed - the failure class that once cost three green pushes to main."""
        release = str(self.jobs[DEPLOY]["concurrency"]["group"])
        self.assertEqual(release, "deploy-dgx-spark")
        self.assertNotEqual(str(self.job["concurrency"]["group"]), release)

    def test_an_in_flight_entry_is_never_cancelled(self):
        self.assertIs(self.job["concurrency"]["cancel-in-progress"], False)


class ARefusalIsRedAndNeverASilentSkip(PipelineCase):
    def test_the_job_does_not_swallow_its_own_failure(self):
        self.assertNotIn("continue-on-error", self.job)
        for step in self.job["steps"]:
            self.assertNotIn("continue-on-error", step, step.get("name"))

    def test_the_probe_step_aborts_on_the_first_failure(self):
        body = "\n".join(s.get("run", "") for s in self.job["steps"])
        self.assertIn("set -euo pipefail", body)
        self.assertIn("box_readiness.py", body)
        self.assertIn("--deploy-root", body)
        self.assertIn("--ref", body)

    def test_no_step_is_conditional_on_a_status_function_that_would_mask_a_failure(self):
        for step in self.job["steps"]:
            self.assertNotIn("always()", str(step.get("if", "")), step.get("name"))
            self.assertNotIn("success()", str(step.get("if", "")), step.get("name"))

    def test_the_deploy_refuses_to_run_without_this_jobs_verdict(self):
        needs = self.jobs[DEPLOY]["needs"]
        self.assertIn(JOB, needs if isinstance(needs, list) else [needs])
        self.assertIn(f"needs.{JOB}.result == 'success'", _flat(self.jobs[DEPLOY]["if"]))

    def test_it_is_gated_behind_the_secret_scan_and_not_only_the_linter(self):
        """gitleaks-over-full-history lives in `security`, not in `policy`."""
        self.assertEqual(sorted(self.job["needs"]), ["policy", "security"])
        steps = " ".join(str(s) for s in self.jobs["security"]["steps"])
        self.assertIn("Secret scan (full git history)", steps)

    def test_it_stays_out_of_the_pr_facing_aggregate_gate(self):
        """It skips on every pull_request, and ci_gate.py counts a skipped
        dependency as a failure, so naming it in `ci-ok` would redden every PR.
        Both lists have to agree, because ci_gate.py refuses when they differ."""
        self.assertNotIn(JOB, self.jobs["ci-ok"]["needs"])
        require = " ".join(str(s.get("run", "")) for s in self.jobs["ci-ok"]["steps"])
        self.assertIn("--require", require)
        self.assertNotIn(JOB, require.split("--require", 1)[1].split("\n", 1)[0])

    def test_the_token_it_holds_grants_nothing(self):
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        self.assertEqual(workflow_policy._write_scopes(self.job["permissions"]), [])


class TheCeilingIsBigEnoughToPrintTheRemedies(PipelineCase):
    """The second defect this module exists for.

    The job used to declare `timeout-minutes: 8` on the strength of a written
    sum - "30 + 30 + 15 + 150 + 15 + 120 + 45 = 405s" - which treated
    box_probes.TIMEOUTS as per PROBE when it is per CALL. deploy-root makes
    three calls, migrations two, and completion four (one of them with
    COMPLETION_CURL_GRACE_S on top). The real figure is 570s, which does not
    fit in 480s: the only box that can reach the ceiling is a slow, sick one,
    and that is precisely the box on which GitHub would have killed the job
    before box_readiness.py printed the remedy table that is its whole product.

    So the sum is no longer written down. It is measured here.
    """

    def worst_case_seconds(self) -> float:
        """Every probe, against a healthy fake box, summing the timeout each
        subprocess call was GIVEN. A healthy scenario is the right one to
        measure: it is the path on which every call is actually made, and a
        probe that returns early can only be faster."""
        recorded: list[float] = []

        class Recording(FakeRunner):
            def run(self, argv, *, timeout, env=None):  # noqa: ANN001
                recorded.append(timeout)
                return super().run(argv, timeout=timeout, env=env)

        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            for name, fn in box_probes.ALL_PROBES:
                box_probes.run_probe(make_env(root, Recording()), name, fn)
        return sum(recorded)

    def test_the_declared_ceiling_is_the_one_box_probes_holds(self):
        self.assertEqual(self.job["timeout-minutes"], box_probes.JOB_TIMEOUT_MINUTES)

    def test_the_declared_ceiling_is_an_int_and_not_a_yaml_boolean(self):
        """P7's reason: YAML reads `timeout-minutes: true` as True, and
        `isinstance(True, int)` is True in Python."""
        self.assertIsInstance(self.job["timeout-minutes"], int)
        self.assertNotIsInstance(self.job["timeout-minutes"], bool)

    def test_every_probe_call_fits_inside_the_ceiling_with_room_for_the_checkout(self):
        budget = box_probes.JOB_TIMEOUT_MINUTES * 60 - box_probes.CHECKOUT_ALLOWANCE_S
        worst = self.worst_case_seconds()
        self.assertLessEqual(
            worst,
            budget,
            f"the probes can spend {worst:.0f}s but only {budget:.0f}s is left "
            f"after the checkout allowance; a refusal would be killed before it printed",
        )

    def test_the_old_written_sum_really_was_wrong_so_this_test_has_teeth(self):
        """If a refactor ever makes 405s correct again, this fails and asks for
        the comment to be rewritten - rather than leaving a test that asserts
        nothing because the thing it guards against became impossible."""
        self.assertGreater(self.worst_case_seconds(), 405)

    def test_the_completion_probes_subprocess_outlives_its_own_deadline(self):
        """curl gets `-m TIMEOUTS['completion']`; the subprocess gets that plus
        the grace, so a curl that honours its own deadline reports an EXIT CODE
        (which names a remedy) instead of being killed from outside it (a
        TimeoutExpired, which names none)."""
        self.assertGreater(box_probes.COMPLETION_CURL_GRACE_S, 0)


if __name__ == "__main__":
    unittest.main()
