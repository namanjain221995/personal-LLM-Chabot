"""Which models `/v1` exposes — the code-level catalogue of CONTRACT §15.

THE POINT OF THIS FILE IS WHAT IT REFUSES TO DO. Infrastructure cannot expose
a model by appearing in Compose, and neither can a database row: the public
catalogue is the six entries built below and nothing else. A `public_models`
row (SCHEMA-V34) may only take a model AWAY; a row naming an id this file does
not declare is ignored. That asymmetry is the whole design — an admin mistake,
a stray migration or a compromised console can narrow the public surface,
never widen it.

SIX MODELS SINCE 2026-09-13 (owner request: "offer EVERY model TechSara
runs"). Until then this file declared only `techsara-35b` and called the
router, OCR, embeddings and reranker "internal engines with no path from
/v1". They still have no path of their OWN: every engine stays unreachable
from outside, and a public id reaches one only through an authenticated,
metered `/v1` route that proxies to it (CONTRACT §11). What changed is that
the catalogue names them — under TechSara brand ids, never under the
checkpoint name or the address behind them.

WHAT THE GUARD MEANS NOW. `internal` is the served model NAME of the engine an
entry resolves to, read from `settings` when the entry is built. Two things
must never be true of it, and `guard_internal_target()` raises at
construction when either is:

* it is an ADDRESS — a URL, a `host:port`, an IP, or the bare hostname of any
  engine this deployment talks to. A public model names a model, and the URL
  it is served from is looked up in `publicapi/engines.py` at call time, so a
  copy-paste of `OCR_BASE_URL` into this file cannot become something a
  caller's request is aimed at;
* it is ANOTHER engine's checkpoint. `techsara-35b` resolving to the router's
  8B, or the embeddings model's name ending up under the OCR key, is the
  one-env-var-away accident the OCR move to the worker (2026-09-09) showed is
  real.

And `guard_public_id()` holds the other side: a public id is TechSara
vocabulary (`techsara-…`), never an internal name, URL or host.

READ AT CALL TIME, NEVER CAPTURED AT IMPORT. The main model has been swapped
under a running deployment more than once (27B → 35B-A3B, 2026-08-29), and a
module-level constant would keep naming the old checkpoint in the usage
ledger and in `/v1/models` until someone noticed. The ceilings come from
`settings` and from each engine's SERVED window (narrow-only, see
`note_served_window`), so the documented ceiling and the served one cannot
disagree in the direction that produces an engine 400.

THIS MODULE IMPORTS ONLY `..config`. The api_contract CI job imports it to
build the OpenAPI document on a runner with no engine stack, so the engine
probe lives in `engines.py` and reports back into `note_served_window`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from ..config import _float, _int, settings

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ ids --

#: The six public ids (CONTRACT §15). A product decision, not a deployment
#: one: adding an id is a contract change, a documentation change and a scope
#: review, so it happens here in a reviewed diff. The ORDER is the order of
#: `/v1/models`, and the first is the flagship the OpenAPI examples use.
TECHSARA_35B = "techsara-35b"
TECHSARA_8B_VISION = "techsara-8b-vision"
TECHSARA_OCR = "techsara-ocr"
TECHSARA_EMBED = "techsara-embed"
TECHSARA_RERANK = "techsara-rerank"
TECHSARA_WHISPER = "techsara-whisper"

PUBLIC_MODEL_IDS: Tuple[str, ...] = (
    TECHSARA_35B,
    TECHSARA_8B_VISION,
    TECHSARA_OCR,
    TECHSARA_EMBED,
    TECHSARA_RERANK,
    TECHSARA_WHISPER,
)

#: The closed set of engine keys a public model may resolve to. `engines.py`
#: maps each to a base URL and served model name at call time; nothing else
#: can be named.
ENGINE_MAIN = "main"
ENGINE_ROUTER = "router"
ENGINE_OCR = "ocr"
ENGINE_EMBED = "embed"
ENGINE_RERANK = "rerank"
ENGINE_ASR = "asr"
ENGINE_KEYS: Tuple[str, ...] = (
    ENGINE_MAIN,
    ENGINE_ROUTER,
    ENGINE_OCR,
    ENGINE_EMBED,
    ENGINE_RERANK,
    ENGINE_ASR,
)

KIND_CHAT = "chat"
KIND_EMBEDDING = "embedding"
KIND_RERANK = "rerank"
KIND_TRANSCRIPTION = "transcription"
KINDS: Tuple[str, ...] = (KIND_CHAT, KIND_EMBEDDING, KIND_RERANK, KIND_TRANSCRIPTION)

ENDPOINT_RESPONSES = "/v1/responses"
ENDPOINT_CHAT_COMPLETIONS = "/v1/chat/completions"
ENDPOINT_EMBEDDINGS = "/v1/embeddings"
ENDPOINT_RERANK = "/v1/rerank"
ENDPOINT_TRANSCRIPTIONS = "/v1/audio/transcriptions"
_CHAT_ENDPOINTS = (ENDPOINT_RESPONSES, ENDPOINT_CHAT_COMPLETIONS)

#: How a generation's input is counted when its output is clamped to the
#: remaining window (see `planning.py`).
CLAMP_EXACT = "exact"  # llm._fit re-clamps with the engine's /tokenize count
CLAMP_ESTIMATE = "estimate"  # the engine's real window absorbs an under-estimate
CLAMP_UPPER_BOUND = "upper_bound"  # public window == engine window: never under-count

STATUS_AVAILABLE = "available"
STATUS_NOT_CONFIGURED = "not_configured"

#: `context.MIN_OUTPUT_TOKENS`, repeated here because this module may import
#: only `..config` (a test pins that the two agree). `llm._fit` drops turns or
#: clips messages when fewer than this many completion tokens fit, which `/v1`
#: must never do silently — so the main model's input ceiling leaves room for
#: it and a public prompt is never trimmed.
MIN_OUTPUT_TOKENS = 256

# ------------------------------------------------ per-engine public numbers --
#
# Constants, not settings, where the architecture review said so: they are
# facts about the engines' behaviour (measured) rather than dials an operator
# is expected to turn. The dials are the PUBLIC_API_* settings read below.

#: 2026-09-09: the main model is a vision-language model; sixteen images is
#: ~8.4k tokens at 896 px, far inside the window and inside the 20 MiB body.
MAIN_MAX_IMAGES = 16
#: The router's window is small (49,152 served) and shared with chat routing.
ROUTER_MAX_IMAGES = 8
#: Unlimited-OCR reads ONE page per call; the chat app's read_images does the
#: same, one request per image (engines/ocr.py).
OCR_IMAGES_PER_REQUEST = 1
#: Measured OCR prompts were 487-1,807 tokens per page (2026-09-11). The OCR
#: public window IS the engine window, so an image is counted at a bound no
#: page reached rather than at an estimate an engine 400 would punish.
OCR_TOKENS_PER_IMAGE_BOUND = 2048
#: What the chat app sends Unlimited-OCR, and the only prompt shape that
#: reads correctly: "document parsing" loops garbage (engines/ocr.py,
#: 2026-09-11). Appended only when the caller sent no text at all.
OCR_DEFAULT_PROMPT = "OCR"
SIDECAR_CONTEXT_RESERVE = 64
EMBEDDING_DIMENSIONS = 1024  # Qwen3-Embedding-0.6B config.json hidden_size
TRANSCRIPTION_RESPONSE_FORMATS: Tuple[str, ...] = ("json", "text", "verbose_json")

#: How long a served-window report from `engines.served_window` is believed.
SERVED_WINDOW_TTL_S = 300.0


class InternalTargetError(RuntimeError):
    """Something tried to make an internal engine publicly reachable, or to
    publish an internal identity under a public id."""


# ------------------------------------------------------------- settings --


def setting_int(name: str, default: int) -> int:
    """A PUBLIC_API_* integer, from `settings` when config.py names it and
    from the environment otherwise — with config.py's own `_int` rule (blank
    means the default), so the value does not change the day the attribute is
    added to `Settings`. Read on every call: a test's monkeypatch and an
    operator's restart both take effect without a re-import."""
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return int(value)
    return int(_int(name, default))


