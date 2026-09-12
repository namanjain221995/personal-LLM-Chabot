"""Runtime layout, local secrets, and non-secret generated configuration."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .cluster import (
    CLUSTER_KV_CACHE_DTYPE,
    DEFAULT_DETECTORS,
    DEFAULT_DISCOVERY,
    DEFAULT_KV_CACHE_MEMORY_GIB,
    DEFAULT_TENSOR_PARALLEL_SIZE,
    ClusterDetectors,
    ClusterDiscovery,
    engine_process_environment,
    gdn_prefill_backend_argument,
    prefix_caching_argument,
    render_engine_env,
    resolve_cluster,
    speculative_config_argument,
)
from .errors import TechSaraError
from .model_manager import ModelInstall
from .modelshape import (
    ModelShape,
    kv_bytes_per_token,
    kv_gib_for_tokens,
    kv_pool_tokens,
    kv_usable_fraction,
    read_model_shape,
    rope_override_argument,
    yarn_factor,
)
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

    @property
    def controller_env(self) -> Path:
        """The engine controller's own secret layer (``.runtime/controller.env``).

        Read by exactly one compose service (``engine-controller``, via
        ``env_file``) and shipped to the worker sentinel by
        scripts/cluster-sync.sh -- never by ``secrets.env``, which every
        runtime service inherits wholesale (the orchestrator included, which
        handles untrusted input and must not hold the restart token).
        """
        return self.runtime_dir / "controller.env"

    @property
    def engine_env(self) -> Path:
        """The vLLM PROCESS environment both ranks read (``.runtime/engine.env``)."""
        return self.runtime_dir / "engine.env"



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
    # The worker sentinel's restart token lives in controller.env (see
    # prepare_controller_secrets), never here: this file is fed wholesale to
    # the orchestrator. A copy an earlier cluster-sync.sh draft minted into
    # secrets.env is retired on the next `up` so no service inherits it.
    values.pop("CLUSTER_SENTINEL_TOKEN", None)

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


def prepare_controller_secrets(
    layout: RuntimeLayout,
    user_env: Mapping[str, str],
) -> dict[str, str]:
    """Create/reuse the worker sentinel's restart token in ``controller.env`` (0600).

    ONE source of truth for both nodes (contract §6.4): a value the operator
    set in .env wins and is copied here, otherwise the existing token is kept,
    otherwise one is minted. scripts/cluster-sync.sh reads THIS file for the
    worker's copy and compares it with what the head's compose renders, so the
    two nodes cannot disagree the way the .env-vs-secrets.env precedence split
    let them (review round 1). Never printed, never in generated.env.
    """
    existing = parse_env_file(layout.controller_env) if layout.controller_env.is_file() else {}
    values = dict(existing)
    configured = (user_env.get("CLUSTER_SENTINEL_TOKEN") or "").strip()
    if configured:
        values["CLUSTER_SENTINEL_TOKEN"] = configured
    elif not (values.get("CLUSTER_SENTINEL_TOKEN") or "").strip():
        values["CLUSTER_SENTINEL_TOKEN"] = secure_token(32)
    layout.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(layout.controller_env, render_env(values), mode=0o600)
    return values


def write_engine_env(layout: RuntimeLayout, user_env: Mapping[str, str]) -> dict[str, str]:
    """Render ``.runtime/engine.env`` (0644, no secrets) from the ``CLUSTER_VLLM_*`` keys.

    Written on every ``up`` next to generated.env so the file always reflects
    .env; the cluster overlay reads it as an ``env_file`` and
    scripts/cluster-sync.sh ships the identical bytes to the worker.
    """
    process_environment = engine_process_environment(user_env)
    layout.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(layout.engine_env, render_engine_env(process_environment), mode=0o644)
    return process_environment


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


PREFIX_CACHING_KEY = "MAIN_MODEL_ENABLE_PREFIX_CACHING"
_FALSY = {"0", "false", "no", "off", "disabled"}


def resolve_prefix_caching(values: Mapping[str, str]) -> bool:
    """Whether the main vLLM engine is launched with prefix caching.

    Until 2026-09-11 this key was only a mirror the orchestrator's /health
    reported as "what the app believes", while the launch flag itself was a
    literal in three overlays; setting the key to false changed the belief
    and nothing else. It now decides the flag (dual-mode
    ``CLUSTER_ENGINE_ARGS`` and the single-node overlays alike) and is
    re-emitted, normalised, into generated.env so the orchestrator's belief
    is the launch. Unset means ON, matching vLLM's own default; a value that
    is neither true- nor false-shaped is an error rather than a silent ON.
    """
    raw = str(values.get(PREFIX_CACHING_KEY, "") or "").strip().lower()
    if not raw or raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in _FALSY:
        return False
    raise TechSaraError(
        f"{PREFIX_CACHING_KEY} must be true or false; got {values.get(PREFIX_CACHING_KEY)!r}"
    )


#: Sidecar KV budgets are a fraction of TOTAL device memory, so on a 122 GiB
#: GB10 a seemingly small fraction is a large reservation: 0.14 for OCR is
#: 17.0 GiB, of which 12.1 GiB became KV cache -- 211,552 tokens for a model
#: whose window is 8,192. These defaults must therefore be overridable per
#: host. They were previously emitted as literals into generated.env, which is
#: the LAST --env-file layer and so outranks the user's .env: setting either
#: key in .env changed nothing and reported nothing. Read the user's value
#: here so the documented knob is the one that wins.
AUX_GPU_MEMORY_UTILIZATION_RANGE = (0.02, 0.95)


def _aux_gpu_memory_utilization(
    values: Mapping[str, str], name: str, default: str
) -> str:
    raw = str(values.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        number = float(raw)
    except ValueError as exc:
        raise TechSaraError(f"{name} must be a number; got {raw!r}") from exc
    low, high = AUX_GPU_MEMORY_UTILIZATION_RANGE
    if not low <= number <= high:
        raise TechSaraError(
            f"{name} must be between {low:.2f} and {high:.2f}; got {raw!r}"
        )
    return f"{number:.2f}"


# --------------------------------------------------------------------------
# The user-owned main-model window
# --------------------------------------------------------------------------

#: The supported spelling of the user's context knob. It REPLACES the window
#: the profile would pick and moves every derived key with it, so there is one
#: number to change and nothing left to disagree with it.
MAIN_CONTEXT_KEY = "MAIN_MODEL_MAX_LEN"
#: The historical spelling. Setting it in .env used to do nothing at all: the
#: launcher emits MODEL_MAX_CONTEXT into .runtime/generated.env, and
#: generated.env is the LAST --env-file Compose reads, so the generated value
#: always won. It is honoured now, but only when it asks for a window other
#: than the one the profile would have chosen, so an existing .env that merely
#: repeats the profile number changes nothing.
MAIN_CONTEXT_ALIAS = "MODEL_MAX_CONTEXT"
#: Below the low bound the deployment cannot do its own retrieval prompts; the
#: high bound is 4x the longest native window any supported model has.
MAIN_CONTEXT_RANGE = (4096, 1048576)


def _requested_window(values: Mapping[str, str], name: str) -> int | None:
    """One context key from .env: an int in range, or None when unset."""
    raw = str(values.get(name, "") or "").strip()
    if not raw:
        return None
    low, high = MAIN_CONTEXT_RANGE
    try:
        number = int(raw)
    except ValueError as exc:
        raise TechSaraError(
            f"{name} must be a whole number of tokens between {low:,} and {high:,}; got {raw!r}"
        ) from exc
    if not low <= number <= high:
        raise TechSaraError(f"{name} must be between {low:,} and {high:,} tokens; got {number:,}")
    return number


def profile_context_length(profile: SelectedProfile, context_override: int | None = None) -> int:
    """The window the profile selects, clamped to the model's manifest limit."""
    requested = int(context_override or profile.context_length)
    return min(requested, profile.main_model.context_limit) if profile.main_model else requested


