"""Model geometry read from a Hugging Face ``config.json``.

Three questions the launcher has to answer before it can honour a user-owned
context window:

* how long a window does the model claim natively (``max_position_embeddings``)
* what does one token of KV cache cost, so a requested window can be checked
  against an explicit ``--kv-cache-memory-bytes`` budget
* what has to be handed to vLLM to serve a window longer than the native one

Everything here is pure: ``read_model_shape`` accepts an already parsed config
so tests never touch a real checkout, and it returns ``None`` rather than
raising on a missing or unfamiliar config so every caller can simply fall back
to today's behaviour.

Two measured facts drive the shapes below (DGX Spark, 2026-08-25):

* ``RadixArk/Qwen3.8-27B-NVFP4`` nests its real configuration under
  ``text_config``.  vLLM 0.26.1rc1 SILENTLY IGNORES a top-level
  ``rope_parameters``/``rope_scaling`` override for this model class - only
  ``{"text_config": {"rope_parameters": ...}}`` reaches ``ModelConfig`` and
  moves ``max_model_len``.  ``rope_override_argument`` therefore always emits
  the nested spelling.
* Only the ``full_attention`` layers hold a paged KV cache.  The 48
  ``linear_attention`` (gated delta net) layers keep a per-sequence state that
  does not grow with the sequence length, so they must not be counted.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import TechSaraError

#: KV cache dtypes that store one byte per element; anything else is 2 bytes.
FP8_KV_CACHE_DTYPES = frozenset({"fp8", "fp8_e4m3", "fp8_e5m2", "fp8_inc"})
#: YaRN beyond 4x is not a supported extension of a trained window.
MAX_YARN_FACTOR = 4.0
#: Fraction of a ``--kv-cache-memory-bytes`` budget that actually becomes paged
#: tokens on a HYBRID model. Measured on the running cluster 2026-08-25: a
#: 16 GiB budget at 16,384 B/token is 1,048,576 tokens of arithmetic but the
#: engine reported a "GPU KV cache size: 933,232 tokens" pool - 89.0% - because
#: the 48 gated-delta-net layers take their per-sequence state from the same
#: budget. 0.88 keeps a hair of margin so the guard never approves a window
#: vLLM then refuses to start with.
HYBRID_KV_USABLE_FRACTION = 0.88
GIB = 1024 ** 3
#: Keys that identify the section of a config holding the real model geometry.
_GEOMETRY_KEYS = ("max_position_embeddings", "num_hidden_layers", "layer_types")


@dataclass(frozen=True)
class ModelShape:
    """The parts of a model config the context/KV arithmetic depends on."""

    native_context: int
    full_attention_layers: int
    num_key_value_heads: int
    head_dim: int
    rope_parameters: dict[str, Any]
    #: True when the geometry (and therefore the rope) lives under
    #: ``text_config`` - the only spelling vLLM honours for that model class.
    nested: bool
    #: Total decoder layers. Greater than ``full_attention_layers`` on a hybrid
    #: model, where the remaining layers keep a recurrent state instead.
    total_layers: int = 0

    @property
    def hybrid(self) -> bool:
        """True when some layers keep a recurrent state instead of a KV cache.

        Those layers (gated delta net here) allocate a per-SEQUENCE conv/SSM
        state out of the same ``--kv-cache-memory-bytes`` budget, so the paged
        token pool is smaller than the budget divided by bytes-per-token.
        """
        return self.total_layers > self.full_attention_layers > 0


def _positive_int(section: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = section.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def _geometry_section(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    """The config section carrying the geometry, and whether it was nested."""
    nested = config.get("text_config")
    if isinstance(nested, Mapping) and any(key in nested for key in _GEOMETRY_KEYS):
        return nested, True
    return config, False


def read_model_shape(
    model_dir: Path | None = None, *, config: Mapping[str, Any] | None = None
) -> ModelShape | None:
    """Read ``model_dir/config.json`` (or an injected ``config``).

    Returns ``None`` for anything unreadable, unparsable, or too unfamiliar to
    reason about: an unknown model is not an error, it just means the launcher
    keeps the profile-selected window and emits no override.
    """
    document: Any = config
    if document is None:
        if model_dir is None:
            return None
        try:
            document = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
    if not isinstance(document, Mapping):
        return None

    section, nested = _geometry_section(document)
    native_context = _positive_int(section, "max_position_embeddings")
    if not native_context:
        return None

    layer_types = section.get("layer_types")
    full_attention = 0
    if isinstance(layer_types, (list, tuple)):
        full_attention = sum(1 for item in layer_types if str(item) == "full_attention")
    total_layers = _positive_int(section, "num_hidden_layers", "num_layers")
    if not full_attention:
        # No layer_types (a plain transformer) or an unfamiliar spelling: every
        # layer is assumed to hold a paged KV cache, which never under-counts.
        full_attention = _positive_int(section, "num_hidden_layers", "num_layers")
    if not full_attention:
        return None

    heads = _positive_int(section, "num_attention_heads", "num_heads")
    key_value_heads = _positive_int(section, "num_key_value_heads") or heads
    if not key_value_heads:
        return None

    head_dim = _positive_int(section, "head_dim")
    if not head_dim:
        hidden = _positive_int(section, "hidden_size", "d_model")
        head_dim = hidden // heads if hidden and heads else 0
    if not head_dim:
        return None

    rope = section.get("rope_parameters")
    if not isinstance(rope, Mapping):
        rope = section.get("rope_scaling")
    rope_parameters = dict(rope) if isinstance(rope, Mapping) else {}

    return ModelShape(
        native_context=native_context,
        full_attention_layers=full_attention,
        num_key_value_heads=key_value_heads,
        head_dim=head_dim,
        rope_parameters=rope_parameters,
        nested=nested,
        total_layers=total_layers,
    )


def kv_bytes_per_token(
    shape: ModelShape, *, tensor_parallel_size: int = 1, kv_cache_dtype: str = "fp8"
) -> int:
    """Bytes of paged KV cache one token costs on ONE node.

    ``2`` for the K and V halves, once per layer that actually pages a cache,
    over the key/value heads this node owns after tensor parallelism.

    Verified against the running cluster on 2026-08-25: 16 full-attention
    layers, 4 KV heads, head_dim 256, fp8 -> 32768 B/token at TP=1 and
    16384 B/token at TP=2, where a 16 GiB budget reported a
    "GPU KV cache size: 933,232 tokens" pool (15.3 GiB usable).
    """
    parallel = max(1, int(tensor_parallel_size))
    element = 1 if str(kv_cache_dtype or "").strip().lower() in FP8_KV_CACHE_DTYPES else 2
    heads = max(1, int(shape.num_key_value_heads) // parallel)
    return 2 * int(shape.full_attention_layers) * heads * int(shape.head_dim) * element


def kv_pool_tokens(
    memory_gib: int, bytes_per_token: int, *, usable_fraction: float = 1.0
) -> int:
    """How many tokens a ``--kv-cache-memory-bytes`` budget really holds.

    ``usable_fraction`` is what survives after the engine takes its own share
    of the budget - see :data:`HYBRID_KV_USABLE_FRACTION`. Left at 1.0 the
    answer is the naive arithmetic, which on a hybrid model is ~12% too
    generous and would approve a window the engine refuses at start-up.
    """
    if bytes_per_token <= 0:
        return 0
    return int(int(memory_gib) * GIB * float(usable_fraction)) // int(bytes_per_token)


def kv_usable_fraction(shape: "ModelShape | None") -> float:
    """The fraction of a KV budget this model's geometry leaves for tokens."""
    return HYBRID_KV_USABLE_FRACTION if shape is not None and shape.hybrid else 1.0