def setting_float(name: str, default: float) -> float:
    """The float twin of `setting_int` (config.py's `_float` rule)."""
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return float(value)
    return float(_float(name, default))


def max_output_tokens_setting() -> int:
    """PUBLIC_API_MAX_OUTPUT_TOKENS (owner decision 2026-09-13: 1,000,000).

    The public ceiling for `max_output_tokens`, independent of the chat
    application's MODEL_MAX_OUTPUT — which stays the chat app's number and is
    no longer read by `/v1` for any ceiling."""
    return max(1, setting_int("PUBLIC_API_MAX_OUTPUT_TOKENS", 1_000_000))


def public_default_max_output_tokens() -> int:
    """PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS (8,192): the budget a request gets
    when it names none — unchanged by the 1M ceiling."""
    return max(1, setting_int("PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS", 8192))


#: The keyword `llm.stream_chat_events` must accept before a public
#: techsara-35b generation can run past the chat app's GEN_WALL_CLOCK_S.
WALL_CLOCK_KEYWORD = "wall_clock_s"


def per_request_wall_clock_live() -> bool:
    """Does the main engine path honour a per-request wall clock yet?

    WHY THIS EXISTS (adversarial review 2026-09-13). The 1,000,000-token
    ceiling is published, but until `llm.stream_chat_events` accepts
    `wall_clock_s` (an integration change to llm.py, another owner's file)
    every techsara-35b generation is still cut at GEN_WALL_CLOCK_S — 4,200 s,
    ~300-420k tokens at the measured 71-101 tok/s. The OpenAPI document and
    CONTRACT §8.3 must say so while it is true, and stop saying it the moment
    it is not; this is the one place that answers the question.

    Answered WITHOUT importing llm (this module is imported by the OpenAPI
    lint job, which has no engine stack): the loaded module's signature when
    the process already has it, otherwise the keyword read from llm.py's
    source with `ast`. Any failure to tell is "not live" — the caveat is the
    safe side of a wrong answer.
    """
    import sys

    package = __name__.rsplit(".", 2)[0]
    loaded = sys.modules.get(f"{package}.llm")
    function = getattr(loaded, "stream_chat_events", None)
    if function is not None:
        import inspect

        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return WALL_CLOCK_KEYWORD in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
    return _source_accepts_wall_clock()


