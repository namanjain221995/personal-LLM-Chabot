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
class MergeToDev(unittest.TestCase):
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
        wrapper = re.search(r"\ng\(\) \{\n(.*?)\n\}\n", text, re.S)
        self.assertIsNotNone(wrapper, "one git wrapper, g()")
        for flag in ("--no-pager", "--no-replace-objects", "-c core.hooksPath=/dev/null", "-c core.fsmonitor=false",
                     "-c core.alternateRefsCommand=true", "-c push.gpgSign=false", "-c maintenance.auto=false",
                     "-c gc.auto=0", '--git-dir="$git_dir"'):
            self.assertIn(flag, wrapper.group(1), flag)
        for call in ("g fetch ", "g push "):
            lines = [ln for ln in text.splitlines() if ln.startswith(call)]
            self.assertEqual(len(lines), 1, call)
            self.assertIn("--no-recurse-submodules", lines[0], call)
        # $GIT, ${GIT} or "$GIT" is used once, inside the wrapper, and nothing
        # outside the configuration line names the git binary or runs git bare
        uses = re.findall(r"\$\{?GIT\}?(?![A-Za-z0-9_])", text)
        self.assertEqual(len(uses), 1, "git runs only through the one hardened wrapper")
        self.assertIn('"$GIT"', wrapper.group(1))
        self.assertEqual(text.count("/usr/bin/git"), 1, "the binary is named only in the configuration block")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        bare = [ln for ln in code if re.search(r"(?:^|[;&|({]|\$\()\s*(?:command\s+|exec\s+|env\s+|\\)?git(?:\s|$)", ln)]
        self.assertEqual(bare, [], "git is called outside the wrapper")


class Syntax(unittest.TestCase):
    def test_the_gate_parses(self):
        # bash -n reads the script without running any of it
        r = subprocess.run(["/bin/bash", "-n", SCRIPT], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
