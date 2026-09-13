"""Everything a `/v1` generation is allowed to be, decided BEFORE it runs.

WHY A PLANNER (2026-09-13). The owner raised the public output ceiling to
1,000,000 tokens — the flagship model's whole context window — and asked for
every chat model TechSara runs on the same two endpoints. Those two decisions
meet in one place: input and output SHARE the window, so how many tokens a
request may generate depends on how long its prompt is, on which engine
answers it, and on how honestly that prompt can be counted before it is
sent. `router.py` and the console playground both plan through this module,
so the two cannot disagree about what a request gets.

THE RESOLUTION ORDER (the architecture review's, and the only order in which
every refusal is a refusal the caller can act on):

1. `requested` = the caller's `max_output_tokens` (Chat Completions:
   `max_tokens` or `max_completion_tokens`), or the model's default (8,192).
   An EXPLICIT value above the model ceiling — or above the project's
   `max_output_tokens` — is a 400: silently clamping a number the caller
   typed would bill for an answer they believe was allowed to be longer.
2. Input over the model's `max_input_tokens`, counted at a bound the prompt
   cannot exceed, is `400 context_length_exceeded`. The ONLY size refusal.
3. CLAMP, NEVER REFUSE: `planned = max(1, min(requested, window - input -
   reserve))`. A caller asking for 1,000,000 tokens with a 300,000-token
   prompt gets the 699,488 that fit, and is told so.
4. APPLIED — what the engine was actually allowed — is reported on the
   terminal event (`applied_max_output_tokens`).

HOW INPUT IS COUNTED, PER MODEL (`PublicModel.clamp_basis`):

* techsara-35b (`exact`): the plan uses `context.estimate_messages`, but the
  engine is sent `requested` and `llm._fit` re-clamps with the engine's own
  `/tokenize` count — so the window is used to the token and never overrun.
* techsara-8b-vision (`estimate`): the public window (24,576) is half the
  engine's served 49,152, so an under-estimate lands inside the engine's real
  room; the planned value is what is sent.
* techsara-ocr (`upper_bound`): the public window IS the engine window, so an
  under-estimate would be an engine 400. Text at its UTF-8 byte length plus
  2,048 tokens per image (measured pages: 487-1,807).

THE WALL CLOCK, PER REQUEST. A 1,000,000-token answer at the measured 71-101
tok/s runs 2.8-3.9 hours — it must not be cut at the chat app's 70 minutes.
`wall_clock_s = min(PUBLIC_API_GEN_WALL_CLOCK_S, max(floor, prefill_allowance
+ planned / min_decode_tps))`: the default 8,192 keeps the chat app's 4,200 s,
and 1,000,000 gets 900 + 20,000 = 20,900 s.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..config import settings
from . import errors, models, registry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationPlan:
    """One request's decided shape. Frozen: nothing in the body may change it
    once it is decided (CONTRACT §8)."""

    model: registry.PublicModel
    messages: List[Dict[str, Any]]
    #: What the caller asked for, or the default when it named nothing.
    requested_max_output_tokens: int
    #: `requested`, clamped to what the window leaves after the input.
    planned_max_output_tokens: int
    #: What the engine is sent: `requested` for techsara-35b (llm._fit
    #: re-clamps exactly), `planned` for the sidecars (applied == planned).
    max_tokens_for_engine: int
    estimated_input_tokens: int
    bounded_input_tokens: int
    footprint_tokens: int
    wall_clock_s: float
    temperature: float
    clamped: bool
    #: The capacity gate this generation must hold, or None (a techsara-35b
    #: request at or under the default output ceiling is gated by the shared
    #: admission lanes alone, as before 2026-09-13).
    gate_engine: Optional[str]
    gate_weight_tokens: int
    yield_to_chat: bool
    explicit_max_output_tokens: bool
    image_count: int

    @property
    def engine(self) -> str:
        return self.model.engine

    @property
    def context_reserve(self) -> int:
        return int(self.model.context_reserve)


# --------------------------------------------------------------- inputs --


def _text_bytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def upper_bound_with_image_bound(messages: List[Dict[str, Any]], per_image: int) -> int:
    """Text at its UTF-8 byte length plus `per_image` per image part — the
    count a prompt cannot exceed on an engine whose public window is its real
    window (a byte-level BPE spends at most one token per byte)."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += _text_bytes(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    total += _text_bytes(part["text"])
                elif isinstance(part, Mapping) and part.get("type") == "image_url":
                    total += int(per_image)
        total += 8
    return total + 8


