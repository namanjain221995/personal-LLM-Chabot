"""No unauthenticated listener in this repository defaults onto the LAN.

Every test here corresponds to an exposure the developer-platform audit
measured on the DGX head on 2026-09-13 (docs/developer-platform/AUDIT.md):

* F050/F042/F012/F065 -- the main vLLM engine, which has no ``--api-key``,
  answered ``GET /v1/models`` with 200 on the office LAN, the tailnet and both
  RoCE rails, because ``PUBLISH_MODEL_PORTS=true`` made the launcher render
  ``CLUSTER_API_BIND_ADDRESS=0.0.0.0`` for the host-network head, and the
  single-node published overlay put port 8000 on ``TECHSARA_BIND_ADDRESS``
  (the APPLICATION's bind) instead of ``TECHSARA_MODEL_BIND_ADDRESS``;
* F051 -- the speech engine's compose file defaulted its bind to a literal
  office-LAN address;
* F056 -- node-exporter defaulted to 0.0.0.0:9100, full host inventory;
* the engine controller's /state and /metrics listened on 0.0.0.0:9838.

The resolution and file checks run on the standard library alone. The render
checks shell out to real ``docker compose config`` through the same harness
test_compose_overlays.py uses (the launcher's own file chain and env layers),
and skip when Docker Compose v2.24+ is unavailable. Nothing here starts,
pulls, or touches a running container.
"""

from __future__ import annotations

import ast
import re
import unittest
from unittest.mock import patch

try:
    from . import test_compose_overlays as overlays
    from .support import REPO_ROOT, fake_discovery
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    import test_compose_overlays as overlays
    from support import REPO_ROOT, fake_discovery

from techsara_cli import environment
from techsara_cli.cluster import ClusterDetectors, resolve_cluster_settings

HEAD_IP = "192.168.100.1"
WORKER_IP = "192.168.100.2"
BRIDGE_GATEWAY = "172.17.0.1"
LOOPBACK = "127.0.0.1"
WILDCARD = "0.0.0.0"

#: Container ports of the model engines across every overlay (vllm 30000,
#: the pre-launcher file's 30001, router 30002, embed 30003, OCR 30004,
#: reranker 30005). A host publish of any of these is an unauthenticated API.
MODEL_CONTAINER_PORTS = {"30000", "30001", "30002", "30003", "30004", "30005"}

#: Every compose file a stack on this repository can be started from.
COMPOSE_FILES = sorted(
    [REPO_ROOT / "compose.yaml", REPO_ROOT / "docker-compose.yml", *(REPO_ROOT / "compose").glob("*.yaml")]
)


def _detectors(gateway: str | None) -> ClusterDetectors:
    return ClusterDetectors(
        ifname_for_ip=lambda ip: {HEAD_IP: "enP2p1s0f1np1"}.get(ip),
        hcas_for_ifnames=lambda names: ["rocep1s0f1" for name in names if name == "enP2p1s0f1np1"],
        docker_bridge_gateway=lambda: gateway,
    )


def _head_bind(*, publish_model_ports: bool, gateway: str | None) -> str:
    values = resolve_cluster_settings(
        {"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": HEAD_IP, "CLUSTER_WORKER_IP": WORKER_IP},
        profile_id="dgx-spark",
        publish_model_ports=publish_model_ports,
        context=262144,
        startup_arguments=("--quantization", "modelopt"),
        vllm_port=8000,
        detectors=_detectors(gateway),
    )
    return values["CLUSTER_API_BIND_ADDRESS"]


class HeadBindResolutionTests(unittest.TestCase):
    """launcher/techsara_cli/cluster.py decides where the host-network head listens."""

    def test_the_dual_mode_head_binds_the_docker_bridge_gateway_even_when_model_ports_are_published(self) -> None:
        # The production .env has PUBLISH_MODEL_PORTS=true; this is the exact
        # input that rendered 0.0.0.0 and put the engine on the LAN.
        self.assertEqual(_head_bind(publish_model_ports=True, gateway=BRIDGE_GATEWAY), BRIDGE_GATEWAY)
        self.assertEqual(_head_bind(publish_model_ports=False, gateway=BRIDGE_GATEWAY), BRIDGE_GATEWAY)

    def test_the_dual_mode_head_falls_back_to_loopback_and_never_to_every_interface_when_the_gateway_is_unknown(self) -> None:
        for publish in (True, False):
            with self.subTest(publish_model_ports=publish):
                self.assertEqual(_head_bind(publish_model_ports=publish, gateway=None), LOOPBACK)

    def test_the_bridge_gateway_is_probed_whether_or_not_model_ports_are_published(self) -> None:
        # The old code skipped the probe when publishing, because it was about
        # to bind every interface anyway. Not probing IS the exposure.
        for publish in (True, False):
            with self.subTest(publish_model_ports=publish):
                probed: list[str] = []
                detectors = ClusterDetectors(
                    ifname_for_ip=_detectors(None).ifname_for_ip,
                    hcas_for_ifnames=_detectors(None).hcas_for_ifnames,
                    docker_bridge_gateway=lambda: probed.append("bridge") or BRIDGE_GATEWAY,
                )
                resolve_cluster_settings(
                    {"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": HEAD_IP, "CLUSTER_WORKER_IP": WORKER_IP},
                    profile_id="dgx-spark", publish_model_ports=publish, context=262144,
                    startup_arguments=(), vllm_port=8000, detectors=detectors,
                )
                self.assertEqual(probed, ["bridge"])


