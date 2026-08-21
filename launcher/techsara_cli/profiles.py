"""Declarative hardware profiles and conservative model/memory selection."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import UnsafeOverrideError
from .hardware import HardwareInfo
from .utils import GIB, load_yaml_json, validate_model_id, validate_profile_name


@dataclass(frozen=True)
class ModelSpec:
    key: str
    id: str
    revision: str
    provider: str
    backend: str
    quantization: str
    approximate_download_bytes: int
    approximate_loaded_weight_bytes: int
    context_limit: int
    tested_context: int
    minimum_memory_bytes: int
    recommended_memory_bytes: int
    supports_chat: bool
    supports_reasoning: bool
    supports_vision: bool
    supports_audio: bool
    supports_tool_calling: bool
    supports_structured_output: bool
    supports_embeddings: bool
    supports_reranking: bool
    supports_ocr: bool
    supports_streaming: bool
    requires_trust_remote_code: bool
    endpoint_type: str
    served_id: str = ""
    startup_arguments: tuple[str, ...] = ()
    tokenizer_arguments: dict[str, Any] = field(default_factory=dict)
    health_probe: dict[str, Any] = field(default_factory=dict)
    license_metadata: dict[str, Any] = field(default_factory=dict)
    required_files: tuple[str, ...] = ()
    file_sha256: dict[str, str] = field(default_factory=dict)
    allow_patterns: tuple[str, ...] = ()
    legacy_directories: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, key: str, value: dict[str, Any]) -> "ModelSpec":
        data = dict(value)
        data["key"] = key
        for name in ("startup_arguments", "required_files", "allow_patterns", "legacy_directories"):
            data[name] = tuple(data.get(name) or ())
        return cls(**data)

    @property
    def api_model_id(self) -> str:
        return self.served_id or self.id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectedProfile:
    schema_version: int
    id: str
    hardware_profile_id: str
    family: str
    runtime_backend: str
    compose_files: tuple[str, ...]
    main_model: ModelSpec | None
    router_model: ModelSpec | None
    router_shared: bool
    embedding_model: ModelSpec | None
    reranker_model: ModelSpec | None
    ocr_model: ModelSpec | None
    context_length: int
    concurrency: int
    features: dict[str, bool]
    memory_budget: dict[str, int]
    degraded_reasons: tuple[str, ...] = ()
    startup_retry_context: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SelectedProfile":
        data = dict(values)
        for name in ("main_model", "router_model", "embedding_model", "reranker_model", "ocr_model"):
            raw = data.get(name)
            if isinstance(raw, dict):
                raw = dict(raw)
                key = str(raw.pop("key"))
                data[name] = ModelSpec.from_dict(key, raw)
            elif raw is not None and not isinstance(raw, ModelSpec):
                raise ValueError(f"invalid selected profile model: {name}")
        data["compose_files"] = tuple(data.get("compose_files") or ())
        data["degraded_reasons"] = tuple(data.get("degraded_reasons") or ())
        return cls(**data)

    def required_models(self, *, skip_ocr: bool = False) -> list[ModelSpec]:
        models: list[ModelSpec] = []
        candidates: Iterable[ModelSpec | None] = (
            self.main_model,
            None if self.router_shared else self.router_model,
            self.embedding_model,
            self.reranker_model,
            None if skip_ocr else self.ocr_model,
        )
        seen: set[tuple[str, str]] = set()
        for model in candidates:
            if model is None or (model.id, model.revision) in seen:
                continue
            seen.add((model.id, model.revision))
            models.append(model)
        return models


def load_model_manifest(project_root: Path) -> tuple[dict[str, ModelSpec], dict[str, Any]]:
    raw = load_yaml_json(project_root / "config" / "model-manifest.yaml")
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported model manifest schema")
    models = {
        key: ModelSpec.from_dict(key, value)
        for key, value in raw.get("models", {}).items()
    }
    return models, raw.get("runtimes", {})


def load_hardware_profiles(project_root: Path) -> list[dict[str, Any]]:
    raw = load_yaml_json(project_root / "config" / "hardware-profiles.yaml")
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported hardware profile schema")
    return list(raw.get("profiles") or [])


def _memory_budget(hardware: HardwareInfo, *, reuse_running_models: bool) -> dict[str, int]:
    total = hardware.total_system_memory_bytes
    available = hardware.available_system_memory_bytes or total
    if hardware.apple_silicon:
        # Unified-memory reservations overlap in time: Docker's VM contains the
        # application services, while Metal allocations remain host-side. These
        # values keep a useful profile possible at 24 GiB without treating all
        # requested service maxima as simultaneously resident RSS.
        os_reserve = max(4 * GIB, int(total * 0.08))
        docker_reserve = max(3 * GIB, int(total * 0.06))
        application_reserve = max(int(3.5 * GIB), int(total * 0.06))
        runtime_reserve = int(1.5 * GIB)
        safety_reserve = max(2 * GIB, int(total * 0.05))
    else:
        os_reserve = max(6 * GIB, int(total * 0.08))
        docker_reserve = 4 * GIB
        application_reserve = 8 * GIB  # PG, orchestrator, sync, UI, search, reports
        runtime_reserve = 3 * GIB
        safety_reserve = max(4 * GIB, int(total * 0.08))
    fixed = os_reserve + docker_reserve + application_reserve + runtime_reserve + safety_reserve
    total_capacity = max(0, total - fixed)
    live_headroom = 2 * GIB if hardware.apple_silicon else max(4 * GIB, runtime_reserve)
    available_capacity = max(0, available - live_headroom)
    selectable = total_capacity if reuse_running_models else min(total_capacity, available_capacity)
    return {
        "total": total,
        "available_at_detection": available,
        "operating_system_reserve": os_reserve,
        "docker_reserve": docker_reserve,
        "application_and_report_reserve": application_reserve,
        "runtime_reserve": runtime_reserve,
        "safety_reserve": safety_reserve,
        "model_capacity": selectable,
    }


def _base_profile_id(hardware: HardwareInfo, *, reuse_running_models: bool = False) -> str:
    total_gib = hardware.total_system_memory_bytes / GIB
    gpu_count = max(1, hardware.gpu_count)
    # No current generic profile enables tensor parallelism, so aggregated VRAM
    # cannot be treated as one contiguous allocation. For a cold start, free
    # VRAM is the binding value; an already-running compatible project model is
    # selected from physical per-device capacity for idempotent reuse.
    gpu_total_gib = hardware.gpu_total_memory_bytes / GIB / gpu_count
    gpu_free_gib = hardware.gpu_available_memory_bytes / GIB / gpu_count
    gpu_gib = gpu_total_gib if reuse_running_models else min(gpu_total_gib, gpu_free_gib)
    if hardware.operating_system == "darwin":
        if hardware.running_under_rosetta or not hardware.apple_silicon:
            return "app-only" if total_gib < 16 else "local-minimal"
        if total_gib >= 128:
            return "mac-128gb-plus"
        if total_gib >= 80:
            return "mac-80-127gb"
        if total_gib >= 48:
            return "mac-48-79gb"
        if total_gib >= 30:
            return "mac-32-47gb"
        return "mac-16-24gb" if total_gib >= 15 else "app-only"
    if hardware.dgx_spark and hardware.docker_gpu_available:
        return "dgx-spark"
    windows_gpu_ok = (
        hardware.operating_system == "windows"
        and hardware.windows_wsl2_available
        and hardware.docker_linux_containers
        and hardware.docker_gpu_available
    )
    linux_gpu_ok = (
        hardware.operating_system == "linux"
        and hardware.docker_linux_containers
        and hardware.docker_gpu_available
    )
    if windows_gpu_ok or linux_gpu_ok:
        if gpu_gib >= 70:
            return "nvidia-large"
        if gpu_gib >= 40:
            return "nvidia-medium"
        if gpu_gib >= 20:
            return "nvidia-small"
        # 10 GiB, not 8: that is the declared minimum of the smallest CUDA
        # model in the manifest. A threshold below its own model card would
        # select a tier that cannot hold weights, KV cache, and CUDA context.
        if gpu_gib >= 10:
            return "nvidia-minimal"
    # A GPU too small or too busy for any CUDA tier is not a reason to give up
    # on local inference: the CPU tier still serves a small model and never
    # touches the device.
    if total_gib >= 8:
        return "local-minimal"
    return "app-only"


def _dynamic_id(profile_id: str, hardware: HardwareInfo) -> str:
    if profile_id.startswith("mac-") and hardware.apple_chip_name:
        chip = re.sub(r"[^a-z0-9]+", "-", hardware.apple_chip_name.lower()).strip("-")
        memory = max(1, round(hardware.apple_unified_memory_bytes / GIB))
        return f"mac-{chip.removeprefix('apple-')}-{memory}gb"
    return profile_id


# Measured FP8 KV layouts for the DGX main models; anything else receives the
# more conservative allowance below.  Qwen3.8-27B is dense (64 layers x 4 KV
# heads x 256 head dim, FP8 KV), so its per-token cost is larger than the 35B
# MoE despite the smaller parameter count.
_DGX_KV_BYTES_PER_TOKEN = {
    "dgx-qwen36-35b-nvfp4": 40 * 1024,
    "dgx-qwen38-27b-nvfp4": 128 * 1024,
}


def _model_bytes(
    main: ModelSpec | None,
    embed: ModelSpec | None,
    reranker: ModelSpec | None,
    ocr: ModelSpec | None,
    *,
    context: int,
    concurrency: int,
) -> int:
    weights = sum(
        m.approximate_loaded_weight_bytes
        for m in (main, embed, reranker, ocr)
        if m is not None
    )
    # A request's full context is not duplicated for every configured worker at
    # startup. Reserve one worst-case KV cache plus bounded concurrency
    # activations. The DGX profile uses the measured/preserved FP8 KV layout;
    # other backends receive the more conservative allowance. Startup still
    # performs a real generation probe and may retry once at the safer context.
    kv_bytes_per_token = _DGX_KV_BYTES_PER_TOKEN.get(main.key if main else "", 96 * 1024)
    kv_and_activations = context * kv_bytes_per_token + max(1, concurrency) * 512 * 1024**2
    if main and main.backend == "vllm-metal":
        overhead = int(weights * 0.14) + GIB
    elif main and main.key in _DGX_KV_BYTES_PER_TOKEN:
        overhead = int(weights * 0.10) + GIB
    else:
        overhead = int(weights * 0.22) + 2 * GIB
    return weights + kv_and_activations + overhead


def _hardware_supports_profile(hardware: HardwareInfo, profile: dict[str, Any]) -> bool:
    family = profile.get("family")
    if family in {"app", "external", "cpu"}:
        return True
    if family == "mac":
        return (
            hardware.operating_system == "darwin"
            and hardware.apple_silicon
            and hardware.native_architecture == "arm64"
            and not hardware.running_under_rosetta
        )
    if family == "nvidia":
        gpu_ready = (
            hardware.gpu_vendor == "nvidia"
            and hardware.docker_gpu_available
            and hardware.docker_linux_containers
            and (
                hardware.operating_system == "linux"
                or (hardware.operating_system == "windows" and hardware.windows_wsl2_available)
            )
        )
        return gpu_ready and (profile.get("id") != "dgx-spark" or hardware.dgx_spark)
    return False


def _profile_chain(profile_id: str) -> list[str]:
    chains = {
        "mac-128gb-plus": ["mac-128gb-plus", "mac-80-127gb", "mac-48-79gb", "mac-32-47gb", "mac-16-24gb"],
        "mac-80-127gb": ["mac-80-127gb", "mac-48-79gb", "mac-32-47gb", "mac-16-24gb"],
        "mac-48-79gb": ["mac-48-79gb", "mac-32-47gb", "mac-16-24gb"],
        "mac-32-47gb": ["mac-32-47gb", "mac-16-24gb"],
        "nvidia-large": ["nvidia-large", "nvidia-medium", "nvidia-small", "nvidia-minimal"],
        "nvidia-medium": ["nvidia-medium", "nvidia-small", "nvidia-minimal"],
        "nvidia-small": ["nvidia-small", "nvidia-minimal"],
    }
    return chains.get(profile_id, [profile_id])


def _loadable_memory(hardware: HardwareInfo, family: str) -> int:
    """The pool a model's weights actually occupy on this family.

    CUDA weights live in device memory; Metal and CPU weights live in the
    unified/system pool. DGX Spark reports one shared pool for both.
    """
    if family == "nvidia":
        gpu_count = max(1, hardware.gpu_count)
        return hardware.gpu_total_memory_bytes // gpu_count
    return hardware.total_system_memory_bytes


def _meets_declared_minimum(
    hardware: HardwareInfo, family: str, models: Iterable[ModelSpec | None]
) -> tuple[bool, str]:
    """Honour each manifest entry's own declared floor.

    The budget calculation below is about what fits *alongside* everything else;
    this is the separate question of whether the publisher's stated minimum is
    met at all. Without it a manifest edit or a manual profile override could
    select a model onto hardware its own model card rules out.
    """
    capacity = _loadable_memory(hardware, family)
    if capacity <= 0:
        return True, ""
    for model in models:
        if model is not None and model.minimum_memory_bytes > capacity:
            return False, (
                f"{model.id} declares a {model.minimum_memory_bytes / GIB:.0f} GiB minimum "
                f"but this host offers {capacity / GIB:.0f} GiB"
            )
    return True, ""


def _compatible_backend(family: str, backend: str) -> bool:
    if family == "mac":
        return backend == "vllm-metal"
    if family == "nvidia":
        return backend in {"vllm-cuda", "transformers-cuda"}
    if family == "cpu":
        return backend == "llama-cpp-cpu"
    return False


def select_profile(
    hardware: HardwareInfo,
    project_root: Path,
    *,
    profile_override: str | None = None,
    model_override: str | None = None,
    skip_ocr: bool = False,
    reuse_running_models: bool = False,
) -> SelectedProfile:
    """Select a deterministic, memory-budgeted runtime and model set."""
    models, _ = load_model_manifest(project_root)
    profiles = load_hardware_profiles(project_root)
    profile_map = {item["id"]: item for item in profiles}
    requested = _base_profile_id(hardware, reuse_running_models=reuse_running_models)
    if profile_override:
        validate_profile_name(profile_override)
        if profile_override not in profile_map:
            raise UnsafeOverrideError(f"unknown profile: {profile_override}")
        if not _hardware_supports_profile(hardware, profile_map[profile_override]):
            raise UnsafeOverrideError(
                f"profile {profile_override} is incompatible with the detected hardware/runtime prerequisites"
            )
        requested = profile_override

    budget = _memory_budget(hardware, reuse_running_models=reuse_running_models)
    chosen: dict[str, Any] | None = None
    chosen_models: tuple[ModelSpec | None, ModelSpec | None, ModelSpec | None, ModelSpec | None] | None = None
    chosen_context = 0
    degraded: list[str] = []

    override_spec: ModelSpec | None = None
    if model_override:
        validate_model_id(model_override)
        override_spec = next((model for model in models.values() if model.id == model_override), None)
        if override_spec is None:
            raise UnsafeOverrideError(
                "manual --model must name a revision-pinned entry in config/model-manifest.yaml"
            )

    chain = [requested] if profile_override else _profile_chain(requested)
    smallest_requirement = 0
    declared_minimum_blocks: list[str] = []
    for candidate_id in chain:
        profile_data = profile_map[candidate_id]
        main = override_spec or models.get(profile_data.get("main_model"))
        router = main if profile_data.get("router_model") == "shared" else models.get(profile_data.get("router_model"))
        embed = models.get(profile_data.get("embedding_model"))
        reranker = models.get(profile_data.get("reranker_model"))
        ocr = None if skip_ocr else models.get(profile_data.get("ocr_model"))
        if override_spec and not _compatible_backend(profile_data["family"], override_spec.backend):
            raise UnsafeOverrideError(
                f"{override_spec.id} uses {override_spec.backend}, incompatible with {profile_data['family']}"
            )
        meets_minimum, minimum_detail = _meets_declared_minimum(
            hardware, profile_data["family"], (main, embed, reranker, ocr)
        )
        if not meets_minimum:
            if profile_override or model_override:
                raise UnsafeOverrideError(minimum_detail)
            declared_minimum_blocks.append(minimum_detail)
            continue
        contexts = list(profile_data.get("context_candidates") or [profile_data["initial_context"]])
        if candidate_id == "mac-48-79gb" and hardware.total_system_memory_bytes < 56 * GIB:
            contexts = [value for value in contexts if value <= 16384]
        for context in contexts:
            context = min(context, main.context_limit if main else context)
            required = _model_bytes(
                main, embed, reranker, ocr,
                context=context,
                concurrency=int(profile_data.get("concurrency", 1)),
            ) if main else 0
            if required and (smallest_requirement == 0 or required < smallest_requirement):
                smallest_requirement = required
            if required <= budget["model_capacity"] or profile_data["family"] in {"app", "external"}:
                chosen = profile_data
                chosen_models = (main, router, embed, reranker)
                chosen_context = context
                budget["estimated_model_runtime"] = required
                break
        if chosen:
            break

    if chosen is None:
        if profile_override or model_override:
            raise UnsafeOverrideError(
                "requested model/profile exceeds the safe available-memory budget; reduce model or free memory"
            )
        chosen = profile_map["app-only"]
        chosen_models = (None, None, None, None)
        chosen_context = 4096
        budget["estimated_model_runtime"] = 0
        # Say what was actually short, in the same units the operator can act
        # on. "No model fits" alone leaves nothing to do about it.
        capacity_gib = budget["model_capacity"] / GIB
        if smallest_requirement:
            degraded.append(
                f"no local model fits: the smallest candidate in this tier needs "
                f"{smallest_requirement / GIB:.1f} GiB but only {capacity_gib:.1f} GiB remains after "
                f"operating-system, Docker, application, runtime, and safety reserves "
                f"(total {budget['total'] / GIB:.0f} GiB)"
            )
        else:
            degraded.append(
                f"no local model fits: only {capacity_gib:.1f} GiB remains after reserves "
                f"(total {budget['total'] / GIB:.0f} GiB)"
            )
        degraded.extend(declared_minimum_blocks)
        degraded.append(
            "the frontend, orchestrator, and warehouse still start; configure "
            "--profile external-development to point at a local model server you already run"
        )

    main, router, embed, reranker = chosen_models
    ocr = None if skip_ocr else models.get(chosen.get("ocr_model"))
    router_shared = chosen.get("router_model") == "shared"
    features = {key: bool(value) for key, value in chosen.get("features", {}).items()}
    if skip_ocr:
        features["ocr"] = False
        degraded.append("OCR skipped by command-line override")
    if chosen["family"] == "mac":
        # Stable vLLM-Metal 0.2.0 is text-first. Embedding/rerank are probed;
        # vision/OCR stay unavailable until a pinned runtime passes image tests.
        features["vision"] = False
        features["ocr"] = False
        degraded.append("vision/OCR disabled: pinned vLLM-Metal runtime has no verified image contract")
    if embed is None:
        degraded.append("embeddings unavailable; RAG and semantic recall use degraded ordering/no index updates")
    if reranker is None:
        degraded.append("reranker unavailable; vector similarity order is preserved")
    if hardware.operating_system == "darwin" and hardware.running_under_rosetta:
        degraded.append("Apple inference disabled under Rosetta; run the launcher from a native arm64 terminal")
    if hardware.operating_system == "windows" and hardware.gpu_vendor == "nvidia" and chosen["family"] != "nvidia":
        degraded.append("Windows NVIDIA acceleration requires WSL2, Linux containers, and a passing Docker GPU probe")
    if hardware.operating_system == "linux" and hardware.gpu_vendor == "nvidia" and chosen["family"] != "nvidia":
        # Without this, a host with a working driver but no *container* GPU path
        # drops to the CPU profile in silence: the accelerator is detected and
        # printed, yet the tiny CPU model is selected with nothing explaining it.
        reason = (
            "NVIDIA acceleration unavailable: the host driver was detected but the Docker GPU probe did not pass"
        )
        if not hardware.nvidia_container_toolkit_available:
            reason += "; install the NVIDIA Container Toolkit"
        else:
            reason += "; register its runtime with Docker (`sudo nvidia-ctk runtime configure --runtime=docker` then restart Docker)"
        degraded.append(reason)

    candidates = list(chosen.get("context_candidates") or [chosen_context])
    safer = next((value for value in candidates if value < chosen_context), max(2048, chosen_context // 2))
    return SelectedProfile(
        schema_version=1,
        id=_dynamic_id(chosen["id"], hardware),
        hardware_profile_id=chosen["id"],
        family=chosen["family"],
        runtime_backend=chosen["runtime_backend"],
        compose_files=tuple(chosen.get("compose_files") or ()),
        main_model=main,
        router_model=router,
        router_shared=router_shared,
        embedding_model=embed,
        reranker_model=reranker,
        ocr_model=ocr,
        context_length=chosen_context,
        concurrency=int(chosen.get("concurrency", 1)),
        features=features,
        memory_budget=budget,
        degraded_reasons=tuple(dict.fromkeys(degraded)),
        startup_retry_context=safer,
    )
