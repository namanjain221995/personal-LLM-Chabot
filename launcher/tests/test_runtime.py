from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

try:
    from .support import GIB, REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB, REPO_ROOT

from techsara_cli.errors import OfflineError, PrerequisiteError, TechSaraError
from techsara_cli.hardware import HardwareInfo
from techsara_cli.profiles import ModelSpec, load_model_manifest
from techsara_cli.runtime import (
    CAPABILITY_SCHEMA,
    PROCESS_STATE_SCHEMA,
    RUNTIME_STATE_SCHEMA,
    CapabilityProber,
    ProcessManager,
    ProcessRecord,
    RuntimeInstall,
    RuntimeManager,
    _command_fingerprint,
    _pid_alive,
)
from techsara_cli.utils import atomic_write_json


RUNTIME_SPEC = {
    "version": "1.2.3-fixture",
    "commit": "a" * 40,
    "python": "3.12",
    "vllm_version": "9.8.7-fixture",
    "wheel_url": "https://fixture.invalid/vllm_metal-fixture-cp312-arm64.whl",
    "wheel_sha256": "b" * 64,
    "vllm_source_url": "https://fixture.invalid/vllm-fixture.tar.gz",
    "vllm_source_sha256": "c" * 64,
}


def apple_hardware(*, rosetta: bool = False) -> HardwareInfo:
    return HardwareInfo(
        operating_system="darwin",
        host_architecture="amd64" if rosetta else "arm64",
        native_architecture="arm64",
        total_system_memory_bytes=64 * GIB,
        available_system_memory_bytes=48 * GIB,
        apple_silicon=True,
        apple_chip_name="Apple M5 Max",
        apple_unified_memory_bytes=64 * GIB,
        running_under_rosetta=rosetta,
    )


def capability_model(**overrides) -> ModelSpec:
    models, _runtimes = load_model_manifest(REPO_ROOT)
    base = next(iter(models.values()))
    values = {
        "id": "fixture/Capability-Model",
        "served_id": "fixture-capability",
        "revision": "d" * 40,
        "backend": "fixture-backend",
        "context_limit": 8192,
        "tested_context": 4096,
        "supports_chat": True,
        "supports_reasoning": True,
        "supports_vision": False,
        "supports_audio": False,
        "supports_tool_calling": True,
        "supports_structured_output": True,
        "supports_embeddings": False,
        "supports_reranking": False,
        "supports_ocr": False,
    }
    values.update(overrides)
    return replace(base, **values)


class RuntimeInstallValueTests(unittest.TestCase):
    def test_only_installed_runtime_is_ready(self) -> None:
        for status, expected in (("installed", True), ("missing", False), ("invalid", False), ("planned", False)):
            with self.subTest(status=status):
                value = RuntimeInstall(status, "/fixture", "1", "3.12")
                self.assertEqual(value.ready, expected)
                self.assertEqual(value.to_dict()["status"], status)


class RuntimeManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.runtime_dir = self.root / "project-runtime"
        self.shared = self.root / "shared" / "runtimes"
        self.project.mkdir()

    def manager(self, runner=None) -> RuntimeManager:
        return RuntimeManager(
            self.project,
            self.runtime_dir,
            self.shared,
            RUNTIME_SPEC,
            uv_path=self.root / "bin" / "uv",
            runner=runner or Mock(side_effect=AssertionError("runtime command must not execute")),
        )

    def write_install(self, manager: RuntimeManager, *, marker_updates=None, python: bool = True) -> Path:
        root = manager.install_path
        root.mkdir(parents=True)
        if python:
            interpreter = manager._python_path(root)
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("fixture", encoding="utf-8")
        marker = {
            "schema_version": RUNTIME_STATE_SCHEMA,
            "runtime": "vllm-metal",
            "version": RUNTIME_SPEC["version"],
            "commit": RUNTIME_SPEC["commit"],
            "wheel_sha256": RUNTIME_SPEC["wheel_sha256"],
            "vllm_source_sha256": RUNTIME_SPEC["vllm_source_sha256"],
            "validation_status": "complete",
        }
        marker.update(marker_updates or {})
        atomic_write_json(root / "runtime.json", marker)
        return root