def _source_accepts_wall_clock() -> bool:
    import ast
    from pathlib import Path

    try:
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("llm.py").read_text("utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_chat_events":
            names = [a.arg for a in (*node.args.args, *node.args.kwonlyargs)]
            return WALL_CLOCK_KEYWORD in names or node.args.kwarg is not None
    return False


def chat_app_wall_clock_s() -> float:
    """GEN_WALL_CLOCK_S, the clock a techsara-35b generation is cut at while
    `per_request_wall_clock_live()` is False."""
    return max(1.0, float(getattr(settings, "gen_wall_clock_s", 0) or 1800.0))


# ---------------------------------------------------------- the model --


@dataclass(frozen=True)
class PublicModel:
    """One row of `GET /v1/models`, plus the engine it resolves to.

    `internal` and `engine` NEVER leave the server: `to_wire()` is the only
    rendering and includes neither. Which checkpoint answers a request is an
    operational detail, and publishing it would let anyone read our upgrade
    schedule off the API.

    The defaults describe the flagship chat model, so an entry built the way
    the one-model registry built them still means what it meant.
    """

    id: str
    internal: str
    chat: bool
    streaming: bool
    vision: bool
    tools: bool
    embeddings: bool
    max_input_tokens: Optional[int]
    max_output_tokens: Optional[int]
    status: str = STATUS_AVAILABLE
    engine: str = ENGINE_MAIN
    kind: str = KIND_CHAT
    rerank: bool = False
    audio_transcription: bool = False
    ocr: bool = False
    background: bool = True
    endpoints: Tuple[str, ...] = _CHAT_ENDPOINTS
    context_window: Optional[int] = None
    default_max_output_tokens: Optional[int] = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    #: 0.2 is what the chat application asks for; OCR's 0.0 is what the chat
    #: app sends Unlimited-OCR (engines/ocr.py). Always sent, so a checkpoint's
    #: generation_config (the router's says 0.7) never silently applies.
    default_temperature: float = 0.2
    clamp_basis: Optional[str] = CLAMP_EXACT
    context_reserve: int = 0
    max_images: int = 0

    def __post_init__(self) -> None:
        # The guards run on CONSTRUCTION, not only on the entries the
        # catalogue builds: a future entry, a test fixture, or a console that
        # ever learns to construct one all pass through here. There is no way
        # to hold a `PublicModel` whose id is internal vocabulary, or whose
        # target is an address or another engine's checkpoint.
        guard_public_id(self.id)
        if self.engine not in ENGINE_KEYS:
            raise InternalTargetError(
                f"a public model must resolve to one of {ENGINE_KEYS}, not {self.engine!r}"
            )
        if self.kind not in KINDS:
            raise ValueError(f"unknown public model kind {self.kind!r}")
        if self.status == STATUS_NOT_CONFIGURED and not str(self.internal or "").strip():
            # A catalogue entry for an engine this deployment does not run
            # names nothing, and nothing can be reached through it.
            return
        guard_internal_target(self.internal, engine=self.engine)

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def supports_endpoint(self, path: str) -> bool:
        return path in self.endpoints

    def to_wire(self) -> Dict[str, Any]:
        """The Model object of CONTRACT §15, additive over the one-model
        shape. Nullable ceilings are null — never 0 — for a model that has
        none (an embedding model generates nothing)."""
        return {
            "id": self.id,
            "object": "model",
            "owned_by": "techsara",
            "status": self.status,
            "kind": self.kind,
            "capabilities": {
                "chat": self.chat,
                "streaming": self.streaming,
                "vision": self.vision,
                "tools": self.tools,
                "embeddings": self.embeddings,
                "rerank": self.rerank,
                "audio_transcription": self.audio_transcription,
                "ocr": self.ocr,
                "background": self.background,
            },
            "endpoints": list(self.endpoints),
            "context_window": self.context_window,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "default_max_output_tokens": self.default_max_output_tokens,
            "limits": dict(self.limits),
        }


# ------------------------------------------------------------ the guard --


def _clean(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


#: A bare `host:port`, the form a copy-and-paste out of Compose produces.
#: Anchored and digits-only on the port so an ordinary model id with a colon
#: in it (`llama3:8b`) is not mistaken for an address.
_HOST_PORT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*):(\d{1,5})$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: The port a scheme implies when the URL does not name one, so
#: `https://host/v1` and `https://host:443/v1` are the same target.
_DEFAULT_PORTS = {"http": "80", "https": "443"}

#: TechSara vocabulary: `techsara-` then lowercase words joined by single
#: dashes. No dot, slash or colon, so no URL, path, host:port or checkpoint
#: name (`Qwen/Qwen3-VL-8B`) can be a public id.
_PUBLIC_ID_RE = re.compile(r"^techsara-[a-z0-9]+(-[a-z0-9]+)*$")


def _address_forms(value: Any) -> Tuple[str, str]:
    """(`scheme://host:port`, `host`) for an address-shaped value, else ("", "").

    WHY THIS EXISTS AT ALL (verifier finding, 2026-09-13): comparing cleaned
    STRINGS let every near-miss of an engine URL through — with the router at
    `http://vllm-router:30002/v1`, `http://vllm-router:30002`,
    `https://vllm-router:30002/v1` and the bare `vllm-router` were all
    "different". A value with no `://` and no `host:port` shape is a model
    NAME and gets ("", "").
    """
    cleaned = _clean(value)
    if not cleaned:
        return "", ""
    if "://" in cleaned:
        parts = urlsplit(cleaned)
        host = (parts.hostname or "").strip("[]")
        if not host:
            return "", ""
        try:
            explicit = parts.port
        except ValueError:
            explicit = None
        port = str(explicit) if explicit else _DEFAULT_PORTS.get(parts.scheme, "")
        return f"{parts.scheme}://{host}:{port}", host
    match = _HOST_PORT_RE.match(cleaned)
    if match:
        return f"//{match.group(1)}:{match.group(2)}", match.group(1)
    return "", ""


def _service_names() -> Dict[str, str]:
    """The served model NAME of every engine this process talks to, cleaned.

    `agent` and `vision` are not public engine keys, but their names are
    identities that must not be published under a key they do not belong to.
    """
    names = {
        ENGINE_MAIN: getattr(settings, "llm_model", ""),
        ENGINE_ROUTER: getattr(settings, "router_model", ""),
        ENGINE_OCR: getattr(settings, "ocr_model", ""),
        ENGINE_EMBED: getattr(settings, "embed_model", ""),
        ENGINE_RERANK: getattr(settings, "rerank_model", ""),
        ENGINE_ASR: getattr(settings, "asr_model", ""),
        "agent": getattr(settings, "agent_model", ""),
        "vision": getattr(settings, "vision_model", ""),
    }
    return {key: _clean(value) for key, value in names.items()}


def _service_urls() -> Tuple[str, ...]:
    """Every base URL this process calls, main engine included."""
    urls = [
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "router_base_url", ""),
        getattr(settings, "agent_base_url", ""),
        getattr(settings, "vision_base_url", ""),
        getattr(settings, "embed_base_url", ""),
        getattr(settings, "ocr_base_url", ""),
        getattr(settings, "ocr_remote_base_url", ""),
        getattr(settings, "rerank_base_url", ""),
        getattr(settings, "asr_base_url", ""),
    ]
    urls.extend(getattr(settings, "asr_base_urls", ()) or ())
    return tuple(str(url) for url in urls if str(url or "").strip())


