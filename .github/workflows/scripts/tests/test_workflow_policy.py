"""The workflow policy checks added on 2026-09-12, and the pipeline obeying them.

Extended 2026-09-13 with the residuals of the wave-1 CI review: a boolean
timeout (P7), a runs-on expression smuggling a self-hosted leg (P4, audit
N021), an inverted or OR-ed branch guard (P4, audit F066) and write scopes a
job only had to declare (P5, audit N022).

P1-P6 were already exercised by every run of the `policy` job against the real
pipeline. P7 (explicit timeouts) and P8 (plain-ASCII job names) are new, and a
check that has never been seen to fail is not a check — so each one is proved
here against a deliberately broken workflow before it is trusted against the
real one.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import workflow_policy  # noqa: E402


WORKFLOWS = pathlib.Path(__file__).resolve().parents[1].parent

MINIMAL = """
name: Example
on:
  push:
    branches: [main]
permissions: {}
jobs:
  build:
    name: "%(display)s"
    runs-on: ubuntu-latest
    permissions:
      contents: read
%(timeout)s    steps:
      - run: echo hello
"""


def _check(display: str = "Build", timeout: str = "    timeout-minutes: 5\n"):
    findings = workflow_policy.Findings()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "example.yml"
        path.write_text(MINIMAL % {"display": display, "timeout": timeout}, encoding="utf-8")
        workflow_policy.check_file(path, "main", findings)
    return findings


class EveryJobMustDeclareATimeout(unittest.TestCase):
    def test_a_job_with_no_timeout_minutes_is_a_finding(self):
        findings = _check(timeout="")
        self.assertFalse(findings.ok)
        self.assertTrue(any(row[0] == "P7 timeout" for row in findings.rows), findings.rows)

    def test_a_job_with_a_timeout_is_accepted(self):
        self.assertTrue(_check().ok)

    def test_a_timeout_that_is_not_a_positive_number_is_a_finding(self):
        findings = _check(timeout='    timeout-minutes: "soon"\n')
        self.assertTrue(any(row[0] == "P7 timeout" for row in findings.rows), findings.rows)

    def test_a_zero_timeout_is_a_finding_because_it_is_not_a_budget(self):
        findings = _check(timeout="    timeout-minutes: 0\n")
        self.assertTrue(any(row[0] == "P7 timeout" for row in findings.rows), findings.rows)


    def test_a_boolean_timeout_is_a_finding_although_python_calls_true_an_int(self):
        # 2026-09-13 CI review: `isinstance(True, int)` is True, so YAML's
        # `timeout-minutes: true` passed P7 as a one-minute budget.
        findings = _check(timeout="    timeout-minutes: true\n")
        self.assertTrue(any(row[0] == "P7 timeout" for row in findings.rows), findings.rows)


def _policy(text: str, filename: str = "example.yml"):
    """Run the whole policy over one workflow body and return the findings."""
    findings = workflow_policy.Findings()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / filename
        path.write_text(text, encoding="utf-8")
        workflow_policy.check_file(path, "main", findings)
    return findings


def _rows(findings, check: str):
    return [row for row in findings.rows if row[0] == check]


#: Audit N021's reproducer, verbatim in shape: no `if:`, a matrix whose second
#: leg is the production runner's label, and a runs-on that is an expression.
MATRIX_SMUGGLE = """
name: m
on: [pull_request, push]
permissions: {}
jobs:
  leak:
    runs-on: ${{ matrix.runner }}
    permissions:
      contents: read
    timeout-minutes: 5
%(if)s    strategy:
      matrix:
        runner: %(runners)s
    steps:
      - run: cat /home/techsphere/Documents/project/personal-LLM-Chabot/.env
