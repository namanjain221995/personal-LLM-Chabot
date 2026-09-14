"""The v1-gateway is pinned to its own code, and nothing else recreates it.

The public /v1 relay (gateway/, no-timeout design T6, 2026-09-13) holds
developer connections while routine deploys recreate the orchestrator and the
frontend. That is only true if a routine deploy never recreates the relay
itself, so its Compose definition must move with its own image inputs and
with nothing else. These tests pin that down from four sides:

* the digest -- the launcher's and scripts/deploy.sh's -- covers the image
  inputs (Dockerfile, server.cjs, lib/) and nothing else;
* the digest reaches Compose through the process environment, never an env
  file: Compose folds an env_file's keys into a service's definition, and
  .env / secrets.env / generated.env are env_files of the orchestrator and the
  frontend, so a key there would recreate THEM whenever the gateway changed;
* the launcher builds only a missing image and never force-recreates;
* rendered by real ``docker compose config``, an orchestrator-only change
  (and a new generated.env key, and a gateway README or test edit) leaves the
  gateway's definition byte-identical, and a gateway code change does not;
* deploy.sh's guards report, and hold before a recreate only for a bounded
  time -- a deploy is never held indefinitely.

Nothing here starts a container, pulls an image or touches a socket; the
Compose checks only resolve and render, and skip without Docker Compose v2.24+.
The one check against a live Docker daemon creates a throwaway network and so
runs only with TECHSARA_V1RELAY_LIVE_NETWORK_TEST=1.
"""

from __future__ import annotations

import ast
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock

try:
    from . import test_compose_overlays as overlays
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    import test_compose_overlays as overlays
    from support import REPO_ROOT

from techsara_cli import cli
from techsara_cli.compose import ComposeManager
from techsara_cli.errors import TechSaraError

DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
LAUNCHER_CLI = REPO_ROOT / "launcher" / "techsara_cli" / "cli.py"
GATEWAY_IMAGE_INPUTS = ("Dockerfile", "server.cjs", "lib")


