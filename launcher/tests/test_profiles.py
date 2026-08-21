from __future__ import annotations

import unittest
from dataclasses import replace

try:
    from .support import GIB, REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB, REPO_ROOT

from techsara_cli.errors import UnsafeOverrideError
from techsara_cli.hardware import HardwareInfo
from techsara_cli.profiles import (
    ModelSpec,
    SelectedProfile,
    load_hardware_profiles,
    load_model_manifest,
    select_profile,
)
from techsara_cli.utils import validate_model_id, validate_profile_name, validate_revision


def mac(
    memory_gib: int,
    *,
    available_gib: int | None = None,
    chip: str = "Apple M5 Max",
    rosetta: bool = False,
) -> HardwareInfo:
    return HardwareInfo(
        operating_system="darwin",
        operating_system_version="15.5",
        host_architecture="amd64" if rosetta else "arm64",
        native_architecture="arm64",
        total_system_memory_bytes=memory_gib * GIB,
        available_system_memory_bytes=(available_gib if available_gib is not None else memory_gib) * GIB,
        gpu_vendor="apple",
        gpu_name=chip,
        gpu_count=1,
        gpu_total_memory_bytes=memory_gib * GIB,
        gpu_available_memory_bytes=(available_gib if available_gib is not None else memory_gib) * GIB,
        apple_silicon=True,
        apple_chip_name=chip,
        apple_unified_memory_bytes=memory_gib * GIB,
        running_under_rosetta=rosetta,
        docker_installed=True,
        docker_running=True,
        docker_compose_available=True,
        docker_linux_containers=True,
        free_disk_bytes=500 * GIB,
    )


def nvidia(
    vram_gib: int,
    *,
    available_vram_gib: int | None = None,
    gpu_count: int = 1,
    operating_system: str = "linux",
    wsl2: bool = False,
    linux_containers: bool = True,
    docker_gpu: bool = True,
    dgx: bool = False,
    system_memory_gib: int = 128,
) -> HardwareInfo:
    return HardwareInfo(
        operating_system=operating_system,
        host_architecture="arm64" if dgx else "amd64",
        native_architecture="arm64" if dgx else "amd64",
        total_system_memory_bytes=system_memory_gib * GIB,
        available_system_memory_bytes=max(8, system_memory_gib - 16) * GIB,
        gpu_vendor="nvidia",
        gpu_name="NVIDIA GB10" if dgx else "NVIDIA Fixture GPU",
        gpu_count=gpu_count,
        gpu_total_memory_bytes=vram_gib * GIB,
        gpu_available_memory_bytes=(
            available_vram_gib
            if available_vram_gib is not None
            else max(1, vram_gib - 2)
        )
        * GIB,
        nvidia_driver_available=True,
        nvidia_container_toolkit_available=docker_gpu,
        docker_installed=True,
        docker_running=True,
        docker_compose_available=True,
        docker_linux_containers=linux_containers,
        docker_gpu_available=docker_gpu,
        windows_wsl2_available=wsl2,
        dgx_spark=dgx,
        free_disk_bytes=500 * GIB,
    )


def cpu(*, memory_gib: int = 32, operating_system: str = "linux") -> HardwareInfo:
    return HardwareInfo(
        operating_system=operating_system,
        host_architecture="amd64",
        native_architecture="amd64",
        total_system_memory_bytes=memory_gib * GIB,
        available_system_memory_bytes=max(1, memory_gib - 4) * GIB,
        gpu_vendor="none",
        docker_installed=True,
        docker_running=True,
        docker_compose_available=True,
        docker_linux_containers=True,
        free_disk_bytes=100 * GIB,
    )


class ManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.models, cls.runtimes = load_model_manifest(REPO_ROOT)
        cls.profiles = load_hardware_profiles(REPO_ROOT)

    def test_every_model_is_revision_pinned_and_has_budget_metadata(self) -> None:
        self.assertGreater(len(self.models), 0)
        for key, model in self.models.items():
            with self.subTest(model=key):
                self.assertIsInstance(model, ModelSpec)
                self.assertEqual(validate_model_id(model.id), model.id)
                self.assertEqual(validate_revision(model.revision), model.revision)
                self.assertGreater(model.approximate_download_bytes, 0)
                self.assertGreater(model.approximate_loaded_weight_bytes, 0)
                self.assertGreaterEqual(model.context_limit, model.tested_context)
                self.assertGreater(model.tested_context, 0)
                self.assertGreaterEqual(model.recommended_memory_bytes, model.minimum_memory_bytes)
                self.assertTrue(model.required_files)
                self.assertIn("spdx", model.license_metadata)
                self.assertNotIn(model.revision.lower(), {"main", "latest", "master"})

    def test_every_profile_reference_resolves_to_a_manifest_entry(self) -> None:
        for profile in self.profiles:
            with self.subTest(profile=profile["id"]):
                self.assertEqual(validate_profile_name(profile["id"]), profile["id"])
                self.assertTrue(profile.get("compose_files"))
                for field in (
                    "main_model",
                    "embedding_model",
                    "reranker_model",
                    "ocr_model",
                ):
                    key = profile.get(field)
                    if key is not None:
                        self.assertIn(key, self.models, f"{field} references {key!r}")
                router = profile.get("router_model")
                if router not in {None, "shared"}:
                    self.assertIn(router, self.models)

    def test_required_hardware_profiles_exist(self) -> None:
        ids = {profile["id"] for profile in self.profiles}
        expected = {
            "mac-16-24gb",
            "mac-32-47gb",
            "mac-48-79gb",
            "mac-80-127gb",
            "mac-128gb-plus",
            "dgx-spark",
            "nvidia-large",
            "nvidia-medium",
            "nvidia-small",
            "nvidia-minimal",
            "local-minimal",
            "app-only",
            "external-development",
        }
        self.assertTrue(expected.issubset(ids), expected - ids)

    def test_mac_models_are_metal_and_nvidia_models_are_cuda(self) -> None:
        for profile in self.profiles:
            main_key = profile.get("main_model")
            if not main_key:
                continue
            backend = self.models[main_key].backend
            with self.subTest(profile=profile["id"], backend=backend):
                if profile["family"] == "mac":
                    self.assertEqual(backend, "vllm-metal")
                elif profile["family"] == "nvidia":
                    self.assertIn(backend, {"vllm-cuda", "transformers-cuda"})
                elif profile["family"] == "cpu":
                    self.assertEqual(backend, "llama-cpp-cpu")


class MacTierSelectionTests(unittest.TestCase):
    def test_all_required_m5_max_memory_tiers_select_the_declared_family(self) -> None:
        cases = {
            24: ("mac-16-24gb", "mac-qwen3-8b-4bit", 8192, 1),
            32: ("mac-32-47gb", "mac-qwen3-14b-4bit", 16384, 1),
            48: ("mac-48-79gb", "mac-qwen36-35b-4bit", 16384, 1),
            64: ("mac-48-79gb", "mac-qwen36-35b-4bit", 32768, 1),
            96: ("mac-80-127gb", "mac-qwen36-35b-4bit", 32768, 2),
            128: ("mac-128gb-plus", "mac-qwen36-35b-6bit", 65536, 2),
        }
        for memory_gib, (profile_id, model_key, context, concurrency) in cases.items():
            with self.subTest(memory_gib=memory_gib):
                selected = select_profile(mac(memory_gib), REPO_ROOT, reuse_running_models=True)
                self.assertEqual(selected.hardware_profile_id, profile_id)
                self.assertEqual(selected.id, f"mac-m5-max-{memory_gib}gb")
                self.assertIsNotNone(selected.main_model)
                self.assertEqual(selected.main_model.key, model_key)
                self.assertEqual(selected.context_length, context)
                self.assertEqual(selected.concurrency, concurrency)
                self.assertEqual(selected.family, "mac")
                self.assertEqual(selected.runtime_backend, "native-vllm-metal")

    def test_mac_always_shares_main_and_router_and_gates_unverified_images(self) -> None:
        selected = select_profile(mac(64), REPO_ROOT, reuse_running_models=True)
        self.assertTrue(selected.router_shared)
        self.assertEqual(selected.router_model, selected.main_model)
        self.assertFalse(selected.features["vision"])
        self.assertFalse(selected.features["ocr"])
        self.assertTrue(any("image contract" in reason for reason in selected.degraded_reasons))

    def test_low_available_memory_degrades_instead_of_overcommitting(self) -> None:
        selected = select_profile(mac(128, available_gib=7), REPO_ROOT)
        self.assertEqual(selected.hardware_profile_id, "app-only")
        self.assertIsNone(selected.main_model)
        self.assertTrue(any("no local model fits" in reason for reason in selected.degraded_reasons))
        # The reason must be actionable: it names both sides of the shortfall.
        self.assertTrue(any("GiB but only" in reason for reason in selected.degraded_reasons))

    def test_reusing_a_healthy_running_model_uses_total_capacity(self) -> None:
        hardware = mac(64, available_gib=7)
        cold = select_profile(hardware, REPO_ROOT, reuse_running_models=False)
        warm = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        self.assertEqual(cold.hardware_profile_id, "app-only")
        self.assertEqual(warm.hardware_profile_id, "mac-48-79gb")

    def test_startup_retry_is_a_strictly_safer_context(self) -> None:
        selected = select_profile(mac(96), REPO_ROOT, reuse_running_models=True)
        self.assertGreater(selected.context_length, selected.startup_retry_context)
        self.assertGreaterEqual(selected.startup_retry_context, 2048)

    def test_rosetta_never_selects_the_apple_inference_runtime(self) -> None:
        selected = select_profile(mac(64, rosetta=True), REPO_ROOT, reuse_running_models=True)
        self.assertNotEqual(selected.family, "mac")
        self.assertNotEqual(selected.runtime_backend, "native-vllm-metal")
        self.assertTrue(any("Rosetta" in reason for reason in selected.degraded_reasons))

    def test_intel_mac_does_not_select_a_metal_profile(self) -> None:
        hardware = HardwareInfo(
            operating_system="darwin",
            host_architecture="amd64",
            native_architecture="amd64",
            total_system_memory_bytes=32 * GIB,
            available_system_memory_bytes=24 * GIB,
            apple_silicon=False,
            docker_installed=True,
            docker_running=True,
            docker_compose_available=True,
        )
        selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        self.assertNotEqual(selected.family, "mac")