def _with_ocr_prompt(
    request_model: models.ResponsesRequest, messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Append the text part "OCR" to the image-bearing user turn when the
    caller sent no text at all (engines/ocr.py, 2026-09-11: "document
    parsing" loops garbage; "OCR" reads correctly in a tenth of the time).
    Caller-supplied text is respected as sent — including a prompt that will
    loop, which the documentation warns about."""
    if request_model.has_text():
        return messages
    shaped = [dict(message) for message in messages]
    for message in reversed(shaped):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            message["content"] = [*content, {"type": "text", "text": registry.OCR_DEFAULT_PROMPT}]
            break
    return shaped


# ---------------------------------------------------------- wall clock --


def wall_clock_for(model: registry.PublicModel, planned_max_output_tokens: int) -> float:
    """The per-request generation wall clock (module docstring).

    The floor for techsara-35b is the chat app's GEN_WALL_CLOCK_S (4,200 s on
    this deployment), so a default-sized public request is cut exactly where
    it always was; 600 s for the sidecars.
    """
    ceiling = registry.setting_float("PUBLIC_API_GEN_WALL_CLOCK_S", 21_600.0)
    if model.engine == registry.ENGINE_MAIN:
        floor = float(getattr(settings, "gen_wall_clock_s", 1800.0) or 1800.0)
        prefill = registry.setting_float("PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S", 900.0)
        rate = registry.setting_float("PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S", 50.0)
    else:
        floor = registry.SIDECAR_WALL_CLOCK_FLOOR_S
        prefill = registry.SIDECAR_PREFILL_ALLOWANCE_S
        rate = registry.SIDECAR_MIN_DECODE_TOKENS_PER_S
    rate = max(0.001, float(rate))
    wanted = max(float(floor), float(prefill) + float(planned_max_output_tokens) / rate)
    return float(min(float(ceiling), wanted)) if ceiling > 0 else float(wanted)


#: The transport check below logs once per process, not once per request.
_TRANSPORT_CHECKED = False


def transport_timeout_is_sufficient() -> bool:
    """THE LLM_REQUEST_TIMEOUT INVARIANT, restated for `/v1` (2026-09-13).

    Every `/v1` generation is a STREAMED engine call, so the httpx read
    timeout bounds the longest SILENCE between two chunks, not the whole
    generation — and the longest legitimate silence on the main engine is a
    full-window prefill (878 s measured at 949,915 tokens, 2026-08-29), which
    PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S (900 s) stands for. A read timeout
    shorter than that kills a legitimate 1M-context request inside the HTTP
    client, where it reads as an engine fault. Logged as an error once, the
    first time a main-engine generation is planned; never a refusal, because
    the operator's setting is the thing to fix.
    """
    global _TRANSPORT_CHECKED
    timeout = float(getattr(settings, "llm_request_timeout", 0) or 0)
    allowance = registry.setting_float("PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S", 900.0)
    sufficient = timeout <= 0 or timeout >= allowance
    if not sufficient and not _TRANSPORT_CHECKED:
        log.error(
            "LLM_REQUEST_TIMEOUT (%.0fs) is shorter than the public prefill allowance "
            "(%.0fs): a long-context /v1 request will be cut by the HTTP read timeout "
            "during its prefill",
            timeout, allowance,
        )
    _TRANSPORT_CHECKED = True
    return sufficient


def main_extended_output_tokens() -> int:
    """PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS: above this PLANNED output, a
    techsara-35b generation takes the `main.extended` gate (2 at a time).

    Defaults to the public default output (8,192), which was also the public
    CEILING before 2026-09-13 — so a request of a size public callers could
    already send holds an admission slot no longer than it could then (~115 s
    at 71 tok/s), and only the new, hour-long answers are gated. Adversarial
    review 2026-09-13: without this, ten public 130,000-token answers held all
    ten NORMAL admission slots for ~22 minutes each and refused a chat turn."""
    return max(
        1,
        registry.setting_int(
            "PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS", registry.public_default_max_output_tokens()
        ),
    )


def main_long_footprint_tokens() -> int:
    """PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS: above this input + planned
    output, a techsara-35b generation takes the one-at-a-time `main.long`
    gate. Defaults to the chat app's own long-admission threshold (131,072)."""
    default = int(getattr(settings, "admission_long_threshold_tokens", 131_072) or 131_072)
    return max(1, registry.setting_int("PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS", default))


# -------------------------------------------------------------- the plan --


def check_endpoint(model: registry.PublicModel, path: str) -> None:
    """A permitted model on an endpoint it does not serve is a 400 naming
    `model` — after the 404 decision, so it discloses nothing new."""
    if not model.supports_endpoint(path):
        raise errors.invalid_request(
            registry.unsupported_endpoint_message(model.id, path), param="model"
        )


def plan_generation(
    request_model: models.ResponsesRequest,
    model: registry.PublicModel,
    *,
    project_max_output_tokens: Optional[int] = None,
    project_max_input_tokens: Optional[int] = None,
    endpoint: str = registry.ENDPOINT_RESPONSES,
) -> GenerationPlan:
    """Decide one generation, or raise the 400 that says why it cannot run.

    Raises `ApiError`: 400 `invalid_request_error` (endpoint, image rules,
    `max_output_tokens`) or 400 `context_length_exceeded`. Never a 413 — the
    text rule was applied when the body was parsed.
    """
    check_endpoint(model, endpoint)
    if model.engine == registry.ENGINE_MAIN and not _TRANSPORT_CHECKED:
        transport_timeout_is_sufficient()

    images = request_model.image_count()
    if images and not model.vision:
        raise errors.invalid_request(
            f"The model `{model.id}` does not accept image input.", param="input"
        )
    if model.ocr and images != registry.OCR_IMAGES_PER_REQUEST:
        raise errors.invalid_request(
            f"The model `{model.id}` reads exactly one image per request.", param="input"
        )
    if images > int(model.max_images or 0):
        raise errors.invalid_request(
            f"The model `{model.id}` accepts at most {int(model.max_images)} images per request.",
            param="input",
        )

    messages = request_model.chat_messages()
    if model.ocr:
        messages = _with_ocr_prompt(request_model, messages)

    # 1. requested, against the ceiling (explicit above it: 400).
    ceiling = max(1, int(model.max_output_tokens or registry.public_default_max_output_tokens()))
    if project_max_output_tokens is not None:
        if int(project_max_output_tokens) < 1:
            raise errors.invalid_request(
                "This project may not generate output tokens.", param="max_output_tokens"
            )
        ceiling = min(ceiling, int(project_max_output_tokens))
    default = int(model.default_max_output_tokens or registry.public_default_max_output_tokens())
    requested = request_model.resolve_max_output_tokens(ceiling=ceiling, default=default)
    explicit = request_model.max_output_tokens is not None

    # 2. input over the ceiling — at a bound the prompt cannot exceed, never
    # at the estimate. A hard ceiling decided on the 3-chars-per-token
    # estimate was gameable DOWN (re-verifier probe, 2026-09-13): the Qwen
    # pre-tokenizer isolates every digit, so `"7" * 2900` estimated at 974
    # tokens and reached the engine as ~2,900. A byte-level BPE spends at
    # most one token per UTF-8 byte, so the byte length is a bound no caller
    # can push below the truth. The trade-off, accepted deliberately: prose
    # at 3-4 bytes per token is refused somewhat before the real window is
    # full — a 400 a developer can act on, where a false admission would put
    # an oversized prompt into lanes shared with the chat app. The model's
    # ceiling is narrowed by the project's `max_input_tokens`, where 0 means
    # zero and only None inherits.
    from .. import context

    if model.clamp_basis == registry.CLAMP_UPPER_BOUND:
        bounded = upper_bound_with_image_bound(messages, registry.OCR_TOKENS_PER_IMAGE_BOUND)
    else:
        bounded = int(context.upper_bound_messages(messages))
    limits = []
    if int(model.max_input_tokens or 0) > 0:
        limits.append(int(model.max_input_tokens))
    if project_max_input_tokens is not None:
        limits.append(max(0, int(project_max_input_tokens)))
    if limits and bounded > min(limits):
        raise errors.context_length_exceeded(
            requested=bounded, limit=min(limits), upper_bound=True
        )

    # 3. clamp to what the window leaves.
    if model.clamp_basis == registry.CLAMP_UPPER_BOUND:
        counted = bounded
    else:
        counted = int(context.estimate_messages(messages))
    estimated = int(context.estimate_messages(messages))
    window = int(model.context_window or 0)
    reserve = int(model.context_reserve or 0)
    if window > 0:
        room = window - counted - reserve
        if room < 1 and model.engine != registry.ENGINE_MAIN:
            # No numbers: `counted` is an estimate on the router, and "the
            # input is N tokens" would be a claim a developer debugs against.
            raise errors.context_length_exceeded()
        planned = max(1, min(requested, room))
    else:
        planned = requested
    for_engine = requested if model.engine == registry.ENGINE_MAIN else planned

    # The GATE's size is taken at the byte bound, never the estimate
    # (adversarial review 2026-09-13): the Qwen pre-tokenizer isolates every
    # digit, so a 120,000-digit prompt estimated at 40,008 tokens and slipped
    # under the long threshold with 90,000 tokens of output while really
    # holding 210,007 tokens of KV; and on the router four digit prompts the
    # gate charged 24,032 tokens really demanded 56,028, over the whole pool.
    # A caller cannot push the bound below the truth. The cost — prose counted
    # at up to 4x its real size — only ever makes PUBLIC work wait for its
    # gate; the clamp above still uses the estimate, so no answer is shortened.
    footprint = int(bounded) + planned
    gate: Optional[str] = None
    weight = 0
    yield_to_chat = False
    if model.engine == registry.ENGINE_MAIN:
        if footprint > main_long_footprint_tokens():
            gate = "main.long"
        elif planned > main_extended_output_tokens():
            gate = "main.extended"
    elif model.engine == registry.ENGINE_ROUTER:
        gate, weight = registry.ENGINE_ROUTER, footprint
    elif model.engine == registry.ENGINE_OCR:
        gate, yield_to_chat = registry.ENGINE_OCR, True

    temperature = (
        float(model.default_temperature)
        if request_model.temperature is None
        else float(request_model.temperature)
    )
    return GenerationPlan(
        model=model,
        messages=messages,
        requested_max_output_tokens=int(requested),
        planned_max_output_tokens=int(planned),
        max_tokens_for_engine=int(for_engine),
        estimated_input_tokens=estimated,
        bounded_input_tokens=int(bounded),
        footprint_tokens=int(footprint),
        wall_clock_s=wall_clock_for(model, planned),
        temperature=temperature,
        clamped=bool(planned < requested),
        gate_engine=gate,
        gate_weight_tokens=int(weight),
        yield_to_chat=yield_to_chat,
        explicit_max_output_tokens=explicit,
        image_count=int(images),
    )


def applied_max_output_tokens(
    *,
    engine: str,
    requested: int,
    planned: int,
    context_window: Optional[int],
    reserve: int,
    usage: Optional[Mapping[str, Any]] = None,
    llm_applied: Optional[int] = None,
) -> int:
    """What the engine was ACTUALLY allowed to generate.

    Sidecars: `planned`, exactly — it is the number that was sent.

    techsara-35b: `llm.get_applied_max_tokens()` once llm.py exposes it (the
    value `_fit` computed with the engine's `/tokenize`). Until then the same
    arithmetic `_fit` does, `min(requested, window - prompt - reserve)`, from
    the prompt count the engine REPORTED; and `planned` only when the engine
    reported nothing. This can exceed `planned` when the estimate over-counted
    the prompt — the engine really was allowed more, and the terminal event
    says what is true rather than what was guessed.
    """
    if engine != registry.ENGINE_MAIN:
        return max(1, int(planned))
    if llm_applied is not None and int(llm_applied) > 0:
        return int(llm_applied)
    prompt = None
    if usage:
        prompt = usage.get("prompt_tokens")
    if prompt is not None and context_window:
        return max(1, min(int(requested), int(context_window) - int(prompt) - int(reserve)))
    return max(1, int(planned))


def reservation_output_tokens(plan: GenerationPlan, *, limits_enforced: bool) -> Optional[int]:
    """The OUTPUT reservation `quotas.reserve` is given at admission.

    Enforced: the planned value, as every generation reserved before.
    Not enforced (the default since 2026-09-13): capped at
    PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS, so a three-hour 1,000,000-token job
    does not show +1,000,000 output tokens in today's ledger for its whole
    life; either way the reservation settles to the measured count at the
    end. None when the caller named no budget and the default applied
    unclamped — `quotas` reserves its own default then, as it always did.
    """
    planned = int(plan.planned_max_output_tokens)
    if not plan.explicit_max_output_tokens and not plan.clamped:
        return None
    if limits_enforced:
        return planned
    return min(planned, registry.public_default_max_output_tokens())


__all__ = [
    "GenerationPlan",
    "applied_max_output_tokens",
    "check_endpoint",
    "main_extended_output_tokens",
    "main_long_footprint_tokens",
    "plan_generation",
    "reservation_output_tokens",
    "upper_bound_with_image_bound",
    "wall_clock_for",
]
