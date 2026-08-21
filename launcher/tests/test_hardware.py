from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .support import CommandStub, GIB, ReadTextStub, docker_handler, linux_files
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import CommandStub, GIB, ReadTextStub, docker_handler, linux_files

from techsara_cli.hardware import (
    GPU_PROBE_IMAGE,
    HARDWARE_SCHEMA_VERSION,
    HardwareInfo,
    _detect_dgx_spark,
    _docker_permission_denied,
    _normalize_arch,
    _parse_nvidia_csv,
    choose_cache_path,
    detect_hardware,
)


class HardwareValueTests(unittest.TestCase):
    def test_architecture_aliases_are_normalized(self) -> None:
        self.assertEqual(_normalize_arch("x86_64"), "amd64")
        self.assertEqual(_normalize_arch("X64"), "amd64")
        self.assertEqual(_normalize_arch("aarch64"), "arm64")
        self.assertEqual(_normalize_arch(" arm64 "), "arm64")
        self.assertEqual(_normalize_arch(""), "unknown")

    def test_hardware_info_round_trips_and_ignores_future_fields(self) -> None:
        original = HardwareInfo(
            operating_system="linux",
            host_architecture="arm64",
            total_system_memory_bytes=128 * GIB,
            docker_running=True,
        )
        payload = original.to_dict()
        payload["future_schema_field"] = "ignored"
        restored = HardwareInfo.from_dict(payload)
        self.assertEqual(restored, original)
        self.assertEqual(restored.schema_version, HARDWARE_SCHEMA_VERSION)

    def test_nvidia_csv_aggregates_multiple_gpus_and_ignores_na_memory(self) -> None:
        name, count, total, free, capability = _parse_nvidia_csv(
            "NVIDIA A, 24576, 20000, 8.9\nNVIDIA A, 24576, 19000, 8.9\n"
        )
        self.assertEqual(name, "NVIDIA A")
        self.assertEqual(count, 2)
        self.assertEqual(total, 49152 * 1024**2)
        self.assertEqual(free, 39000 * 1024**2)
        self.assertEqual(capability, "8.9")

        _name, _count, total, free, _capability = _parse_nvidia_csv(
            "NVIDIA GB10, N/A, [N/A], 12.1"
        )
        self.assertEqual((total, free), (0, 0))

    def test_nvidia_compute_capability_uses_numeric_not_lexicographic_order(self) -> None:
        _name, count, _total, _free, capability = _parse_nvidia_csv(
            "NVIDIA Old, 24576, 20000, 9.0\n"
            "NVIDIA New, 24576, 20000, 12.1\n"
        )
        self.assertEqual(count, 2)
        self.assertEqual(capability, "12.1")

    def test_dgx_detection_requires_multiple_signals(self) -> None:
        self.assertTrue(
            _detect_dgx_spark(
                arch="arm64",
                gpu_name="NVIDIA GB10",
                total_memory=128 * GIB,
                cpu_name="20-core ARM",
                system_metadata="NVIDIA DGX Spark",
            )
        )
        self.assertFalse(
            _detect_dgx_spark(
                arch="amd64",
                gpu_name="NVIDIA RTX 4090",
                total_memory=128 * GIB,
                cpu_name="Fixture CPU",
                system_metadata="Generic workstation",
            )
        )

    def test_dgx_detection_accepts_the_dmi_spelling_of_the_product_name(self) -> None:
        # Real DGX Spark DMI reports "NVIDIA_DGX_Spark"; an `\s*` separator
        # silently dropped the strongest signal on the very host it identifies.
        self.assertTrue(
            _detect_dgx_spark(
                arch="arm64",
                gpu_name="NVIDIA_DGX_Spark",
                total_memory=128 * GIB,
                cpu_name="",
                system_metadata="NVIDIA_DGX_Spark",
            )
        )


