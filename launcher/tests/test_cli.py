"""Unit contracts for the cross-platform launcher command surface.

All operating-system, Docker, process, signal, network, download, and runtime
boundaries are replaced with test doubles.  Tests may create ordinary fixture
files only inside temporary directories.
"""

from __future__ import annotations

import argparse
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, Mock, call, patch

try:
    from .support import GIB, REPO_ROOT
    from .test_profiles import cpu, mac, nvidia
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB, REPO_ROOT
    from test_profiles import cpu, mac, nvidia

from techsara_cli import cli
from techsara_cli.environment import RuntimeLayout
from techsara_cli.errors import PrerequisiteError, TechSaraError
from techsara_cli.model_manager import ModelInstall
from techsara_cli.profiles import SelectedProfile, load_model_manifest, select_profile
from techsara_cli.runtime import RuntimeInstall


def selected(hardware, *, skip_ocr: bool = False) -> SelectedProfile:
    return select_profile(
        hardware,
        REPO_ROOT,
        skip_ocr=skip_ocr,
        reuse_running_models=True,
    )


def installs_for(profile: SelectedProfile, cache: Path) -> list[ModelInstall]:
    return [
        ModelInstall(
            "complete",
            str(cache / model.key),
            model.id,
            model.revision,
            "managed",
        )
        for model in profile.required_models(skip_ocr=False)
    ]


def command_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["fixture"], returncode, stdout, stderr)


