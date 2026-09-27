"""One box state per test: what the readiness job would say, and why.

Each case is a state the production box has actually been in, or has to be
able to be in. The assertion is never just "it went red": it is the VERDICT
and the COMMAND the operator is handed, because a readiness failure that only
moves the 3 a.m. guessing an hour earlier has bought nothing.

Nothing here needs the DGX, docker, a database or a network. Every probe
reaches the world through one injected CommandRunner.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import box_probes  # noqa: E402
import box_readiness  # noqa: E402
from _box_fixtures import (  # noqa: E402
    BIND_EXPOSED,
    BIND_EXPOSED_TWO,
    BIND_EXPOSED_UNCOUNTABLE,
    CONTROLLER_READY,
    FakeRunner,
    Raises,
    make_box,
    make_env,
    timeout_expired,
)


class BoxCase(unittest.TestCase):
    def readiness(self, *, box=None, **overrides):
        """Run the CLI against a fake box; return (exit code, output)."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = make_box(self._tmp.name, **(box or {}))
        runner = FakeRunner(**overrides)
        out = io.StringIO()
        code = box_readiness.run(make_env(root, runner), out=out)
        self.root = root
        self.runner = runner
        return code, out.getvalue()

    def assertRefused(self, code, output, probe, verdict):
        self.assertEqual(code, box_readiness.EXIT_REFUSED, output)
        self.assertIn("VERDICT: NOT READY", output)
        self.assertRegex(output, rf"(?m)^{probe}\s+{verdict}\b")
        self.assertIn(f"  {probe}: {verdict}", output)

    def assertReportedButNotRefused(self, code, output, probe, verdict):
        """The verdict is in the table and the job is still green.

        For the two verdicts where scripts/deploy.sh itself proceeds. A refusal
        here would be this job blocking a deploy the release path allows -- and
        in both cases the deploy being blocked is what clears the state.
        """
        self.assertEqual(code, box_readiness.EXIT_OK, output)
        self.assertIn("VERDICT: READY  (7 of 7 probes passed", output)
        self.assertRegex(output, rf"(?m)^{probe}\s+{verdict}\b")
        self.assertNotIn("VERDICT: NOT READY", output)
        self.assertNotIn(f"  {probe}: {verdict}", output)


class AHealthyBoxPasses(BoxCase):
    def test_every_probe_passes_and_the_job_would_go_green(self):
        code, output = self.readiness()
        self.assertEqual(code, box_readiness.EXIT_OK, output)
        self.assertIn("VERDICT: READY  (7 of 7 probes passed", output)
        for probe in ("deploy-root", "disk", "deploy-lock", "engine-exposure",
                      "engine-controller", "completion", "migrations"):
            self.assertRegex(output, rf"(?m)^{probe}\s+\S+")

    def test_the_lock_is_taken_only_for_an_instant_and_never_waited_for(self):
        """What is actually true: `flock -n` on a read-only descriptor inside a
        `bash -c` that does nothing else, so the lock is held for microseconds,
        is never waited for, and is never taken in order to act."""
        _code, _output = self.readiness()
        joined = [" ".join(call) for call in self.runner.calls]
        self.assertTrue(any("flock -n 9" in c for c in joined), joined)
        # Read-only descriptor, and no blocking wait anywhere.
        self.assertTrue(any('exec 9<"$1"' in c for c in joined), joined)
        self.assertFalse(any("flock -w" in c for c in joined), joined)
        self.assertFalse(any("deploy-lock.sh --wait" in c for c in joined), joined)

    def test_it_asks_the_engine_for_a_completion_and_not_for_health(self):
        _code, _output = self.readiness()
        targets = [call[-1] for call in self.runner.calls if call[0] == "curl"]
        self.assertTrue(any(t.endswith("/v1/chat/completions") for t in targets), targets)
        self.assertEqual([t for t in targets if t.endswith("/health")], [])