class CachePathTests(unittest.TestCase):
    def test_explicit_cache_override_has_highest_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = choose_cache_path(
                root / "project",
                {
                    "TECHSARA_MODEL_CACHE": str(root / "explicit"),
                    "VLLM_MODELS_DIR": str(root / "legacy"),
                },
            )
            self.assertEqual(selected, (root / "explicit").resolve())

    def test_existing_legacy_cache_is_reused_without_a_hard_coded_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "portable-model-cache"
            legacy.mkdir()
            selected = choose_cache_path(root / "project", {"VLLM_MODELS_DIR": str(legacy)})
            self.assertEqual(selected, legacy.resolve())
            self.assertNotIn("techsphere", str(selected).lower())

    def test_platform_defaults_are_user_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_home = Path(temporary) / "home"
            cases = {
                "Darwin": fake_home / "Library" / "Caches" / "TechSara" / "models",
                "Linux": fake_home / ".cache" / "techsara" / "models",
                "Windows": fake_home / "AppData" / "Local" / "TechSara" / "models",
            }
            for system, expected in cases.items():
                with self.subTest(system=system), patch(
                    "techsara_cli.hardware.platform.system", return_value=system
                ), patch("techsara_cli.hardware.Path.home", return_value=fake_home):
                    self.assertEqual(choose_cache_path(fake_home / "project", {}), expected.resolve())


class MacDetectionTests(unittest.TestCase):
    def _detect_mac(
        self,
        *,
        chip: str = "Apple M5 Max",
        memory_gib: int = 64,
        memory_value: str | None = None,
        page_size_value: str | None = None,
        translated: bool = False,
        native_arm: bool = True,
        docker_status: str = "",
    ) -> tuple[HardwareInfo, CommandStub]:
        profiler = {"SPHardwareDataType": [{"chip_type": chip}]} if chip else {"SPHardwareDataType": [{}]}

        def extra(args: tuple[str, ...], _timeout: float):
            if args == ("system_profiler", "SPHardwareDataType", "-json"):
                return 0, json.dumps(profiler), ""
            if args[:3] == ("sysctl", "-n", "hw.memsize"):
                return 0, memory_value if memory_value is not None else str(memory_gib * GIB), ""
            if args[:3] == ("sysctl", "-n", "hw.pagesize"):
                return 0, page_size_value if page_size_value is not None else "4096", ""
            if args[:3] == ("sysctl", "-n", "sysctl.proc_translated"):
                return 0, "1" if translated else "0", ""
            if args[:3] == ("sysctl", "-n", "hw.optional.arm64"):
                return 0, "1" if native_arm else "0", ""
            if args[:3] == ("sysctl", "-n", "machdep.cpu.brand_string"):
                return 0, chip, ""
            if args == ("vm_stat",):
                return 0, "Pages free: 1000.\nPages inactive: 2000.\nPages speculative: 500.\nPages purgeable: 250.\n", ""
            return None

        handler = docker_handler(
            model_status=docker_status,
            extra=extra,
        )
        command = CommandStub(handler)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "techsara_cli.hardware.shutil.which",
            return_value="/fixture/docker" if docker_status else None,
        ), patch("techsara_cli.hardware.disk_free", return_value=700 * GIB), patch(
            "techsara_cli.hardware.platform.mac_ver", return_value=("15.5", ("", "", ""), "")
        ), patch("techsara_cli.hardware.platform.release", return_value="24.5.0"), patch(
            "techsara_cli.hardware.platform.machine",
            return_value="x86_64" if translated else ("arm64" if native_arm else "x86_64"),
        ):
            info = detect_hardware(
                Path(temporary),
                command=command,
                system="Darwin",
                machine="x86_64" if translated else ("arm64" if native_arm else "x86_64"),
                environ={"TECHSARA_MODEL_CACHE": str(Path(temporary) / "cache")},
            )
        return info, command

    def test_m5_max_memory_and_available_pages_are_detected(self) -> None:
        info, _command = self._detect_mac(memory_gib=64)
        self.assertEqual(info.operating_system, "darwin")
        self.assertEqual(info.operating_system_version, "15.5")
        self.assertEqual(info.host_architecture, "arm64")
        self.assertEqual(info.native_architecture, "arm64")
        self.assertTrue(info.apple_silicon)
        self.assertEqual(info.apple_chip_name, "Apple M5 Max")
        self.assertEqual(info.apple_unified_memory_bytes, 64 * GIB)
        self.assertEqual(info.gpu_vendor, "apple")
        self.assertEqual(info.gpu_total_memory_bytes, 64 * GIB)
        self.assertEqual(info.available_system_memory_bytes, 3750 * 4096)
        self.assertEqual(info.free_disk_bytes, 700 * GIB)

    def test_rosetta_reports_host_and_native_architectures_separately(self) -> None:
        info, _command = self._detect_mac(translated=True)
        self.assertEqual(info.host_architecture, "amd64")
        self.assertEqual(info.native_architecture, "arm64")
        self.assertTrue(info.apple_silicon)
        self.assertTrue(info.running_under_rosetta)

    def test_intel_mac_is_not_misclassified_as_apple_silicon(self) -> None:
        info, _command = self._detect_mac(chip="Intel Core i9", native_arm=False)
        self.assertFalse(info.apple_silicon)
        self.assertEqual(info.gpu_vendor, "none")
        self.assertEqual(info.gpu_count, 0)
        self.assertEqual(info.apple_unified_memory_bytes, 0)

    def test_unknown_apple_chip_is_safe_and_does_not_invent_m5(self) -> None:
        info, _command = self._detect_mac(chip="")
        self.assertEqual(info.apple_chip_name, "")
        self.assertEqual(info.cpu_name, "Apple Silicon")
        self.assertNotIn("M5", info.cpu_name)

    def test_malformed_numeric_sysctl_output_degrades_without_crashing(self) -> None:
        info, _command = self._detect_mac(
            memory_value="not-a-number",
            page_size_value="also-invalid",
        )
        self.assertEqual(info.total_system_memory_bytes, 0)
        self.assertEqual(info.available_system_memory_bytes, 0)
        self.assertTrue(info.apple_silicon)

    def test_dmr_presence_alone_does_not_claim_vllm_metal(self) -> None:
        info, _command = self._detect_mac(
            docker_status="Docker Model Runner is running\nllama.cpp: running with Metal"
        )
        self.assertTrue(info.docker_model_runner_available)
        self.assertFalse(info.docker_model_runner_vllm_metal_available)

    def test_future_dmr_capability_must_explicitly_report_vllm_and_metal(self) -> None:
        info, _command = self._detect_mac(
            docker_status="Docker Model Runner is running\nvllm Metal: running"
        )
        self.assertTrue(info.docker_model_runner_vllm_metal_available)


