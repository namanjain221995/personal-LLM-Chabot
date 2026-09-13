"""The rollback rehearsal must catch a broken INTERFACE, not a broken help text.

Each mutation below is one the 2026-09-12 CI step (`deploy-rollback.sh --help`
grepped for its flag names) passed, because none of them touches the header
comment `--help` prints. The rehearsal runs the real argument loop and the
real schema check in a sandbox, so each one is a failure here.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import rollback_rehearsal  # noqa: E402

REAL_SCRIPT = rollback_rehearsal.DEFAULT_SCRIPT


def _mutated(tmp: str, old: str, new: str) -> pathlib.Path:
    """A copy of the real script (and the lib it sources) with one edit."""
    root = pathlib.Path(tmp) / "scripts"
    (root / "lib").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT.parent / "lib" / "deploy-common.sh", root / "lib" / "deploy-common.sh")
    text = REAL_SCRIPT.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"the mutation anchor {old!r} is no longer unique in the script"
    target = root / "deploy-rollback.sh"
    target.write_text(text.replace(old, new), encoding="utf-8")
    return target


def _rehearse(script: pathlib.Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = rollback_rehearsal.main(["--script", str(script)])
    return rc, out.getvalue()


def _old_help_step_passes(script: pathlib.Path) -> bool:
    """The wave-1 pipeline step, reproduced: `--help`, then grep the flags."""
    proc = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, timeout=30)
    text = proc.stdout
    flags = ("--list", "--to", "--dry-run", "--yes", "--i-accept-schema-drift")
    return proc.returncode == 0 and all(f in text for f in flags) and "NEVER RESTORES A DATABASE" in text


class TheRealRollbackScriptRehearsesCleanly(unittest.TestCase):
    def test_every_scenario_passes_against_the_script_that_ships(self):
        rc, output = _rehearse(REAL_SCRIPT)
        self.assertEqual(rc, 0, output)
        self.assertIn("nothing was changed", output)


class ABrokenInterfaceIsCaughtWhereTheHelpCheckWasBlind(unittest.TestCase):
    def assertCaughtButHelpPassed(self, script: pathlib.Path, scenario: str) -> None:
        self.assertTrue(
            _old_help_step_passes(script),
            "precondition: the old --help step should still have passed this mutation",
        )
        rc, output = _rehearse(script)
        self.assertEqual(rc, 1, output)
        self.assertIn(f"FAIL {scenario}", output)

    def test_a_script_that_no_longer_parses_dry_run_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _mutated(tmp, "    --dry-run) DRY=1 ;;\n", "")
            self.assertCaughtButHelpPassed(script, "a compatible dry run by release stamp changes nothing")

    def test_a_dry_run_that_goes_on_to_act_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _mutated(tmp, 'if [ "$DRY" = 1 ]; then', "if false; then")
            self.assertCaughtButHelpPassed(script, "a compatible dry run by release stamp changes nothing")

    def test_a_schema_refusal_softened_into_a_warning_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _mutated(
                tmp,
                '    dr_die "refusing the rollback (no --i-accept-schema-drift). Nothing was changed."',
                '    dr_warn "refusing the rollback (no --i-accept-schema-drift). Nothing was changed."',
            )
            self.assertCaughtButHelpPassed(script, "a dry run across a schema boundary is refused")

    def test_a_script_that_silently_ignores_unknown_options_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _mutated(tmp, '    *) dr_die "unknown option: $1" ;;\n', "    *) ;;\n")
            self.assertCaughtButHelpPassed(script, "an unknown option is refused")


class TheSandboxRefusesToBeAWayIntoTheMachine(unittest.TestCase):
    def test_a_mutating_docker_verb_is_reported_even_when_the_script_ignores_its_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _mutated(
                tmp,
                'if [ "$DRY" = 1 ]; then\n',
                'docker tag "$TARGET_ORCH" sf-local-ai-orchestrator:latest || true\n'
                'if [ "$DRY" = 1 ]; then\n',
            )
            scenario = next(
                s for s in rollback_rehearsal.SCENARIOS if s.name.startswith("a compatible dry run")
            )
            problems = rollback_rehearsal.check(script, scenario)
        self.assertTrue(any("mutating docker call" in p for p in problems), problems)


class ThePolicyJobRunsTheRehearsal(unittest.TestCase):
    def test_the_pipeline_runs_the_rehearsal_rather_than_grepping_help(self):
        import yaml

        pipeline = pathlib.Path(__file__).resolve().parents[2] / "pipeline.yml"
        steps = yaml.safe_load(pipeline.read_text(encoding="utf-8"))["jobs"]["policy"]["steps"]
        runs = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("rollback_rehearsal.py", runs)
        self.assertNotIn('help="$(scripts/deploy-rollback.sh --help)"', runs)


if __name__ == "__main__":
    unittest.main()