class TheSharedDeployRoot(BoxCase):
    def test_a_dirty_tree_refuses_and_names_the_status_command(self):
        code, output = self.readiness(
            **{"git-status": box_probes.Completed(0, " M orchestrator/app/chat.py\n M frontend/x.tsx\n")}
        )
        self.assertRefused(code, output, "deploy-root", "dirty-tree")
        self.assertIn("dirty_files 2", output)
        self.assertIn(f"git -C {self.root} status --porcelain --untracked-files=no", output)
        self.assertIn("SHARED working tree", output)
        # The file names themselves are a private tree's paths; the command is
        # what shows them, not this log.
        self.assertNotIn("orchestrator/app/chat.py", output)

    def test_every_git_call_against_the_shared_root_refuses_to_take_its_index_lock(self):
        """`git status` refreshes the index unless told not to, and the deploy
        root is a working tree several sessions write to. Measured on git 2.43.0
        on 2026-09-27: a plain `git status --porcelain --untracked-files=no`
        against a stale index opens `.git/index.lock` with O_CREAT|O_EXCL and
        rewrites `.git/index`; with `--no-optional-locks` it opens no lock and
        the index mtime does not move. box_probes.py and box_readiness.py both
        claim this library does not write, so the flag is the claim's guard."""
        self.readiness()
        git_calls = [c for c in self.runner.calls if c[0] == "git"]
        self.assertEqual(len(git_calls), 3, git_calls)
        for call in git_calls:
            self.assertIn("--no-optional-locks", call, call)
            # Before the path, so it is a git option and not a subcommand's.
            self.assertLess(call.index("--no-optional-locks"), call.index("-C"), call)

    def test_a_branch_that_is_not_the_default_is_reported_and_does_not_block(self):
        """`wrong-branch` used to refuse, and that was a SELF-LOCKOUT.

        Nothing in the release path requires the deploy root to be on a branch:
        deploy.sh's `land()` detaches on purpose when DEPLOY_BRANCH is unset, and
        the workflow's "the box is serving the commit we asked for" step compares
        `git rev-parse HEAD`, which reads the same detached. Meanwhile
        deploy.sh's `detach_to` (deploy.sh:290) is reached from seven call sites
        on an ORDINARY push-to-main deploy -- one of them "checked out in another
        worktree", which deploy.sh's own comment calls "a real configuration on
        this box". So a refusal blocked `deploy` on a state the release path
        creates itself, and `land_on_branch` recovers from it on the very deploy
        that was being blocked."""
        branch = "feat-some-private-branch-name"
        code, output = self.readiness(**{"git-branch": box_probes.Completed(0, branch + "\n")})
        self.assertReportedButNotRefused(code, output, "deploy-root", "wrong-branch")
        self.assertIn("on_default_branch no", output)
        # The branch NAME is still not printed anywhere - only the flag is.
        self.assertNotIn(branch, output)

    def test_a_detached_production_checkout_does_not_lock_the_release_path_out(self):
        """The exact state `workflow_dispatch` with `branch: '-'` leaves behind:
        pipeline.yml sets DEPLOY_BRANCH="" for '-', deploy.sh detaches, and
        `git rev-parse --abbrev-ref HEAD` then prints `HEAD` (measured on git
        2.43.0 on 2026-09-27). A refusal here blocked every subsequent push AND
        dispatch until a human ran `git checkout main` in the shared checkout."""
        code, output = self.readiness(**{"git-branch": box_probes.Completed(0, "HEAD\n")})
        self.assertReportedButNotRefused(code, output, "deploy-root", "wrong-branch")
        self.assertIn("on_default_branch no", output)

    def test_a_dirty_tree_is_still_a_refusal_even_on_the_default_branch(self):
        """Widening `wrong-branch` must not widen `dirty-tree`: that one has
        deploy.sh:189 behind it, which aborts on any porcelain output."""
        code, output = self.readiness(
            **{"git-status": box_probes.Completed(0, " M x.py\n"),
               "git-branch": box_probes.Completed(0, "main\n")}
        )
        self.assertRefused(code, output, "deploy-root", "dirty-tree")

    def test_a_deploy_root_that_is_not_a_checkout_refuses(self):
        code, output = self.readiness(**{"git-inside": box_probes.Completed(128, "", "fatal")})
        self.assertRefused(code, output, "deploy-root", "not-a-checkout")

    def test_git_failing_outright_refuses_rather_than_passing(self):
        code, output = self.readiness(**{"git-status": box_probes.Completed(129, "")})
        self.assertRefused(code, output, "deploy-root", "git-unreadable")


