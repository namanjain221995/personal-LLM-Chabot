"""Unit contracts for safe Docker Compose command and environment assembly.

These tests do not invoke Docker.  They exercise the exact argv and subprocess
environment that the launcher will hand to Docker Compose.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from .support import REPO_ROOT  # noqa: F401 - installs launcher on sys.path
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT  # noqa: F401

from techsara_cli.compose import _INTERNAL_PROBE, ComposeManager, reconcile_project_services
from techsara_cli.errors import TechSaraError


class ComposeManagerEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project with spaces"
        self.root.mkdir()
        self.user_env = self.root / ".env"
        self.secrets_env = self.root / ".runtime" / "secrets.env"
        self.generated_env = self.root / ".runtime" / "generated.env"
        self.secrets_env.parent.mkdir()
        self.user_env.write_text(
            "PRECEDENCE=user\nUSER_ONLY=from-user\nOPENAI_API_KEY=legacy\n",
            encoding="utf-8",
        )
        self.secrets_env.write_text(
            "PRECEDENCE=secret\nSECRET_ONLY=from-secret\nOPENAI_API_KEY=private\n",
            encoding="utf-8",
        )
        self.generated_env.write_text(
            "PRECEDENCE=generated\nGENERATED_ONLY=from-generated\nMODEL_MAX_CONTEXT=8192\n",
            encoding="utf-8",
        )
        self.manager = ComposeManager(
            self.root,
            ("compose.yaml", "compose/compose.cpu.yaml"),
            self.generated_env,
            self.secrets_env,
            profiles=("search", "embeddings", "search"),
        )

    def test_file_chain_overrides_inherited_managed_values_but_preserves_unrelated_state(self) -> None:
        inherited = {
            "PATH": "/fixture/bin",
            "DOCKER_HOST": "unix:///fixture/docker.sock",
            "PRECEDENCE": "exported",
            "MODEL_MAX_CONTEXT": "262144",
            "OPENAI_API_KEY": "exported-key",
        }
        with patch.dict(os.environ, inherited, clear=True):
            values = self.manager._environment()

        self.assertEqual(values["PATH"], inherited["PATH"])
        self.assertEqual(values["DOCKER_HOST"], inherited["DOCKER_HOST"])
        self.assertEqual(values["USER_ONLY"], "from-user")
        self.assertEqual(values["SECRET_ONLY"], "from-secret")
        self.assertEqual(values["GENERATED_ONLY"], "from-generated")
        self.assertEqual(values["OPENAI_API_KEY"], "private")
        self.assertEqual(values["PRECEDENCE"], "generated")
        self.assertEqual(values["MODEL_MAX_CONTEXT"], "8192")
        self.assertEqual(values["TECHSARA_GENERATED_ENV"], str(self.generated_env.resolve()))
        self.assertEqual(values["TECHSARA_SECRET_ENV"], str(self.secrets_env.resolve()))

    def test_command_declares_env_files_compose_files_and_profiles_in_stable_order(self) -> None:
        command = self.manager.command("config", "--quiet")
        self.assertEqual(command[:4], ["docker", "compose", "--project-name", "sf-local-ai"])
        self.assertEqual(
            command[4:],
            [
                "--env-file", str(self.user_env),
                "--env-file", str(self.secrets_env.resolve()),
                "--env-file", str(self.generated_env.resolve()),
                "-f", str(self.root / "compose.yaml"),
                "-f", str(self.root / "compose/compose.cpu.yaml"),
                "--profile", "embeddings",
                "--profile", "search",
                "config", "--quiet",
            ],
        )


class ProbeAndHealthContractTests(unittest.TestCase):
    def test_reasoning_probe_sends_chat_template_kwargs_at_top_level(self) -> None:
        self.assertIn(
            "body = dict(chat, chat_template_kwargs={'enable_thinking':True})",
            _INTERNAL_PROBE,
        )
        self.assertNotIn("extra_body={'chat_template_kwargs'", _INTERNAL_PROBE)

    def test_orchestrator_compose_healthcheck_validates_json_and_app_database(self) -> None:
        compose_text = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        orchestrator = compose_text.split("  orchestrator:", 1)[1].split(
            "  sync-worker:", 1
        )[0]
        self.assertIn("payload = json.load(response)", orchestrator)
        self.assertIn('payload.get("status") not in {"ok", "healthy", "degraded"}', orchestrator)
        self.assertIn('app_db.get("status") != "ok"', orchestrator)
        self.assertIn("raise SystemExit(1)", orchestrator)
        self.assertNotIn("-o /dev/null", orchestrator)


class ProjectServiceReconciliationTests(unittest.TestCase):
    def test_only_known_stale_or_public_model_services_are_stopped_by_validated_id(self) -> None:
        calls: list[tuple[tuple[str, ...], float]] = []
        listing = "\n".join(
            (
                "a1b2c3d4e5f6\tvllm\t30000/tcp",
                "112233aabbcc\tvllm-embed\t0.0.0.0:8003->30003/tcp",
                "abcdef123456\tvllm-ocr\t30004/tcp",
                "1234bad-id\tvllm-router\t30002/tcp",
                "998877665544\torchestrator\t127.0.0.1:8080->8080/tcp",
            )
        )

        def runner(args, *, timeout):
            normalized = tuple(str(item) for item in args)
            calls.append((normalized, timeout))
            if normalized[:2] == ("docker", "ps"):
                return SimpleNamespace(returncode=0, stdout=listing, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        stopped = reconcile_project_services(
            {"vllm", "vllm-embed", "vllm-router"}, runner=runner
        )

        self.assertEqual(stopped, ["vllm-embed", "vllm-ocr"])
        self.assertEqual(
            [call for call, _timeout in calls[1:]],
            [
                ("docker", "stop", "--time", "60", "112233aabbcc"),
                ("docker", "stop", "--time", "60", "abcdef123456"),
            ],
        )
        self.assertIn("label=com.docker.compose.project=sf-local-ai", calls[0][0])

    def test_inspection_or_stop_failure_aborts_without_claiming_success(self) -> None:
        def inspection_failure(args, *, timeout):  # noqa: ARG001
            return SimpleNamespace(returncode=1, stdout="", stderr="unavailable")

        with self.assertRaises(TechSaraError):
            reconcile_project_services(set(), runner=inspection_failure)

        def stop_failure(args, *, timeout):  # noqa: ARG001
            normalized = tuple(str(item) for item in args)
            if normalized[:2] == ("docker", "ps"):
                return SimpleNamespace(
                    returncode=0,
                    stdout="abcdef123456\tvllm-vision\t8001->30001/tcp",
                    stderr="",
                )
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")

        with self.assertRaises(TechSaraError):
            reconcile_project_services(set(), runner=stop_failure)


if __name__ == "__main__":
    unittest.main()