def resolve_requested_context(
    values: Mapping[str, str], *, profile_context: int
) -> tuple[int, str]:
    """The main-model window to serve, and the name of the key that chose it.

    ``MAIN_MODEL_MAX_LEN`` wins outright; ``MODEL_MAX_CONTEXT`` is accepted as
    a deprecated alias only when it differs from the profile's own choice.
    """
    explicit = _requested_window(values, MAIN_CONTEXT_KEY)
    if explicit is not None:
        return explicit, MAIN_CONTEXT_KEY
    alias = _requested_window(values, MAIN_CONTEXT_ALIAS)
    if alias is not None and alias != int(profile_context):
        return alias, MAIN_CONTEXT_ALIAS
    return int(profile_context), "profile"


def _main_model_shape(
    profile: SelectedProfile,
    install_map: Mapping[tuple[str, str], ModelInstall],
    *,
    allow_planned: bool,
) -> ModelShape | None:
    """The main model's geometry, or None when it cannot be read yet."""
    main = profile.main_model
    if not main:
        return None
    install = install_map.get((main.id, main.revision))
    if not install or not (install.ready or (allow_planned and install.status == "planned")):
        return None
    return read_model_shape(Path(install.path).expanduser())


def _require_cluster_kv_capacity(
    cluster_values: Mapping[str, str], *, context: int, shape: ModelShape, key: str
) -> None:
    """Refuse a window the explicit two-node KV budget provably cannot hold.

    vLLM requires the KV pool to fit at least one max-length request, and in
    dual mode the pool is not profiled - it is exactly
    ``--kv-cache-memory-bytes``, so the arithmetic is decidable here instead of
    30 minutes into a start-up.
    """
    parallel = int(cluster_values.get("CLUSTER_TENSOR_PARALLEL_SIZE") or DEFAULT_TENSOR_PARALLEL_SIZE)
    budget = int(cluster_values.get("CLUSTER_KV_CACHE_MEMORY_GIB") or DEFAULT_KV_CACHE_MEMORY_GIB)
    per_token = kv_bytes_per_token(
        shape, tensor_parallel_size=parallel, kv_cache_dtype=CLUSTER_KV_CACHE_DTYPE
    )
    fraction = kv_usable_fraction(shape)
    pool = kv_pool_tokens(budget, per_token, usable_fraction=fraction)
    if pool >= int(context):
        return
    hybrid_note = (
        " (the recurrent layers take their per-sequence state from the same budget)"
        if fraction < 1.0
        else ""
    )
    raise TechSaraError(
        f"{key}={int(context):,} needs a KV pool of at least {int(context):,} tokens; "
        f"CLUSTER_KV_CACHE_MEMORY_GIB={budget} holds {pool:,} tokens at TP={parallel} "
        f"({per_token:,} bytes/token at --kv-cache-dtype {CLUSTER_KV_CACHE_DTYPE}){hybrid_note}; "
        f"set CLUSTER_KV_CACHE_MEMORY_GIB="
        f"{kv_gib_for_tokens(context, per_token, usable_fraction=fraction)} on both nodes "
        f"or lower {key}"
    )


