"""Which models `/v1` exposes — the code-level registry of CONTRACT §15.

THE POINT OF THIS FILE IS WHAT IT REFUSES TO DO. Infrastructure cannot expose
a model by appearing in Compose, and neither can a database row: the public
catalogue is the tuple built in `declared_models()` and nothing else. A
`public_models` row (SCHEMA-V34) may only take a model AWAY; a row naming an
id this file does not declare is ignored. That asymmetry is the whole design —
an admin mistake, a stray migration or a compromised console can narrow the
public surface, never widen it.

WHAT IS NOT HERE, DELIBERATELY. The router, the embeddings service, the OCR
engine and the reranker. They are internal engines with no authentication of
their own, and `/v1` has no path to them (CONTRACT §11: the public API calls
`llm.stream_chat_events` and nothing lower). `guard_internal_target()` turns
that from a convention into a check that raises at construction time, so the
mistake cannot survive a test run — the OCR engine on the worker (2026-09-09)
is one env var away from the main model in `config.py`, and a copy-paste
between the two is exactly the accident worth making impossible.

READ AT CALL TIME, NEVER CAPTURED AT IMPORT. `internal` is
`settings.llm_model` READ WHEN THE FUNCTION RUNS. The main model has been
swapped under a running deployment more than once (the 27B → 35B-A3B move of
2026-08-29 is three files and a redetect), and a module-level constant would
keep naming the old checkpoint in the usage ledger and in `/v1/models` until
someone noticed. The same goes for the limits: they come from `settings`,
which reads MAIN_MODEL_MAX_LEN, so the documented ceiling and the served
ceiling cannot disagree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from ..config import settings

#: The one public model today (CONTRACT §15). A product decision, not a
#: deployment one: adding a second id is a contract change, a documentation
#: change and a scope review, so it happens here in a reviewed diff.
TECHSARA_35B = "techsara-35b"

PUBLIC_MODEL_IDS: Tuple[str, ...] = (TECHSARA_35B,)


class InternalTargetError(RuntimeError):
    """Something tried to make an internal engine publicly reachable."""


@dataclass(frozen=True)
class PublicModel:
    """One row of `GET /v1/models`, plus the internal target it resolves to.

    `internal` NEVER leaves the server: `to_wire()` is the only rendering, and
    it does not include the field. Which checkpoint answers a request is an
    operational detail, and publishing it would let anyone read our upgrade
    schedule off the API.
    """

    id: str
    internal: str
    chat: bool
    streaming: bool
    vision: bool
    tools: bool
    embeddings: bool
    max_input_tokens: int
    max_output_tokens: int
    status: str = "available"

    def __post_init__(self) -> None:
        # The guard runs on CONSTRUCTION, not only on the one entry
        # `declared_models()` builds today: a future wave adding a second
        # model, a test building a fixture, or a console that ever learns to
        # construct one all pass through here. There is no way to hold a
        # `PublicModel` that names an internal engine.
        guard_internal_target(self.internal)

    def to_wire(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": "techsara",
            "status": self.status,
            "capabilities": {
                "chat": self.chat,
                "streaming": self.streaming,
                "vision": self.vision,
                "tools": self.tools,
                "embeddings": self.embeddings,
            },
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


def _clean(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


#: A bare `host:port`, the form a copy-and-paste out of Compose produces.
#: Anchored and digits-only on the port so an ordinary model id with a colon
#: in it (`llama3:8b`) is not mistaken for an address.
_HOST_PORT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*):(\d{1,5})$")

#: The port a scheme implies when the URL does not name one, so
#: `https://host/v1` and `https://host:443/v1` are the same target.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _address_forms(value: Any) -> Tuple[str, str]:
    """(`scheme://host:port`, `host`) for an address-shaped target, else ("", "").

    WHY THIS EXISTS AT ALL. `guard_internal_target` used to compare `_clean`ed
    STRINGS, which lowercase and strip one trailing slash and nothing else, so
    every near-miss of the very URLs it exists to refuse went straight through
    (verifier finding, 2026-09-13): with the router configured as
    `http://vllm-router:30002/v1`, the guard allowed `http://vllm-router:30002`,
    `https://vllm-router:30002/v1`, and the bare name `vllm-router`. The
    accident the file names as its motivation — the OCR engine moving to the
    worker on 2026-09-09, one env var away from the main model — is exactly
    that shape: `http://192.168.9.68:30007/v1` and `192.168.9.68:30007` are one
    target, and a guard that only knows the first is theatre.

    A value with no `://` and no `host:port` shape is a model NAME
    (`nvidia/Qwen3.6-35B-A3B-NVFP4`), not an address, and gets ("", "") — names
    are compared as names.
    """
    cleaned = _clean(value)
    if not cleaned:
        return "", ""
    if "://" in cleaned:
        parts = urlsplit(cleaned)
        host = (parts.hostname or "").strip("[]")
        if not host:
            return "", ""
        port = str(parts.port) if parts.port else _DEFAULT_PORTS.get(parts.scheme, "")
        return f"{parts.scheme}://{host}:{port}", host
    match = _HOST_PORT_RE.match(cleaned)
    if match:
        return f"//{match.group(1)}:{match.group(2)}", match.group(1)
    return "", ""


def internal_targets() -> frozenset:
    """Every model name and base URL that must never be a public target.

    Built from `settings` at call time, so a deployment that moves the OCR
    engine to the worker (OCR_REMOTE_BASE_URL, 2026-09-09) is still covered.

    The main model's own name and URL are SUBTRACTED at the end. `VISION_MODEL`
    and `AGENT_MODEL` may legitimately point at the main engine — the default
    for VISION_MODEL is the main checkpoint, because Qwen3.6-35B-A3B is itself
    a vision-language model — and flagging that as an internal target would
    refuse to build the registry on a perfectly ordinary deployment. If a
    router variable happens to name the main model, the target IS the main
    model and nothing internal is reachable through it.
    """
    candidates = {
        _clean(getattr(settings, "router_base_url", "")),
        _clean(getattr(settings, "router_model", "")),
        _clean(getattr(settings, "agent_base_url", "")),
        _clean(getattr(settings, "agent_model", "")),
        _clean(getattr(settings, "embed_base_url", "")),
        _clean(getattr(settings, "embed_model", "")),
        _clean(getattr(settings, "ocr_base_url", "")),
        _clean(getattr(settings, "ocr_model", "")),
        _clean(getattr(settings, "ocr_remote_base_url", "")),
        _clean(getattr(settings, "rerank_base_url", "")),
        _clean(getattr(settings, "rerank_model", "")),
        _clean(getattr(settings, "asr_base_url", "")),
        _clean(getattr(settings, "asr_model", "")),
    }
    candidates.discard("")
    candidates.discard(_clean(getattr(settings, "llm_model", "")))
    candidates.discard(_clean(getattr(settings, "openai_base_url", "")))
    return frozenset(candidates)


def _internal_addresses() -> Tuple[frozenset, frozenset]:
    """(`scheme://host:port` forms, bare hosts) of every internal engine.

    The MAIN engine's own address is subtracted from both, for the same reason
    `internal_targets()` subtracts its name: a deployment that runs the main
    model and an internal engine on one box (the head Spark serves the main
    model on :30000 and, before 2026-09-09, OCR on :30007) shares a hostname
    between them, and refusing the main engine's host outright would refuse to
    build the registry on an ordinary single-node install. The host:port and
    full-URL rules still cover that case — a different port is a different
    address — so what is given up here is only the bare-hostname rule, and only
    for the one host we deliberately serve from.
    """
    addresses = set()
    hosts = set()
    for target in internal_targets():
        address, host = _address_forms(target)
        if address:
            addresses.add(address)
        if host:
            hosts.add(host)
    _, main_host = _address_forms(getattr(settings, "openai_base_url", ""))
    hosts.discard(main_host)
    hosts.discard("")
    return frozenset(addresses), frozenset(hosts)


def guard_internal_target(target: str) -> str:
    """Raise unless `target` is safe to serve publicly.

    Called on every construction of a `PublicModel`, not only on the one entry
    that exists today, because the next entry will be added by someone reading
    the first as a template.

    Three comparisons, not one: the cleaned NAME (a model id such as
    `Qwen/Qwen3-VL-8B`), the normalised ADDRESS (`scheme://host:port`, with the
    path discarded and the scheme's default port filled in), and the bare HOST.
    See `_address_forms` for the near-misses that made the string comparison
    alone useless.
    """
    cleaned = _clean(target)
    if not cleaned:
        raise InternalTargetError("a public model must name an internal target")
    refusal = InternalTargetError(
        "refusing to expose an internal engine through /v1: the router, "
        "embeddings, OCR and reranker services are not public models "
        "(CONTRACT §15)"
    )
    if cleaned in internal_targets():
        raise refusal
    addresses, hosts = _internal_addresses()
    address, host = _address_forms(cleaned)
    # The bare-host comparison uses the CLEANED value as well as the parsed
    # host, so `vllm-router` on its own — no scheme, no port, the form a
    # docstring or a Compose service name arrives in — is refused too.
    if (address and address in addresses) or (host and host in hosts) or cleaned in hosts:
        raise refusal
    return str(target)


def _main_limits() -> Tuple[int, int]:
    """(input ceiling, output ceiling) for the main model, from settings.

    Not literals copied out of the documentation: `MAIN_MODEL_MAX_LEN` is what
    vLLM was actually started with, and the 1M-context work of 2026-09-02
    proved that a number the app believes but the engine does not produce 400s
    rather than more context.
    """
    capabilities = getattr(settings, "main_capabilities", None)
    context = int(getattr(settings, "model_max_context", 0) or 0)
    output = int(getattr(settings, "model_max_output", 0) or 0)
    if capabilities is not None:
        context = context or int(getattr(capabilities, "context_length", 0) or 0)
        output = output or int(getattr(capabilities, "output_limit", 0) or 0)
    return context, output


def default_max_output_tokens() -> int:
    """The output reservation a request gets when it does not ask for one
    (CONTRACT §12: "8,192 default, model ceiling max")."""
    _, output = _main_limits()
    return output


def declared_models() -> Tuple[PublicModel, ...]:
    """The code-level truth, rebuilt on every call.

    `tools` is False even though the main model supports tool calling: CONTRACT
    §7 does not expose tools on `/v1`, and advertising a capability the
    endpoint refuses to accept is how a client ends up with a request that
    validates against our schema and fails on our server.
    """
    capabilities = getattr(settings, "main_capabilities", None)
    context, output = _main_limits()
    return (
        PublicModel(
            id=TECHSARA_35B,
            internal=guard_internal_target(settings.llm_model),
            chat=bool(getattr(capabilities, "supports_chat", True)),
            streaming=bool(getattr(capabilities, "supports_streaming", True)),
            vision=bool(getattr(capabilities, "supports_vision", False)),
            tools=False,
            embeddings=False,
            max_input_tokens=context,
            max_output_tokens=output,
            status="available",
        ),
    )


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
    project created with defaults can use every public model, and an
    allowlist is how an operator restricts one. That is the only reading that
    makes the schema default usable, but it does mean a wave that wants
    default-deny must change the column default and this line together.
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