class TheDiskFloorIsTheDeploysOwnNumber(BoxCase):
    def test_twenty_one_gigabytes_passes(self):
        code, output = self.readiness(**{"df": box_probes.Completed(0, "Avail\n21G\n")})
        self.assertEqual(code, box_readiness.EXIT_OK, output)
        self.assertIn("free_gb 21 | floor_gb 20", output)

    def test_nineteen_gigabytes_refuses_and_names_the_df_command(self):
        code, output = self.readiness(**{"df": box_probes.Completed(0, "Avail\n19G\n")})
        self.assertRefused(code, output, "disk", "below-floor")
        self.assertIn("free_gb 19 | floor_gb 20", output)
        self.assertIn(f"df -BG --output=avail {self.root}", output)
        self.assertIn("previous release's image ids", output)

    def test_exactly_the_floor_passes(self):
        code, output = self.readiness(**{"df": box_probes.Completed(0, "Avail\n20G\n")})
        self.assertEqual(code, box_readiness.EXIT_OK, output)

    def test_the_floor_is_the_one_the_deploy_preflight_uses(self):
        self.assertEqual(box_probes.DISK_FLOOR_GB, 20)

    def test_unreadable_disk_refuses(self):
        code, output = self.readiness(**{"df": box_probes.Completed(1, "")})
        self.assertRefused(code, output, "disk", "unreadable")


class TheDeployLockIsAskedWithANonBlockingFlock(BoxCase):
    """Named for what is asserted. The class used to be called
    `TheDeployLockIsObservedNeverTaken`, which restated the claim the commit
    itself withdrew -- `flock -n` DOES take the lock -- to every reader of a
    test run."""

    def test_a_held_lock_refuses_and_names_the_holder(self):
        code, output = self.readiness(**{"flock": box_probes.Completed(1, "")})
        self.assertRefused(code, output, "deploy-lock", "held")
        self.assertIn("holder_pid 40321", output)
        self.assertIn("holder_origin github-actions", output)
        self.assertIn("holder_head bd532e383e5b", output)
        self.assertIn(f"{self.root}/scripts/deploy-lock.sh --status", output)
        # The holder's HOSTNAME and actor are in the file and stay there.
        self.assertNotIn("spark-0e68", output)
        self.assertNotIn("some-operator", output)

    def test_a_lock_file_that_was_never_created_is_the_first_deploy_and_passes(self):
        code, output = self.readiness(box={"lock_file": False})
        self.assertEqual(code, box_readiness.EXIT_OK, output)
        self.assertRegex(output, r"(?m)^deploy-lock\s+never-created")

    def test_a_lock_that_cannot_even_be_opened_refuses(self):
        code, output = self.readiness(**{"flock": box_probes.Completed(3, "")})
        self.assertRefused(code, output, "deploy-lock", "unreadable")


class TheEngineExposureAssertion(BoxCase):
    def test_an_accepted_non_cluster_address_refuses_and_asks_for_root(self):
        """The fixture carries engine_bind's real `- FAIL:` line, which is what
        made this number wrong: the bare phrase "ACCEPTED the connection" occurs
        twice per exposed run, so ONE accepting address used to be reported as
        `accepted_addresses 2`. Measured on 2026-09-27 against the real
        `engine_bind.evaluate` (see test_box_probes.TheAcceptedCountMatchesTheRealReport)."""
        code, output = self.readiness(**{"bind-check": box_probes.Completed(1, BIND_EXPOSED)})
        self.assertRefused(code, output, "engine-exposure", "exposed")
        self.assertIn("accepted_addresses 1", output)
        self.assertNotIn("accepted_addresses 2", output)
        self.assertIn("NEEDS ROOT", output)
        self.assertIn("sudo systemctl restart techsara-host-guard.service", output)

    def test_two_accepted_addresses_are_counted_as_two(self):
        code, output = self.readiness(**{"bind-check": box_probes.Completed(1, BIND_EXPOSED_TWO)})
        self.assertRefused(code, output, "engine-exposure", "exposed")
        self.assertIn("accepted_addresses 2", output)
        self.assertNotIn("accepted_addresses 3", output)

    def test_an_acceptance_the_probe_cannot_count_still_refuses_and_invents_no_number(self):
        """The trip-wire. Narrowing the pattern to the per-address line is what
        fixed the count; if engine_bind ever reports an acceptance in some other
        shape, this gate must not answer `closed` - and it must not guess a
        count either."""
        code, output = self.readiness(
            **{"bind-check": box_probes.Completed(1, BIND_EXPOSED_UNCOUNTABLE)}
        )
        self.assertRefused(code, output, "engine-exposure", "exposed")
        self.assertNotIn("accepted_addresses", output)
        self.assertIn("ACCEPTED the connection", output)

    def test_an_accepted_line_refuses_even_when_the_exit_code_says_success(self):
        # Fail closed on the stronger signal. If these two ever disagree, the
        # one that names an address that answered is the one to believe.
        code, output = self.readiness(**{"bind-check": box_probes.Completed(0, BIND_EXPOSED)})
        self.assertRefused(code, output, "engine-exposure", "exposed")

    def test_a_check_that_could_not_prove_anything_refuses(self):
        code, output = self.readiness(
            **{"bind-check": box_probes.Completed(1, "FATAL: ssh to the worker failed\n")}
        )
        self.assertRefused(code, output, "engine-exposure", "unproven")


