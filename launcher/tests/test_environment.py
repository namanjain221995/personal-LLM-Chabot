from __future__ import annotations

import base64
import json
import os
import shlex
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

try:
    from .support import REPO_ROOT, fake_discovery
    from .test_model_shape import FLAT_CONFIG, NESTED_CONFIG
    from .test_profiles import cpu, mac, nvidia
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT, fake_discovery
    from test_model_shape import FLAT_CONFIG, NESTED_CONFIG
    from test_profiles import cpu, mac, nvidia

from techsara_cli import environment
from techsara_cli.cluster import ClusterDetectors
from techsara_cli.environment import (
    MAIN_CONTEXT_RANGE,
    RuntimeLayout,
    _container_path,
    build_generated_environment,
    effective_user_environment,
    has_salesforce_credentials,
    main_context_notices,
    prepare_local_secrets,
    profile_context_length,
)
from techsara_cli.errors import TechSaraError
from techsara_cli.model_manager import ModelInstall
from techsara_cli.profiles import SelectedProfile, select_profile
from techsara_cli.utils import parse_env_file


class EnvironmentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.shared = self.root / "shared home"
        with patch.dict(os.environ, {"TECHSARA_HOME": str(self.shared)}, clear=False):
            self.layout = RuntimeLayout.for_project(self.project)
        # CLUSTER_MODE defaults to auto: without this, a dgx-spark fixture would
        # probe the real RoCE links and ssh to a real peer.
        self.discovery_calls: list[str] = []
        discovery = patch.object(environment, "CLUSTER_DISCOVERY", fake_discovery(calls=self.discovery_calls))
        discovery.start()
        self.addCleanup(discovery.stop)

    @staticmethod
    def installs(profile: SelectedProfile, cache: Path) -> list[ModelInstall]:
        result = []
        for model in profile.required_models():
            path = cache / "repos" / f"{model.key}--{model.revision[:12]}"
            result.append(
                ModelInstall(
                    "complete",
                    str(path),
                    model.id,
                    model.revision,
                    "managed",
                )
            )
        return result


class RuntimeLayoutTests(EnvironmentCase):
    def test_layout_keeps_project_state_local_and_shared_assets_user_scoped(self) -> None:
        self.assertEqual(self.layout.project_root, self.project.resolve())
        self.assertEqual(self.layout.runtime_dir, self.project.resolve() / ".runtime")
        self.assertEqual(self.layout.hardware_file, self.layout.runtime_dir / "hardware.json")
        self.assertEqual(self.layout.profile_file, self.layout.runtime_dir / "selected-profile.json")
        self.assertEqual(self.layout.generated_env, self.layout.runtime_dir / "generated.env")
        self.assertEqual(self.layout.secrets_env, self.layout.runtime_dir / "secrets.env")
        self.assertEqual(self.layout.shared_root, self.shared.resolve())

    def test_create_makes_only_expected_project_runtime_directories(self) -> None:
        neighbor = self.project / "user-data"
        neighbor.write_text("preserve", encoding="utf-8")
        self.layout.create()
        for path in (
            self.layout.runtime_dir,
            self.layout.locks_dir,
            self.layout.logs_dir,
            self.layout.pids_dir,
        ):
            self.assertTrue(path.is_dir())
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(self.layout.generated_env.exists())
        self.assertFalse(self.layout.secrets_env.exists())


