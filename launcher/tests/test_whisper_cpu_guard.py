"""scripts/whisper-cpu.sh never starts the CPU speech replica on an unguarded port.

The replica has no authentication, like the GPU replica on 30007. The worker's packet filter
(scripts/host-guard.sh) judges ONLY the ports it lists (`tcp dport != @guarded_ports accept`), so
a port missing from its list is open to the office LAN and the tailnet. The filter loaded on the
worker since 2026-09-27 lists {9100, 9835, 9839, 30004, 30007}, not 30008. And it is loaded twice
over: by `host-guard.sh apply` now, and by the boot copy (`install-boot`) at every reboot. A guard
applied from the new script but never installed for boot reopens 30008 at the next reboot, while
`restart: unless-stopped` brings the replica straight back.

So `up` reads both, without root: the table loaded now (/run/techsara-host-guard/ruleset.nft,
0644 by design) and the boot copy's own `plan`. Each must judge 30008 and let the head through.
The functions are extracted from the script and run in bash with a fake ``ssh`` that answers each
read from a file. Nothing here touches Docker, a socket, the network or a real host guard.
"""

from __future__ import annotations

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

SCRIPT = REPO_ROOT / "scripts" / "whisper-cpu.sh"
GUARD = REPO_ROOT / "scripts" / "host-guard.sh"
PORT = "30008"

#: The worker's table as loaded on 2026-09-27 (host-guard.sh's rendered form, sets only): what
#: /run/techsara-host-guard/ruleset.nft held when this replica was built.
GUARD_2026_09_27 = """\
table inet techsara_guard {
  set guarded_ports {
    type inet_service
    flags interval
    elements = { 9100, 9835, 9839, 30004, 30007 }
  }

  set head_lan_ports {
    type inet_service
    flags interval
    elements = { 9100, 9835, 30004, 30007 }
  }

  chain input {
    type filter hook input priority -10; policy accept;
    tcp dport != @guarded_ports accept comment "only the engine ports are judged"
  }
}
"""


