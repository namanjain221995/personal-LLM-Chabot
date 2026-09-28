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


class ItRunsInsideTheSameApprovalGateAsTheDeploy(PipelineCase):
    """The gate this repository already advertises has to actually hold.

    This job is the FIRST thing in the graph to execute anything on the
    production box - box_readiness.py plus git in the shared deploy root, df,
    `flock -n` on the real deploy lock, engine_bind.py (which ssh's to the worker
    and opens TCP connections), curl asking the unauthenticated engine for a live
    generation, and a `bash -c` whose fallback runs `docker exec <production
    postgres> psql`. It declared no `environment:`, so once anyone configured the
    `production` environment's required reviewers - which pipeline.yml's own
    comment recommends - all of that would still have run on any push to main
    BEFORE a reviewer clicked approve.

    Measured on 2026-09-27: `gh api .../environments/production` returns
    "protection_rules": [] and declares zero variables and zero secrets, so
    adding the line changes nothing about how a run behaves today. It changes
    what configuring the gate BUYS.
    """

    def test_it_declares_the_same_environment_as_the_deploy(self):
        self.assertEqual(self.job.get("environment"), self.jobs[DEPLOY].get("environment"))
        self.assertEqual(self.job.get("environment"), "production")

    def test_no_job_reaches_the_box_ahead_of_that_environment(self):
        """The general form, so a future job cannot walk in front of the gate the
        way this one did. `verify` and `recovery` are environment-less too, but
        they `needs:` the deploy, so the gate is already passed by then."""
        gated = {DEPLOY}
        for name, job in self.jobs.items():
            runs_on = [str(x) for x in (job.get("runs-on") or [])]
            if "self-hosted" not in runs_on:
                continue
            if job.get("environment") == "production":
                gated.add(name)
                continue
            needs = job.get("needs") or []
            needs = needs if isinstance(needs, list) else [needs]
            self.assertTrue(
                any(n in gated for n in needs),
                f"{name} runs on the box, declares no production environment, and does "
                f"not need a job that does: needs={needs!r}",
            )


class TheDeployDecisionStepDoesNotPromiseHalfAGraph(PipelineCase):
    """`ci-ok`'s "Will the deploy run?" step exists because "A skipped job
    reports NO reason - GitHub shows 'skipped' and nothing else - so a deploy
    that does not happen is otherwise a guessing game", the failure class the
    same file blames for three lost green pushes to main. `deploy` now needs
    `box-readiness` as well, so a bare "Deploy will run." reintroduced exactly
    that guessing game inside this branch's own blast radius."""

    def _decision_step(self):
        for step in self.jobs["ci-ok"]["steps"]:
            if step.get("name") == "Will the deploy run?":
                return str(step.get("run", ""))
        self.fail("ci-ok no longer has a 'Will the deploy run?' step")

    def test_the_push_to_main_arm_names_box_readiness_as_the_second_condition(self):
        body = self._decision_step()
        arm = body.split("push:refs/heads/main:*)", 1)
        self.assertEqual(len(arm), 2, body)
        arm = arm[1].split(";;", 1)[0]
        self.assertIn("Box readiness", arm)
        self.assertNotRegex(arm, r'Deploy \*\*will run\*\*\.')

    def test_the_held_arm_says_the_box_half_is_held_too(self):
        """box-readiness carries the same deploy-intent clause, so the kill
        switch silences it as well - and the summary should say so rather than
        leaving a reader to wonder why the box was never read."""
        body = self._decision_step()
        held = body.split("the kill switch is set.", 1)
        self.assertEqual(len(held), 2, body)
        self.assertIn("Box readiness", held[1].split(";;", 1)[0])

    def test_the_step_still_prints_the_three_inputs_to_the_decision(self):
        body = self._decision_step()
        for token in ("EVENT", "REF", "KILL_SWITCH", "GITHUB_STEP_SUMMARY"):
            self.assertIn(token, body)


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


