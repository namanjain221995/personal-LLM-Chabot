"""Tests for ops/deploy/merge_to_dev.sh against a throwaway origin and a fake `gh`.

Run: python3 -m unittest discover -s ops/autopilot/tests -v
Nothing here talks to GitHub; every repository lives in a temporary directory.
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

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    scenario = json.load(open(os.environ["FAKE_GH_SCENARIO"]))
    args = sys.argv[1:]
    path = next((a for a in args if a.startswith("repos/")), "")
    if path.split("?")[0].endswith("/check-runs"):
        for name, status, conclusion in scenario["runs"]:
            print("\\t".join([name, status, conclusion or "none"]))
    elif path.endswith("/status"):
        print("\\t".join([scenario.get("state", "pending"), str(scenario.get("count", 0))]))
    else:
        sys.exit(1)
    """
)

GOOD_RUNS = [["CI passed", "completed", "success"], ["Backend quality (orchestrator, shard 1 of 3)", "completed", "success"], ["Deploy", "completed", "skipped"]]


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


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
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/dev")
        git(self.wt, "checkout", "-q", "-b", "autopilot/dev")
        self.commit("work.txt", "autopilot work")
        os.makedirs(os.path.join(self.wt, "docs/ai-platform-upgrade"))
        self.commit("docs/ai-platform-upgrade/FINAL_REPORT.md", "final report")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/autopilot/dev")
        # The test origin is a local path; the script derives the slug from the URL.
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "gh"), "w") as fh:
            fh.write(FAKE_GH)
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.set_scenario(GOOD_RUNS, "success", 0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, rel, msg):
        path = os.path.join(self.wt, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(msg + "\n")
        git(self.wt, "add", rel)
        git(self.wt, "commit", "-q", "-m", msg)

    def set_scenario(self, runs, state, count):
        with open(self.scenario, "w") as fh:
            json.dump({"runs": runs, "state": state, "count": count}, fh)

    def run_script(self, *args):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_GH_SCENARIO=self.scenario,
                   MERGE_TO_DEV_REPO=self.wt, MERGE_TO_DEV_LOG=os.path.join(self.tmp, "mtd.log"))
        return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60)

    def origin_ref(self, ref):
        return git(self.origin, "rev-parse", ref)

    def test_happy_path_fast_forwards_dev_and_never_touches_main(self):
        main_before = self.origin_ref("main")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), self.origin_ref("autopilot/dev"))
        self.assertEqual(self.origin_ref("main"), main_before)

    def test_dry_run_pushes_nothing(self):
        dev_before = self.origin_ref("dev")
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.origin_ref("dev"), dev_before)

    def test_refuses_without_final_report(self):
        git(self.wt, "rm", "-q", "docs/ai-platform-upgrade/FINAL_REPORT.md")
        git(self.wt, "commit", "-q", "-m", "drop report")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/autopilot/dev")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FINAL_REPORT.md", r.stderr)

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
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not an ancestor", r.stderr)

    def test_refuses_commit_that_is_not_the_pushed_tip(self):
        self.commit("local.txt", "local only")
        r = self.run_script(git(self.wt, "rev-parse", "HEAD"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pushed tip", r.stderr)

    def test_refuses_failed_missing_or_pending_checks(self):
        cases = [
            ([["CI passed", "completed", "failure"]], "success", 0, "not passed"),
            ([], "success", 0, "no check runs"),
            ([["Backend", "completed", "success"]], "success", 0, "CI passed"),
            ([["CI passed", "in_progress", None]], "success", 0, "not passed"),
            (GOOD_RUNS, "failure", 2, "combined commit status"),
        ]
        for runs, state, count, needle in cases:
            with self.subTest(needle=needle, runs=runs):
                self.set_scenario(runs, state, count)
                dev_before = self.origin_ref("dev")
                r = self.run_script()
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(needle, r.stderr)
                self.assertEqual(self.origin_ref("dev"), dev_before)


if __name__ == "__main__":
    unittest.main()
