"""The shell side of the round-2 sre findings, run for real where it can be.

The cluster helpers stay bash (they drive ssh, rsync and Docker on the worker
host), so the pieces the launcher relies on are proven here by extracting the
function under test from the script and running it in a throwaway root with a
fake ``docker`` on PATH that records its argv. Nothing here touches Docker, a
socket, or the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
REQUIRED_CHAIN = (
    "compose.yaml",
    "compose/compose.dgx-spark.yaml",
    "compose/compose.published-dgx-spark.yaml",
    "compose/compose.cluster-dgx-spark.yaml",
)


def _function_source(script: Path, name: str) -> str:
    """The text of one top-level ``name() { ... }`` bash function."""
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{script.name} has no function {name}")
    return match.group(0)


class ClusterScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bash") is None or shutil.which("sha256sum") is None:
            self.skipTest("bash and coreutils are required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-scripts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    # -- (1) the sentinel's code digest -----------------------------------
    def test_cluster_sync_sentinel_code_sha_matches_the_launchers_scheme(self) -> None:
        """Review round 2 (sre, major): the sentinel's program is a bind
        mount, so a code-only change never recreated it. cluster-sync.sh now
        writes SENTINEL_CODE_SHA (sha256 of sentinel.py then common.py as ONE
        stream) into worker.env; the head's controller uses the same scheme
        (environment.controller_code_sha), so the two never disagree."""
        program = self.root / "engine-controller"
        program.mkdir()
        (program / "sentinel.py").write_bytes(b"print('sentinel v1')\n")
        (program / "common.py").write_bytes(b"VERSION = 1\n")
        function = _function_source(SCRIPTS / "cluster-sync.sh", "sentinel_code_sha")
        script = (
            "set -euo pipefail\n"
            f"SENTINEL_SRC_DIR={json.dumps(str(program))}\n"
            "SENTINEL_FILES=(sentinel.py common.py)\n"
            f"{function}\n"
            "sentinel_code_sha\n"
        )
        first = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(first, hashlib.sha256(b"print('sentinel v1')\nVERSION = 1\n").hexdigest())
        # Same bytes, same digest (no recreate); one changed byte, a new one.
        again = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(again, first)
        (program / "sentinel.py").write_bytes(b"print('sentinel v2')\n")
        changed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        self.assertNotEqual(changed, first)
        # The launcher's controller digest over the same two files (renamed
        # controller.py/common.py) is the same computation.
        from techsara_cli.environment import controller_code_sha

        launcher_root = self.root / "launcher-root"
        (launcher_root / "monitoring" / "engine-controller").mkdir(parents=True)
        (launcher_root / "monitoring" / "engine-controller" / "controller.py").write_bytes(b"print('sentinel v2')\n")
        (launcher_root / "monitoring" / "engine-controller" / "common.py").write_bytes(b"VERSION = 1\n")
        self.assertEqual(controller_code_sha(launcher_root), changed)

    def test_cluster_sync_ships_the_digest_the_age_guard_and_the_sentinel_image_on_a_routine_deploy(self) -> None:
        """The worker.env written by cluster-sync.sh carries SENTINEL_CODE_SHA
        and the launcher-resolved VLLM_HEALTHCHECK_MIN_AGE_S, and --env-only
        (the routine deploy path) runs the sentinel-image stage before naming
        the image (review round 2, minor: a Docker Hub pull mid-deploy with
        no save/load fallback rolled a frontend-only deploy back)."""
        text = (SCRIPTS / "cluster-sync.sh").read_text(encoding="utf-8")
        plain_keys = re.search(r"^WORKER_PLAIN_KEYS='([^']+)'", text, re.M)
        self.assertIsNotNone(plain_keys)
        self.assertIn("VLLM_HEALTHCHECK_MIN_AGE_S", plain_keys.group(1).split("|"))
        self.assertIn("VLLM_HEALTHCHECK_KILL_AFTER", plain_keys.group(1).split("|"))
        self.assertIn('echo "SENTINEL_CODE_SHA=$sentinel_sha"', text)
        # The env-only branch calls the image stage; the image stage is one
        # function, so the two paths cannot drift apart.
        env_only = re.search(r'if \[ "\$DO_IMAGE" != 1 \]; then\n(.*?)\n  fi\n', text, re.S)
        self.assertIsNotNone(env_only)
        self.assertIn("ensure_sentinel_image", env_only.group(1))
        self.assertEqual(text.count("ensure_sentinel_image\n"), 2, "the image stage and --env-only both run it")

    # -- (3) the head is recreated from its CURRENT definition ---------------
    def _throwaway_root_with_chain(self, files: tuple[str, ...]) -> tuple[Path, list[str]]:
        """A project root holding copies of the compose files, a state.json
        recording them the way the launcher does, and a fake docker on PATH."""
        root = self.root / "project"
        for relative in REQUIRED_CHAIN:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, target)
        (root / ".runtime").mkdir()
        chain = ["docker", "compose", "--project-name", "sf-local-ai",
                 "--env-file", str(root / ".env"),
                 "--env-file", str(root / ".runtime" / "secrets.env"),
                 "--env-file", str(root / ".runtime" / "generated.env")]
        for relative in files:
            chain += ["-f", str(root / relative)]
        chain += ["--profile", "admin", "up", "-d"]
        (root / ".runtime" / "state.json").write_text(json.dumps({"compose_command": chain}), encoding="utf-8")
        fake_bin = self.root / "bin"
        fake_bin.mkdir(exist_ok=True)
        log = self.root / "docker-argv.log"
        (fake_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" >> {json.dumps(str(log))}\n"
            "printf -- '--\\n' >> " + json.dumps(str(log)) + "\n",
            encoding="utf-8",
        )
        (fake_bin / "docker").chmod(0o755)
        return root, chain

    def _run_head_compose_current(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        function = _function_source(SCRIPTS / "cluster-recover.sh", "head_compose_current")
        script = (
            "set -eo pipefail\n"
            f"ROOT={json.dumps(str(root))}\n"
            f". {json.dumps(str(SCRIPTS / 'lib' / 'deploy-common.sh'))}\n"
            f"{function}\n"
            "head_compose_current \"$@\"\n"
        )
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ.get('PATH', '')}", TECHSARA_DEPLOY_ROOT=str(root))
        return subprocess.run(["bash", "-c", script, "_", *args], capture_output=True, text=True, env=env, check=False)

    def test_cluster_recover_recreates_the_head_with_the_launchers_verified_chain(self) -> None:
        """Review round 2 (sre, major): --clear-kernel-cache recreated the
        worker from the CURRENT worker.env but `docker start`ed the head's OLD
        container, so after a routine deploy that changed an engine argument
        the two ranks started with different configurations. The head is now
        started with `up -d --no-deps vllm` on the chain state.json recorded
        (deploy-common.sh dr_compose_prefix), the same verb the worker gets."""
        root, chain = self._throwaway_root_with_chain(REQUIRED_CHAIN)
        result = self._run_head_compose_current(root, "up", "-d", "--no-deps", "vllm")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = (self.root / "docker-argv.log").read_text(encoding="utf-8").split("\n--\n")[0].split("\n")
        self.assertEqual(argv, chain[1:chain.index("up")] + ["up", "-d", "--no-deps", "vllm"])
        # The script itself never `docker start`s the head any more, and the
        # by-hand path is the one that says so.
        text = (SCRIPTS / "cluster-recover.sh").read_text(encoding="utf-8")
        self.assertNotIn('docker start "$HEAD_CTR"', text)
        self.assertIn("head_compose_current up -d --no-deps vllm", text)
        self.assertIn(". \"$CLUSTER_LIB_DIR/deploy-common.sh\"", text)

    def test_cluster_recover_refuses_a_subset_chain_before_touching_docker(self) -> None:
        """A state.json missing the cluster overlay would render the head as a
        single-node service; dr_compose_prefix refuses it and nothing runs."""
        root, _chain = self._throwaway_root_with_chain(REQUIRED_CHAIN[:3])
        result = self._run_head_compose_current(root, "up", "-d", "--no-deps", "vllm")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SUBSET", result.stderr)
        self.assertFalse((self.root / "docker-argv.log").exists(), "no docker call on a refused chain")

    # -- (9) shellcheck: cmd vs the array in cluster-common.sh ----------------
    def test_cluster_worker_uses_action_not_the_arrays_name(self) -> None:
        text = (SCRIPTS / "cluster-worker.sh").read_text(encoding="utf-8")
        self.assertIn('action="${1:-status}"', text)
        self.assertIn('case "$action" in', text)
        self.assertNotRegex(text, r'^cmd=', "cluster-common.sh's head_compose declares a local array named cmd")

    # -- (10) .gitignore: a node_modules symlink is ignored too --------------
    def test_gitignore_ignores_a_node_modules_symlink(self) -> None:
        """Review round 2 (sre, minor): `node_modules/` matches a directory
        only, so a frontend/node_modules symlink showed up as untracked."""
        if shutil.which("git") is None:
            self.skipTest("git is required")
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        shutil.copyfile(REPO_ROOT / ".gitignore", repo / ".gitignore")
        (repo / "frontend").mkdir()
        (repo / "real").mkdir()
        (repo / "frontend" / "node_modules").symlink_to("../real")
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "-uall", "--ignored"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("!! frontend/node_modules", status)
        self.assertNotIn("?? frontend/node_modules", status)


if __name__ == "__main__":
    unittest.main()