class TheEngineController(BoxCase):
    def test_a_controller_that_does_not_answer_refuses(self):
        code, output = self.readiness(**{"controller": box_probes.Completed(7, "")})
        self.assertRefused(code, output, "engine-controller", "unreachable")
        self.assertIn("sf-local-ai-engine-controller-1", output)

    def test_a_reply_that_is_not_json_refuses(self):
        code, output = self.readiness(**{"controller": box_probes.Completed(0, "<html>502</html>")})
        self.assertRefused(code, output, "engine-controller", "unreadable")

    def test_a_recovery_in_flight_refuses(self):
        doc = CONTROLLER_READY.replace('"in_progress": false', '"in_progress": true')
        code, output = self.readiness(**{"controller": box_probes.Completed(0, doc)})
        self.assertRefused(code, output, "engine-controller", "recovering")
        self.assertIn("recovery_in_progress yes", output)

    def test_a_recovery_field_that_is_not_an_object_refuses_rather_than_reading_as_idle(self):
        """The reading that let a box mid-recovery pass. `recovery` was read as
        `doc.get("recovery") or {}` and `in_progress` only when it happened to
        be a dict, so a `recovery` of any other shape silently meant "nothing
        is recovering" and a READY/BUSY state_code then carried the probe. A
        document this probe cannot read is a probe that cannot be performed."""
        for malformed in ('[]', '"starting"', '3'):
            with self.subTest(recovery=malformed):
                code, output = self.readiness(
                    **{
                        "controller": box_probes.Completed(
                            0,
                            '{"state": "READY", "state_code": 2, '
                            '"primary_ready": true, "recovery": ' + malformed + "}",
                        )
                    }
                )
                self.assertRefused(code, output, "engine-controller", "unreadable")

    def test_a_state_that_is_not_serving_refuses(self):
        doc = CONTROLLER_READY.replace('"state_code": 2', '"state_code": 5').replace(
            '"state": "READY"', '"state": "DEGRADED"'
        )
        code, output = self.readiness(**{"controller": box_probes.Completed(0, doc)})
        self.assertRefused(code, output, "engine-controller", "not-ready")
        self.assertIn("engine_state DEGRADED", output)