class RuntimeInspectionTests(RuntimeManagerCase):
    def test_missing_runtime_does_not_invoke_any_command(self) -> None:
        runner = Mock(side_effect=AssertionError("missing inspection must not run commands"))
        manager = self.manager(runner)
        result = manager.inspect()
        self.assertEqual(result.status, "missing")
        self.assertEqual(Path(result.path), manager.install_path)
        runner.assert_not_called()

    def test_marker_mismatch_or_missing_python_is_invalid_and_preserved(self) -> None:
        manager = self.manager()
        root = self.write_install(manager, marker_updates={"commit": "wrong"})
        result = manager.inspect()
        self.assertEqual(result.status, "invalid")
        self.assertIn("marker", result.message)
        self.assertTrue(root.exists())

        # A new isolated manager path avoids mutating the first invalid fixture.
        manager2 = RuntimeManager(
            self.project,
            self.runtime_dir,
            self.root / "other" / "runtimes",
            RUNTIME_SPEC,
            runner=Mock(side_effect=AssertionError("missing Python must stop before import")),
        )
        self.write_install(manager2, python=False)
        missing_python = manager2.inspect()
        self.assertEqual(missing_python.status, "invalid")
        self.assertIn("Python is missing", missing_python.message)

    def test_incomplete_marker_or_changed_python_contract_is_invalid(self) -> None:
        success = Mock(
            return_value=subprocess.CompletedProcess(
                ["python"], 0, json.dumps({"arch": "arm64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
            )
        )
        incomplete = self.manager(success)
        self.write_install(incomplete, marker_updates={"validation_status": "partial"})
        self.assertEqual(incomplete.inspect().status, "invalid")

        changed_spec = dict(RUNTIME_SPEC, python="3.13")
        other = RuntimeManager(
            self.project,
            self.runtime_dir,
            self.root / "changed-python" / "runtimes",
            changed_spec,
            runner=success,
        )
        self.write_install(other, marker_updates={"python": "3.12"})
        result = other.inspect()
        self.assertEqual(result.status, "invalid")
        self.assertIn("Python", result.message)

    def test_import_failure_or_wrong_architecture_is_invalid(self) -> None:
        failing = self.manager(
            Mock(return_value=subprocess.CompletedProcess(["python"], 1, "", "import failed"))
        )
        self.write_install(failing)
        self.assertIn("import verification failed", failing.inspect().message)

        other_root = self.root / "wrong-arch" / "runtimes"
        wrong_arch = RuntimeManager(
            self.project,
            self.runtime_dir,
            other_root,
            RUNTIME_SPEC,
            runner=Mock(
                return_value=subprocess.CompletedProcess(
                    ["python"], 0, json.dumps({"arch": "x86_64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
                )
            ),
        )
        self.write_install(wrong_arch)
        result = wrong_arch.inspect()
        self.assertEqual(result.status, "invalid")
        self.assertIn("not native arm64", result.message)

    def test_matching_marker_native_python_and_import_are_installed(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["python"], 0, json.dumps({"arch": "arm64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
            )
        )
        manager = self.manager(runner)
        self.write_install(manager)
        result = manager.inspect()
        self.assertTrue(result.ready)
        self.assertEqual(result.version, RUNTIME_SPEC["version"])
        command = runner.call_args.args[0]
        self.assertEqual(command[0], str(manager._python_path(manager.install_path)))
        self.assertEqual(command[1], "-c")

    def test_wrong_pinned_vllm_version_is_invalid(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["python"], 0, json.dumps({"arch": "arm64", "vllm": "unexpected-version"}), ""
            )
        )
        manager = self.manager(runner)
        self.write_install(manager)
        result = manager.inspect()
        self.assertEqual(result.status, "invalid")
        self.assertIn("vLLM", result.message)

    def test_runtime_install_root_symlink_is_rejected_before_execution(self) -> None:
        runner = Mock(side_effect=AssertionError("symlinked runtime Python must not execute"))
        manager = self.manager(runner)
        outside = self.root / "outside-runtime"
        python = manager._python_path(outside)
        python.parent.mkdir(parents=True)
        python.write_text("fixture", encoding="utf-8")
        atomic_write_json(
            outside / "runtime.json",
            {
                "schema_version": RUNTIME_STATE_SCHEMA,
                "runtime": "vllm-metal",
                "version": RUNTIME_SPEC["version"],
                "commit": RUNTIME_SPEC["commit"],
                "python": RUNTIME_SPEC["python"],
                "wheel_sha256": RUNTIME_SPEC["wheel_sha256"],
                "vllm_source_sha256": RUNTIME_SPEC["vllm_source_sha256"],
                "validation_status": "complete",
            },
        )
        manager.install_path.parent.mkdir(parents=True)
        try:
            manager.install_path.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("test filesystem does not support directory symlinks")

        result = manager.inspect()
        self.assertEqual(result.status, "invalid")
        self.assertTrue(
            "symlink" in result.message.lower()
            or "symbolic link" in result.message.lower(),
            result.message,
        )
        runner.assert_not_called()


class RuntimeEnsureTests(RuntimeManagerCase):
    def test_non_mac_intel_mac_and_rosetta_are_rejected_before_download(self) -> None:
        manager = self.manager()
        fixtures = (
            HardwareInfo(operating_system="linux", native_architecture="arm64"),
            HardwareInfo(operating_system="darwin", native_architecture="amd64", apple_silicon=False),
            apple_hardware(rosetta=True),
        )
        for hardware in fixtures:
            with self.subTest(hardware=hardware), self.assertRaises(PrerequisiteError):
                manager.ensure(hardware, dry_run=True)

    def test_offline_missing_fails_and_dry_run_plans_without_writes(self) -> None:
        runner = Mock(side_effect=AssertionError("offline/dry-run must not run commands"))
        manager = self.manager(runner)
        with self.assertRaises(OfflineError):
            manager.ensure(apple_hardware(), offline=True)
        planned = manager.ensure(apple_hardware(), dry_run=True)
        self.assertEqual(planned.status, "planned")
        self.assertFalse(self.shared.exists())
        self.assertFalse(self.runtime_dir.exists())
        runner.assert_not_called()

    def test_existing_installed_runtime_is_idempotent(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["python"], 0, json.dumps({"arch": "arm64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
            )
        )
        manager = self.manager(runner)
        self.write_install(manager)
        first = manager.ensure(apple_hardware())
        second = manager.ensure(apple_hardware())
        self.assertTrue(first.ready)
        self.assertEqual(first, second)
        self.assertEqual(runner.call_count, 2)  # one non-mutating import check per ensure

    def test_invalid_runtime_is_preserved_and_never_reinstalled_implicitly(self) -> None:
        runner = Mock(side_effect=AssertionError("invalid runtime must not invoke installer"))
        manager = self.manager(runner)
        root = self.write_install(manager, marker_updates={"commit": "wrong"})
        sentinel = root / "user-data"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(TechSaraError, "invalid and was preserved"):
            manager.ensure(apple_hardware())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        runner.assert_not_called()

    def test_successful_install_uses_verified_local_artifacts_and_validates_final_runtime(self) -> None:
        commands = []

        def runner(args, **kwargs):
            args = [str(arg) for arg in args]
            commands.append((args, dict(kwargs)))
            if len(args) >= 2 and args[1] == "venv":
                staging = Path(args[-1])
                python = RuntimeManager._python_path(staging)
                python.parent.mkdir(parents=True)
                python.write_text("fixture", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "freeze" in args:
                return subprocess.CompletedProcess(args, 0, "vllm==9.8.7-fixture\nvllm-metal==1.2.3\n", "")
            if args[0].endswith("python") and "-c" in args:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"arch": "arm64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        downloads = []

        def verified(_url, destination, _sha):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"verified fixture")
            downloads.append(destination)
            return destination

        manager = self.manager(runner)
        with patch("techsara_cli.runtime.verified_download", side_effect=verified):
            result = manager.ensure(apple_hardware())
        self.assertTrue(result.ready)
        self.assertEqual(len(downloads), 2)
        self.assertTrue(all(path.parent.name == "downloads" for path in downloads))
        self.assertTrue(any(args[1:3] == ["venv", "--python"] for args, _kwargs in commands))
        install_commands = [item for item in commands if item[0][1:3] == ["pip", "install"]]
        self.assertEqual(len(install_commands), 1)
        install_args, install_kwargs = install_commands[0]
        self.assertIn("--require-hashes", install_args)
        self.assertIn("TECHSARA_VLLM_SOURCE", install_kwargs["env"])
        self.assertIn("TECHSARA_VLLM_METAL_WHEEL", install_kwargs["env"])
        marker = json.loads((manager.install_path / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["validation_status"], "complete")
        self.assertEqual(marker["resolved_packages"], ["vllm==9.8.7-fixture", "vllm-metal==1.2.3"])

    def test_hash_locked_install_fallback_uses_only_verified_local_paths(self) -> None:
        install_attempts = []

        def runner(args, **_kwargs):
            args = [str(arg) for arg in args]
            if len(args) >= 2 and args[1] == "venv":
                python = RuntimeManager._python_path(Path(args[-1]))
                python.parent.mkdir(parents=True)
                python.write_text("fixture", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1:3] == ["pip", "install"]:
                install_attempts.append(args)
                return subprocess.CompletedProcess(args, 1 if len(install_attempts) == 1 else 0, "", "")
            if args[0].endswith("python"):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"arch": "arm64", "vllm": RUNTIME_SPEC["vllm_version"]}), ""
                )
            if "freeze" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def verified(_url, destination, _sha):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"verified")
            return destination

        manager = self.manager(runner)
        with patch("techsara_cli.runtime.verified_download", side_effect=verified):
            self.assertTrue(manager.ensure(apple_hardware()).ready)
        self.assertEqual(len(install_attempts), 2)
        fallback = install_attempts[1]
        self.assertNotIn("https://", " ".join(fallback))
        self.assertTrue(fallback[-2].endswith(".tar.gz"))
        self.assertTrue(fallback[-1].endswith(".whl"))

    def test_failed_self_test_keeps_partial_runtime_for_diagnosis(self) -> None:
        def runner(args, **_kwargs):
            args = [str(arg) for arg in args]
            if len(args) >= 2 and args[1] == "venv":
                python = RuntimeManager._python_path(Path(args[-1]))
                python.parent.mkdir(parents=True)
                python.write_text("fixture", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1:3] == ["pip", "install"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0].endswith("python"):
                return subprocess.CompletedProcess(args, 1, "", "wrong architecture")
            return subprocess.CompletedProcess(args, 0, "", "")

        def verified(_url, destination, _sha):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"verified")
            return destination

        manager = self.manager(runner)
        with patch("techsara_cli.runtime.verified_download", side_effect=verified):
            with self.assertRaisesRegex(TechSaraError, "native import self-test"):
                manager.ensure(apple_hardware())
        staging = manager.install_path.with_name(manager.install_path.name + ".partial")
        self.assertTrue(staging.exists())
        self.assertFalse(manager.install_path.exists())


def process_record(project: Path, **overrides) -> ProcessRecord:
    values = {
        "schema_version": PROCESS_STATE_SCHEMA,
        "service": "main-model",
        "pid": 4242,
        "process_identity": "fixture:identity",
        "command_fingerprint": _command_fingerprint(["fixture", "serve"]),
        "project_root": str(project.resolve()),
        "model_id": "fixture/model",
        "runtime_version": "1.2.3",
        "port": 18000,
        "log_path": str(project / "runtime" / "logs" / "main-model.log"),
        "started_at": "2026-08-11T00:00:00+00:00",
        "health": "starting",
    }
    values.update(overrides)
    return ProcessRecord(**values)


class ProcessRecordAndPortTests(unittest.TestCase):
    def test_record_parser_rejects_missing_or_wrongly_typed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            valid = process_record(Path(temporary))
            self.assertEqual(ProcessRecord.from_dict(valid.to_dict()), valid)
            payload = valid.to_dict()
            payload.pop("pid")
            self.assertIsNone(ProcessRecord.from_dict(payload))
            payload = valid.to_dict()
            payload["unexpected"] = "ignored"
            self.assertEqual(ProcessRecord.from_dict(payload), valid)

            for field, bad_value in (
                ("schema_version", "1"),
                ("pid", "4242"),
                ("port", "18000"),
                ("service", "../model"),
                ("project_root", 123),
            ):
                with self.subTest(field=field, value=bad_value):
                    payload = valid.to_dict()
                    payload[field] = bad_value
                    self.assertIsNone(ProcessRecord.from_dict(payload))

    def test_command_fingerprint_is_deterministic_and_argument_boundary_safe(self) -> None:
        self.assertEqual(_command_fingerprint(["a", "bc"]), _command_fingerprint(["a", "bc"]))
        self.assertNotEqual(_command_fingerprint(["a", "bc"]), _command_fingerprint(["ab", "c"]))

    def test_pid_alive_handles_lookup_and_permission_without_signalling(self) -> None:
        with patch("techsara_cli.runtime.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(_pid_alive(123))
        with patch("techsara_cli.runtime.os.kill", side_effect=PermissionError):
            self.assertTrue(_pid_alive(123))
        self.assertFalse(_pid_alive(0))

    def test_port_validation_happens_before_socket_creation(self) -> None:
        with patch("techsara_cli.runtime.socket.socket") as socket_factory:
            for port in (0, 80, 1023, 65536, 99999):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    ProcessManager.port_available(port)
        socket_factory.assert_not_called()

    def test_port_probe_uses_short_loopback_connect_only(self) -> None:
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=None)
        sock.connect_ex.return_value = 1
        with patch("techsara_cli.runtime.socket.socket", return_value=sock):
            self.assertTrue(ProcessManager.port_available(18000))
        sock.settimeout.assert_called_once_with(0.3)
        sock.connect_ex.assert_called_once_with(("127.0.0.1", 18000))


class ProcessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.runtime = self.root / "runtime"
        self.project.mkdir()
        self.manager = ProcessManager(self.project, self.runtime)

    def save(self, record: ProcessRecord) -> Path:
        path = self.manager.record_path(record.service)
        atomic_write_json(path, record.to_dict(), mode=0o600)
        return path

    def test_service_name_rejects_paths_shell_syntax_and_empty_values(self) -> None:
        for value in ("", "../main", "main/model", "main;id", "main model", "."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.record_path(value)
        self.assertEqual(self.manager.record_path("main-model").name, "main-model.json")

    def test_corrupt_record_types_are_reported_stale_without_crashing_or_signalling(self) -> None:
        path = self.manager.record_path("main-model")
        atomic_write_json(
            path,
            {
                **process_record(self.project).to_dict(),
                "pid": "not-an-integer",
            },
            mode=0o600,
        )
        with patch("techsara_cli.runtime.os.kill") as kill:
            status = self.manager.status()
            stopped = self.manager.stop("main-model")
        self.assertEqual(status[0]["state"], "stale")
        self.assertFalse(stopped)
        kill.assert_not_called()

    def test_owned_running_requires_schema_project_pid_and_process_identity(self) -> None:
        record = process_record(self.project)
        with patch("techsara_cli.runtime._pid_alive", return_value=True), patch(
            "techsara_cli.runtime._process_identity", return_value=record.process_identity
        ):
            self.assertTrue(self.manager.is_owned_running(record))
        with patch("techsara_cli.runtime._pid_alive", return_value=True), patch(
            "techsara_cli.runtime._process_identity", return_value="reused-pid"
        ):
            self.assertFalse(self.manager.is_owned_running(record))
        self.assertFalse(
            self.manager.is_owned_running(replace(record, project_root=str(self.root / "other")))
        )

    def test_dry_run_start_never_creates_files_or_processes(self) -> None:
        with patch.object(self.manager, "port_available", return_value=True), patch(
            "techsara_cli.runtime.subprocess.Popen"
        ) as popen:
            record = self.manager.start(
                "main-model",
                ["fixture", "literal;not-shell", "$(id)"],
                model_id="fixture/model",
                runtime_version="1",
                port=18000,
                env={"API_KEY": "secret"},
                dry_run=True,
            )
        self.assertEqual(record.pid, 0)
        self.assertEqual(record.health, "planned")
        self.assertFalse(self.runtime.exists())
        popen.assert_not_called()

    def test_compatible_running_service_is_reused_without_port_or_process_probe(self) -> None:
        args = ["fixture", "serve"]
        record = process_record(
            self.project,
            command_fingerprint=_command_fingerprint(args),
            model_id="fixture/model",
            runtime_version="1",
            port=18000,
            health="healthy",
        )
        with patch.object(self.manager, "load", return_value=record), patch.object(
            self.manager, "is_owned_running", return_value=True
        ), patch.object(self.manager, "port_available") as port, patch(
            "techsara_cli.runtime.subprocess.Popen"
        ) as popen:
            result = self.manager.start(
                "main-model", args, model_id="fixture/model", runtime_version="1", port=18000
            )
        self.assertIs(result, record)
        port.assert_not_called()
        popen.assert_not_called()

    def test_only_healthy_matching_runtime_process_is_reused(self) -> None:
        args = ["fixture", "serve"]
        for health, runtime_version in (
            ("starting", "1"),
            ("unhealthy", "1"),
            ("healthy", "old-runtime"),
        ):
            with self.subTest(health=health, runtime_version=runtime_version):
                record = process_record(
                    self.project,
                    command_fingerprint=_command_fingerprint(args),
                    model_id="fixture/model",
                    runtime_version=runtime_version,
                    port=18000,
                    health=health,
                )
                with patch.object(self.manager, "load", return_value=record), patch.object(
                    self.manager, "is_owned_running", return_value=True
                ), patch.object(self.manager, "port_available") as port, patch(
                    "techsara_cli.runtime.subprocess.Popen"
                ) as popen:
                    with self.assertRaisesRegex(
                        TechSaraError, "already running|healthy|runtime"
                    ):
                        self.manager.start(
                            "main-model",
                            args,
                            model_id="fixture/model",
                            runtime_version="1",
                            port=18000,
                        )
                port.assert_not_called()
                popen.assert_not_called()

    def test_exact_owned_starting_process_is_resumable_but_other_state_is_not(self) -> None:
        args = ["fixture", "serve"]
        starting = process_record(
            self.project,
            command_fingerprint=_command_fingerprint(args),
            model_id="fixture/model",
            runtime_version="1",
            port=18000,
            health="starting",
        )
        with patch.object(self.manager, "load", return_value=starting), patch.object(
            self.manager, "is_owned_running", return_value=True
        ):
            self.assertTrue(
                self.manager.is_resumable_start(
                    "main-model",
                    args,
                    model_id="fixture/model",
                    runtime_version="1",
                    port=18000,
                )
            )
            self.assertFalse(
                self.manager.is_resumable_start(
                    "main-model",
                    ["fixture", "different"],
                    model_id="fixture/model",
                    runtime_version="1",
                    port=18000,
                )
            )
        with patch.object(self.manager, "load", return_value=replace(starting, health="unhealthy")), patch.object(
            self.manager, "is_owned_running", return_value=True
        ):
            self.assertFalse(
                self.manager.is_resumable_start(
                    "main-model",
                    args,
                    model_id="fixture/model",
                    runtime_version="1",
                    port=18000,
                )
            )

    def test_incompatible_running_service_or_port_collision_is_never_replaced(self) -> None:
        record = process_record(self.project)
        with patch.object(self.manager, "load", return_value=record), patch.object(
            self.manager, "is_owned_running", return_value=True
        ), patch("techsara_cli.runtime.subprocess.Popen") as popen:
            with self.assertRaisesRegex(TechSaraError, "different model or command"):
                self.manager.start(
                    "main-model", ["different"], model_id="other/model", runtime_version="1", port=18000
                )
        popen.assert_not_called()

        with patch.object(self.manager, "load", return_value=None), patch.object(
            self.manager, "port_available", return_value=False
        ), patch("techsara_cli.runtime.subprocess.Popen") as popen:
            with self.assertRaisesRegex(TechSaraError, "already in use"):
                self.manager.start(
                    "main-model", ["fixture"], model_id="fixture/model", runtime_version="1", port=18000
                )
        popen.assert_not_called()

    def test_start_uses_exact_argv_detached_session_and_writes_private_record(self) -> None:
        process = Mock(pid=4242)
        process.poll.return_value = None
        args = ["fixture", "serve", "literal;not-shell", "$(id)"]
        with patch.object(self.manager, "port_available", return_value=True), patch(
            "techsara_cli.runtime.subprocess.Popen", return_value=process
        ) as popen, patch("techsara_cli.runtime._process_identity", return_value="fixture:4242"), patch(
            "techsara_cli.runtime.time.sleep"
        ):
            record = self.manager.start(
                "main-model",
                args,
                model_id="fixture/model",
                runtime_version="1.2.3",
                port=18000,
                env={"MODEL_API_KEY": "fixture-secret"},
            )
        self.assertEqual(record.pid, 4242)
        self.assertEqual(record.process_identity, "fixture:4242")
        passed_args, kwargs = popen.call_args
        self.assertEqual(passed_args[0], args)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], self.project.resolve())
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["MODEL_API_KEY"], "fixture-secret")
        self.assertRegex(kwargs["env"]["TECHSARA_PROJECT_OWNER"], r"^[0-9a-f]{64}$")
        record_path = self.manager.record_path("main-model")
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        self.assertNotIn("fixture-secret", record_path.read_text(encoding="utf-8"))

    def test_early_process_exit_does_not_create_ownership_record(self) -> None:
        process = Mock(pid=4242)
        process.poll.return_value = 1
        with patch.object(self.manager, "port_available", return_value=True), patch(
            "techsara_cli.runtime.subprocess.Popen", return_value=process
        ), patch("techsara_cli.runtime._process_identity", return_value=""), patch(
            "techsara_cli.runtime.time.sleep"
        ):
            with self.assertRaisesRegex(TechSaraError, "exited before its ownership record"):
                self.manager.start(
                    "main-model", ["fixture"], model_id="fixture/model", runtime_version="1", port=18000
                )
        self.assertFalse(self.manager.record_path("main-model").exists())

    def test_mark_health_only_updates_a_currently_owned_process(self) -> None:
        record = process_record(self.project)
        path = self.save(record)
        with patch.object(self.manager, "is_owned_running", return_value=True):
            self.manager.mark_health(record.service, "healthy")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["health"], "healthy")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        before = path.read_text(encoding="utf-8")
        with patch.object(self.manager, "is_owned_running", return_value=False):
            self.manager.mark_health(record.service, "compromised")
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_stale_record_is_removed_without_signalling_any_pid(self) -> None:
        record = process_record(self.project)
        path = self.save(record)
        with patch.object(self.manager, "is_owned_running", return_value=False), patch(
            "techsara_cli.runtime.os.kill"
        ) as kill:
            self.assertFalse(self.manager.stop(record.service))
        kill.assert_not_called()
        self.assertFalse(path.exists())

    def test_dry_run_stop_reports_owned_service_without_signal_or_state_change(self) -> None:
        record = process_record(self.project)
        path = self.save(record)
        with patch.object(self.manager, "is_owned_running", return_value=True), patch(
            "techsara_cli.runtime.os.kill"
        ) as kill:
            self.assertTrue(self.manager.stop(record.service, dry_run=True))
        kill.assert_not_called()
        self.assertTrue(path.exists())

    def test_clean_stop_sends_term_only_then_removes_record(self) -> None:
        record = process_record(self.project)
        path = self.save(record)
        with patch.object(self.manager, "is_owned_running", return_value=True), patch(
            "techsara_cli.runtime._pid_alive", return_value=False
        ), patch(
            "techsara_cli.runtime._process_identity", return_value=record.process_identity
        ), patch("techsara_cli.runtime.os.kill") as kill:
            self.assertTrue(self.manager.stop(record.service, timeout=1))
        kill.assert_called_once_with(record.pid, signal.SIGTERM)
        self.assertFalse(path.exists())

    def test_timeout_escalates_only_when_identity_still_matches(self) -> None:
        record = process_record(self.project)
        self.save(record)
        with patch.object(self.manager, "is_owned_running", return_value=True), patch(
            "techsara_cli.runtime.time.monotonic", side_effect=[0.0, 2.0]
        ), patch("techsara_cli.runtime._pid_alive", return_value=True), patch(
            "techsara_cli.runtime._process_identity", return_value=record.process_identity
        ), patch("techsara_cli.runtime.os.kill") as kill:
            self.assertTrue(self.manager.stop(record.service, timeout=1))
        self.assertEqual(
            kill.call_args_list,
            [call(record.pid, signal.SIGTERM), call(record.pid, signal.SIGKILL)],
        )

    def test_stop_rechecks_process_identity_immediately_before_first_signal(self) -> None:
        record = process_record(self.project)
        path = self.save(record)
        with patch.object(
            self.manager, "is_owned_running", side_effect=[True, False]
        ), patch("techsara_cli.runtime.os.kill") as kill:
            stopped = self.manager.stop(record.service, timeout=0)
        self.assertFalse(stopped)
        kill.assert_not_called()
        self.assertFalse(path.exists())

    def test_stop_all_is_sorted_and_delegates_dry_run(self) -> None:
        for service in ("z-model", "a-model"):
            self.save(process_record(self.project, service=service))
        with patch.object(self.manager, "stop", return_value=True) as stop:
            stopped = self.manager.stop_all(timeout=3, dry_run=True)
        self.assertEqual(stopped, ["a-model", "z-model"])
        self.assertEqual(
            stop.call_args_list,
            [call("a-model", timeout=3, dry_run=True), call("z-model", timeout=3, dry_run=True)],
        )


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class CapabilityRequestTests(unittest.TestCase):
    def test_request_uses_bearer_header_json_body_timeout_and_no_shell(self) -> None:
        response = FakeHTTPResponse(b'{"ok": true}')
        seen = []

        def urlopen(request, *, timeout):
            seen.append((request, timeout))
            return response

        prober = CapabilityProber(timeout=7.5)
        with patch("techsara_cli.runtime.urllib.request.urlopen", side_effect=urlopen):
            ok, body, error = prober._request(
                "http://127.0.0.1:18000/",
                "/v1/models",
                api_key="fixture-secret",
                payload={"probe": "safe"},
            )
        self.assertTrue(ok)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(error, "")
        request, timeout = seen[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18000/v1/models")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        self.assertEqual(json.loads(request.data), {"probe": "safe"})
        self.assertEqual(timeout, 7.5)

    def test_network_error_returns_only_exception_class_not_secret_or_url_detail(self) -> None:
        prober = CapabilityProber()
        error = urllib.error.URLError("fixture-secret internal detail")
        with patch("techsara_cli.runtime.urllib.request.urlopen", side_effect=error):
            ok, body, detail = prober._request(
                "http://127.0.0.1:18000", "/health", api_key="fixture-secret"
            )
        self.assertFalse(ok)
        self.assertIsNone(body)
        self.assertEqual(detail, "URLError")
        self.assertNotIn("fixture-secret", detail)


class RecordingProber(CapabilityProber):
    def __init__(self) -> None:
        super().__init__(timeout=1)
        self.calls = []

    def _request(self, base_url, path, *, api_key="", payload=None, stream=False):
        self.calls.append(
            {
                "base_url": base_url,
                "path": path,
                "api_key": api_key,
                "payload": payload,
                "stream": stream,
            }
        )
        if path == "/v1/models":
            return True, {"data": [{"id": "fixture-capability"}]}, ""
        if path == "/health":
            return True, {}, ""
        if path == "/tokenize":
            return True, {"tokens": [1, 2, 3]}, ""
        if path == "/score":
            return True, {"data": [{"score": 0.9}]}, ""
        if path == "/v1/embeddings":
            return True, {"data": [{"embedding": [0.1, 0.2, 0.3]}]}, ""
        if path == "/v1/chat/completions" and stream:
            return True, 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n', ""
        if path == "/v1/chat/completions":
            if payload and payload.get("tools"):
                return True, {"choices": [{"message": {"tool_calls": [{"id": "fixture"}]}}]}, ""
            if payload and payload.get("response_format"):
                return True, {"choices": [{"message": {"content": '{"ok": true}'}}]}, ""
            return True, {"choices": [{"message": {"content": "OK", "reasoning_content": "fixture"}}]}, ""
        return False, None, "fixture-unhandled"


class CapabilityProbeTests(unittest.TestCase):
    def test_declared_contracts_are_probed_and_observed_not_assumed(self) -> None:
        model = capability_model(
            supports_embeddings=True,
            supports_reranking=True,
            supports_vision=True,
            supports_ocr=True,
        )
        prober = RecordingProber()
        result = prober.probe(
            name="main",
            base_url="http://127.0.0.1:18000",
            model=model,
            api_key="fixture-key",
            selected_context=4096,
        )
        for feature in (
            "models",
            "health",
            "chat",
            "streaming",
            "cancellation",
            "reasoning",
            "structured_output",
            "tool_calling",
            "tokenization",
            "maximum_context",
            "embeddings",
            "reranking",
            "vision",
            "ocr",
        ):
            with self.subTest(feature=feature):
                self.assertTrue(result[feature]["supported"])
        self.assertEqual(result["embeddings"]["dimension"], 3)
        self.assertFalse(result["model_load_unload"]["supported"])
        self.assertEqual(result["schema_version"], CAPABILITY_SCHEMA)
        self.assertTrue(all(item["api_key"] == "fixture-key" for item in prober.calls))
        self.assertGreaterEqual(sum(item["path"] == "/health" for item in prober.calls), 2)

    def test_undeclared_features_are_disabled_without_calling_their_endpoints(self) -> None:
        model = capability_model(
            supports_chat=False,
            supports_reasoning=False,
            supports_tool_calling=False,
            supports_structured_output=False,
            supports_embeddings=False,
            supports_reranking=False,
            supports_vision=False,
            supports_ocr=False,
        )
        prober = RecordingProber()
        result = prober.probe(name="embed", base_url="http://fixture", model=model)
        for feature in (
            "chat",
            "streaming",
            "cancellation",
            "reasoning",
            "structured_output",
            "tool_calling",
            "embeddings",
            "reranking",
            "vision",
            "ocr",
        ):
            with self.subTest(feature=feature):
                self.assertFalse(result[feature]["supported"])
        called_paths = [item["path"] for item in prober.calls]
        self.assertNotIn("/v1/chat/completions", called_paths)
        self.assertNotIn("/v1/embeddings", called_paths)
        self.assertNotIn("/score", called_paths)

    def test_selected_context_above_manifest_limit_is_never_reported_supported(self) -> None:
        model = capability_model(context_limit=4096, tested_context=2048)
        result = RecordingProber().probe(
            name="main", base_url="http://fixture", model=model, selected_context=8192
        )
        self.assertFalse(result["maximum_context"]["supported"])
        self.assertEqual(result["maximum_context"]["configured"], 8192)
        self.assertEqual(result["maximum_context"]["manifest_limit"], 4096)

    def test_failed_health_check_is_reported_as_degraded_not_raised_or_hidden(self) -> None:
        class UnhealthyProber(RecordingProber):
            def _request(self, base_url, path, *, api_key="", payload=None, stream=False):
                if path == "/health":
                    self.calls.append(
                        {
                            "base_url": base_url,
                            "path": path,
                            "api_key": api_key,
                            "payload": payload,
                            "stream": stream,
                        }
                    )
                    return False, None, "TimeoutError"
                return super()._request(
                    base_url, path, api_key=api_key, payload=payload, stream=stream
                )

        result = UnhealthyProber().probe(
            name="main", base_url="http://fixture", model=capability_model()
        )
        self.assertFalse(result["health"]["supported"])
        self.assertEqual(result["health"]["detail"], "TimeoutError")
        self.assertFalse(result["cancellation"]["supported"])

    def test_http_success_without_required_response_contracts_is_not_supported(self) -> None:
        class MalformedSuccessProber(RecordingProber):
            def _request(self, base_url, path, *, api_key="", payload=None, stream=False):
                self.calls.append(
                    {
                        "base_url": base_url,
                        "path": path,
                        "api_key": api_key,
                        "payload": payload,
                        "stream": stream,
                    }
                )
                if path == "/health":
                    return True, {}, ""
                if path == "/v1/models":
                    return True, {"data": [{"id": "some-other-model"}]}, ""
                if path == "/tokenize":
                    return True, {"unexpected": True}, ""
                if path == "/score":
                    return True, {"unexpected": True}, ""
                if path == "/v1/chat/completions" and stream:
                    return True, "ordinary text, not an SSE data frame", ""
                if path == "/v1/chat/completions" and payload and payload.get("response_format"):
                    return True, {"choices": [{"message": {"content": "not json"}}]}, ""
                if path == "/v1/chat/completions":
                    return True, {"unexpected": True}, ""
                return True, {"unexpected": True}, ""

        model = capability_model(supports_reranking=True)
        result = MalformedSuccessProber().probe(
            name="main",
            base_url="http://fixture",
            model=model,
            selected_context=4096,
        )
        for feature in (
            "models",
            "chat",
            "streaming",
            "structured_output",
            "tokenization",
            "maximum_context",
            "reranking",
        ):
            with self.subTest(feature=feature):
                self.assertFalse(result[feature]["supported"], feature)

    def test_probe_payloads_contain_only_synthetic_fixture_content(self) -> None:
        prober = RecordingProber()
        prober.probe(name="main", base_url="http://fixture", model=capability_model())
        serialized = json.dumps([item["payload"] for item in prober.calls], sort_keys=True)
        self.assertIn("TechSara", serialized)
        for forbidden in ("Salesforce", "HF_TOKEN", "password", "conversation"):
            self.assertNotIn(forbidden, serialized)

    def test_capability_results_are_atomically_written_with_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime" / "capabilities.json"
            CapabilityProber().write_results(path, [{"name": "fixture", "chat": {"supported": True}}])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], CAPABILITY_SCHEMA)
            self.assertEqual(payload["models"][0]["name"], "fixture")
            self.assertTrue(payload["generated_at"])


if __name__ == "__main__":
    unittest.main()
