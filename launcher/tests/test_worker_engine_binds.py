"""Where the cluster scripts put the unauthenticated engines (audit F043/F050/F051).

The developer-platform audit (2026-09-13) reached three model APIs with no
credential from the office LAN and the tailnet: the main engine through a
0.0.0.0 bind, and the worker's OCR (:30004) and speech (:30007) engines
through a bind on the worker's management address, which scripts/ocr.sh and
scripts/whisper.sh read from enP7s7 over ssh. The worker engines now bind
CLUSTER_WORKER_IP, the RoCE rail between the two Sparks, and the scripts'
fallback for the head's API bind is loopback, agreeing with the launcher.

Each bind function is extracted from its script and run in bash with a fake
``ssh`` on PATH that records any call and answers with the LAN address the
audit measured, so a regression to the management-interface read shows up as
that address in the output. Nothing here touches Docker, a socket or the
network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from techsara_cli import cluster

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
MANAGEMENT_LAN_ADDRESS = "192.168.9.68"  # the worker's enP7s7 address the audit probed
RAIL_ADDRESS = "10.100.184.2"


def _function_source(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{script.name} has no function {name}")
    return match.group(0)


class WorkerEngineBindTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bash") is None:
            self.skipTest("bash is required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-binds-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.ssh_log = self.root / "ssh.log"
        fake_ssh = bin_dir / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.ssh_log}"\n'
            f'printf "{MANAGEMENT_LAN_ADDRESS}\\n"\n',
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def _bind_address(self, script: str, function: str, node: str, **env: str) -> subprocess.CompletedProcess[str]:
        program = "\n".join(
            [
                "set -euo pipefail",
                'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
                'is_dual_mode() { [ "${CLUSTER_MODE:-single}" = "dual" ]; }',
                "ssh_worker() { ssh -o BatchMode=yes \"$CLUSTER_WORKER_SSH\" -- \"$@\"; }",
                _function_source(SCRIPTS / script, function),
                f'{function} "$1"',
            ]
        )
        environment = {
            "PATH": self.path,
            "HOME": str(self.root),
            "CLUSTER_MODE": "dual",
            "CLUSTER_WORKER_SSH": f"techsphere@{MANAGEMENT_LAN_ADDRESS}",
            **env,
        }
        return subprocess.run(
            ["bash", "-c", program, "bind-test", node],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_the_worker_ocr_and_speech_engines_bind_the_rail_address_not_the_management_lan(self) -> None:
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            with self.subTest(script=script):
                result = self._bind_address(script, function, "worker", CLUSTER_WORKER_IP=RAIL_ADDRESS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, RAIL_ADDRESS)
                self.assertNotIn(MANAGEMENT_LAN_ADDRESS, result.stdout)
                self.assertFalse(
                    self.ssh_log.exists(),
                    "the bind must not come from reading a worker interface over ssh",
                )

    def test_a_missing_or_wildcard_worker_address_stops_the_script_instead_of_binding_wider(self) -> None:
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            for value in ("", "0.0.0.0", "::"):
                with self.subTest(script=script, CLUSTER_WORKER_IP=value):
                    result = self._bind_address(script, function, "worker", CLUSTER_WORKER_IP=value)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(result.stdout, "", "nothing may be printed as a bind address")
                    self.assertIn("CLUSTER_WORKER_IP", result.stderr)

    def test_the_head_engines_still_bind_the_docker_bridge_gateway(self) -> None:
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            with self.subTest(script=script):
                result = self._bind_address(script, function, "head", CLUSTER_WORKER_IP=RAIL_ADDRESS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "172.17.0.1")

    def test_the_scripts_fall_back_to_loopback_for_the_head_api_bind_like_the_launcher(self) -> None:
        """cluster-common.sh defaulted CLUSTER_API_BIND_ADDRESS to 0.0.0.0, the
        wildcard audit F050 reached from the LAN; the launcher's fallback is
        now 127.0.0.1 and the scripts must agree with it."""
        lib = self.root / "repo" / "scripts" / "lib"
        lib.mkdir(parents=True)
        shutil.copy(SCRIPTS / "lib" / "cluster-common.sh", lib / "cluster-common.sh")
        program = (
            f'. "{lib / "cluster-common.sh"}"\n'
            "cluster_load_settings\n"
            'printf "%s|%s" "$CLUSTER_API_BIND_ADDRESS" "$(api_url)"\n'
        )
        result = subprocess.run(
            ["bash", "-c", program],
            env={"PATH": self.path, "HOME": str(self.root), "NO_COLOR": "1"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "127.0.0.1|http://127.0.0.1:8000")
        self.assertNotIn("0.0.0.0", result.stdout)
        self.assertEqual(result.stdout.split("|")[0], cluster.DEFAULT_API_BIND_ADDRESS)


if __name__ == "__main__":
    unittest.main()