class TheWedgedEngine(BoxCase):
    """The single most important case in this file.

    A wedged vLLM engine kept a green /health for five and a half hours on this
    box. It answers, it returns text, and `vllm:generation_tokens_total` does
    not move. A probe that stopped at "the reply was not empty" would have
    passed every minute of those five and a half hours.
    """

    def test_text_came_back_but_the_counter_did_not_advance_so_it_refuses(self):
        code, output = self.readiness(metrics_counts=[3243672, 3243672])
        self.assertRefused(code, output, "completion", "wedged")
        self.assertIn("tokens_before 3243672 | tokens_after 3243672", output)
        self.assertIn("reply_chars 6", output)
        self.assertIn(f"{self.root}/scripts/cluster-verify-engine.sh", output)
        self.assertIn("never as part of a routine deploy", output)

    def test_the_counter_advancing_by_one_token_is_enough(self):
        code, output = self.readiness(metrics_counts=[3243672, 3243673])
        self.assertEqual(code, box_readiness.EXIT_OK, output)

    def test_an_empty_completion_refuses(self):
        code, output = self.readiness(
            **{"completion": box_probes.Completed(0, '{"choices": [{"message": {"content": "  "}}]}')}
        )
        self.assertRefused(code, output, "completion", "empty-reply")

    def test_an_engine_that_does_not_answer_at_all_refuses(self):
        code, output = self.readiness(**{"completion": box_probes.Completed(7, "")})
        self.assertRefused(code, output, "completion", "unreachable")

    def test_metrics_that_cannot_be_read_refuse_rather_than_pass(self):
        code, output = self.readiness(**{"metrics": box_probes.Completed(0, "# nothing here\n")})
        self.assertRefused(code, output, "completion", "metrics-unreadable")

    def test_a_counter_that_is_not_a_finite_number_refuses(self):
        """The Prometheus text format permits NaN, +Inf and -Inf, and `float()`
        accepts all three. Every comparison against NaN is false, so a NaN
        total would make the wedged test (`after <= before`) false whatever the
        engine did, and the probe would report `generated` on a box that
        generated nothing."""
        for sample in ("NaN", "+Inf", "-Inf"):
            with self.subTest(sample=sample):
                body = (
                    "# TYPE vllm:generation_tokens_total counter\n"
                    'vllm:generation_tokens_total{model_name="m"} ' + sample + "\n"
                )
                code, output = self.readiness(**{"metrics": box_probes.Completed(0, body)})
                self.assertRefused(code, output, "completion", "metrics-unreadable")

    def test_a_deploy_root_with_no_main_model_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            (root / ".runtime" / "generated.env").write_text("VLLM_PORT=8000\n", encoding="utf-8")
            out = io.StringIO()
            code = box_readiness.run(make_env(root, FakeRunner()), out=out)
        self.assertEqual(code, box_readiness.EXIT_REFUSED)
        self.assertIn("model-unknown", out.getvalue())
        self.assertIn("./techsara redetect", out.getvalue())


