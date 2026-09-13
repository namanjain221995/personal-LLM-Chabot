"""The aggregate gate's refusals, pinned.

ci_gate.py has carried the load for this repository since the `contains(
needs.*.result, 'failure')` gate let a skipped job through, but until now the
proof of that was a paragraph in its docstring. These tests are that proof,
and they are the reason the job-id lists in pipeline.yml and in `--require`
can be edited with any confidence at all.
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ci_gate  # noqa: E402


REQUIRED = ["policy", "orchestrator", "frontend"]


def _needs(**results: str) -> dict:
    return {job: {"result": result} for job, result in results.items()}


@contextlib.contextmanager
def _quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class EveryRequiredJobMustHaveSucceeded(unittest.TestCase):
    def test_all_success_passes(self):
        ok, _ = ci_gate.evaluate(
            _needs(policy="success", orchestrator="success", frontend="success"), REQUIRED
        )
        self.assertTrue(ok)

    def test_a_skipped_dependency_is_a_refusal_which_is_the_whole_point_of_this_script(self):
        ok, rows = ci_gate.evaluate(
            _needs(policy="success", orchestrator="skipped", frontend="success"), REQUIRED
        )
        self.assertFalse(ok)
        self.assertIn(("orchestrator", "skipped", "FAIL"), rows)

    def test_a_cancelled_dependency_is_a_refusal(self):
        ok, _ = ci_gate.evaluate(
            _needs(policy="success", orchestrator="cancelled", frontend="success"), REQUIRED
        )
        self.assertFalse(ok)

    def test_a_failed_dependency_is_a_refusal(self):
        ok, _ = ci_gate.evaluate(
            _needs(policy="failure", orchestrator="success", frontend="success"), REQUIRED
        )
        self.assertFalse(ok)

    def test_a_required_job_that_is_no_longer_a_dependency_is_a_refusal(self):
        ok, rows = ci_gate.evaluate(_needs(policy="success", frontend="success"), REQUIRED)
        self.assertFalse(ok)
        self.assertIn(("orchestrator", "<not a dependency>", "FAIL"), rows)

    def test_a_dependency_nobody_required_is_a_refusal_so_the_two_lists_cannot_drift(self):
        ok, rows = ci_gate.evaluate(
            _needs(
                policy="success",
                orchestrator="success",
                frontend="success",
                brand_new_suite="success",
            ),
            REQUIRED,
        )
        self.assertFalse(ok)
        self.assertTrue(
            any(row[0] == "brand_new_suite" and "not in --require" in row[2] for row in rows)
        )


class TheCommandLineRefusesNonsense(unittest.TestCase):
    def test_an_empty_needs_payload_is_a_usage_error_rather_than_a_pass(self):
        with _quiet():
            rc = ci_gate.main(["--needs", "   ", "--require", "policy"])
        self.assertEqual(rc, 2)

    def test_a_needs_payload_that_is_not_json_is_a_usage_error(self):
        with _quiet():
            rc = ci_gate.main(["--needs", "not json at all", "--require", "policy"])
        self.assertEqual(rc, 2)

    def test_a_needs_payload_that_is_a_list_is_a_usage_error(self):
        with _quiet():
            rc = ci_gate.main(["--needs", "[]", "--require", "policy"])
        self.assertEqual(rc, 2)

    def test_a_green_run_exits_zero_and_a_skipped_one_exits_one(self):
        green = json.dumps(_needs(policy="success"))
        amber = json.dumps(_needs(policy="skipped"))
        with _quiet():
            self.assertEqual(ci_gate.main(["--needs", green, "--require", "policy"]), 0)
            self.assertEqual(ci_gate.main(["--needs", amber, "--require", "policy"]), 1)


class TheGateAndThePipelineAgreeOnTheJobList(unittest.TestCase):
    """The lists in pipeline.yml must not drift apart from each other.

    ci_gate.py already fails at RUN time if `needs:` and `--require` disagree,
    but that costs a whole pipeline to discover. Reading the workflow here
    catches it in the policy job, in seconds, on the commit that caused it.
    """

    PIPELINE = pathlib.Path(__file__).resolve().parents[1].parent / "pipeline.yml"

    def _gate_job(self) -> dict:
        import yaml

        doc = yaml.safe_load(self.PIPELINE.read_text(encoding="utf-8"))
        return doc["jobs"]["ci-ok"]

    def test_the_require_list_is_exactly_the_needs_list(self):
        job = self._gate_job()
        needs = set(job["needs"])
        run = "".join(
            step.get("run", "") for step in job["steps"] if isinstance(step, dict)
        )
        marker = "--require "
        self.assertIn(marker, run)
        required = run.split(marker, 1)[1].split()[0]
        self.assertEqual(set(required.split(",")), needs)

    def test_every_required_job_id_is_a_job_that_exists(self):
        import yaml

        doc = yaml.safe_load(self.PIPELINE.read_text(encoding="utf-8"))
        for job_id in doc["jobs"]["ci-ok"]["needs"]:
            self.assertIn(job_id, doc["jobs"])


if __name__ == "__main__":
    unittest.main()