class TheGateFloorCoversTheTestsThisBranchAdded(PipelineCase):
    """The third defect this module exists for.

    This branch took .github/workflows/scripts/tests from 183 executed tests to
    291 and left `--min-tests 95` in place, under a comment that said "102 run
    today". 196 of 291 tests could have stopped executing with the gate still
    green - in a branch whose entire subject is gates that mean what they say.

    The reason given for not fixing it was that the floor is "shared with other
    tracks". It is not: `--min-tests 95` appeared exactly once in pipeline.yml,
    against `--start .github/workflows/scripts/tests`; the launcher suite has
    its own (450) and monitoring a third (33). So these cases assert the floor
    against a LIVE COUNT of the directory, in both directions, rather than
    against a number typed into a comment.
    """

    #: How far the floor may sit below what executes today. Slack enough that
    #: adding a test is not an edit to pipeline.yml; tight enough that a class
    #: dropping out of discovery fails the job.
    SLACK = 20

    #: The suite these cases are about.
    OURS = ".github/workflows/scripts/tests"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        suite = unittest.defaultTestLoader.discover(str(HERE), top_level_dir=str(HERE))
        cls.collected = suite.countTestCases()

    def floors(self) -> dict:
        """Every `--min-tests` in the file, keyed by the suite it gates.

        Read from each `run:` block with its SHELL LINE CONTINUATIONS removed
        rather than with one multi-line regex: the arguments are spread over
        continuation lines in this job and sit on a single line in the launcher's,
        and a pattern that has to cope with both is a pattern that quietly
        matches neither after somebody reindents the YAML.
        """
        found = {}
        for job in self.jobs.values():
            for step in job.get("steps", []) or []:
                flat = _flat(str(step.get("run", "")).replace("\\\n", " "))
                for match in re.finditer(r"--start (\S+) --min-tests (\d+)", flat):
                    found[match.group(1)] = int(match.group(2))
                for match in re.finditer(r"--min-tests (\d+) --start (\S+)", flat):
                    found[match.group(2)] = int(match.group(1))
        return found

    def test_the_file_still_gates_this_directory_at_all(self):
        self.assertIn(self.OURS, self.floors(), self.floors())

    def test_more_than_one_suite_is_gated_so_the_next_case_has_teeth(self):
        self.assertGreater(len(self.floors()), 1, self.floors())

    def test_the_floor_is_not_shared_with_another_suite(self):
        """The claim that stopped this being fixed for two rounds, checked."""
        floors = self.floors()
        ours = floors[self.OURS]
        others = {start: floor for start, floor in floors.items() if start != self.OURS}
        self.assertNotIn(ours, others.values(), floors)

    def test_the_floor_actually_covers_the_tests_in_this_directory(self):
        floor = self.floors()[self.OURS]
        self.assertGreaterEqual(
            floor,
            self.collected - self.SLACK,
            f"{self.collected} tests are collected here and the floor is {floor}: "
            f"{self.collected - floor} of them could stop executing with the gate "
            f"still green. Raise `--min-tests` at the 'The CI gate scripts have "
            f"tests, and they pass' step in pipeline.yml to {self.collected - 8}, "
            f"and correct the count in the comment above it in the same edit.",
        )

    def test_the_floor_is_never_above_what_exists(self):
        """A floor over the real count is a gate that is permanently red, which
        is the other way to make a gate stop meaning something."""
        floor = self.floors()[self.OURS]
        self.assertLessEqual(floor, self.collected, f"floor {floor} > {self.collected} collected")


class ThePreDeployCompletionMatchesThePostDeployOne(PipelineCase):
    """The pre-deploy read must not refuse a box `verify` would have passed.

    box_probes.probe_real_completion and `verify`'s "The model actually
    generates (not just answers /health)" step ask the same engine the same
    question. They gave their curl DIFFERENT deadlines - 120 here, 180 there -
    so a slow prefill could refuse the deploy an hour before the step that would
    have accepted it. The same argument as the disk floor: a readiness check
    standing on a different number is noise rather than evidence.
    """

    def _verify_step(self) -> str:
        for step in self.jobs["verify"]["steps"]:
            if "actually generates" in str(step.get("name", "")):
                return str(step["run"])
        self.fail("the verify job no longer has a real-completion step")
        raise AssertionError  # unreachable, for the type checker

    def test_the_completion_deadline_is_the_one_verify_gives_the_same_curl(self):
        run = self._verify_step()
        deadlines = re.findall(r"curl -fsS -m (\d+) -H 'Content-Type: application/json'", run)
        self.assertEqual(len(deadlines), 1, run)
        self.assertEqual(int(deadlines[0]), box_probes.TIMEOUTS["completion"])

    def test_both_read_the_reply_from_content_and_from_reasoning_content(self):
        """`chat_template_kwargs.enable_thinking` is honoured by the chat
        TEMPLATE, not by the server. A template that ignores it puts the tokens
        in `reasoning_content`, and a one-field read then fails a healthy engine
        - and in verify's case fires `recovery`, which restarts the main model,
        to fix a reply-parsing bug."""
        self.assertIn("reasoning_content", self._verify_step())
        source = pathlib.Path(box_probes.__file__).read_text(encoding="utf-8")
        self.assertIn("reasoning_content", source)

    def test_both_ask_for_the_same_generation(self):
        """The JSON body in the shell step carries backslash-escaped quotes, so
        the comparison is made against the text with backslashes and whitespace
        removed - not against a guess at how the shell was quoted today."""
        run = self._verify_step()
        naked = re.sub(r"[\s\\]+", "", run)
        self.assertIn(re.sub(r"\s+", "", box_probes.COMPLETION_PROMPT), naked)
        self.assertIn('"max_tokens":' + str(box_probes.COMPLETION_MAX_TOKENS), naked)
        self.assertIn('"enable_thinking":false', naked)

    def test_both_assert_the_global_counter_advanced_and_not_a_reply_alone(self):
        run = self._verify_step()
        self.assertIn("vllm:generation_tokens_total", run)
        self.assertIn('[ "$after" -gt "$before" ]', run)


if __name__ == "__main__":
    unittest.main()