"""


class ARunsOnExpressionCannotSmuggleASelfHostedLegPastP4(unittest.TestCase):
    def test_a_matrix_with_a_self_hosted_leg_and_no_guard_is_a_finding(self):
        findings = _policy(MATRIX_SMUGGLE % {"if": "", "runners": '[ubuntu-latest, "self-hosted"]'})
        rows = _rows(findings, "P4 self-hosted")
        self.assertTrue(rows, findings.rows)
        self.assertIn("expression", rows[0][2])

    def test_a_matrix_leg_that_is_a_label_list_containing_self_hosted_is_a_finding(self):
        findings = _policy(
            MATRIX_SMUGGLE % {"if": "", "runners": "[ubuntu-latest, [self-hosted, dgx-spark]]"}
        )
        self.assertTrue(_rows(findings, "P4 self-hosted"), findings.rows)

    def test_a_self_hosted_leg_added_through_include_is_a_finding(self):
        text = MATRIX_SMUGGLE % {"if": "", "runners": "[ubuntu-latest]"}
        text = text.replace(
            "        runner: [ubuntu-latest]\n",
            "        runner: [ubuntu-latest]\n        include:\n          - runner: self-hosted\n",
        )
        self.assertTrue(_rows(_policy(text), "P4 self-hosted"))

    def test_a_runner_taken_from_a_repository_variable_is_presumed_self_hosted(self):
        text = MATRIX_SMUGGLE % {"if": "", "runners": "[ubuntu-latest]"}
        text = text.replace("runs-on: ${{ matrix.runner }}", "runs-on: ${{ vars.RUNNER }}")
        self.assertTrue(_rows(_policy(text), "P4 self-hosted"))

    def test_a_matrix_that_is_itself_an_expression_is_presumed_self_hosted(self):
        text = MATRIX_SMUGGLE % {"if": "", "runners": "${{ fromJSON(vars.RUNNERS) }}"}
        self.assertTrue(_rows(_policy(text), "P4 self-hosted"))

    def test_a_matrix_of_hosted_runners_only_is_accepted(self):
        findings = _policy(MATRIX_SMUGGLE % {"if": "", "runners": "[ubuntu-latest, ubuntu-24.04-arm]"})
        self.assertFalse(_rows(findings, "P4 self-hosted"), findings.rows)

    def test_an_expression_runner_behind_the_main_branch_guard_is_accepted(self):
        findings = _policy(
            MATRIX_SMUGGLE
            % {
                "if": "    if: github.ref == 'refs/heads/main'\n",
                "runners": '[ubuntu-latest, "self-hosted"]',
            }
        )
        self.assertFalse(_rows(findings, "P4 self-hosted"), findings.rows)


SELF_HOSTED_GUARD = """
name: g
on:
  pull_request:
  push:
permissions: {}
jobs:
  deploy:
    if: %(cond)s
    runs-on: [self-hosted, dgx-spark]
    permissions:
      contents: read
    timeout-minutes: 5
    steps:
      - run: echo deploy
"""


class TheBranchGuardMustActuallyRestrictToMain(unittest.TestCase):
    def test_an_inverted_guard_is_a_finding(self):
        # Audit F066's evil.yml: "every branch except main" passed the
        # substring test because the text `refs/heads/main` was present.
        findings = _policy(SELF_HOSTED_GUARD % {"cond": "github.ref != 'refs/heads/main'"})
        self.assertTrue(_rows(findings, "P4 self-hosted"), findings.rows)

    def test_a_guard_that_is_only_one_side_of_an_or_is_a_finding(self):
        findings = _policy(
            SELF_HOSTED_GUARD
            % {"cond": "github.ref == 'refs/heads/main' || github.event_name == 'push'"}
        )
        self.assertTrue(_rows(findings, "P4 self-hosted"), findings.rows)

    def test_a_branch_whose_name_merely_starts_with_main_is_not_the_guard(self):
        findings = _policy(SELF_HOSTED_GUARD % {"cond": "github.ref == 'refs/heads/main-evil'"})
        self.assertTrue(_rows(findings, "P4 self-hosted"), findings.rows)

    def test_the_guard_anded_into_the_condition_is_accepted(self):
        findings = _policy(
            SELF_HOSTED_GUARD
            % {"cond": "github.event_name == 'push' && github.ref == 'refs/heads/main'"}
        )
        self.assertFalse(_rows(findings, "P4 self-hosted"), findings.rows)


PERMISSIONS = """
name: p
on:
  push:
permissions: {}
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
%(perms)s
    steps:
      - run: echo build
"""


class AJobMayNotGrantItselfWriteScopesItDoesNotNeed(unittest.TestCase):
    """Audit N022: P5 checked only that `permissions:` was DECLARED."""

    def test_a_contents_write_grant_is_a_finding(self):
        findings = _policy(PERMISSIONS % {"perms": "      contents: write"})
        rows = _rows(findings, "P5 permissions")
        self.assertTrue(rows, findings.rows)
        self.assertIn("contents: write", rows[0][2])

    def test_every_write_scope_is_named_not_just_the_first(self):
        findings = _policy(
            PERMISSIONS % {"perms": "      packages: write\n      id-token: write\n      contents: read"}
        )
        details = " ".join(row[2] for row in _rows(findings, "P5 permissions"))
        self.assertIn("packages: write", details)
        self.assertIn("id-token: write", details)

    def test_write_all_is_a_finding(self):
        text = PERMISSIONS.replace("    permissions:\n%(perms)s\n", "    permissions: write-all\n")
        self.assertTrue(_rows(_policy(text), "P5 permissions"))

    def test_a_scope_value_the_policy_cannot_read_is_a_finding(self):
        findings = _policy(PERMISSIONS % {"perms": "      contents: admin"})
        self.assertTrue(_rows(findings, "P5 permissions"), findings.rows)

    def test_a_write_scope_on_a_reusable_workflow_call_is_a_finding_too(self):
        text = """
name: r
on:
  push:
permissions: {}
jobs:
  call:
    uses: ./.github/workflows/other.yml
    permissions:
      contents: write
"""
        self.assertTrue(_rows(_policy(text), "P5 permissions"))

    def test_a_write_scope_listed_with_its_reason_is_accepted(self):
        key = ("example.yml", "build")
        workflow_policy.WRITE_SCOPES_NEEDED[key] = {"packages": "publishes the image to GHCR"}
        try:
            findings = _policy(PERMISSIONS % {"perms": "      contents: read\n      packages: write"})
        finally:
            del workflow_policy.WRITE_SCOPES_NEEDED[key]
        self.assertFalse(_rows(findings, "P5 permissions"), findings.rows)

    def test_read_only_scopes_are_accepted(self):
        findings = _policy(PERMISSIONS % {"perms": "      contents: read\n      actions: none"})
        self.assertFalse(_rows(findings, "P5 permissions"), findings.rows)


class JobDisplayNamesMustBePlainAscii(unittest.TestCase):
    def test_an_emoji_in_a_job_name_is_a_finding(self):
        findings = _check(display="\N{LOCK} policy")
        self.assertTrue(any(row[0] == "P8 job name" for row in findings.rows), findings.rows)

    def test_a_plain_name_is_accepted(self):
        self.assertTrue(_check(display="Policy validation").ok)

    def test_a_matrix_expression_in_a_name_is_not_mistaken_for_decoration(self):
        self.assertTrue(_check(display="Launcher tests (Python ${{ matrix.python }})").ok)


class TheRealPipelineObeysEveryCheck(unittest.TestCase):
    """The point of the exercise: run P1-P8 against the file that ships."""

    def test_the_repository_workflows_pass_the_whole_policy(self):
        findings = workflow_policy.Findings()
        files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        self.assertTrue(files, "no workflow files were found to check")
        for path in files:
            workflow_policy.check_file(path, "main", findings)
        self.assertTrue(
            findings.ok,
            "workflow policy findings:\n"
            + "\n".join(f"  [{c}] {w}: {d}" for c, w, d in findings.rows),
        )


if __name__ == "__main__":
    unittest.main()