class NvidiaAndFallbackSelectionTests(unittest.TestCase):
    def test_linux_nvidia_vram_tiers_are_deterministic(self) -> None:
        cases = {
            12: "nvidia-minimal",
            24: "nvidia-small",
            48: "nvidia-medium",
            80: "nvidia-large",
        }
        for vram_gib, expected in cases.items():
            with self.subTest(vram_gib=vram_gib):
                selected = select_profile(nvidia(vram_gib), REPO_ROOT, reuse_running_models=True)
                self.assertEqual(selected.hardware_profile_id, expected)
                self.assertEqual(selected.family, "nvidia")

    def test_dgx_spark_profile_takes_precedence_over_vram_tiers(self) -> None:
        selected = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        self.assertEqual(selected.hardware_profile_id, "dgx-spark")
        self.assertFalse(selected.router_shared)
        self.assertEqual(selected.context_length, 262144)
        self.assertTrue(selected.features["vision"])
        self.assertTrue(selected.features["ocr"])

    def test_linux_nvidia_without_container_gpu_falls_back_to_cpu(self) -> None:
        selected = select_profile(nvidia(80, docker_gpu=False), REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.family, "cpu")
        self.assertNotEqual(selected.runtime_backend, "docker-vllm-cuda")

    def test_linux_without_nvidia_uses_local_minimal(self) -> None:
        selected = select_profile(cpu(memory_gib=32), REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.hardware_profile_id, "local-minimal")
        self.assertEqual(selected.family, "cpu")

    def test_unknown_future_nvidia_gpu_uses_measured_vram_not_marketing_name(self) -> None:
        hardware = replace(
            nvidia(16),
            gpu_name="NVIDIA Future Architecture Unknown to TechSara",
        )
        selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.hardware_profile_id, "nvidia-minimal")
        self.assertEqual(selected.family, "nvidia")

    def test_cold_selection_never_uses_total_vram_when_free_vram_is_exhausted(self) -> None:
        hardware = nvidia(80, available_vram_gib=2)
        selected = select_profile(hardware, REPO_ROOT, reuse_running_models=False)
        # No CUDA tier may be sized from total VRAM when almost none is free.
        self.assertNotEqual(selected.family, "nvidia")
        self.assertIsNone(
            next((m for m in selected.required_models() if m.backend == "vllm-cuda"), None)
        )
        # The fallback is host-side inference, so its estimate is budgeted
        # against system memory — never against the exhausted device.
        self.assertLessEqual(
            selected.memory_budget.get("estimated_model_runtime", 0),
            selected.memory_budget["model_capacity"],
        )

    def test_a_gpu_too_small_for_any_cuda_tier_still_gets_cpu_inference(self) -> None:
        """An 8 GiB card is below the smallest CUDA model's declared minimum.

        Falling all the way to app-only would strand a machine that can still
        serve the CPU tier perfectly well without touching the device.
        """
        selected = select_profile(nvidia(8, system_memory_gib=32), REPO_ROOT)
        self.assertEqual(selected.hardware_profile_id, "local-minimal")
        self.assertEqual(selected.family, "cpu")
        self.assertEqual(selected.main_model.backend, "llama-cpp-cpu")

    def test_a_model_is_never_selected_below_its_own_declared_minimum(self) -> None:
        models, _ = load_model_manifest(REPO_ROOT)
        for hardware in (mac(24), mac(64), mac(128), nvidia(24), nvidia(80), cpu()):
            with self.subTest(hardware=hardware.operating_system + str(hardware.gpu_total_memory_bytes)):
                selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
                pool = (
                    hardware.gpu_total_memory_bytes // max(1, hardware.gpu_count)
                    if selected.family == "nvidia"
                    else hardware.total_system_memory_bytes
                )
                for model in selected.required_models():
                    self.assertLessEqual(
                        model.minimum_memory_bytes,
                        pool,
                        f"{model.id} was selected below its declared minimum",
                    )
                self.assertTrue(models)

    def test_multiple_gpus_are_not_treated_as_one_contiguous_device_without_tensor_parallel(self) -> None:
        hardware = nvidia(
            48,
            available_vram_gib=44,
            gpu_count=2,
        )
        selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.hardware_profile_id, "nvidia-small")
        self.assertNotIn("--tensor-parallel-size", selected.main_model.startup_arguments)

    def test_tiny_system_uses_app_only(self) -> None:
        selected = select_profile(cpu(memory_gib=6), REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.hardware_profile_id, "app-only")
        self.assertEqual(selected.runtime_backend, "disabled")

    def test_windows_nvidia_requires_all_three_gpu_gates(self) -> None:
        valid = select_profile(
            nvidia(48, operating_system="windows", wsl2=True),
            REPO_ROOT,
            reuse_running_models=True,
        )
        self.assertEqual(valid.hardware_profile_id, "nvidia-medium")

        invalid = (
            nvidia(48, operating_system="windows", wsl2=False),
            nvidia(48, operating_system="windows", wsl2=True, linux_containers=False),
            nvidia(48, operating_system="windows", wsl2=True, docker_gpu=False),
        )
        for hardware in invalid:
            with self.subTest(hardware=hardware):
                selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
                self.assertNotEqual(selected.family, "nvidia")
                self.assertTrue(any("WSL2" in reason for reason in selected.degraded_reasons))

    def test_linux_nvidia_downgrade_to_cpu_always_records_a_reason(self) -> None:
        # A DGX Spark falling back to a 0.6B CPU model must say why; silence here
        # looks exactly like "this box has no GPU".
        cases = [
            ("toolkit missing", replace(
                nvidia(120, dgx=True, docker_gpu=False),
                nvidia_container_toolkit_available=False,
            ), "install the NVIDIA Container Toolkit"),
            ("toolkit unregistered", replace(
                nvidia(120, dgx=True, docker_gpu=False),
                nvidia_container_toolkit_available=True,
            ), "nvidia-ctk runtime configure"),
        ]
        for label, hardware, expected in cases:
            with self.subTest(label=label):
                selected = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
                self.assertNotEqual(selected.family, "nvidia")
                reasons = " ".join(selected.degraded_reasons)
                self.assertIn("NVIDIA acceleration unavailable", reasons)
                self.assertIn(expected, reasons)

    def test_working_linux_nvidia_host_records_no_acceleration_complaint(self) -> None:
        selected = select_profile(nvidia(120, dgx=True), REPO_ROOT, reuse_running_models=True)
        self.assertEqual(selected.family, "nvidia")
        self.assertNotIn(
            "NVIDIA acceleration unavailable", " ".join(selected.degraded_reasons)
        )

    def test_windows_without_nvidia_uses_cpu_profile_without_cloud_fallback(self) -> None:
        selected = select_profile(
            cpu(memory_gib=32, operating_system="windows"),
            REPO_ROOT,
            reuse_running_models=True,
        )
        self.assertEqual(selected.family, "cpu")
        self.assertEqual(selected.runtime_backend, "docker-llama-cpp-cpu")
        self.assertNotIn("openai", selected.runtime_backend.lower())


