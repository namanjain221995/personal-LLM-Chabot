"""The automatic rollback must refuse what it cannot prove.

scripts/deploy.sh's rollback used to consult `schema_gate "$PREVIOUS"`, and
that function returns 0 without checking anything when the live schema version
cannot be read:

    live="$(dr_live_schema_version 2>/dev/null || true)"
    if [ -z "$live" ]; then
      say "  schema: proceeding WITHOUT the compatibility check ..."
      return 0
    fi

The rollback is reached only when the health gate failed or `techsara up`
failed. At that moment /health is down and the compose project may be
mid-recreate, so BOTH readings behind dr_live_schema_version - the /health
read and the `docker exec <pg> psql` fallback - can fail together. The gate
returned 0, `apply "$PREVIOUS"` ran, and older code started on a newer
database: exactly the outcome the comment above the gate says it exists to
prevent.

The decision now lives in three pure shell functions in
scripts/lib/deploy-common.sh so it can be driven from here with fixture
numbers instead of a broken production box. `live schema version unreadable at
rollback time -> REFUSE` is the case this whole file exists for.
"""
from __future__ import annotations

import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[4]
LIB = REPO / "scripts" / "lib" / "deploy-common.sh"
DEPLOY_SH = REPO / "scripts" / "deploy.sh"
RECORD_SH = REPO / "scripts" / "deploy-record.sh"

#: The numbers of the release observed on 2026-09-22: the database had applied
#: V40, the commit being deployed knew V41, and the commit it would have rolled
#: back to knew V40.
OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE = "40", "41", "40"


def call(function: str, *args: str) -> tuple[int, str, str]:
    """Run one helper out of the real deploy-common.sh, with fixture inputs."""
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; shift; ' + function + ' "$@"', "bash", str(LIB), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def verdict(previous: str, target: str, live: str) -> str:
    rc, out, err = call("dr_reversibility_verdict", previous, target, live)
    assert rc == 0, f"verdict({previous},{target},{live}) failed: {err or out}"
    return out


