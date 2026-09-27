"""A --full deploy reloads the main model ON PURPOSE.

WHY THIS FILE EXISTS. scripts/deploy-smoke.sh asserts four post-rollout
invariants, and one of them is MODEL CLOCK: the main model container did not
restart, compared against the pre-deploy record rather than merely observed. It
was written for the verify job and, until 2026-09-27, had no caller at all -
`grep -rn deploy-smoke` over the tree found its own header and documentation
prose. The deploy-honesty branch wires it in, and always passes `--baseline`.

MEASURED THE SAME DAY, against a real .runtime/releases/*/record.json - all 41
of them on the box carry sf-local-ai-vllm-1.started_at, checked - with the start
time a `--full` reload would produce:

    FAIL the main model RESTARTED: 2026-09-24T23:01:47.425914126Z ->
    2026-09-27T14:40:00.000000000Z. A routine deploy must not reset this clock.
    passed=0 failed=1
    exit=1

deploy-smoke.sh exits 1 when anything failed, the verify step re-raises that
with `exit "$rc"`, and the recovery job's `if` fires on
`needs.verify.result == 'failure'`. So a deliberate, successful `--full` deploy
- workflow_dispatch input `full`, or the repository variable DEPLOY_FULL - would
have turned verify red and fired a diagnostics job, for doing exactly what it
was asked to do. The step immediately above it in the same job has had a `--full`
exemption since it was written, for that precise reason.

The two callers now share ONE decision, dr_model_clock_verdict, so they cannot
disagree again: with a reload expected the check asserts the clock MOVED, and
without one it asserts it did not. Both directions are assertions - neither is a
skip - because "the reload did not happen" is also a failed --full deploy.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[4]
PIPELINE = REPO / ".github" / "workflows" / "pipeline.yml"
LIB = REPO / "scripts" / "lib" / "deploy-common.sh"
SMOKE = REPO / "scripts" / "deploy-smoke.sh"
VLLM = "sf-local-ai-vllm-1"

#: Two instants in the shape docker reports and deploy-record.sh stores.
WAS = "2026-09-24T23:01:47.425914126Z"
NOW = "2026-09-27T14:40:00.000000000Z"


def verdict(recorded: str, observed: str, expected: str) -> tuple[int, str]:
    """dr_model_clock_verdict out of the real deploy-common.sh."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; shift; dr_model_clock_verdict "$@"',
            "bash",
            str(LIB),
            recorded,
            observed,
            expected,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout.strip()


class TheVerdictAnswersTheQuestionThatWasAsked(unittest.TestCase):
    """Four states, and which of them is a failure depends on the request."""

    def test_an_unchanged_clock_on_a_rolling_deploy_is_the_property_we_want(self):
        self.assertEqual((0, "preserved"), verdict(WAS, WAS, "0"))

    def test_a_moved_clock_on_a_rolling_deploy_is_the_regression(self):
        # TECHSARA_PRESERVE_MAIN_MODEL did not hold: 15-25 minutes of the
        # product answering nothing, on a deploy that did not ask for it.
        self.assertEqual((1, "restarted"), verdict(WAS, NOW, "0"))

    def test_a_moved_clock_when_a_reload_was_asked_for_is_success(self):
        self.assertEqual((0, "reloaded"), verdict(WAS, NOW, "1"))

    def test_an_unchanged_clock_when_a_reload_was_asked_for_is_a_failure(self):
        # --full means `techsara down` then `up`. If the engine came through
        # that with the same start time, the reload the operator asked for did
        # not happen, and saying nothing about it would be the same kind of
        # green tick this whole track is about.
        self.assertEqual((1, "not-reloaded"), verdict(WAS, WAS, "1"))

    def test_a_missing_instant_is_not_a_verdict(self):
        for recorded, observed in (("", NOW), (WAS, ""), ("", "")):
            with self.subTest(recorded=recorded, observed=observed):
                self.assertEqual((1, "unreadable"), verdict(recorded, observed, "0"))

    def test_only_a_literal_1_turns_the_assertion_around(self):
        # The preservation assertion is the one that must not be switched off by
        # a typo or by a truthy-looking string, so anything that is not exactly
        # `1` leaves it armed.
        for expected in ("", "0", "true", "yes", "TRUE", "2", "-1"):
            with self.subTest(expected=expected):
                self.assertEqual((1, "restarted"), verdict(WAS, NOW, expected))