class LinuxDetectionTests(unittest.TestCase):
    def _detect_linux(
        self,
        *,
        nvidia_row: str = "",
        docker_running: bool = True,
        docker_denied: bool = False,
        linux_containers: bool = True,
        gpu_smoke: bool = True,
        files: ReadTextStub | None = None,
        arch: str = "amd64",
        kernel: str = "6.8.0-fixture",
        cached_images: list[str] | None = None,
        pullable: bool = True,
        allow_network: bool = True,
    ) -> tuple[HardwareInfo, CommandStub]:
        def extra(args: tuple[str, ...], _timeout: float):
            if args and args[0] == "nvidia-smi" and len(args) > 1:
                return (0, nvidia_row, "") if nvidia_row else (1, "", "not installed")
            return None

        command = CommandStub(
            docker_handler(
                running=docker_running,
                denied=docker_denied,
                linux_containers=linux_containers,
                gpu_smoke=gpu_smoke,
                cached_images=cached_images,
                pullable=pullable,
                extra=extra,
            )
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "techsara_cli.hardware.shutil.which", return_value="/fixture/docker"
        ), patch("techsara_cli.hardware.disk_free", return_value=250 * GIB), patch(
            "techsara_cli.hardware.platform.release", return_value=kernel
        ):
            info = detect_hardware(
                Path(temporary),
                command=command,
                read_text=files or linux_files(),
                system="Linux",
                machine=arch,
                environ={"TECHSARA_MODEL_CACHE": str(Path(temporary) / "cache")},
                allow_network=allow_network,
            )
        return info, command

    def test_linux_nvidia_uses_real_container_probe_contract_without_pull(self) -> None:
        info, command = self._detect_linux(
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9"
        )
        self.assertEqual(info.gpu_vendor, "nvidia")
        self.assertEqual(info.gpu_total_memory_bytes, 24576 * 1024**2)
        self.assertTrue(info.nvidia_driver_available)
        self.assertTrue(info.docker_gpu_available)
        self.assertTrue(info.nvidia_container_toolkit_available)
        smoke_calls = [args for args, _timeout in command.calls if args[:2] == ("docker", "run")]
        self.assertEqual(len(smoke_calls), 1)
        self.assertIn("--pull", smoke_calls[0])
        self.assertEqual(smoke_calls[0][smoke_calls[0].index("--pull") + 1], "never")
        self.assertIn("@sha256:", " ".join(smoke_calls[0]))
        self.assertIn("--gpus", smoke_calls[0])
        self.assertIn("/dev/nvidiactl", " ".join(smoke_calls[0]))
        # Nothing was pulled: a cached candidate answered the probe.
        self.assertFalse([args for args, _t in command.calls if args[:2] == ("docker", "pull")])

    def test_clean_clone_without_a_cached_runtime_image_pulls_only_the_tiny_pinned_probe(self) -> None:
        info, command = self._detect_linux(
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9",
            cached_images=[],
        )
        self.assertTrue(info.docker_gpu_available)
        pulls = [args for args, _t in command.calls if args[:2] == ("docker", "pull")]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0][-1], GPU_PROBE_IMAGE)
        self.assertIn("@sha256:", GPU_PROBE_IMAGE)
        # The multi-gigabyte runtime images are never pulled just to detect.
        self.assertNotIn("vllm", pulls[0][-1])

    def test_offline_detection_never_pulls_and_reports_no_container_gpu(self) -> None:
        info, command = self._detect_linux(
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9",
            cached_images=[],
            allow_network=False,
        )
        self.assertTrue(info.nvidia_driver_available)
        self.assertFalse(info.docker_gpu_available)
        self.assertFalse([args for args, _t in command.calls if args[:2] == ("docker", "pull")])

    def test_a_failed_probe_pull_is_reported_as_no_container_gpu(self) -> None:
        info, _command = self._detect_linux(
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9",
            cached_images=[],
            pullable=False,
        )
        self.assertFalse(info.docker_gpu_available)
        self.assertFalse(info.nvidia_container_toolkit_available)

    def test_nvidia_driver_without_passing_docker_gpu_smoke_stays_unavailable(self) -> None:
        info, command = self._detect_linux(
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9",
            gpu_smoke=False,
        )
        self.assertTrue(info.nvidia_driver_available)
        self.assertEqual(info.gpu_vendor, "nvidia")
        self.assertFalse(info.docker_gpu_available)
        self.assertFalse(info.nvidia_container_toolkit_available)
        self.assertTrue(any(args[:2] == ("docker", "run") for args, _timeout in command.calls))

    def test_dgx_spark_uses_system_memory_when_nvidia_smi_reports_na(self) -> None:
        files = linux_files(total_gib=128, available_gib=100)
        files.values["/proc/cpuinfo"] = "Model : NVIDIA Grace 20-core\n"
        files.values["/sys/class/dmi/id/product_name"] = "NVIDIA DGX Spark\n"
        files.values["/sys/class/dmi/id/sys_vendor"] = "NVIDIA\n"
        info, _command = self._detect_linux(
            nvidia_row="NVIDIA GB10, N/A, N/A, 12.1",
            files=files,
            arch="aarch64",
        )
        self.assertTrue(info.dgx_spark)
        self.assertEqual(info.native_architecture, "arm64")
        self.assertEqual(info.gpu_total_memory_bytes, 128 * GIB)
        self.assertEqual(info.gpu_available_memory_bytes, 100 * GIB)

    def test_linux_without_nvidia_is_cpu_only_and_never_runs_gpu_container(self) -> None:
        info, command = self._detect_linux(nvidia_row="")
        self.assertEqual(info.gpu_vendor, "none")
        self.assertFalse(info.docker_gpu_available)
        self.assertFalse(any(args[:2] == ("docker", "run") for args, _timeout in command.calls))

    def test_docker_daemon_stopped_is_distinct_from_missing_cli(self) -> None:
        info, command = self._detect_linux(docker_running=False, nvidia_row="")
        self.assertTrue(info.docker_installed)
        self.assertFalse(info.docker_running)
        self.assertTrue(info.docker_compose_available)
        self.assertTrue(command.called("docker", "version", "--format", "{{json .}}"))

    def test_stopped_daemon_is_not_reported_as_a_permission_problem(self) -> None:
        info, _ = self._detect_linux(docker_running=False, nvidia_row="")
        self.assertFalse(info.docker_running)
        self.assertFalse(info.docker_permission_denied)

    def test_unreadable_socket_is_distinct_from_a_stopped_daemon(self) -> None:
        info, command = self._detect_linux(docker_denied=True, nvidia_row="")
        self.assertTrue(info.docker_installed)
        self.assertFalse(info.docker_running)
        self.assertTrue(info.docker_permission_denied)
        self.assertTrue(command.called("docker", "version", "--format", "{{json .}}"))

    def test_permission_classifier_reads_the_endpoint_error_not_the_exit_code(self) -> None:
        # Docker words this differently per platform and release; every wording
        # must land on "grant access", never on "start the daemon".
        for text in (
            "permission denied while trying to connect to the docker API at"
            " unix:///var/run/docker.sock",
            "Got permission denied while trying to connect to the Docker daemon socket",
            "dial unix /var/run/docker.sock: connect: permission denied",
            "open //./pipe/docker_engine: Access is denied.",
        ):
            with self.subTest(text=text):
                self.assertTrue(_docker_permission_denied(text))
        for text in (
            "",
            "   ",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
            " Is the docker daemon running?",
            "dial unix /var/run/docker.sock: connect: no such file or directory",
            "error during connect: connection refused",
        ):
            with self.subTest(text=text):
                self.assertFalse(_docker_permission_denied(text))

    def test_docker_unavailable_makes_no_docker_subprocess_calls(self) -> None:
        command = CommandStub(
            lambda args, _timeout: (1, "", "not installed") if args[0] == "nvidia-smi" else (99, "", "unexpected")
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "techsara_cli.hardware.shutil.which", return_value=None
        ), patch("techsara_cli.hardware.disk_free", return_value=10 * GIB), patch(
            "techsara_cli.hardware.platform.release", return_value="fixture"
        ):
            info = detect_hardware(
                Path(temporary),
                command=command,
                read_text=linux_files(total_gib=8, available_gib=2),
                system="Linux",
                machine="x86_64",
                environ={"TECHSARA_MODEL_CACHE": str(Path(temporary) / "cache")},
            )
        self.assertFalse(info.docker_installed)
        self.assertFalse(info.docker_running)
        self.assertFalse(any(args[0] == "docker" for args, _timeout in command.calls))

    def test_wsl_kernel_is_detected_without_assuming_windows_gpu_support(self) -> None:
        info, _command = self._detect_linux(
            nvidia_row="", kernel="5.15.90.1-microsoft-standard-WSL2"
        )
        self.assertTrue(info.running_under_wsl2)
        self.assertFalse(info.docker_gpu_available)