class TheLibrarySourcesCleanly(unittest.TestCase):
    """A helper nobody can source is a helper nobody can test."""

    def test_sourcing_it_succeeds_and_says_nothing(self):
        proc = subprocess.run(
            ["bash", "-c", '. "$1"', "bash", str(LIB)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "sourcing printed to stdout")
        self.assertEqual(proc.stderr, "", "sourcing printed to stderr")

    def test_all_three_helpers_are_defined(self):
        proc = subprocess.run(
            ["bash", "-c",
             '. "$1"; type -t dr_reversibility_verdict dr_reversibility_sentence dr_rollback_is_reversible',
             "bash", str(LIB)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["function", "function", "function"])


class TheForwardVerdict(unittest.TestCase):
    """Computed before anything is touched, while /health still answers."""

    def test_previous_equals_target_is_reversible(self):
        line = verdict("41", "41", "41")
        self.assertIn("verdict=reversible", line)
        self.assertIn("previous=41", line)
        self.assertIn("after=41", line)

    def test_previous_below_target_is_forward_only_and_names_the_range(self):
        line = verdict(OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE)
        self.assertIn("verdict=forward-only", line)
        self.assertIn("range=V41..V41", line)

    def test_a_multi_migration_release_names_the_whole_range(self):
        line = verdict("38", "41", "38")
        self.assertIn("verdict=forward-only", line)
        self.assertIn("range=V39..V41", line)

    def test_the_database_ahead_of_the_target_still_decides_the_range(self):
        # The database is at V41 and the target only knows V40, so after this
        # deploy the database is still V41 - the target's number does not
        # lower it, because migrations do not come back.
        line = verdict("39", "40", "41")
        self.assertIn("after=41", line)
        self.assertIn("range=V40..V41", line)

    def test_newer_previous_than_target_is_reversible(self):
        # Rolling back to code that knows MORE than the database is safe: it
        # applies nothing and starts.
        line = verdict("41", "40", "41")
        self.assertIn("verdict=reversible", line)

    def test_a_missing_input_yields_no_verdict_at_all(self):
        for previous, target, live in (("", "41", "40"), ("40", "", "40"), ("40", "41", "")):
            with self.subTest(previous=previous, target=target, live=live):
                rc, out, _ = call("dr_reversibility_verdict", previous, target, live)
                self.assertEqual(rc, 1)
                self.assertEqual(out, "", "a verdict was printed from incomplete inputs")

    def test_a_non_numeric_input_yields_no_verdict(self):
        rc, out, _ = call("dr_reversibility_verdict", "V40", "41", "40")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")


class TheSentenceStatesTheConsequence(unittest.TestCase):
    """The log has to say what the verdict MEANS, not that a gate ran."""

    def test_the_observed_release_reads_cannot(self):
        rc, out, _ = call(
            "dr_reversibility_sentence", verdict(OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            "V41 > V40, so a failed health gate CANNOT be rolled back automatically - "
            "the database will have applied V41 and V40 code cannot start on it.",
        )

    def test_a_reversible_release_reads_can(self):
        rc, out, _ = call("dr_reversibility_sentence", verdict("41", "41", "41"))
        self.assertEqual(rc, 0)
        self.assertIn("CAN be rolled back automatically", out)

    def test_no_verdict_still_produces_a_sentence_that_says_refuse(self):
        rc, out, _ = call("dr_reversibility_sentence", "")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSE", out)


class TheRollbackGateRefusesWhatItCannotProve(unittest.TestCase):
    """Every branch that is not a proof of safety is a refusal."""

    def test_the_live_version_being_unreadable_at_rollback_time_refuses(self):
        # THE case. The recorded verdict says this rollback is reversible, and
        # the rollback still refuses, because the one reading that could
        # confirm it cannot be taken. The old code returned 0 here.
        rc, out, _ = call("dr_rollback_is_reversible", verdict("41", "41", "41"), "")
        self.assertEqual(rc, 1, f"the gate PASSED on an unreadable live version: {out}")
        self.assertIn("refuse", out)
        self.assertIn("reason=live-schema-unreadable", out)

    def test_an_unreadable_live_version_refuses_even_for_a_reversible_release(self):
        for previous, target, live in (("41", "41", "41"), ("45", "41", "40")):
            with self.subTest(previous=previous):
                rc, out, _ = call("dr_rollback_is_reversible", verdict(previous, target, live), "")
                self.assertEqual(rc, 1, out)
                self.assertIn("reason=live-schema-unreadable", out)

    def test_a_missing_recorded_verdict_refuses(self):
        rc, out, _ = call("dr_rollback_is_reversible", "", "41")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=no-recorded-verdict", out)

    def test_a_blank_recorded_verdict_refuses(self):
        rc, out, _ = call("dr_rollback_is_reversible", "   \t ", "41")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=no-recorded-verdict", out)

    def test_an_unparseable_recorded_verdict_refuses(self):
        for line in ("garbage", "verdict=maybe previous=40", "verdict=reversible previous=four",
                     "previous=40", "verdict=reversible"):
            with self.subTest(line=line):
                rc, out, _ = call("dr_rollback_is_reversible", line, "41")
                self.assertEqual(rc, 1, out)
                self.assertIn("reason=unparseable-verdict", out)

    def test_a_forward_only_release_whose_migrations_ran_refuses_and_names_the_range(self):
        # The health gate failed: `techsara up` succeeded, the orchestrator
        # started, init_schema applied V41. The database is past PREVIOUS.
        line = verdict(OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE)
        rc, out, _ = call("dr_rollback_is_reversible", line, "41")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=forward-only", out)
        self.assertIn("range=V41..V41", out)

    def test_a_forward_only_release_whose_migrations_never_ran_proceeds(self):
        # `techsara up` FAILED - a build error, a container that would not
        # start - so nothing migrated anything and the database is still where
        # PREVIOUS left it. Rolling back is safe and is the right thing to do.
        # Refusing here on the verdict alone would make the automatic rollback
        # useless on most releases: db.py went V13 -> V41 in 22 days, so
        # nearly every release carries a forward-only verdict.
        line = verdict(OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE)
        rc, out, _ = call("dr_rollback_is_reversible", line, "40")
        self.assertEqual(rc, 0, out)
        self.assertIn("proceed", out)
        self.assertIn("the-targets-migrations-did-not-apply", out)

    def test_a_forward_only_release_still_refuses_when_the_live_version_is_unreadable(self):
        # The previous test is the ONLY reason forward-only can proceed, and
        # it depends entirely on a good live reading. Without one, refuse.
        line = verdict(OBSERVED_PREVIOUS, OBSERVED_TARGET, OBSERVED_LIVE)
        rc, out, _ = call("dr_rollback_is_reversible", line, "")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=live-schema-unreadable", out)

    def test_a_partially_applied_migration_range_refuses(self):
        # previous knows V40, the target knows V43, and V42 is what actually
        # landed before the deploy fell over. Still past PREVIOUS.
        rc, out, _ = call("dr_rollback_is_reversible", verdict("40", "43", "40"), "42")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=forward-only", out)

    def test_a_database_that_moved_past_previous_refuses_even_on_a_reversible_verdict(self):
        # The verdict was taken before the deploy ran. If something applied
        # more migrations since, the fresh reading wins.
        rc, out, _ = call("dr_rollback_is_reversible", verdict("41", "41", "41"), "42")
        self.assertEqual(rc, 1, out)
        self.assertIn("reason=database-moved-past-previous", out)

    def test_a_reversible_verdict_the_live_reading_agrees_with_proceeds(self):
        rc, out, _ = call("dr_rollback_is_reversible", verdict("41", "41", "41"), "41")
        self.assertEqual(rc, 0, out)
        self.assertIn("proceed", out)
        self.assertIn("previous=41", out)

    def test_the_rollbacks_own_direction_is_computable_and_reversible(self):
        # The verdict the ROLLBACK files with its own release record. The
        # transition is TARGET -> PREVIOUS, so the roles swap: what is running
        # now is TARGET (knows V41), what is being started is PREVIOUS (knows
        # V40), and the database is still V40 because `techsara up` never got
        # far enough to migrate. Undoing this rollback is safe, and that is
        # what the record should say - not the forward release's
        # "forward-only", which is a fact about a different transition.
        line = verdict("41", "40", "40")
        self.assertIn("verdict=reversible", line)
        self.assertIn("previous=41", line)
        self.assertIn("after=40", line)

    def test_previous_ahead_of_the_live_database_proceeds(self):
        rc, out, _ = call("dr_rollback_is_reversible", verdict("42", "41", "41"), "41")
        self.assertEqual(rc, 0, out)
        self.assertIn("proceed", out)


class TheDeployScriptActuallyUsesIt(unittest.TestCase):
    """A gate the caller does not call is a comment.

    These are structural assertions on scripts/deploy.sh, not behaviour: the
    behaviour needs a failed production deploy to exercise, which is not a
    thing to arrange. What they protect is the wiring - that the fail-open
    call site does not come back, and that the verdict reaches the release
    record so a later process can read it.
    """

    def setUp(self):
        self.deploy = DEPLOY_SH.read_text(encoding="utf-8")
        self.record = RECORD_SH.read_text(encoding="utf-8")
        # The "this used to happen" comments in these files quote the code they
        # replaced, so an absence assertion has to look at CODE. Whole-line
        # comments are dropped; nothing asserted below appears after code on a
        # line.
        self.deploy_code = "\n".join(
            line for line in self.deploy.splitlines() if not line.lstrip().startswith("#")
        )

    def test_the_rollback_no_longer_consults_the_fail_open_schema_gate(self):
        self.assertNotIn(
            'schema_gate "$PREVIOUS"',
            self.deploy_code,
            "the rollback is consulting schema_gate again, which returns 0 when the "
            "live schema version cannot be read - the exact fail-open this track removed",
        )

    def test_the_rollback_consults_the_recorded_verdict(self):
        self.assertIn("dr_rollback_is_reversible", self.deploy)
        self.assertIn("$RECORD_DIR/reversibility", self.deploy)

    def test_the_verdict_is_computed_before_apply_runs(self):
        computed = self.deploy.index("compute_reversibility\n")
        applied = self.deploy.index('if apply "$TARGET"; then')
        self.assertLess(
            computed, applied,
            "the verdict must be taken while the stack is still healthy, before apply()",
        )

    def test_the_forward_schema_gate_keeps_its_own_behaviour(self):
        # Deliberately NOT widened by this track: the forward path runs with a
        # human watching the run. Only the rollback was made fail-closed.
        self.assertIn('schema_gate "$TARGET" "the commit being deployed"', self.deploy_code)
        self.assertIn("proceeding WITHOUT the compatibility check", self.deploy)

    def test_the_verdict_is_persisted_into_the_release_record(self):
        self.assertIn("--reversibility", self.deploy)
        self.assertIn("--reversibility) REVERSIBILITY=", self.record)
        self.assertIn('"$DIR/reversibility"', self.record)

    def test_the_deploy_publishes_the_manifest_and_record_paths(self):
        # verify runs deploy-smoke.sh against these, and a glob would pick the
        # wrong release directory when a hand-run deploy is concurrent.
        self.assertIn("DEPLOY_RESULT_FILE", self.deploy)
        for key in ("manifest=%s", "record=%s", "reversibility=%s"):
            self.assertIn(key, self.deploy)

    def test_the_drain_loop_no_longer_swallows_its_outcome(self):
        self.assertNotIn(
            '--deadline "${DEPLOY_DRAIN_DEADLINE:-90}" --quiet-for 5 >>"$LOG" 2>&1 || true',
            self.deploy_code,
            "the drain result is being swallowed by `|| true` again",
        )
        self.assertIn("could not drain $svc", self.deploy)

    def test_the_rollback_files_its_own_verdict_and_not_the_forward_one(self):
        # apply() is called TWICE - once for $TARGET and once for $PREVIOUS -
        # and each call writes --reversibility into the release record it takes
        # first. Passing $REVERSIBILITY_VERDICT both times files a verdict
        # about PREVIOUS -> TARGET under a record whose `git.head` is $TARGET,
        # which is a plausible-looking wrong fact in the one file a 3 a.m.
        # reader trusts. apply() reads $APPLY_VERDICT, and the rollback path
        # re-points it for its own direction before calling apply().
        self.assertNotIn(
            'record_args+=(--reversibility "$REVERSIBILITY_VERDICT")',
            self.deploy_code,
            "the rollback's release record is being filed with the FORWARD verdict",
        )
        self.assertIn('record_args+=(--reversibility "$APPLY_VERDICT")', self.deploy_code)
        repointed = self.deploy_code.index('APPLY_VERDICT="$(dr_reversibility_verdict')
        rolled_back = self.deploy_code.index('if apply "$PREVIOUS" && health; then')
        self.assertLess(
            repointed, rolled_back,
            "the rollback's verdict must be computed before apply() writes the record",
        )

    def test_the_sync_worker_is_no_longer_drained_as_a_listener(self):
        self.assertNotIn("for svc in orchestrator frontend sync-worker; do", self.deploy_code)
        self.assertIn("sync-worker publishes no port", self.deploy)


if __name__ == "__main__":
    unittest.main()