class TheSmokeScriptUsesIt(unittest.TestCase):
    """The real MODEL CLOCK block, run against fixtures.

    The two lines that read the live container are replaced by arguments;
    everything else in the block is the shipped text. That is what makes this a
    behaviour test rather than a grep: the sentence a person reads in the job
    log is produced here.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = SMOKE.read_text(encoding="utf-8")
        lines = cls.source.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if "== MODEL CLOCK ==" in line), None
        )
        assert start is not None, "deploy-smoke.sh no longer has a MODEL CLOCK section"
        stop = next(
            (i for i in range(start + 1, len(lines)) if "== MODEL ==" in lines[i]), None
        )
        assert stop is not None, "deploy-smoke.sh no longer has a MODEL section after it"
        body = [
            line
            for line in lines[start:stop]
            if not line.startswith('vllm="$(dr_container_for')
            and not line.startswith('started="$(docker inspect')
        ]
        assert len(body) == stop - start - 2, (
            "the MODEL CLOCK block no longer reads the container in the two lines this "
            "test replaces with its own inputs"
        )
        cls.block = "\n".join(body)

    def run_block(self, *, recorded: str | None, observed: str, expect_restart: str):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = pathlib.Path(tmp) / "record.json"
            containers = {}
            if recorded is not None:
                containers[VLLM] = {"started_at": recorded}
            baseline.write_text(json.dumps({"containers": containers}), encoding="utf-8")
            harness = (
                "set -euo pipefail\n"
                f'. "{LIB}"\n'
                f'BASELINE="{baseline}"\n'
                f'started="{observed}"\n'
                f'EXPECT_MODEL_RESTART="{expect_restart}"\n'
                f'vllm="{VLLM}"\n'
                "PASS=0; FAIL=0\n"
                "ok()  { printf '  PASS %s\\n' \"$*\"; PASS=$((PASS + 1)); }\n"
                "bad() { printf '  FAIL %s\\n' \"$*\"; FAIL=$((FAIL + 1)); }\n"
                "skip(){ printf '  SKIP %s\\n' \"$*\"; }\n"
                f"{self.block}\n"
                '[ "$FAIL" -eq 0 ] || exit 1\n'
            )
            proc = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, timeout=120
            )
            return proc.returncode, proc.stdout + proc.stderr

    def test_a_full_deploy_that_reloaded_the_model_passes(self):
        # THE case that was red. Same fixture shape as the record on the box.
        rc, out = self.run_block(recorded=WAS, observed=NOW, expect_restart="1")
        self.assertEqual(0, rc, out)
        self.assertIn("PASS", out)
        self.assertIn("what --full asked for", out)
        self.assertNotIn("must not reset this clock", out)

    def test_a_rolling_deploy_that_restarted_the_model_still_fails(self):
        rc, out = self.run_block(recorded=WAS, observed=NOW, expect_restart="0")
        self.assertEqual(1, rc, out)
        self.assertIn("the main model RESTARTED", out)
        self.assertIn("A routine deploy must not reset this clock.", out)

    def test_a_rolling_deploy_that_preserved_the_model_passes(self):
        rc, out = self.run_block(recorded=WAS, observed=WAS, expect_restart="0")
        self.assertEqual(0, rc, out)
        self.assertIn("was NOT restarted", out)

    def test_a_full_deploy_whose_model_never_reloaded_fails(self):
        rc, out = self.run_block(recorded=WAS, observed=WAS, expect_restart="1")
        self.assertEqual(1, rc, out)
        self.assertIn("did not move", out)

    def test_a_baseline_that_does_not_mention_the_container_skips(self):
        # Unchanged behaviour, asserted because the rewrite moved the branch it
        # lives in: a record with no vllm entry is not evidence either way.
        rc, out = self.run_block(recorded=None, observed=NOW, expect_restart="1")
        self.assertEqual(0, rc, out)
        self.assertIn("SKIP", out)
        self.assertIn("does not mention", out)


class TheFlagIsDeclaredAndDocumented(unittest.TestCase):
    def setUp(self):
        self.source = SMOKE.read_text(encoding="utf-8")

    def test_the_option_is_parsed(self):
        self.assertIn("--expect-model-restart) EXPECT_MODEL_RESTART=1 ;;", self.source)

    def test_it_defaults_to_asserting_preservation(self):
        self.assertIn("EXPECT_MODEL_RESTART=0", self.source)

    def test_the_usage_header_lists_it(self):
        # --help is an awk over this header, so the header IS the help text.
        header = self.source.split("set -euo pipefail", 1)[0]
        self.assertIn("--expect-model-restart", header)

    def test_the_comparison_goes_through_the_shared_verdict(self):
        # A second hand-rolled comparison here is how the two callers drifted
        # apart in the first place.
        self.assertIn("dr_model_clock_verdict", self.source)


class ThePipelinePassesItExactlyWhenFullWasAskedFor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PIPELINE.read_text(encoding="utf-8")
        document = yaml.safe_load(cls.text)
        cls.verify = document["jobs"]["verify"]
        cls.deploy = document["jobs"]["deploy"]
        cls.invariants = cls.step("Post-rollout invariants")
        cls.rolling = cls.step("A rolling deploy did not restart the main model")

    @classmethod
    def step(cls, prefix: str) -> dict:
        found = [s for s in cls.verify["steps"] if str(s.get("name", "")).startswith(prefix)]
        assert len(found) == 1, (prefix, [s.get("name") for s in cls.verify["steps"]])
        return found[0]

    def test_the_deploy_job_publishes_whether_it_was_full(self):
        self.assertEqual(
            "${{ steps.rollout.outputs.was_full }}", self.deploy["outputs"]["was_full"]
        )

    def test_the_invariants_step_reads_that_output(self):
        self.assertEqual(
            "${{ needs.deploy.outputs.was_full }}",
            (self.invariants.get("env") or {}).get("SMOKE_WAS_FULL"),
            "the post-rollout invariants step does not know whether --full was asked for, "
            "so deploy-smoke.sh's MODEL CLOCK check asserts the model was PRESERVED on a "
            "deploy that was told to reload it",
        )

    def test_the_flag_is_passed_only_on_the_full_path(self):
        body = self.invariants["run"]
        self.assertIn("--expect-model-restart", body)
        self.assertIn('if [ "${SMOKE_WAS_FULL:-}" = "true" ]; then', body)
        # The flag must be inside that branch, not unconditional: passing it
        # always would invert the assertion for every rolling deploy.
        branch = body.split('if [ "${SMOKE_WAS_FULL:-}" = "true" ]; then', 1)[1]
        self.assertIn("--expect-model-restart", branch.split("fi", 1)[0])
        self.assertEqual(1, body.count("--expect-model-restart"))

    def test_the_baseline_is_still_passed_on_the_full_path(self):
        # Withholding --baseline would also have silenced the false failure, and
        # it would have silenced a real one with it: a --full deploy whose
        # engine never came back would report SKIP.
        body = self.invariants["run"]
        self.assertIn("--baseline", body)
        self.assertNotIn("SMOKE_WAS_FULL", body.split("--baseline", 1)[0])

    def test_the_two_model_clock_checks_in_this_job_still_agree_about_full(self):
        # The other step exits 0 on --full. If its exemption is ever removed,
        # the flag above becomes wrong in the opposite direction, so the two are
        # asserted together rather than one at a time.
        rolling = self.rolling["run"]
        self.assertIn('if [ "${DEPLOY_WAS_FULL:-}" = "true" ]; then', rolling)
        self.assertEqual(
            "${{ needs.deploy.outputs.was_full }}", self.rolling["env"]["DEPLOY_WAS_FULL"]
        )


if __name__ == "__main__":
    unittest.main()
