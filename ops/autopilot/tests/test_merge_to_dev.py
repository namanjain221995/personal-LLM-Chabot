"""Tests for ops/deploy/merge_to_dev.sh against a throwaway origin and a fake `gh`.

Run: python3 -m pytest ops/autopilot/tests -q -p no:cacheprovider
Nothing here talks to GitHub; every repository lives in a temporary directory.
The gate takes no settings from its environment, so each test runs a copy whose
configuration block points at the temporary repositories and the fake gh.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "deploy", "merge_to_dev.sh")
JQ = "/usr/bin/jq"

# Answers the gate's three API reads from scenario.json next to itself, through
# the real jq, so the gate's --jq programs are exercised as written.
FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/python3
    import json, os, subprocess, sys
    scenario = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenario.json")))
    args = sys.argv[1:]
    path = next((a for a in args if a.startswith("repos/")), "").split("?")[0]
    jq = args[args.index("--jq") + 1] if "--jq" in args else "."
    if scenario.get("fail"):
        sys.exit(1)
    if path.endswith("/check-runs"):
        doc = {"total_count": len(scenario["check_runs"]), "check_runs": scenario["check_runs"]}
    elif "/actions/runs" in path:
        doc = {"total_count": len(scenario["workflow_runs"]), "workflow_runs": scenario["workflow_runs"]}
    elif path.endswith("/status"):
        doc = scenario["status"]
    elif "/compare/" in path:
        if scenario.get("fail_compare"):
            sys.exit(1)
        doc = {"status": scenario.get("compare", "ahead")}
    else:
        sys.exit(1)
    r = subprocess.run(["/usr/bin/jq", "-r", jq], input=json.dumps(doc), text=True, capture_output=True)
    sys.stdout.write(r.stdout)
    sys.exit(r.returncode)
    """
)


def check_run(name, conclusion="success", status="completed", app="github-actions", suite=1):
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}, "check_suite": {"id": suite}}


def wf_run(conclusion="success", status="completed", name="Pipeline", suite=1, prs=(("dev", "autopilot/dev"),)):
    return {"name": name, "status": status, "conclusion": conclusion, "check_suite_id": suite,
            "pull_requests": [{"base": {"ref": b}, "head": {"ref": h}} for b, h in prs]}