class ParserAndDispatchTests(unittest.TestCase):
    def test_parser_exposes_the_complete_command_surface(self) -> None:
        parser = cli._parser()
        for command in (
            "up",
            "down",
            "restart",
            "status",
            "doctor",
            "logs",
            "models",
            "redetect",
            "update-models",
        ):
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args([command]).command, command)

    def test_up_and_restart_accept_all_declared_overrides(self) -> None:
        parser = cli._parser()
        flags = [
            "--dry-run",
            "--profile",
            "nvidia-small",
            "--model",
            "Qwen/Qwen3-8B-AWQ",
            "--skip-ocr",
            "--offline",
            "--verbose",
        ]
        for command in ("up", "restart"):
            with self.subTest(command=command):
                args = parser.parse_args([command, *flags])
                self.assertTrue(args.dry_run)
                self.assertEqual(args.profile, "nvidia-small")
                self.assertEqual(args.model, "Qwen/Qwen3-8B-AWQ")
                self.assertTrue(args.skip_ocr)
                self.assertTrue(args.offline)
                self.assertTrue(args.verbose)

    def test_command_specific_defaults_are_bounded_and_predictable(self) -> None:
        parser = cli._parser()
        logs = parser.parse_args(["logs"])
        update = parser.parse_args(["update-models"])
        self.assertEqual(logs.tail, 200)
        self.assertIsNone(logs.service)
        self.assertFalse(update.offline)
        self.assertFalse(update.dry_run)

    def test_missing_or_unknown_command_is_rejected_before_dispatch(self) -> None:
        for argv in ([], ["destroy-everything"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_main_dispatches_each_non_restart_command_with_project_root(self) -> None:
        root = Path("/fixture/project")
        cases = {
            "up": "_cmd_up",
            "down": "_cmd_down",
            "status": "_cmd_status",
            "doctor": "_cmd_doctor",
            "logs": "_cmd_logs",
            "models": "_cmd_models",
            "redetect": "_cmd_redetect",
        }
        for command, target in cases.items():
            with self.subTest(command=command), ExitStack() as stack:
                stack.enter_context(patch.object(cli, "_project_root", return_value=root))
                handler = stack.enter_context(patch.object(cli, target, return_value=17))
                self.assertEqual(cli.main([command]), 17)
                handler.assert_called_once()
                self.assertEqual(handler.call_args.kwargs["root"], root)

    def test_update_models_dispatches_to_models_in_ensure_mode(self) -> None:
        with (
            patch.object(cli, "_project_root", return_value=Path("/fixture")),
            patch.object(cli, "_cmd_models", return_value=0) as models,
        ):
            result = cli.main(["update-models", "--offline", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertTrue(models.call_args.kwargs["ensure"])
        self.assertTrue(models.call_args.args[0].offline)
        self.assertTrue(models.call_args.args[0].dry_run)

    def test_restart_stops_before_starting_and_preserves_all_up_flags(self) -> None:
        events: list[tuple[str, argparse.Namespace]] = []

        def down(args, *, root):
            events.append(("down", args))
            return 0

        def up(args, *, root):
            events.append(("up", args))
            return 23

        argv = [
            "restart",
            "--dry-run",
            "--profile",
            "mac-48-79gb",
            "--model",
            "mlx-community/model",
            "--skip-ocr",
            "--offline",
            "--verbose",
        ]
        with (
            patch.object(cli, "_project_root", return_value=Path("/fixture")),
            patch.object(cli, "_cmd_down", side_effect=down),
            patch.object(cli, "_cmd_up", side_effect=up),
        ):
            self.assertEqual(cli.main(argv), 23)
        self.assertEqual([name for name, _ in events], ["down", "up"])
        self.assertTrue(events[0][1].dry_run)
        up_args = events[1][1]
        self.assertEqual(up_args.profile, "mac-48-79gb")
        self.assertEqual(up_args.model, "mlx-community/model")
        self.assertTrue(up_args.skip_ocr)
        self.assertTrue(up_args.offline)
        self.assertTrue(up_args.verbose)

    def test_keyboard_interrupt_has_shell_standard_exit_code(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(cli, "_cmd_status", side_effect=KeyboardInterrupt),
            redirect_stderr(stderr),
        ):
            self.assertEqual(cli.main(["status"]), 130)
        self.assertIn("interrupted", stderr.getvalue().lower())

    def test_known_launcher_error_returns_two_without_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(cli, "_cmd_status", side_effect=TechSaraError("fixture failure")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(cli.main(["status"]), 2)
        self.assertEqual(stderr.getvalue().strip(), "TechSara error: fixture failure")

    def test_top_level_error_output_never_discloses_bearer_secrets(self) -> None:
        secret = "sk-fixture-ultra-sensitive-123456"
        stderr = io.StringIO()
        with (
            patch.object(
                cli,
                "_cmd_status",
                side_effect=TechSaraError(f"upstream rejected Authorization: Bearer {secret}"),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(cli.main(["status"]), 2)
        self.assertNotIn(secret, stderr.getvalue())

    def test_logs_tail_validation_prevents_handler_invocation(self) -> None:
        for tail in (0, 10001):
            with self.subTest(tail=tail):
                stderr = io.StringIO()
                with (
                    patch.object(cli, "_cmd_logs") as logs,
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(cli.main(["logs", "--tail", str(tail)]), 2)
                logs.assert_not_called()
                self.assertIn("between 1 and 10000", stderr.getvalue())

    def test_module_entry_point_exits_with_main_result(self) -> None:
        with patch.object(cli, "main", return_value=29) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("techsara_cli.__main__", run_name="__main__")
        self.assertEqual(caught.exception.code, 29)
        main.assert_called_once_with()


class WrapperDelegationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.posix_path = REPO_ROOT / "techsara"
        cls.powershell_path = REPO_ROOT / "techsara.ps1"
        cls.cmd_path = REPO_ROOT / "techsara.cmd"
        cls.posix = cls.posix_path.read_text(encoding="utf-8")
        cls.powershell = cls.powershell_path.read_text(encoding="utf-8")
        cls.cmd = cls.cmd_path.read_text(encoding="utf-8")

    def test_posix_wrapper_is_executable_and_delegates_all_arguments(self) -> None:
        self.assertTrue(self.posix_path.stat().st_mode & 0o111)
        self.assertTrue(self.posix.startswith("#!/bin/sh\n"))
        self.assertIn("python -m techsara_cli \"$@\"", self.posix)
        self.assertIn("exec \"$TECHSARA_UV\" run", self.posix)

    def test_powershell_wrapper_delegates_remaining_arguments_to_same_module(self) -> None:
        self.assertIn('@("--", "python", "-m", "techsara_cli")', self.powershell)
        self.assertIn("$UvArgs += $args", self.powershell)
        self.assertIn("& $Uv @UvArgs", self.powershell)
        self.assertIn("exit $LASTEXITCODE", self.powershell)

    def test_cmd_wrapper_forwards_every_argument_and_exit_code_to_powershell(self) -> None:
        self.assertIn('-File "%~dp0techsara.ps1" %*', self.cmd)
        self.assertIn("exit /b %ERRORLEVEL%", self.cmd)

    def test_wrappers_do_not_pipe_downloads_to_shell_or_request_elevation(self) -> None:
        lowered = "\n".join((self.posix, self.powershell, self.cmd)).lower()
        self.assertNotRegex(lowered, r"curl[^\n|]*\|[^\n]*\b(?:sh|bash)\b")
        self.assertNotRegex(lowered, r"wget[^\n|]*\|[^\n]*\b(?:sh|bash)\b")
        self.assertNotRegex(lowered, r"\beval\b|invoke-expression|\bsudo\b|runas")

    def test_bootstrap_downloads_are_version_and_checksum_pinned_before_execution(self) -> None:
        self.assertIn("TECHSARA_UV_VERSION=0.11.32", self.posix)
        self.assertRegex(self.posix, r"TECHSARA_INSTALLER_SHA256=[0-9a-f]{64}")
        self.assertLess(self.posix.index('verify_sha256 "$TECHSARA_INSTALLER"'), self.posix.index('sh "$TECHSARA_INSTALLER"'))
        self.assertIn('$UvVersion = "0.11.32"', self.powershell)
        self.assertRegex(self.powershell, r'\$InstallerSha256 = "[0-9a-f]{64}"')
        self.assertLess(self.powershell.index("Get-FileHash"), self.powershell.index("& powershell.exe"))


class CliSelectionHelperTests(unittest.TestCase):
    def test_require_docker_gates_each_required_capability(self) -> None:
        base = cpu()
        cases = {
            "docker_installed": "not installed",
            "docker_running": "daemon is not running",
            "docker_compose_available": "Compose is unavailable",
            "docker_linux_containers": "Linux containers",
        }
        for field, message in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(PrerequisiteError, message):
                    cli._require_docker(replace(base, **{field: False}))
        cli._require_docker(base)

    def test_unreadable_docker_socket_is_not_reported_as_a_stopped_daemon(self) -> None:
        # The daemon answering with EACCES and the daemon being down are opposite
        # problems; sending the user to "start Docker" wastes the whole session.
        hardware = replace(cpu(), docker_running=False, docker_permission_denied=True)
        with patch.object(cli, "_docker_group_members", return_value=(False, [])), patch.dict(
            cli.os.environ, {"USER": "fixture"}, clear=True
        ):
            with self.assertRaises(PrerequisiteError) as caught:
                cli._require_docker(hardware)
        message = str(caught.exception)
        self.assertNotIn("daemon is not running", message)
        self.assertIn("not allowed to use its socket", message)
        self.assertIn("sudo usermod -aG docker fixture", message)
        self.assertIn("newgrp docker", message)

    def test_docker_permission_remedy_matches_the_actual_group_state(self) -> None:
        # (group applied to this process, members of the group, expected hint)
        cases = [
            (False, [], "sudo usermod -aG docker fixture"),
            (False, ["fixture"], "newgrp docker"),
            (True, ["fixture"], "endpoint is at fault"),
        ]
        for active, members, expected in cases:
            with self.subTest(active=active, members=members):
                with patch.object(
                    cli, "_docker_group_members", return_value=(active, members)
                ), patch.dict(cli.os.environ, {"USER": "fixture"}, clear=True):
                    self.assertIn(expected, cli._docker_permission_remedy())
        # A host with no `docker` group at all must not invent one.
        with patch.object(cli, "_docker_group_members", return_value=None), patch.dict(
            cli.os.environ, {"USER": "fixture"}, clear=True
        ):
            self.assertIn("No 'docker' group exists", cli._docker_permission_remedy())

    def test_compose_files_are_deterministic_and_windows_nvidia_gets_wsl_overlay(self) -> None:
        root = Path("/fixture")
        linux_profile = selected(nvidia(24))
        linux_files = cli._compose_files(root, nvidia(24), linux_profile)
        windows_files = cli._compose_files(
            root,
            nvidia(24, operating_system="windows", wsl2=True),
            linux_profile,
        )
        self.assertEqual(linux_files[0], root / "compose.yaml")
        self.assertEqual(
            linux_files[1:],
            [root / item for item in linux_profile.compose_files],
        )
        self.assertEqual(windows_files[:-1], linux_files)
        self.assertEqual(windows_files[-1], root / "compose" / "compose.windows-wsl2.yaml")

    def test_model_downloader_reads_only_hf_token_from_dotenv_and_export_wins(self) -> None:
        root = Path("/fixture")
        layout = RuntimeLayout.for_project(root)
        hardware = replace(cpu(), selected_cache_path="/fixture/cache")
        with patch.dict(cli.os.environ, {"PATH": "/bin"}, clear=True), patch.object(
            cli, "ModelManager"
        ) as manager:
            cli._model_manager(
                layout,
                hardware,
                {"huggingface_hub": {"version": "1.2.3"}},
                {"HF_TOKEN": "dotenv-token", "SF_CLIENT_SECRET": "must-not-cross-boundary"},
            )
        environment = manager.call_args.kwargs["environ"]
        self.assertEqual(environment["HF_TOKEN"], "dotenv-token")
        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("SF_CLIENT_SECRET", environment)

        with patch.dict(cli.os.environ, {"HF_TOKEN": "exported-token"}, clear=True), patch.object(
            cli, "ModelManager"
        ) as manager:
            cli._model_manager(layout, hardware, {}, {"HF_TOKEN": "dotenv-token"})
        self.assertEqual(manager.call_args.kwargs["environ"]["HF_TOKEN"], "exported-token")

    def test_only_the_dgx_overlay_gets_a_standalone_reranker_service(self) -> None:
        """compose.nvidia.yaml declares no vllm-reranker, so only DGX asks for one."""
        dgx = selected(nvidia(128, dgx=True))
        self.assertTrue(cli._has_reranker_service(dgx))
        self.assertIn("reranker", cli._compose_profiles(dgx, {}, skip_ocr=False))
        self.assertIn(
            "vllm-reranker",
            cli._desired_optional_services(dgx, [], salesforce_ready=False),
        )

        generic = selected(nvidia(80))
        self.assertTrue(generic.reranker_model)
        self.assertFalse(cli._has_reranker_service(generic))
        self.assertNotIn("reranker", cli._compose_profiles(generic, {}, skip_ocr=False))
        self.assertNotIn(
            "vllm-reranker",
            cli._desired_optional_services(generic, [], salesforce_ready=False),
        )

    def test_compose_profiles_follow_features_skip_ocr_and_user_opt_ins(self) -> None:
        profile = selected(nvidia(80))
        models, _ = load_model_manifest(REPO_ROOT)
        profile = replace(
            profile,
            ocr_model=models["unlimited-ocr"],
            features=dict(profile.features, ocr=True),
        )
        values = {"SEARCH_ENABLED": "true", "COMPOSE_PROFILES": "admin,search"}
        self.assertEqual(
            cli._compose_profiles(profile, values, skip_ocr=False),
            ["embeddings", "ocr", "search", "admin"],
        )
        self.assertEqual(
            cli._compose_profiles(profile, values, skip_ocr=True),
            ["embeddings", "search", "admin"],
        )
        self.assertNotIn(
            "search",
            cli._compose_profiles(
                profile,
                {"SEARCH_ENABLED": "true", "SEARCH_PROVIDER": "tavily"},
                skip_ocr=True,
            ),
        )

    def test_secret_value_discovery_uses_names_not_arbitrary_configuration(self) -> None:
        values = {
            "POSTGRES_PASSWORD": "database-secret",
            "OPENAI_API_KEY": "model-secret",
            "NORMAL_SETTING": "not-secret",
            "TINY_TOKEN": "abc",
        }
        self.assertEqual(
            cli._secret_values(values),
            ["database-secret", "model-secret"],
        )

    def test_compose_state_rejects_paths_outside_the_project(self) -> None:
        layout = SimpleNamespace(
            project_root=Path("/safe/project"),
            state_file=Path("/safe/project/.runtime/state.json"),
            generated_env=MagicMock(),
            secrets_env=Path("/safe/project/.runtime/secrets.env"),
        )
        layout.generated_env.is_file.return_value = True
        with (
            patch.object(cli, "load_json", return_value={"compose_files": ["../foreign.yaml"]}),
            patch.object(cli, "effective_user_environment", return_value={}),
            patch.object(cli, "ComposeManager") as compose,
        ):
            with self.assertRaises(TechSaraError):
                cli._compose_from_state(layout)
        compose.assert_not_called()


class UpCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = SimpleNamespace(
            project_root=self.root,
            runtime_dir=self.root / ".runtime",
            hardware_file=self.root / ".runtime" / "hardware.json",
            profile_file=self.root / ".runtime" / "profile.json",
            generated_env=self.root / ".runtime" / "generated.env",
            secrets_env=self.root / ".runtime" / "secrets.env",
            state_file=self.root / ".runtime" / "state.json",
            capabilities_file=self.root / ".runtime" / "capabilities.json",
            locks_dir=self.root / ".runtime" / "locks",
            logs_dir=self.root / ".runtime" / "logs",
            pids_dir=self.root / ".runtime" / "pids",
            shared_root=self.root / "shared",
            create=Mock(),
        )
        self.hardware = replace(cpu(), selected_cache_path=str(self.root / "models"))
        self.profile = selected(self.hardware)
        self.installs = installs_for(self.profile, self.root / "models")
        self.manager = Mock()
        self.manager.ensure_all.return_value = self.installs
        self.compose = Mock()
        self.compose.command.return_value = ["docker", "compose", "up", "-d"]
        self.compose.profiles = ()
        self.lock = MagicMock()

        self.layout_factory = self.stack.enter_context(
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout)
        )
        self.file_lock = self.stack.enter_context(patch.object(cli, "FileLock", return_value=self.lock))
        self.detect = self.stack.enter_context(patch.object(cli, "detect_hardware", return_value=self.hardware))
        self.require = self.stack.enter_context(patch.object(cli, "_require_docker"))
        self.running_models = self.stack.enter_context(
            patch.object(cli, "docker_project_has_running_models", return_value=False)
        )
        self.select = self.stack.enter_context(patch.object(cli, "select_profile", return_value=self.profile))
        self.manifest = self.stack.enter_context(
            patch.object(
                cli,
                "load_model_manifest",
                return_value=({}, {"huggingface_hub": {"version": "1.2.3"}, "vllm-metal": {}}),
            )
        )
        self.user_env = {"SEARCH_ENABLED": "true", "COMPOSE_PROFILES": "admin"}
        self.parse_env = self.stack.enter_context(patch.object(cli, "parse_env_file", return_value=self.user_env))
        self.prepare_secrets = self.stack.enter_context(
            patch.object(
                cli,
                "prepare_local_secrets",
                return_value=({"POSTGRES_PASSWORD": "fixture-password"}, ["fixture warning"]),
            )
        )
        self.model_manager = self.stack.enter_context(patch.object(cli, "_model_manager", return_value=self.manager))
        self.generated = {
            "OPENAI_BASE_URL": "http://fixture/v1",
            "MAIN_MODEL": "fixture-main",
        }
        self.build_environment = self.stack.enter_context(
            patch.object(cli, "build_generated_environment", return_value=self.generated)
        )
        self.write_configuration = self.stack.enter_context(
            patch.object(cli, "_write_configuration", return_value=self.generated)
        )
        self.compose_manager = self.stack.enter_context(
            patch.object(cli, "ComposeManager", return_value=self.compose)
        )
        self.salesforce = self.stack.enter_context(
            patch.object(cli, "has_salesforce_credentials", return_value=False)
        )
        self.start_compose = self.stack.enter_context(
            patch.object(cli, "_start_compose", return_value={"status": "running"})
        )
        self.atomic_json = self.stack.enter_context(patch.object(cli, "atomic_write_json"))
        self.atomic_text = self.stack.enter_context(patch.object(cli, "atomic_write_text"))
        self.print_selection = self.stack.enter_context(patch.object(cli, "_print_selection"))
        self.capability_prober = self.stack.enter_context(patch.object(cli, "CapabilityProber"))

    @staticmethod
    def args(**overrides):
        values = {
            "dry_run": False,
            "profile": None,
            "model": None,
            "skip_ocr": False,
            "offline": False,
            "verbose": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_up_passes_manual_overrides_and_follows_staged_startup_order(self) -> None:
        events: list[str] = []
        self.layout.create.side_effect = lambda: events.append("layout")
        self.detect.side_effect = lambda root, **kwargs: events.append("detect") or self.hardware
        self.select.side_effect = lambda *a, **k: events.append("select") or self.profile
        self.manager.ensure_all.side_effect = lambda *a, **k: events.append("models") or self.installs
        self.write_configuration.side_effect = lambda *a, **k: events.append("configuration") or self.generated
        self.compose.validate.side_effect = lambda: events.append("validate")
        self.start_compose.side_effect = lambda *a, **k: events.append("start") or {"status": "running"}
        self.atomic_json.side_effect = lambda *a, **k: events.append("state")
        args = self.args(
            profile="local-minimal",
            model="Qwen/Qwen3-0.6B-GGUF",
            skip_ocr=True,
            offline=True,
        )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._cmd_up(args, root=self.root), 0)

        self.assertEqual(
            events,
            ["layout", "detect", "select", "models", "configuration", "validate", "start", "state"],
        )
        self.select.assert_called_once_with(
            self.hardware,
            self.root,
            profile_override="local-minimal",
            model_override="Qwen/Qwen3-0.6B-GGUF",
            skip_ocr=True,
            reuse_running_models=False,
        )
        self.manager.ensure_all.assert_called_once_with(
            self.profile.required_models(skip_ocr=True),
            offline=True,
            dry_run=False,
            reporter=cli._step,
        )
        self.write_configuration.assert_called_once_with(
            self.layout,
            self.hardware,
            self.profile,
            self.installs,
            skip_ocr=True,
            search_enabled=True,
            search_provider="searxng",
            allow_planned=False,
            user_environment=self.user_env,
        )
        self.start_compose.assert_called_once_with(
            self.compose,
            self.profile,
            self.generated,
            salesforce_ready=False,
            search_enabled=True,
            dry_run=False,
            endpoints={
                "orchestrator": "http://127.0.0.1:8080",
                "frontend": "http://127.0.0.1:3000",
            },
        )
        state = self.atomic_json.call_args.args[1]
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["profile"], self.profile.id)
        self.assertEqual(state["compose_command"], ["docker", "compose", "up", "-d"])

    def test_up_dry_run_has_no_persistent_or_lifecycle_mutations(self) -> None:
        args = self.args(dry_run=True, offline=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._cmd_up(args, root=self.root), 0)

        called_persistent_write_boundaries = [
            name
            for name, mock in (
                ("RuntimeLayout.create", self.layout.create),
                ("prepare_local_secrets", self.prepare_secrets),
                ("_write_configuration", self.write_configuration),
                ("atomic_write_json(state)", self.atomic_json),
            )
            if mock.called
        ]
        self.assertEqual(called_persistent_write_boundaries, [])
        self.manager.ensure_all.assert_called_once_with(
            self.profile.required_models(skip_ocr=False),
            offline=True,
            dry_run=True,
        )
        planned_layout = self.build_environment.call_args.args[0]
        self.assertNotEqual(planned_layout.runtime_dir, self.layout.runtime_dir)
        self.assertFalse(planned_layout.runtime_dir.is_relative_to(self.root))
        self.build_environment.assert_called_once_with(
            planned_layout,
            self.profile,
            self.installs,
            cache_root=Path(self.hardware.selected_cache_path),
            skip_ocr=False,
            search_enabled=True,
            search_provider="searxng",
            allow_planned=True,
            user_environment=self.user_env,
        )
        self.assertEqual(
            [(item.args[0], item.kwargs["mode"]) for item in self.atomic_text.call_args_list],
            [(planned_layout.generated_env, 0o644), (planned_layout.secrets_env, 0o600)],
        )
        self.start_compose.assert_called_once_with(
            self.compose,
            self.profile,
            self.generated,
            salesforce_ready=False,
            search_enabled=True,
            dry_run=True,
            endpoints={
                "orchestrator": "http://127.0.0.1:8080",
                "frontend": "http://127.0.0.1:3000",
            },
        )
        self.compose.build.assert_not_called()
        self.compose.up_service.assert_not_called()
        self.compose.wait_service.assert_not_called()

    def test_native_up_ensures_runtime_then_records_capability_results(self) -> None:
        self.hardware = replace(mac(64), selected_cache_path=str(self.root / "models"))
        self.profile = selected(self.hardware)
        self.installs = installs_for(self.profile, self.root / "models")
        self.detect.return_value = self.hardware
        self.select.return_value = self.profile
        self.manager.ensure_all.return_value = self.installs
        runtime = RuntimeInstall("installed", str(self.root / "runtime"), "0.10.2", "3.12")
        runtime_manager = Mock()
        runtime_manager.ensure.return_value = runtime
        runtime_class = self.stack.enter_context(
            patch.object(cli, "RuntimeManager", return_value=runtime_manager)
        )
        capability_results = [{"name": "main", "status": "ok"}]
        docker_capability_results = [
            {"name": "docker-main", "chat": {"supported": True}}
        ]
        self.start_compose.return_value = {
            "status": "running",
            "capability_results": docker_capability_results,
        }
        start_native = self.stack.enter_context(
            patch.object(
                cli,
                "_start_native_models",
                return_value=(self.profile, capability_results),
            )
        )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._cmd_up(self.args(offline=True), root=self.root), 0)

        runtime_class.assert_called_once_with(
            self.root,
            self.layout.runtime_dir,
            self.layout.shared_root / "runtimes",
            {},
            uv_path=self.layout.shared_root / "bin" / "uv",
        )
        runtime_manager.ensure.assert_called_once_with(self.hardware, offline=True, dry_run=False)
        start_native.assert_called_once_with(
            self.layout,
            self.hardware,
            self.profile,
            self.installs,
            runtime,
            {"POSTGRES_PASSWORD": "fixture-password"},
            dry_run=False,
        )
        self.capability_prober.return_value.write_results.assert_called_once_with(
            self.layout.capabilities_file,
            [*capability_results, *docker_capability_results],
        )
        state = self.atomic_json.call_args.args[1]
        self.assertEqual(state["runtime"], runtime.to_dict())

    def test_up_persists_real_startup_degradation_and_retry_outcomes(self) -> None:
        models, _ = load_model_manifest(REPO_ROOT)
        self.hardware = replace(nvidia(80), selected_cache_path=str(self.root / "models"))
        self.profile = replace(
            selected(self.hardware),
            router_model=models["nvidia-qwen3-14b-awq"],
            router_shared=False,
        )
        self.installs = installs_for(self.profile, self.root / "models")
        self.detect.return_value = self.hardware
        self.select.return_value = self.profile
        self.manager.ensure_all.return_value = self.installs
        self.start_compose.return_value = {
            "status": "running",
            "orchestrator": {"status": "degraded", "checks": {}},
            "disabled_features": ["embeddings"],
            "router_fallback": True,
            "startup_retry_context": self.profile.startup_retry_context,
        }

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._cmd_up(self.args(), root=self.root), 0)

        self.assertEqual(len(self.atomic_json.call_args_list), 2)
        self.assertEqual(self.atomic_json.call_args_list[0].args[0], self.layout.profile_file)
        persisted = SelectedProfile.from_dict(self.atomic_json.call_args_list[0].args[1])
        self.assertFalse(persisted.features["embeddings"])
        self.assertTrue(persisted.router_shared)
        self.assertEqual(persisted.router_model, persisted.main_model)
        self.assertEqual(persisted.context_length, self.profile.startup_retry_context)
        self.assertEqual(persisted.concurrency, 1)
        reasons = " ".join(persisted.degraded_reasons)
        self.assertIn("embeddings disabled", reasons)
        self.assertIn("router failed", reasons)
        self.assertIn("safer startup retry", reasons)
        state = self.atomic_json.call_args_list[1].args[1]
        self.assertEqual(state["result"], self.start_compose.return_value)


class NativeStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = RuntimeLayout.for_project(self.root)
        self.profile = selected(mac(96))
        self.installs = installs_for(self.profile, self.root / "models")
        self.runtime = RuntimeInstall("installed", str(self.root / "runtime"), "0.10.2", "3.12")

    def test_native_component_dry_run_builds_exact_argv_without_probing_or_spawning(self) -> None:
        model = self.profile.main_model
        self.assertIsNotNone(model)
        install = next(item for item in self.installs if item.model_id == model.id)
        process = Mock()
        prober = Mock()
        with patch.object(cli, "_wait_native_models") as wait:
            result = cli._start_native_component(
                self.layout,
                process,
                prober,
                Path(self.runtime.path),
                model,
                install,
                name="main",
                direct_port=18000,
                bridge_port=18100,
                context=self.profile.context_length,
                concurrency=self.profile.concurrency,
                api_key="fixture-key",
                runtime_version=self.runtime.version,
                dry_run=True,
            )
        executable = str(Path(self.runtime.path) / "bin" / "vllm")
        expected_server = [
            executable,
            "serve",
            install.path,
            "--served-model-name",
            model.api_model_id,
            "--host",
            "127.0.0.1",
            "--port",
            "18000",
            "--max-model-len",
            str(min(self.profile.context_length, model.context_limit)),
            "--max-num-seqs",
            str(self.profile.concurrency),
            *model.startup_arguments,
        ]
        self.assertEqual(process.start.call_args_list[0].args[:2], ("main-model", expected_server))
        self.assertEqual(
            process.start.call_args_list[0].kwargs["env"],
            {
                "HF_HOME": str(self.layout.shared_root / "model-cache" / "huggingface"),
                "HF_HUB_OFFLINE": "1",
            },
        )
        bridge_call = process.start.call_args_list[1]
        self.assertEqual(bridge_call.args[0], "main-bridge")
        self.assertEqual(
            bridge_call.args[1],
            [
                sys.executable,
                "-m",
                "techsara_cli.bridge",
                "--listen-host",
                "0.0.0.0",
                "--listen-port",
                "18100",
                "--target",
                "http://127.0.0.1:18000",
            ],
        )
        self.assertTrue(all(item.kwargs["dry_run"] for item in process.start.call_args_list))
        wait.assert_not_called()
        prober.probe.assert_not_called()
        self.assertEqual(result, {"name": "main", "status": "planned", "model_id": model.api_model_id})

    def test_native_component_waits_for_direct_and_authenticated_bridge_health(self) -> None:
        model = self.profile.main_model
        install = next(item for item in self.installs if item.model_id == model.id)
        process = Mock()
        prober = Mock()
        prober.probe.return_value = {"name": "main", "chat": {"supported": True}}
        with patch.object(cli, "_wait_native_models") as wait:
            result = cli._start_native_component(
                self.layout,
                process,
                prober,
                Path(self.runtime.path),
                model,
                install,
                name="main",
                direct_port=18000,
                bridge_port=18100,
                context=999999,
                concurrency=2,
                api_key="fixture-key",
                runtime_version=self.runtime.version,
                dry_run=False,
            )
        self.assertEqual(
            wait.call_args_list,
            [
                call(prober, "http://127.0.0.1:18000"),
                call(prober, "http://127.0.0.1:18100", api_key="fixture-key", timeout=30.0),
            ],
        )
        prober.probe.assert_called_once_with(
            name="main",
            base_url="http://127.0.0.1:18000",
            model=model,
            selected_context=model.context_limit,
        )
        self.assertEqual(
            process.mark_health.call_args_list,
            [call("main-model", "healthy"), call("main-bridge", "healthy")],
        )
        self.assertEqual(result, prober.probe.return_value)

    def test_native_component_resumes_exact_interrupted_model_and_bridge(self) -> None:
        model = self.profile.main_model
        install = next(item for item in self.installs if item.model_id == model.id)
        process = Mock()
        process.is_resumable_start.side_effect = [True, True]
        prober = Mock()
        prober.probe.return_value = {"name": "main", "chat": {"supported": True}}
        with patch.object(cli, "_wait_native_models") as wait:
            result = cli._start_native_component(
                self.layout,
                process,
                prober,
                Path(self.runtime.path),
                model,
                install,
                name="main",
                direct_port=18000,
                bridge_port=18100,
                context=self.profile.context_length,
                concurrency=self.profile.concurrency,
                api_key="fixture-key",
                runtime_version=self.runtime.version,
                dry_run=False,
            )
        process.start.assert_not_called()
        self.assertEqual(process.is_resumable_start.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(result, prober.probe.return_value)

    def test_optional_native_failure_stops_only_that_pair_and_degrades_feature(self) -> None:
        process = Mock()
        prober = Mock()

        def start(*args, **kwargs):
            if kwargs["name"] == "embedding":
                raise TechSaraError("embedding probe failed")
            return {"name": kwargs["name"], "status": "ok"}

        with (
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(cli, "CapabilityProber", return_value=prober),
            patch.object(cli, "_start_native_component", side_effect=start) as component,
        ):
            updated, results = cli._start_native_models(
                self.layout,
                mac(96),
                self.profile,
                self.installs,
                self.runtime,
                {"TECHSARA_MODEL_API_KEY": "fixture-key"},
                dry_run=False,
            )
        self.assertFalse(updated.features["embeddings"])
        self.assertTrue(updated.features["reranker"])
        self.assertTrue(any("embedding unavailable" in reason for reason in updated.degraded_reasons))
        self.assertEqual(
            process.stop.call_args_list,
            [call("embedding-bridge"), call("embedding-model")],
        )
        self.assertEqual([item["name"] for item in results], ["reranker", "main"])
        self.assertEqual([item.kwargs["name"] for item in component.call_args_list], ["embedding", "reranker", "main"])

    def test_main_native_failure_gets_exactly_one_safer_retry(self) -> None:
        profile = replace(
            self.profile,
            embedding_model=None,
            reranker_model=None,
            features=dict(self.profile.features, embeddings=False, reranker=False),
        )
        installs = installs_for(profile, self.root / "models")
        process = Mock()
        with (
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(cli, "CapabilityProber", return_value=Mock()),
            patch.object(
                cli,
                "_start_native_component",
                side_effect=[TechSaraError("first start failed"), {"name": "main", "status": "ok"}],
            ) as component,
        ):
            updated, results = cli._start_native_models(
                self.layout,
                mac(96),
                profile,
                installs,
                self.runtime,
                {},
                dry_run=False,
            )
        self.assertEqual(len(component.call_args_list), 2)
        self.assertEqual(component.call_args_list[0].kwargs["context"], profile.context_length)
        self.assertEqual(component.call_args_list[0].kwargs["concurrency"], profile.concurrency)
        self.assertEqual(component.call_args_list[1].kwargs["context"], profile.startup_retry_context)
        self.assertEqual(component.call_args_list[1].kwargs["concurrency"], 1)
        self.assertEqual(process.stop.call_args_list, [call("main-bridge"), call("main-model")])
        self.assertEqual(updated.context_length, profile.startup_retry_context)
        self.assertEqual(updated.concurrency, 1)
        self.assertIn("single safer startup retry", updated.degraded_reasons[-1])
        self.assertEqual(results, [{"name": "main", "status": "ok"}])

    def test_dry_run_never_retries_a_failed_native_start(self) -> None:
        profile = replace(
            self.profile,
            embedding_model=None,
            reranker_model=None,
            features=dict(self.profile.features, embeddings=False, reranker=False),
        )
        with (
            patch.object(cli, "ProcessManager") as process,
            patch.object(cli, "CapabilityProber"),
            patch.object(cli, "_start_native_component", side_effect=TechSaraError("planned failure")) as component,
        ):
            with self.assertRaisesRegex(TechSaraError, "planned failure"):
                cli._start_native_models(
                    self.layout,
                    mac(96),
                    profile,
                    installs_for(profile, self.root / "models"),
                    self.runtime,
                    {},
                    dry_run=True,
                )
        component.assert_called_once()
        process.return_value.stop.assert_not_called()


class ComposeStartupTests(unittest.TestCase):
    def test_dry_run_has_no_compose_or_health_calls(self) -> None:
        compose = Mock()
        compose.profiles = ("admin",)
        with patch.object(cli, "_probe_orchestrator") as health:
            result = cli._start_compose(
                compose,
                selected(nvidia(80)),
                {},
                salesforce_ready=True,
                search_enabled=True,
                dry_run=True,
            )
        self.assertEqual(result, {"status": "planned"})
        self.assertEqual(compose.mock_calls, [])
        health.assert_not_called()

    def test_nvidia_services_start_in_dependency_and_health_order(self) -> None:
        models, _ = load_model_manifest(REPO_ROOT)
        profile = selected(nvidia(80))
        profile = replace(
            profile,
            router_model=models["nvidia-qwen3-14b-awq"],
            router_shared=False,
            ocr_model=models["unlimited-ocr"],
            features=dict(profile.features, ocr=True),
        )
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "ROUTER_BASE_URL": "http://router/v1",
            "ROUTER_MODEL": "router",
            "OCR_BASE_URL": "http://ocr/v1",
            "OCR_MODEL": "ocr",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
            "VISION_ENABLED": "true",
        }
        compose = Mock()
        compose.profiles = ("admin",)
        health_result = {"status": "healthy", "checks": {"app_db": {"status": "ok"}}}
        with patch.object(cli, "_probe_orchestrator", return_value=health_result) as health:
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=True,
                search_enabled=True,
                dry_run=False,
            )
        self.assertEqual(
            compose.method_calls,
            [
                call.build(),
                call.up_service("postgres"),
                call.wait_service("postgres", timeout=180.0, reporter=cli._step),
                call.up_service("searxng"),
                call.wait_service("searxng", timeout=180.0, reporter=cli._step),
                call.up_service("vllm-embed"),
                call.wait_service("vllm-embed", timeout=1800.0, reporter=cli._step),
                call.probe_internal_model("http://embed/v1", "embed", kind="embedding"),
                call.up_service("vllm-router"),
                call.wait_service("vllm-router", timeout=1800.0, reporter=cli._step),
                call.probe_internal_model("http://router/v1", "router"),
                call.up_service("vllm-ocr"),
                call.wait_service("vllm-ocr", timeout=1800.0, reporter=cli._step),
                call.probe_internal_model("http://ocr/v1", "ocr", kind="ocr"),
                call.up_service("vllm"),
                call.wait_service("vllm", timeout=2400.0, reporter=cli._step),
                call.probe_internal_model("http://main/v1", "main"),
                call.probe_internal_model("http://main/v1", "main", kind="vision"),
                call.up_service("orchestrator"),
                call.wait_service("orchestrator", timeout=300.0, reporter=cli._step),
                call.up_service("sync-worker"),
                call.wait_service("sync-worker", timeout=60.0, require_health=False, reporter=cli._step),
                call.up_service("frontend"),
                call.wait_service("frontend", timeout=240.0, reporter=cli._step),
                call.up_service("pgadmin"),
                call.wait_service("pgadmin", timeout=180.0, require_health=False, reporter=cli._step),
            ],
        )
        health.assert_called_once_with("http://127.0.0.1:8080/health")
        capabilities = result.pop("capability_results")
        self.assertEqual(
            result,
            {
                "status": "running",
                "orchestrator": health_result,
                "disabled_features": [],
                "router_fallback": False,
                "startup_retry_context": 0,
            },
        )
        by_name = {item["name"]: item for item in capabilities}
        self.assertTrue(by_name["docker-embedding"]["embeddings"]["supported"])
        self.assertTrue(by_name["docker-router"]["chat"]["supported"])
        self.assertTrue(by_name["docker-ocr"]["ocr"]["supported"])
        self.assertTrue(by_name["docker-main"]["chat"]["supported"])
        self.assertTrue(by_name["docker-main"]["vision"]["supported"])

    def test_cpu_starts_only_cpu_backend_before_application(self) -> None:
        compose = Mock()
        compose.profiles = ()
        generated = {"OPENAI_BASE_URL": "http://llama/v1", "MAIN_MODEL": "cpu-main"}
        with patch.object(
            cli,
            "_probe_orchestrator",
            return_value={"status": "ok", "checks": {}},
        ):
            cli._start_compose(
                compose,
                selected(cpu()),
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )
        self.assertEqual(
            compose.method_calls,
            [
                call.build(),
                call.up_service("postgres"),
                call.wait_service("postgres", timeout=180.0, reporter=cli._step),
                call.up_service("llama-cpp"),
                call.wait_service("llama-cpp", timeout=900.0, reporter=cli._step),
                call.probe_internal_model("http://llama/v1", "cpu-main"),
                call.up_service("orchestrator"),
                call.wait_service("orchestrator", timeout=300.0, reporter=cli._step),
                call.up_service("frontend"),
                call.wait_service("frontend", timeout=240.0, reporter=cli._step),
            ],
        )

    def test_mac_requires_real_container_to_host_probe_before_application(self) -> None:
        compose = Mock()
        compose.profiles = ()
        generated = {"OPENAI_BASE_URL": "http://host.docker.internal:18100/v1", "MAIN_MODEL": "mac-main"}
        with patch.object(
            cli,
            "_probe_orchestrator",
            return_value={"status": "ok", "checks": {}},
        ):
            cli._start_compose(
                compose,
                selected(mac(64)),
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )
        self.assertIn(
            call.probe_internal_model("http://host.docker.internal:18100/v1", "mac-main"),
            compose.method_calls,
        )
        probe_index = compose.method_calls.index(
            call.probe_internal_model("http://host.docker.internal:18100/v1", "mac-main")
        )
        orchestrator_index = compose.method_calls.index(call.up_service("orchestrator"))
        self.assertLess(probe_index, orchestrator_index)
        self.assertNotIn(call.up_service("vllm"), compose.method_calls)

    def test_ocr_failure_stops_and_disables_ocr_but_keeps_main_and_application(self) -> None:
        models, _ = load_model_manifest(REPO_ROOT)
        profile = selected(nvidia(80))
        profile = replace(
            profile,
            ocr_model=models["unlimited-ocr"],
            features=dict(profile.features, ocr=True),
        )
        compose = Mock()
        compose.profiles = ()
        compose.generated_env = Path("/fixture/generated.env")
        compose.probe_internal_model.side_effect = [
            None,
            TechSaraError("ocr unhealthy"),
            None,
        ]
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "OCR_BASE_URL": "http://ocr/v1",
            "OCR_MODEL": "ocr",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
        }
        health_result = {"status": "degraded", "checks": {"app_db": {"status": "ok"}}}
        with (
            patch.object(cli, "_probe_orchestrator", return_value=health_result) as health,
            patch.object(cli, "atomic_write_text") as publish,
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )
        compose.stop_service.assert_called_once_with("vllm-ocr")
        self.assertIn(
            call.probe_internal_model("http://ocr/v1", "ocr", kind="ocr"),
            compose.method_calls,
        )
        self.assertIn(call.up_service("vllm"), compose.method_calls)
        self.assertIn(call.up_service("orchestrator"), compose.method_calls)
        self.assertIn(call.up_service("frontend"), compose.method_calls)
        health.assert_called_once_with("http://127.0.0.1:8080/health")
        self.assertEqual(generated["OCR_ENABLED"], "false")
        self.assertEqual(generated["OCR_BASE_URL"], "http://disabled.invalid/v1")
        self.assertEqual(generated["OCR_MODEL"], "disabled")
        publish.assert_called_once_with(
            compose.generated_env,
            cli.render_env(generated),
            mode=0o644,
        )
        self.assertEqual(result["disabled_features"], ["ocr"])
        self.assertFalse(result["router_fallback"])
        self.assertEqual(result["startup_retry_context"], 0)
        ocr_result = next(
            item for item in result["capability_results"] if item["name"] == "docker-ocr"
        )
        self.assertFalse(ocr_result["ocr"]["supported"])

    def test_main_vision_failure_disables_only_vision_and_keeps_application(self) -> None:
        profile = selected(nvidia(80))
        compose = Mock()
        compose.profiles = ()
        compose.generated_env = Path("/fixture/generated.env")
        compose.probe_internal_model.side_effect = [
            {"kind": "embedding", "supported": True},
            {"kind": "chat", "supported": True},
            TechSaraError("vision contract failed"),
        ]
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
            "VISION_ENABLED": "true",
            "VISION_BASE_URL": "http://main/v1",
            "VISION_MODEL": "main",
        }
        with (
            patch.object(
                cli,
                "_probe_orchestrator",
                return_value={"status": "degraded", "checks": {"app_db": {"status": "ok"}}},
            ),
            patch.object(cli, "atomic_write_text") as publish,
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )

        self.assertEqual(result["disabled_features"], ["vision"])
        self.assertEqual(generated["VISION_ENABLED"], "false")
        self.assertEqual(generated["VISION_MODEL"], "disabled")
        self.assertIn(call.up_service("orchestrator"), compose.method_calls)
        self.assertIn(call.up_service("frontend"), compose.method_calls)
        publish.assert_called_once()
        main_result = next(
            item for item in result["capability_results"] if item["name"] == "docker-main"
        )
        self.assertTrue(main_result["chat"]["supported"])
        self.assertFalse(main_result["vision"]["supported"])

    def test_external_main_and_optional_embedding_use_container_probes(self) -> None:
        profile = replace(
            selected(cpu()),
            id="external-development",
            hardware_profile_id="external-development",
            family="external",
            runtime_backend="external-openai-compatible",
        )
        compose = Mock()
        compose.profiles = ()
        compose.probe_internal_model.side_effect = [
            {"kind": "chat", "supported": True},
            {"kind": "embedding", "supported": True},
        ]
        generated = {
            "OPENAI_BASE_URL": "http://host.docker.internal:19000/v1",
            "MAIN_MODEL": "external-main",
            "EMBED_ENABLED": "true",
            "EMBED_BASE_URL": "http://host.docker.internal:19003/v1",
            "EMBED_MODEL": "external-embed",
        }
        with patch.object(
            cli,
            "_probe_orchestrator",
            return_value={"status": "ok", "checks": {"app_db": {"status": "ok"}}},
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )

        main_probe = call.probe_internal_model(
            "http://host.docker.internal:19000/v1", "external-main"
        )
        embed_probe = call.probe_internal_model(
            "http://host.docker.internal:19003/v1",
            "external-embed",
            kind="embedding",
        )
        self.assertIn(main_probe, compose.method_calls)
        self.assertIn(embed_probe, compose.method_calls)
        self.assertLess(compose.method_calls.index(main_probe), compose.method_calls.index(embed_probe))
        self.assertLess(
            compose.method_calls.index(embed_probe),
            compose.method_calls.index(call.up_service("orchestrator")),
        )
        by_name = {item["name"]: item for item in result["capability_results"]}
        self.assertTrue(by_name["docker-main"]["chat"]["supported"])
        self.assertTrue(by_name["docker-embedding"]["embeddings"]["supported"])

    def test_router_failure_falls_back_to_healthy_main_and_republishes_routes(self) -> None:
        models, _ = load_model_manifest(REPO_ROOT)
        profile = replace(
            selected(nvidia(80)),
            router_model=models["nvidia-qwen3-14b-awq"],
            router_shared=False,
        )
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "ROUTER_BASE_URL": "http://router/v1",
            "ROUTER_MODEL": "router",
            "AGENT_BASE_URL": "http://router/v1",
            "AGENT_MODEL": "router",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
            "MAIN_CONTEXT_LENGTH": "32768",
            "MAIN_SUPPORTS_CHAT": "true",
        }
        compose = Mock()
        compose.profiles = ()
        compose.generated_env = Path("/fixture/generated.env")
        compose.probe_internal_model.side_effect = [
            None,
            TechSaraError("router unhealthy"),
            None,
        ]
        with (
            patch.object(
                cli,
                "_probe_orchestrator",
                return_value={"status": "ok", "checks": {}},
            ),
            patch.object(cli, "atomic_write_text") as publish,
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )
        compose.stop_service.assert_called_once_with("vllm-router")
        self.assertTrue(result["router_fallback"])
        self.assertEqual(generated["ROUTER_BASE_URL"], "http://main/v1")
        self.assertEqual(generated["ROUTER_MODEL"], "main")
        self.assertEqual(generated["AGENT_BASE_URL"], "http://main/v1")
        self.assertEqual(generated["AGENT_MODEL"], "main")
        self.assertEqual(generated["ROUTER_CONTEXT_LENGTH"], "32768")
        self.assertEqual(generated["AGENT_SUPPORTS_CHAT"], "true")
        publish.assert_called_once()

    def test_main_failure_gets_one_forced_recreate_at_retry_context(self) -> None:
        profile = selected(nvidia(80))
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
            "MODEL_MAX_CONTEXT": str(profile.context_length),
            "DEFAULT_MAX_CONTEXT": str(profile.context_length),
            "REPORT_MAX_CONTEXT": str(profile.context_length),
            "MAIN_CONTEXT_LENGTH": str(profile.context_length),
            "MODEL_CONCURRENCY": str(profile.concurrency),
            "MAIN_CONCURRENCY": str(profile.concurrency),
        }
        compose = Mock()
        compose.profiles = ()
        compose.generated_env = Path("/fixture/generated.env")
        compose.probe_internal_model.side_effect = [
            None,
            TechSaraError("main unhealthy"),
            None,
        ]
        with (
            patch.object(
                cli,
                "_probe_orchestrator",
                return_value={"status": "ok", "checks": {}},
            ),
            patch.object(cli, "atomic_write_text") as publish,
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=False,
                search_enabled=False,
                dry_run=False,
            )
        self.assertEqual(
            [item for item in compose.up_service.call_args_list if item.args == ("vllm",)],
            [call("vllm"), call("vllm", force_recreate=True)],
        )
        compose.validate.assert_called_once_with()
        self.assertEqual(result["startup_retry_context"], profile.startup_retry_context)
        for key in (
            "MODEL_MAX_CONTEXT",
            "DEFAULT_MAX_CONTEXT",
            "REPORT_MAX_CONTEXT",
            "MAIN_CONTEXT_LENGTH",
        ):
            self.assertEqual(generated[key], str(profile.startup_retry_context))
        self.assertEqual(generated["MODEL_CONCURRENCY"], "1")
        self.assertEqual(generated["MAIN_CONCURRENCY"], "1")
        publish.assert_called_once()

    def test_embedding_failure_preserves_salesforce_sync_and_keeps_main_and_frontend(self) -> None:
        profile = selected(nvidia(80))
        generated = {
            "EMBED_BASE_URL": "http://embed/v1",
            "EMBED_VIA": "http://embed/v1",
            "EMBED_MODEL": "embed",
            "OPENAI_BASE_URL": "http://main/v1",
            "MAIN_MODEL": "main",
        }
        compose = Mock()
        compose.profiles = ()
        compose.generated_env = Path("/fixture/generated.env")
        compose.probe_internal_model.side_effect = [
            TechSaraError("embedding unhealthy"),
            None,
        ]
        with (
            patch.object(
                cli,
                "_probe_orchestrator",
                return_value={"status": "degraded", "checks": {}},
            ),
            patch.object(cli, "atomic_write_text"),
        ):
            result = cli._start_compose(
                compose,
                profile,
                generated,
                salesforce_ready=True,
                search_enabled=False,
                dry_run=False,
            )
        compose.stop_service.assert_called_once_with("vllm-embed")
        self.assertEqual(result["disabled_features"], ["embeddings"])
        self.assertEqual(generated["EMBED_ENABLED"], "false")
        self.assertEqual(generated["EMBED_MODEL"], "disabled")
        self.assertIn(call.up_service("sync-worker"), compose.method_calls)
        self.assertIn(
            call.wait_service("sync-worker", timeout=60.0, require_health=False, reporter=cli._step),
            compose.method_calls,
        )
        self.assertIn(call.up_service("frontend"), compose.method_calls)