class SecretPreparationTests(EnvironmentCase):
    def test_first_run_generates_only_local_secrets_with_private_mode_and_reuses_them(self) -> None:
        profile = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        counter = iter(f"fixture-secret-{index:02d}-" + "x" * 48 for index in range(20))
        with patch("techsara_cli.environment.secure_token", side_effect=lambda _size: next(counter)) as token:
            first, warnings = prepare_local_secrets(self.layout, profile, {})
        self.assertEqual(warnings, [])
        expected = {
            "POSTGRES_PASSWORD",
            "PGADMIN_DEFAULT_PASSWORD",
            "SEARXNG_SECRET",
            "SESSION_SECRET",
            "TECHSARA_MODEL_API_KEY",
            "OPENAI_API_KEY",
            "EMBED_API_KEY",
            "RERANK_API_KEY",
        }
        self.assertEqual(set(first), expected)
        self.assertEqual(first["OPENAI_API_KEY"], first["TECHSARA_MODEL_API_KEY"])
        self.assertEqual(first["EMBED_API_KEY"], first["TECHSARA_MODEL_API_KEY"])
        self.assertEqual(first["RERANK_API_KEY"], first["TECHSARA_MODEL_API_KEY"])
        self.assertEqual(stat.S_IMODE(self.layout.secrets_env.stat().st_mode), 0o600)
        serialized = self.layout.secrets_env.read_text(encoding="utf-8")
        self.assertNotIn("HF_TOKEN", serialized)
        self.assertNotIn("SF_CLIENT_SECRET", serialized)

        with patch(
            "techsara_cli.environment.secure_token",
            side_effect=AssertionError("idempotent secret preparation regenerated a token"),
        ):
            second, second_warnings = prepare_local_secrets(self.layout, profile, {})
        self.assertEqual(second, first)
        self.assertEqual(second_warnings, [])
        self.assertGreaterEqual(token.call_count, 5)

    def test_user_configured_values_win_and_are_not_duplicated_into_generated_secret_file(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        self.layout.runtime_dir.mkdir()
        self.layout.secrets_env.write_text(
            "POSTGRES_PASSWORD=old-generated\nOPENAI_API_KEY=external-user-key\n",
            encoding="utf-8",
        )
        user = {
            "POSTGRES_PASSWORD": "user-postgres",
            "OPENAI_API_KEY": "user-openai",
            "SESSION_SECRET": "change-me-session",
        }
        values, _warnings = prepare_local_secrets(self.layout, profile, user)
        self.assertNotIn("POSTGRES_PASSWORD", values)
        self.assertEqual(values["OPENAI_API_KEY"], "external-user-key")
        self.assertNotEqual(values["SESSION_SECRET"], "change-me-session")
        self.assertNotIn("user-postgres", self.layout.secrets_env.read_text(encoding="utf-8"))
        self.assertNotIn("user-openai", self.layout.secrets_env.read_text(encoding="utf-8"))

    def test_switching_away_from_native_removes_only_ephemeral_api_aliases(self) -> None:
        native = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        prepare_local_secrets(self.layout, native, {})
        native_values = parse_env_file(self.layout.secrets_env)
        self.assertEqual(native_values["OPENAI_API_KEY"], native_values["TECHSARA_MODEL_API_KEY"])

        cpu_profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        non_native, _warnings = prepare_local_secrets(self.layout, cpu_profile, {})
        for key in ("OPENAI_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY"):
            self.assertNotIn(key, non_native)
        self.assertIn("TECHSARA_MODEL_API_KEY", non_native)

    def test_salesforce_host_key_is_bounded_encoded_and_never_copied_as_a_path(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        key = self.root / "salesforce private key.pem"
        key.write_bytes(b"fixture-private-key")
        values, warnings = prepare_local_secrets(
            self.layout,
            profile,
            {"SF_PRIVATE_KEY_HOST_FILE": str(key)},
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            values["SF_PRIVATE_KEY_B64"],
            base64.b64encode(b"fixture-private-key").decode("ascii"),
        )
        serialized = self.layout.secrets_env.read_text(encoding="utf-8")
        self.assertNotIn(str(key), serialized)

    def test_missing_or_oversized_salesforce_host_key_degrades_without_crashing(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        missing = self.root / "missing.pem"
        values, warnings = prepare_local_secrets(
            self.layout, profile, {"SF_PRIVATE_KEY_HOST_FILE": str(missing)}
        )
        self.assertNotIn("SF_PRIVATE_KEY_B64", values)
        self.assertEqual(len(warnings), 1)

        large = self.root / "large.pem"
        with large.open("wb") as handle:
            handle.truncate(128 * 1024 + 1)
        values, warnings = prepare_local_secrets(
            self.layout, profile, {"SF_PRIVATE_KEY_HOST_FILE": str(large)}
        )
        self.assertNotIn("SF_PRIVATE_KEY_B64", values)
        self.assertEqual(len(warnings), 1)

    def test_user_base64_key_prevents_host_file_staging(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        host = self.root / "must-not-be-read.pem"
        host.write_text("fixture", encoding="utf-8")
        values, warnings = prepare_local_secrets(
            self.layout,
            profile,
            {
                "SF_PRIVATE_KEY_HOST_FILE": str(host),
                "SF_PRIVATE_KEY_B64": "user-owned-base64",
            },
        )
        self.assertNotIn("SF_PRIVATE_KEY_B64", values)
        self.assertEqual(warnings, [])


class EffectiveEnvironmentTests(EnvironmentCase):
    def test_effective_environment_merges_project_then_private_runtime_values(self) -> None:
        (self.project / ".env").write_text(
            "USER_ONLY=value\nSHARED=user-value\n",
            encoding="utf-8",
        )
        self.layout.runtime_dir.mkdir()
        self.layout.secrets_env.write_text(
            "GENERATED_ONLY=value\nSHARED=runtime-value\n",
            encoding="utf-8",
        )
        values = effective_user_environment(self.layout)
        self.assertEqual(values["USER_ONLY"], "value")
        self.assertEqual(values["GENERATED_ONLY"], "value")
        self.assertEqual(values["SHARED"], "runtime-value")

    def test_salesforce_credentials_require_identity_and_one_assertion_method(self) -> None:
        base = {"SF_CLIENT_ID": "client", "SF_USERNAME": "user"}
        for assertion_key in ("SF_CLIENT_SECRET", "SF_PRIVATE_KEY_B64", "SF_PRIVATE_KEY_HOST_FILE"):
            with self.subTest(assertion=assertion_key):
                self.assertTrue(has_salesforce_credentials({**base, assertion_key: "configured"}))
        self.assertFalse(has_salesforce_credentials(base))
        self.assertFalse(has_salesforce_credentials({"SF_CLIENT_SECRET": "configured"}))
        self.assertFalse(
            has_salesforce_credentials(
                {**base, "SF_CLIENT_SECRET": "change-me-before-use"}
            )
        )


class GeneratedEnvironmentTests(EnvironmentCase):
    def test_model_container_path_requires_cache_containment(self) -> None:
        profile = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        model = profile.main_model
        cache = self.root / "cache"
        inside = ModelInstall(
            "complete",
            str(cache / "repos" / "managed"),
            model.id,
            model.revision,
            "managed",
        )
        self.assertEqual(_container_path(cache, inside, model), "/models/repos/managed")

        outside = ModelInstall(
            "complete",
            str(self.root / "outside"),
            model.id,
            model.revision,
            "managed",
        )
        with self.assertRaisesRegex(TechSaraError, "outside TECHSARA_MODEL_CACHE"):
            _container_path(cache, outside, model)

    def test_cpu_single_file_model_path_includes_the_exact_gguf_filename(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        model = profile.main_model
        self.assertEqual(model.backend, "llama-cpp-cpu")
        self.assertEqual(len(model.required_files), 1)
        cache = self.root / "cache"
        install = ModelInstall(
            "complete",
            str(cache / "repos" / "cpu"),
            model.id,
            model.revision,
            "managed",
        )
        self.assertEqual(
            _container_path(cache, install, model),
            f"/models/repos/cpu/{model.required_files[0]}",
        )

    def test_only_ready_model_installs_are_exposed_to_containers(self) -> None:
        profile = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        cache = self.root / "cache"
        ready = self.installs(profile, cache)
        main = profile.main_model
        other_installs = [
            item
            for item in ready
            if (item.model_id, item.revision) != (main.id, main.revision)
        ]
        for status in ("missing", "partial", "invalid", "planned"):
            with self.subTest(status=status):
                unusable = ModelInstall(
                    status,
                    str(cache / "repos" / f"unusable-{status}"),
                    main.id,
                    main.revision,
                    "managed",
                )
                values = build_generated_environment(
                    self.layout,
                    profile,
                    [unusable, *other_installs],
                    cache_root=cache,
                )
                self.assertNotIn("MAIN_MODEL_CONTAINER_PATH", values)
                self.assertNotIn("ROUTER_MODEL_CONTAINER_PATH", values)

        planned = ModelInstall(
            "planned",
            str(cache / "repos" / "planned-main"),
            main.id,
            main.revision,
            "managed",
        )
        dry_run_values = build_generated_environment(
            self.layout,
            profile,
            [planned, *other_installs],
            cache_root=cache,
            allow_planned=True,
        )
        self.assertEqual(
            dry_run_values["MAIN_MODEL_CONTAINER_PATH"],
            "/models/repos/planned-main",
        )

    def test_native_generated_environment_uses_authenticated_host_bridge_and_contains_no_secrets(self) -> None:
        profile = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        cache = self.root / "model cache"
        values = build_generated_environment(
            self.layout,
            profile,
            self.installs(profile, cache),
            cache_root=cache,
            search_enabled=True,
        )
        self.assertEqual(values["OPENAI_BASE_URL"], "http://host.docker.internal:18100/v1")
        self.assertEqual(values["ROUTER_BASE_URL"], values["OPENAI_BASE_URL"])
        self.assertEqual(values["MAIN_REQUIRES_AUTHENTICATION"], "true")
        self.assertEqual(values["EMBED_REQUIRES_AUTHENTICATION"], "true")
        self.assertEqual(values["VISION_ENABLED"], "false")
        self.assertEqual(values["OCR_ENABLED"], "false")
        self.assertEqual(values["SEARCH_ENABLED"], "true")
        self.assertEqual(values["SEARXNG_URL"], "http://searxng:8080")
        self.assertIn("MAIN_MODEL_CONTAINER_PATH", values)
        forbidden = {
            "HF_TOKEN",
            "POSTGRES_PASSWORD",
            "OPENAI_API_KEY",
            "TECHSARA_MODEL_API_KEY",
            "SESSION_SECRET",
            "SF_CLIENT_SECRET",
            "SF_PRIVATE_KEY_B64",
        }
        self.assertTrue(forbidden.isdisjoint(values), forbidden.intersection(values))

    def test_search_provider_is_preserved_without_starting_an_unneeded_searx_endpoint(self) -> None:
        profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        cache = self.root / "cache"
        values = build_generated_environment(
            self.layout,
            profile,
            self.installs(profile, cache),
            cache_root=cache,
            search_enabled=True,
            search_provider="tavily",
        )
        self.assertEqual(values["SEARCH_PROVIDER"], "tavily")
        self.assertEqual(values["SEARXNG_URL"], "")
        with self.assertRaisesRegex(TechSaraError, "SEARCH_PROVIDER"):
            build_generated_environment(
                self.layout,
                profile,
                self.installs(profile, cache),
                cache_root=cache,
                search_enabled=True,
                search_provider="unknown-provider",
            )

    def test_dgx_environment_uses_internal_model_services_and_skip_ocr_is_consistent(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        cache = self.root / "cache"
        values = build_generated_environment(
            self.layout,
            profile,
            self.installs(profile, cache),
            cache_root=cache,
            skip_ocr=True,
        )
        self.assertEqual(values["OPENAI_BASE_URL"], "http://vllm:30000/v1")
        self.assertEqual(values["ROUTER_BASE_URL"], "http://vllm-router:30002/v1")
        self.assertEqual(values["EMBED_BASE_URL"], "http://vllm-embed:30003/v1")
        self.assertEqual(values["RERANK_BACKEND"], "remote")
        self.assertEqual(values["RERANK_BASE_URL"], "http://vllm-reranker:30005")
        self.assertEqual(values["MAIN_REQUIRES_AUTHENTICATION"], "false")
        self.assertEqual(values["OCR_ENABLED"], "false")
        self.assertEqual(values["OCR_MODEL"], "disabled")
        self.assertNotIn("OCR_MODEL_CONTAINER_PATH", values)

    def test_cpu_and_app_only_modes_never_invent_cloud_fallbacks(self) -> None:
        cache = self.root / "cache"
        cpu_profile = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        cpu_values = build_generated_environment(
            self.layout,
            cpu_profile,
            self.installs(cpu_profile, cache),
            cache_root=cache,
        )
        self.assertEqual(cpu_values["OPENAI_BASE_URL"], "http://llama-cpp:30000/v1")
        self.assertEqual(cpu_values["EMBED_MODEL"], "disabled")

        app_profile = select_profile(
            cpu(memory_gib=6), REPO_ROOT, reuse_running_models=True
        )
        app_values = build_generated_environment(
            self.layout, app_profile, [], cache_root=cache
        )
        self.assertEqual(app_values["OPENAI_BASE_URL"], "http://disabled.invalid/v1")
        self.assertEqual(app_values["MAIN_MODEL"], "disabled")
        self.assertEqual(app_values["MAIN_ENABLED"], "false")
        self.assertNotIn("api.openai.com", " ".join(app_values.values()).lower())

    def test_external_development_accepts_only_explicit_local_endpoints(self) -> None:
        profile = select_profile(
            cpu(),
            REPO_ROOT,
            profile_override="external-development",
            reuse_running_models=True,
        )
        configured = {
            "OPENAI_BASE_URL": "http://host.docker.internal:19000/v1",
            "MAIN_MODEL": "fixture-local-model",
        }
        values = build_generated_environment(
            self.layout,
            profile,
            [],
            cache_root=self.root / "cache",
            external_environment=configured,
        )
        self.assertEqual(values["MAIN_MODEL"], "fixture-local-model")
        self.assertEqual(values["MAIN_ENABLED"], "true")
        self.assertEqual(values["MAIN_SUPPORTS_CHAT"], "true")
        self.assertEqual(values["MAIN_SUPPORTS_STREAMING"], "true")

        for unsafe in (
            "https://api.openai.com/v1",
            "http://127.attacker.example/v1",
            "http://user:password@localhost:19000/v1",
        ):
            with self.subTest(url=unsafe), self.assertRaisesRegex(
                TechSaraError, "automatic cloud fallback is prohibited"
            ):
                build_generated_environment(
                    self.layout,
                    profile,
                    [],
                    cache_root=self.root / "cache",
                    external_environment={**configured, "OPENAI_BASE_URL": unsafe},
                )

    def test_context_override_is_globally_capped_to_the_model_limit(self) -> None:
        profile = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        cache = self.root / "cache"
        override = profile.main_model.context_limit + 4096
        values = build_generated_environment(
            self.layout,
            profile,
            self.installs(profile, cache),
            cache_root=cache,
            context_override=override,
        )
        self.assertEqual(values["MODEL_MAX_CONTEXT"], str(profile.main_model.context_limit))
        self.assertEqual(values["DEFAULT_MAX_CONTEXT"], str(profile.main_model.context_limit))
        self.assertEqual(values["REPORT_MAX_CONTEXT"], str(profile.main_model.context_limit))
        self.assertEqual(values["MAIN_CONTEXT_LENGTH"], str(profile.main_model.context_limit))


class ClusterEnvironmentTests(EnvironmentCase):
    """Two-node DGX Spark cluster keys are generated only when requested."""

    DUAL = {
        "CLUSTER_MODE": "dual",
        "CLUSTER_HEAD_IP": "192.168.100.1",
        "CLUSTER_WORKER_IP": "192.168.100.2",
    }

    def setUp(self) -> None:
        super().setUp()
        self.detectors = ClusterDetectors(
            ifname_for_ip=lambda ip: {"192.168.100.1": "enP2p1s0f1np1"}.get(ip),
            hcas_for_ifnames=lambda names: ["rocep1s0f1" for name in names if name == "enP2p1s0f1np1"],
            docker_bridge_gateway=lambda: "172.17.0.1",
        )
        patcher = patch.object(environment, "CLUSTER_DETECTORS", self.detectors)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _generate(self, profile: SelectedProfile, user_environment: dict[str, str]) -> dict[str, str]:
        cache = self.root / "cache"
        return build_generated_environment(
            self.layout,
            profile,
            self.installs(profile, cache),
            cache_root=cache,
            user_environment=user_environment,
        )

    def test_single_mode_emits_only_the_mode_marker_and_no_cluster_keys(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        # The default (auto) with no RoCE link degrades to single with a reason.
        baseline = self._generate(profile, {})
        self.assertEqual(baseline["TECHSARA_CLUSTER_MODE"], "single")
        self.assertEqual(baseline["TECHSARA_CLUSTER_REASON"], "no RoCE link carries an address")
        self.assertEqual(self.discovery_calls, ["links"])
        explicit = self._generate(profile, {"CLUSTER_MODE": "single"})
        self.assertEqual(explicit["TECHSARA_CLUSTER_MODE"], "single")
        self.assertEqual(explicit["TECHSARA_CLUSTER_REASON"], "CLUSTER_MODE=single in .env")
        self.assertEqual(self.discovery_calls, ["links"], "forced single must not probe anything")
        self.assertEqual({**explicit, "TECHSARA_CLUSTER_REASON": ""}, {**baseline, "TECHSARA_CLUSTER_REASON": ""})
        self.assertEqual([key for key in baseline if key.startswith("CLUSTER_")], [])
        self.assertEqual(baseline["OPENAI_BASE_URL"], "http://vllm:30000/v1")
        # Cluster-only keys are ignored, not validated, when the mode is single.
        ignored = self._generate(profile, {"CLUSTER_MODE": "single", "CLUSTER_HEAD_IP": "not-an-address"})
        self.assertEqual(ignored, explicit)

    def test_auto_mode_off_dgx_spark_is_single_without_any_discovery(self) -> None:
        for name, hardware in (("nvidia-large", nvidia(80)), ("local-minimal", cpu()), ("mac", mac(64))):
            with self.subTest(profile=name):
                values = self._generate(select_profile(hardware, REPO_ROOT, reuse_running_models=True), {})
                self.assertEqual(values["TECHSARA_CLUSTER_MODE"], "single")
                self.assertEqual(values["TECHSARA_CLUSTER_REASON"], "")
                self.assertEqual([key for key in values if key.startswith("CLUSTER_")], [])
        self.assertEqual(self.discovery_calls, [])

    def test_auto_mode_on_dgx_spark_generates_dual_from_the_discovered_peer(self) -> None:
        from techsara_cli.cluster import ClusterLink

        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        discovery = fake_discovery(
            links=[ClusterLink("enP2p1s0f1np1", "192.168.100.1", 24)],
            peers={"enP2p1s0f1np1": "192.168.100.2"},
            calls=self.discovery_calls,
        )
        with patch.object(environment, "CLUSTER_DISCOVERY", discovery):
            values = self._generate(profile, {})
        self.assertEqual(values["TECHSARA_CLUSTER_MODE"], "dual")
        self.assertEqual(values["TECHSARA_CLUSTER_REASON"], "second DGX Spark spark-2 at 192.168.100.2 (auto-detected)")
        self.assertEqual(values["CLUSTER_HEAD_IP"], "192.168.100.1")
        self.assertEqual(values["CLUSTER_WORKER_IP"], "192.168.100.2")
        self.assertEqual(values["CLUSTER_WORKER_SSH"], "tester@192.168.100.2")
        self.assertEqual(values["OPENAI_BASE_URL"], "http://vllm:8000/v1")
        self.assertIn("--master-addr 192.168.100.1", values["CLUSTER_ENGINE_ARGS"])
        self.assertEqual(self.discovery_calls, ["links", "peer:enP2p1s0f1np1", "preflight:tester@192.168.100.2"])

    def test_dual_mode_on_dgx_spark_emits_cluster_keys_and_host_port_urls(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        values = self._generate(profile, dict(self.DUAL))
        self.assertEqual(values["TECHSARA_CLUSTER_MODE"], "dual")
        self.assertEqual(values["TECHSARA_CLUSTER_REASON"], "second DGX Spark spark-2 at 192.168.100.2 (configured)")
        self.assertEqual(values["CLUSTER_WORKER_SSH"], "tester@192.168.100.2")
        self.assertEqual(values["OPENAI_BASE_URL"], "http://vllm:8000/v1")
        self.assertEqual(values["VISION_BASE_URL"], "http://vllm:8000/v1")
        self.assertEqual(values["VLLM_PORT"], "8000")
        # The separate router is unchanged; only the main endpoint moves to the host.
        self.assertEqual(values["ROUTER_BASE_URL"], "http://vllm-router:30002/v1")
        self.assertEqual(values["EMBED_BASE_URL"], "http://vllm-embed:30003/v1")
        self.assertEqual(values["RERANK_BASE_URL"], "http://vllm-reranker:30005")
        self.assertEqual(values["CLUSTER_HEAD_IP"], "192.168.100.1")
        self.assertEqual(values["CLUSTER_WORKER_IP"], "192.168.100.2")
        self.assertEqual(values["CLUSTER_HEAD_IP_2"], "")
        self.assertEqual(values["CLUSTER_WORKER_IP_2"], "")
        self.assertEqual(values["CLUSTER_MASTER_PORT"], "29501")
        self.assertEqual(values["CLUSTER_TENSOR_PARALLEL_SIZE"], "2")
        self.assertEqual(values["CLUSTER_PIPELINE_PARALLEL_SIZE"], "1")
        self.assertEqual(values["CLUSTER_GPU_MEMORY_UTILIZATION"], "0.30")
        self.assertEqual(values["CLUSTER_NCCL_DEBUG"], "INFO")
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enP2p1s0f1np1")
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1")
        self.assertEqual(values["CLUSTER_API_BIND_ADDRESS"], "172.17.0.1")
        engine = values["CLUSTER_ENGINE_ARGS"]
        self.assertTrue(engine.startswith(f"--max-model-len {values['MODEL_MAX_CONTEXT']} "))
        self.assertIn("--quantization modelopt --attention-backend flashinfer", engine)
        self.assertIn("--nnodes 2 --master-addr 192.168.100.1 --master-port 29501", engine)
        # A shared router (main model) follows the main endpoint.
        shared = replace(profile, router_model=profile.main_model, router_shared=True)
        self.assertEqual(self._generate(shared, dict(self.DUAL))["ROUTER_BASE_URL"], "http://vllm:8000/v1")

    def test_dual_mode_honours_a_custom_published_port_and_the_publish_opt_in(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        values = self._generate(
            profile, {**self.DUAL, "VLLM_PORT": "18000", "PUBLISH_MODEL_PORTS": "true"}
        )
        self.assertEqual(values["OPENAI_BASE_URL"], "http://vllm:18000/v1")
        self.assertEqual(values["VLLM_PORT"], "18000")
        self.assertEqual(values["CLUSTER_API_BIND_ADDRESS"], "0.0.0.0")
        self.assertEqual(values["TECHSARA_PUBLISH_MODEL_PORTS"], "true")

    def test_dual_mode_is_rejected_on_every_non_dgx_profile(self) -> None:
        for name, hardware in (("nvidia-large", nvidia(80)), ("local-minimal", cpu()), ("mac", mac(64))):
            with self.subTest(profile=name), self.assertRaisesRegex(
                TechSaraError, "CLUSTER_MODE=dual is only supported on the dgx-spark profile"
            ):
                self._generate(
                    select_profile(hardware, REPO_ROOT, reuse_running_models=True), dict(self.DUAL)
                )

    def test_invalid_cluster_values_are_rejected_before_anything_is_generated(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_MODE must be one of auto, single, dual"):
            self._generate(profile, {"CLUSTER_MODE": "triple"})
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP is required"):
            self._generate(profile, {"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": "192.168.100.1"})


class MainContextWindowTests(EnvironmentCase):
    """MAIN_MODEL_MAX_LEN: one user-owned knob, YaRN when it exceeds native."""

    DUAL = {
        "CLUSTER_MODE": "dual",
        "CLUSTER_HEAD_IP": "192.168.100.1",
        "CLUSTER_WORKER_IP": "192.168.100.2",
    }
    #: 800,000 / 262,144 rounded up to two decimals.
    FACTOR_800K = 3.06

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root / "cache"
        self.profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        self.detectors = ClusterDetectors(
            ifname_for_ip=lambda ip: {"192.168.100.1": "enP2p1s0f1np1"}.get(ip),
            hcas_for_ifnames=lambda names: ["rocep1s0f1" for name in names if name == "enP2p1s0f1np1"],
            docker_bridge_gateway=lambda: "172.17.0.1",
        )
        patcher = patch.object(environment, "CLUSTER_DETECTORS", self.detectors)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_main_config(self, document: dict | None = NESTED_CONFIG) -> None:
        """Materialise the main model's config.json where the install points."""
        main = self.profile.main_model
        directory = self.cache / "repos" / f"{main.key}--{main.revision[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        if document is not None:
            (directory / "config.json").write_text(json.dumps(document), encoding="utf-8")

    def _generate(self, user_environment: dict[str, str] | None = None) -> dict[str, str]:
        return build_generated_environment(
            self.layout,
            self.profile,
            self.installs(self.profile, self.cache),
            cache_root=self.cache,
            user_environment=user_environment or {},
        )

    def _notices(self, values: dict[str, str], generated: dict[str, str]) -> list[str]:
        return main_context_notices(
            values, generated, profile_context=profile_context_length(self.profile)
        )

    # -- the default: nothing changes -------------------------------------

    def test_an_unset_window_keeps_the_profile_choice_and_still_emits_both_keys(self) -> None:
        self._write_main_config()
        generated = self._generate()
        self.assertEqual(generated["MODEL_MAX_CONTEXT"], str(self.profile.context_length))
        self.assertEqual(generated["MAIN_CONTEXT_LENGTH"], str(self.profile.context_length))
        # Always emitted so the overlays interpolate deterministically.
        self.assertEqual(generated["MAIN_MODEL_NATIVE_CONTEXT"], "262144")
        self.assertEqual(generated["MAIN_MODEL_ROPE_OVERRIDE"], "")
        self.assertEqual(generated["MAIN_MODEL_KV_BYTES_PER_TOKEN"], "32768")
        self.assertEqual(self._notices({}, generated), [], "an ordinary window says nothing extra")

    def test_an_unreadable_model_directory_falls_back_to_todays_behaviour(self) -> None:
        generated = self._generate()  # no config.json was written
        self.assertEqual(generated["MAIN_MODEL_NATIVE_CONTEXT"], "")
        self.assertEqual(generated["MAIN_MODEL_ROPE_OVERRIDE"], "")
        self.assertEqual(generated["MAIN_MODEL_KV_BYTES_PER_TOKEN"], "")
        self.assertEqual(generated["MODEL_MAX_CONTEXT"], str(self.profile.context_length))
        # An explicit window is still honoured; only the extension is unknown.
        widened = self._generate({"MAIN_MODEL_MAX_LEN": "800000"})
        self.assertEqual(widened["MODEL_MAX_CONTEXT"], "800000")
        self.assertEqual(widened["MAIN_MODEL_ROPE_OVERRIDE"], "")

    # -- the knob ----------------------------------------------------------

    def test_the_knob_replaces_the_profile_window_in_every_derived_key(self) -> None:
        self._write_main_config()
        generated = self._generate({"MAIN_MODEL_MAX_LEN": "800000"})
        for key in (
            "MODEL_MAX_CONTEXT",
            "DEFAULT_MAX_CONTEXT",
            "REPORT_MAX_CONTEXT",
            "MAIN_CONTEXT_LENGTH",
        ):
            with self.subTest(key=key):
                self.assertEqual(generated[key], "800000")
        # The router is the same model on this profile, so it moves too; a
        # role with its own smaller limit does not.
        self.assertEqual(generated["VISION_CONTEXT_LENGTH"], "800000")
        self.assertEqual(
            generated["EMBED_CONTEXT_LENGTH"], str(self.profile.embedding_model.context_limit)
        )

    def test_a_window_past_native_enables_yarn_with_the_exact_override(self) -> None:
        self._write_main_config()
        generated = self._generate({"MAIN_MODEL_MAX_LEN": "800000"})
        self.assertEqual(generated["MAIN_MODEL_NATIVE_CONTEXT"], "262144")
        override = generated["MAIN_MODEL_ROPE_OVERRIDE"]
        argv = shlex.split(override)
        self.assertEqual(argv[0], "--hf-overrides")
        self.assertEqual(len(argv), 2, "the JSON must stay one argv element")
        parameters = json.loads(argv[1])["text_config"]["rope_parameters"]
        self.assertEqual(parameters["rope_type"], "yarn")
        self.assertEqual(parameters["factor"], self.FACTOR_800K)
        self.assertEqual(parameters["original_max_position_embeddings"], 262144)
        self.assertEqual(parameters["mrope_section"], [11, 11, 10])
        # A window inside the native one asks for no override at all.
        self.assertEqual(self._generate({"MAIN_MODEL_MAX_LEN": "131072"})["MAIN_MODEL_ROPE_OVERRIDE"], "")

    def test_beyond_four_times_native_is_refused(self) -> None:
        # This model's 4x ceiling (1,048,576) is also the key's upper bound, so
        # the refusal is proved on a model with a shorter native window.
        self._write_main_config(FLAT_CONFIG)
        with self.assertRaisesRegex(TechSaraError, "natively 32,768 tokens and 4x"):
            self._generate({"MAIN_MODEL_MAX_LEN": str(32768 * 4 + 1)})
        self.assertEqual(
            self._generate({"MAIN_MODEL_MAX_LEN": str(32768 * 4)})["MODEL_MAX_CONTEXT"], "131072"
        )

    def test_the_window_is_validated_before_anything_is_generated(self) -> None:
        self._write_main_config()
        low, high = MAIN_CONTEXT_RANGE
        for key in ("MAIN_MODEL_MAX_LEN", "MODEL_MAX_CONTEXT"):
            for value in ("not-a-number", "800_000.5", "1e6"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(
                    TechSaraError, f"{key} must be a whole number of tokens"
                ):
                    self._generate({key: value})
            for value in (str(low - 1), str(high + 1), "0", "-262144"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(
                    TechSaraError, f"{key} must be between 4,096 and 1,048,576 tokens"
                ):
                    self._generate({key: value})

    # -- the deprecated alias ---------------------------------------------

    def test_the_deprecated_alias_is_honoured_only_when_it_asks_for_something_else(self) -> None:
        self._write_main_config()
        baseline = self._generate()
        # Repeating the profile's own number changes nothing at all: this is
        # what most existing .env files contain.
        repeated = {"MODEL_MAX_CONTEXT": str(self.profile.context_length)}
        self.assertEqual(self._generate(repeated), baseline)
        self.assertEqual(self._notices(repeated, baseline), [])

        alias = {"MODEL_MAX_CONTEXT": "400000"}
        generated = self._generate(alias)
        self.assertEqual(generated["MODEL_MAX_CONTEXT"], "400000")
        self.assertEqual(generated["MAIN_CONTEXT_LENGTH"], "400000")
        self.assertTrue(generated["MAIN_MODEL_ROPE_OVERRIDE"].startswith("--hf-overrides "))
        notices = self._notices(alias, generated)
        self.assertIn("MAIN_MODEL_MAX_LEN is the supported spelling", notices[0])
        # Nothing extra is generated for the alias; only the line is different.
        self.assertEqual(generated, self._generate({"MAIN_MODEL_MAX_LEN": "400000"}))

    def test_the_supported_spelling_wins_when_both_are_set(self) -> None:
        self._write_main_config()
        both = {"MAIN_MODEL_MAX_LEN": "300000", "MODEL_MAX_CONTEXT": "400000"}
        generated = self._generate(both)
        self.assertEqual(generated["MODEL_MAX_CONTEXT"], "300000")
        self.assertEqual(
            self._notices(both, generated),
            [
                "Context: 300,000 tokens (model is natively 262,144; YaRN factor 1.15 enabled "
                "- long-context extension trades some short-prompt quality)",
                "Context: that window needs about 11 GiB of KV cache at TP=1 (32,768 bytes/token); "
                "vLLM sizes the pool while it starts and will refuse the window if the GPU cannot hold it",
            ],
        )

    # -- what `up` says ----------------------------------------------------

    def test_the_extended_window_is_announced_once_with_its_factor(self) -> None:
        self._write_main_config()
        values = {"MAIN_MODEL_MAX_LEN": "800000"}
        generated = self._generate(values)
        self.assertEqual(
            self._notices(values, generated)[0],
            "Context: 800,000 tokens (model is natively 262,144; YaRN factor 3.06 enabled "
            "- long-context extension trades some short-prompt quality)",
        )

    def test_a_single_node_only_estimates_the_kv_cost_and_never_refuses(self) -> None:
        self._write_main_config()
        values = {"MAIN_MODEL_MAX_LEN": "800000"}
        generated = self._generate(values)
        self.assertEqual(generated["TECHSARA_CLUSTER_MODE"], "single")
        # 800,000 x 32,768 B = 24.42 GiB of tokens, but only 88% of a hybrid
        # model's KV budget becomes paged tokens (the gated-delta-net layers
        # take the rest), so the honest estimate is 28 GiB.
        self.assertIn("about 28 GiB of KV cache at TP=1", self._notices(values, generated)[1])

    # -- the two-node KV budget -------------------------------------------

    def test_the_measured_838860_window_fits_the_default_16_gib_budget(self) -> None:
        self._write_main_config()
        values = {**self.DUAL, "MAIN_MODEL_MAX_LEN": "838860"}
        generated = self._generate(values)
        self.assertEqual(generated["TECHSARA_CLUSTER_MODE"], "dual")
        self.assertEqual(generated["CLUSTER_KV_CACHE_MEMORY_GIB"], "16")
        self.assertEqual(generated["MODEL_MAX_CONTEXT"], "838860")
        engine = generated["CLUSTER_ENGINE_ARGS"]
        self.assertIn("--max-model-len 838860 --hf-overrides ", engine)
        self.assertEqual(json.loads(shlex.split(engine)[shlex.split(engine).index("--hf-overrides") + 1])
                         ["text_config"]["rope_parameters"]["factor"], 3.2)
        # Exactly 4x native does NOT fit 16 GiB: the measured pool is 922,746
        # tokens, not the naive 1,048,576, so the guard refuses it here rather
        # than letting vLLM refuse it 30 minutes into a start-up.
        with self.assertRaisesRegex(TechSaraError, "set CLUSTER_KV_CACHE_MEMORY_GIB=19"):
            self._generate({**self.DUAL, "MAIN_MODEL_MAX_LEN": "1048576"})
        # It fits once the budget is raised to what the guard asks for.
        self.assertEqual(
            self._generate({**self.DUAL, "MAIN_MODEL_MAX_LEN": "1048576",
                            "CLUSTER_KV_CACHE_MEMORY_GIB": "19"})["MODEL_MAX_CONTEXT"],
            "1048576",
        )
        # The cluster line is the only extra narration beyond the window ones.
        self.assertEqual(len(self._notices(values, generated)), 1, "no TP=1 estimate in dual mode")

    def test_a_window_the_kv_pool_cannot_hold_is_refused_with_the_arithmetic(self) -> None:
        self._write_main_config()
        with self.assertRaises(TechSaraError) as caught:
            self._generate({**self.DUAL, "MAIN_MODEL_MAX_LEN": "1048576", "CLUSTER_KV_CACHE_MEMORY_GIB": "15"})
        message = str(caught.exception)
        self.assertIn("MAIN_MODEL_MAX_LEN=1,048,576 needs a KV pool of at least 1,048,576 tokens", message)
        self.assertIn("CLUSTER_KV_CACHE_MEMORY_GIB=15 holds 865,075 tokens at TP=2", message)
        self.assertIn("16,384 bytes/token at --kv-cache-dtype fp8", message)
        self.assertIn("set CLUSTER_KV_CACHE_MEMORY_GIB=19", message)
        self.assertIn("recurrent layers take their per-sequence state", message)
        # TP=1 (pipeline parallel across the two nodes) doubles the per-token
        # cost, so the same budget holds half as much.
        with self.assertRaisesRegex(TechSaraError, "holds 461,373 tokens at TP=1"):
            self._generate(
                {
                    **self.DUAL,
                    "MAIN_MODEL_MAX_LEN": "600000",
                    "CLUSTER_TENSOR_PARALLEL_SIZE": "1",
                    "CLUSTER_PIPELINE_PARALLEL_SIZE": "2",
                }
            )

    def test_a_flat_config_model_is_measured_from_its_own_geometry(self) -> None:
        self._write_main_config(FLAT_CONFIG)
        generated = self._generate({"MAIN_MODEL_MAX_LEN": "65536"})
        self.assertEqual(generated["MAIN_MODEL_NATIVE_CONTEXT"], "32768")
        # 32 paged layers, 8 KV heads, head_dim 128, fp8.
        self.assertEqual(generated["MAIN_MODEL_KV_BYTES_PER_TOKEN"], str(2 * 32 * 8 * 128))
        self.assertIn('"factor":2.0', generated["MAIN_MODEL_ROPE_OVERRIDE"])


if __name__ == "__main__":
    unittest.main()
