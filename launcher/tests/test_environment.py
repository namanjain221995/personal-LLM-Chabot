from __future__ import annotations

import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .support import REPO_ROOT
    from .test_profiles import cpu, mac, nvidia
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT
    from test_profiles import cpu, mac, nvidia

from techsara_cli.environment import (
    RuntimeLayout,
    _container_path,
    build_generated_environment,
    effective_user_environment,
    has_salesforce_credentials,
    prepare_local_secrets,
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


if __name__ == "__main__":
    unittest.main()
