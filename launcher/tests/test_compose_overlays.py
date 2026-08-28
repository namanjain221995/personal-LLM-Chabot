"""End-to-end validation of every platform overlay against real Docker Compose.

Unlike the rest of the launcher suite, this module shells out to
``docker compose config`` so the repository's acceptance criterion — every
overlay resolves — is proved by the tool that will actually consume the files
rather than by a hand-written parser.  It creates nothing, starts nothing, and
pulls nothing: ``config`` only resolves and renders.

The whole module skips when Docker Compose v2.24+ is unavailable, so a machine
without Docker still runs the other 250 launcher tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

try:
    from .support import GIB, REPO_ROOT, fake_discovery
    from .test_model_shape import NESTED_CONFIG
    from .test_profiles import cpu, mac, nvidia
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB, REPO_ROOT, fake_discovery
    from test_model_shape import NESTED_CONFIG
    from test_profiles import cpu, mac, nvidia

from techsara_cli import environment
from techsara_cli.cli import _compose_files
from techsara_cli.cluster import ClusterDetectors
from techsara_cli.errors import TechSaraError
from techsara_cli.environment import RuntimeLayout, build_generated_environment
from techsara_cli.hardware import HardwareInfo
from techsara_cli.model_manager import ModelInstall
from techsara_cli.profiles import SelectedProfile, select_profile
from techsara_cli.utils import render_env, slug_model


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in result.stdout)
    parts = (digits.split() or ["0"])[0].split(".")
    try:
        return tuple(int(item) for item in parts[:2]) >= (2, 24)
    except ValueError:
        return False


COMPOSE_AVAILABLE = _compose_available()

#: Every host fixture the launcher must be able to render a Compose plan for.
FIXTURES: dict[str, HardwareInfo] = {
    "mac-16-24gb": mac(24),
    "mac-32-47gb": mac(32),
    "mac-48-79gb": mac(64),
    "mac-80-127gb": mac(96),
    "mac-128gb-plus": mac(128),
    "dgx-spark": nvidia(122, dgx=True, system_memory_gib=122),
    "nvidia-large": nvidia(80),
    "nvidia-medium": nvidia(48),
    "nvidia-small": nvidia(24),
    "nvidia-minimal": nvidia(12),
    "windows-nvidia": nvidia(24, operating_system="windows", wsl2=True),
    "local-minimal": cpu(),
    "app-only": cpu(memory_gib=4),
}


def _planned_installs(profile: SelectedProfile, cache_root: Path) -> list[ModelInstall]:
    """Model records as they look before the first download completes."""
    installs: list[ModelInstall] = []
    for model in profile.required_models():
        path = cache_root / "repos" / f"{slug_model(model.id)}--{model.revision[:12]}"
        installs.append(
            ModelInstall("planned", str(path), model.id, model.revision, "managed")
        )
    return installs


class ComposeOverlayValidationTests(unittest.TestCase):
    """`docker compose config` must resolve for every supported host."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        if not COMPOSE_AVAILABLE:
            raise unittest.SkipTest("Docker Compose v2.24+ is required for overlay validation")

    def setUp(self) -> None:
        # CLUSTER_MODE defaults to auto; the dgx-spark fixture must not probe
        # this host's RoCE links or ssh anywhere while rendering overlays.
        discovery = patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery())
        discovery.start()
        self.addCleanup(discovery.stop)

    def _render(
        self,
        hardware: HardwareInfo,
        user_environment: dict | None = None,
        *,
        model_config: dict | None = None,
    ) -> tuple[SelectedProfile, dict]:
        """Resolve one fixture's Compose plan through Docker Compose itself.

        ``model_config`` materialises a ``config.json`` for the main model, the
        way a completed download would, so the launcher can read the geometry a
        user-owned context window is measured against.
        """
        with tempfile.TemporaryDirectory(prefix="techsara-overlay-") as temporary:
            workspace = Path(temporary)
            cache_root = workspace / "models"
            cache_root.mkdir(parents=True)
            hardware = replace(hardware, selected_cache_path=str(cache_root))
            profile = select_profile(hardware, REPO_ROOT)
            if model_config is not None and profile.main_model:
                main = profile.main_model
                directory = cache_root / "repos" / f"{slug_model(main.id)}--{main.revision[:12]}"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "config.json").write_text(json.dumps(model_config), encoding="utf-8")
            layout = RuntimeLayout.for_project(REPO_ROOT)
            external = (
                {"OPENAI_BASE_URL": "http://host.docker.internal:9099/v1", "MAIN_MODEL": "fixture-external"}
                if profile.family == "external"
                else None
            )
            generated = build_generated_environment(
                layout,
                profile,
                _planned_installs(profile, cache_root),
                cache_root=cache_root,
                allow_planned=True,
                external_environment=external,
                user_environment=user_environment or {},
            )
            publish_model_ports = str(
                generated.get("TECHSARA_PUBLISH_MODEL_PORTS", "false")
            ).lower() == "true"
            generated_env = workspace / "generated.env"
            secrets_env = workspace / "secrets.env"
            # The base file resolves its own env_file chain from these two
            # values, exactly as ComposeManager._environment does at runtime.
            generated["TECHSARA_GENERATED_ENV"] = str(generated_env)
            generated["TECHSARA_SECRET_ENV"] = str(secrets_env)
            generated_env.write_text(render_env(generated), encoding="utf-8")
            secrets_env.write_text(
                render_env(
                    {
                        "POSTGRES_PASSWORD": "fixture-password",
                        "SESSION_SECRET": "fixture-session-secret",
                        "SEARXNG_SECRET": "fixture-searxng-secret",
                        "PGADMIN_DEFAULT_PASSWORD": "fixture-pgadmin",
                    }
                ),
                encoding="utf-8",
            )

            files = _compose_files(
                REPO_ROOT, hardware, profile,
                publish_model_ports=publish_model_ports,
                cluster_mode=str(generated.get("TECHSARA_CLUSTER_MODE") or "single"),
            )
            command = ["docker", "compose", "--project-name", "sf-local-ai-overlay-test"]
            command += ["--env-file", str(secrets_env), "--env-file", str(generated_env)]
            for path in files:
                command += ["-f", str(path)]
            # Every optional profile at once: a service that only resolves when
            # its profile is inactive would still be a latent failure.
            for name in ("embeddings", "ocr", "search", "admin"):
                command += ["--profile", name]
            command += ["config", "--format", "json"]
            environment = dict(os.environ)
            environment.update(
                TECHSARA_GENERATED_ENV=str(generated_env),
                TECHSARA_SECRET_ENV=str(secrets_env),
            )
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=180, check=False,
                cwd=REPO_ROOT, env=environment,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"docker compose config failed for {profile.hardware_profile_id}:\n{result.stderr[-2000:]}",
            )
            return profile, json.loads(result.stdout)

    def test_every_supported_host_fixture_resolves_and_keeps_its_platform_invariants(self) -> None:
        model_services = {"vllm", "vllm-router", "vllm-embed", "vllm-ocr", "vllm-vision", "llama-cpp"}
        cuda_services = model_services - {"llama-cpp"}
        for name, hardware in FIXTURES.items():
            with self.subTest(fixture=name):
                profile, rendered = self._render(hardware)
                services = rendered.get("services", {})
                self.assertIn("orchestrator", services)
                self.assertIn("frontend", services)
                self.assertIn("postgres", services)

                # Persistent state survives every overlay.
                volumes = rendered.get("volumes", {})
                for volume in ("data", "reports", "pgdata"):
                    self.assertIn(volume, volumes, f"{name} dropped the {volume} volume")

                # By default no model API is published to a host port anywhere.
                for service in model_services & set(services):
                    self.assertFalse(
                        services[service].get("ports"),
                        f"{name} publishes {service} to a host port",
                    )

                # Every published port is loopback-bound by default.
                for service, definition in services.items():
                    for port in definition.get("ports") or []:
                        self.assertIn(
                            str(port.get("host_ip") or ""),
                            {"127.0.0.1", "::1"},
                            f"{name}/{service} publishes {port} beyond loopback",
                        )

                orchestrator_image = str(services["orchestrator"].get("image", ""))
                if profile.family in {"mac", "cpu", "app", "external"}:
                    self.assertTrue(
                        orchestrator_image.endswith(":cpu"),
                        f"{name} must use the CUDA-free orchestrator image",
                    )
                    self.assertNotIn(
                        "nvcr.io", str(services["orchestrator"].get("build", {}).get("dockerfile", ""))
                    )
                    for service, definition in services.items():
                        reservations = (
                            definition.get("deploy", {}).get("resources", {}).get("reservations", {})
                        )
                        self.assertFalse(
                            reservations.get("devices"),
                            f"{name}/{service} reserves an NVIDIA device on a non-NVIDIA profile",
                        )
                    self.assertFalse(
                        cuda_services & set(services),
                        f"{name} defines CUDA model services",
                    )
                    if profile.family == "cpu":
                        self.assertIn("llama-cpp", services)
                else:
                    self.assertEqual(orchestrator_image, "sf-local-ai-orchestrator:cuda")
                    devices = (
                        services["vllm"]["deploy"]["resources"]["reservations"]["devices"]
                    )
                    self.assertEqual(devices[0]["driver"], "nvidia")
                    self.assertIn("gpu", devices[0]["capabilities"])

    def test_the_dgx_overlay_keeps_its_measured_model_family_and_flags(self) -> None:
        profile, rendered = self._render(FIXTURES["dgx-spark"])
        self.assertEqual(profile.hardware_profile_id, "dgx-spark")
        self.assertEqual(profile.main_model.id, "nvidia/Qwen3.6-35B-A3B-NVFP4")
        command = " ".join(rendered["services"]["vllm"]["command"])
        for flag in (
            "--kv-cache-dtype fp8",
            "--reasoning-parser qwen3",
            "--quantization modelopt",
            "--attention-backend flashinfer",
            "--enable-chunked-prefill",
            "--enable-prefix-caching",
            "--max-num-batched-tokens 8192",
            "--gpu-memory-utilization 0.35",
        ):
            self.assertIn(flag, command)
        self.assertIn("--served-model-name Qwen/Qwen3.6-35B-A3B-NVFP4", command)
        self.assertIn("vllm-router", rendered["services"])
        self.assertIn("vllm-ocr", rendered["services"])

    def test_an_extended_window_reaches_the_dgx_command_line_as_one_argv_element(self) -> None:
        """MAIN_MODEL_MAX_LEN past the native window: --hf-overrides, intact.

        The override is a single-quoted JSON blob travelling through a folded
        YAML scalar, Compose interpolation, and Compose's shell-style split.
        Anything that mangles it produces a vLLM argument parse error 30
        minutes into a start-up, so it is proved with Compose itself.
        """
        _profile, rendered = self._render(
            FIXTURES["dgx-spark"], {"MAIN_MODEL_MAX_LEN": "800000"}, model_config=NESTED_CONFIG
        )
        argv = list(rendered["services"]["vllm"]["command"])
        window = argv.index("--max-model-len")
        self.assertEqual(argv[window + 1], "800000")
        # Immediately after the window it belongs to, and still one element.
        self.assertEqual(argv[window + 2], "--hf-overrides")
        self.assertEqual(argv.count("--hf-overrides"), 1)
        parameters = json.loads(argv[window + 3])["text_config"]["rope_parameters"]
        self.assertEqual(parameters["rope_type"], "yarn")
        self.assertEqual(parameters["factor"], 3.06)
        self.assertEqual(parameters["original_max_position_embeddings"], 262144)
        self.assertEqual(parameters["mrope_section"], [11, 11, 10])
        self.assertTrue(parameters["mrope_interleaved"])
        # The speculative JSON is still whole, so nothing was re-split.
        self.assertEqual(
            argv[argv.index("--speculative-config") + 1],
            '{"method":"mtp","num_speculative_tokens":1}',
        )

        # A window inside the native one folds the variable away completely.
        _profile, native = self._render(FIXTURES["dgx-spark"], model_config=NESTED_CONFIG)
        native_argv = list(native["services"]["vllm"]["command"])
        self.assertNotIn("--hf-overrides", native_argv)
        self.assertEqual(native_argv[native_argv.index("--max-model-len") + 2], "--gpu-memory-utilization")

    def test_an_extended_window_reaches_both_cluster_nodes_through_the_shared_engine_args(self) -> None:
        detectors = ClusterDetectors(
            ifname_for_ip=lambda ip: {"192.168.100.1": "enP2p1s0f1np1"}.get(ip),
            hcas_for_ifnames=lambda names: ["rocep1s0f1" for name in names if name == "enP2p1s0f1np1"],
            docker_bridge_gateway=lambda: "172.17.0.1",
        )
        with (
            patch.object(environment, "CLUSTER_DETECTORS", detectors),
            patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery()),
        ):
            _profile, rendered = self._render(
                FIXTURES["dgx-spark"],
                {
                    "CLUSTER_MODE": "dual",
                    "CLUSTER_HEAD_IP": "192.168.100.1",
                    "CLUSTER_WORKER_IP": "192.168.100.2",
                    # 838,860 tokens is 3.2x native and the widest window the
                    # default 16 GiB per-node KV budget can hold at TP=2.
                    "MAIN_MODEL_MAX_LEN": "838860",
                },
                model_config=NESTED_CONFIG,
            )
        argv = list(rendered["services"]["vllm"]["command"])
        window = argv.index("--max-model-len")
        self.assertEqual(argv[window + 1], "838860")
        self.assertEqual(argv[window + 2], "--hf-overrides")
        self.assertEqual(
            json.loads(argv[window + 3])["text_config"]["rope_parameters"]["factor"], 3.2
        )

    def test_every_host_bind_source_resolves_under_the_project_or_the_configured_cache(self) -> None:
        """No overlay may hard-code one developer's directory.

        Compose legitimately renders the *checkout* path for build contexts and
        relative binds, so the invariant is not "no absolute path" but "every
        absolute path was derived from this project root or from the cache
        directory the launcher selected", never from a value baked into a file.
        """
        for name, hardware in FIXTURES.items():
            with self.subTest(fixture=name):
                _profile, rendered = self._render(hardware)
                root = str(REPO_ROOT)
                for service, definition in rendered.get("services", {}).items():
                    for volume in definition.get("volumes") or []:
                        if volume.get("type") != "bind":
                            continue
                        source = str(volume.get("source", ""))
                        self.assertTrue(
                            source.startswith(root) or "techsara-overlay-" in source,
                            f"{name}/{service} binds an unmanaged host path: {source}",
                        )
                    context = str(definition.get("build", {}).get("context", ""))
                    if context:
                        self.assertTrue(
                            context.startswith(root),
                            f"{name}/{service} builds from outside the project: {context}",
                        )

    def test_no_compose_source_file_hard_codes_a_developer_home_directory(self) -> None:
        for path in [REPO_ROOT / "compose.yaml", *sorted((REPO_ROOT / "compose").glob("*.yaml"))]:
            with self.subTest(compose_file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/home/techsphere", text)
                self.assertNotIn("/Users/", text)

    def test_every_image_is_pinned_by_immutable_digest(self) -> None:
        for name, hardware in FIXTURES.items():
            with self.subTest(fixture=name):
                _profile, rendered = self._render(hardware)
                for service, definition in rendered.get("services", {}).items():
                    image = str(definition.get("image", ""))
                    if image.startswith("sf-local-ai-"):
                        continue  # locally built from a digest-pinned base
                    self.assertIn(
                        "@sha256:", image, f"{name}/{service} uses a floating tag: {image}"
                    )

    def test_model_ports_are_published_only_on_explicit_opt_in_and_at_the_configured_address(self) -> None:
        """Publishing is opt-in, and the opt-in must be obeyed exactly."""
        opt_in = {
            "TECHSARA_BIND_ADDRESS": "0.0.0.0",
            "PUBLISH_MODEL_PORTS": "true",
            "VLLM_PORT": "8000",
            "VLLM_ROUTER_PORT": "8002",
            "VLLM_EMBED_PORT": "8003",
            "VLLM_OCR_PORT": "8004",
        }
        expected = {
            "dgx-spark": {
                "vllm": ("0.0.0.0", 8000, 30000),
                "vllm-router": ("0.0.0.0", 8002, 30002),
                "vllm-embed": ("0.0.0.0", 8003, 30003),
                "vllm-ocr": ("0.0.0.0", 8004, 30004),
            },
            "nvidia-large": {
                "vllm": ("0.0.0.0", 8000, 30000),
                "vllm-embed": ("0.0.0.0", 8003, 30003),
            },
            "local-minimal": {"llama-cpp": ("0.0.0.0", 8000, 30000)},
        }
        for name, services in expected.items():
            with self.subTest(fixture=name):
                _profile, rendered = self._render(FIXTURES[name], opt_in)
                for service, (host, published, target) in services.items():
                    ports = rendered["services"][service].get("ports") or []
                    self.assertEqual(len(ports), 1, f"{name}/{service} publish count")
                    port = ports[0]
                    self.assertEqual(port.get("host_ip"), host)
                    self.assertEqual(int(port.get("published")), published)
                    self.assertEqual(int(port.get("target")), target)

    def test_a_published_service_is_attached_to_a_routable_network(self) -> None:
        """A port binding on an internal-only network is silently unreachable.

        Regression: the opt-in overlay published the model APIs while leaving
        the services on `inference` (`internal: true`). Docker recorded the
        binding, `docker inspect` showed it, and `curl` got nothing — the
        symptom is invisible unless you actually connect.
        """
        opt_in = {"TECHSARA_BIND_ADDRESS": "0.0.0.0", "PUBLISH_MODEL_PORTS": "true"}
        for name in ("dgx-spark", "nvidia-large", "local-minimal"):
            with self.subTest(fixture=name):
                _profile, rendered = self._render(FIXTURES[name], opt_in)
                internal = {
                    network
                    for network, definition in (rendered.get("networks") or {}).items()
                    if (definition or {}).get("internal")
                }
                self.assertIn("inference", internal, "the internal network must stay internal")
                for service, definition in rendered["services"].items():
                    if not definition.get("ports"):
                        continue
                    attached = set((definition.get("networks") or {}) or {"default"})
                    self.assertTrue(
                        attached - internal,
                        f"{name}/{service} publishes a port but is only on {sorted(attached)}, "
                        "which is internal-only and therefore unreachable",
                    )

    def test_the_publish_opt_in_never_moves_postgres_or_pgadmin_off_loopback(self) -> None:
        opt_in = {"TECHSARA_BIND_ADDRESS": "0.0.0.0", "PUBLISH_MODEL_PORTS": "true"}
        _profile, rendered = self._render(FIXTURES["dgx-spark"], opt_in)
        for service in ("postgres", "pgadmin"):
            for port in rendered["services"][service].get("ports") or []:
                self.assertEqual(
                    port.get("host_ip"),
                    "127.0.0.1",
                    f"{service} must stay on loopback regardless of the opt-in",
                )

    def test_the_application_bind_address_follows_the_opt_in(self) -> None:
        _profile, rendered = self._render(
            FIXTURES["dgx-spark"], {"TECHSARA_BIND_ADDRESS": "0.0.0.0"}
        )
        for service in ("frontend", "orchestrator"):
            hosts = {p.get("host_ip") for p in rendered["services"][service].get("ports") or []}
            self.assertEqual(hosts, {"0.0.0.0"})
        # Without the model opt-in, the model APIs stay internal even so.
        self.assertFalse(rendered["services"]["vllm"].get("ports"))

    def test_a_non_literal_bind_address_is_rejected_rather_than_widening_exposure(self) -> None:
        for value in ("example.com", "0.0.0.0 ; rm -rf /", "all", "*"):
            with self.subTest(value=value):
                with self.assertRaises(TechSaraError):
                    self._render(FIXTURES["dgx-spark"], {"TECHSARA_BIND_ADDRESS": value})

    def test_every_launcher_built_image_starts_from_a_digest_pinned_base(self) -> None:
        """`docker compose config` cannot see inside a Dockerfile, so check them."""
        dockerfiles = (
            REPO_ROOT / "orchestrator" / "Dockerfile.cpu",
            REPO_ROOT / "orchestrator" / "Dockerfile.cuda",
            REPO_ROOT / "sync-worker" / "Dockerfile",
            REPO_ROOT / "frontend" / "Dockerfile",
        )
        for path in dockerfiles:
            with self.subTest(dockerfile=str(path.relative_to(REPO_ROOT))):
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if not stripped.upper().startswith("FROM "):
                        continue
                    reference = stripped.split()[1]
                    self.assertIn(
                        "@sha256:", reference, f"{path.name} builds from a floating tag: {reference}"
                    )

    def test_generated_environment_reaches_the_application_containers(self) -> None:
        _profile, rendered = self._render(FIXTURES["dgx-spark"])
        for service in ("orchestrator", "sync-worker", "frontend"):
            environment = rendered["services"][service].get("environment", {})
            self.assertTrue(environment, f"{service} received no environment")
        orchestrator = rendered["services"]["orchestrator"]["environment"]
        self.assertEqual(orchestrator["MAIN_MODEL"], "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(orchestrator["OPENAI_BASE_URL"], "http://vllm:30000/v1")
        self.assertTrue(orchestrator["RERANKER_MODEL"].startswith("/models/"))
        self.assertEqual(
            rendered["services"]["sync-worker"]["environment"]["EMBED_VIA"],
            "http://vllm-embed:30003/v1",
        )

    def test_every_gpu_overlay_that_takes_a_window_also_takes_the_rope_override(self) -> None:
        """A long window without its YaRN override is a start-up failure.

        compose.nvidia.yaml once interpolated --max-model-len but not
        MAIN_MODEL_ROPE_OVERRIDE, so an extended window on an nvidia-* profile
        passed vLLM a length it had no rope for: the engine refuses with
        "greater than the derived max_model_len" while the launcher had already
        printed "YaRN factor N enabled". The narration is profile-independent,
        so the delivery has to be too.
        """
        for name in ("compose.dgx-spark.yaml", "compose.nvidia.yaml"):
            overlay = (REPO_ROOT / "compose" / name).read_text(encoding="utf-8")
            window = overlay.count("--max-model-len ${MODEL_MAX_CONTEXT")
            self.assertTrue(window, f"{name} no longer sets the main window")
            self.assertIn(
                "${MAIN_MODEL_ROPE_OVERRIDE:-}",
                overlay,
                f"{name} takes an extended --max-model-len but drops the rope override",
            )

    def test_the_dgx_cluster_head_overlay_turns_vllm_into_node_rank_zero_on_the_host(self) -> None:
        """CLUSTER_MODE=dual: host networking, identical engine args, one JSON argv."""
        detectors = ClusterDetectors(
            ifname_for_ip=lambda ip: {"192.168.100.1": "enP2p1s0f1np1"}.get(ip),
            hcas_for_ifnames=lambda names: ["rocep1s0f1" for name in names if name == "enP2p1s0f1np1"],
            docker_bridge_gateway=lambda: "172.17.0.1",
        )
        dual = {
            "CLUSTER_MODE": "dual",
            "CLUSTER_HEAD_IP": "192.168.100.1",
            "CLUSTER_WORKER_IP": "192.168.100.2",
        }
        with (
            patch.object(environment, "CLUSTER_DETECTORS", detectors),
            patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery()),
        ):
            profile, rendered = self._render(FIXTURES["dgx-spark"], dual)
            _profile, published = self._render(
                FIXTURES["dgx-spark"],
                {**dual, "PUBLISH_MODEL_PORTS": "true", "TECHSARA_BIND_ADDRESS": "0.0.0.0", "VLLM_PORT": "18000"},
            )
        self.assertEqual(profile.hardware_profile_id, "dgx-spark")
        vllm = rendered["services"]["vllm"]
        self.assertEqual(vllm.get("network_mode"), "host")
        self.assertFalse(vllm.get("ports"))
        self.assertFalse(vllm.get("networks"))
        self.assertFalse(vllm.get("expose"))
        argv = list(vllm["command"])
        command = " ".join(argv)
        for flag in (
            "--nnodes 2",
            "--node-rank 0",
            "--tensor-parallel-size 2",
            "--pipeline-parallel-size 1",
            "--master-addr 192.168.100.1",
            "--master-port 29501",
            "--distributed-executor-backend mp",
            "--host 172.17.0.1",
            "--port 8000",
            "--gpu-memory-utilization 0.30",
            "--quantization modelopt",
            "--attention-backend flashinfer",
            f"--max-model-len {profile.context_length}",
            "--served-model-name Qwen/Qwen3.6-35B-A3B-NVFP4",
        ):
            self.assertIn(flag, command)
        self.assertNotIn("--headless", argv)
        # The speculative JSON must survive Compose's shell-style split intact.
        self.assertEqual(
            argv[argv.index("--speculative-config") + 1],
            '{"method":"mtp","num_speculative_tokens":1}',
        )
        self.assertEqual(vllm["environment"]["VLLM_HOST_IP"], "192.168.100.1")
        self.assertEqual(vllm["environment"]["NCCL_SOCKET_IFNAME"], "enP2p1s0f1np1")
        self.assertEqual(vllm["environment"]["NCCL_IB_HCA"], "rocep1s0f1")
        for service in ("orchestrator", "sync-worker"):
            # Compose renders `host:target` as `host=target` in newer versions.
            extra_hosts = {
                str(item).replace("=", ":")
                for item in rendered["services"][service].get("extra_hosts") or []
            }
            self.assertIn("vllm:host-gateway", extra_hosts, f"{service} cannot resolve the host-mode head")
        self.assertEqual(
            rendered["services"]["orchestrator"]["environment"]["OPENAI_BASE_URL"],
            "http://vllm:8000/v1",
        )
        # Every other model service keeps its single-node internal layout.
        for service in ("vllm-router", "vllm-embed", "vllm-ocr"):
            self.assertNotEqual(rendered["services"][service].get("network_mode"), "host")
            self.assertFalse(rendered["services"][service].get("ports"))

        # With the publish opt-in the head listens on every interface at the
        # configured port; the cluster overlay still strips the port mapping
        # the published overlay adds, because a host-mode container has none.
        published_vllm = published["services"]["vllm"]
        self.assertEqual(published_vllm.get("network_mode"), "host")
        self.assertFalse(published_vllm.get("ports"))
        self.assertIn("--host 0.0.0.0 --port 18000", " ".join(published_vllm["command"]))
        self.assertEqual(
            published["services"]["orchestrator"]["environment"]["OPENAI_BASE_URL"],
            "http://vllm:18000/v1",
        )
        self.assertTrue(published["services"]["vllm-router"].get("ports"))


if __name__ == "__main__":
    unittest.main()
