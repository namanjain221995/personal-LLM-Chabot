"""Tests for ops/deploy/merge_to_dev.sh against a throwaway origin and a fake `gh`.

Run: python3 -m pytest ops/autopilot/tests -q -p no:cacheprovider
Nothing here talks to GitHub; every repository lives in a temporary directory.
The gate takes no settings from its environment, so each test runs a copy whose
configuration block points at the temporary repositories and the fake gh.
"""

import json
import os
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

    def make_gate(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        subs = {
            "GH=/usr/bin/gh": f"GH={self.gh}",
            "REPO=$HOME/work/llm-dev": f"REPO={self.wt}",
            "ORIGIN_URL=https://github.com/namanjain221995/personal-LLM-Chabot.git": f"ORIGIN_URL={self.origin}",
            "LOG=$HOME/.llm-autopilot/logs/merge_to_dev.log": f"LOG={self.log}",
            "CI_APPROVALS=$HOME/.llm-autopilot/approved-ci-trees": f"CI_APPROVALS={self.approvals}",
        }
        for old, new in subs.items():
            self.assertEqual(text.count("\n" + old + "\n"), 1, old)
            text = text.replace("\n" + old + "\n", "\n" + new + "\n")
        path = os.path.join(self.tmp, "gate", "merge_to_dev.sh")
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

    def run_gate(self, *args, env=None):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
        e.update(env or {})
        return subprocess.run([self.gate, *args], env=e, capture_output=True, text=True, timeout=60)

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


if __name__ == "__main__":
    unittest.main()