def internal_targets() -> frozenset:
    """Every model name and base URL that must never be the MAIN public
    target: the other engines' names, and every URL. Built from `settings` at
    call time, so an engine moved to the worker is still covered."""
    names = _service_names()
    own = names[ENGINE_MAIN]
    candidates = {name for key, name in names.items() if key != ENGINE_MAIN and name != own}
    candidates.update(_clean(url) for url in _service_urls())
    candidates.discard("")
    return frozenset(candidates)


def _internal_addresses() -> Tuple[frozenset, frozenset]:
    """(`scheme://host:port` forms, bare hosts) of every engine, main included.

    The main engine's host is NOT subtracted any more (it was, while `internal`
    could be compared as a string against addresses): since 2026-09-13 an
    `internal` is a model name and may not be address-shaped at all, so no
    host — ours or anyone's — is a legitimate value for it.
    """
    addresses = set()
    hosts = set()
    for url in _service_urls():
        address, host = _address_forms(url)
        if address:
            addresses.add(address)
        if host:
            hosts.add(host)
    hosts.discard("")
    return frozenset(addresses), frozenset(hosts)


def _is_address(cleaned: str) -> bool:
    if "://" in cleaned or _HOST_PORT_RE.match(cleaned) or _IPV4_RE.match(cleaned):
        return True
    addresses, hosts = _internal_addresses()
    address, host = _address_forms(cleaned)
    return bool((address and address in addresses) or (host and host in hosts) or cleaned in hosts)


