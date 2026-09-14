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

NO WALL CLOCK (no-timeout design, 2026-09-13). The per-request wall clock
this module used to size (`wall_clock_for`, up to PUBLIC_API_GEN_WALL_CLOCK_S
= 6 h) is DELETED: liveness comes from engine evidence (`liveness.py`), and a
generation that crosses a deploy is resumed (`durable.py`). The retired
settings PUBLIC_API_GEN_WALL_CLOCK_S, PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S and
PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S are ignored (T1's config logs one
warning each). `GenerationPlan.wall_clock_s` stays as 0.0 — meaning "none" —
only so a reader that still logs it (router metadata) keeps working.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..config import settings
from . import errors, models, registry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileInputs:
    """What files and audio parts add to one plan (files-hookup, 2026-09-13).

    Built by `publicapi/file_inputs.py` from `apifiles.service.prepare`, so this
    module never imports the Files package. Two states:

    * `pending=True` — the request carries file parts that are not resolved
      yet (the plan made before admission). The image rules that depend on how
      many images the files hold (OCR's exactly-one, the per-model maximum) are
      left to the plan made after resolution; everything else is decided as
      for any request.
    * resolved — `messages` are the engine messages with each file's blocks
      spliced where its part was (`service.splice_messages`), `tokens` the
      file context's estimated tokens, `images` the images the files add.

    HOW FILE TEXT IS COUNTED (senior fix 2026-09-14; the first hookup counted
    it at the ESTIMATE, which the review showed a digit-dense file games DOWN:
    120,000 random digits estimated 40,001 tokens, planned `main.extended`
    where the same text typed in planned `main.long`, and a numeric CSV in
    `full` mode passed the 1M input ceiling).

    * `measured_tokens` — the engine's EXACT `/tokenize` count of the file
      text plus the image estimates (`FileRun.planning_inputs`). Used for the
      hard input ceiling, the gate footprint and the output clamp. Exact, so it
      can be gamed in neither direction, and a 100,000-token prose file is
      100,000 tokens — not the ~300,000 of its byte bound.
    * `bounded_tokens` — the file text at its UTF-8 byte length (never below
      `tokens`). Used in place of the count when the engine could not count:
      the same byte rule caller-typed text keeps, so a failure to count can
      only make a request wait for a larger gate or be refused, never slip an
      oversized prompt into a shared lane.
    * `tokens` — the context builder's estimate. Only the soft input-TPM
      reservation, which settles to the measured count at the end.

    Caller-typed text keeps the byte bound for the ceiling and the gate,
    exactly as before.
    """

    messages: Optional[List[Dict[str, Any]]] = None
    tokens: int = 0
    bounded_tokens: int = 0
    images: int = 0
    pending: bool = False
    measured_tokens: Optional[int] = None

    def ceiling_tokens(self) -> int:
        """The file text for the hard ceiling and the gate: the engine's count,
        else the byte bound (never below the estimate)."""
        if self.measured_tokens is not None:
            return max(0, int(self.measured_tokens))
        return max(0, int(self.bounded_tokens), int(self.tokens))

    def clamp_tokens(self) -> int:
        """The file text for the output clamp: the count when there is one."""
        if self.measured_tokens is not None:
            return max(0, int(self.measured_tokens))
        return max(0, int(self.tokens))


#: The plan made before a request's files are resolved.
FILES_PENDING = FileInputs(pending=True)


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
    #: RETIRED (2026-09-13): always 0.0, meaning no wall clock.
    wall_clock_s: float
    temperature: float
    clamped: bool
    #: The capacity gate this generation must hold, or None. techsara-35b:
    #: decided by the PLANNED OUTPUT only (`main_gate_for`); at or under the
    #: default output ceiling the shared admission lanes alone decide, as
    #: before 2026-09-13 — including a long prompt, which is admission's LONG
    #: lane on the exact count.
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


# ------------------------------------------------------------ transport --


#: The transport check below logs once per process, not once per request.
_TRANSPORT_CHECKED = False


