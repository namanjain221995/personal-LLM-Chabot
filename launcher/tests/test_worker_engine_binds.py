"""Where the cluster scripts put the unauthenticated worker engines.

The developer-platform audit (2026-09-13, F043/F051) reached the worker's OCR
(:30004) and speech (:30007) engines with no credential from the office LAN,
through a bind on the worker's management address, which scripts/ocr.sh and
scripts/whisper.sh read from enP7s7 over ssh. Commit 229031c moved both to
CLUSTER_WORKER_IP, the RoCE rail. The owner reverted that the same day
(option A): every consumer -- the head orchestrator's OCR_BASE_URL and
ASR_BASE_URLS, Prometheus's OCR scrape target -- dials the management address,
and the next `ocr.sh up` / `whisper.sh up` would have left them all dialling a
refused port. The engines stay on the management address and the host packet
filter closes the LAN (scripts/host-guard.sh, operator actions OA-4/OA-6).
What 229031c added and these tests keep: an empty or wildcard address stops
the script instead of binding nothing or everything.

Each bind function is extracted from its script and run in bash with a fake
``ssh`` on PATH that records the call and answers with a chosen address.
Nothing here touches Docker, a socket or the network.
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
MANAGEMENT_LAN_ADDRESS = "192.168.9.68"  # the worker's enP7s7 address; OCR/ASR consumers dial it
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
        # The answer comes from FAKE_SSH_ANSWER so a test can make the read
        # come back empty or as a wildcard; the default is what the worker's
        # enP7s7 carries in production.
        fake_ssh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.ssh_log}"\n'
            f'printf "%s" "${{FAKE_SSH_ANSWER-{MANAGEMENT_LAN_ADDRESS}}}"\n',
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

    def test_the_worker_ocr_and_speech_engines_bind_the_management_address_their_consumers_dial(self) -> None:
        # The head orchestrator dials http://192.168.9.68:30004/v1 and
        # :30007/v1; a rail bind (229031c) refuses every one of those calls.
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            with self.subTest(script=script):
                if self.ssh_log.exists():
                    self.ssh_log.unlink()
                result = self._bind_address(script, function, "worker", CLUSTER_WORKER_IP=RAIL_ADDRESS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, MANAGEMENT_LAN_ADDRESS)
                self.assertNotEqual(result.stdout, RAIL_ADDRESS)
                self.assertIn("enP7s7", self.ssh_log.read_text(encoding="utf-8"), "the address is read from the management interface")

    def test_an_empty_or_wildcard_management_address_stops_the_script_instead_of_binding_nothing_or_everything(self) -> None:
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            for answer in ("", "0.0.0.0", "::"):
                with self.subTest(script=script, ssh_answer=answer):
                    result = self._bind_address(
                        script, function, "worker", CLUSTER_WORKER_IP=RAIL_ADDRESS, FAKE_SSH_ANSWER=answer
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(result.stdout, "", "nothing may be printed as a bind address")
                    self.assertIn("enP7s7", result.stderr)

    def test_the_voice_archive_store_binds_the_management_address_the_orchestrator_dials(self) -> None:
        # The head orchestrator dials https://192.168.9.68:30011 (VOICE_ARCHIVE_URL,
        # written by the same script) and the certificate's IP SAN is this
        # address. Worker only: nothing new runs on the head.
        result = self._bind_address(
            "voice-store.sh", "voice_store_bind_address", "worker",
            VOICE_STORE_MANAGEMENT_IFNAME="enP7s7", CLUSTER_WORKER_IP=RAIL_ADDRESS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, MANAGEMENT_LAN_ADDRESS)
        self.assertIn("enP7s7", self.ssh_log.read_text(encoding="utf-8"), "the address is read from the management interface")

    def test_the_voice_archive_store_never_binds_nothing_everything_or_the_fabric(self) -> None:
        for answer in ("", "0.0.0.0", "::", "[::]", RAIL_ADDRESS, "10.100.185.2"):
            with self.subTest(ssh_answer=answer):
                result = self._bind_address(
                    "voice-store.sh", "voice_store_bind_address", "worker",
                    VOICE_STORE_MANAGEMENT_IFNAME="enP7s7", FAKE_SSH_ANSWER=answer,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "", "nothing may be printed as a bind address")
                self.assertIn("enP7s7", result.stderr)

    def test_a_candidate_voice_store_never_rewrites_the_production_stores_compose_file(self) -> None:
        # lib/cluster-common.sh sets WORKER_REMOTE_DIR itself, so a candidate
        # that took its directory from it ran `docker compose` out of, and
        # copied its compose file over, the production store's
        # ~/.techsara-cluster/compose.voice-store.yaml (candidate run,
        # 2026-09-30). The whole script runs here, every ssh call recorded.
        def compose_dirs(*args: str, **env: str) -> list[str]:
            if self.ssh_log.exists():
                self.ssh_log.unlink()
            result = subprocess.run(
                ["bash", str(SCRIPTS / "voice-store.sh"), "down", *args],
                env={
                    "PATH": self.path,
                    "HOME": str(self.root),
                    "CLUSTER_MODE": "dual",
                    "CLUSTER_WORKER_SSH": f"techsphere@{MANAGEMENT_LAN_ADDRESS}",
                    **env,
                },
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = self.ssh_log.read_text(encoding="utf-8")
            return re.findall(r"-- cd (\S+) && .*docker compose", log)

        self.assertEqual(compose_dirs(), ["$HOME/.techsara-cluster"])
        self.assertEqual(
            compose_dirs("--candidate", VOICE_STORE_PORT="30195"),
            ["$HOME/.techsara-cluster/candidates/voice-store-candidate"],
        )
        self.assertEqual(
            compose_dirs("--candidate", VOICE_STORE_PORT="30195", VOICE_STORE_PROJECT="trackb-voice-store"),
            ["$HOME/.techsara-cluster/candidates/trackb-voice-store"],
        )

    def test_a_rotated_voice_archive_token_never_appears_on_a_command_line(self) -> None:
        # /proc/<pid>/cmdline is readable by every user of the head (no
        # hidepid), and rotate-token put the new token there: `sed -i
        # "s|...|VOICE_ARCHIVE_TOKEN=${new}|"` (review 2026-09-30). It goes
        # through stdin now, like the worker's copy. The function runs here
        # with a python3 on PATH that records its own arguments.
        script = SCRIPTS / "voice-store.sh"
        self.assertNotRegex(script.read_text(encoding="utf-8"), r"sed -i[^\n]*\$\{?new")
        real_python = shutil.which("python3")
        self.assertIsNotNone(real_python)
        argv_log = self.root / "argv.log"
        shim = self.root / "bin" / "python3"
        shim.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{argv_log}"\nexec "{real_python}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        secrets_env = self.root / "secrets.env"
        secrets_env.write_text("# kept\nOTHER=1\nVOICE_ARCHIVE_TOKEN=old-token-" + "c" * 40 + "\n", encoding="utf-8")
        secrets_env.chmod(0o600)
        token = "new-token-" + "d" * 40  # low entropy: secret scanners flag random-looking literals
        program = "\n".join([
            "set -euo pipefail",
            _function_source(script, "set_secret"),
            'printf "%s" "$TOKEN_UNDER_TEST" | set_secret "$1" VOICE_ARCHIVE_TOKEN',
        ])
        result = subprocess.run(
            ["bash", "-c", program, "set-secret-test", str(secrets_env)],
            env={"PATH": self.path, "HOME": str(self.root), "TOKEN_UNDER_TEST": token},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(secrets_env.read_text(encoding="utf-8"), f"# kept\nOTHER=1\nVOICE_ARCHIVE_TOKEN={token}\n")
        self.assertEqual(secrets_env.stat().st_mode & 0o777, 0o600)
        arguments = argv_log.read_text(encoding="utf-8")
        self.assertIn("VOICE_ARCHIVE_TOKEN", arguments, "python3 did the replacement")
        self.assertNotIn(token, arguments)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".secrets.")], [])

    def _cpu_bind_address(self, **env: str) -> subprocess.CompletedProcess[str]:
        """scripts/whisper-cpu.sh's bind_address: worker only, so it takes no node argument."""
        program = "\n".join(
            [
                "set -euo pipefail",
                'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
                "ssh_worker() { ssh -o BatchMode=yes \"$CLUSTER_WORKER_SSH\" -- \"$@\"; }",
                'WHISPER_MANAGEMENT_IFNAME="${WHISPER_MANAGEMENT_IFNAME:-enP7s7}"',
                _function_source(SCRIPTS / "whisper-cpu.sh", "bind_address"),
                "bind_address",
            ]
        )
        environment = {
            "PATH": self.path,
            "HOME": str(self.root),
            "CLUSTER_MODE": "dual",
            "CLUSTER_WORKER_SSH": f"techsphere@{MANAGEMENT_LAN_ADDRESS}",
            **env,
        }
        return subprocess.run(["bash", "-c", program], env=environment, capture_output=True, text=True, timeout=30)

    def test_the_cpu_speech_replica_binds_the_management_address_like_the_gpu_replica(self) -> None:
        result = self._cpu_bind_address(CLUSTER_WORKER_IP=RAIL_ADDRESS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, MANAGEMENT_LAN_ADDRESS)
        self.assertIn("enP7s7", self.ssh_log.read_text(encoding="utf-8"))

    def test_the_cpu_speech_replica_refuses_an_empty_wildcard_or_rail_address(self) -> None:
        for answer in ("", "0.0.0.0", "::", RAIL_ADDRESS):
            with self.subTest(ssh_answer=answer):
                result = self._cpu_bind_address(FAKE_SSH_ANSWER=answer)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "", "nothing may be printed as a bind address")

    def test_the_head_engines_still_bind_the_docker_bridge_gateway(self) -> None:
        for script, function in (("ocr.sh", "ocr_bind_address"), ("whisper.sh", "whisper_bind_address")):
            with self.subTest(script=script):
                result = self._bind_address(script, function, "head", CLUSTER_WORKER_IP=RAIL_ADDRESS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "172.17.0.1")

    def _probe(self, generated_bind: str | None) -> str:
        """cluster-common.sh's CLUSTER_API_BIND_ADDRESS and api_url, as the
        cluster-*.sh scripts see them, for a generated.env holding
        ``generated_bind`` (None: no generated.env at all)."""
        repo = self.root / f"repo-{generated_bind or 'none'}"
        lib = repo / "scripts" / "lib"
        lib.mkdir(parents=True)
        shutil.copy(SCRIPTS / "lib" / "cluster-common.sh", lib / "cluster-common.sh")
        if generated_bind is not None:
            (repo / ".runtime").mkdir()
            (repo / ".runtime" / "generated.env").write_text(
                f"CLUSTER_API_BIND_ADDRESS={generated_bind}\n", encoding="utf-8"
            )
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
        return result.stdout

    def test_the_scripts_probe_loopback_when_generated_env_names_no_head_bind(self) -> None:
        self.assertEqual(self._probe(None), "127.0.0.1|http://127.0.0.1:8000")

    def test_the_scripts_probe_the_wildcard_head_the_launcher_generates_on_loopback(self) -> None:
        # The launcher's dual-mode bind; a probe of http://0.0.0.0 would be a
        # URL no operator could copy, so api_host maps it to 127.0.0.1.
        self.assertEqual(cluster.DEFAULT_API_BIND_ADDRESS, "0.0.0.0")
        self.assertEqual(self._probe(cluster.DEFAULT_API_BIND_ADDRESS), "0.0.0.0|http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