def main_context_notices(
    values: Mapping[str, str], generated: Mapping[str, str], *, profile_context: int
) -> list[str]:
    """The lines ``up`` prints about the window; empty when nothing is unusual.

    Derived from the generated values so the launcher narrates exactly what it
    is about to serve rather than a second, parallel calculation.
    """
    lines: list[str] = []
    _window, source = resolve_requested_context(values, profile_context=profile_context)
    if source == MAIN_CONTEXT_ALIAS:
        lines.append(
            f"Context: {MAIN_CONTEXT_ALIAS} in .env is applied as the main-model window; "
            f"{MAIN_CONTEXT_KEY} is the supported spelling"
        )
    context = int(str(generated.get("MODEL_MAX_CONTEXT") or 0) or 0)
    native = int(str(generated.get("MAIN_MODEL_NATIVE_CONTEXT") or 0) or 0)
    if not (native and context > native):
        return lines
    factor = yarn_factor(context, native)
    lines.append(
        f"Context: {context:,} tokens (model is natively {native:,}; YaRN factor {factor:g} enabled "
        "- long-context extension trades some short-prompt quality)"
    )
    per_token = int(str(generated.get("MAIN_MODEL_KV_BYTES_PER_TOKEN") or 0) or 0)
    fraction = float(str(generated.get("MAIN_MODEL_KV_USABLE_FRACTION") or 1.0) or 1.0)
    if per_token and str(generated.get("TECHSARA_CLUSTER_MODE") or "single") != "dual":
        # Single node: the pool is profiled at start-up from whatever memory is
        # free, so this is the honest estimate rather than a refusal.
        lines.append(
            f"Context: that window needs about "
            f"{kv_gib_for_tokens(context, per_token, usable_fraction=fraction)} GiB of KV cache "
            f"at TP=1 ({per_token:,} bytes/token); vLLM sizes the pool while it starts and will "
            "refuse the window if the GPU cannot hold it"
        )
    return lines


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


