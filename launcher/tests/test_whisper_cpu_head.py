"""The CPU speech replica's HEAD copy (the owner's exception to "nothing new on the head", 2026-09-30).

scripts/whisper-cpu.sh acts on the worker's copy unless WHISPER_CPU_NODE=head. The head's copy:

* binds a Docker bridge gateway and nothing else. The head's host guard does not judge 30008 (nor
  30007, where the head's GPU replica listens on the same gateway), so the bind is the boundary;
* never starts below 20 GiB of MemAvailable, the floor the owner's exception was given against;
* compiles and converts NOTHING on the head: it loads the worker's image (same image ID) and copies
  the worker's q8_0 file (same SHA-256 pin);
* keeps its cores in .env, so a cap decided at the chat gate survives the next `up`;
* is MERGED into ASR_CPU_BASE_URLS, LAST, and never replaces the worker's entry (the router offers
  overflow to the list in order, and replacing is how a one-node rebuild halved the GPU fleet on
  2026-09-08).

Functions are extracted from the script and run in bash, or the whole script runs in a copied tree
with a fake ``ssh`` and ``docker`` on PATH that record every call. Nothing here touches Docker, a
socket or the network, except the render tests, which run ``docker compose config`` (it creates no
container) and skip without Compose v2.24+.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from . import test_compose_overlays as overlays
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    import test_compose_overlays as overlays
    from support import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "whisper-cpu.sh"
COMMON = REPO_ROOT / "scripts" / "lib" / "cluster-common.sh"
COMPOSE_FILE = REPO_ROOT / "compose" / "compose.whisper-cpu.yaml"
MANAGEMENT_LAN_ADDRESS = "192.168.9.68"
WORKER_URL = f"http://{MANAGEMENT_LAN_ADDRESS}:30008/v1"
HEAD_URL = "http://172.17.0.1:30008/v1"
KIB_PER_GIB = 1024 * 1024
WORKER_IMAGE_ID = "sha256:" + "ab" * 32
GGML_DIR = "openai--whisper-large-v3--06f233fe06e7-ggml"


def _function(script: Path, name: str) -> str:
    """The text of one top-level ``name() {`` bash function (a comment may follow the brace)."""
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{[^\n]*\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{script.name} has no function {name}")
    return match.group(0)


class _BashCase(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bash") is None:
            self.skipTest("bash is required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-wcpu-head-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        self.calls.touch()
        self.path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def _fake(self, name: str, body: str) -> None:
        fake = self.bin / name
        fake.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        fake.chmod(0o755)

    def _run(self, lines: list[str], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {"PATH": self.path, "HOME": str(self.root), "NO_COLOR": "1", "CALLS": str(self.calls), **env}
        return subprocess.run(
            ["bash", "-c", "\n".join(lines), "wcpu-test", *args],
            env=environment, capture_output=True, text=True, timeout=60,
        )

    def _call_log(self) -> str:
        return self.calls.read_text(encoding="utf-8")


class HeadBindTests(_BashCase):
    PRELUDE = [
        "set -euo pipefail",
        'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
        'ssh_worker() { ssh -o BatchMode=yes "$CLUSTER_WORKER_SSH" -- "$@"; }',
        'WHISPER_MANAGEMENT_IFNAME="${WHISPER_MANAGEMENT_IFNAME:-enP7s7}"',
    ]

    def setUp(self) -> None:
        super().setUp()
        # Records the call and answers with the worker's management address, as production does.
        self._fake("ssh", f'printf "ssh %s\\n" "$*" >> "$CALLS"\nprintf "%s" "{MANAGEMENT_LAN_ADDRESS}"\n')

    def _bind(self, *node: str, **env: str) -> subprocess.CompletedProcess[str]:
        program = [*self.PRELUDE, _function(SCRIPT, "bind_address"), 'bind_address "$@"']
        return self._run(program, *node, CLUSTER_WORKER_SSH=f"techsphere@{MANAGEMENT_LAN_ADDRESS}", **env)

    def test_the_head_copy_binds_the_bridge_gateway_and_never_asks_the_worker(self) -> None:
        result = self._bind("head")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "172.17.0.1")
        self.assertEqual(self._call_log(), "", "the head's own address is not read over ssh")

    def test_with_no_argument_the_node_is_WHISPER_CPU_NODE(self) -> None:
        result = self._bind(WHISPER_CPU_NODE="head")
        self.assertEqual((result.returncode, result.stdout), (0, "172.17.0.1"), result.stderr)

    def test_with_no_node_at_all_the_worker_management_address_is_read_as_before(self) -> None:
        result = self._bind()
        self.assertEqual((result.returncode, result.stdout), (0, MANAGEMENT_LAN_ADDRESS), result.stderr)
        self.assertIn("enP7s7", self._call_log())

    def test_another_docker_bridge_gateway_is_accepted(self) -> None:
        result = self._bind("head", WHISPER_CPU_HEAD_BIND="172.18.0.1")
        self.assertEqual((result.returncode, result.stdout), (0, "172.18.0.1"), result.stderr)

    def test_a_lan_rail_loopback_wildcard_or_injected_head_bind_is_refused(self) -> None:
        # The head's LAN and rail addresses, loopback (the orchestrator's container cannot reach it),
        # wildcards, a neighbour of 172.16/12, and text that is not an address at all.
        for address in ("192.168.9.54", "10.100.184.1", "127.0.0.1", "0.0.0.0", "::",
                        "172.32.0.1", "172.17.0.1 ; true", "172.17.0.1/16"):
            with self.subTest(address=address):
                result = self._bind("head", WHISPER_CPU_HEAD_BIND=address)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "", "nothing may be printed as a bind address")
                self.assertIn("Docker bridge gateway", result.stderr)


class CpuEndpointListTests(_BashCase):
    FUNCTIONS = ("_set_env", "cpu_endpoints_recorded", "is_head_endpoint", "record_cpu_endpoint", "forget_cpu_endpoint")

    def _steps(self, *steps: str, env_text: str = "") -> str:
        (self.root / ".env").write_text(env_text, encoding="utf-8")
        program = ["set -euo pipefail", f'ROOT="{self.root}"']
        program += [_function(SCRIPT, name) for name in self.FUNCTIONS]
        program += list(steps)
        result = self._run(program)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.root / ".env").read_text(encoding="utf-8")

    @staticmethod
    def _listed(env_text: str) -> str:
        values = re.findall(r"(?m)^ASR_CPU_BASE_URLS=(.*)$", env_text)
        assert len(values) == 1, env_text
        return values[0]

    def test_the_head_joins_after_the_worker_and_never_replaces_it(self) -> None:
        env = self._steps(f"record_cpu_endpoint {WORKER_URL}", f"record_cpu_endpoint {HEAD_URL}")
        self.assertEqual(self._listed(env), f"{WORKER_URL},{HEAD_URL}")

    def test_the_worker_goes_first_even_when_the_head_was_recorded_first(self) -> None:
        env = self._steps(f"record_cpu_endpoint {WORKER_URL}", env_text=f"ASR_CPU_BASE_URLS={HEAD_URL}\n")
        self.assertEqual(self._listed(env), f"{WORKER_URL},{HEAD_URL}")

    def test_up_again_on_either_node_does_not_duplicate_it(self) -> None:
        env = self._steps(
            f"record_cpu_endpoint {WORKER_URL}", f"record_cpu_endpoint {HEAD_URL}",
            f"record_cpu_endpoint {HEAD_URL}", f"record_cpu_endpoint {WORKER_URL}",
        )
        self.assertEqual(self._listed(env), f"{WORKER_URL},{HEAD_URL}")

    def test_down_on_one_node_leaves_the_other_listed(self) -> None:
        both = f"ASR_CPU_BASE_URLS={WORKER_URL},{HEAD_URL}\n"
        self.assertEqual(self._listed(self._steps(f"forget_cpu_endpoint {HEAD_URL}", env_text=both)), WORKER_URL)
        self.assertEqual(self._listed(self._steps(f"forget_cpu_endpoint {WORKER_URL}", env_text=both)), HEAD_URL)
        self.assertEqual(
            self._listed(self._steps(f"forget_cpu_endpoint {HEAD_URL}", f"forget_cpu_endpoint {WORKER_URL}", env_text=both)),
            "",
        )

    def test_the_gpu_fleet_and_every_other_key_are_left_alone(self) -> None:
        before = "ASR_BASE_URLS=http://192.168.9.68:30007/v1,http://172.17.0.1:30007/v1\nCLUSTER_MODE=dual\n"
        env = self._steps(f"record_cpu_endpoint {HEAD_URL}", env_text=before)
        self.assertTrue(env.startswith(before), env)
        self.assertEqual(self._listed(env), HEAD_URL)


class HeadPlacementTests(_BashCase):
    def _placement(self, env_text: str = "", **env: str) -> subprocess.CompletedProcess[str]:
        (self.root / ".env").write_text(env_text, encoding="utf-8")
        program = [
            "set -euo pipefail",
            'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
            f'ENV_FILE="{self.root / ".env"}"',
            _function(COMMON, "env_get"),
            _function(SCRIPT, "head_setting"),
            _function(SCRIPT, "head_placement"),
            "head_placement",
        ]
        return self._run(program, **env)

    def test_the_default_is_eight_x925_cores_without_5_and_6(self) -> None:
        result = self._placement()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "WHISPER_CPU_CPUSET=7-9,15-19 WHISPER_CPU_CPUS=8 WHISPER_CPU_THREADS=8")

    def test_a_cap_kept_in_env_survives_the_next_up(self) -> None:
        result = self._placement("WHISPER_CPU_HEAD_CPUSET=16-19\nWHISPER_CPU_HEAD_CPUS=4\nWHISPER_CPU_HEAD_THREADS=4\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "WHISPER_CPU_CPUSET=16-19 WHISPER_CPU_CPUS=4 WHISPER_CPU_THREADS=4")

    def test_the_environment_wins_over_env(self) -> None:
        result = self._placement("WHISPER_CPU_HEAD_CPUSET=16-19\n", WHISPER_CPU_HEAD_CPUSET="0-4,10-14")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WHISPER_CPU_CPUSET=0-4,10-14 ", result.stdout)

    def test_malformed_values_are_refused(self) -> None:
        for key, value in (("WHISPER_CPU_HEAD_CPUSET", "7-9;true"), ("WHISPER_CPU_HEAD_CPUSET", "all"),
                           ("WHISPER_CPU_HEAD_CPUS", "0"), ("WHISPER_CPU_HEAD_CPUS", "21"),
                           ("WHISPER_CPU_HEAD_CPUS", "7.5"), ("WHISPER_CPU_HEAD_THREADS", "32")):
            with self.subTest(key=key, value=value):
                result = self._placement(**{key: value})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn(key, result.stderr)


class HeadMemoryFloorTests(_BashCase):
    def _floor(self, available_gib: float, **env: str) -> subprocess.CompletedProcess[str]:
        meminfo = self.root / "meminfo"
        meminfo.write_text(
            f"MemTotal:       127600812 kB\nMemFree:         1810600 kB\nMemAvailable:   {int(available_gib * KIB_PER_GIB)} kB\n",
            encoding="utf-8",
        )
        program = [
            "set -euo pipefail",
            'die() { printf "error: %s\\n" "$*" >&2; exit 2; }',
            'check_pass() { printf "PASS %s\\n" "$*"; }',
            'WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB="${WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB:-20}"',
            _function(SCRIPT, "head_memory_floor"),
            "head_memory_floor",
        ]
        return self._run(program, WHISPER_CPU_MEMINFO=str(meminfo), **env)

    def test_below_20_gib_available_the_head_copy_does_not_start(self) -> None:
        result = self._floor(19.9)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("under the 20 GiB floor", result.stderr)

    def test_at_or_above_the_floor_it_may(self) -> None:
        for available in (20.0, 29.2):
            with self.subTest(available_gib=available):
                result = self._floor(available)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PASS head memory", result.stdout)

    def test_the_floor_can_be_raised_but_must_be_a_whole_number(self) -> None:
        self.assertEqual(self._floor(25.0, WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB="30").returncode, 2)
        result = self._floor(25.0, WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB="twenty")
        self.assertEqual(result.returncode, 2)
        self.assertIn("whole number", result.stderr)


class HeadUpScriptTests(_BashCase):
    """The whole script in a copied tree, with every ssh and docker call recorded."""

    def setUp(self) -> None:
        super().setUp()
        self.tree = self.root / "repo"
        (self.tree / "scripts" / "lib").mkdir(parents=True)
        (self.tree / "compose").mkdir()
        shutil.copy(SCRIPT, self.tree / "scripts" / "whisper-cpu.sh")
        shutil.copy(COMMON, self.tree / "scripts" / "lib" / "cluster-common.sh")
        shutil.copy(COMPOSE_FILE, self.tree / "compose" / "compose.whisper-cpu.yaml")
        self.state = self.root / "state"
        self.state.mkdir()
        self.cache = self.root / "model-cache"
        self.cache.mkdir()
        self._fake(
            "ssh",
            'printf "ssh %s\\n" "$*" >> "$CALLS"\n'
            'cmd="${!#}"\n'
            'case "$cmd" in\n'
            f'  *"docker image inspect"*) printf "%s" "{WORKER_IMAGE_ID}" ;;\n'
            '  *"docker save"*) printf "image-tar" ;;\n'
            '  *"cat "*) printf "not the q8_0 model" ;;\n'
            f'  *"ip -4"*) printf "{MANAGEMENT_LAN_ADDRESS}" ;;\n'
            "esac\n",
        )
        self._fake(
            "docker",
            'printf "docker %s\\n" "$*" >> "$CALLS"\n'
            'case "$1" in\n'
            f'  image) [ -f "$STATE/loaded" ] || exit 1; printf "%s\\n" "{WORKER_IMAGE_ID}" ;;\n'
            '  load) cat >/dev/null; touch "$STATE/loaded" ;;\n'
            "esac\n",
        )
        self._fake("scp", 'printf "scp %s\\n" "$*" >> "$CALLS"\n')
        # No real network: a regression that reaches wait_ready fails in seconds, recorded.
        self._fake("curl", 'printf "curl %s\\n" "$*" >> "$CALLS"\nexit 7\n')

    def _script(self, *args: str, env_text: str = "CLUSTER_MODE=dual\nCLUSTER_WORKER_SSH=techsphere@10.100.184.2\n",
                available_gib: float = 29.2, **env: str) -> subprocess.CompletedProcess[str]:
        (self.tree / ".env").write_text(env_text, encoding="utf-8")
        meminfo = self.root / "meminfo"
        meminfo.write_text(f"MemAvailable:   {int(available_gib * KIB_PER_GIB)} kB\n", encoding="utf-8")
        program = [f'exec bash "{self.tree / "scripts" / "whisper-cpu.sh"}" "$@"']
        return self._run(
            program, *args, STATE=str(self.state), WHISPER_CPU_MEMINFO=str(meminfo),
            WHISPER_CPU_HEAD_MODEL_CACHE=str(self.cache), WHISPER_MODEL_CACHE="/srv/worker-cache",
            WHISPER_CPU_READY_TIMEOUT_S="2", **env,
        )

    def test_an_unknown_node_is_refused_before_anything_runs(self) -> None:
        result = self._script("up", WHISPER_CPU_NODE="spark-0e68")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("WHISPER_CPU_NODE must be 'worker' or 'head'", result.stderr)
        self.assertEqual(self._call_log(), "")

    def test_single_mode_refuses_the_head_copy_before_anything_runs(self) -> None:
        result = self._script("up", env_text="CLUSTER_MODE=single\n", WHISPER_CPU_NODE="head")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("two-node cluster", result.stderr)
        self.assertEqual(self._call_log(), "")

    def test_under_the_floor_up_touches_neither_docker_nor_the_worker(self) -> None:
        result = self._script("up", available_gib=19.5, WHISPER_CPU_NODE="head")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("under the 20 GiB floor", result.stderr)
        self.assertEqual(self._call_log(), "")
        self.assertNotIn("ASR_CPU_BASE_URLS", (self.tree / ".env").read_text(encoding="utf-8"))

    def test_the_head_loads_the_workers_image_and_refuses_a_model_that_misses_the_pin(self) -> None:
        result = self._script("up", WHISPER_CPU_NODE="head")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("does not match its pin", result.stderr)
        calls = self._call_log()
        # The image came from the worker, under the worker's ID ...
        self.assertIn("docker save", calls)
        self.assertIn("docker load", calls)
        self.assertIn(f"is the worker's build ({WORKER_IMAGE_ID[7:19]})", result.stdout)
        # ... the model was asked of the worker's cache, and nothing was compiled, converted,
        # copied by scp or started on the head.
        self.assertIn(f"cat '/srv/worker-cache/repos/{GGML_DIR}/ggml-large-v3-q8_0.bin'", calls)
        for forbidden in ("docker build", "docker compose", "docker run", "scp "):
            self.assertNotIn(forbidden, calls)
        # The rejected copy left nothing behind, and nothing reached .env.
        self.assertEqual(list((self.cache / "repos" / GGML_DIR).iterdir()), [])
        self.assertNotIn("ASR_CPU_BASE_URLS", (self.tree / ".env").read_text(encoding="utf-8"))

    def test_a_different_worker_image_id_after_the_load_is_refused(self) -> None:
        # docker load "succeeds" but the head's image is not the worker's: never run a stranger.
        self._fake(
            "docker",
            'printf "docker %s\\n" "$*" >> "$CALLS"\n'
            'case "$1" in\n'
            '  image) [ -f "$STATE/loaded" ] || exit 1; printf "sha256:%s\\n" "cd00000000000000000000000000000000000000000000000000000000000000" ;;\n'
            '  load) cat >/dev/null; touch "$STATE/loaded" ;;\n'
            "esac\n",
        )
        result = self._script("up", WHISPER_CPU_NODE="head")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("not the worker's", result.stderr)
        self.assertNotIn(" cat ", self._call_log())


class ComposeDefaultsTests(unittest.TestCase):
    TEXT = COMPOSE_FILE.read_text(encoding="utf-8")

    def test_the_worker_values_are_the_defaults_and_only_a_variable_changes_them(self) -> None:
        self.assertIn('cpuset: "${WHISPER_CPU_CPUSET:-5-9,15-19}"', self.TEXT)
        self.assertRegex(self.TEXT, r"(?m)^    cpus: \$\{WHISPER_CPU_CPUS:-8\}$")
        self.assertIn('WHISPER_CPU_THREADS: "${WHISPER_CPU_THREADS:-8}"', self.TEXT)
        # The one hard memory limit and the OOM order are the same on both nodes.
        self.assertIn("mem_limit: 4g", self.TEXT)
        self.assertIn("oom_score_adj: 850", self.TEXT)


@unittest.skipUnless(overlays.COMPOSE_AVAILABLE, "Docker Compose v2.24+ is required")
class ComposeRenderTests(unittest.TestCase):
    """What `docker compose up` would create, rendered with `config` (no container is created)."""

    def _render(self, **env: str) -> dict:
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                       "WHISPER_CPU_MODEL_DIR": "/srv/models/ggml", **env}
        result = subprocess.run(
            ["docker", "compose", "--project-name", "sf-local-ai-whisper-cpu", "-f", str(COMPOSE_FILE),
             "config", "--format", "json"],
            env=environment, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["services"]["whisper-cpu"]

    def test_the_worker_render_is_what_it_was(self) -> None:
        service = self._render(WHISPER_BIND=MANAGEMENT_LAN_ADDRESS)
        self.assertEqual(service["cpuset"], "5-9,15-19")
        self.assertEqual(float(service["cpus"]), 8.0)
        self.assertEqual(service["environment"]["WHISPER_CPU_THREADS"], "8")
        self.assertEqual(service["environment"]["WHISPER_BIND"], MANAGEMENT_LAN_ADDRESS)

    def test_the_head_values_reach_the_container(self) -> None:
        service = self._render(WHISPER_BIND="172.17.0.1", WHISPER_CPU_CPUSET="16-19", WHISPER_CPU_CPUS="4",
                               WHISPER_CPU_THREADS="4")
        self.assertEqual(service["cpuset"], "16-19")
        self.assertEqual(float(service["cpus"]), 4.0)
        self.assertEqual(service["environment"]["WHISPER_CPU_THREADS"], "4")
        self.assertEqual(service["environment"]["WHISPER_BIND"], "172.17.0.1")
        self.assertEqual(service["network_mode"], "host")
        self.assertEqual(service["oom_score_adj"], 850)


if __name__ == "__main__":
    unittest.main()
