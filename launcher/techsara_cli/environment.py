"""Runtime layout, local secrets, and non-secret generated configuration."""

from __future__ import annotations

import base64
import ipaddress
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .cluster import DEFAULT_DETECTORS, DEFAULT_DISCOVERY, ClusterDetectors, ClusterDiscovery, resolve_cluster
from .errors import TechSaraError
from .model_manager import ModelInstall
from .profiles import ModelSpec, SelectedProfile
from .utils import atomic_write_text, parse_env_file, render_env, secure_token

#: Host probes used to derive the two-node cluster settings. Module-level so
#: tests can substitute fakes without touching real interfaces or Docker.
CLUSTER_DETECTORS: ClusterDetectors = DEFAULT_DETECTORS
#: Peer discovery for CLUSTER_MODE=auto (RoCE links, neighbour, ssh preflight).
CLUSTER_DISCOVERY: ClusterDiscovery = DEFAULT_DISCOVERY


@dataclass(frozen=True)
class RuntimeLayout:
    project_root: Path
    runtime_dir: Path
    hardware_file: Path
    profile_file: Path
    generated_env: Path
    secrets_env: Path
    state_file: Path
    capabilities_file: Path
    locks_dir: Path
    logs_dir: Path
    pids_dir: Path
    shared_root: Path

    @classmethod
    def for_project(cls, project_root: Path) -> "RuntimeLayout":
        root = project_root.resolve()
        runtime = root / ".runtime"
        shared = Path(os.environ.get("TECHSARA_HOME", str(Path.home() / ".techsara"))).expanduser().resolve()
        return cls(
            root, runtime, runtime / "hardware.json", runtime / "selected-profile.json",
            runtime / "generated.env", runtime / "secrets.env", runtime / "state.json",
            runtime / "capabilities.json", runtime / "locks", runtime / "logs", runtime / "pids", shared,
        )

    def create(self) -> None:
        for path in (self.runtime_dir, self.locks_dir, self.logs_dir, self.pids_dir):
            path.mkdir(parents=True, exist_ok=True)



DGX_COMPOSE_OVERLAY = "compose/compose.dgx-spark.yaml"