class HealthContractTests(unittest.TestCase):
    @staticmethod
    def response(payload) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps(payload))
        return response

    def test_orchestrator_health_accepts_healthy_or_degraded_contract(self) -> None:
        for status in ("ok", "healthy", "degraded"):
            payload = {"status": status, "checks": {"app_db": {"status": "ok"}}}
            with self.subTest(status=status), patch.object(
                cli.urllib.request,
                "urlopen",
                return_value=self.response(payload),
            ) as urlopen:
                self.assertEqual(cli._probe_orchestrator("http://fixture/health"), payload)
                urlopen.assert_called_once_with("http://fixture/health", timeout=15.0)

    def test_orchestrator_health_rejects_invalid_shape_and_database_failure(self) -> None:
        payloads = [
            {"status": "starting", "checks": {}},
            {"status": "ok", "checks": []},
            {"status": "ok", "checks": {"app_db": {"status": "failed"}}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(
                cli.urllib.request,
                "urlopen",
                return_value=self.response(payload),
            ):
                with self.assertRaises(TechSaraError):
                    cli._probe_orchestrator("http://127.0.0.1:8080/health")

    def test_orchestrator_network_error_is_wrapped_without_raw_detail(self) -> None:
        with patch.object(
            cli.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("Authorization: Bearer fixture-secret"),
        ):
            with self.assertRaises(TechSaraError) as caught:
                cli._probe_orchestrator("http://127.0.0.1:8080/health")
        self.assertIn("URLError", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_native_wait_uses_bounded_polling_and_no_real_sleep(self) -> None:
        prober = Mock()
        prober._request.side_effect = [
            (False, None, "unreachable"),
            (True, {}, ""),
        ]
        with (
            patch.object(cli.time, "monotonic", side_effect=[0.0, 0.1, 1.0]),
            patch.object(cli.time, "sleep") as sleep,
        ):
            cli._wait_native_models(prober, "http://fixture", timeout=5.0)
        self.assertEqual(prober._request.call_count, 2)
        prober._request.assert_called_with("http://fixture", "/v1/models", api_key="")
        sleep.assert_called_once_with(2.0)


class StatusAndDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = RuntimeLayout.for_project(self.root)

    def test_status_reports_models_processes_docker_sync_and_capabilities_read_only(self) -> None:
        hardware = replace(cpu(), selected_cache_path=str(self.root / "models"))
        profile = selected(hardware)
        process = Mock()
        process.status.return_value = [{"service": "main-model", "state": "running"}]

        def load(path, default):
            if path == self.layout.state_file:
                return {"salesforce_sync": "ready"}
            if path is capabilities_file:
                return {"models": [{"name": "main"}, {"name": "embed"}]}
            return default

        capabilities_file = MagicMock()
        capabilities_file.is_file.return_value = True
        layout = replace(self.layout, capabilities_file=capabilities_file)
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
            patch.object(cli, "_load_hardware", return_value=hardware),
            patch.object(cli, "_load_profile", return_value=profile),
            patch.object(cli, "_print_selection") as print_selection,
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(
                cli,
                "run_command",
                return_value=command_result(0, "sf-local-ai-frontend\tUp 1 minute\n"),
            ) as runner,
            patch.object(cli, "load_json", side_effect=load),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_status(argparse.Namespace(), root=self.root), 0)
        print_selection.assert_called_once_with(hardware, profile)
        process.status.assert_called_once_with()  # one listing feeds both the table and endpoint health
        runner.assert_called_once_with(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.project=sf-local-ai",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            timeout=15.0,
        )
        output = stdout.getvalue()
        self.assertIn("main-model: running", output)
        self.assertIn("sf-local-ai-frontend", output)
        self.assertIn("Salesforce sync: ready", output)
        self.assertIn("main (unknown)", output)
        self.assertIn("embed (unknown)", output)
        self.assertIn("Feature capabilities:", output)
        self.assertIn("Degraded components:", output)

    def test_status_handles_unconfigured_and_unavailable_dependencies(self) -> None:
        stdout = io.StringIO()
        process = Mock()
        process.status.return_value = []
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_load_hardware", return_value=None),
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(cli, "run_command", return_value=command_result(127, "", "missing")),
            patch.object(cli, "load_json", return_value={}),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_status(argparse.Namespace(), root=self.root), 0)
        output = stdout.getvalue()
        self.assertIn("not configured", output)
        self.assertIn("Native processes:\n  none", output)
        self.assertIn("Docker services:\n  none/unavailable", output)

    def test_doctor_is_non_mutating_and_fails_only_blocking_prerequisites(self) -> None:
        hardware = replace(cpu(), docker_running=False, free_disk_bytes=GIB)
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "detect_hardware", return_value=hardware),
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "_reachable_host", return_value=True),
            patch.object(cli, "atomic_write_json") as write_json,
            patch.object(cli, "atomic_write_text") as write_text,
            patch.object(cli, "ComposeManager") as compose,
            patch.object(RuntimeLayout, "create") as create,
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_doctor(argparse.Namespace(), root=self.root), 1)
        write_json.assert_not_called()
        write_text.assert_not_called()
        compose.assert_not_called()
        create.assert_not_called()
        self.assertIn("FAIL  Docker daemon", stdout.getvalue())
        self.assertIn("FAIL  Free disk", stdout.getvalue())

    def test_doctor_does_not_cascade_a_denied_socket_into_invented_problems(self) -> None:
        # Every container probe runs *through* the socket, so a denied socket
        # makes them all look failed.  A Linux host with no Docker Desktop was
        # being told to switch Docker Desktop to Linux containers.
        hardware = replace(
            nvidia(24),
            docker_running=False,
            docker_permission_denied=True,
            docker_linux_containers=False,
            docker_gpu_available=False,
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "detect_hardware", return_value=hardware),
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "_reachable_host", return_value=True),
            patch.object(cli, "_docker_group_members", return_value=(False, [])),
            patch.dict(cli.os.environ, {"USER": "fixture"}, clear=True),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_doctor(argparse.Namespace(), root=self.root), 1)
        output = stdout.getvalue()
        self.assertIn("FAIL  Docker socket access", output)
        # Match whole report lines; the skip note legitimately names both checks.
        self.assertNotIn("  Docker daemon\n", output)
        self.assertNotIn("  Linux containers\n", output)
        self.assertNotIn("  Container GPU\n", output)
        self.assertIn("were skipped", output)
        # The one-line rendering must not run two commands together.
        self.assertIn("sudo usermod -aG docker fixture; then: newgrp docker.", output)

    def test_doctor_validates_existing_compose_without_starting_it(self) -> None:
        profile = selected(cpu())
        compose = Mock()
        generated = MagicMock()
        generated.is_file.return_value = True
        secrets = MagicMock()
        secrets.exists.return_value = True
        secrets.stat.return_value.st_mode = 0o100600
        layout = replace(self.layout, generated_env=generated, secrets_env=secrets)
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
            patch.object(cli, "detect_hardware", return_value=cpu()),
            patch.object(cli, "_load_profile", return_value=profile),
            patch.object(cli, "_reachable_host", return_value=True),
            patch.object(cli, "_compose_from_state", return_value=compose),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_doctor(argparse.Namespace(), root=self.root), 0)
        compose.validate.assert_called_once_with()
        compose.build.assert_not_called()
        compose.up_service.assert_not_called()
        compose.down.assert_not_called()
        self.assertIn("PASS  Secret permissions", stdout.getvalue())
        self.assertIn("PASS  Compose configuration", stdout.getvalue())

    def test_doctor_reports_rosetta_and_nvidia_gpu_as_diagnostics(self) -> None:
        cases = [
            (mac(64, rosetta=True), "Native arm64"),
            (replace(nvidia(24), docker_gpu_available=False), "Container GPU"),
        ]
        for hardware, expected in cases:
            with self.subTest(expected=expected):
                stdout = io.StringIO()
                with (
                    patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
                    patch.object(cli, "detect_hardware", return_value=hardware),
                    patch.object(cli, "_load_profile", return_value=None),
                    patch.object(cli, "_reachable_host", return_value=True),
                    redirect_stdout(stdout),
                ):
                    cli._cmd_doctor(argparse.Namespace(), root=self.root)
                self.assertIn(f"FAIL  {expected}", stdout.getvalue())


