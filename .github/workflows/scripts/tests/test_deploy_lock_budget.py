"""The deploy job's waiting has to fit inside the deploy job's own ceiling.

WHY THIS FILE EXISTS, and why the first attempt at it was wrong.

scripts/deploy.sh waits on purpose. It queues behind a hand-run deploy for the
deploy lock (dr_lock_acquire, default 1800 s) and behind an automatic engine
recovery for the engine lock (engine_lock_acquire, default 1200 s). A person at
a terminal can wait that long. A GitHub Actions job cannot: `timeout-minutes`
cancels it wherever the step happens to be, and if that is inside
`techsara up` the box is left part-recreated with no health gate and no
rollback.

The first fix for that set the two waits to 900 and 600 in the deploy job's
`env:` and called it "25 minutes of waiting inside 45". That arithmetic counts
one wait of each kind. The job performs FOUR:

  1. the preflight DRY RUN invokes deploy.sh, and dr_lock_acquire runs before
     the `--dry-run` exit, so the dry run queues for the deploy lock too. The
     step before it tests the lock with `flock -n`, which narrows the window
     and does not close it;
  2. the ROLLOUT invokes deploy.sh again - a second process, a second wait;
  3. apply() takes the engine lock on the way forward;
  4. apply() takes the engine lock AGAIN for the rollback. That looks
     re-entrant and is not: engine_lock_acquire() skips only while
     ENGINE_LOCK_HELD_BY names a live process, and engine_lock_release()
     unsets it. The second acquire is a real flock with a real timeout.

900 + 900 + 600 + 600 = 3000 s = 50 minutes, inside a 45-minute job: the same
number the pair of variables was set to remove.

So the ceiling is now a BUDGET per invocation - DEPLOY_WALL_BUDGET_S, which
deploy.sh clamps every wait against - and the arithmetic is asserted from the
workflow file itself, here, in the `policy` job. A number changed in
pipeline.yml with no thought for the ceiling fails a test instead of a rollout.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[4]
PIPELINE = REPO / ".github" / "workflows" / "pipeline.yml"
LIB = REPO / "scripts" / "lib" / "deploy-common.sh"
DEPLOY_SH = REPO / "scripts" / "deploy.sh"

#: What the job needs for WORK, on top of every second it spends waiting. An
#: ALLOWANCE, not a measurement, and deliberately generous: it covers the
#: rollback path, which is the expensive one.
#:
#:   * two `techsara up` runs (forward, then the rollback's);
#:   * two health gates. Each is bounded by its own curl timeouts in
#:     scripts/deploy.sh: 20 s for the orchestrator, 20 s for the frontend,
#:     180 s for the real completion, plus the v1 gateway and, in dual mode,
#:     scripts/cluster-status.sh;
#:   * the job's other steps - checkout, freshness, the two preflights, "the
#:     box is serving the commit we asked for", "report what is now serving".
#:
#: Nothing here is a claim about how long a deploy takes, and NOTHING HERE CAN
#: MEASURE ONE. An earlier version of this comment said that if the allowance
#: were too small "the assertion below fails in CI rather than the rollout being
#: cancelled on the box, which is the safe direction". It cannot: the assertion
#: reads constants out of pipeline.yml, so the only thing that can make it fail
#: is somebody editing a number. A deploy that genuinely takes longer than this
#: allowance is cancelled on the box exactly as it would be without this file.
#:
#: What the file does buy is the thing that actually went wrong: a wait raised,
#: a budget deleted, or `timeout-minutes` lowered can no longer pass review
#: unnoticed. Timing the real thing needs a real Actions run.
#:
#: It is also the ROLLING path's allowance. A `--full` deploy reloads the main
#: model on purpose and does not fit - see
#: TheFullDeployPathIsOutsideTheCeiling below, which keeps pipeline.yml's
#: written acknowledgement of that and the arithmetic in agreement.
WORK_ALLOWANCE_S = 900


def call(function: str, *args: str) -> tuple[int, str, str]:
    """Run one helper out of the real deploy-common.sh, with fixture inputs."""
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; shift; ' + function + ' "$@"', "bash", str(LIB), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def clamp(requested: str, started: str, ceiling: str, now: str) -> str:
    rc, out, err = call("dr_wait_within_budget", requested, started, ceiling, now)
    assert rc == 0, f"dr_wait_within_budget({requested},{started},{ceiling},{now}): {err or out}"
    return out


class TheDeployJob:
    """The deploy job as pipeline.yml actually declares it."""

    def __init__(self) -> None:
        self.text = PIPELINE.read_text(encoding="utf-8")
        document = yaml.safe_load(self.text)
        self.job = document["jobs"]["deploy"]
        self.timeout_s = int(self.job["timeout-minutes"]) * 60
        self.job_env = {str(k): str(v) for k, v in (self.job.get("env") or {}).items()}
        # YAML 1.1 reads a bare `on:` as the boolean true, which is what PyYAML
        # hands back for a workflow's trigger block.
        self.triggers = document.get("on") or document.get(True) or {}

    def model_reload_seconds(self) -> int:
        """The worst case the workflow itself puts on one model reload.

        Parsed out of the `full` input's own description rather than restated
        here, so the two cannot drift: "+15-25 min: the main model reloads".
        """
        description = str(
            self.triggers["workflow_dispatch"]["inputs"]["full"]["description"]
        )
        match = re.search(r"\+\s*(\d+)\s*-\s*(\d+)\s*min", description)
        assert match, f"the `full` input no longer states a reload cost: {description!r}"
        return int(match.group(2)) * 60

    def invocations(self) -> list[tuple[str, dict[str, str]]]:
        """(step name, effective env) for every step that runs deploy.sh.

        Matched on the invocation, not on the words "deploy.sh": two other
        steps mention the path without running it.
        """
        found = []
        for step in self.job.get("steps") or []:
            body = step.get("run") or ""
            if 'scripts/deploy.sh" --ref' not in body:
                continue
            env = dict(self.job_env)
            env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
            found.append((str(step.get("name")), env))
        return found

    @staticmethod
    def waits(step_name: str, env: dict[str, str]) -> int:
        """The seconds this invocation can spend WAITING, worst case."""
        deploy_lock = int(env.get("DEPLOY_LOCK_WAIT", "1800"))
        engine_lock = int(env.get("ENGINE_LOCK_WAIT", "1200"))
        if "--dry-run" in step_name or "dry run" in step_name:
            # `--dry-run` exits before apply(), so the engine lock is never
            # reached. The deploy lock still is.
            raw = deploy_lock
        else:
            # The deploy lock once, and the engine lock twice: once going
            # forward and once for the rollback.
            raw = deploy_lock + 2 * engine_lock
        budget = int(env.get("DEPLOY_WALL_BUDGET_S", "0"))
        return min(raw, budget) if budget > 0 else raw


class TheBudgetArithmeticHolds(unittest.TestCase):
    def setUp(self):
        self.deploy = TheDeployJob()

    def test_the_job_runs_the_deploy_script_more_than_once(self):
        # The premise of the whole file. If this ever becomes one invocation,
        # the sums below are more conservative than they need to be - which is
        # safe - but the reader should know the shape changed.
        names = [name for name, _ in self.deploy.invocations()]
        self.assertGreaterEqual(len(names), 2, names)

    def test_every_invocation_is_given_a_ceiling(self):
        for name, env in self.deploy.invocations():
            with self.subTest(step=name):
                self.assertIn(
                    "DEPLOY_WALL_BUDGET_S", env,
                    f"the step {name!r} runs deploy.sh with no DEPLOY_WALL_BUDGET_S, so its "
                    "waits are bounded only by the job timeout - which cancels it wherever "
                    "it happens to be, including inside `techsara up`",
                )
                self.assertGreater(int(env["DEPLOY_WALL_BUDGET_S"]), 0, name)

    def test_a_ceiling_is_never_shorter_than_the_lock_wait_it_governs(self):
        # Not a safety property - a clamp is always safe - but a budget below
        # the deploy-lock wait silently shortens the primary wait, and that
        # should be a deliberate edit rather than a side effect.
        for name, env in self.deploy.invocations():
            with self.subTest(step=name):
                self.assertGreaterEqual(
                    int(env["DEPLOY_WALL_BUDGET_S"]),
                    int(env.get("DEPLOY_LOCK_WAIT", "1800")),
                    name,
                )

    def test_all_the_waiting_plus_the_work_fits_inside_the_job_timeout(self):
        total = sum(self.deploy.waits(name, env) for name, env in self.deploy.invocations())
        self.assertLessEqual(
            total + WORK_ALLOWANCE_S,
            self.deploy.timeout_s,
            f"the deploy job can wait {total}s and needs {WORK_ALLOWANCE_S}s to do the work, "
            f"which is more than its own {self.deploy.timeout_s}s timeout. When the timeout "
            "wins, GitHub cancels the step wherever it is - and inside `techsara up` that "
            "leaves the stack part-recreated with no health gate and no rollback.",
        )

    def test_the_per_wait_numbers_alone_would_not_fit(self):
        # WHY THE BUDGET IS NOT REDUNDANT. Without it, the same DEPLOY_LOCK_WAIT
        # and ENGINE_LOCK_WAIT values overrun the job - so removing
        # DEPLOY_WALL_BUDGET_S and "just keeping the numbers small" reintroduces
        # the defect. If this test ever fails, the numbers really are small
        # enough on their own and the budget can be reconsidered deliberately.
        unbudgeted = 0
        for name, env in self.deploy.invocations():
            stripped = {k: v for k, v in env.items() if k != "DEPLOY_WALL_BUDGET_S"}
            unbudgeted += self.deploy.waits(name, stripped)
        self.assertGreater(
            unbudgeted + WORK_ALLOWANCE_S,
            self.deploy.timeout_s,
            "the raw per-wait numbers now fit inside the job timeout on their own",
        )


class TheClampIsArithmeticAnyoneCanCheck(unittest.TestCase):
    """dr_wait_within_budget takes its clock as an argument, so it is testable."""

    def test_no_ceiling_leaves_the_requested_wait_alone(self):
        # The hand-run default. An operator's 30-minute wait is theirs.
        for ceiling in ("0", "", "none", "-1"):
            with self.subTest(ceiling=ceiling):
                self.assertEqual(clamp("1800", "1000", ceiling, "1000"), "1800")

    def test_a_wait_inside_the_budget_is_unchanged(self):
        self.assertEqual(clamp("900", "1000", "1200", "1000"), "900")

    def test_a_wait_longer_than_the_budget_is_cut_to_what_is_left(self):
        self.assertEqual(clamp("1800", "1000", "1200", "1000"), "1200")

    def test_time_already_spent_comes_off_the_budget(self):
        # 1200 s of budget, 900 of it spent: a 600 s wait gets 300.
        self.assertEqual(clamp("600", "1000", "1200", "1900"), "300")

    def test_a_spent_budget_yields_a_non_blocking_attempt_not_a_negative_wait(self):
        # flock -w 0 still tries once, and then FAILS DIAGNOSABLY - naming the
        # lock and its holder - which is the whole point: better than being
        # cancelled mid-`techsara up`.
        self.assertEqual(clamp("600", "1000", "1200", "2400"), "0")
        self.assertEqual(clamp("600", "1000", "1200", "99999"), "0")

    def test_the_rollbacks_engine_wait_is_what_this_actually_bounds(self):
        # The case the pair of per-wait limits missed. 1200 s of budget: the
        # deploy lock took 900, the forward engine lock took 200, and the
        # rollback's engine lock is offered 600 - it gets 100.
        self.assertEqual(clamp("600", "0", "1200", "1100"), "100")

    def test_a_non_numeric_request_is_an_error_not_a_guess(self):
        for requested in ("", "abc", "-5", "60s"):
            with self.subTest(requested=requested):
                rc, _, _ = call("dr_wait_within_budget", requested, "0", "1200", "0")
                self.assertEqual(rc, 1)

    def test_an_unreadable_clock_with_a_ceiling_set_is_an_error(self):
        # The caller falls back to the unclamped number and says so, rather
        # than this function inventing one.
        for started, now in (("", "10"), ("10", ""), ("x", "10"), ("10", "x")):
            with self.subTest(started=started, now=now):
                rc, _, _ = call("dr_wait_within_budget", "600", started, "1200", now)
                self.assertEqual(rc, 1)


class TheDeployScriptActuallyClampsWithIt(unittest.TestCase):
    """A budget the script does not consult is a comment."""

    def setUp(self):
        self.deploy = DEPLOY_SH.read_text(encoding="utf-8")
        self.code = "\n".join(
            line for line in self.deploy.splitlines() if not line.lstrip().startswith("#")
        )

    def test_both_locks_are_acquired_with_the_clamped_value(self):
        self.assertIn('dr_lock_acquire "$LOCK_WAIT_ALLOWED"', self.code)
        self.assertIn('engine_lock_acquire "$LOCK_WAIT_ALLOWED"', self.code)

    def test_neither_lock_is_acquired_with_the_raw_default_any_more(self):
        self.assertNotIn('dr_lock_acquire "${DEPLOY_LOCK_WAIT:-1800}"', self.code)
        self.assertNotIn('engine_lock_acquire "${ENGINE_LOCK_WAIT:-1200}"', self.code)

    def test_the_clamp_runs_before_each_acquire(self):
        for requested, acquire in (
            ('clamp_lock_wait "${DEPLOY_LOCK_WAIT:-1800}"', 'dr_lock_acquire "$LOCK_WAIT_ALLOWED"'),
            ('clamp_lock_wait "${ENGINE_LOCK_WAIT:-1200}"', 'engine_lock_acquire "$LOCK_WAIT_ALLOWED"'),
        ):
            with self.subTest(acquire=acquire):
                self.assertLess(self.code.index(requested), self.code.index(acquire))

    def test_the_budget_is_measured_from_the_start_of_the_script(self):
        started = self.code.index('DEPLOY_STARTED_EPOCH="$(date +%s)"')
        first_wait = self.code.index('clamp_lock_wait "${DEPLOY_LOCK_WAIT:-1800}"')
        self.assertLess(started, first_wait)

    def test_a_budget_that_is_set_but_unreadable_is_said_out_loud(self):
        # The worst of the three states: the caller believes it has a ceiling
        # and has none. `DEPLOY_WALL_BUDGET_S=20m` reads as non-numeric, the
        # clamp returns the request unchanged, and that has to be visible.
        self.assertIn("is not a whole number", self.deploy)
        self.assertIn("NO ceiling is being applied", self.deploy)

    def test_the_script_documents_the_variable_it_now_reads(self):
        # `deploy.sh --help` prints the header block, and an env var that
        # changes when a deploy gives up belongs in it.
        header = []
        for line in self.deploy.splitlines()[1:]:
            if not line.startswith("#"):
                break
            header.append(line)
        self.assertIn("DEPLOY_WALL_BUDGET_S", "\n".join(header))


class TheFullDeployPathIsOutsideTheCeiling(unittest.TestCase):
    """The case the budget cannot fix, written down instead of implied.

    Everything above is about WAITING, which a budget can bound. A `--full`
    deploy's cost is WORK: pipeline.yml's own `full` input says "+15-25 min: the
    main model reloads", and the rollback path runs `techsara up` twice, so the
    worst case is two reloads - more than the whole job timeout on its own,
    whatever the budget is set to.

    This branch does not cause that, and it makes it less likely to bite: the
    permitted waiting here is 1500 s, against 6000 s with the
    DEPLOY_LOCK_WAIT/ENGINE_LOCK_WAIT defaults this job now overrides and 3000 s
    with the "first fix" the module docstring describes - all three re-derived
    from the workflow file on 2026-09-27. What is still not true for the --full
    path is "all the waiting plus the work fits inside the job timeout", and the
    test that defends it cannot tell. So the workflow says so in words, and this
    class keeps the words and the numbers in agreement in BOTH directions: if
    someone later sizes the job so the --full rollback does fit, the note has to
    go.
    """

    #: The sentence pipeline.yml must carry while the numbers do not fit.
    ACKNOWLEDGEMENT = (
        "a --full deploy's rollback path does not fit inside this job's timeout"
    )

    def setUp(self):
        self.deploy = TheDeployJob()
        self.waiting_s = sum(
            self.deploy.waits(name, env) for name, env in self.deploy.invocations()
        )

    def test_the_rollback_path_of_a_full_deploy_needs_two_model_reloads(self):
        # The premise: apply() runs for the forward release and again for the
        # rollback, and with --full each one reloads the engine.
        self.assertGreater(self.deploy.model_reload_seconds(), 0)
        self.assertGreater(2 * self.deploy.model_reload_seconds(), self.deploy.timeout_s)

    def test_the_note_and_the_arithmetic_agree(self):
        needed = self.waiting_s + 2 * self.deploy.model_reload_seconds()
        fits = needed <= self.deploy.timeout_s
        written_down = self.ACKNOWLEDGEMENT in self.deploy.text
        self.assertEqual(
            not fits, written_down,
            f"a --full deploy's rollback path needs {needed}s worst case "
            f"({self.waiting_s}s of permitted waiting plus two "
            f"{self.deploy.model_reload_seconds()}s model reloads) against a "
            f"{self.deploy.timeout_s}s job timeout, so it "
            + ("does NOT fit" if not fits else "DOES fit")
            + " - and pipeline.yml "
            + ("does not say so" if not written_down else "says it does not")
            + ". Either the note or the numbers is now wrong.",
        )

    def test_the_rolling_path_does_fit_which_is_what_the_budget_defends(self):
        # Stated next to the case that does not, so a reader is not left to
        # infer that the ceiling is decorative. This is the unattended path.
        self.assertLessEqual(self.waiting_s + WORK_ALLOWANCE_S, self.deploy.timeout_s)


if __name__ == "__main__":
    unittest.main()