def _configured(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return bool(normalized and not normalized.startswith(("change-me", "please-change", "replace-me")))


def prepare_local_secrets(
    layout: RuntimeLayout,
    profile: SelectedProfile,
    user_env: Mapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Create/reuse only local credentials; never overwrite the user's .env."""
    existing = parse_env_file(layout.secrets_env)
    values = dict(existing)
    warnings: list[str] = []
    generated_defaults = {
        "POSTGRES_PASSWORD": 32,
        "PGADMIN_DEFAULT_PASSWORD": 24,
        "SEARXNG_SECRET": 32,
        "SESSION_SECRET": 32,
        "TECHSARA_MODEL_API_KEY": 32,
    }
    for key, size in generated_defaults.items():
        if _configured(user_env.get(key)):
            values.pop(key, None)
        elif not _configured(values.get(key)):
            values[key] = secure_token(size)

    native = profile.runtime_backend == "native-vllm-metal"
    if native:
        key = values.get("TECHSARA_MODEL_API_KEY") or secure_token(32)
        values["TECHSARA_MODEL_API_KEY"] = key
        values["OPENAI_API_KEY"] = key
        values["EMBED_API_KEY"] = key
        values["RERANK_API_KEY"] = key
    else:
        # These aliases are native-profile scoped. Removing only our ephemeral
        # aliases allows a later external profile to use the user's .env again.
        for key in ("OPENAI_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY"):
            if key in values and values[key] == values.get("TECHSARA_MODEL_API_KEY"):
                values.pop(key, None)

    # Preserve the old host-file JWT workflow without an optional, nonportable
    # Compose bind. The private value stays in a mode-0600 runtime file.
    host_key = (user_env.get("SF_PRIVATE_KEY_HOST_FILE") or "").strip()
    if host_key and not _configured(user_env.get("SF_PRIVATE_KEY_B64")):
        source = Path(host_key).expanduser()
        try:
            if not source.is_file() or source.stat().st_size > 128 * 1024:
                raise OSError("not a bounded regular file")
            values["SF_PRIVATE_KEY_B64"] = base64.b64encode(source.read_bytes()).decode("ascii")
        except OSError:
            warnings.append("Salesforce JWT host key could not be staged; synchronization will remain unavailable")
    elif _configured(user_env.get("SF_PRIVATE_KEY_B64")):
        values.pop("SF_PRIVATE_KEY_B64", None)

    layout.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(layout.secrets_env, render_env(values), mode=0o600)
    return values, warnings


def effective_user_environment(layout: RuntimeLayout) -> dict[str, str]:
    values = parse_env_file(layout.project_root / ".env")
    values.update(parse_env_file(layout.secrets_env))
    return values


def has_salesforce_credentials(values: Mapping[str, str]) -> bool:
    identity = _configured(values.get("SF_CLIENT_ID")) and _configured(values.get("SF_USERNAME"))
    assertion = any(
        _configured(values.get(key))
        for key in ("SF_CLIENT_SECRET", "SF_PRIVATE_KEY_B64", "SF_PRIVATE_KEY_HOST_FILE")
    )
    return identity and assertion


def _container_path(
    cache_root: Path,
    install: ModelInstall,
    model: ModelSpec,
    *,
    allow_planned: bool = False,
) -> str:
    if not install.ready and not (allow_planned and install.status == "planned"):
        raise TechSaraError(f"model is not complete and cannot be mapped into a runtime: {model.id}")
    path = Path(install.path).expanduser().resolve()
    try:
        relative = path.relative_to(cache_root.expanduser().resolve())
    except ValueError as exc:
        raise TechSaraError(f"model path is outside TECHSARA_MODEL_CACHE: {model.id}") from exc
    result = Path("/models") / relative
    if model.backend == "llama-cpp-cpu" and len(model.required_files) == 1:
        filename = model.required_files[0]
        if not any(character in filename for character in "*?["):
            result /= filename
    return result.as_posix()


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


#: Host ports the launcher may publish, with the values this deployment has
#: historically used. Model ports are only published on explicit opt-in.
MODEL_PORT_DEFAULTS = {
    "VLLM_PORT": 8000,
    "VLLM_ROUTER_PORT": 8002,
    "VLLM_EMBED_PORT": 8003,
    "VLLM_OCR_PORT": 8004,
    "LLAMA_CPP_PORT": 8000,
}


def resolve_bind_address(values: Mapping[str, str]) -> str:
    """Resolve the publish address, defaulting to loopback.

    Anything other than a literal IP address is rejected: this value is
    interpolated into a Compose port mapping, so a hostname or free text would
    either fail obscurely at container creation or widen exposure by accident.
    """
    raw = str(values.get("TECHSARA_BIND_ADDRESS", "") or "").strip()
    if not raw:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise TechSaraError(
            "TECHSARA_BIND_ADDRESS must be a literal IP address such as 127.0.0.1 "
            f"(loopback only) or 0.0.0.0 (all interfaces); got {raw!r}"
        ) from exc
    return str(address)


def publishes_beyond_loopback(bind_address: str) -> bool:
    try:
        return not ipaddress.ip_address(bind_address).is_loopback
    except ValueError:
        return False


def resolve_published_port(values: Mapping[str, str], name: str) -> int:
    raw = str(values.get(name, "") or "").strip()
    if not raw:
        return MODEL_PORT_DEFAULTS[name]
    try:
        port = int(raw)
    except ValueError as exc:
        raise TechSaraError(f"{name} must be an integer port; got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise TechSaraError(f"{name} must be between 1 and 65535; got {port}")
    return port


def _external_url(value: str, name: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        loopback_literal = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback_literal = False
    local = (
        host in {"localhost", "host.docker.internal"}
        or loopback_literal
        or (host and "." not in host and ":" not in host)
    )
    if parsed.scheme not in {"http", "https"} or not local or parsed.username or parsed.password:
        raise TechSaraError(
            f"{name} must be an explicit local/container OpenAI-compatible URL; automatic cloud fallback is prohibited"
        )
    return url


def _external_value(values: Mapping[str, str], name: str, default: str = "") -> str:
    value = str(values.get(name, default)).strip()
    if any(character in value for character in "\r\n\x00") or len(value) > 512:
        raise TechSaraError(f"invalid external development setting: {name}")
    return value


def _capability_values(
    prefix: str,
    model: ModelSpec | None,
    *,
    enabled: bool,
    context: int,
    concurrency: int,
    native_auth: bool,
) -> dict[str, str]:
    declared = model is not None and enabled
    supports = lambda name: bool(declared and getattr(model, name))
    values = {
        f"{prefix}_PROVIDER": model.provider if model else "disabled",
        f"{prefix}_BACKEND": model.backend if model else "disabled",
        f"{prefix}_ENABLED": _bool(declared),
        f"{prefix}_SUPPORTS_CHAT": _bool(supports("supports_chat")),
        f"{prefix}_SUPPORTS_STREAMING": _bool(supports("supports_streaming")),
        f"{prefix}_SUPPORTS_REASONING": _bool(supports("supports_reasoning")),
        f"{prefix}_SUPPORTS_TOOL_CALLING": _bool(supports("supports_tool_calling")),
        f"{prefix}_SUPPORTS_STRUCTURED_OUTPUT": _bool(supports("supports_structured_output")),
        f"{prefix}_SUPPORTS_VISION": _bool(supports("supports_vision")),
        f"{prefix}_SUPPORTS_EMBEDDINGS": _bool(supports("supports_embeddings")),
        f"{prefix}_SUPPORTS_RERANKING": _bool(supports("supports_reranking")),
        f"{prefix}_SUPPORTS_OCR": _bool(supports("supports_ocr")),
        f"{prefix}_SUPPORTS_TOKENIZATION": _bool(bool(declared and model.endpoint_type != "in-process")),
        f"{prefix}_REASONING_FIELD": "auto" if supports("supports_reasoning") else "none",
        f"{prefix}_CONTEXT_LENGTH": str(context if declared else 0),
        f"{prefix}_OUTPUT_LIMIT": str(min(8192, max(256, context // 4)) if declared else 0),
        f"{prefix}_CONCURRENCY": str(concurrency if declared else 0),
        f"{prefix}_EXTRA_BODY_ALLOWED": "chat_template_kwargs" if declared and model.backend == "vllm-cuda" and model.supports_reasoning else "",
        f"{prefix}_REQUIRES_AUTHENTICATION": _bool(declared and native_auth),
    }
    return values


def build_generated_environment(
    layout: RuntimeLayout,
    profile: SelectedProfile,
    installs: Iterable[ModelInstall],
    *,
    cache_root: Path,
    skip_ocr: bool = False,
    search_enabled: bool = False,
    search_provider: str = "searxng",
    context_override: int | None = None,
    allow_planned: bool = False,
    external_environment: Mapping[str, str] | None = None,
    user_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a secret-free env map consumed by Compose and application code."""
    user_values = dict(user_environment or {})
    bind_address = resolve_bind_address(user_values)
    publish_model_ports = _truthy(user_values.get("PUBLISH_MODEL_PORTS"))
    search_provider = search_provider.strip().lower() or "searxng"
    if search_provider not in {"searxng", "tavily", "brave"}:
        raise TechSaraError(
            "SEARCH_PROVIDER must be one of searxng, tavily, or brave"
        )
    requested_context = int(context_override or profile.context_length)
    context = min(requested_context, profile.main_model.context_limit) if profile.main_model else requested_context
    cache = cache_root.expanduser().resolve()
    install_map = {(item.model_id, item.revision): item for item in installs}

    # Two-node DGX Spark cluster: CLUSTER_MODE=auto (default) discovers and
    # verifies a second Spark on the dgx-spark profile only; every other host
    # resolves to single without touching the network, so its generated.env
    # gains just TECHSARA_CLUSTER_MODE/TECHSARA_CLUSTER_REASON.
    cluster = resolve_cluster(
        user_values,
        profile_id=profile.hardware_profile_id,
        publish_model_ports=publish_model_ports,
        context=context,
        startup_arguments=profile.main_model.startup_arguments if profile.main_model else (),
        vllm_port=resolve_published_port(user_values, "VLLM_PORT"),
        detectors=CLUSTER_DETECTORS,
        discovery=CLUSTER_DISCOVERY,
    )
    cluster_mode = cluster.mode
    cluster_values = cluster.generated()

    native = profile.runtime_backend == "native-vllm-metal"
    family = profile.family
    if native:
        main_url, embed_url, rerank_url, ocr_url = (
            "http://host.docker.internal:18100/v1",
            "http://host.docker.internal:18103/v1",
            "http://host.docker.internal:18105",
            "http://disabled.invalid/v1",
        )
    elif family == "nvidia":
        # In dual mode the head runs with host networking and its API listens
        # on the host port VLLM_PORT; `vllm` resolves to the host via extra_hosts.
        main_url = (
            f"http://vllm:{resolve_published_port(user_values, 'VLLM_PORT')}/v1"
            if cluster_mode == "dual"
            else "http://vllm:30000/v1"
        )
        embed_url = "http://vllm-embed:30003/v1"
        # Only the DGX overlay declares a standalone vllm-reranker service;
        # the other NVIDIA overlays still score in-process.  No /v1 suffix:
        # reranker_score_url() appends the root /score path.
        rerank_url = (
            "http://vllm-reranker:30005"
            if DGX_COMPOSE_OVERLAY in profile.compose_files
            else ""
        )
        ocr_url = "http://vllm-ocr:30004/v1"
    elif family == "cpu":
        main_url = "http://llama-cpp:30000/v1"
        embed_url = "http://disabled.invalid/v1"
        rerank_url = ""
        ocr_url = "http://disabled.invalid/v1"
    elif family == "external":
        external = external_environment or {}
        main_url = _external_url(_external_value(external, "OPENAI_BASE_URL"), "OPENAI_BASE_URL")
        embed_raw = _external_value(external, "EMBED_BASE_URL")
        embed_url = _external_url(embed_raw, "EMBED_BASE_URL") if embed_raw else "http://disabled.invalid/v1"
        rerank_raw = _external_value(external, "RERANK_BASE_URL")
        rerank_url = _external_url(rerank_raw, "RERANK_BASE_URL") if rerank_raw else ""
        ocr_raw = _external_value(external, "OCR_BASE_URL")
        ocr_url = _external_url(ocr_raw, "OCR_BASE_URL") if ocr_raw else "http://disabled.invalid/v1"
    else:
        main_url = "http://disabled.invalid/v1"
        embed_url = "http://disabled.invalid/v1"
        rerank_url = ""
        ocr_url = "http://disabled.invalid/v1"

    main = profile.main_model
    router = profile.router_model or main
    embed = profile.embedding_model if profile.features.get("embeddings") else None
    reranker = profile.reranker_model if profile.features.get("reranker") else None
    ocr = None if skip_ocr or not profile.features.get("ocr") else profile.ocr_model
    router_url = main_url if profile.router_shared else "http://vllm-router:30002/v1"
    values: dict[str, str] = {
        "TECHSARA_PROFILE": profile.id,
        "TECHSARA_HARDWARE_PROFILE": profile.hardware_profile_id,
        "TECHSARA_RUNTIME_BACKEND": profile.runtime_backend,
        "TECHSARA_MODEL_CACHE": str(cache),
        "TECHSARA_GENERATED_ENV": str(layout.generated_env),
        "TECHSARA_SECRET_ENV": str(layout.secrets_env),
        "TECHSARA_BIND_ADDRESS": bind_address,
        "TECHSARA_PUBLISH_MODEL_PORTS": _bool(publish_model_ports),
        "TECHSARA_CLUSTER_MODE": cluster_mode,
        "TECHSARA_CLUSTER_REASON": cluster.reason,
        "OPENAI_BASE_URL": main_url,
        "MAIN_MODEL": main.api_model_id if main else "disabled",
        "ROUTER_BASE_URL": router_url,
        "ROUTER_MODEL": router.api_model_id if router else "disabled",
        "AGENT_BASE_URL": router_url,
        "AGENT_MODEL": router.api_model_id if router else "disabled",
        "VISION_BASE_URL": main_url,
        "VISION_MODEL": main.api_model_id if main else "disabled",
        "EMBED_BASE_URL": embed_url,
        "EMBED_VIA": embed_url,
        "EMBED_MODEL": embed.api_model_id if embed else "disabled",
        "OCR_BASE_URL": ocr_url,
        "OCR_MODEL": ocr.api_model_id if ocr else "disabled",
        "OCR_ENABLED": _bool(bool(ocr and profile.features.get("ocr"))),
        "RERANK_BACKEND": ("remote" if rerank_url else "inprocess") if reranker else "disabled",
        "RERANK_ENABLED": _bool(bool(reranker)),
        "RERANK_BASE_URL": rerank_url,
        "RERANK_MODEL": reranker.api_model_id if reranker else "disabled",
        "MODEL_MAX_CONTEXT": str(context),
        "DEFAULT_MAX_CONTEXT": str(context),
        "REPORT_MAX_CONTEXT": str(context),
        "MODEL_CONCURRENCY": str(profile.concurrency),
        "SEARCH_ENABLED": _bool(search_enabled),
        "SEARCH_PROVIDER": search_provider,
        "SEARXNG_URL": (
            "http://searxng:8080"
            if search_enabled and search_provider == "searxng"
            else ""
        ),
        "MAIN_STARTUP_ARGUMENTS": shlex.join(main.startup_arguments) if main else "",
        "MAIN_GPU_MEMORY_UTILIZATION": {"nvidia-large": "0.82", "nvidia-medium": "0.82", "nvidia-small": "0.85", "nvidia-minimal": "0.82"}.get(profile.hardware_profile_id, "0.35"),
        "EMBED_GPU_MEMORY_UTILIZATION": "0.08",
        # vLLM sizes its KV cache from memory free *at profile time*, and the
        # launcher starts model services sequentially rather than all at once,
        # so each later service profiles against less headroom than the old
        # parallel `docker compose up` gave it. 0.09 was enough for OCR then
        # and is not now: it yielded 0.06 GiB of KV against a 0.47 GiB need.
        "OCR_GPU_MEMORY_UTILIZATION": "0.14",
        "ROUTER_GPU_MEMORY_UTILIZATION": "0.17",
        "VLLM_SHM_SIZE": "16g",
    }
    # Always emitted so the published-model overlay interpolates deterministically
    # whether or not it is layered in.
    for name in MODEL_PORT_DEFAULTS:
        values[name] = str(resolve_published_port(user_values, name))
    values.update(cluster_values)
    if family == "external":
        for prefix in ("MAIN", "ROUTER", "AGENT", "VISION", "EMBED", "OCR", "RERANKER"):
            values.update(
                _capability_values(
                    prefix, None, enabled=False, context=0, concurrency=0, native_auth=False
                )
            )
        external = external_environment or {}
        external_main = _external_value(external, "MAIN_MODEL") or _external_value(external, "LLM_MODEL")
        if not external_main:
            raise TechSaraError("external-development requires MAIN_MODEL in .env")
        external_router_url = _external_url(
            _external_value(external, "ROUTER_BASE_URL", main_url), "ROUTER_BASE_URL"
        )
        external_router = _external_value(external, "ROUTER_MODEL", external_main)
        values.update(
            OPENAI_BASE_URL=main_url,
            MAIN_MODEL=external_main,
            ROUTER_BASE_URL=external_router_url,
            ROUTER_MODEL=external_router,
            AGENT_BASE_URL=_external_url(_external_value(external, "AGENT_BASE_URL", external_router_url), "AGENT_BASE_URL"),
            AGENT_MODEL=_external_value(external, "AGENT_MODEL", external_router),
            VISION_BASE_URL=_external_url(_external_value(external, "VISION_BASE_URL", main_url), "VISION_BASE_URL"),
            VISION_MODEL=_external_value(external, "VISION_MODEL", external_main),
            EMBED_BASE_URL=embed_url,
            EMBED_VIA=embed_url,
            EMBED_MODEL=_external_value(external, "EMBED_MODEL", "disabled"),
            OCR_BASE_URL=ocr_url,
            OCR_MODEL=_external_value(external, "OCR_MODEL", "disabled"),
            RERANK_BASE_URL=rerank_url,
            RERANK_MODEL=_external_value(external, "RERANK_MODEL", "disabled"),
        )
        # Explicit capability settings are non-secret and are copied from the
        # user configuration. Conservative local-chat defaults make a simple
        # fake OpenAI server useful without claiming extensions it did not set.
        for prefix in ("MAIN", "ROUTER", "AGENT", "VISION", "EMBED", "OCR", "RERANKER"):
            for suffix in (
                "PROVIDER", "BACKEND", "ENABLED", "SUPPORTS_CHAT", "SUPPORTS_STREAMING",
                "SUPPORTS_REASONING", "SUPPORTS_TOOL_CALLING", "SUPPORTS_STRUCTURED_OUTPUT",
                "SUPPORTS_VISION", "SUPPORTS_EMBEDDINGS", "SUPPORTS_RERANKING", "SUPPORTS_OCR",
                "SUPPORTS_TOKENIZATION", "REASONING_FIELD", "CONTEXT_LENGTH", "OUTPUT_LIMIT",
                "CONCURRENCY", "EXTRA_BODY_ALLOWED", "REQUIRES_AUTHENTICATION",
            ):
                key = f"{prefix}_{suffix}"
                if key in external:
                    values[key] = _external_value(external, key)
        for prefix in ("MAIN", "ROUTER", "AGENT"):
            defaults = {
                f"{prefix}_PROVIDER": "local-external",
                f"{prefix}_BACKEND": "openai-compatible",
                f"{prefix}_ENABLED": "true",
                f"{prefix}_SUPPORTS_CHAT": "true",
                f"{prefix}_SUPPORTS_STREAMING": "true",
                f"{prefix}_CONTEXT_LENGTH": str(context),
                f"{prefix}_OUTPUT_LIMIT": str(min(8192, max(256, context // 4))),
                f"{prefix}_CONCURRENCY": str(max(1, profile.concurrency)),
                f"{prefix}_REQUIRES_AUTHENTICATION": _bool(bool(external.get("OPENAI_API_KEY"))),
            }
            for key, default in defaults.items():
                if key not in external:
                    values[key] = default
        if embed_raw and values["EMBED_MODEL"] != "disabled":
            for key, default in {
                "EMBED_ENABLED": "true",
                "EMBED_SUPPORTS_EMBEDDINGS": "true",
                "EMBED_PROVIDER": "local-external",
                "EMBED_BACKEND": "openai-compatible",
                "EMBED_REQUIRES_AUTHENTICATION": _bool(bool(external.get("EMBED_API_KEY") or external.get("OPENAI_API_KEY"))),
            }.items():
                if key not in external:
                    values[key] = default
        if rerank_raw and values["RERANK_MODEL"] != "disabled":
            for key, default in {
                "RERANKER_ENABLED": "true",
                "RERANKER_SUPPORTS_RERANKING": "true",
                "RERANKER_PROVIDER": "local-external",
                "RERANKER_BACKEND": "remote",
                "RERANKER_REQUIRES_AUTHENTICATION": _bool(bool(external.get("RERANK_API_KEY"))),
                "RERANK_BACKEND": "remote",
                "RERANK_ENABLED": "true",
            }.items():
                if key not in external:
                    values[key] = default
    role_models = {
        "MAIN": main,
        "ROUTER": router,
        "AGENT": router,
        "VISION": main if profile.features.get("vision") else None,
        "EMBED": embed,
        "OCR": ocr,
        "RERANKER": reranker,
    }
    if family != "external":
        for prefix, model in role_models.items():
            role_context = min(context, model.context_limit) if model else 0
            values.update(_capability_values(prefix, model, enabled=model is not None, context=role_context, concurrency=profile.concurrency, native_auth=native and model is not None and model.endpoint_type != "in-process"))

    for key, model in (
        ("MAIN_MODEL_CONTAINER_PATH", main),
        ("ROUTER_MODEL_CONTAINER_PATH", router),
        ("EMBED_MODEL_CONTAINER_PATH", embed),
        ("RERANKER_MODEL_CONTAINER_PATH", reranker),
        ("OCR_MODEL_CONTAINER_PATH", ocr),
    ):
        if model:
            install = install_map.get((model.id, model.revision))
            if install and (install.ready or (allow_planned and install.status == "planned")):
                values[key] = _container_path(cache, install, model, allow_planned=allow_planned)
    return values