def resolve_model_bind_address(values: Mapping[str, str]) -> str:
    """Where the UNAUTHENTICATED model APIs publish, if they publish at all.

    Separate from TECHSARA_BIND_ADDRESS and defaulting to loopback REGARDLESS
    of it (2026-09-01). One variable governed both until an audit found that
    exposing the app — a reasonable thing to do, it sits behind a login —
    had also put four model endpoints with no authentication on the office
    LAN. Nothing in the stack needs them published: the orchestrator and
    Prometheus both reach them by service name on the Docker network, so this
    is purely a debugging convenience and should stay shut by default.
    """
    raw = str(values.get("TECHSARA_MODEL_BIND_ADDRESS", "") or "").strip()
    if not raw:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise TechSaraError(
            "TECHSARA_MODEL_BIND_ADDRESS must be a literal IP address such as "
            f"127.0.0.1 (loopback only) or 0.0.0.0 (all interfaces); got {raw!r}"
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


def _host_for_bind(bind_address: str) -> str:
    """Where a listener bound to ``bind_address`` is reached from this host.

    0.0.0.0 (or nothing) answers on loopback; a specific address answers on
    that address only -- the rule scripts/lib/cluster-common.sh:api_host
    encodes for the shell side.
    """
    return "127.0.0.1" if bind_address in {"0.0.0.0", ""} else bind_address


def router_health_url(
    *,
    publish_model_ports: bool,
    router_shared: bool,
    model_bind_address: str,
    router_port: str | int,
) -> str:
    """The router's /health as seen from the HOST network, or "" when unreachable.

    v2 (strict one-model mode): the router is an internal classifier, never a
    serving path; its health is one DEGRADED signal for the controller
    (contract §2). A published port on 0.0.0.0 is reached on loopback; a
    published port on a specific address is reached on that address. Nothing
    published, or no separate router (``router_shared``: the main model
    answers as the router), means there is nothing to probe.
    """
    if not publish_model_ports or router_shared:
        return ""
    return f"http://{_host_for_bind(model_bind_address)}:{int(router_port)}/health"


def engine_head_api_url(
    *,
    cluster_mode: str,
    publish_model_ports: bool,
    model_bind_address: str,
    api_bind_address: str,
    vllm_port: str | int,
) -> str:
    """Where the engine controller (host network) reaches the vLLM head, or "".

    Derived from where the head ACTUALLY listens, never assumed (review round
    1 blocker: a hard-wired 127.0.0.1 with PUBLISH_MODEL_PORTS=false left the
    controller unable to connect, and it published DOWN for a healthy engine):
    dual mode binds the host at CLUSTER_API_BIND_ADDRESS (0.0.0.0, or the
    Docker bridge gateway when the ports are not published -- reachable from
    the host network either way); a single node is on the bridge and reachable
    from the host network only through a PUBLISHED port. Empty means "not
    observable": the launcher then does not start the controller at all, so
    nothing publishes a state the orchestrator would act on.
    """
    if cluster_mode == "dual":
        return f"http://{_host_for_bind(api_bind_address)}:{int(vllm_port)}"
    if not publish_model_ports:
        return ""
    return f"http://{_host_for_bind(model_bind_address)}:{int(vllm_port)}"


#: The head's GPU exporter as compose.monitoring.yaml publishes it (127.0.0.1
#: only) for the controller's participation probe (contract §5 step 4). The
#: full scrape path, the way the controller's own default names it
#: (controller.py Config.head_gpu_exporter_url): the exporter answers
#: identically on `/` and `/metrics`, but the URL the controller fetches is
#: used verbatim, so the launcher renders the one that is documented.
GPU_EXPORTER_PORT = 9835
HEAD_GPU_EXPORTER_URL = f"http://127.0.0.1:{GPU_EXPORTER_PORT}/metrics"


def worker_gpu_exporter_url(worker_mgmt_ip: str) -> str:
    """The worker's dgx-gpu exporter on its management address, or "" when
    that address is unknown (the controller then records the participation
    probe as unobserved rather than failed)."""
    ip = (worker_mgmt_ip or "").strip()
    return f"http://{ip}:{GPU_EXPORTER_PORT}/metrics" if ip else ""


#: The engine controller's program, bind-mounted read-only into its container
#: (compose.dgx-spark.yaml: ./monitoring/engine-controller:/app), and the two
#: files of it the controller process actually imports.
ENGINE_CONTROLLER_DIR = Path("monitoring") / "engine-controller"
ENGINE_CONTROLLER_CODE_FILES = ("controller.py", "common.py")
ENGINE_CONTROLLER_CODE_SHA_KEY = "ENGINE_CONTROLLER_CODE_SHA"


def controller_code_sha(project_root: Path) -> str:
    """sha256 of the controller's code as ONE digest, or "" when it is absent.

    `docker compose up -d` recreates a container only when its DEFINITION
    changed; a bind-mounted program is not part of the definition, so a
    routine deploy that changed only controller.py used to leave the old
    module running in memory while the deploy record said the commit was
    live (review round 2). Rendered into the service's environment as
    ENGINE_CONTROLLER_CODE_SHA, the digest makes a code change a definition
    change -- and an unchanged one leaves the container exactly as it is.
    The bytes of controller.py then common.py, in that order, hashed as one
    stream: `cat controller.py common.py | sha256sum` on the shell side
    (scripts/cluster-sync.sh does the same for the sentinel's two files).
    """
    digest = hashlib.sha256()
    directory = project_root / ENGINE_CONTROLLER_DIR
    for name in ENGINE_CONTROLLER_CODE_FILES:
        try:
            digest.update((directory / name).read_bytes())
        except OSError:
            return ""
    return digest.hexdigest()
_IMAGE_DIGEST_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$")


def main_model_image(values: Mapping[str, str]) -> str:
    """``MAIN_MODEL_IMAGE``: a digest-pinned vLLM image for the main engine, or "".

    Candidate B (docs/availability/CANDIDATE-B.md) swaps only the MAIN engine's
    image on both ranks; the router and the OCR engine stay on the pinned
    digest in the overlay. A tag would move under a deploy, so only
    ``repository@sha256:<64 hex>`` is accepted; the worker gets the same
    reference through scripts/cluster-sync.sh (it reads the head's rendered
    image), which is what keeps the two ranks byte-identical.
    """
    raw = str(values.get("MAIN_MODEL_IMAGE", "") or "").strip()
    if raw and not _IMAGE_DIGEST_REFERENCE.fullmatch(raw):
        raise TechSaraError(
            "MAIN_MODEL_IMAGE must be a digest-pinned reference such as "
            f"vllm/vllm-openai@sha256:<64 hex digits> (or empty for the overlay's pin); got {raw!r}"
        )
    return raw


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


#: The user-owned .env key scripts/ocr.sh writes when it starts the OCR engine
#: off the head (docs/ocr-on-the-worker.md), and clears when it stops it.
OCR_REMOTE_KEY = "OCR_REMOTE_BASE_URL"


def remote_ocr_url(values: Mapping[str, str]) -> str:
    """The OCR engine scripts/ocr.sh started elsewhere, or "" for the head's own.

    Checked the way the external-development URLs are (an http(s) scheme, a
    host, no embedded credentials, no whitespace or control characters, at
    most 512 characters) but WITHOUT their local-host rule: the whole point of
    this key is an address on the other node, ``http://192.168.9.68:30004/v1``,
    which ``_external_url`` would refuse as non-local. A bad value raises
    rather than falling back to the head's engine, because the operator just
    moved 17 GB of memory on the strength of it and a silent fallback would
    start that engine again on the node they were emptying.
    """
    raw = str(values.get(OCR_REMOTE_KEY, "") or "").strip()
    if not raw:
        return ""
    problem = ""
    if len(raw) > 512:
        problem = "is longer than 512 characters"
    elif any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw):
        problem = "contains whitespace or control characters"
    else:
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
        except ValueError:
            problem = "is not a valid URL"
        else:
            if parsed.scheme not in {"http", "https"}:
                problem = "must start with http:// or https://"
            elif not hostname:
                problem = "has no host"
            elif parsed.username or parsed.password:
                problem = "must not embed credentials"
    if problem:
        raise TechSaraError(
            f"{OCR_REMOTE_KEY} in .env {problem}; expected the address scripts/ocr.sh recorded, "
            "for example http://192.168.9.68:30004/v1 (clear the key to use the head's own vllm-ocr)"
        )
    return raw.rstrip("/")


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
    model_bind_address = resolve_model_bind_address(user_values)
    publish_model_ports = _truthy(user_values.get("PUBLISH_MODEL_PORTS"))
    # Validated here, before anything is generated, like every other knob.
    remote_ocr = remote_ocr_url(user_values)
    search_provider = search_provider.strip().lower() or "searxng"
    if search_provider not in {"searxng", "tavily", "brave"}:
        raise TechSaraError(
            "SEARCH_PROVIDER must be one of searxng, tavily, or brave"
        )
    cache = cache_root.expanduser().resolve()
    install_map = {(item.model_id, item.revision): item for item in installs}

    # The window: the profile's choice unless the user owns it in .env. An
    # explicit MAIN_MODEL_MAX_LEN replaces the profile value outright (that is
    # the point of the knob) and, when it is longer than the window the model
    # was trained for, brings a YaRN --hf-overrides argument with it.
    profile_context = profile_context_length(profile, context_override)
    context, context_source = resolve_requested_context(user_values, profile_context=profile_context)
    shape = _main_model_shape(profile, install_map, allow_planned=allow_planned)
    rope_override = ""
    if shape is not None and context > shape.native_context:
        rope_override = rope_override_argument(shape, yarn_factor(context, shape.native_context))

    # Two-node DGX Spark cluster: CLUSTER_MODE=auto (default) discovers and
    # verifies a second Spark on the dgx-spark profile only; every other host
    # resolves to single without touching the network, so its generated.env
    # gains just TECHSARA_CLUSTER_MODE/TECHSARA_CLUSTER_REASON.
    # The two main-engine switches the overlays used to hard-code. Validated
    # before anything is generated, like every other knob, and rendered ONCE
    # here so the dual-mode engine string and the single-node overlays cannot
    # disagree about them.
    prefix_caching = resolve_prefix_caching(user_values)
    speculative_argument = speculative_config_argument(user_values)
    gdn_prefill_argument = gdn_prefill_backend_argument(user_values)
    main_model_image(user_values)
    cluster = resolve_cluster(
        user_values,
        profile_id=profile.hardware_profile_id,
        publish_model_ports=publish_model_ports,
        context=context,
        startup_arguments=profile.main_model.startup_arguments if profile.main_model else (),
        vllm_port=resolve_published_port(user_values, "VLLM_PORT"),
        detectors=CLUSTER_DETECTORS,
        discovery=CLUSTER_DISCOVERY,
        rope_override=rope_override,
        enable_prefix_caching=prefix_caching,
    )
    cluster_mode = cluster.mode
    cluster_values = cluster.generated()
    if shape is not None and cluster_mode == "dual":
        _require_cluster_kv_capacity(
            cluster_values,
            context=context,
            shape=shape,
            key=context_source if context_source != "profile" else MAIN_CONTEXT_KEY,
        )

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
    if remote_ocr and ocr is not None:
        # scripts/ocr.sh moved the engine off the head: the orchestrator is
        # pointed at it, and cli._compose_profiles leaves the head's `ocr`
        # profile off so `techsara up` stops starting vllm-ocr here. The
        # model, the enabled flag and every OCR_* capability value below are
        # exactly what they would be for the head's engine -- OCR is still
        # on, just elsewhere. With OCR off (skipped, or not a feature of the
        # profile) the key is validated but changes nothing.
        ocr_url = remote_ocr
    router_url = main_url if profile.router_shared else "http://vllm-router:30002/v1"
    values: dict[str, str] = {
        "TECHSARA_PROFILE": profile.id,
        "TECHSARA_HARDWARE_PROFILE": profile.hardware_profile_id,
        "TECHSARA_RUNTIME_BACKEND": profile.runtime_backend,
        "TECHSARA_MODEL_CACHE": str(cache),
        "TECHSARA_GENERATED_ENV": str(layout.generated_env),
        "TECHSARA_SECRET_ENV": str(layout.secrets_env),
        # The two env_file layers the DGX overlays read (paths only, no
        # secrets): the controller's token file and the per-rank engine env.
        "TECHSARA_CONTROLLER_ENV": str(layout.controller_env),
        "TECHSARA_ENGINE_ENV": str(layout.engine_env),
        "TECHSARA_BIND_ADDRESS": bind_address,
        "TECHSARA_MODEL_BIND_ADDRESS": model_bind_address,
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
        # Always emitted, empty when the model's geometry could not be read, so
        # the overlays interpolate deterministically in both states.
        "MAIN_MODEL_NATIVE_CONTEXT": str(shape.native_context) if shape else "",
        "MAIN_MODEL_ROPE_OVERRIDE": rope_override,
        "MAIN_MODEL_KV_BYTES_PER_TOKEN": (
            str(kv_bytes_per_token(shape, tensor_parallel_size=1, kv_cache_dtype=CLUSTER_KV_CACHE_DTYPE))
            if shape
            else ""
        ),
        "MAIN_MODEL_KV_USABLE_FRACTION": f"{kv_usable_fraction(shape):g}",
        "MODEL_CONCURRENCY": str(profile.concurrency),
        "SEARCH_ENABLED": _bool(search_enabled),
        "SEARCH_PROVIDER": search_provider,
        "SEARXNG_URL": (
            "http://searxng:8080"
            if search_enabled and search_provider == "searxng"
            else ""
        ),
        "MAIN_STARTUP_ARGUMENTS": shlex.join(main.startup_arguments) if main else "",
        # The prefix-caching and speculative-decoding flags for the single-node
        # overlays (compose.dgx-spark.yaml / compose.nvidia.yaml). Always
        # emitted so interpolation is deterministic; the orchestrator reads the
        # normalised boolean so /health reports the launch, not a belief.
        PREFIX_CACHING_KEY: _bool(prefix_caching),
        "MAIN_MODEL_PREFIX_CACHING_ARGUMENT": prefix_caching_argument(prefix_caching),
        "MAIN_MODEL_SPECULATIVE_ARGUMENT": speculative_argument,
        "MAIN_MODEL_GDN_PREFILL_ARGUMENT": gdn_prefill_argument,
        "MAIN_GPU_MEMORY_UTILIZATION": {"nvidia-large": "0.82", "nvidia-medium": "0.82", "nvidia-small": "0.85", "nvidia-minimal": "0.82"}.get(profile.hardware_profile_id, "0.35"),
        "VLLM_SHM_SIZE": "16g",
    }
    # Sidecar GPU budgets are emitted ONLY when the user set one, so the
    # per-profile default in the overlay stays the single source of truth.
    #
    # Emitting a default here instead would be wrong in both directions.
    # generated.env is the LAST --env-file, so a literal here outranks the
    # user's .env: that is the defect this replaced, where setting
    # OCR_GPU_MEMORY_UTILIZATION in .env changed nothing and reported nothing.
    # But emitting a launcher-side default is equally wrong, because the
    # defaults are NOT profile-independent -- embed is 0.04 on dgx-spark
    # (compose.dgx-spark.yaml) and 0.08 on nvidia (compose.nvidia.yaml). A
    # single default here would silently double embed's reservation on one
    # profile. The overlays already spell every one as `${VAR:-default}`, so
    # interpolation stays deterministic when the key is absent.
    for _sidecar in (
        "EMBED_GPU_MEMORY_UTILIZATION",
        "OCR_GPU_MEMORY_UTILIZATION",
        "ROUTER_GPU_MEMORY_UTILIZATION",
    ):
        _chosen = _aux_gpu_memory_utilization(user_values, _sidecar, "")
        if _chosen:
            values[_sidecar] = _chosen
    # Always emitted so the published-model overlay interpolates deterministically
    # whether or not it is layered in.
    for name in MODEL_PORT_DEFAULTS:
        values[name] = str(resolve_published_port(user_values, name))
    # What the engine controller (compose.dgx-spark.yaml, host network) can
    # reach, generated from the real bind addresses so the compose files never
    # assume loopback (contract §2: an unreachable probe is MONITORING_UNKNOWN,
    # never DOWN). The router lives on the bridge, so from the host network it
    # is reachable only through its published port; when the model ports are
    # not published -- or when the router IS the main model -- the value is
    # empty and the controller reports the router as "not configured", never
    # as unhealthy. The head URL follows the same rule; empty means the
    # launcher leaves the controller unstarted (cli._start_compose).
    values["TECHSARA_ROUTER_HEALTH_URL"] = router_health_url(
        publish_model_ports=publish_model_ports,
        router_shared=bool(profile.router_shared),
        model_bind_address=model_bind_address,
        router_port=values["VLLM_ROUTER_PORT"],
    )
    values["TECHSARA_ENGINE_HEAD_API_URL"] = engine_head_api_url(
        cluster_mode=cluster_mode,
        publish_model_ports=publish_model_ports,
        model_bind_address=model_bind_address,
        api_bind_address=cluster_values.get("CLUSTER_API_BIND_ADDRESS", ""),
        vllm_port=values["VLLM_PORT"],
    )
    # The two GPU exporters the controller samples during its participation
    # probe (contract §5 step 4): the head's, published on loopback by
    # compose.monitoring.yaml, and the worker's on its management address.
    # Dual mode only -- one GPU has no second rank to prove. Empty = not
    # observable (the controller records the probe as unproven, not failed).
    worker_mgmt_ip = cluster_values.get("CLUSTER_WORKER_MGMT_IP", "")
    values["TECHSARA_HEAD_GPU_EXPORTER_URL"] = HEAD_GPU_EXPORTER_URL if cluster_mode == "dual" else ""
    values["TECHSARA_WORKER_GPU_EXPORTER_URL"] = (
        worker_gpu_exporter_url(worker_mgmt_ip) if cluster_mode == "dual" else ""
    )
    # The controller's code digest (see controller_code_sha): part of the
    # engine-controller service definition, so a code-only change recreates
    # the container on the next `up` and an unchanged one does not.
    values[ENGINE_CONTROLLER_CODE_SHA_KEY] = controller_code_sha(layout.project_root)
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
            # The main model serves exactly the window above, extension
            # included: clamping it back to the manifest limit here is what
            # used to make one knob disagree with itself. Every other role
            # keeps its own limit.
            if model is not None and model is main:
                role_context = context
            else:
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