def good():
    return {
        "check_runs": [check_run("CI passed"), check_run("Backend quality (orchestrator, shard 1 of 3)"), check_run("Deploy", "skipped")],
        "workflow_runs": [wf_run()],
        "status": {"state": "pending", "total_count": 0},
    }


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(os.path.exists(JQ), "the fake gh needs jq")
class GateHarness(unittest.TestCase):
    """A throwaway origin and checkout, a fake gh and helpers; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mtd-test-")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.wt = os.path.join(self.tmp, "wt")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.wt], check=True, capture_output=True)
        git(self.wt, "config", "user.email", "t@example.invalid")
        git(self.wt, "config", "user.name", "t")
        self.commit("README", "base")
        self.commit(".github/workflows/scripts/ci_gate.py", "import sys; sys.exit(run_all())")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/dev")
        git(self.wt, "checkout", "-q", "-b", "autopilot/dev")
        self.commit("work.txt", "autopilot work")
        self.commit("docs/ai-platform-upgrade/FINAL_REPORT.md", "final report")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/autopilot/dev")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.gh = self.fake_gh(self.bin, good())
        self.log = os.path.join(self.tmp, "mtd.log")
        self.approvals = os.path.join(self.tmp, "approved-ci-trees")
        self.gate = self.make_gate()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def fake_gh(self, d, scenario):
        path = os.path.join(d, "gh")
        with open(path, "w") as fh:
            fh.write(FAKE_GH)
        os.chmod(path, 0o755)
        with open(os.path.join(d, "scenario.json"), "w") as fh:
            json.dump(scenario, fh)
        return path

    def set_scenario(self, scenario):
        with open(os.path.join(self.bin, "scenario.json"), "w") as fh:
            json.dump(scenario, fh)

    def make_gate(self, repo=None, name="gate", protocol="file", git_bin="/usr/bin/git"):
        # The throwaway origin is a local path, which git's allow-list calls "file".
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        subs = {
            "GIT=/usr/bin/git": f"GIT={git_bin}",
            "GH=/usr/bin/gh": f"GH={self.gh}",
            "REPO=$HOME/work/llm-dev": f"REPO={repo or self.wt}",
            "EXPECTED_COMMON_DIR=$HOME/Documents/project/personal-LLM-Chabot/.git": f"EXPECTED_COMMON_DIR={self.wt}/.git",
            "ORIGIN_URL=https://github.com/namanjain221995/personal-LLM-Chabot.git": f"ORIGIN_URL={self.origin}",
            "ORIGIN_PROTOCOL=https": f"ORIGIN_PROTOCOL={protocol}",
            "LOG=$HOME/.llm-autopilot/logs/merge_to_dev.log": f"LOG={self.log}",
            "CI_APPROVALS=$HOME/.llm-autopilot/approved-ci-trees": f"CI_APPROVALS={self.approvals}",
        }
        for old, new in subs.items():
            self.assertEqual(text.count("\n" + old + "\n"), 1, old)
            text = text.replace("\n" + old + "\n", "\n" + new + "\n")
        path = os.path.join(self.tmp, name, "merge_to_dev.sh")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
        return path

    def commit(self, rel, content, msg=None):
        path = os.path.join(self.wt, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(content + "\n")
        git(self.wt, "add", rel)
        git(self.wt, "commit", "-q", "-m", msg or content)

    def push_tip(self):
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/autopilot/dev")

    def run_gate(self, *args, env=None, gate=None):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
        e.update(env or {})
        return subprocess.run([gate or self.gate, *args], env=e, capture_output=True, text=True, timeout=60)

    def origin_ref(self, ref):
        return git(self.origin, "rev-parse", ref)

    def assert_refused(self, r, needle, dev_before):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertEqual(self.origin_ref("dev"), dev_before)


@unittest.skipUnless(os.path.exists(JQ), "the fake gh needs jq")
class MergeToDev(GateHarness):
    # ---- the original gates
    def test_happy_path_fast_forwards_dev_and_never_touches_main(self):
        main_before = self.origin_ref("main")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertEqual(self.origin_ref("main"), main_before)
        self.assertIn("MERGED", open(self.log).read())

    def test_explicit_commit_and_branch_arguments(self):
        tip = self.origin_ref("autopilot/dev")
        self.assertEqual(self.run_gate("--dry-run", tip).returncode, 0)
        self.assertEqual(self.run_gate("--dry-run", "origin/autopilot/dev").returncode, 0)

    def test_dry_run_pushes_nothing(self):
        dev_before = self.origin_ref("dev")
        r = self.run_gate("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), dev_before)

    def test_refuses_without_final_report(self):
        dev_before = self.origin_ref("dev")
        git(self.wt, "rm", "-q", "docs/ai-platform-upgrade/FINAL_REPORT.md")
        git(self.wt, "commit", "-q", "-m", "drop report")
        self.push_tip()
        self.assert_refused(self.run_gate(), "FINAL_REPORT.md does not exist", dev_before)

    def test_refuses_empty_directory_or_symlink_report(self):
        dev_before = self.origin_ref("dev")
        report = os.path.join(self.wt, "docs/ai-platform-upgrade/FINAL_REPORT.md")

        def empty():
            open(report, "w").close()

        def directory():
            git(self.wt, "rm", "-q", "docs/ai-platform-upgrade/FINAL_REPORT.md")
            os.makedirs(report)
            open(os.path.join(report, "x"), "w").write("x\n")

        def symlink():
            git(self.wt, "rm", "-q", "-r", "docs/ai-platform-upgrade/FINAL_REPORT.md")
            shutil.rmtree(report, ignore_errors=True)
            os.makedirs(os.path.dirname(report), exist_ok=True)
            os.symlink("../../README", report)

        for name, change, needle in (("empty", empty, "is empty"), ("directory", directory, "not a regular file"),
                                     ("symlink", symlink, "not a regular file")):
            with self.subTest(name):
                change()
                git(self.wt, "add", "-A")
                git(self.wt, "commit", "-q", "-m", name)
                self.push_tip()
                self.assert_refused(self.run_gate("--dry-run"), needle, dev_before)

    def test_refuses_non_fast_forward(self):
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", "-b", "dev", self.origin, other], check=True, capture_output=True)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "o")
        with open(os.path.join(other, "operator.txt"), "w") as fh:
            fh.write("operator work\n")
        git(other, "add", "operator.txt")
        git(other, "commit", "-q", "-m", "operator work on dev")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/dev")
        r = self.run_gate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not an ancestor", r.stderr)

    def test_refuses_commit_that_is_not_the_pushed_tip(self):
        self.commit("local.txt", "local only")
        r = self.run_gate(git(self.wt, "rev-parse", "HEAD"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pushed tip", r.stderr)

    def test_refuses_failed_missing_or_pending_checks(self):
        def with_runs(runs, state="pending", count=0):
            s = good()
            s["check_runs"] = runs
            s["status"] = {"state": state, "total_count": count}
            return s

        cases = [
            (with_runs([check_run("CI passed", "failure")]), "not passed"),
            (with_runs([]), "no check runs"),
            (with_runs([check_run("Backend")]), "CI passed"),
            (with_runs([check_run("CI passed", None, "in_progress")]), "not passed"),
            (with_runs(good()["check_runs"], "failure", 2), "combined commit status"),
            ({**good(), "fail": True}, "could not read"),
        ]
        dev_before = self.origin_ref("dev")
        for scenario, needle in cases:
            with self.subTest(needle=needle):
                self.set_scenario(scenario)
                self.assert_refused(self.run_gate(), needle, dev_before)

    # ---- D9: the right workflow run, from the right pull request and app
    def test_requires_the_pipeline_run_of_the_dev_pull_request(self):
        def with_wf(runs, checks=None):
            s = good()
            s["workflow_runs"] = runs
            if checks is not None:
                s["check_runs"] = checks
            return s

        cases = [
            (with_wf([]), "no Pipeline run"),
            (with_wf([wf_run(prs=(("main", "autopilot/dev"),))]), "no Pipeline run"),
            (with_wf([wf_run(prs=(("dev", "upgrade/x/y"),))]), "no Pipeline run"),
            (with_wf([wf_run(name="Other")]), "no Pipeline run"),
            (with_wf([wf_run("failure")]), "has not succeeded"),
            (with_wf([wf_run(None, "in_progress")]), "has not succeeded"),
            (with_wf([wf_run(), wf_run("failure", suite=2)]), "has not succeeded"),
            (with_wf([wf_run()], [check_run("CI passed", app="some-other-app")]), "required check"),
            (with_wf([wf_run(suite=1), wf_run(suite=2, prs=(("main", "autopilot/dev"),))],
                     [check_run("CI passed", suite=2)]), "required check"),
        ]
        dev_before = self.origin_ref("dev")
        for scenario, needle in cases:
            with self.subTest(needle=needle, runs=scenario["workflow_runs"]):
                self.set_scenario(scenario)
                self.assert_refused(self.run_gate(), needle, dev_before)
        self.set_scenario(with_wf([wf_run(), wf_run(suite=2, prs=(("main", "autopilot/dev"),))]))
        self.assertEqual(self.run_gate("--dry-run").returncode, 0)

    # ---- D6: a commit may not be graded by CI it changed
    def test_refuses_changed_ci_definition_unless_approved(self):
        dev_before = self.origin_ref("dev")
        self.commit(".github/workflows/scripts/ci_gate.py", "import sys; sys.exit(0)")
        self.push_tip()
        r = self.run_gate()
        self.assert_refused(r, "CI definition", dev_before)
        tree = git(self.wt, "rev-parse", "HEAD:.github")
        self.assertIn(tree, r.stderr)
        with open(self.approvals, "w") as fh:
            fh.write("0" * 40 + "\n")
        self.assert_refused(self.run_gate(), "CI definition", dev_before)
        with open(self.approvals, "a") as fh:
            fh.write(tree + "\n")
        self.assertEqual(self.run_gate("--dry-run").returncode, 0)

    def test_unchanged_ci_with_other_code_changes_passes(self):
        self.commit("orchestrator/x.py", "x = 1")
        self.push_tip()
        self.assertEqual(self.run_gate("--dry-run").returncode, 0)

    # ---- D3, D4, D7: nothing from the caller is trusted
    def failing_truth(self):
        s = good()
        s["check_runs"] = [check_run("CI passed", "failure")]
        self.set_scenario(s)

    def test_ignores_a_gh_or_functions_from_the_callers_environment(self):
        self.failing_truth()
        evil = os.path.join(self.tmp, "evil")
        os.makedirs(evil)
        self.fake_gh(evil, good())
        env_sh = os.path.join(self.tmp, "env.sh")
        with open(env_sh, "w") as fh:
            fh.write("gh() { printf 'CI passed\\tcompleted\\tsuccess\\tgithub-actions\\t1\\n'; }\nawk() { return 0; }\n")
        dev_before = self.origin_ref("dev")
        attacks = {
            "PATH": {"PATH": evil + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")},
            "BASH_ENV": {"BASH_ENV": env_sh},
            "ENV": {"ENV": env_sh},
            "exported functions": {"BASH_FUNC_awk%%": "() { return 0; }", "BASH_FUNC_gh%%": "() { return 0; }"},
            "SHELLOPTS": {"SHELLOPTS": "xtrace"},
        }
        for name, env in attacks.items():
            with self.subTest(name):
                self.assert_refused(self.run_gate(env=env), "not passed", dev_before)

    def test_ignores_git_configuration_from_the_environment(self):
        decoy = os.path.join(self.tmp, "decoy.git")
        subprocess.run(["git", "init", "--bare", "-q", decoy], check=True)
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.pushurl", "GIT_CONFIG_VALUE_0": decoy,
               "GIT_DIR": os.path.join(self.tmp, "nowhere"), "GIT_SSH_COMMAND": "false"}
        r = self.run_gate(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertEqual(git(decoy, "for-each-ref"), "")

    def test_ignores_the_old_override_variables(self):
        git(self.wt, "checkout", "-q", "-b", "upgrade/x/y")
        self.commit("feature.txt", "feature")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/upgrade/x/y")
        fake_rc = os.path.join(self.tmp, "fake_bashrc")
        other = os.path.join(self.tmp, "other-clone")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True, capture_output=True)
        env = {"MERGE_TO_DEV_SOURCE": "upgrade/x/y", "MERGE_TO_DEV_REPO": other, "MERGE_TO_DEV_LOG": fake_rc}
        r = self.run_gate(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertNotEqual(self.origin_ref("dev"), self.origin_ref("upgrade/x/y"))
        self.assertFalse(os.path.exists(fake_rc))

    def test_refuses_bad_arguments_before_logging_them(self):
        marker = os.path.join(self.tmp, "pwned")
        for arg in (f"x; touch {marker}", "HEAD", "--force", "refs/heads/upgrade/x", "a" * 40 + "\n"):
            with self.subTest(arg=arg):
                r = self.run_gate(arg, env={"MERGE_TO_DEV_LOG": os.path.join(self.tmp, "fake_bashrc")})
                self.assertEqual(r.returncode, 2)
                self.assertNotIn("touch", open(self.log).read() if os.path.exists(self.log) else "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "fake_bashrc")))
        self.assertFalse(os.path.exists(marker))

    def test_refuses_an_origin_that_points_elsewhere(self):
        decoy = os.path.join(self.tmp, "decoy.git")
        subprocess.run(["git", "init", "--bare", "-q", decoy], check=True)
        dev_before = self.origin_ref("dev")
        for key in ("remote.origin.pushurl", "remote.origin.url"):
            with self.subTest(key):
                git(self.wt, "config", key, decoy)
                self.assert_refused(self.run_gate(), "URL of origin", dev_before)
                git(self.wt, "config", "--unset", key)
        git(self.wt, "config", f"url.{decoy}.pushInsteadOf", self.origin)
        self.assert_refused(self.run_gate(), "URL of origin", dev_before)
        self.assertEqual(git(decoy, "for-each-ref"), "")

    def test_help_prints_the_whole_header(self):
        r = self.run_gate("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("# merge_to_dev.sh: "), lines[0])
        self.assertTrue(lines[-1].startswith("# Usage: merge_to_dev.sh "), lines[-1])

    def test_source_has_no_environment_overrides(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("#!/bin/bash -p\n"), "bash -p ignores BASH_ENV, ENV and exported functions")
        self.assertNotIn("MERGE_TO_DEV_", text)
        self.assertNotIn("${GH:-", text)

    # ---- P0-17: the repository's hooks, replace refs and identity
    def install_hooks(self, hooks_dir, marker):
        os.makedirs(hooks_dir, exist_ok=True)
        for name in ("pre-push", "reference-transaction", "pre-auto-gc", "post-checkout"):
            path = os.path.join(hooks_dir, name)
            with open(path, "w") as fh:
                fh.write(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n")
            os.chmod(path, 0o755)

    def test_repository_hooks_never_run(self):
        marker = os.path.join(self.tmp, "hook-ran")
        self.install_hooks(os.path.join(self.wt, ".git", "hooks"), marker)
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertFalse(os.path.exists(marker), "a hook in .git/hooks ran inside the gate")

    def test_a_configured_hooks_path_is_ignored_too(self):
        marker = os.path.join(self.tmp, "hook-ran")
        custom = os.path.join(self.tmp, "custom-hooks")
        self.install_hooks(custom, marker)
        git(self.wt, "config", "core.hooksPath", custom)
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertFalse(os.path.exists(marker), "a hook from core.hooksPath ran inside the gate")

    def test_replace_refs_cannot_supply_a_missing_report(self):
        dev_before = self.origin_ref("dev")
        git(self.wt, "rm", "-q", "docs/ai-platform-upgrade/FINAL_REPORT.md")
        git(self.wt, "commit", "-q", "-m", "drop report")
        self.push_tip()
        git(self.wt, "replace", "HEAD", "HEAD~1")  # the tip now reads as its parent, which has a report
        self.assert_refused(self.run_gate(), "FINAL_REPORT.md does not exist", dev_before)

    def test_replace_refs_cannot_hide_a_ci_change(self):
        dev_before = self.origin_ref("dev")
        self.commit(".github/workflows/scripts/ci_gate.py", "import sys; sys.exit(0)")
        self.push_tip()
        git(self.wt, "replace", "HEAD", "HEAD~1")  # the tip now reads as its parent, with dev's .github
        self.assert_refused(self.run_gate(), "CI definition", dev_before)

    def test_refuses_a_checkout_of_another_repository(self):
        # Another clone of the same origin passes the origin URL check; only
        # its git common directory tells it apart from the expected repository.
        dev_before = self.origin_ref("dev")
        decoy = os.path.join(self.tmp, "decoy")
        subprocess.run(["git", "clone", "-q", self.origin, decoy], check=True, capture_output=True)
        pointer = os.path.join(self.tmp, "pointer")  # a .git file that points into the decoy
        os.makedirs(pointer)
        with open(os.path.join(pointer, ".git"), "w") as fh:
            fh.write(f"gitdir: {decoy}/.git\n")
        for name, repo in (("clone", decoy), ("gitfile", pointer)):
            with self.subTest(name):
                gate = self.make_gate(repo=repo, name=f"gate-{name}")
                r = self.run_gate(gate=gate)
                self.assert_refused(r, f"the git common directory of {repo} is", dev_before)
                self.assertIn(f"not the expected {os.path.realpath(self.wt)}/.git", r.stderr)
                self.assertNotIn("OK:", r.stderr, "it refuses before any other check")

    def test_refuses_a_directory_outside_any_repository(self):
        dev_before = self.origin_ref("dev")
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        gate = self.make_gate(repo=plain, name="gate-plain")
        self.assert_refused(self.run_gate(gate=gate), f"cannot read the git directory of {plain}", dev_before)

    def test_refuses_when_the_expected_repository_is_missing(self):
        dev_before = self.origin_ref("dev")
        shutil.move(os.path.join(self.wt, ".git"), os.path.join(self.tmp, "moved.git"))
        self.assert_refused(self.run_gate(), "the expected git common directory", dev_before)

    def test_accepts_a_linked_worktree_of_the_expected_repository(self):
        # The dev worktree is a linked worktree; its common dir is the main checkout's .git.
        linked = os.path.join(self.tmp, "linked")
        git(self.wt, "worktree", "add", "-q", "--detach", linked, "HEAD")
        gate = self.make_gate(repo=linked, name="gate-linked")
        r = self.run_gate(gate=gate)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))

    def test_the_git_directory_is_pinned_after_the_check(self):
        # Once step 0 has verified the repository, repointing $REPO's .git file
        # (here: by a git stand-in, between the check and the next call) must
        # not move the fetch and push to another repository.
        dev_before = self.origin_ref("dev")
        decoy = os.path.join(self.tmp, "decoy")
        subprocess.run(["git", "clone", "-q", self.origin, decoy], check=True, capture_output=True)
        stale = git(decoy, "rev-parse", "refs/remotes/origin/autopilot/dev")
        self.commit("more.txt", "more")
        self.push_tip()
        pointer = os.path.join(self.tmp, "pointer")
        os.makedirs(pointer)
        with open(os.path.join(pointer, ".git"), "w") as fh:
            fh.write(f"gitdir: {self.wt}/.git\n")
        swapped = os.path.join(self.tmp, "swapped")
        stand_in = os.path.join(self.tmp, "git-swapping")
        with open(stand_in, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/bin/sh
                case " $* " in
                    *" remote get-url "*)
                        if [ ! -e '{swapped}' ]; then
                            printf 'gitdir: %s\\n' '{decoy}/.git' > '{pointer}/.git'
                            : > '{swapped}'
                        fi ;;
                esac
                exec /usr/bin/git "$@"
                """))
        os.chmod(stand_in, 0o755)
        gate = self.make_gate(repo=pointer, name="gate-swap", git_bin=stand_in)
        r = self.run_gate(gate=gate)
        self.assertTrue(os.path.exists(swapped), "the .git file was repointed during the run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.origin_ref("dev"), dev_before)
        self.assertEqual(self.origin_ref("dev"), git(self.wt, "rev-parse", "refs/remotes/origin/autopilot/dev"))
        self.assertEqual(git(decoy, "rev-parse", "refs/remotes/origin/autopilot/dev"), stale,
                         "git kept to the verified git directory; the decoy was never fetched into")

    # ---- P0-18: the git directory itself belongs to the expected repository
    def swapping_git(self, trigger, path, content, swapped):
        # a git stand-in that rewrites `path` the first time a call contains `trigger`
        stand_in = os.path.join(self.tmp, "git-swapping")
        with open(stand_in, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/bin/sh
                case " $* " in
                    *" {trigger} "*)
                        if [ ! -e '{swapped}' ]; then
                            printf '%s\\n' '{content}' > '{path}'
                            : > '{swapped}'
                        fi ;;
                esac
                exec /usr/bin/git "$@"
                """))
        os.chmod(stand_in, 0o755)
        return stand_in

    def agent_made_git_dir(self, fake, pointer):
        # A git directory nobody made with `git worktree add`: its commondir file
        # names the expected repository, so the common-directory check alone
        # passes, but git re-reads that file on every call.
        os.makedirs(fake)
        os.makedirs(pointer, exist_ok=True)
        with open(os.path.join(fake, "commondir"), "w") as fh:
            fh.write(os.path.join(self.wt, ".git") + "\n")
        with open(os.path.join(fake, "HEAD"), "w") as fh:
            fh.write(git(self.wt, "rev-parse", "HEAD") + "\n")
        with open(os.path.join(fake, "gitdir"), "w") as fh:
            fh.write(os.path.join(pointer, ".git") + "\n")
        with open(os.path.join(pointer, ".git"), "w") as fh:
            fh.write(f"gitdir: {fake}\n")

    def test_refuses_a_git_directory_outside_the_expected_repository(self):
        dev_before = self.origin_ref("dev")
        decoy = os.path.join(self.tmp, "decoy")
        subprocess.run(["git", "clone", "-q", self.origin, decoy], check=True, capture_output=True)
        wt_git = os.path.realpath(os.path.join(self.wt, ".git"))
        linked = os.path.join(self.tmp, "linked")
        git(self.wt, "worktree", "add", "-q", "--detach", linked, "HEAD")
        places = {
            "elsewhere": os.path.join(self.tmp, "agent-zone", "fake-gitdir"),
            "nested under worktrees/": os.path.join(wt_git, "worktrees", "linked", "nested"),
            "beside the repository": wt_git + "-fake",
        }
        for name, fake in places.items():
            with self.subTest(name):
                pointer = os.path.join(self.tmp, "pointer-" + name.replace(" ", "-").strip("/"))
                self.agent_made_git_dir(fake, pointer)
                swapped = os.path.join(fake, "swapped")
                stand_in = self.swapping_git("remote get-url", os.path.join(fake, "commondir"), f"{decoy}/.git", swapped)
                gate = self.make_gate(repo=pointer, name="gate-" + os.path.basename(pointer), git_bin=stand_in)
                r = self.run_gate(gate=gate)
                self.assert_refused(r, f"the git directory {os.path.realpath(fake)} of {pointer} is not", dev_before)
                self.assertNotIn("OK:", r.stderr)
                self.assertFalse(os.path.exists(swapped), "it refuses before git reads anything else")

    def test_a_swapped_commondir_file_changes_nothing(self):
        # A linked worktree of the expected repository passes step 0; its
        # commondir file is then pointed at another repository. git must keep
        # to the common directory the gate verified.
        dev_before = self.origin_ref("dev")
        decoy = os.path.join(self.tmp, "decoy")
        subprocess.run(["git", "clone", "-q", self.origin, decoy], check=True, capture_output=True)
        stale = git(decoy, "rev-parse", "refs/remotes/origin/autopilot/dev")
        self.commit("more.txt", "more")
        self.push_tip()
        linked = os.path.join(self.tmp, "linked")
        git(self.wt, "worktree", "add", "-q", "--detach", linked, "HEAD")
        commondir = os.path.join(self.wt, ".git", "worktrees", "linked", "commondir")
        self.assertTrue(os.path.exists(commondir))
        for trigger in ("remote get-url", "fetch"):
            with self.subTest(trigger):
                with open(commondir, "w") as fh:
                    fh.write("../..\n")
                swapped = os.path.join(self.tmp, "swapped-" + trigger.replace(" ", "-"))
                stand_in = self.swapping_git(trigger, commondir, f"{decoy}/.git", swapped)
                gate = self.make_gate(repo=linked, name="gate-cd-" + trigger.replace(" ", "-"), git_bin=stand_in)
                r = self.run_gate("--dry-run", gate=gate) if trigger == "fetch" else self.run_gate(gate=gate)
                self.assertTrue(os.path.exists(swapped), "the commondir file was repointed during the run")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(git(decoy, "rev-parse", "refs/remotes/origin/autopilot/dev"), stale,
                                 "git kept to the verified common directory; the decoy was never fetched into")
        self.assertNotEqual(self.origin_ref("dev"), dev_before)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))

    def test_refuses_a_commondir_file_in_the_main_git_directory(self):
        # git runs on the common directory after step 0; a commondir file there
        # would take its refs elsewhere, so the gate refuses one, even dangling.
        dev_before = self.origin_ref("dev")
        decoy = os.path.join(self.tmp, "decoy")
        subprocess.run(["git", "clone", "-q", self.origin, decoy], check=True, capture_output=True)
        linked = os.path.join(self.tmp, "linked")
        git(self.wt, "worktree", "add", "-q", "--detach", linked, "HEAD")
        gate = self.make_gate(repo=linked, name="gate-main-commondir")
        planted = os.path.join(self.wt, ".git", "commondir")
        for name, plant in (("file", lambda: open(planted, "w").write(f"{decoy}/.git\n")),
                            ("dangling symlink", lambda: os.symlink(os.path.join(self.tmp, "nowhere"), planted))):
            with self.subTest(name):
                plant()
                try:
                    self.assert_refused(self.run_gate(gate=gate), "has a commondir file", dev_before)
                finally:
                    os.remove(planted)
        self.assertEqual(self.run_gate("--dry-run", gate=gate).returncode, 0)

    # ---- P0-17: repository configuration cannot make the gate's git run a command
    def marker_script(self, name, body=""):
        marker = os.path.join(self.tmp, name + ".ran")
        path = os.path.join(self.tmp, name + ".sh")
        with open(path, "w") as fh:
            fh.write(f"#!/bin/sh\necho ran >> '{marker}'\n{body}exit 1\n")
        os.chmod(path, 0o755)
        return marker, path

    def assert_merged_without(self, marker, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertFalse(os.path.exists(marker), "a command from the repository's configuration ran inside the gate")

    def test_a_configured_fsmonitor_never_runs(self):
        # core.fsmonitor names a hook program, which core.hooksPath does not
        # cover. A fetch that recurses into submodules reads the index and so
        # asks it; the gate pins it off and also fetches without recursing, so
        # this fails only if both go (test_every_git_call_is_hardened checks each).
        marker, script = self.marker_script("fsmonitor")
        git(self.wt, "config", "core.fsmonitor", script)
        self.assert_merged_without(marker, self.run_gate())

    def test_a_configured_alternate_refs_command_never_runs(self):
        marker, script = self.marker_script("alternate-refs")
        alternate = os.path.join(self.tmp, "alternate.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.origin, alternate], check=True, capture_output=True)
        with open(os.path.join(self.wt, ".git", "objects", "info", "alternates"), "w") as fh:
            fh.write(os.path.join(alternate, "objects") + "\n")
        git(self.wt, "config", "core.alternateRefsCommand", script)
        other = os.path.join(self.tmp, "other")  # a new tip the gate's fetch must negotiate for
        subprocess.run(["git", "clone", "-q", "-b", "autopilot/dev", self.origin, other], check=True, capture_output=True)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "o")
        with open(os.path.join(other, "more.txt"), "w") as fh:
            fh.write("more\n")
        git(other, "add", "more.txt")
        git(other, "commit", "-q", "-m", "more")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/autopilot/dev")
        self.assert_merged_without(marker, self.run_gate())

    def test_the_push_is_never_signed_by_a_configured_program(self):
        marker, script = self.marker_script("gpg")
        git(self.origin, "config", "receive.certNonceSeed", "test-seed")  # origin accepts signed pushes
        git(self.wt, "config", "push.gpgSign", "true")
        git(self.wt, "config", "gpg.program", script)
        self.assert_merged_without(marker, self.run_gate())

    def test_automatic_maintenance_never_runs(self):
        marker, script = self.marker_script("recent-objects")
        packs = os.path.join(self.wt, ".git", "objects", "pack")
        for i in range(3):  # three packs where gc.autoPackLimit allows one
            self.commit(f"pack{i}.txt", f"pack {i}")
            git(self.wt, "repack", "-q")
        self.push_tip()
        before = sorted(f for f in os.listdir(packs) if f.endswith(".pack"))
        self.assertGreater(len(before), 1)
        for key, value in (("gc.auto", "1"), ("gc.autoPackLimit", "1"), ("gc.autoDetach", "false"),
                           ("maintenance.auto", "true"), ("gc.recentObjectsHook", script)):
            git(self.wt, "config", key, value)
        self.assert_merged_without(marker, self.run_gate())
        self.assertEqual(sorted(f for f in os.listdir(packs) if f.endswith(".pack")), before,
                         "git gc --auto repacked the repository inside the gate")

    def test_a_configured_pager_never_runs_on_a_terminal(self):
        import pty
        marker, script = self.marker_script("pager", body="cat >/dev/null\n")
        for cmd in ("fetch", "push", "ls-remote", "rev-parse", "merge-base"):
            git(self.wt, "config", f"pager.{cmd}", script)
        master, slave = pty.openpty()
        try:
            e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
            r = subprocess.run([self.gate], env=e, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE,
                               text=True, timeout=60)
        finally:
            os.close(slave)
            os.close(master)
        self.assert_merged_without(marker, r)

    # ---- P0-17: the URL git resolves for origin, and the transports it may use
    def test_refuses_origin_urls_that_differ_from_the_origin_in_any_way(self):
        # The origin URL without its .git suffix names the same repository on
        # GitHub, but git resolves it differently; only the exact URL passes.
        dev_before = self.origin_ref("dev")
        marker, script = self.marker_script("ext")
        git(self.wt, "remote", "set-url", "origin", self.origin[: -len(".git")])
        git(self.wt, "config", f"url.ext::{script} %S.insteadOf", self.origin)
        git(self.wt, "config", "protocol.ext.allow", "always")
        self.assert_refused(self.run_gate(), "the fetch URL of origin", dev_before)
        self.assertFalse(os.path.exists(marker))

    def test_refuses_when_git_rewrites_the_url_the_gate_fetches_from(self):
        # origin's own URL reads back as the origin, but the explicit URL the
        # gate fetches from and lists is rewritten to somewhere else.
        dev_before = self.origin_ref("dev")
        marker, script = self.marker_script("ext")
        decoy = os.path.join(self.tmp, "decoy.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.origin, decoy], check=True, capture_output=True)
        alias = os.path.join(self.tmp, "alias-of-origin")
        git(self.wt, "remote", "set-url", "origin", alias)
        git(self.wt, "config", f"url.{self.origin}.insteadOf", alias)
        git(self.wt, "config", "protocol.ext.allow", "always")
        for name, target in (("another repository", decoy), ("a command", f"ext::{script} %S")):
            with self.subTest(name):
                git(self.wt, "config", f"url.{target}.insteadOf", self.origin)
                self.assertEqual(git(self.wt, "remote", "get-url", "origin"), self.origin)
                self.assertEqual(git(self.wt, "remote", "get-url", "--push", "origin"), self.origin)
                self.assert_refused(self.run_gate(), "rewrites the URL", dev_before)
                self.assertFalse(os.path.exists(marker))
                self.assertEqual(git(decoy, "rev-parse", "dev"), dev_before)
                git(self.wt, "config", "--remove-section", f"url.{target}")

    def test_git_may_use_only_the_origins_protocol(self):
        # The gate allows git one transport (https for GitHub). Allowing only
        # https here, where the origin is a local path, stops the fetch.
        dev_before = self.origin_ref("dev")
        gate = self.make_gate(name="gate-https-only", protocol="https")
        r = self.run_gate(gate=gate)
        self.assert_refused(r, "git fetch of dev and autopilot/dev failed", dev_before)
        self.assertIn("transport 'file' not allowed", r.stderr)

    def test_every_git_call_is_hardened(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(text, r"\nGIT_NO_REPLACE_OBJECTS=1\n")
        self.assertRegex(text, r"\nexport [^\n]*\bGIT_NO_REPLACE_OBJECTS\b")
        self.assertRegex(text, r"\nGIT_ALLOW_PROTOCOL=\$ORIGIN_PROTOCOL\n")
        self.assertRegex(text, r"\nexport [^\n]*\bGIT_ALLOW_PROTOCOL\b")
        self.assertRegex(text, r"\nGIT_COMMON_DIR=\$want_common\n")
        self.assertRegex(text, r"\nexport [^\n]*\bGIT_COMMON_DIR\b")
        wrapper = re.search(r"\ng\(\) \{\n(.*?)\n\}\n", text, re.S)
        self.assertIsNotNone(wrapper, "one git wrapper, g()")
        for flag in ("--no-pager", "--no-replace-objects", "-c core.hooksPath=/dev/null", "-c core.fsmonitor=false",
                     "-c core.alternateRefsCommand=true", "-c push.gpgSign=false", "-c maintenance.auto=false",
                     "-c gc.auto=0", '--git-dir="$git_dir"', "-c core.askPass= -c credential.helper= "):
            self.assertIn(flag, wrapper.group(1), flag)
        # the one helper added back comes after the reset, from $cred alone
        self.assertIn('-c credential.helper= ${cred[@]+"${cred[@]}"}', wrapper.group(1))
        self.assertEqual(len(re.findall(r"\bcred=\(", text)), 2, "cred is set empty, then once from the global config")
        self.assertRegex(text, r'\n {12}cred=\(-c "credential\.\$cred_scope\.helper=\$helper"\)\n')
        self.assertRegex(text, r'helper=\$\(g config --global --get-urlmatch credential\.helper "\$ORIGIN_URL"')
        self.assertRegex(text, r"\nGIT_TERMINAL_PROMPT=0\n")
        self.assertRegex(text, r"\nexport [^\n]*\bGIT_TERMINAL_PROMPT\b")
        self.assertRegex(text, r"\nunset GIT_ASKPASS SSH_ASKPASS\n")
        self.assertRegex(text, r"\nGIT_GRAFT_FILE=/dev/null\n")
        self.assertRegex(text, r"\nexport [^\n]*\bGIT_GRAFT_FILE\b")
        self.assertIn("-c credential.useHttpPath=false", wrapper.group(1))
        self.assertIn("-c core.commitGraph=false", wrapper.group(1))
        for call, flags in (("g fetch ", ("--no-recurse-submodules", "--no-write-fetch-head")),
                            ("g push ", ("--no-recurse-submodules", "--no-follow-tags", "--no-verify"))):
            lines = [ln for ln in text.splitlines() if ln.startswith(call)]
            self.assertEqual(len(lines), 1, call)
            for flag in flags:
                self.assertIn(flag, lines[0], call + flag)
        # the remote-tracking refs are only written (by the fetch), never read
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        tracking = [ln.strip() for ln in code if "refs/remotes/" in ln]
        self.assertEqual(tracking, ['"+refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH" \\',
                                    '"+refs/heads/$SOURCE_BRANCH:refs/remotes/origin/$SOURCE_BRANCH" \\'])
        # $GIT, ${GIT} or "$GIT" is used once, inside the wrapper, and nothing
        # outside the configuration line names the git binary or runs git bare
        uses = re.findall(r"\$\{?GIT\}?(?![A-Za-z0-9_])", text)
        self.assertEqual(len(uses), 1, "git runs only through the one hardened wrapper")
        self.assertIn('"$GIT"', wrapper.group(1))
        self.assertEqual(text.count("/usr/bin/git"), 1, "the binary is named only in the configuration block")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        bare = [ln for ln in code if re.search(r"(?:^|[;&|({]|\$\()\s*(?:command\s+|exec\s+|env\s+|\\)?git(?:\s|$)", ln)]
        self.assertEqual(bare, [], "git is called outside the wrapper")

    # ---- P0-18: repository configuration that reroutes the transport is refused
    def test_refuses_repository_configuration_that_reroutes_the_transport(self):
        dev_before = self.origin_ref("dev")
        other = os.path.join(self.tmp, "elsewhere.git")
        subprocess.run(["git", "init", "--bare", "-q", other], check=True)
        included = os.path.join(self.tmp, "included.cfg")
        with open(included, "w") as fh:
            fh.write("[http]\n\tproxy = http://127.0.0.1:9\n")
        config = os.path.join(self.wt, ".git", "config")
        worktree_config = os.path.join(self.wt, ".git", "config.worktree")
        with open(config) as fh:
            pristine = fh.read()

        def worktree_scope():
            with open(config, "a") as fh:
                fh.write("[extensions]\n\tworktreeConfig = true\n")
            with open(worktree_config, "w") as fh:
                fh.write(f'[remote "origin"]\n\tpushurl = {self.origin}\n[url "{other}"]\n\tpushInsteadOf = {self.origin}\n')

        cases = [
            # get-url --push origin shows the explicit pushurl, but the gate's push by URL is rewritten
            ("url.<...>.pushinsteadof", [("remote.origin.pushurl", self.origin), (f"url.{other}.pushInsteadOf", self.origin)]),
            ("url.<...>.insteadof", [("url.https://example.invalid/.insteadOf", "unrelated:")]),
            ("http.proxy", [("http.proxy", "http://127.0.0.1:9")]),
            ("http.<...>.extraheader", [("http.https://example.invalid/.extraHeader", "X-Probe: 1")]),
            ("http.curloptresolve", [("http.curloptResolve", "example.invalid:443:127.0.0.1")]),
            ("http.<...>.sslverify", [(f"http.{self.origin}.sslVerify", "false")]),
            ("core.sshcommand", [("core.sshCommand", "false")]),
            ("remote.<...>.pushurl", [(f"remote.file://{self.origin}.pushurl", other)]),
            ("include.path", [("include.path", included)]),
            ("includeif.<...>.path", [("includeIf.gitdir:/.path", included)]),
            ("url.<...>.pushinsteadof", worktree_scope),
        ]
        for needle, change in cases:
            with self.subTest(needle, change=change if isinstance(change, list) else "config.worktree"):
                try:
                    if callable(change):
                        change()
                    else:
                        for key, value in change:
                            git(self.wt, "config", key, value)
                    r = self.run_gate()
                    self.assert_refused(r, "sets ", dev_before)
                    self.assertIn(needle, r.stderr.split("sets ", 1)[1].split(", which", 1)[0].split(","))
                    self.assertNotIn("OK:", r.stderr)
                    for value in ("example.invalid", "X-Probe", "127.0.0.1:9"):
                        self.assertNotIn(value, r.stderr, "the log shows key names without values or subsections")
                    self.assertEqual(git(other, "for-each-ref"), "", "nothing was pushed elsewhere")
                finally:
                    with open(config, "w") as fh:
                        fh.write(pristine)
                    if os.path.exists(worktree_config):
                        os.remove(worktree_config)
        r = self.run_gate("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_rerouting_key_added_during_the_checks_is_refused_before_the_push(self):
        # The configuration is listed again right before the push: a key that
        # appears after the first check (here: added by a git stand-in while
        # the gate reads the commit) still stops it.
        dev_before = self.origin_ref("dev")
        added = os.path.join(self.tmp, "added")
        stand_in = os.path.join(self.tmp, "git-adding")
        with open(stand_in, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/bin/sh
                case " $* " in
                    *" ls-tree "*)
                        if [ ! -e '{added}' ]; then
                            printf '[url "{self.tmp}/elsewhere.git"]\\n\\tpushInsteadOf = {self.origin}\\n' >> '{self.wt}/.git/config'
                            : > '{added}'
                        fi ;;
                esac
                exec /usr/bin/git "$@"
                """))
        os.chmod(stand_in, 0o755)
        gate = self.make_gate(name="gate-late-key", git_bin=stand_in)
        r = self.run_gate(gate=gate)
        self.assertTrue(os.path.exists(added), "the key was added after the first check")
        self.assert_refused(r, "sets url.<...>.pushinsteadof, which", dev_before)
        self.assertIn("OK:", r.stderr, "every other check had passed")

    def test_the_production_shaped_configuration_passes(self):
        # The production checkout's repository configuration (key names checked
        # 2026-10-03): branch metadata, pull.rebase and a URL-scoped credential
        # helper, which the gate neutralises rather than refuses.
        marker, script = self.marker_script("repo-helper")
        for key, value in (("pull.rebase", "false"), ("branch.dev.github-pr-owner-number", "o#r#1"),
                           ("branch.main.vscode-merge-base", "origin/main"),
                           ("credential.https://github.com.helper", ""),
                           ("credential.https://github.com.helper", "!" + script)):
            git(self.wt, "config", "--add", key, value)
        r = self.run_gate()
        self.assert_merged_without(marker, r)

    # ---- P0-18 review: the branch tips come from the origin, not from the shared remote-tracking refs
    def ci_edit_below_the_tip(self):
        # The autopilot's history gets a commit that edits the CI definition
        # (kept as the branch upgrade/ci-edit), then more work on top: dev ->
        # CI edit -> tip is a fast-forward whose .github/ differs from dev's.
        self.commit(".github/workflows/scripts/ci_gate.py", "sys.exit(0)  # every check passes now", msg="ci: always pass")
        git(self.wt, "branch", "-q", "upgrade/ci-edit")
        edit = git(self.wt, "rev-parse", "HEAD")
        self.commit("more.txt", "work on top of the CI edit")
        self.push_tip()
        return edit

    def repointing_git(self, ref, target):
        # a git stand-in that points `ref` at `target` before every call made
        # after the gate's fetch, as a concurrent process sharing the
        # repository can
        fetched = os.path.join(self.tmp, "fetched")
        stand_in = os.path.join(self.tmp, "git-repointing")
        with open(stand_in, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/bin/sh
                if [ -e '{fetched}' ]; then
                    /usr/bin/git --git-dir='{self.wt}/.git' update-ref '{ref}' '{target}'
                fi
                case " $* " in
                    *" fetch "*) /usr/bin/git "$@"; rc=$?; : > '{fetched}'; exit $rc ;;
                esac
                exec /usr/bin/git "$@"
                """))
        os.chmod(stand_in, 0o755)
        return stand_in, fetched

    def test_a_repointed_dev_ref_cannot_hide_a_ci_change(self):
        # With refs/remotes/origin/dev pointing at the CI edit, the edit would
        # look like dev's own CI definition and an ancestor of the tip.
        dev_before = self.origin_ref("dev")
        edit = self.ci_edit_below_the_tip()
        stand_in, fetched = self.repointing_git("refs/remotes/origin/dev", edit)
        gate = self.make_gate(name="gate-repoint-dev", git_bin=stand_in)
        r = self.run_gate(gate=gate)
        self.assertTrue(os.path.exists(fetched), "the stand-in ran after the fetch")
        self.assertEqual(git(self.wt, "rev-parse", "refs/remotes/origin/dev"), edit, "the ref was repointed")
        self.assert_refused(r, "CI definition (.github/)", dev_before)
        self.assertIn(dev_before, r.stderr, "the refusal names origin's dev, not the repointed ref")
        self.assertNotIn("always pass", git(self.origin, "log", "--format=%s", "dev"))

    def test_a_repointed_source_ref_cannot_offer_an_unpushed_commit(self):
        # With refs/remotes/origin/autopilot/dev pointing at a commit that was
        # never pushed, that commit would pass as the pushed tip.
        dev_before = self.origin_ref("dev")
        pushed = self.origin_ref("autopilot/dev")
        self.commit("local.txt", "local only, never pushed")
        local = git(self.wt, "rev-parse", "HEAD")
        stand_in, fetched = self.repointing_git("refs/remotes/origin/autopilot/dev", local)
        gate = self.make_gate(name="gate-repoint-tip", git_bin=stand_in)
        r = self.run_gate("--dry-run", local, gate=gate)
        self.assertTrue(os.path.exists(fetched), "the stand-in ran after the fetch")
        self.assert_refused(r, "is not the pushed tip of origin/autopilot/dev", dev_before)
        # the branch name means origin's tip, which passes: dev moves there, not to the local commit
        r = self.run_gate("origin/autopilot/dev", gate=gate)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), pushed)

    def test_a_background_loop_repointing_dev_never_gets_a_ci_change_merged(self):
        # The probe from the review: a loop in a linked worktree keeps fetching
        # the CI edit into the shared refs/remotes/origin/dev (a spelling of
        # fetch that only writes local refs) while the gate runs.
        dev_before = self.origin_ref("dev")
        self.ci_edit_below_the_tip()
        linked = os.path.join(self.tmp, "llm-dev")
        git(self.wt, "worktree", "add", "-q", "--detach", linked, "HEAD")
        gate = self.make_gate(repo=linked, name="gate-race")
        loop = subprocess.Popen(
            ["/bin/sh", "-c", f"while :; do git -C '{linked}' fetch -q . "
                              "+refs/heads/upgrade/ci-edit:refs/remotes/origin/dev 2>/dev/null; done"],
            start_new_session=True)
        try:
            results = [self.run_gate(gate=gate) for _ in range(5)]
        finally:
            os.killpg(loop.pid, 9)
            loop.wait()
        for r in results:
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("MERGED", r.stderr)
        self.assertTrue(any("CI definition (.github/)" in r.stderr for r in results),
                        [r.stderr.strip().splitlines()[-1:] for r in results])
        self.assertEqual(self.origin_ref("dev"), dev_before)
        self.assertNotIn("always pass", git(self.origin, "log", "--format=%s", "dev"))

    def test_a_grafts_file_cannot_fake_a_fast_forward(self):
        # dev moves on without the autopilot; a grafts file that gives the tip
        # dev's new commit as its parent would pass the ancestry check, and git
        # push would then trust the same grafted history and rewind dev.
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", "-b", "dev", self.origin, other], check=True, capture_output=True)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "o")
        with open(os.path.join(other, "operator.txt"), "w") as fh:
            fh.write("operator work\n")
        git(other, "add", "operator.txt")
        git(other, "commit", "-q", "-m", "operator work on dev")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/dev")
        dev_now = self.origin_ref("dev")
        git(self.wt, "fetch", "-q", "origin", "dev")
        tip = self.origin_ref("autopilot/dev")
        os.makedirs(os.path.join(self.wt, ".git", "info"), exist_ok=True)
        with open(os.path.join(self.wt, ".git", "info", "grafts"), "w") as fh:
            fh.write(f"{tip} {dev_now}\n")
        r = self.run_gate()
        self.assert_refused(r, "is not an ancestor of", dev_now)
        self.assertEqual(git(self.origin, "log", "-1", "--format=%s", "dev"), "operator work on dev")

    def test_github_must_agree_that_the_push_is_a_fast_forward(self):
        # The local ancestry check reads parents from the shared object store
        # without re-hashing them; GitHub's compare is asked as well.
        dev_before = self.origin_ref("dev")
        for compare, needle in (("diverged", "GitHub reports"), ("behind", "GitHub reports"),
                                ("", "GitHub reports"), (None, "could not compare")):
            with self.subTest(compare=compare):
                s = good()
                if compare is None:
                    s["fail_compare"] = True
                else:
                    s["compare"] = compare
                self.set_scenario(s)
                self.assert_refused(self.run_gate(), needle, dev_before)
        self.set_scenario({**good(), "compare": "identical"})
        self.assertEqual(self.run_gate("--dry-run").returncode, 0)

    def test_a_branch_named_like_dev_does_not_confuse_the_tips(self):
        # ls-remote matches patterns on the ref name's tail, so branches like
        # upgrade/refs/heads/dev are listed for refs/heads/dev too; only the
        # exact names count, before the push and after it.
        git(self.wt, "push", "-q", "origin", "HEAD~1:refs/heads/upgrade/refs/heads/dev",
            "HEAD~1:refs/heads/x/refs/heads/autopilot/dev")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MERGED", r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))


class GitHttpServer:
    """A smart-HTTP git server over `git http-backend`, like GitHub for these
    tests: fetches are anonymous (the repository is public) and a push needs
    Basic auth u:p. Every request is recorded with the Authorization it carried."""

    def __init__(self, root, bind="127.0.0.1", port=0):
        import base64
        import http.server
        import threading

        self.seen = []
        want = "Basic " + base64.b64encode(b"u:p").decode()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _run(self):
                path, _, query = self.path.partition("?")
                auth = self.headers.get("Authorization")
                outer.seen.append((self.command, self.path, auth))
                push = "git-receive-pack" in self.path
                if push and auth != want:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="test"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                env = {"PATH": "/usr/bin:/bin", "HOME": root, "GIT_CONFIG_NOSYSTEM": "1", "GIT_PROJECT_ROOT": root,
                       "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path, "QUERY_STRING": query,
                       "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1"}
                for header, var in (("Content-Encoding", "HTTP_CONTENT_ENCODING"), ("Git-Protocol", "GIT_PROTOCOL")):
                    if self.headers.get(header):
                        env[var] = self.headers[header]
                if push:
                    env["REMOTE_USER"] = "u"
                p = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                head, _, rest = p.stdout.partition(b"\r\n\r\n")
                status, headers = 200, []
                for line in head.split(b"\r\n"):
                    if line:
                        k, _, v = line.decode().partition(":")
                        if k.lower() == "status":
                            status = int(v.strip().split()[0])
                        else:
                            headers.append((k, v.strip()))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(rest)))
                self.end_headers()
                self.wfile.write(rest)

            do_GET = _run
            do_POST = _run

        self.httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def authenticated_pushes(self):
        return [p for (m, p, a) in self.seen if m == "POST" and "git-receive-pack" in p and a]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(os.path.exists(JQ), "the fake gh needs jq")
class HttpGate(GateHarness):
    """The gate against an http origin that wants a credential for the push.
    The gate copy's HOME is a temporary directory whose .gitconfig plays the
    operator's global git config; the real one is never read."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.srv = GitHttpServer(self.tmp)
        self.servers = [self.srv]
        self.url = f"http://127.0.0.1:{self.srv.port}/origin.git"
        self.gate = self.http_gate(self.url)

    def tearDown(self):
        for s in self.servers:
            s.close()
        super().tearDown()

    def http_gate(self, url, name="gate-http"):
        git(self.wt, "remote", "set-url", "origin", url)
        gate = self.make_gate(name=name, protocol="http")
        with open(gate) as fh:
            text = fh.read()
        for old, new in (('HOME=$(getent passwd "$(id -u)" | cut -d: -f6)', f"HOME={self.home}"),
                         (f"ORIGIN_URL={self.origin}", f"ORIGIN_URL={url}")):
            self.assertEqual(text.count("\n" + old + "\n"), 1, old)
            text = text.replace("\n" + old + "\n", "\n" + new + "\n")
        with open(gate, "w") as fh:
            fh.write(text)
        return gate

    def push_tip(self):
        # straight into the origin's directory: the test's own git has no credential
        git(self.wt, "push", "-q", self.origin, "HEAD:refs/heads/autopilot/dev")

    def new_tip(self, name):
        # something new to merge, so every run pushes (and needs the credential)
        self.commit(f"{name}.txt", name)
        self.push_tip()

    def operator_helper(self, shape="gh", host=None):
        store = os.path.join(self.home, "creds")
        host = host or f"127.0.0.1:{self.srv.port}"
        with open(store, "w") as fh:
            fh.write(f"http://u:p@{host}\n")
        if shape == "gh":  # what `gh auth setup-git` writes: a reset, then its helper, for one host
            text = f'[credential "http://{host}"]\n\thelper =\n\thelper = store --file {store}\n'
        else:
            text = f"[credential]\n\thelper = store --file {store}\n"
        with open(os.path.join(self.home, ".gitconfig"), "w") as fh:
            fh.write(text)

    def no_operator_helper(self):
        path = os.path.join(self.home, ".gitconfig")
        if os.path.exists(path):
            os.remove(path)

    def credential_script(self, name):
        marker = os.path.join(self.tmp, name + ".ran")
        path = os.path.join(self.tmp, name + ".sh")
        with open(path, "w") as fh:  # answers like a helper (get) and like askpass (Username/Password)
            fh.write(f"#!/bin/sh\necho \"$*\" >> '{marker}'\n"
                     "case \"$1\" in get) printf 'username=u\\npassword=p\\n' ;; Username*) echo u ;; Password*) echo p ;; esac\n"
                     "exit 0\n")
        os.chmod(path, 0o755)
        return marker, path

    def test_the_operators_global_helper_authenticates_the_push(self):
        for shape in ("gh", "generic"):
            with self.subTest(shape):
                self.operator_helper(shape)
                self.new_tip(shape)
                before = len(self.srv.authenticated_pushes())
                r = self.run_gate()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
                self.assertGreater(len(self.srv.authenticated_pushes()), before, "the push carried the credential")

    def test_without_the_operators_helper_the_push_is_refused_without_a_prompt(self):
        self.no_operator_helper()
        self.new_tip("no-helper")
        dev_before = self.origin_ref("dev")
        r = self.run_gate()
        self.assert_refused(r, "push to origin/dev failed", dev_before)
        self.assertIn("terminal prompts disabled", r.stderr)

    def test_a_repository_credential_helper_never_runs(self):
        port = self.srv.port
        cases = {
            "credential.helper appended": [("credential.helper", "!{script}")],
            "credential.helper reset, then its own": [("credential.helper", ""), ("credential.helper", "!{script}")],
            "URL-scoped to the host": [(f"credential.http://127.0.0.1:{port}.helper", "!{script}")],
            "URL-scoped to the repository": [(f"credential.{self.url}.helper", "!{script}")],
        }
        config = os.path.join(self.wt, ".git", "config")
        with open(config) as fh:
            pristine = fh.read()
        n = 0
        for operator in (True, False):
            for name, entries in cases.items():
                with self.subTest(name, operator_helper=operator):
                    n += 1
                    if operator:
                        self.operator_helper()
                    else:
                        self.no_operator_helper()
                    marker, script = self.credential_script(f"repo-helper-{n}")
                    try:
                        for key, value in entries:
                            git(self.wt, "config", "--add", key, value.format(script=script))
                        self.new_tip(f"helper-{n}")
                        dev_before = self.origin_ref("dev")
                        r = self.run_gate()
                        if operator:
                            self.assertEqual(r.returncode, 0, r.stderr)
                            self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
                        else:
                            self.assert_refused(r, "push to origin/dev failed", dev_before)
                        self.assertFalse(os.path.exists(marker), "a credential helper from the repository's configuration ran")
                    finally:
                        with open(config, "w") as fh:
                            fh.write(pristine)

    def test_a_repository_or_caller_askpass_never_runs(self):
        config = os.path.join(self.wt, ".git", "config")
        with open(config) as fh:
            pristine = fh.read()
        for operator in (True, False):
            with self.subTest("core.askPass", operator_helper=operator):
                if operator:
                    self.operator_helper()
                else:
                    self.no_operator_helper()
                marker, script = self.credential_script(f"askpass-{operator}")
                try:
                    git(self.wt, "config", "credential.helper", "")  # drops every helper read before it
                    git(self.wt, "config", "core.askPass", script)
                    self.new_tip(f"askpass-{operator}")
                    dev_before = self.origin_ref("dev")
                    r = self.run_gate()
                    if operator:
                        self.assertEqual(r.returncode, 0, r.stderr)
                        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
                    else:
                        self.assert_refused(r, "push to origin/dev failed", dev_before)
                    self.assertFalse(os.path.exists(marker), "core.askPass from the repository's configuration ran")
                finally:
                    with open(config, "w") as fh:
                        fh.write(pristine)
        with self.subTest("GIT_ASKPASS and SSH_ASKPASS from the caller"):
            self.no_operator_helper()
            marker, script = self.credential_script("caller-askpass")
            self.new_tip("caller-askpass")
            dev_before = self.origin_ref("dev")
            r = self.run_gate(env={"GIT_ASKPASS": script, "SSH_ASKPASS": script, "SSH_ASKPASS_REQUIRE": "force",
                                   "DISPLAY": ":0", "GIT_TERMINAL_PROMPT": "1"})
            self.assert_refused(r, "push to origin/dev failed", dev_before)
            self.assertFalse(os.path.exists(marker))

    def attacker(self, bind="127.0.0.2"):
        # another server on the origin's port, holding a copy of the origin
        root = os.path.join(self.tmp, "attacker")
        os.makedirs(root)
        subprocess.run(["git", "clone", "-q", "--bare", self.origin, os.path.join(root, "origin.git")], check=True)
        bad = GitHttpServer(root, bind=bind, port=self.srv.port)
        self.servers.append(bad)
        return bad

    def test_repository_configuration_cannot_send_the_push_or_credential_elsewhere(self):
        port = self.srv.port
        cases = [
            ("http.curloptresolve", "localhost", [("http.curloptResolve", f"localhost:{port}:127.0.0.2")]),
            ("http.<...>.curloptresolve", "localhost",
             [(f"http.http://localhost:{port}/origin.git.curloptResolve", f"localhost:{port}:127.0.0.2")]),
            ("remote.<...>.pushurl", "127.0.0.1",
             [(f"remote.http://127.0.0.1:{port}/origin.git.pushurl", f"http://127.0.0.2:{port}/origin.git")]),
        ]
        bad = self.attacker()
        config = os.path.join(self.wt, ".git", "config")
        for needle, host, entries in cases:
            with self.subTest(needle):
                url = f"http://{host}:{port}/origin.git"
                self.operator_helper("generic", host=f"{host}:{port}")
                gate = self.http_gate(url, name="gate-" + needle.replace("<...>", "x"))
                with open(config) as fh:
                    pristine = fh.read()
                try:
                    for key, value in entries:
                        git(self.wt, "config", key, value)
                    self.new_tip(needle.replace("<...>", "x"))
                    dev_before = self.origin_ref("dev")
                    r = self.run_gate(gate=gate)
                    self.assert_refused(r, f"sets {needle}, which", dev_before)
                    self.assertEqual(bad.seen, [], "the other server never heard from the gate")
                finally:
                    with open(config, "w") as fh:
                        fh.write(pristine)


class Syntax(unittest.TestCase):
    def test_the_gate_parses(self):
        # bash -n reads the script without running any of it
        r = subprocess.run(["/bin/bash", "-n", SCRIPT], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
