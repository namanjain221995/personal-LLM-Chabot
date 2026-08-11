"""Typed model/runtime capability declarations.

The orchestrator talks to several OpenAI-compatible services, but not every
runtime accepts the vLLM/Qwen extensions used by the DGX deployment.  This
module keeps those differences in data rather than scattering backend-name
checks through request code.  It is deliberately dependency-free: importing
capabilities must never import a model framework or touch the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Mapping


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ModelRole(str, Enum):
    MAIN = "main"
    ROUTER = "router"
    AGENT = "agent"
    VISION = "vision"
    EMBED = "embed"
    OCR = "ocr"
    RERANKER = "reranker"


class ReasoningField(str, Enum):
    """Where a streaming backend places reasoning deltas."""

    NONE = "none"
    AUTO = "auto"
    REASONING = "reasoning"
    REASONING_CONTENT = "reasoning_content"


class RerankerBackend(str, Enum):
    INPROCESS = "inprocess"
    REMOTE = "remote"
    DISABLED = "disabled"

    @classmethod
    def parse(cls, value: object) -> "RerankerBackend":
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().lower()
        try:
            return cls(raw)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"RERANK_BACKEND must be one of: {allowed}; got {value!r}") from exc


@dataclass(frozen=True)
class CapabilityDefaults:
    """Role-specific defaults used before environment overrides."""

    provider: str = "local"
    backend: str = "vllm"
    enabled: bool = True
    supports_chat: bool = False
    supports_streaming: bool = False
    supports_reasoning: bool = False
    reasoning_field: ReasoningField = ReasoningField.NONE
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supports_vision: bool = False
    supports_embeddings: bool = False
    supports_reranking: bool = False
    supports_ocr: bool = False
    supports_tokenization: bool = False
    context_length: int = 0
    output_limit: int = 0
    concurrency: int = 1
    extra_body_arguments: tuple[str, ...] = ()
    requires_authentication: bool = False


@dataclass(frozen=True)
class ModelCapabilities:
    """Capabilities of one configured model role.

    ``extra_body_arguments`` contains top-level OpenAI ``extra_body`` keys the
    backend accepts.  For example, DGX vLLM allows
    ``chat_template_kwargs``; a native OpenAI-compatible server can advertise
    an empty set and will never receive that extension.
    """

    role: ModelRole
    model_id: str
    provider: str
    backend: str
    enabled: bool
    supports_chat: bool
    supports_streaming: bool
    supports_reasoning: bool
    reasoning_field: ReasoningField
    supports_tool_calling: bool
    supports_structured_output: bool
    supports_vision: bool
    supports_embeddings: bool
    supports_reranking: bool
    supports_ocr: bool
    supports_tokenization: bool
    context_length: int
    output_limit: int
    concurrency: int
    extra_body_arguments: frozenset[str]
    requires_authentication: bool

    def __post_init__(self) -> None:
        if self.context_length < 0:
            raise ValueError(f"{self.role.value} context length must be >= 0")
        if self.output_limit < 0:
            raise ValueError(f"{self.role.value} output limit must be >= 0")
        if self.concurrency < 1:
            raise ValueError(f"{self.role.value} concurrency must be >= 1")

    def allows_extra_body(self, argument: str) -> bool:
        return argument in self.extra_body_arguments or "*" in self.extra_body_arguments

    def as_dict(self) -> dict:
        """JSON-safe public description for health/diagnostic output."""
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "backend": self.backend,
            "enabled": self.enabled,
            "supports_chat": self.supports_chat,
            "supports_streaming": self.supports_streaming,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_field": self.reasoning_field.value,
            "supports_tool_calling": self.supports_tool_calling,
            "supports_structured_output": self.supports_structured_output,
            "supports_vision": self.supports_vision,
            "supports_embeddings": self.supports_embeddings,
            "supports_reranking": self.supports_reranking,
            "supports_ocr": self.supports_ocr,
            "supports_tokenization": self.supports_tokenization,
            "context_length": self.context_length,
            "output_limit": self.output_limit,
            "concurrency": self.concurrency,
            "extra_body_arguments": sorted(self.extra_body_arguments),
            "requires_authentication": self.requires_authentication,
        }


@dataclass(frozen=True)
class ModelCapabilityRegistry:
    main: ModelCapabilities
    router: ModelCapabilities
    agent: ModelCapabilities
    vision: ModelCapabilities
    embed: ModelCapabilities
    ocr: ModelCapabilities
    reranker: ModelCapabilities


def _bool_env(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean; got {raw!r}")


def _int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def _reasoning_field(
    environ: Mapping[str, str], name: str, default: ReasoningField
) -> ReasoningField:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower().replace("-", "_")
    try:
        return ReasoningField(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ReasoningField)
        raise ValueError(f"{name} must be one of: {allowed}; got {raw!r}") from exc


def capabilities_from_env(
    role: ModelRole,
    model_id: str,
    defaults: CapabilityDefaults,
    *,
    environ: Mapping[str, str] | None = None,
    enabled_env: str | None = None,
    backend_env: str | None = None,
    enabled_override: bool | None = None,
    backend_override: str | None = None,
) -> ModelCapabilities:
    """Resolve one typed capability record from ``<ROLE>_*`` variables.

    ``enabled_env`` and ``backend_env`` support established names such as
    ``OCR_ENABLED`` and ``RERANK_BACKEND`` while every other field follows the
    regular role prefix (``MAIN_SUPPORTS_REASONING``, etc.).
    """
    env = os.environ if environ is None else environ
    prefix = role.value.upper()

    def text(name: str, default: str) -> str:
        raw = env.get(name)
        return default if raw is None or not raw.strip() else raw.strip()

    enabled_name = enabled_env or f"{prefix}_ENABLED"
    selected_backend_name = backend_env or f"{prefix}_BACKEND"
    supports_reasoning = _bool_env(
        env, f"{prefix}_SUPPORTS_REASONING", defaults.supports_reasoning
    )
    reasoning_field = _reasoning_field(
        env, f"{prefix}_REASONING_FIELD", defaults.reasoning_field
    )
    if not supports_reasoning:
        reasoning_field = ReasoningField.NONE

    extra_raw = env.get(f"{prefix}_EXTRA_BODY_ALLOWED")
    if extra_raw is None:
        extra_body = frozenset(defaults.extra_body_arguments)
    else:
        extra_body = frozenset(
            item.strip() for item in extra_raw.split(",") if item.strip() and item.strip() != "none"
        )

    return ModelCapabilities(
        role=role,
        model_id=model_id,
        provider=text(f"{prefix}_PROVIDER", defaults.provider),
        backend=(
            backend_override
            if backend_override is not None
            else text(selected_backend_name, defaults.backend)
        ),
        enabled=(
            enabled_override
            if enabled_override is not None
            else _bool_env(env, enabled_name, defaults.enabled)
        ),
        supports_chat=_bool_env(env, f"{prefix}_SUPPORTS_CHAT", defaults.supports_chat),
        supports_streaming=_bool_env(
            env, f"{prefix}_SUPPORTS_STREAMING", defaults.supports_streaming
        ),
        supports_reasoning=supports_reasoning,
        reasoning_field=reasoning_field,
        supports_tool_calling=_bool_env(
            env, f"{prefix}_SUPPORTS_TOOL_CALLING", defaults.supports_tool_calling
        ),
        supports_structured_output=_bool_env(
            env,
            f"{prefix}_SUPPORTS_STRUCTURED_OUTPUT",
            defaults.supports_structured_output,
        ),
        supports_vision=_bool_env(
            env, f"{prefix}_SUPPORTS_VISION", defaults.supports_vision
        ),
        supports_embeddings=_bool_env(
            env, f"{prefix}_SUPPORTS_EMBEDDINGS", defaults.supports_embeddings
        ),
        supports_reranking=_bool_env(
            env, f"{prefix}_SUPPORTS_RERANKING", defaults.supports_reranking
        ),
        supports_ocr=_bool_env(env, f"{prefix}_SUPPORTS_OCR", defaults.supports_ocr),
        supports_tokenization=_bool_env(
            env, f"{prefix}_SUPPORTS_TOKENIZATION", defaults.supports_tokenization
        ),
        context_length=_int_env(
            env, f"{prefix}_CONTEXT_LENGTH", defaults.context_length
        ),
        output_limit=_int_env(env, f"{prefix}_OUTPUT_LIMIT", defaults.output_limit),
        concurrency=_int_env(env, f"{prefix}_CONCURRENCY", defaults.concurrency),
        extra_body_arguments=extra_body,
        requires_authentication=_bool_env(
            env,
            f"{prefix}_REQUIRES_AUTHENTICATION",
            defaults.requires_authentication,
        ),
    )