def transport_timeout_is_sufficient() -> bool:
    """THE LLM_REQUEST_TIMEOUT INVARIANT, restated for `/v1` (2026-09-13).

    Every `/v1` generation is a STREAMED engine call, so the httpx read
    timeout bounds the longest SILENCE between two chunks — and the longest
    legitimate silence on the main engine is a full-window prefill (~800 s
    measured at 950k tokens, 2026-09-12). Once llm.py accepts
    `read_timeout_s=None` (T1) the public path has no read timeout at all and
    this check is moot; until then a read timeout shorter than 900 s kills a
    legitimate long-context request inside the HTTP client. Logged as an
    error once; never a refusal, because the operator's setting is the thing
    to fix.
    """
    global _TRANSPORT_CHECKED
    timeout = float(getattr(settings, "llm_request_timeout", 0) or 0)
    allowance = 900.0
    sufficient = timeout <= 0 or timeout >= allowance
    if not sufficient and not _TRANSPORT_CHECKED:
        log.error(
            "LLM_REQUEST_TIMEOUT (%.0fs) is shorter than a full-window prefill "
            "(%.0fs): a long-context /v1 request will be cut by the HTTP read timeout "
            "during its prefill until llm.py accepts read_timeout_s=None",
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


def main_solo_output_tokens() -> int:
    """PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS (800,000): above this PLANNED output
    a techsara-35b generation takes `main.long`, one at a time.

    WHY A NUMBER OF OUTPUT TOKENS (integration 2026-09-13). The main engine's
    KV pool holds 1,663,201 tokens (vllm:cache_config_info, 2026-09-13): two
    answers of more than ~800,000 tokens are the whole pool, with nothing left
    for a chat turn. admission's KV budget already never admits two at the
    default ADMISSION_KV_RESERVE_FRACTION (0.35); this rule does not depend on
    that setting, and it is decided before the status line, so the second one
    is a real 503 rather than a lane wait."""
    return max(1, registry.setting_int("PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS", 800_000))


def main_gate_for(planned_max_output_tokens: int) -> Optional[str]:
    """The main-engine gate for a techsara-35b answer of this planned size.

    BY THE PLANNED OUTPUT, NEVER BY THE PROMPT (integration 2026-09-13). The
    previous rule charged `main.long` on input + output with the input at its
    UTF-8 byte bound, so any document over ~123 KB — 35-47k real tokens of
    prose, a NORMAL prompt for admission's exact count — went through the
    one-at-a-time gate even at the default 8,192-token output, and a single
    1M-output job refused every such document for its whole life (rereview
    probe P7/P9: 503 after 0.5 s). The prompt's size is admission's decision,
    on the exact /tokenize count (app/admission.py SIZING), in its LONG lane —
    one at a time, KV-charged, chat first — which a digit prompt cannot game
    either: /tokenize counts every digit."""
    planned = int(planned_max_output_tokens)
    if planned > main_solo_output_tokens():
        return "main.long"
    if planned > main_extended_output_tokens():
        return "main.extended"
    return None


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
    files: Optional[FileInputs] = None,
) -> GenerationPlan:
    """Decide one generation, or raise the 400 that says why it cannot run.

    Raises `ApiError`: 400 `invalid_request_error` (endpoint, image rules,
    `max_output_tokens`) or 400 `context_length_exceeded`. Never a 413 — the
    text rule was applied when the body was parsed.

    `files` (2026-09-13): see `FileInputs` — pending before resolution, the
    spliced messages and the file counts after.
    """
    check_endpoint(model, endpoint)
    if model.engine == registry.ENGINE_MAIN and not _TRANSPORT_CHECKED:
        transport_timeout_is_sufficient()

    pending = bool(files is not None and files.pending)
    file_images = 0 if files is None or pending else max(0, int(files.images))
    images = request_model.image_count() + file_images
    if images and not model.vision:
        raise errors.invalid_request(
            f"The model `{model.id}` does not accept image input.", param="input"
        )
    if model.ocr and images != registry.OCR_IMAGES_PER_REQUEST and not pending:
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
    #: The caller's own turns, counted as before; file text is added below
    #: (FileInputs, HOW FILE TEXT IS COUNTED): the engine's count or the byte
    #: bound for the ceiling, the gate and the clamp; the estimate for the TPM.
    caller_messages = messages
    file_tokens = 0
    file_ceiling = 0
    file_clamp = 0
    if files is not None and not pending and files.messages is not None:
        messages = [dict(message) for message in files.messages]
        file_tokens = max(0, int(files.tokens))
        file_ceiling = files.ceiling_tokens()
        file_clamp = files.clamp_tokens()

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
        # The engine window IS the public window: the spliced messages, image
        # parts included, at their bound (an OCR request's file is one image).
        bounded = upper_bound_with_image_bound(messages, registry.OCR_TOKENS_PER_IMAGE_BOUND)
    else:
        bounded = int(context.upper_bound_messages(caller_messages)) + file_ceiling
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
    caller_estimate = int(context.estimate_messages(caller_messages))
    estimated = caller_estimate + file_tokens
    if model.clamp_basis == registry.CLAMP_UPPER_BOUND:
        counted = bounded
    else:
        counted = caller_estimate + file_clamp
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

    # The ROUTER gate's token charge is taken at the byte bound, never the
    # estimate (adversarial review 2026-09-13): the Qwen pre-tokenizer
    # isolates every digit, and four digit prompts the gate charged 24,032
    # tokens really demanded 56,028, over the whole router pool. A caller
    # cannot push the bound below the truth; the clamp above still uses the
    # estimate, so no answer is shortened.
    #
    # The MAIN engine's gates are not sized by the prompt at all (see
    # `main_gate_for`): the byte bound over-counts prose up to ~4x, and on a
    # one-at-a-time gate that turned every ~123 KB document into a request
    # that queued behind a 1M-output job. Admission sizes the prompt exactly.
    footprint = int(bounded) + planned
    gate: Optional[str] = None
    weight = 0
    yield_to_chat = False
    if model.engine == registry.ENGINE_MAIN:
        gate = main_gate_for(planned)
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
        wall_clock_s=0.0,
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
    "FILES_PENDING",
    "FileInputs",
    "GenerationPlan",
    "applied_max_output_tokens",
    "check_endpoint",
    "main_extended_output_tokens",
    "main_gate_for",
    "main_solo_output_tokens",
    "plan_generation",
    "reservation_output_tokens",
    "upper_bound_with_image_bound",
]