def guard_internal_target(target: str, engine: str = ENGINE_MAIN) -> str:
    """Raise unless `target` is a served model NAME that may be published
    under `engine`.

    Refused: a blank target; an engine key outside the closed set; anything
    address-shaped (a URL in any spelling, a `host:port`, an IP, the bare
    hostname of any engine); the checkpoint name of any OTHER public engine;
    and the checkpoint of an internal-only service (the agent, vision) unless
    it is this engine's own — VISION_MODEL defaults to the main checkpoint
    because Qwen3.6-35B-A3B is a vision-language model, and the agent defaults
    to the router's.

    Called on every construction of a `PublicModel`, not only on the entries
    that exist today, because the next entry will be added by someone reading
    these as a template. `engine` defaults to the main engine, so the
    one-argument form asks what it always asked: "is this safe to serve as
    techsara-35b?"
    """
    refusal = InternalTargetError(
        "refusing to publish an internal engine identity through /v1: a public "
        "model names the checkpoint of its own engine, never an address and "
        "never another engine's checkpoint (CONTRACT §15)"
    )
    if engine not in ENGINE_KEYS:
        raise refusal
    cleaned = _clean(target)
    if not cleaned:
        raise InternalTargetError("a public model must name an internal target")
    if _is_address(cleaned):
        raise refusal
    names = _service_names()
    own = names.get(engine, "")
    for key, name in names.items():
        if key == engine or not name or cleaned != name:
            continue
        if key in ENGINE_KEYS:
            if engine == ENGINE_MAIN and cleaned == own:
                # A sidecar variable naming the MAIN checkpoint withdraws that
                # sidecar (`_configured_name`); it says nothing against the
                # main model publishing its own checkpoint.
                continue
            # Two PUBLIC engines never legitimately serve one checkpoint: the
            # rest is a pasted variable (OCR_MODEL set to the embeddings
            # model's name).
            raise refusal
        if cleaned != own:
            raise refusal
    return str(target)


def guard_public_id(model_id: str) -> str:
    """Raise unless `model_id` is TechSara vocabulary and names nothing internal.

    The shape rule alone excludes URLs, paths, `host:port` and checkpoint
    names; the second check catches the one thing a well-shaped id could
    still be — a bare hostname or served name that happens to look like ours
    (a Compose service called `techsara-ocr`)."""
    value = str(model_id or "")
    if not _PUBLIC_ID_RE.match(value):
        raise InternalTargetError(
            f"a public model id must match {_PUBLIC_ID_RE.pattern} (CONTRACT §15)"
        )
    cleaned = _clean(value)
    _, hosts = _internal_addresses()
    if cleaned in hosts or cleaned in set(_service_names().values()):
        raise InternalTargetError(
            "a public model id may not be the name or host of an internal engine"
        )
    return value


# ------------------------------------------------------ served windows --

#: engine key → (base URL it was measured on, served max_model_len, when).
_SERVED_WINDOWS: Dict[str, Tuple[str, int, float]] = {}


def engine_base_url(engine: str) -> str:
    """The base URL `settings` names for an engine key, read now. For speech,
    the LAST replica — the one dictation's least-active routing reaches for
    last (asr.RoutedProvider breaks ties toward index 0)."""
    if engine == ENGINE_ASR:
        urls = tuple(getattr(settings, "asr_base_urls", ()) or ())
        return str(urls[-1] if urls else getattr(settings, "asr_base_url", "") or "").rstrip("/")
    attribute = {
        ENGINE_MAIN: "openai_base_url",
        ENGINE_ROUTER: "router_base_url",
        ENGINE_OCR: "ocr_base_url",
        ENGINE_EMBED: "embed_base_url",
        ENGINE_RERANK: "rerank_base_url",
    }.get(engine, "")
    return str(getattr(settings, attribute, "") or "").rstrip("/") if attribute else ""


def note_served_window(engine: str, base_url: str, max_model_len: Optional[int]) -> None:
    """Record what an engine says it serves (`engines.served_window` calls
    this after its probe). NARROW-ONLY by construction: the catalogue takes
    the smaller of this and the public number, so a probe can shrink a
    ceiling to the engine's truth and can never widen one past the owner's
    decision — the router's generated.env says 65,536 while the engine serves
    49,152, and a registry that trusted the larger number would advertise a
    window the engine 400s on."""
    if engine not in ENGINE_KEYS or not max_model_len or int(max_model_len) <= 0:
        return
    _SERVED_WINDOWS[engine] = (str(base_url or "").rstrip("/"), int(max_model_len), time.monotonic())


def served_window_hint(engine: str) -> Optional[int]:
    """The served window last reported for `engine`, if it still describes the
    URL settings name now and is younger than SERVED_WINDOW_TTL_S."""
    entry = _SERVED_WINDOWS.get(engine)
    if entry is None:
        return None
    base_url, tokens, at = entry
    if base_url != engine_base_url(engine):
        return None
    if time.monotonic() - at > SERVED_WINDOW_TTL_S:
        return None
    return tokens


def clear_served_windows() -> None:
    """For tests."""
    _SERVED_WINDOWS.clear()


def _narrowed(public: int, engine: str) -> int:
    served = served_window_hint(engine)
    return min(public, served) if served else public


# ------------------------------------------------------------ builders --


def _same_address(first: Any, second: Any) -> bool:
    a, _ = _address_forms(first)
    b, _ = _address_forms(second)
    if a and b:
        return a == b
    return bool(_clean(first)) and _clean(first) == _clean(second)