class WindowsDetectionTests(unittest.TestCase):
    def _detect_windows(
        self,
        *,
        wsl2: bool,
        linux_containers: bool,
        gpu_smoke: bool,
        wsl_status: str | None = None,
        nvidia_row: str = "",
        payload_updates: dict | None = None,
    ) -> tuple[HardwareInfo, CommandStub]:
        payload = {
            "caption": "Windows Fixture",
            "version": "11.0.99999",
            "total": 64 * GIB,
            "free": 48 * GIB,
            "cpu": "Fixture CPU",
            "cores": 24,
            "gpus": [{"name": "NVIDIA RTX Fixture", "ram": 24 * GIB}],
        }
        payload.update(payload_updates or {})

        def extra(args: tuple[str, ...], _timeout: float):
            if args and args[0] == "powershell":
                return 0, json.dumps(payload), ""
            if args == ("wsl.exe", "--status"):
                if wsl_status is not None:
                    return 0, wsl_status, ""
                return (0, "Default Version: 2", "") if wsl2 else (1, "", "not installed")
            if "nvidia-smi" in args and args[0] != "docker":
                return (0, nvidia_row, "") if nvidia_row else (1, "", "not available")
            return None

        command = CommandStub(
            docker_handler(
                running=True,
                linux_containers=linux_containers,
                gpu_smoke=gpu_smoke,
                extra=extra,
            )
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "techsara_cli.hardware.shutil.which", return_value="C:/Docker/docker.exe"
        ), patch("techsara_cli.hardware.disk_free", return_value=300 * GIB):
            info = detect_hardware(
                Path(temporary),
                command=command,
                system="Windows",
                machine="AMD64",
                environ={"TECHSARA_MODEL_CACHE": str(Path(temporary) / "cache")},
            )
        return info, command

    def test_windows_wsl2_linux_containers_and_container_gpu_are_separate_facts(self) -> None:
        info, _command = self._detect_windows(wsl2=True, linux_containers=True, gpu_smoke=True)
        self.assertEqual(info.operating_system, "windows")
        self.assertEqual(info.host_architecture, "amd64")
        self.assertTrue(info.windows_wsl2_available)
        self.assertTrue(info.docker_linux_containers)
        self.assertTrue(info.docker_gpu_available)
        self.assertEqual(info.gpu_total_memory_bytes, 24 * GIB)

    def test_host_gpu_does_not_enable_acceleration_without_wsl2(self) -> None:
        info, _command = self._detect_windows(wsl2=False, linux_containers=True, gpu_smoke=True)
        self.assertEqual(info.gpu_vendor, "nvidia")
        self.assertFalse(info.windows_wsl2_available)
        # Detection records the independent Docker smoke. Profile gating is
        # tested separately and must still reject this combination.
        self.assertTrue(info.docker_gpu_available)

    def test_windows_containers_prevent_docker_gpu_smoke(self) -> None:
        info, command = self._detect_windows(wsl2=True, linux_containers=False, gpu_smoke=True)
        self.assertFalse(info.docker_linux_containers)
        self.assertFalse(info.docker_gpu_available)
        self.assertFalse(any(args[:2] == ("docker", "run") for args, _timeout in command.calls))

    def test_wsl1_status_with_unrelated_digit_two_is_not_misclassified_as_wsl2(self) -> None:
        info, _command = self._detect_windows(
            wsl2=False,
            linux_containers=True,
            gpu_smoke=True,
            wsl_status="WSL tool version: 1.2.5\nDefault Version: 1",
        )
        self.assertFalse(info.windows_wsl2_available)

    def test_windows_nvidia_memory_includes_measured_free_vram(self) -> None:
        info, command = self._detect_windows(
            wsl2=True,
            linux_containers=True,
            gpu_smoke=True,
            nvidia_row="NVIDIA RTX Fixture, 24576, 20000, 8.9",
        )
        self.assertEqual(info.gpu_total_memory_bytes, 24576 * 1024**2)
        self.assertEqual(info.gpu_available_memory_bytes, 20000 * 1024**2)
        self.assertEqual(info.nvidia_compute_capability, "8.9")
        self.assertTrue(
            any("nvidia-smi" in args and args[0] != "docker" for args, _timeout in command.calls)
        )

    def test_malformed_windows_cim_numbers_degrade_without_crashing(self) -> None:
        info, _command = self._detect_windows(
            wsl2=False,
            linux_containers=True,
            gpu_smoke=False,
            payload_updates={
                "total": "not-a-number",
                "free": {},
                "cores": "invalid",
                "gpus": [{"name": "NVIDIA Fixture", "ram": "invalid"}],
            },
        )
        self.assertEqual(info.total_system_memory_bytes, 0)
        self.assertEqual(info.available_system_memory_bytes, 0)
        self.assertGreaterEqual(info.cpu_core_count, 1)
        self.assertEqual(info.gpu_total_memory_bytes, 0)


if __name__ == "__main__":
    unittest.main()