class ComposeFileBindTests(unittest.TestCase):
    """The committed files, read as text: the VARIABLE a port names is the
    thing under test, and interpolation erases it from a rendered config."""

    def test_no_compose_file_defaults_a_bind_to_every_interface_or_to_a_lan_address(self) -> None:
        # `${VAR:-0.0.0.0}` and `${VAR:-192.168.x.y}` are the two shapes that
        # shipped (node-exporter's MONITORING_NODE_BIND, whisper's WHISPER_BIND):
        # a deployment that never set the variable got the widest bind.
        wide_default = re.compile(
            r"\$\{[A-Z0-9_]+:-(0\.0\.0\.0|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\}"
        )
        offenders = []
        for path in COMPOSE_FILES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if wide_default.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a bind must default to loopback or the bridge gateway, or be required")

    def test_every_published_model_port_names_the_model_bind_address_and_never_the_application_bind(self) -> None:
        port_line = re.compile(r'^\s*-\s*"(?P<spec>[^"]+)"\s*$')
        checked = 0
        offenders = []
        for path in COMPOSE_FILES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = port_line.match(line)
                if not match:
                    continue
                spec = match.group("spec")
                target = spec.rsplit(":", 1)[-1]
                if target not in MODEL_CONTAINER_PORTS or spec.count(":") < 1:
                    continue
                checked += 1
                if not spec.startswith("${TECHSARA_MODEL_BIND_ADDRESS:-127.0.0.1}:"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {spec}")
        # The three published overlays carry eight model ports and the
        # pre-launcher docker-compose.yml five; a lower count means the pattern
        # stopped matching, not that the files are safe.
        self.assertGreaterEqual(checked, 13)
        self.assertEqual(offenders, [], "an unauthenticated model port follows TECHSARA_MODEL_BIND_ADDRESS")

    def test_node_exporter_on_the_head_defaults_to_the_bridge_gateway_prometheus_dials(self) -> None:
        text = (REPO_ROOT / "compose" / "compose.monitoring.yaml").read_text(encoding="utf-8")
        match = re.search(r"--web\.listen-address=\$\{MONITORING_NODE_BIND:-([^}]+)\}:9100", text)
        self.assertIsNotNone(match, "node-exporter's listen address is no longer spelled the way this test reads it")
        self.assertEqual(match.group(1), BRIDGE_GATEWAY)
        # And Prometheus still scrapes it through that same gateway.
        prometheus = (REPO_ROOT / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
        self.assertIn('"host.docker.internal:9100"', prometheus)

    def test_the_speech_engine_refuses_to_start_without_an_explicit_bind(self) -> None:
        text = (REPO_ROOT / "compose" / "compose.whisper.yaml").read_text(encoding="utf-8")
        self.assertIn('WHISPER_BIND: "${WHISPER_BIND:?', text)
        self.assertNotIn("192.168.9.68}", text)

    def test_the_env_example_warns_that_the_main_model_port_8000_is_unauthenticated_too(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        paragraph = text[: text.index("#TECHSARA_MODEL_BIND_ADDRESS=127.0.0.1")]
        paragraph = paragraph[paragraph.rindex("# Bind address for the UNAUTHENTICATED model APIs"):]
        self.assertIn("(8000 and 8002-8005)", paragraph)
        self.assertNotIn("model APIs (8002-8005)", text)


class EngineControllerBindTests(unittest.TestCase):
    """monitoring/engine-controller/controller.py, read without importing it:
    the launcher stages that file into a python:3.12 container, and its
    `from common import ...` is not importable from this suite's path."""

    CONTROLLER = REPO_ROOT / "monitoring" / "engine-controller" / "controller.py"

    def _bind_addresses(self):
        """Compile controller_bind_addresses on its own and return it."""
        source = self.CONTROLLER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = [
            node for node in tree.body
            if (isinstance(node, ast.FunctionDef) and node.name == "controller_bind_addresses")
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_CONTROLLER_BIND" for t in node.targets))
        ]
        self.assertEqual(len(wanted), 2, "controller.py must define DEFAULT_CONTROLLER_BIND and controller_bind_addresses")
        namespace: dict = {"List": list}
        exec(compile(ast.Module(body=wanted, type_ignores=[]), str(self.CONTROLLER), "exec"), namespace)  # noqa: S102
        return namespace["DEFAULT_CONTROLLER_BIND"], namespace["controller_bind_addresses"]

    def test_the_engine_controller_listens_on_loopback_when_no_bind_is_configured(self) -> None:
        default, bind_addresses = self._bind_addresses()
        self.assertEqual(default, LOOPBACK)
        self.assertEqual(bind_addresses(""), [LOOPBACK])
        # Config's dataclass default and from_env's fallback both use the
        # constant, so neither can drift back to a literal 0.0.0.0.
        source = self.CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("bind: str = DEFAULT_CONTROLLER_BIND", source)
        self.assertIn('env_str("CONTROLLER_BIND", DEFAULT_CONTROLLER_BIND)', source)
        self.assertNotIn('"0.0.0.0"),', source)

    def test_the_engine_controller_listens_on_each_listed_address_once_and_in_order(self) -> None:
        _default, bind_addresses = self._bind_addresses()
        self.assertEqual(bind_addresses("127.0.0.1,172.17.0.1"), [LOOPBACK, BRIDGE_GATEWAY])
        self.assertEqual(bind_addresses(" 127.0.0.1 , 127.0.0.1 ,"), [LOOPBACK])

    def test_an_older_generated_env_with_the_wildcard_collapses_to_one_bind_instead_of_failing_to_bind_twice(self) -> None:
        # "127.0.0.1,0.0.0.0" is what the dual overlay renders from a
        # generated.env written before this fix; binding both on one port is
        # EADDRINUSE, i.e. a controller crash loop on the next routine deploy.
        _default, bind_addresses = self._bind_addresses()
        self.assertEqual(bind_addresses("127.0.0.1,0.0.0.0"), [WILDCARD])


class RenderedExposureTests(unittest.TestCase):
    """The launcher's real Compose plan, rendered by Docker Compose itself."""

    maxDiff = None
    # The harness from test_compose_overlays.py, borrowed method by method: it
    # builds the generated env the launcher would write and runs
    # `docker compose ... config` over the launcher's own -f chain. Borrowing
    # (rather than inheriting) keeps that module's tests from running twice.
    _render = overlays.ComposeOverlayValidationTests._render
    _compose_config = overlays.ComposeOverlayValidationTests._compose_config

    @classmethod
    def setUpClass(cls) -> None:
        if not overlays.COMPOSE_AVAILABLE:
            raise unittest.SkipTest("Docker Compose v2.24+ is required for rendered exposure checks")

    def setUp(self) -> None:
        discovery = patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery())
        discovery.start()
        self.addCleanup(discovery.stop)

    def test_opening_the_app_to_the_lan_leaves_every_published_model_port_on_loopback(self) -> None:
        opened = {"TECHSARA_BIND_ADDRESS": WILDCARD, "PUBLISH_MODEL_PORTS": "true"}
        expected_models = {
            "dgx-spark": ("vllm", "vllm-router", "vllm-embed", "vllm-ocr"),
            "nvidia-large": ("vllm", "vllm-embed"),
            "local-minimal": ("llama-cpp",),
        }
        for name, models in expected_models.items():
            with self.subTest(fixture=name):
                _profile, rendered = self._render(overlays.FIXTURES[name], opened)
                services = rendered["services"]
                # The opt-in really took effect: the APPLICATION is on 0.0.0.0,
                # so a loopback model port below is the split working, not a
                # render that ignored the environment.
                self.assertEqual({p.get("host_ip") for p in services["orchestrator"]["ports"]}, {WILDCARD})
                for service in models:
                    ports = services[service].get("ports") or []
                    self.assertTrue(ports, f"{name}/{service} is not published at all")
                    self.assertEqual({p.get("host_ip") for p in ports}, {LOOPBACK}, f"{name}/{service}")

    def test_the_published_dual_mode_head_and_its_controller_stay_off_the_lan(self) -> None:
        with (
            patch.object(environment, "CLUSTER_DETECTORS", _detectors(BRIDGE_GATEWAY)),
            patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery()),
        ):
            _profile, rendered = self._render(
                overlays.FIXTURES["dgx-spark"],
                {
                    "CLUSTER_MODE": "dual",
                    "CLUSTER_HEAD_IP": HEAD_IP,
                    "CLUSTER_WORKER_IP": WORKER_IP,
                    # Both production values that used to open the engine.
                    "PUBLISH_MODEL_PORTS": "true",
                    "TECHSARA_BIND_ADDRESS": WILDCARD,
                },
            )
        services = rendered["services"]
        argv = list(services["vllm"]["command"])
        self.assertEqual(argv[argv.index("--host") + 1], BRIDGE_GATEWAY)
        controller = services["engine-controller"]["environment"]
        self.assertEqual(controller["HEAD_API_URL"], f"http://{BRIDGE_GATEWAY}:8000")
        self.assertEqual(controller["CONTROLLER_BIND"], f"{LOOPBACK},{BRIDGE_GATEWAY}")
        # The callers that reach the head and the controller through the
        # bridge gateway are still wired to it.
        for service in ("orchestrator", "sync-worker"):
            extra_hosts = {str(item).replace("=", ":") for item in services[service].get("extra_hosts") or []}
            self.assertIn("vllm:host-gateway", extra_hosts)

    def test_the_single_node_engine_controller_listens_on_loopback_only(self) -> None:
        _profile, rendered = self._render(overlays.FIXTURES["dgx-spark"], {"TECHSARA_BIND_ADDRESS": WILDCARD})
        self.assertEqual(rendered["services"]["engine-controller"]["environment"]["CONTROLLER_BIND"], LOOPBACK)


if __name__ == "__main__":
    unittest.main()