def _main_limits() -> Tuple[int, int]:
    """(context window, public output ceiling) for the main model.

    The window is MAIN_MODEL_MAX_LEN — what vLLM was actually started with
    (1,000,000 since 2026-09-02, needle-verified at 949,915 tokens). The
    output ceiling is PUBLIC_API_MAX_OUTPUT_TOKENS narrowed by that window,
    because input and output share it: 1,000,000 on this deployment.
    """
    capabilities = getattr(settings, "main_capabilities", None)
    context = int(getattr(settings, "model_max_context", 0) or 0)
    if not context and capabilities is not None:
        context = int(getattr(capabilities, "context_length", 0) or 0)
    ceiling = max_output_tokens_setting()
    if context > 0:
        ceiling = min(ceiling, context)
    return context, ceiling


def default_max_output_tokens() -> int:
    """The output budget a techsara-35b request gets when it names none —
    PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS (8,192), narrowed by the ceiling."""
    _, ceiling = _main_limits()
    return max(1, min(public_default_max_output_tokens(), ceiling))


def _chat_window_numbers(window: int, reserve: int) -> Tuple[Optional[int], Optional[int]]:
    """(context_window, max_input_tokens) for a chat model, or (None, None)
    when the deployment never told us its window — inventing one would refuse
    requests the engine would serve."""
    if window <= 0:
        return None, None
    return window, max(1, window - reserve - MIN_OUTPUT_TOKENS)