class PublishedEndpointResolutionTests(unittest.TestCase):
    """Health probes and printed URLs must follow the configured ports."""

    def test_configured_ports_and_bind_address_drive_the_probe_urls(self) -> None:
        endpoints = cli._local_endpoints(
            {"TECHSARA_BIND_ADDRESS": "127.0.0.1"},
            {"ORCHESTRATOR_PORT": "18080", "FRONTEND_PORT": "13000"},
        )
        self.assertEqual(endpoints["orchestrator"], "http://127.0.0.1:18080")
        self.assertEqual(endpoints["frontend"], "http://127.0.0.1:13000")

    def test_defaults_apply_when_the_user_configures_no_ports(self) -> None:
        endpoints = cli._local_endpoints({}, {})
        self.assertEqual(endpoints["orchestrator"], "http://127.0.0.1:8080")
        self.assertEqual(endpoints["frontend"], "http://127.0.0.1:3000")

    def test_a_wildcard_publish_address_is_probed_over_loopback(self) -> None:
        self.assertEqual(cli._local_base_url("0.0.0.0", 8080), "http://127.0.0.1:8080")
        self.assertEqual(cli._local_base_url("::", 8080), "http://127.0.0.1:8080")
        self.assertEqual(cli._local_base_url("::1", 8080), "http://[::1]:8080")

    def test_an_invalid_port_is_a_safe_actionable_error(self) -> None:
        for value in ("not-a-port", "0", "70000", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(TechSaraError):
                    cli._port({"ORCHESTRATOR_PORT": value}, "ORCHESTRATOR_PORT", 8080)


class DoctorCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = RuntimeLayout.for_project(self.root)

    def _run(self, hardware, **patches) -> str:
        stdout = io.StringIO()
        defaults = {
            "_load_profile": None,
            "_reachable_host": True,
        }
        defaults.update(patches)
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout)
            )
            stack.enter_context(patch.object(cli, "detect_hardware", return_value=hardware))
            for name, value in defaults.items():
                stack.enter_context(patch.object(cli, name, return_value=value))
            stack.enter_context(redirect_stdout(stdout))
            cli._cmd_doctor(argparse.Namespace(offline=False), root=self.root)
        return stdout.getvalue()

    def test_doctor_covers_every_documented_validation_area(self) -> None:
        output = self._run(cpu())
        for expected in (
            "Docker CLI",
            "Docker Compose",
            "Host architecture",
            "System memory",
            "Available memory",
            "Free disk",
            "Model cache writable",
            "Environment file",
            "Network reachability",
        ):
            self.assertIn(expected, output)
        self.assertIn("Salesforce sync:", output)

    def test_offline_doctor_skips_network_checks_and_detection_pulls(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "detect_hardware", return_value=cpu()) as detect,
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "_reachable_host") as reachable,
            redirect_stdout(stdout),
        ):
            cli._cmd_doctor(argparse.Namespace(offline=True), root=self.root)
        reachable.assert_not_called()
        self.assertEqual(detect.call_args.kwargs["allow_network"], False)
        self.assertIn("Network checks skipped", stdout.getvalue())

    def test_an_unreachable_artifact_host_is_reported_without_blocking_the_exit_code(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "detect_hardware", return_value=cpu()),
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "_reachable_host", return_value=False),
            redirect_stdout(stdout),
        ):
            # Runtime configuration is absent in this fixture, which is not a
            # blocking prerequisite either; the command still exits 0.
            self.assertEqual(cli._cmd_doctor(argparse.Namespace(offline=False), root=self.root), 0)
        self.assertIn("FAIL  Network reachability (huggingface.co)", stdout.getvalue())
        self.assertIn("first-run downloads will fail", stdout.getvalue())


class LifecycleAndUtilityCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = RuntimeLayout.for_project(self.root)

    def test_down_stops_only_compose_and_owned_native_process_managers(self) -> None:
        compose = Mock()
        process = Mock()
        process.stop_all.return_value = ["main-bridge", "main-model"]
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_compose_from_state", return_value=compose),
            patch.object(cli, "ProcessManager", return_value=process) as process_class,
            patch.object(cli, "ModelManager") as models,
            patch.object(cli, "RuntimeManager") as runtimes,
            patch.object(cli, "run_command", return_value=command_result(0, "")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_down(argparse.Namespace(dry_run=False), root=self.root), 0)
        compose.down.assert_called_once_with()
        process_class.assert_called_once_with(self.root, self.layout.runtime_dir)
        process.stop_all.assert_called_once_with(dry_run=False)
        models.assert_not_called()
        runtimes.assert_not_called()
        self.assertIn("Models, runtimes, data volumes, reports, and user configuration were preserved", stdout.getvalue())

    def test_down_never_claims_to_have_stopped_a_stack_it_does_not_manage(self) -> None:
        """A no-op `down` must not read as "everything is stopped now".

        Regression: with no recorded state the command printed "Stopped
        TechSara services" while ten containers from the superseded root
        Compose file were still running.
        """
        process = Mock()
        process.stop_all.return_value = []
        listing = (
            "vllm\t/repo/docker-compose.yml\n"
            "frontend\t/repo/docker-compose.yml\n"
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_compose_from_state", return_value=None),
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(cli, "run_command", return_value=command_result(0, listing)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_down(argparse.Namespace(dry_run=False), root=self.root), 0)
        output = stdout.getvalue()
        self.assertNotIn("Stopped TechSara services", output)
        self.assertIn("Nothing to stop", output)
        self.assertIn("Still running, started outside this launcher", output)
        self.assertIn("frontend, vllm", output)
        self.assertIn("docker compose -f", output)
        self.assertIn("/repo/docker-compose.yml", output)

    def test_down_does_not_report_launcher_started_containers_as_unmanaged(self) -> None:
        compose = Mock()
        process = Mock()
        process.stop_all.return_value = []
        listing = f"vllm\t{self.root / 'compose.yaml'},{self.root / 'compose/compose.dgx-spark.yaml'}\n"
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_compose_from_state", return_value=compose),
            patch.object(cli, "ProcessManager", return_value=process),
            patch.object(cli, "run_command", return_value=command_result(0, listing)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_down(argparse.Namespace(dry_run=False), root=self.root), 0)
        compose.down.assert_called_once_with()
        output = stdout.getvalue()
        self.assertIn("Stopped the launcher-managed Compose project", output)
        self.assertNotIn("Still running, started outside this launcher", output)

    def test_down_dry_run_only_displays_compose_argv_and_plans_owned_process_stop(self) -> None:
        compose = Mock()
        compose.display_command.return_value = '["docker", "compose", "down", "--timeout", "120"]'
        process = Mock()
        process.stop_all.return_value = ["main-model"]
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_compose_from_state", return_value=compose),
            patch.object(cli, "ProcessManager", return_value=process),
            # The suite never inspects the real Docker daemon.
            patch.object(cli, "run_command", return_value=command_result(0, "")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_down(argparse.Namespace(dry_run=True), root=self.root), 0)
        compose.display_command.assert_called_once_with("down", "--timeout", "120")
        compose.down.assert_not_called()
        process.stop_all.assert_called_once_with(dry_run=True)
        self.assertIn("Would run:", stdout.getvalue())
        self.assertIn("Would stop the launcher-managed Compose project", stdout.getvalue())

    def test_models_is_status_only_and_uses_saved_selection(self) -> None:
        hardware = replace(cpu(), selected_cache_path=str(self.root / "models"))
        profile = selected(hardware)
        install = installs_for(profile, self.root / "models")[0]
        manager = Mock()
        manager.status.return_value = [install]
        layout = MagicMock(wraps=self.layout)
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
            patch.object(cli, "_load_hardware", return_value=hardware),
            patch.object(cli, "_load_profile", return_value=profile),
            patch.object(cli, "load_model_manifest", return_value=({}, {"huggingface_hub": {}})),
            patch.object(cli, "_model_manager", return_value=manager),
            patch.object(cli, "detect_hardware") as detect,
            redirect_stdout(stdout),
        ):
            self.assertEqual(cli._cmd_models(argparse.Namespace(), root=self.root), 0)
        manager.status.assert_called_once_with(profile.required_models(skip_ocr=False))
        manager.ensure_all.assert_not_called()
        layout.create.assert_not_called()
        detect.assert_not_called()
        self.assertIn(install.model_id, stdout.getvalue())

    def test_update_models_ensures_selected_models_with_offline_and_dry_run_flags(self) -> None:
        hardware = replace(cpu(), selected_cache_path=str(self.root / "models"))
        profile = selected(hardware)
        manager = Mock()
        manager.ensure_all.return_value = installs_for(profile, self.root / "models")
        layout = MagicMock(wraps=self.layout)
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
            patch.object(cli, "_load_hardware", return_value=hardware),
            patch.object(cli, "_load_profile", return_value=profile),
            patch.object(cli, "load_model_manifest", return_value=({}, {"huggingface_hub": {}})),
            patch.object(cli, "_model_manager", return_value=manager),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli._cmd_models(
                    argparse.Namespace(offline=True, dry_run=True),
                    root=self.root,
                    ensure=True,
                ),
                0,
            )
        layout.create.assert_called_once_with()
        manager.ensure_all.assert_called_once_with(
            profile.required_models(skip_ocr=False),
            offline=True,
            dry_run=True,
        )
        manager.status.assert_not_called()

    def test_models_fallback_detects_and_selects_when_saved_state_is_absent(self) -> None:
        hardware = replace(cpu(), selected_cache_path=str(self.root / "models"))
        profile = selected(hardware)
        manager = Mock()
        manager.status.return_value = []
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(cli, "_load_hardware", return_value=None),
            patch.object(cli, "detect_hardware", return_value=hardware) as detect,
            patch.object(cli, "_load_profile", return_value=None),
            patch.object(cli, "docker_project_has_running_models", return_value=True) as running,
            patch.object(cli, "select_profile", return_value=profile) as choose,
            patch.object(cli, "load_model_manifest", return_value=({}, {"huggingface_hub": {}})),
            patch.object(cli, "_model_manager", return_value=manager),
            redirect_stdout(io.StringIO()),
        ):
            cli._cmd_models(argparse.Namespace(), root=self.root)
        detect.assert_called_once_with(self.root)
        running.assert_called_once_with()
        choose.assert_called_once_with(hardware, self.root, reuse_running_models=True)

    def test_redetect_replaces_only_hardware_and_profile_state(self) -> None:
        hardware = replace(nvidia(24), selected_cache_path=str(self.root / "models"))
        profile = selected(hardware)
        layout = MagicMock(wraps=self.layout)
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
            patch.object(cli, "detect_hardware", return_value=hardware),
            patch.object(cli, "docker_project_has_running_models", return_value=False),
            patch.object(cli, "select_profile", return_value=profile) as choose,
            patch.object(cli, "atomic_write_json") as write,
            patch.object(cli, "_print_selection") as display,
        ):
            self.assertEqual(cli._cmd_redetect(argparse.Namespace(), root=self.root), 0)
        layout.create.assert_called_once_with()
        choose.assert_called_once_with(hardware, self.root, reuse_running_models=False)
        self.assertEqual(
            write.call_args_list,
            [
                call(layout.hardware_file, hardware.to_dict()),
                call(layout.profile_file, profile.to_dict()),
            ],
        )
        display.assert_called_once_with(hardware, profile)

    def test_logs_redacts_compose_and_local_log_output_and_honors_tail(self) -> None:
        self.layout.logs_dir.mkdir(parents=True)
        (self.layout.logs_dir / "orchestrator.log").write_text(
            "old line\nrecent supersecret-value\n",
            encoding="utf-8",
        )
        (self.layout.logs_dir / "frontend.log").write_text("unselected\n", encoding="utf-8")
        compose = Mock()
        compose.run.return_value = command_result(
            0,
            "compose supersecret-value\n",
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.RuntimeLayout, "for_project", return_value=self.layout),
            patch.object(
                cli,
                "effective_user_environment",
                return_value={"OPENAI_API_KEY": "supersecret-value"},
            ),
            patch.object(cli, "_compose_from_state", return_value=compose),
            redirect_stdout(stdout),
        ):
            self.assertEqual(
                cli._cmd_logs(
                    argparse.Namespace(tail=1, service="orchestrator"),
                    root=self.root,
                ),
                0,
            )
        compose.run.assert_called_once_with(
            "logs",
            "--no-color",
            "--tail",
            "1",
            "orchestrator",
            timeout=120.0,
        )
        output = stdout.getvalue()
        self.assertNotIn("supersecret-value", output)
        self.assertIn("[REDACTED]", output)
        self.assertIn("[orchestrator]", output)
        self.assertNotIn("old line", output)
        self.assertNotIn("unselected", output)


if __name__ == "__main__":
    unittest.main()