class TheMigrationBoundary(BoxCase):
    def test_a_commit_behind_the_database_refuses(self):
        code, output = self.readiness(
            **{"schema-live": box_probes.Completed(0, "41\n"),
               "schema-code": box_probes.Completed(0, "39\n")}
        )
        self.assertRefused(code, output, "migrations", "behind")
        self.assertIn("live_schema 41 | code_schema 39", output)
        self.assertIn("old code in front of a newer schema", output)

    def test_forward_migrations_pass_and_are_counted(self):
        code, output = self.readiness(**{"schema-code": box_probes.Completed(0, "43\n")})
        self.assertEqual(code, box_readiness.EXIT_OK, output)
        self.assertIn("forward_n 2", output)

    def test_a_live_schema_that_cannot_be_read_is_reported_and_does_not_block(self):
        """scripts/deploy.sh:244-252 PROCEEDS in exactly this case -- "orchestrator
        down and psql unavailable ... proceeding WITHOUT the compatibility check -
        this is the one case where the check cannot be made, and it is worth
        knowing it was skipped". Refusing here meant a stack that is DOWN could
        no longer be deployed to through CI, and a deploy is what brings it back.
        The reading that WAS taken still prints, which is deploy.sh's "proceed
        and say so"."""
        code, output = self.readiness(**{"schema-live": box_probes.Completed(1, "")})
        self.assertReportedButNotRefused(code, output, "migrations", "unreadable")
        self.assertIn("code_schema 41", output)
        self.assertNotIn("live_schema", output)

    def test_a_code_schema_that_cannot_be_read_is_reported_and_does_not_block(self):
        """deploy.sh:256-257 skips the check for the same reason on this side."""
        code, output = self.readiness(**{"schema-code": box_probes.Completed(1, "")})
        self.assertReportedButNotRefused(code, output, "migrations", "unreadable")
        self.assertIn("live_schema 41", output)

    def test_behind_is_still_a_refusal_because_deploy_sh_refuses_it_too(self):
        """Widening `unreadable` must not widen `behind`. deploy.sh:263-271
        refuses a downgrade, and its ALLOW_SCHEMA_DOWNGRADE=1 override has never
        been reachable from CI on any branch - verified 2026-09-27,
        `git grep ALLOW_SCHEMA_DOWNGRADE origin/dev -- .github/` finds nothing,
        and the same on this branch - so nothing is lost by refusing here."""
        code, output = self.readiness(**{"schema-code": box_probes.Completed(0, "39\n")})
        self.assertRefused(code, output, "migrations", "behind")

    def test_the_live_and_code_versions_are_read_from_different_trees(self):
        """The live number belongs to the deploy root; the code number belongs
        to the commit being deployed, which is checked out in the runner
        workspace and is not necessarily in the deploy root's object store."""
        self.readiness()
        roots = {}
        for argv, env in zip(self.runner.calls, self.runner.envs):
            joined = " ".join(argv)
            if "dr_live_schema_version" in joined:
                roots["live"] = env.get("TECHSARA_DEPLOY_ROOT")
            elif "dr_code_schema_version_from_git" in joined:
                roots["code"] = env.get("TECHSARA_DEPLOY_ROOT")
        self.assertEqual(roots["live"], str(self.root))
        self.assertEqual(roots["code"], str(self.root.parent / "workspace"))
        self.assertNotEqual(roots["live"], roots["code"])


    def test_the_commit_reaches_bash_as_an_argument_and_never_as_script_text(self):
        """`dr_code_schema_version_from_git` used to be built by interpolating
        the ref into the `bash -c` script (`f'... "{env.ref}"'`). Today's ref is
        $GITHUB_SHA and is forty hex digits, so nothing was exploitable - but
        that is the shape in which a future caller's `--ref` becomes shell code,
        and a positional parameter costs nothing."""
        self.readiness()
        ref = "bd532e383e5bcafe0123456789abcdef01234567"
        calls = [c for c in self.runner.calls if "dr_code_schema_version_from_git" in " ".join(c)]
        self.assertEqual(len(calls), 1, self.runner.calls)
        argv = calls[0]
        script = argv[2]
        self.assertNotIn(ref, script, "the commit is script text, not data")
        self.assertIn(ref, argv[3:], f"the commit is not a positional argument: {argv!r}")
        self.assertIn('"$@"', script)


class AProbeThatCannotBePerformedIsARefusal(BoxCase):
    def test_a_probe_that_raises_refuses(self):
        code, output = self.readiness(**{"df": Raises(RuntimeError("the runner exploded"))})
        self.assertRefused(code, output, "disk", "probe-raised")
        self.assertIn("exception_type RuntimeError", output)
        self.assertIn("box_readiness.py --deploy-root", output)

    def test_a_probe_that_times_out_refuses(self):
        code, output = self.readiness(**{"bind-check": Raises(timeout_expired())})
        self.assertRefused(code, output, "engine-exposure", "probe-timed-out")

    def test_one_refusal_does_not_stop_the_others_from_reporting(self):
        code, output = self.readiness(
            **{"df": box_probes.Completed(0, "Avail\n19G\n"), "controller": box_probes.Completed(7, "")}
        )
        self.assertEqual(code, box_readiness.EXIT_REFUSED)
        self.assertIn("REFUSED by 2 of 7 probes. Nothing was changed.", output)
        self.assertRegex(output, r"(?m)^completion\s+generated")


class TheCommandLine(unittest.TestCase):
    def test_a_deploy_root_that_does_not_exist_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = box_readiness.main(["--deploy-root", "/nowhere/at/all", "--ref", "HEAD"])
        self.assertEqual(code, box_readiness.EXIT_USAGE)
        self.assertIn("not the runner workspace", err.getvalue())

    def test_the_summary_is_the_same_already_sanitized_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            summary = pathlib.Path(tmp) / "summary.md"
            out = io.StringIO()
            box_readiness.run(make_env(root, FakeRunner()), out=out, summary_path=str(summary))
            written = summary.read_text(encoding="utf-8")
        self.assertIn("### Box readiness: READY (7/7)", written)
        for line in out.getvalue().splitlines():
            if line.strip():
                self.assertIn(line, written)


if __name__ == "__main__":
    unittest.main()