def _function_source(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    # The definition line may carry a usage comment after the brace.
    match = re.search(rf"^{re.escape(name)}\(\) \{{[^\n]*\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{SCRIPT.name} has no function {name}")
    return match.group(0)


def _assignment(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=.*$", SCRIPT.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise AssertionError(f"{SCRIPT.name} does not set {name}")
    return match.group(0)


class TheReplicaIsNeverStartedOnAnUnguardedPortTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bash") is None or shutil.which("awk") is None:
            self.skipTest("bash and awk are required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-wcpu-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.applied = self.root / "applied.nft"
        self.boot = self.root / "boot.nft"
        fake_ssh = bin_dir / "ssh"
        # Each read answers from its own file; a missing file answers nothing, like a worker
        # with no guard at all.
        fake_ssh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.ssh_log}"\n'
            'case "$*" in\n'
            f'  *ruleset.nft*) cat "{self.applied}" 2>/dev/null ;;\n'
            f'  *"plan --role worker"*) cat "{self.boot}" 2>/dev/null ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    # -- helpers ----------------------------------------------------------------------------------
    def rendered_worker_guard(self) -> str:
        """This checkout's worker ruleset, rendered by host-guard.sh itself (no root)."""
        state = self.root / "state"
        result = subprocess.run(
            ["bash", str(GUARD), "plan", "--role", "worker"],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root),
                "LC_ALL": "C",
                "GUARD_STATE_DIR": str(state),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("set guarded_ports", result.stdout)
        return result.stdout

    def guards(self, ruleset: str, port: str = PORT) -> bool:
        program = "\n".join(["set -euo pipefail", _function_source("ruleset_guards_port"), 'ruleset_guards_port "$1"'])
        result = subprocess.run(
            ["bash", "-c", program, "guard-test", port],
            input=ruleset,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": self.path, "LC_ALL": "C"},
        )
        return result.returncode == 0

    def require_host_guard(self) -> subprocess.CompletedProcess[str]:
        program = "\n".join(
            [
                "set -euo pipefail",
                'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
                'check_pass() { printf "PASS %s\\n" "$*"; }',
                'ssh_worker() { ssh -o BatchMode=yes "$CLUSTER_WORKER_SSH" -- "$@"; }',
                f"WHISPER_CPU_PORT={PORT}",
                _assignment("GUARD_APPLIED_RULESET"),
                _assignment("GUARD_BOOT_COPY"),
                _assignment("GUARD_OWNER_STEPS"),
                _function_source("ruleset_guards_port"),
                _function_source("require_host_guard"),
                "require_host_guard",
            ]
        )
        return subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": self.path,
                "HOME": str(self.root),
                "LC_ALL": "C",
                "CLUSTER_WORKER_SSH": "techsphere@192.168.9.68",
            },
        )

    # -- reading a ruleset ------------------------------------------------------------------------
    def test_the_guard_this_checkout_ships_closes_the_replicas_port(self) -> None:
        ruleset = self.rendered_worker_guard()
        self.assertTrue(self.guards(ruleset))
        # Not vacuous: a port the guard does not list is refused on the same text.
        self.assertFalse(self.guards(ruleset, "30200"))

    def test_the_guard_loaded_on_the_worker_since_2026_09_27_does_not(self) -> None:
        self.assertFalse(self.guards(GUARD_2026_09_27))
        self.assertTrue(self.guards(GUARD_2026_09_27, "30007"), "the GPU replica's port is guarded there")

    def test_a_port_that_is_guarded_but_closed_to_the_head_is_refused(self) -> None:
        # The orchestrator could not reach it, so starting it would only look like a deploy.
        ruleset = GUARD_2026_09_27.replace("9839, 30004, 30007 }", "9839, 30004, 30007, 30008 }")
        self.assertIn("30008", ruleset)
        self.assertFalse(self.guards(ruleset))

    def test_port_ranges_count_and_no_guard_at_all_is_refused(self) -> None:
        ranged = GUARD_2026_09_27.replace("{ 9100, 9835, 9839, 30004, 30007 }", "{ 9100, 30000-30010 }").replace(
            "{ 9100, 9835, 30004, 30007 }", "{ 30004-30008 }"
        )
        self.assertTrue(self.guards(ranged))
        self.assertFalse(self.guards(ranged, "30011"))
        self.assertFalse(self.guards(""))

    # -- what `up` does with the two readings -----------------------------------------------------
    def test_a_worker_whose_loaded_table_misses_the_port_is_refused_with_the_owners_steps(self) -> None:
        self.applied.write_text(GUARD_2026_09_27, encoding="utf-8")
        self.boot.write_text(self.rendered_worker_guard(), encoding="utf-8")
        result = self.require_host_guard()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("does not close port 30008", result.stderr)
        for step in ("install-boot --role worker", "apply --role worker", "verify --role worker"):
            self.assertIn(step, result.stderr)
        self.assertNotIn("PASS", result.stdout)

    def test_a_guard_applied_but_not_installed_for_boot_is_refused(self) -> None:
        self.applied.write_text(self.rendered_worker_guard(), encoding="utf-8")
        self.boot.write_text(GUARD_2026_09_27, encoding="utf-8")
        result = self.require_host_guard()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("boot copy", result.stderr)
        self.assertIn("install-boot --role worker", result.stderr)

    def test_a_worker_with_no_guard_is_refused(self) -> None:
        result = self.require_host_guard()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("does not close port 30008", result.stderr)

    def test_a_worker_guarded_now_and_after_a_reboot_passes(self) -> None:
        ruleset = self.rendered_worker_guard()
        self.applied.write_text(ruleset, encoding="utf-8")
        self.boot.write_text(ruleset, encoding="utf-8")
        result = self.require_host_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)
        reads = self.ssh_log.read_text(encoding="utf-8")
        self.assertIn("cat /run/techsara-host-guard/ruleset.nft", reads)
        self.assertIn("/usr/local/sbin/techsara-host-guard plan --role worker", reads)

    def test_the_paths_read_are_the_ones_host_guard_writes(self) -> None:
        guard = GUARD.read_text(encoding="utf-8")
        self.assertIn('GUARD_STATE_DIR="${GUARD_STATE_DIR:-/run/techsara-host-guard}"', guard)
        self.assertIn('"$GUARD_STATE_DIR/ruleset.nft"', guard)
        self.assertIn('GUARD_BOOT_SCRIPT="${GUARD_BOOT_SCRIPT:-/usr/local/sbin/techsara-host-guard}"', guard)
        self.assertEqual(_assignment("GUARD_APPLIED_RULESET"), 'GUARD_APPLIED_RULESET="/run/techsara-host-guard/ruleset.nft"')
        self.assertEqual(_assignment("GUARD_BOOT_COPY"), 'GUARD_BOOT_COPY="/usr/local/sbin/techsara-host-guard"')

    def test_up_checks_the_guard_before_it_syncs_builds_or_starts_anything(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        up = re.search(r"^  up\)\n(.*?)^    ;;\n", text, re.S | re.M)
        self.assertIsNotNone(up, "whisper-cpu.sh has no `up)` case")
        body = up.group(1)
        guard_at = body.index("require_host_guard")
        # What reaches the worker: its files, its image, its model, its container. (How the endpoint
        # is then recorded in .env is not pinned here; it happens after the container is up.)
        for later in ("sync_files", "build_images", "ensure_model", 'compose_worker "$bind" up -d'):
            self.assertLess(guard_at, body.index(later), later)


if __name__ == "__main__":
    unittest.main()