def _bash_function(script: Path, name: str) -> str:
    """One top-level ``name() {  # comment`` ... ``}`` function of a bash script."""
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{[^\n]*\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{script.name} has no function {name}")
    return match.group(0)


def _write_gateway(root: Path) -> Path:
    """A small gateway/ tree with every kind of file the real one has."""
    gateway = root / "gateway"
    (gateway / "lib").mkdir(parents=True)
    (gateway / "test").mkdir()
    (gateway / "testkit").mkdir()
    (gateway / "Dockerfile").write_text("FROM node:20-alpine@sha256:" + "0" * 64 + "\nCOPY server.cjs lib ./\n")
    (gateway / "server.cjs").write_text("'use strict';\nrequire('./lib/relay.cjs');\n")
    (gateway / "lib" / "relay.cjs").write_text("module.exports = 1;\n")
    (gateway / "lib" / "headers.cjs").write_text("module.exports = 2;\n")
    (gateway / "README.md").write_text("# v1-gateway\n")
    (gateway / ".dockerignore").write_text("test\ntestkit\nREADME.md\n")
    (gateway / "test" / "relay.test.cjs").write_text("// a test\n")
    (gateway / "testkit" / "harness.cjs").write_text("// a harness\n")
    return gateway


class TheGatewayDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="techsara-v1gw-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gateway = _write_gateway(self.root)

    def test_the_digest_is_sha256sum_of_the_image_inputs_sorted_by_path(self) -> None:
        listing = "".join(
            f"{hashlib.sha256((self.gateway / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("Dockerfile", "lib/headers.cjs", "lib/relay.cjs", "server.cjs")
        )
        self.assertEqual(cli.v1_gateway_code_sha(self.root), hashlib.sha256(listing.encode()).hexdigest())

    def test_a_readme_test_testkit_or_dockerignore_edit_leaves_the_digest_unchanged(self) -> None:
        first = cli.v1_gateway_code_sha(self.root)
        (self.gateway / "README.md").write_text("# v1-gateway\n\n## Operating it\n")
        (self.gateway / "test" / "relay.test.cjs").write_text("// a changed test\n")
        (self.gateway / "test" / "new.test.cjs").write_text("// a new test\n")
        (self.gateway / "testkit" / "harness.cjs").write_text("// a changed harness\n")
        (self.gateway / ".dockerignore").write_text("test\n")
        self.assertEqual(cli.v1_gateway_code_sha(self.root), first)

    def test_any_image_input_edit_changes_the_digest(self) -> None:
        edits = {
            "Dockerfile": lambda: (self.gateway / "Dockerfile").write_text("FROM node:20-alpine@sha256:" + "1" * 64 + "\n"),
            "server.cjs": lambda: (self.gateway / "server.cjs").write_text("'use strict';\n"),
            "a lib .cjs": lambda: (self.gateway / "lib" / "relay.cjs").write_text("module.exports = 3;\n"),
            "a lib non-.cjs file": lambda: (self.gateway / "lib" / "limits.json").write_text("{}\n"),
            "a nested lib file": lambda: ((self.gateway / "lib" / "sub").mkdir(), (self.gateway / "lib" / "sub" / "a.cjs").write_text("1\n")),
        }
        for label, edit in edits.items():
            with self.subTest(edit=label):
                before = cli.v1_gateway_code_sha(self.root)
                edit()
                self.assertNotEqual(cli.v1_gateway_code_sha(self.root), before)

    def test_a_checkout_without_the_gateway_program_has_no_digest(self) -> None:
        (self.gateway / "server.cjs").unlink()
        self.assertEqual(cli.v1_gateway_code_sha(self.root), "")
        self.assertEqual(cli.v1_gateway_code_sha(self.root / "nowhere"), "")

    def test_deploy_sh_prints_the_same_digest_as_the_launcher_for_a_tree_and_for_this_repository(self) -> None:
        if shutil.which("bash") is None or shutil.which("sha256sum") is None:
            self.skipTest("bash and coreutils are required")
        (self.gateway / "lib" / "sub").mkdir()
        (self.gateway / "lib" / "sub" / "nested.json").write_text("[]\n")

        def shell(root: Path) -> str:
            environment = {**os.environ, "TECHSARA_DEPLOY_ROOT": str(root)}
            result = subprocess.run(
                ["bash", str(DEPLOY_SH), "--print-v1-gateway-sha"],
                capture_output=True, text=True, env=environment, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        self.assertEqual(shell(self.root), cli.v1_gateway_code_sha(self.root))
        self.assertRegex(shell(REPO_ROOT), r"^[0-9a-f]{64}$")
        self.assertEqual(shell(REPO_ROOT), cli.v1_gateway_code_sha(REPO_ROOT))
        self.assertEqual(shell(self.root / "nowhere"), "")


class TheDigestReachesComposeThroughTheProcessOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="techsara-v1gw-pin-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_gateway(self.root)

    def test_every_compose_call_sees_the_pin_and_no_env_file_is_written(self) -> None:
        runtime = self.root / ".runtime"
        runtime.mkdir()
        generated = runtime / "generated.env"
        generated.write_text("MAIN_MODEL=fixture\n")
        secrets = runtime / "secrets.env"
        secrets.write_text("POSTGRES_PASSWORD=fixture\n")
        environ: dict[str, str] = {}
        sha = cli._pin_v1_gateway(self.root, {".env": {}, "secrets.env": {"POSTGRES_PASSWORD": "fixture"}}, environ=environ)
        self.assertEqual(environ, {cli.V1_GATEWAY_CODE_SHA_KEY: sha})
        seen: list[dict] = []
        runner = Mock(side_effect=lambda argv, **kwargs: seen.append(kwargs.get("env") or {}) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        manager = ComposeManager(self.root, [self.root / "compose.yaml"], generated, secrets, runner=runner)
        # ComposeManager starts every command's environment from os.environ,
        # which is where the launcher puts the pin.
        previous = os.environ.get(cli.V1_GATEWAY_CODE_SHA_KEY)
        os.environ[cli.V1_GATEWAY_CODE_SHA_KEY] = sha
        try:
            manager.run("config", "--quiet")
            manager.up_service(cli.V1_GATEWAY_SERVICE)
        finally:
            if previous is None:
                os.environ.pop(cli.V1_GATEWAY_CODE_SHA_KEY, None)
            else:
                os.environ[cli.V1_GATEWAY_CODE_SHA_KEY] = previous
        self.assertEqual([env.get(cli.V1_GATEWAY_CODE_SHA_KEY) for env in seen], [sha, sha])
        self.assertEqual(generated.read_text(), "MAIN_MODEL=fixture\n")
        self.assertEqual(secrets.read_text(), "POSTGRES_PASSWORD=fixture\n")

    def test_an_env_file_that_names_the_key_is_refused_before_anything_renders(self) -> None:
        for layer in (".env", "/srv/.runtime/secrets.env"):
            with self.subTest(layer=layer):
                environ: dict[str, str] = {}
                with self.assertRaisesRegex(TechSaraError, re.escape(f"V1_GATEWAY_CODE_SHA is set in {layer}")):
                    cli._pin_v1_gateway(self.root, {layer: {"V1_GATEWAY_CODE_SHA": "a" * 64}}, environ=environ)
                self.assertEqual(environ, {})

    def test_a_checkout_without_a_gateway_clears_a_stale_pin(self) -> None:
        environ = {cli.V1_GATEWAY_CODE_SHA_KEY: "b" * 64, "OTHER": "kept"}
        self.assertEqual(cli._pin_v1_gateway(self.root / "nowhere", {}, environ=environ), "")
        self.assertEqual(environ, {"OTHER": "kept"})

    def test_the_launcher_writes_the_key_into_no_generated_environment(self) -> None:
        """generated.env is written from environment.py's values; the key must
        never be one of them (it would fold into the orchestrator and the
        frontend). cli.py names it once, in its constant."""
        environment_source = (REPO_ROOT / "launcher" / "techsara_cli" / "environment.py").read_text(encoding="utf-8")
        self.assertNotIn("V1_GATEWAY_CODE_SHA", environment_source)
        tree = ast.parse(LAUNCHER_CLI.read_text(encoding="utf-8"))
        literals = [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "V1_GATEWAY_CODE_SHA"]
        self.assertEqual(len(literals), 1)

    def test_up_pins_the_gateway_before_the_first_compose_render(self) -> None:
        source = LAUNCHER_CLI.read_text(encoding="utf-8")
        body = source[source.index("def _cmd_up("):source.index("def _compose_from_state(")]
        self.assertLess(body.index("_pin_v1_gateway("), body.index("compose = ComposeManager("))
        self.assertLess(body.index("_start_compose("), body.index("_start_v1_gateway("))


class StartingTheGatewayTests(unittest.TestCase):
    SHA = "c" * 64

    def _compose(
        self, *, image_present: bool, before_id: str, after_id: str,
        services: str = "orchestrator\nv1-gateway\n", health: str = "healthy",
    ) -> Mock:
        compose = Mock()
        compose.run.return_value = SimpleNamespace(returncode=0, stdout=services, stderr="")
        compose.wait_service.return_value = {"Status": "Up 3 days (healthy)"}
        compose.ps.return_value = [{"State": "running", "Health": health, "Status": f"Up 3 days ({health})"}]
        inspections = [before_id, after_id]

        def runner(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                return SimpleNamespace(returncode=0 if image_present else 1, stdout="sha256:" + "d" * 64 if image_present else "", stderr="")
            if argv[:2] == ["docker", "inspect"]:
                value = inspections.pop(0)
                if not value:
                    return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
                return SimpleNamespace(returncode=0, stdout=value, stderr="")
            raise AssertionError(argv)

        compose.runner = Mock(side_effect=runner)
        return compose

    def test_an_unchanged_gateway_is_left_running_and_never_force_recreated(self) -> None:
        image = f"sf-local-ai-v1-gateway:{self.SHA}"
        compose = self._compose(image_present=True, before_id=f"abc\t{image}\ttrue", after_id="abc")
        result = cli._start_v1_gateway(compose, self.SHA)
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(result["health"], "healthy")
        self.assertFalse(result["built"])
        compose.up_service.assert_called_once_with("v1-gateway")
        self.assertNotIn(("build", "v1-gateway"), [c.args for c in compose.run.call_args_list])
        # Read once, never waited on: its health cannot fail this run.
        compose.wait_service.assert_not_called()
        compose.ps.assert_called_once_with("v1-gateway")

    def test_an_unchanged_but_unhealthy_gateway_is_a_loud_warning_and_never_fails_the_run(self) -> None:
        """Review finding 2026-09-14: it raised, so the deploy AND its rollback
        (same gateway/, same untouched container) failed."""
        image = f"sf-local-ai-v1-gateway:{self.SHA}"
        compose = self._compose(image_present=True, before_id=f"abc\t{image}\ttrue", after_id="abc", health="unhealthy")
        with mock.patch.object(cli, "_step") as step:
            result = cli._start_v1_gateway(compose, self.SHA)
        self.assertEqual((result["action"], result["health"]), ("unchanged", "unhealthy"))
        said = "\n".join(str(c.args[0]) for c in step.call_args_list)
        self.assertIn("WARNING it is unhealthy and this run did not change it, so the run goes on", said)
        self.assertIn("docker restart sf-local-ai-v1-gateway-1", said)
        compose.wait_service.assert_not_called()
        compose.up_service.assert_called_once_with("v1-gateway")

    def test_an_unchanged_unhealthy_gateway_passes_through_a_real_compose_manager(self) -> None:
        """The reviewer's probe: a real ComposeManager whose `ps` says unhealthy."""
        image = f"sf-local-ai-v1-gateway:{self.SHA}"
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["docker", "image", "inspect"]:
                return SimpleNamespace(returncode=0, stdout="sha256:dd", stderr="")
            if argv[:2] == ["docker", "inspect"]:
                if "{{.RestartCount}}" in argv:
                    return SimpleNamespace(returncode=0, stdout="0", stderr="")
                return SimpleNamespace(returncode=0, stdout=f"abc\t{image}\ttrue" if "\t" in argv[-1] else "abc", stderr="")
            if "config" in argv and "--services" in argv:
                return SimpleNamespace(returncode=0, stdout="orchestrator\nv1-gateway\n", stderr="")
            if "ps" in argv:
                row = {"ID": "abc", "Service": "v1-gateway", "State": "running", "Health": "unhealthy", "Status": "Up 2 days (unhealthy)"}
                return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="")
            if "up" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory(prefix="techsara-v1gw-real-") as name:
            root = Path(name)
            manager = ComposeManager(root, [root / "compose.yaml"], root / "generated.env", root / "secrets.env", runner=runner)
            with mock.patch.object(cli, "_step"):
                result = cli._start_v1_gateway(manager, self.SHA)
        self.assertEqual((result["action"], result["health"]), ("unchanged", "unhealthy"))
        self.assertFalse(any("--force-recreate" in c for c in calls))

    def test_a_stopped_gateway_started_again_that_stays_unhealthy_warns_and_does_not_fail(self) -> None:
        image = f"sf-local-ai-v1-gateway:{self.SHA}"
        compose = self._compose(image_present=True, before_id=f"abc\t{image}\tfalse", after_id="abc", health="starting")
        compose.wait_service.side_effect = TechSaraError("service v1-gateway did not become healthy before timeout")
        with mock.patch.object(cli, "_step") as step:
            result = cli._start_v1_gateway(compose, self.SHA)
        self.assertEqual((result["action"], result["health"]), ("started", "starting"))
        compose.wait_service.assert_called_once_with("v1-gateway", timeout=120.0, reporter=step)
        self.assertIn("WARNING it is starting", "\n".join(str(c.args[0]) for c in step.call_args_list))

    def test_a_running_unpinned_gateway_is_named_before_it_is_put_back(self) -> None:
        compose = self._compose(image_present=True, before_id="abc\tsf-local-ai-v1-gateway:unpinned\ttrue", after_id="def")
        with mock.patch.object(cli, "_step") as step:
            result = cli._start_v1_gateway(compose, self.SHA)
        self.assertEqual(result["action"], "recreated")
        self.assertIn("WARNING the running container is on sf-local-ai-v1-gateway:unpinned", "\n".join(str(c.args[0]) for c in step.call_args_list))

    def test_a_changed_gateway_is_built_once_and_recreated_by_compose_not_forced(self) -> None:
        compose = self._compose(image_present=False, before_id="abc\tsf-local-ai-v1-gateway:" + "e" * 64 + "\ttrue", after_id="def")
        result = cli._start_v1_gateway(compose, self.SHA)
        self.assertEqual(result["action"], "recreated")
        self.assertTrue(result["built"])
        self.assertEqual(result["image"], f"sf-local-ai-v1-gateway:{self.SHA}")
        builds = [c for c in compose.run.call_args_list if c.args[:1] == ("build",)]
        self.assertEqual([c.args for c in builds], [("build", "v1-gateway")])
        compose.up_service.assert_called_once_with("v1-gateway")

    def test_a_first_start_creates_it(self) -> None:
        compose = self._compose(image_present=False, before_id="", after_id="new")
        self.assertEqual(cli._start_v1_gateway(compose, self.SHA)["action"], "created")

    def test_a_checkout_without_the_gateway_program_touches_nothing(self) -> None:
        compose = Mock()
        self.assertEqual(cli._start_v1_gateway(compose, ""), {"status": "absent"})
        self.assertEqual(compose.mock_calls, [])

    def test_a_chain_that_does_not_define_the_service_starts_nothing(self) -> None:
        compose = self._compose(image_present=True, before_id="", after_id="", services="orchestrator\nfrontend\n")
        self.assertEqual(cli._start_v1_gateway(compose, self.SHA)["status"], "not_rendered")
        compose.up_service.assert_not_called()
        compose.runner.assert_not_called()

    def test_a_gateway_this_run_created_or_recreated_that_does_not_become_healthy_fails_the_up(self) -> None:
        for label, before_id, after_id in (
            ("created", "", "new"),
            ("recreated", "abc\tsf-local-ai-v1-gateway:" + "e" * 64 + "\ttrue", "def"),
        ):
            with self.subTest(label):
                compose = self._compose(image_present=True, before_id=before_id, after_id=after_id)
                compose.wait_service.side_effect = TechSaraError("service v1-gateway did not become healthy before timeout")
                with self.assertRaisesRegex(TechSaraError, "v1-gateway"):
                    cli._start_v1_gateway(compose, self.SHA)
                compose.wait_service.assert_called_once_with("v1-gateway", timeout=120.0, reporter=cli._step)

    def test_no_launcher_call_force_recreates_the_gateway(self) -> None:
        tree = ast.parse(LAUNCHER_CLI.read_text(encoding="utf-8"))
        gateway_ups = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "up_service"
            and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "V1_GATEWAY_SERVICE"
        ]
        self.assertEqual(len(gateway_ups), 1)
        self.assertEqual(gateway_ups[0].keywords, [])


#: Address space the v1relay subnet must stay out of (compose.yaml, v1relay).
DOCKER_DEFAULT_POOLS = tuple(ipaddress.ip_network(f"172.{n}.0.0/16") for n in range(17, 32)) + (ipaddress.ip_network("192.168.0.0/16"),)
#: The kinds of network a DGX host has (RoCE rails, LAN, a VPN). Illustrative
#: ranges, not this deployment's: host addressing is kept out of the repository.
THIS_HOSTS_RANGES = tuple(ipaddress.ip_network(item) for item in ("10.60.184.0/23", "192.168.50.0/24", "100.64.0.0/10"))

#: `ip -4 route show table all` in the shape a DGX host prints it (addresses
#: illustrative, bridge names shortened), plus the shapes the parser must skip.
HOST_ROUTES = """\
100.101.102.103 dev tailscale0 table 52
default via 192.168.50.1 dev enP7s7 proto dhcp src 192.168.50.20 metric 102
10.60.184.0/24 dev enp1s0f1np1 proto kernel scope link src 10.60.184.1 metric 101
10.60.185.0/24 dev enP2p1s0f1np1 proto kernel scope link src 10.60.185.1 metric 100
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1
172.20.0.0/16 dev br-fa95 proto kernel scope link src 172.20.0.1
192.168.50.0/24 dev enP7s7 proto kernel scope link src 192.168.50.20 metric 102
local 10.231.231.9 dev lo table local proto kernel scope host src 10.231.231.9
broadcast 10.231.231.15 dev lo table local proto kernel scope link src 10.231.231.9
unreachable 198.51.100.0/24 proto static
0.0.0.0/1 via 10.8.0.1 dev tun0
128.0.0.0/1 via 10.8.0.1 dev tun0
"""


class TheRelayNetworkSubnetCheckTests(unittest.TestCase):
    """cli._check_v1relay_subnet: refuse, by name, a pinned subnet already in use."""

    NAME = "sf-local-ai_v1relay"

    def _compose(self, *, subnet: str | None = "10.231.231.0/28", networks: dict[str, str] | None = None,
                 routes: str | None = HOST_ROUTES, network_ls_fails: bool = False) -> tuple[Mock, list[list[str]]]:
        document: dict = {"services": {"orchestrator": {}}, "networks": {"application": {"name": "sf-local-ai_application"}}}
        if subnet is not None:
            document["networks"]["v1relay"] = {
                "name": self.NAME, "internal": True,
                "ipam": {"config": [{"subnet": subnet, "ip_range": "10.231.231.8/29", "gateway": "10.231.231.1"}]},
            }
        networks = {"bridge": "172.17.0.0/16", "sf-local-ai_application": "172.18.0.0/16", "pra-net": "172.20.0.0/16", "host": "", "none": ""} if networks is None else networks
        calls: list[list[str]] = []
        compose = Mock()
        compose.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["docker", "network", "ls"]:
                if network_ls_fails:
                    return SimpleNamespace(returncode=1, stdout="", stderr="daemon")
                return SimpleNamespace(returncode=0, stdout="\n".join(networks) + "\n", stderr="")
            if argv[:3] == ["docker", "network", "inspect"]:
                names = [item for item in argv[3:] if not item.startswith("--") and "{{" not in item]
                if len(names) == 1 and "\t" not in argv[-1]:
                    return SimpleNamespace(returncode=0, stdout=networks.get(names[0], "") + " ", stderr="")
                lines = [f"{name}\t{networks[name]} " for name in names]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")
            if argv[:2] == ["ip", "-4"]:
                if routes is None:
                    return SimpleNamespace(returncode=127, stdout="", stderr="FileNotFoundError")
                return SimpleNamespace(returncode=0, stdout=routes, stderr="")
            raise AssertionError(argv)

        compose.runner = Mock(side_effect=runner)
        return compose, calls

    def test_a_free_subnet_passes_on_this_hosts_routes_and_networks(self) -> None:
        compose, calls = self._compose()
        with mock.patch.object(cli, "_step"):
            result = cli._check_v1relay_subnet(compose)
        self.assertEqual(result, {"status": "free", "network": self.NAME, "subnet": "10.231.231.0/28"})
        compose.run.assert_called_once_with("config", "--format", "json", timeout=60.0)
        self.assertIn(["ip", "-4", "route", "show", "table", "all"], calls)

    def test_a_host_route_covering_the_subnet_stops_the_start_and_names_the_route(self) -> None:
        """Review finding 2026-09-14: `ip -4 route | grep '^172\\.30\\.231\\.'` passed a covering /16."""
        compose, _calls = self._compose(routes=HOST_ROUTES + "10.231.0.0/16 dev br-0123456789ab proto kernel scope link src 10.231.0.1\n")
        with self.assertRaisesRegex(TechSaraError, r"pins 10\.231\.231\.0/28 in compose\.yaml, which overlaps host route 10\.231\.0\.0/16"):
            cli._check_v1relay_subnet(compose)

    def test_a_docker_network_covering_the_subnet_stops_the_start_and_names_the_network(self) -> None:
        networks = {"bridge": "172.17.0.0/16", "someone-elses": "10.231.0.0/16 fd00::/64"}
        compose, _calls = self._compose(networks=networks, routes="")
        with self.assertRaisesRegex(TechSaraError, r"overlaps Docker network someone-elses 10\.231\.0\.0/16\. Docker cannot create it there"):
            cli._check_v1relay_subnet(compose)

    def test_an_existing_relay_network_is_left_to_compose_and_a_different_subnet_is_named(self) -> None:
        compose, calls = self._compose(networks={"bridge": "172.17.0.0/16", self.NAME: "10.231.231.0/28"})
        with mock.patch.object(cli, "_step") as step:
            self.assertEqual(cli._check_v1relay_subnet(compose)["status"], "exists")
        self.assertFalse(any(c[:2] == ["ip", "-4"] for c in calls), "an allocated network is not re-checked")
        step.assert_not_called()
        compose, _calls = self._compose(networks={self.NAME: "10.9.9.0/28"})
        with mock.patch.object(cli, "_step") as step:
            self.assertEqual(cli._check_v1relay_subnet(compose)["status"], "exists")
        self.assertIn("exists with subnet 10.9.9.0/28, but compose.yaml pins 10.231.231.0/28", str(step.call_args_list))

    def test_without_the_ip_tool_the_main_route_table_is_read_from_proc(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            proc = Path(name) / "route"
            # 10.231.0.0/16 (little-endian hex) on br-x, and a default route.
            proc.write_text(
                "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                "enP7s7\t00000000\t0108A8C0\t0003\t0\t0\t102\t00000000\t0\t0\t0\n"
                "br-x\t0000E70A\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
            )
            compose, _calls = self._compose(routes=None)
            with self.assertRaisesRegex(TechSaraError, r"host route 10\.231\.0\.0/16"):
                cli._check_v1relay_subnet(compose, proc_net_route=proc)
            compose, _calls = self._compose(routes=None)
            with mock.patch.object(cli, "_step") as step:
                self.assertEqual(cli._check_v1relay_subnet(compose, proc_net_route=Path(name) / "missing")["status"], "free")
            self.assertIn("could not read the host's routes", str(step.call_args_list))

    def test_catch_all_local_broadcast_and_default_routes_are_not_ranges_in_use(self) -> None:
        routes = cli._ipv4_routes(HOST_ROUTES)
        self.assertIn(ipaddress.ip_network("100.101.102.103/32"), routes)
        self.assertIn(ipaddress.ip_network("198.51.100.0/24"), routes, "the prefix after a route type")
        self.assertNotIn(ipaddress.ip_network("0.0.0.0/1"), routes)
        self.assertFalse(any(route.overlaps(ipaddress.ip_network("10.231.231.0/28")) for route in routes))

    def test_a_chain_without_the_relay_network_checks_nothing(self) -> None:
        compose, calls = self._compose(subnet=None)
        self.assertEqual(cli._check_v1relay_subnet(compose), {"status": "not_rendered"})
        self.assertEqual(calls, [])

    def test_up_checks_the_subnet_before_the_first_service_starts(self) -> None:
        source = LAUNCHER_CLI.read_text(encoding="utf-8")
        body = source[source.index("def _cmd_up("):source.index("def _compose_from_state(")]
        self.assertLess(body.index("_check_v1relay_subnet(compose)"), body.index("result = _start_compose("))

    @unittest.skipUnless(
        os.environ.get("TECHSARA_V1RELAY_LIVE_NETWORK_TEST") == "1" and shutil.which("docker") and shutil.which("ip"),
        "opt-in (TECHSARA_V1RELAY_LIVE_NETWORK_TEST=1): creates a Docker network on the daemon",
    )
    def test_a_real_overlapping_docker_network_is_refused_by_name(self) -> None:
        """Against a real Docker daemon and routes: one throwaway network, removed at the end.

        Opt-in because it CREATES a network: on the deploy host (the Actions
        runner is that host) a test killed before its cleanup would leave
        10.231.0.0/16 taken, which is exactly what stops v1relay.
        """
        from techsara_cli.utils import run_command
        probe = subprocess.run(["docker", "network", "ls"], capture_output=True, text=True, check=False)
        if probe.returncode != 0:
            self.skipTest("no Docker daemon")
        wide = f"techsara-v1relay-test-{os.getpid()}"
        made = subprocess.run(["docker", "network", "create", "--internal", "--subnet", "10.231.0.0/16", wide], capture_output=True, text=True, check=False)
        if made.returncode != 0:
            self.skipTest(f"cannot create a throwaway network: {made.stderr.strip()}")
        self.addCleanup(subprocess.run, ["docker", "network", "rm", wide], capture_output=True, check=False)
        compose, _calls = self._compose()
        compose.runner = run_command
        with self.assertRaisesRegex(TechSaraError, rf"Docker network {re.escape(wide)} 10\.231\.0\.0/16"):
            cli._check_v1relay_subnet(compose)


@unittest.skipUnless(overlays.COMPOSE_AVAILABLE, "Docker Compose v2.24+ is required")
class ComposeRendersThePinnedGatewayTests(unittest.TestCase):
    """What `up -d` compares, rendered by Compose itself."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="techsara-v1gw-compose-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        shutil.copyfile(REPO_ROOT / "compose.yaml", self.project / "compose.yaml")
        shutil.copytree(REPO_ROOT / "gateway", self.project / "gateway", ignore=shutil.ignore_patterns("node_modules"))
        (self.project / "orchestrator" / "app").mkdir(parents=True)
        (self.project / "orchestrator" / "app" / "main.py").write_text("VERSION = 1\n")
        runtime = self.project / ".runtime"
        runtime.mkdir()
        self.generated = runtime / "generated.env"
        self.generated.write_text("MAIN_MODEL=fixture\nENGINE_CONTROLLER_CODE_SHA=" + "1" * 64 + "\n")
        self.secrets = runtime / "secrets.env"
        self.secrets.write_text("POSTGRES_PASSWORD=fixture-password\n")

    def _render(self) -> tuple[dict, dict[str, str]]:
        sha = cli.v1_gateway_code_sha(self.project)
        base = [
            "docker", "compose", "--project-name", "sf-local-ai", "--project-directory", str(self.project),
            "--env-file", str(self.secrets), "--env-file", str(self.generated),
            "-f", str(self.project / "compose.yaml"),
        ]
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("V1_GATEWAY", "PUBLIC_API", "TRUSTED_", "TECHSARA_", "POSTGRES_"))
        }
        environment.update({
            "V1_GATEWAY_CODE_SHA": sha,
            "TECHSARA_GENERATED_ENV": str(self.generated),
            "TECHSARA_SECRET_ENV": str(self.secrets),
        })
        rendered = subprocess.run(base + ["config", "--format", "json"], capture_output=True, text=True, env=environment, timeout=120, check=False)
        self.assertEqual(rendered.returncode, 0, rendered.stderr[-2000:])
        hashed = subprocess.run(base + ["config", "--hash=*"], capture_output=True, text=True, env=environment, timeout=120, check=False)
        self.assertEqual(hashed.returncode, 0, hashed.stderr[-2000:])
        hashes = dict(line.split(" ", 1) for line in hashed.stdout.strip().splitlines())
        return json.loads(rendered.stdout), hashes

    def test_the_gateway_renders_with_its_digest_tag_its_guards_and_nothing_that_moves_per_deploy(self) -> None:
        document, _hashes = self._render()
        gateway = document["services"]["v1-gateway"]
        self.assertEqual(gateway["image"], f"sf-local-ai-v1-gateway:{cli.v1_gateway_code_sha(self.project)}")
        self.assertNotIn("env_file", gateway)
        self.assertNotIn("depends_on", gateway)
        self.assertNotIn("ports", gateway)
        self.assertNotIn("MAIN_MODEL", gateway["environment"], "a generated.env key reached the gateway")
        self.assertEqual(gateway["environment"]["ORCHESTRATOR_URL"], "http://orchestrator-v1relay:8080")
        self.assertTrue(gateway["read_only"])
        self.assertTrue(gateway["init"])
        self.assertEqual(gateway["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", gateway["security_opt"])
        self.assertEqual(gateway["ulimits"]["nofile"], {"soft": 65536, "hard": 65536})
        self.assertEqual(gateway["pids_limit"], 256)
        self.assertEqual(gateway["healthcheck"]["test"], ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8090/healthz"])
        self.assertEqual(gateway["stop_grace_period"], "30s")
        self.assertEqual([v["target"] for v in gateway["volumes"]], ["/spool"])
        self.assertEqual(document["volumes"]["v1gateway_spool"]["name"], "sf-local-ai_v1gateway_spool")
        # The frontend's LAN path relays through it.
        self.assertEqual(document["services"]["frontend"]["environment"]["V1_GATEWAY_URL"], "http://v1-gateway:8090")
        # The in-memory body budget reaches it (blank = the gateway's 256 MiB).
        self.assertIn("V1_GATEWAY_MEMORY_BUDGET_BYTES", gateway["environment"])

    def test_the_frontend_edge_the_gateway_and_the_orchestrator_read_the_same_body_caps(self) -> None:
        """Review 2026-09-14: the frontend got only PUBLIC_API_MAX_BODY_BYTES."""
        caps = ("PUBLIC_API_MAX_BODY_BYTES", "PUBLIC_API_MAX_MEDIA_BODY_BYTES", "PUBLIC_API_MAX_AUDIO_BODY_BYTES",
                "PUBLIC_API_MAX_POOLING_BODY_BYTES", "PUBLIC_API_FILES_MAX_BODY_BYTES", "PUBLIC_API_FILES_PART_MAX_BYTES")
        compose_text = (self.project / "compose.yaml").read_text(encoding="utf-8")
        for service in ("frontend", "v1-gateway"):
            block = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  [a-z][\w-]*:\n|^networks:)", compose_text, re.S | re.M).group(1)
            for cap in caps:
                with self.subTest(service=service, cap=cap):
                    self.assertIn(f"{cap}: ${{{cap}:-}}", block)

    def test_only_the_gateway_and_the_orchestrator_share_the_relay_and_the_gateway_address_is_never_handed_out(self) -> None:
        document, _hashes = self._render()
        members = sorted(name for name, service in document["services"].items() if "v1relay" in (service.get("networks") or {}))
        self.assertEqual(members, ["orchestrator", "v1-gateway"])
        network = document["networks"]["v1relay"]
        self.assertTrue(network["internal"])
        (pool,) = network["ipam"]["config"]
        subnet, dynamic = ipaddress.ip_network(pool["subnet"]), ipaddress.ip_network(pool["ip_range"])
        pinned = ipaddress.ip_address(document["services"]["v1-gateway"]["networks"]["v1relay"]["ipv4_address"])
        self.assertIn(pinned, subnet)
        self.assertNotIn(pinned, dynamic, "a container started before the gateway could take its address")
        self.assertNotEqual(pinned, ipaddress.ip_address(pool["gateway"]))
        # Review finding 2026-09-14: 172.30.231.0/28 sat inside Docker's default
        # pools, so the next network any project created could take its /16.
        for taken in DOCKER_DEFAULT_POOLS + THIS_HOSTS_RANGES:
            self.assertFalse(subnet.overlaps(taken), f"{subnet} overlaps {taken}")
        self.assertEqual(document["services"]["orchestrator"]["networks"]["v1relay"]["aliases"], ["orchestrator-v1relay"])
        # The alias the gateway dials exists on the relay network only.
        for name, attachment in document["services"]["orchestrator"]["networks"].items():
            if name != "v1relay":
                self.assertNotIn("orchestrator-v1relay", (attachment or {}).get("aliases") or [])

    def test_an_orchestrator_only_change_leaves_the_sha_and_the_gateway_definition_byte_identical(self) -> None:
        before_document, before_hashes = self._render()
        before_sha = cli.v1_gateway_code_sha(self.project)
        # What a routine commit and deploy change: orchestrator code, a new
        # generated.env key, the controller digest, a gateway doc and a test.
        (self.project / "orchestrator" / "app" / "main.py").write_text("VERSION = 2\n")
        self.generated.write_text("MAIN_MODEL=fixture-2\nENGINE_CONTROLLER_CODE_SHA=" + "2" * 64 + "\nA_NEW_KEY=1\n")
        with (self.project / "gateway" / "README.md").open("a") as handle:
            handle.write("\n## Another operator note\n")
        (self.project / "gateway" / "test" / "another.test.cjs").write_text("// new\n")
        after_document, after_hashes = self._render()
        self.assertEqual(cli.v1_gateway_code_sha(self.project), before_sha)
        self.assertEqual(
            json.dumps(after_document["services"]["v1-gateway"], sort_keys=True),
            json.dumps(before_document["services"]["v1-gateway"], sort_keys=True),
        )
        self.assertEqual(after_hashes["v1-gateway"], before_hashes["v1-gateway"])
        # Control: the same edit does reach a service that folds generated.env.
        self.assertNotEqual(
            after_document["services"]["frontend"]["environment"].get("MAIN_MODEL"),
            before_document["services"]["frontend"]["environment"].get("MAIN_MODEL"),
        )

    def test_a_gateway_code_change_alters_the_sha_the_image_and_the_hash(self) -> None:
        before_document, before_hashes = self._render()
        relay = self.project / "gateway" / "lib" / "relay.cjs"
        relay.write_bytes(relay.read_bytes() + b"\n// a behaviour change\n")
        after_document, after_hashes = self._render()
        self.assertNotEqual(after_document["services"]["v1-gateway"]["image"], before_document["services"]["v1-gateway"]["image"])
        self.assertNotEqual(after_hashes["v1-gateway"], before_hashes["v1-gateway"])
        # And it moves nothing else.
        others = sorted(set(before_hashes) - {"v1-gateway"})
        self.assertTrue(others)
        self.assertEqual({k: after_hashes[k] for k in others}, {k: before_hashes[k] for k in others})


class DeployShGatewayGuardTests(unittest.TestCase):
    """scripts/deploy.sh's guards, run for real with a fake docker on PATH."""

    FUNCTIONS = (
        "bounded_wait", "v1_gateway_relays", "v1_gateway_guard", "public_api_in_flight",
        "live_orchestrator_suspends_public_runs", "public_api_not_resumable_in_flight", "public_api_live_in_flight",
        "public_work_guard", "v1_gateway_health", "pin_v1_gateway",
    )

    def setUp(self) -> None:
        if shutil.which("bash") is None or shutil.which("python3") is None:
            self.skipTest("bash and python3 are required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-v1gw-deploy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        fake = self.bin / "docker"
        # Each answer comes from a file under $FAKE (absent = the command fails),
        # and a counter file lets a value change between polls.
        fake.write_text(
            """#!/usr/bin/env bash
            S="$FAKE"
            printf '%s\\n' "$*" >>"$S/calls"
            answer() { [ -f "$S/$1" ] || exit 1; cat "$S/$1"; exit 0; }
            # One line per call; the last line repeats.
            next_line() {
              [ -f "$S/$1" ] || exit 1
              n="$(head -n1 "$S/$1")"; tail -n +2 "$S/$1" >"$S/$1.next"
              [ -s "$S/$1.next" ] && mv "$S/$1.next" "$S/$1" || rm -f "$S/$1.next"
              printf '%s\\n' "$n"; exit 0
            }
            case "$*" in
              "inspect sf-local-ai-v1-gateway-1 --format {{.State.Running}}") answer running ;;
              "inspect sf-local-ai-v1-gateway-1 --format {{.Id}}") next_line gw_id ;;
              *"com.docker.compose.config-hash"*) answer have_hash ;;
              "inspect sf-local-ai-v1-gateway-1 --format {{.Config.Image}}") answer image ;;
              *"State.Health.Status"*) answer health ;;
              "exec sf-local-ai-v1-gateway-1 wget -q -O - http://127.0.0.1:8090/healthz")
                 n="$(next_line relays)" || exit 1
                 printf '{"status":"ok","relays":%s}\\n' "$n"; exit 0 ;;
              "exec sf-local-ai-v1-gateway-1 timeout 20 wget -S"*)
                 [ -f "$S/relay_status" ] || exit 1
                 printf '  HTTP/1.1 %s Whatever\\n' "$(cat "$S/relay_status")" >&2; exit 1 ;;
              "exec sf-local-ai-orchestrator-1 test -f /app/app/publicapi/durable.py") [ -f "$S/suspends" ]; exit $? ;;
              "exec sf-local-ai-orchestrator-1 printenv PUBLIC_API_RESUME_ENABLED") answer resume_enabled ;;
              "inspect sf-local-ai-orchestrator-1 --format {{.State.Running}}") answer orch_running ;;
              "inspect sf-local-ai-orchestrator-1 --format {{.State.StartedAt}}") answer orch_started ;;
              "inspect sf-local-ai-postgres-1") [ -f "$S/postgres" ]; exit $? ;;
              *"to_regclass"*) answer table_present ;;
              *"column_name = 'resumable'"*) answer resumable_column ;;
              *"resumable IS NOT TRUE"*) answer not_resumable ;;
              *"created_at >= '2026-09-13T10:00:00.123456789Z'::timestamptz"*) next_line in_flight_live ;;
              *"FROM api_responses"*) next_line in_flight ;;
            esac
            exit 1
            """.replace("\n            ", "\n")
        )
        fake.chmod(0o755)

    def _run(self, body: str, *, env: dict[str, str] | None = None, timeout: float = 60) -> tuple[subprocess.CompletedProcess, float]:
        functions = "".join(_bash_function(DEPLOY_SH, name) for name in self.FUNCTIONS)
        script = (
            "set -euo pipefail\n"
            f"ROOT={json.dumps(str(self.root))}\n"
            f"LOG={json.dumps(str(self.root / 'deploy.log'))}\n"
            "FULL=\"${FULL:-0}\"\n"
            'V1_GATEWAY_CONTAINER="sf-local-ai-v1-gateway-1"\nPUBLIC_WORK_SINCE=""\nV1_GATEWAY_ID_BEFORE=""\n'
            "say() { printf '%s\\n' \"$*\"; }\n"
            "dr_container_for() { printf 'sf-local-ai-%s-1' \"$1\"; }\n"
            "dr_env_value() { :; }\n"
            f"{_bash_function(DEPLOY_SH, 'public_api_psql_ro')}"
            f"{_bash_function(DEPLOY_SH, 'v1_gateway_code_sha')}"
            f"{functions}"
            # The rendered hash needs the launcher's chain; the test says what it is.
            "v1_gateway_rendered_hash() { cat \"$FAKE/rendered_hash\" 2>/dev/null || true; }\n"
            f"{body}\n"
        )
        environment = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.state)}
        environment.update(env or {})
        started = time.monotonic()
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment, timeout=timeout, check=False)
        return result, time.monotonic() - started

    def _set(self, **files: str) -> None:
        for name, value in files.items():
            (self.state / name).write_text(value + "\n")

    def test_an_unchanged_gateway_is_reported_unchanged_and_never_waited_for(self) -> None:
        self._set(running="true", have_hash="a" * 64, rendered_hash="a" * 64, relays="7")
        result, elapsed = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v1-gateway unchanged (code sha ffffffffffff", result.stdout)
        self.assertLess(elapsed, 5)
        self.assertNotIn("healthz", (self.state / "calls").read_text(), "an unchanged gateway was polled")

    def test_a_recreate_waits_for_the_relays_and_goes_ahead_as_soon_as_they_finish(self) -> None:
        self._set(running="true", have_hash="a" * 64, rendered_hash="b" * 64, relays="2\n1\n0")
        result, elapsed = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard",
                                    env={"DEPLOY_GATEWAY_DRAIN_DEADLINE": "30", "DEPLOY_GATEWAY_DRAIN_POLL": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("will be recreated - its definition changed (aaaaaaaaaaaa -> bbbbbbbbbbbb", result.stdout)
        self.assertIn("waiting up to 30s for its 2 relay(s)", result.stdout)
        self.assertIn("its relays finished", result.stdout)
        self.assertLess(elapsed, 10)

    def test_a_recreate_never_waits_past_its_deadline(self) -> None:
        self._set(running="true", have_hash="a" * 64, rendered_hash="b" * 64, relays="3")
        result, elapsed = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard",
                                    env={"DEPLOY_GATEWAY_DRAIN_DEADLINE": "2", "DEPLOY_GATEWAY_DRAIN_POLL": "1"}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING still relaying 3 connection(s) after 2s; deploying anyway", result.stdout)
        self.assertLess(elapsed, 8)

    def test_full_always_counts_as_a_recreate_and_a_gateway_that_cannot_answer_is_not_waited_for(self) -> None:
        self._set(running="true", have_hash="a" * 64, rendered_hash="a" * 64)
        result, elapsed = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard", env={"FULL": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--full removes every container, this one included; it cannot report its relays, so not waiting", result.stdout)
        self.assertLess(elapsed, 5)

    def test_a_first_deploy_creates_it_and_a_tree_without_it_warns_about_the_tunnel_route(self) -> None:
        result, _ = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard")
        self.assertIn("not running; this deploy creates it", result.stdout)
        self._set(running="true")
        result, _ = self._run("unset V1_GATEWAY_CODE_SHA; v1_gateway_guard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING this tree has no gateway/", result.stdout)
        self.assertIn("delete that route now", result.stdout)

    def test_public_runs_on_an_orchestrator_that_suspends_them_are_reported_and_not_waited_for(self) -> None:
        self._set(postgres="", table_present="t", in_flight="4", suspends="", orch_running="true")
        result, elapsed = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "30", "DEPLOY_PUBLIC_WORK_POLL": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 queued/in-progress /v1 run(s); the running orchestrator suspends the resumable ones", result.stdout)
        self.assertNotIn("waiting up to", result.stdout)
        self.assertLess(elapsed, 5)

    def test_public_runs_on_an_orchestrator_that_cannot_suspend_them_hold_the_deploy_only_until_the_deadline(self) -> None:
        self._set(postgres="", table_present="t", in_flight="5", in_flight_live="2",
                  orch_running="true", orch_started="2026-09-13T10:00:00.123456789Z")
        result, elapsed = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "2", "DEPLOY_PUBLIC_WORK_POLL": "1"}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 queued/in-progress /v1 run(s) on an orchestrator that CANNOT suspend them", result.stdout)
        self.assertIn("WARNING 2 run(s) still in flight after 2s; deploying anyway", result.stdout)
        # bash's SECONDS counts whole seconds, so a 2 s bound can end after just over 1 s.
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 8)
        # ... and goes ahead as soon as they finish.
        self._set(in_flight_live="2\n2\n0")
        result, elapsed = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "60", "DEPLOY_PUBLIC_WORK_POLL": "1"})
        self.assertIn("in-flight /v1 runs finished (or became unreadable); continuing", result.stdout)
        self.assertLess(elapsed, 10)

    def test_nothing_waits_for_runs_nobody_is_executing_or_during_a_rollback(self) -> None:
        self._set(postgres="", table_present="t", in_flight="3")
        # The orchestrator is down: no process is running those rows.
        result, elapsed = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "30"})
        self.assertIn("the orchestrator is not running, so nothing is executing them; not waiting", result.stdout)
        # Rows older than the running process: code that cannot suspend cannot have resumed them.
        self._set(orch_running="true", orch_started="2026-09-13T10:00:00.123456789Z", in_flight_live="0")
        result, _ = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "30"})
        self.assertIn("no /v1 run started since the orchestrator did", result.stdout)
        # A start time that is not Docker's RFC 3339 never reaches SQL.
        self._set(orch_started="2026-09-13'; DROP TABLE api_responses; --", in_flight_live="9")
        result, _ = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "30"})
        self.assertIn("cannot tell which /v1 runs the running orchestrator started; not waiting", result.stdout)
        self.assertNotIn("DROP TABLE", (self.state / "calls").read_text())
        # A rollback reports and never waits, for public runs or for the gateway.
        self._set(orch_started="2026-09-13T10:00:00.123456789Z", in_flight_live="2")
        result, elapsed = self._run("public_work_guard", env={"DEPLOY_PUBLIC_WORK_DEADLINE": "30", "DEPLOY_ROLLING_BACK": "1"})
        self.assertIn("rolling back; not waiting", result.stdout)
        self.assertLess(elapsed, 5)
        self._set(running="true", have_hash="a" * 64, rendered_hash="b" * 64, relays="4")
        result, elapsed = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard", env={"DEPLOY_ROLLING_BACK": "1"})
        self.assertIn("cutting 4 relay(s) now (rolling back)", result.stdout)
        self.assertLess(elapsed, 5)

    def test_an_unreadable_or_older_database_never_holds_the_deploy(self) -> None:
        result, elapsed = self._run("public_work_guard")
        self.assertIn("cannot read the in-flight /v1 runs (database unreachable); not waiting", result.stdout)
        self._set(postgres="", table_present="f")
        result, elapsed = self._run("public_work_guard")
        self.assertIn("no queued or in-progress /v1 runs", result.stdout)
        self.assertLess(elapsed, 5)

    def test_the_health_gate_requires_the_pinned_healthy_gateway_and_only_reports_its_relay_path(self) -> None:
        sha = "f" * 64
        self._set(image=f"sf-local-ai-v1-gateway:{'e' * 64}", health="healthy", relay_status="401")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("FAILED", result.stdout)
        self._set(image=f"sf-local-ai-v1-gateway:{sha}", health="unhealthy")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("not healthy", result.stdout)
        self.assertIn("FAILED", result.stdout)
        self._set(health="healthy", relay_status="401")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("v1-gateway ok (code sha ffffffffffff; /v1/models relayed, the orchestrator answered 401)", result.stdout)
        self.assertIn("PASSED", result.stdout)
        self._set(relay_status="503")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("WARNING v1-gateway could not reach the orchestrator (status 503)", result.stdout)
        self.assertIn("PASSED", result.stdout)
        result, _ = self._run("unset V1_GATEWAY_CODE_SHA; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("PASSED", result.stdout)

    def test_by_default_a_recreate_with_relays_in_flight_is_reported_and_never_waited_for(self) -> None:
        """Design deploy_survival: deploys never wait (review 2026-09-14: the
        bounded waits defaulted to 300 s + 600 s)."""
        self._set(running="true", have_hash="a" * 64, rendered_hash="b" * 64, relays="3")
        environment = {"DEPLOY_GATEWAY_DRAIN_DEADLINE": "", "DEPLOY_PUBLIC_WORK_DEADLINE": ""}
        result, elapsed = self._run(
            "unset DEPLOY_GATEWAY_DRAIN_DEADLINE DEPLOY_PUBLIC_WORK_DEADLINE; V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard",
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("will be recreated - its definition changed (aaaaaaaaaaaa -> bbbbbbbbbbbb", result.stdout)
        self.assertIn("cutting 3 relay(s) now (deploys never wait", result.stdout)
        self.assertNotIn("waiting up to", result.stdout)
        self.assertLess(elapsed, 3)
        self.assertEqual((self.state / "calls").read_text().count("healthz"), 1, "asked once for the count, never polled")

    def test_by_default_runs_an_orchestrator_cannot_suspend_are_reported_as_failing_and_never_waited_for(self) -> None:
        self._set(postgres="", table_present="t", in_flight="5", in_flight_live="2",
                  orch_running="true", orch_started="2026-09-13T10:00:00.123456789Z")
        result, elapsed = self._run("unset DEPLOY_PUBLIC_WORK_DEADLINE; public_work_guard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 queued/in-progress /v1 run(s) on an orchestrator that CANNOT suspend them", result.stdout)
        self.assertIn("not waiting (deploys never wait; DEPLOY_PUBLIC_WORK_DEADLINE opts a hand-run deploy in) - they end failed", result.stdout)
        self.assertLess(elapsed, 3)

    def test_resume_switched_off_counts_as_an_orchestrator_that_cannot_suspend(self) -> None:
        """Review 2026-09-14: durable.py alone was taken as 'suspends'."""
        self._set(postgres="", table_present="t", in_flight="4", in_flight_live="4", suspends="",
                  orch_running="true", orch_started="2026-09-13T10:00:00.123456789Z", resume_enabled="False")
        result, _ = self._run("public_work_guard")
        self.assertIn("4 queued/in-progress /v1 run(s) on an orchestrator that CANNOT suspend them", result.stdout)
        self.assertNotIn("suspends the resumable ones", result.stdout)
        for spelling in ("true", " On ", "1", "yes"):
            with self.subTest(resume_enabled=spelling):
                self._set(resume_enabled=spelling)
                result, _ = self._run("public_work_guard")
                self.assertIn("the running orchestrator suspends the resumable ones on SIGTERM", result.stdout)
        # Unset (printenv fails) is config.py's default: on.
        (self.state / "resume_enabled").unlink()
        result, _ = self._run("public_work_guard")
        self.assertIn("the running orchestrator suspends the resumable ones on SIGTERM", result.stdout)

    def test_runs_that_are_not_resumable_are_named_as_cut_even_when_the_orchestrator_suspends(self) -> None:
        self._set(postgres="", table_present="t", in_flight="4", suspends="", orch_running="true",
                  resumable_column="t", not_resumable="3")
        result, _ = self._run("public_work_guard")
        self.assertIn("3 of them are not resumable (foreground sync/stream runs, or store:false) and will be cut", result.stdout)
        self._set(not_resumable="0")
        result, _ = self._run("public_work_guard")
        self.assertNotIn("not resumable", result.stdout)
        # A database before V36 has no such column: nothing is claimed.
        self._set(resumable_column="f", not_resumable="3")
        result, _ = self._run("public_work_guard")
        self.assertNotIn("not resumable", result.stdout)

    def test_an_unhealthy_gateway_this_deploy_did_not_touch_is_a_warning_not_a_failed_deploy(self) -> None:
        """Review finding 2026-09-14: returning 1 here failed the deploy and then its rollback."""
        sha = "f" * 64
        self._set(running="true", have_hash="a" * 64, rendered_hash="a" * 64, gw_id="abc",
                  image=f"sf-local-ai-v1-gateway:{sha}", health="unhealthy")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_guard >/dev/null; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING v1-gateway is 'unhealthy' and this deploy did not change it; not failing the deploy for it.", result.stdout)
        self.assertIn("docker restart sf-local-ai-v1-gateway-1", result.stdout)
        self.assertIn("PASSED", result.stdout)

    def test_an_unhealthy_gateway_this_deploy_created_or_recreated_still_fails_the_deploy(self) -> None:
        sha = "f" * 64
        # Recreated: a different container id after `up`.
        self._set(running="true", have_hash="a" * 64, rendered_hash="b" * 64, gw_id="abc\ndef",
                  image=f"sf-local-ai-v1-gateway:{sha}", health="unhealthy")
        result, _ = self._run(f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_guard >/dev/null; v1_gateway_health && echo PASSED || echo FAILED")
        self.assertIn("health: v1-gateway is 'unhealthy', not healthy", result.stdout)
        self.assertIn("FAILED", result.stdout)
        # Created: no container before `up`.
        (self.state / "gw_id").unlink()
        self._set(running="false")
        body = (
            f"V1_GATEWAY_CODE_SHA={sha}; v1_gateway_guard >/dev/null; "
            f"printf 'new\\n' >\"$FAKE/gw_id\"; v1_gateway_health && echo PASSED || echo FAILED"
        )
        result, _ = self._run(body)
        self.assertIn("FAILED", result.stdout)

    def test_a_running_unpinned_gateway_is_named(self) -> None:
        self._set(running="true", image="sf-local-ai-v1-gateway:unpinned", have_hash="a" * 64, rendered_hash="b" * 64, relays="0")
        result, _ = self._run("V1_GATEWAY_CODE_SHA=" + "f" * 64 + " v1_gateway_guard")
        self.assertIn("WARNING it runs sf-local-ai-v1-gateway:unpinned - a hand-run `docker compose` without V1_GATEWAY_CODE_SHA", result.stdout)

    def test_apply_pins_before_the_first_render_and_guards_before_the_engine_lock(self) -> None:
        text = DEPLOY_SH.read_text(encoding="utf-8")
        apply = _bash_function(DEPLOY_SH, "apply")
        self.assertLess(apply.index('pin_v1_gateway "$ROOT"'), apply.index('"$ROOT/scripts/deploy-record.sh"'))
        self.assertLess(apply.index("public_work_guard"), apply.index("engine_lock_acquire"))
        self.assertLess(apply.index("v1_gateway_guard"), apply.index("engine_lock_acquire"))
        self.assertLess(text.index("DEPLOY_ROLLING_BACK=1"), text.index('if apply "$PREVIOUS" && health; then'))
        self.assertIn("v1_gateway_health || return 1", _bash_function(DEPLOY_SH, "health"))
        # The option exits before the lock, git or docker.
        self.assertLess(text.index('if [ "$PRINT_V1_GATEWAY_SHA" = 1 ]'), text.index("dr_lock_acquire"))


if __name__ == "__main__":
    unittest.main()