class OverrideAndProfileBehaviorTests(unittest.TestCase):
    def test_identical_hardware_produces_byte_for_byte_equal_selection_data(self) -> None:
        hardware = mac(96)
        first = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        second = select_profile(hardware, REPO_ROOT, reuse_running_models=True)
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_known_compatible_model_override_is_accepted(self) -> None:
        selected = select_profile(
            mac(64),
            REPO_ROOT,
            model_override="mlx-community/Qwen3-8B-4bit",
            reuse_running_models=True,
        )
        self.assertEqual(selected.main_model.id, "mlx-community/Qwen3-8B-4bit")
        self.assertEqual(selected.router_model, selected.main_model)

    def test_unknown_unpinned_and_cross_backend_model_overrides_are_rejected(self) -> None:
        with self.assertRaises(UnsafeOverrideError):
            select_profile(
                mac(64),
                REPO_ROOT,
                model_override="mlx-community/Not-In-Manifest",
                reuse_running_models=True,
            )
        with self.assertRaises(UnsafeOverrideError):
            select_profile(
                mac(128),
                REPO_ROOT,
                model_override="nvidia/Qwen3.6-35B-A3B-NVFP4",
                reuse_running_models=True,
            )

    def test_unsafe_large_model_override_is_rejected(self) -> None:
        with self.assertRaises(UnsafeOverrideError):
            select_profile(
                mac(24),
                REPO_ROOT,
                model_override="mlx-community/Qwen3.6-35B-A3B-6bit",
                reuse_running_models=True,
            )

    def test_profile_override_rejects_unknown_or_shell_shaped_names(self) -> None:
        for value in ("does-not-exist", "../dgx-spark", "dgx-spark;id", "DGX-SPARK"):
            with self.subTest(value=value), self.assertRaises((UnsafeOverrideError, ValueError)):
                select_profile(mac(128), REPO_ROOT, profile_override=value, reuse_running_models=True)

    def test_hardware_specific_override_cannot_cross_platform_families(self) -> None:
        for profile in ("dgx-spark", "nvidia-large"):
            with self.subTest(profile=profile), self.assertRaises(UnsafeOverrideError):
                select_profile(
                    mac(128),
                    REPO_ROOT,
                    profile_override=profile,
                    reuse_running_models=True,
                )

    def test_explicit_external_and_app_only_overrides_are_platform_neutral(self) -> None:
        for profile in ("external-development", "app-only"):
            with self.subTest(profile=profile):
                selected = select_profile(
                    mac(64), REPO_ROOT, profile_override=profile, reuse_running_models=True
                )
                self.assertEqual(selected.hardware_profile_id, profile)

    def test_skip_ocr_removes_model_feature_and_adds_reason(self) -> None:
        selected = select_profile(nvidia(128, dgx=True), REPO_ROOT, skip_ocr=True)
        self.assertIsNone(selected.ocr_model)
        self.assertFalse(selected.features["ocr"])
        self.assertTrue(any("OCR skipped" in reason for reason in selected.degraded_reasons))
        self.assertFalse(any(model.supports_ocr for model in selected.required_models(skip_ocr=True)))

    def test_required_models_deduplicates_shared_router(self) -> None:
        selected = select_profile(mac(96), REPO_ROOT, reuse_running_models=True)
        required = selected.required_models()
        keys = [(model.id, model.revision) for model in required]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys.count((selected.main_model.id, selected.main_model.revision)), 1)

    def test_selected_profile_is_a_serializable_value_object(self) -> None:
        selected = select_profile(cpu(), REPO_ROOT, reuse_running_models=True)
        self.assertIsInstance(selected, SelectedProfile)
        payload = selected.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["hardware_profile_id"], "local-minimal")


if __name__ == "__main__":
    unittest.main()