def _build_main(configured: bool) -> PublicModel:
    capabilities = getattr(settings, "main_capabilities", None)
    window, ceiling = _main_limits()
    reserve = max(0, int(getattr(settings, "context_safety_margin", 512) or 0))
    context_window, max_input = _chat_window_numbers(window, reserve)
    vision = bool(getattr(capabilities, "supports_vision", False))
    limits: Dict[str, Any] = {}
    if vision:
        limits["max_images_per_request"] = MAIN_MAX_IMAGES
    return PublicModel(
        id=TECHSARA_35B,
        internal=guard_internal_target(settings.llm_model, engine=ENGINE_MAIN),
        engine=ENGINE_MAIN,
        kind=KIND_CHAT,
        chat=bool(getattr(capabilities, "supports_chat", True)),
        streaming=bool(getattr(capabilities, "supports_streaming", True)),
        vision=vision,
        # False although the checkpoint calls tools: CONTRACT §7 does not
        # expose tools on /v1, and advertising a capability the endpoint
        # refuses is how a client ends up with a request that validates
        # against our schema and fails on our server.
        tools=False,
        embeddings=False,
        background=True,
        endpoints=_CHAT_ENDPOINTS,
        context_window=context_window,
        max_input_tokens=max_input,
        max_output_tokens=ceiling,
        default_max_output_tokens=max(1, min(public_default_max_output_tokens(), ceiling)),
        limits=limits,
        default_temperature=0.2,
        clamp_basis=CLAMP_EXACT,
        context_reserve=reserve,
        max_images=MAIN_MAX_IMAGES if vision else 0,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


def _configured_name(name: Any) -> bool:
    """A sidecar's served name is usable: set, and not the MAIN checkpoint —
    which would publish the main model under a second id, around its breaker
    and admission lanes."""
    return bool(_clean(name)) and _clean(name) != _clean(getattr(settings, "llm_model", ""))


def _router_configured() -> bool:
    url = getattr(settings, "router_base_url", "")
    name = getattr(settings, "router_model", "")
    if not _clean(url) or not _clean(name):
        return False
    # A profile pointing the router at the main engine would publish the main
    # model under a second id, around its breaker and admission lanes.
    if _same_address(url, getattr(settings, "openai_base_url", "")):
        return False
    return _clean(name) != _clean(getattr(settings, "llm_model", ""))


def _build_router(configured: bool) -> PublicModel:
    # Half the engine's 49,152 on purpose (architecture review 2026-09-13):
    # the router's KV pool holds 1.07 full windows and the chat app classifies
    # EVERY turn on it, so a public full-window request would starve chat.
    window = _narrowed(max(1, setting_int("PUBLIC_API_ROUTER_CONTEXT_TOKENS", 24_576)), ENGINE_ROUTER)
    context_window, max_input = _chat_window_numbers(window, 0)
    # 24,576 = the public window; the launcher's ROUTER_OUTPUT_LIMIT of 8,192
    # is a generic min(8192, ctx//4) formula, not an engine limit.
    ceiling = max(1, min(window, max_output_tokens_setting()))
    return PublicModel(
        id=TECHSARA_8B_VISION,
        internal=(getattr(settings, "router_model", "") if configured else ""),
        engine=ENGINE_ROUTER,
        kind=KIND_CHAT,
        chat=True,
        streaming=True,
        vision=True,
        tools=False,
        embeddings=False,
        background=True,
        endpoints=_CHAT_ENDPOINTS,
        context_window=context_window,
        max_input_tokens=max_input,
        max_output_tokens=ceiling,
        default_max_output_tokens=max(1, min(public_default_max_output_tokens(), ceiling)),
        limits={"max_images_per_request": ROUTER_MAX_IMAGES},
        default_temperature=0.2,
        clamp_basis=CLAMP_ESTIMATE,
        context_reserve=SIDECAR_CONTEXT_RESERVE,
        max_images=ROUTER_MAX_IMAGES,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


def _ocr_configured() -> bool:
    if not bool(getattr(settings, "ocr_enabled", False)):
        return False
    url = getattr(settings, "ocr_base_url", "")
    name = getattr(settings, "ocr_model", "")
    if not _clean(url) or not _configured_name(name):
        return False
    return not _same_address(url, getattr(settings, "openai_base_url", ""))


def _build_ocr(configured: bool) -> PublicModel:
    window = _narrowed(max(1, setting_int("PUBLIC_API_OCR_CONTEXT_TOKENS", 8192)), ENGINE_OCR)
    context_window, max_input = _chat_window_numbers(window, 0)
    ceiling = max(1, min(window, max_output_tokens_setting()))
    return PublicModel(
        id=TECHSARA_OCR,
        internal=(getattr(settings, "ocr_model", "") if configured else ""),
        engine=ENGINE_OCR,
        kind=KIND_CHAT,
        chat=True,
        streaming=True,
        vision=True,
        ocr=True,
        tools=False,
        embeddings=False,
        background=True,
        endpoints=_CHAT_ENDPOINTS,
        context_window=context_window,
        max_input_tokens=max_input,
        max_output_tokens=ceiling,
        default_max_output_tokens=max(1, min(public_default_max_output_tokens(), ceiling)),
        limits={"max_images_per_request": OCR_IMAGES_PER_REQUEST},
        default_temperature=0.0,
        clamp_basis=CLAMP_UPPER_BOUND,
        context_reserve=SIDECAR_CONTEXT_RESERVE,
        max_images=OCR_IMAGES_PER_REQUEST,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


def _embed_configured() -> bool:
    url = getattr(settings, "embed_base_url", "")
    name = getattr(settings, "embed_model", "")
    if not _clean(url) or not _configured_name(name):
        return False
    return not _same_address(url, getattr(settings, "openai_base_url", ""))


def _build_embed(configured: bool) -> PublicModel:
    window = _narrowed(max(1, setting_int("PUBLIC_API_EMBED_CONTEXT_TOKENS", 4096)), ENGINE_EMBED)
    return PublicModel(
        id=TECHSARA_EMBED,
        internal=(getattr(settings, "embed_model", "") if configured else ""),
        engine=ENGINE_EMBED,
        kind=KIND_EMBEDDING,
        chat=False,
        streaming=False,
        vision=False,
        tools=False,
        embeddings=True,
        background=False,
        endpoints=(ENDPOINT_EMBEDDINGS,),
        context_window=window,
        max_input_tokens=window,
        max_output_tokens=None,
        default_max_output_tokens=None,
        limits={
            "max_inputs_per_request": max(1, setting_int("PUBLIC_API_EMBED_MAX_INPUTS", 2048)),
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
        },
        default_temperature=0.0,
        clamp_basis=None,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


def _rerank_configured() -> bool:
    backend = getattr(settings, "rerank_backend", "")
    if str(getattr(backend, "value", backend) or "").lower() != "remote":
        return False
    url = getattr(settings, "rerank_base_url", "")
    name = getattr(settings, "rerank_model", "")
    if not _clean(url) or not _configured_name(name):
        return False
    return not _same_address(url, getattr(settings, "openai_base_url", ""))


def _build_rerank(configured: bool) -> PublicModel:
    window = _narrowed(max(1, setting_int("PUBLIC_API_RERANK_CONTEXT_TOKENS", 4096)), ENGINE_RERANK)
    return PublicModel(
        id=TECHSARA_RERANK,
        internal=(getattr(settings, "rerank_model", "") if configured else ""),
        engine=ENGINE_RERANK,
        kind=KIND_RERANK,
        chat=False,
        streaming=False,
        vision=False,
        tools=False,
        embeddings=False,
        rerank=True,
        background=False,
        endpoints=(ENDPOINT_RERANK,),
        context_window=window,
        max_input_tokens=window,
        max_output_tokens=None,
        default_max_output_tokens=None,
        limits={
            "max_documents_per_request": max(
                1, setting_int("PUBLIC_API_RERANK_MAX_DOCUMENTS", 1000)
            ),
        },
        default_temperature=0.0,
        clamp_basis=None,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


def _asr_configured() -> bool:
    if not bool(getattr(settings, "asr_enabled", False)):
        return False
    urls = tuple(getattr(settings, "asr_base_urls", ()) or ())
    return bool(urls) and _configured_name(getattr(settings, "asr_model", ""))


def _build_asr(configured: bool) -> PublicModel:
    return PublicModel(
        id=TECHSARA_WHISPER,
        internal=(getattr(settings, "asr_model", "") if configured else ""),
        engine=ENGINE_ASR,
        kind=KIND_TRANSCRIPTION,
        chat=False,
        streaming=False,
        vision=False,
        tools=False,
        embeddings=False,
        audio_transcription=True,
        background=False,
        endpoints=(ENDPOINT_TRANSCRIPTIONS,),
        context_window=None,
        max_input_tokens=None,
        max_output_tokens=None,
        default_max_output_tokens=None,
        # No seconds limit (no-timeout design, 2026-09-14): audio of any
        # duration is transcribed in windows. The byte cap is ONE request's
        # file part; longer recordings arrive through the Files API.
        limits={
            "max_audio_bytes": max(1, setting_int("PUBLIC_API_MAX_AUDIO_BYTES", 93_323_264)),
            "response_formats": list(TRANSCRIPTION_RESPONSE_FORMATS),
        },
        default_temperature=0.0,
        clamp_basis=None,
        status=STATUS_AVAILABLE if configured else STATUS_NOT_CONFIGURED,
    )


#: id → (is it configured on this deployment?, how to build it), in
#: PUBLIC_MODEL_IDS order.
_BUILDERS = (
    (TECHSARA_35B, lambda: True, _build_main),
    (TECHSARA_8B_VISION, _router_configured, _build_router),
    (TECHSARA_OCR, _ocr_configured, _build_ocr),
    (TECHSARA_EMBED, _embed_configured, _build_embed),
    (TECHSARA_RERANK, _rerank_configured, _build_rerank),
    (TECHSARA_WHISPER, _asr_configured, _build_asr),
)


def catalogue() -> Tuple[PublicModel, ...]:
    """All six entries, each `available` or `not_configured` — for the
    console, which shows an operator what this deployment could offer.

    NEVER the answer to "what may a caller use": that is `declared_models()`.
    """
    entries = []
    for model_id, configured, build in _BUILDERS:
        is_configured = bool(configured())
        try:
            entries.append(build(is_configured))
        except InternalTargetError:
            if model_id == TECHSARA_35B or not is_configured:
                raise
            log.error(
                "public model %s is withdrawn: its engine configuration names an "
                "address or another engine's checkpoint",
                model_id,
            )
            entries.append(build(False))
    return tuple(entries)


def declared_models() -> Tuple[PublicModel, ...]:
    """The code-level truth for callers, rebuilt on every call: every
    catalogue entry whose engine this deployment actually runs.

    techsara-35b is always declared, and a guard failure on it RAISES — the
    mistake must not survive a test run. A sidecar whose configuration fails
    the guard is WITHDRAWN (logged as an error) instead: one misconfigured
    OCR variable must not take the flagship model's `/v1/models` down with it.
    """
    return tuple(model for model in catalogue() if model.available)


def _disabled_ids(overrides: Optional[Any]) -> frozenset:
    """Read the `public_models` rows as the ONE thing they are allowed to say.

    Accepts a mapping (`{id: enabled}`) or an iterable of rows with `id` and
    `enabled` — a psycopg row is a Mapping, so the query result goes straight
    in. Anything that says `enabled` is ignored: a row can only disable, and a
    row for an id the code does not declare cannot conjure one.
    """
    if not overrides:
        return frozenset()
    if isinstance(overrides, Mapping):
        items: Iterable[Tuple[Any, Any]] = overrides.items()
    else:
        items = (
            (row.get("id"), row.get("enabled", True))
            for row in overrides
            if isinstance(row, Mapping)
        )
    return frozenset(
        str(model_id) for model_id, enabled in items if model_id and not bool(enabled)
    )


def public_models(overrides: Optional[Any] = None) -> Tuple[PublicModel, ...]:
    """What `GET /v1/models` may list: the declared models, minus the disabled.

    A disabled model is REMOVED rather than listed as unavailable, so a caller
    never writes code against an id it will get a 404 for.
    """
    disabled = _disabled_ids(overrides)
    return tuple(model for model in declared_models() if model.id not in disabled)


def resolve_public_model(
    model_id: str,
    allowed: Optional[Iterable[str]] = None,
    overrides: Optional[Any] = None,
) -> Optional[PublicModel]:
    """The model for this request, or **None** — which the router turns into
    404 `model_not_found`, never 403 (CONTRACT §4: no existence disclosure).

    `allowed` is the key's allowlist. **Empty or None means no narrowing**,
    matching `api_projects.allowed_models`, whose V34 default is `'[]'` — a
    project created with defaults can use every public model. Since
    2026-09-13 that includes the new ids on EXISTING keys (a changelog item):
    a project that wants the flagship only lists it.
    """
    wanted = str(model_id or "").strip()
    if not wanted:
        return None
    permitted = {str(item).strip() for item in (allowed or []) if str(item).strip()}
    if permitted and wanted not in permitted:
        return None
    for model in public_models(overrides):
        if model.id == wanted:
            return model
    return None


def unsupported_endpoint_message(model_id: str, path: str) -> str:
    """The 400 sentence for a permitted model on the wrong endpoint — one
    spelling for every route that refuses it."""
    return f"The model `{model_id}` does not support {path}."