def kv_gib_for_tokens(
    tokens: int, bytes_per_token: int, *, usable_fraction: float = 1.0
) -> int:
    """The smallest whole-GiB budget that holds ``tokens`` (rounded up)."""
    if bytes_per_token <= 0 or usable_fraction <= 0:
        return 0
    return math.ceil(int(tokens) * int(bytes_per_token) / (GIB * float(usable_fraction)))


def yarn_factor(requested: int, native: int) -> float:
    """The YaRN scaling factor that reaches ``requested`` from ``native``.

    Rounded UP to two decimals so the served window is never a few hundred
    tokens short of what the user asked for, and never below 1.0.
    """
    if int(native) <= 0:
        raise TechSaraError("the model's native context length is unknown; YaRN cannot be sized")
    factor = math.ceil(int(requested) / int(native) * 100) / 100
    if factor <= 1.0:
        return 1.0
    if factor > MAX_YARN_FACTOR:
        raise TechSaraError(
            f"a {int(requested):,}-token window needs a YaRN factor of {factor:.2f}; this model is natively "
            f"{int(native):,} tokens and {MAX_YARN_FACTOR:.0f}x ({int(native) * int(MAX_YARN_FACTOR):,} tokens) "
            "is the supported ceiling"
        )
    return factor


def rope_override_argument(shape: ModelShape, factor: float) -> str:
    """``--hf-overrides '<json>'`` turning the model's rope into YaRN.

    Every existing ``rope_parameters`` key is preserved (mrope_section and
    mrope_interleaved in particular: dropping them would rebuild the rope
    embedding with the wrong layout), and the JSON is compact and
    single-quoted so it survives Compose interpolation and shell splitting as
    ONE argv element.  Empty when no extension is needed.
    """
    if factor <= 1.0:
        return ""
    parameters = dict(shape.rope_parameters)
    parameters["rope_type"] = "yarn"
    parameters["factor"] = factor
    parameters["original_max_position_embeddings"] = int(shape.native_context)
    payload = {"text_config": {"rope_parameters": parameters}}
    return "--hf-overrides '" + json.dumps(payload, separators=(",", ":")) + "'"
