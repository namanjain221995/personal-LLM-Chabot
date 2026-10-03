#!/usr/bin/env bash
# Write the three env files of the dev stack, see ops/dev/README.md.
#
# Usage, from the worktree root:
#   ops/dev/init-env.sh --main http://HOST:PORT [--router URL] [--embed URL] [--rerank URL]
#
# The file locations are the three paths named in ops/dev/stack.vars, namely
# TECHSARA_SECRET_ENV, TECHSARA_GENERATED_ENV and TECHSARA_DEV_ENGINES_ENV, so
# this script writes exactly what Compose reads. DEV_ENV_DIR, default ops/dev,
# re-roots them for tests.
#
# TECHSARA_SECRET_ENV is created once with fresh random values, mode 0600,
# never printed. A second run keeps every value and only appends a missing key.
# TECHSARA_DEV_ENGINES_ENV holds CAP_UPSTREAMS and CAP_MAX_INFLIGHT for the
# inference cap. It is the only file that names engines on the head.
# TECHSARA_GENERATED_ENV holds the engine and feature settings of the dev
# orchestrator. Every engine URL in it points at http://inference-cap:9100/NAME
# and never at a host. Disabled engines point at disabled.invalid as the
# launcher does.
#
# Each URL is http://HOST:PORT with an optional /v1 and nothing else. No https,
# because the cap speaks plain HTTP to the engines. No user or password in it.
# Loopback hosts are refused because the containers run on the worker and
# cannot reach loopback addresses of the head. DEV_INIT_ALLOW_LOOPBACK=1 lifts
# that for tests only. Every engine given is probed with GET /v1/models for its
# served model id and max_model_len. A failed probe refuses and writes nothing.
#
# No docker command here, this only writes files.
#
# The autopilot guard parses this text and checks the words of column-0
# comments as if they were arguments, so these comments avoid quotes, shell
# operators and words that look like file names of credentials.
set -euo pipefail

die() {
  printf 'init-env: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'USAGE'
usage: ops/dev/init-env.sh --main http://HOST:PORT [--router http://HOST:PORT]
                           [--embed http://HOST:PORT] [--rerank http://HOST:PORT]

  --main    the production main vLLM (OpenAI-compatible); required
  --router  a router engine; omitted = the main model serves router and agent
  --embed   an embedding engine; omitted = embeddings disabled
  --rerank  a reranker engine; omitted = reranking disabled
Run from the worktree root. DEV_ENV_DIR overrides the output directory.
USAGE
}

if [ ! -f compose.yaml ] || [ ! -f ops/dev/stack.vars ]; then
  die "run from the worktree root (compose.yaml and ops/dev/stack.vars must be here)"
fi

main_url=""
router_url=""
embed_url=""
rerank_url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --main | --router | --embed | --rerank)
      [ "$#" -ge 2 ] || die "$1 needs a URL"
      case "$1" in
        --main) main_url="$2" ;;
        --router) router_url="$2" ;;
        --embed) embed_url="$2" ;;
        --rerank) rerank_url="$2" ;;
      esac
      shift 2
      ;;
    --main=*) main_url="${1#*=}"; shift ;;
    --router=*) router_url="${1#*=}"; shift ;;
    --embed=*) embed_url="${1#*=}"; shift ;;
    --rerank=*) rerank_url="${1#*=}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
if [ -z "$main_url" ]; then
  usage >&2
  die "--main is required"
fi

out_dir="${DEV_ENV_DIR:-ops/dev}"
allow_loopback="${DEV_INIT_ALLOW_LOOPBACK:-0}"
umask 077

exec python3 - "$out_dir" "$allow_loopback" "$main_url" "$router_url" "$embed_url" "$rerank_url" <<'PY'
import ipaddress
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

out_dir, allow_loopback = sys.argv[1], sys.argv[2] == "1"
given = dict(zip(("main", "router", "embed", "rerank"), sys.argv[3:7]))

CAP = "http://inference-cap:9100"
DISABLED_URL = "http://disabled.invalid/v1"
CAP_MAX_INFLIGHT = 2
SECRET_KEYS = ("POSTGRES_PASSWORD", "SESSION_SECRET", "API_KEY_PEPPER")
STACK_VARS = "ops/dev/stack.vars"
STACK_PREFIX = "ops/dev/"
MANIFEST = "config/model-manifest.yaml"


def die(message):
    print(f"init-env: {message}", file=sys.stderr)
    raise SystemExit(2)


def stack_paths():
    """The three file locations, from stack.vars (the file Compose reads)."""
    values = {}
    with open(STACK_VARS, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    paths = {}
    for role, key in (("secrets", "TECHSARA_SECRET_ENV"), ("generated", "TECHSARA_GENERATED_ENV"),
                      ("engines", "TECHSARA_DEV_ENGINES_ENV")):
        value = values.get(key, "")
        rel = value[len(STACK_PREFIX):] if value.startswith(STACK_PREFIX) else ""
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            die(f"{STACK_VARS}: {key} must be a path under {STACK_PREFIX}")
        paths[role] = os.path.join(out_dir, rel)
    return paths


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


def engine_base(role, raw):
    """http://HOST:PORT[/v1] -> http://HOST:PORT, or refuse."""
    if len(raw) > 256 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        die(f"--{role}: not a URL")
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        die(f"--{role}: not a valid URL ({exc})")
    if parts.scheme != "http":
        die(f"--{role}: only http:// URLs (the cap speaks plain HTTP to the engines); got {parts.scheme or 'no'} scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        die(f"--{role}: credentials in the URL are not accepted")
    if parts.query or parts.fragment or parts.path.rstrip("/") not in ("", "/v1"):
        die(f"--{role}: give http://HOST:PORT (an optional /v1, nothing else)")
    host = parts.hostname or ""
    if port is None:
        die(f"--{role}: give an explicit port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if not HOST_RE.match(host):
            die(f"--{role}: not a host name or address")
    loopback = (address is not None and (address.is_loopback or address.is_unspecified)) or (
        address is None and (host.lower() == "localhost" or host.lower().endswith(".localhost")))
    if loopback and not allow_loopback:
        die(f"--{role}: {host} is a loopback address; the dev containers run on the worker and "
            "cannot reach the head's loopback. Give an address the worker can reach.")
    netloc = f"[{host}]:{port}" if address is not None and address.version == 6 else f"{host}:{port}"
    return f"http://{netloc}"


def probe(role, base):
    """GET /v1/models: (served model id, max_model_len or None)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"{base}/v1/models"
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=8) as resp:
            payload = json.loads(resp.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        die(f"--{role}: GET {url} answered HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f"--{role}: GET {url} failed ({type(exc).__name__}: {getattr(exc, 'reason', exc)})")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or not data[0].get("id"):
        die(f"--{role}: GET {url} listed no model")
    model_id = str(data[0]["id"])
    window = data[0].get("max_model_len")
    window = window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
    # Written unquoted into an env file Compose parses: no blanks, quotes,
    # comment or interpolation characters.
    if len(model_id) > 256 or not MODEL_ID_RE.match(model_id):
        die(f"--{role}: GET {url} returned a model id this script will not write: {model_id[:80]!r}")
    return model_id, window


def manifest_models():
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            models = json.load(handle).get("models", {})
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for spec in models.values():
        if isinstance(spec, dict):
            api_id = spec.get("served_id") or spec.get("id")
            if api_id:
                out.setdefault(api_id, spec)
    return out


def flag(value):
    return "true" if value else "false"


def capability(prefix, model_id, spec, context):
    """The launcher's per-role keys (environment._capability_values) for one
    served model. A model the manifest does not know keeps the orchestrator's
    own defaults for everything but the window."""
    output = min(8192, max(256, context // 4))
    if spec is None:
        return {
            f"{prefix}_ENABLED": "true",
            f"{prefix}_CONTEXT_LENGTH": str(context),
            f"{prefix}_OUTPUT_LIMIT": str(output),
            f"{prefix}_CONCURRENCY": str(CAP_MAX_INFLIGHT),
            f"{prefix}_REQUIRES_AUTHENTICATION": "false",
        }
    has = lambda name: bool(spec.get(name))
    return {
        f"{prefix}_PROVIDER": str(spec.get("provider") or "local"),
        f"{prefix}_BACKEND": str(spec.get("backend") or "vllm"),
        f"{prefix}_ENABLED": "true",
        f"{prefix}_SUPPORTS_CHAT": flag(has("supports_chat")),
        f"{prefix}_SUPPORTS_STREAMING": flag(has("supports_streaming")),
        f"{prefix}_SUPPORTS_REASONING": flag(has("supports_reasoning")),
        f"{prefix}_SUPPORTS_TOOL_CALLING": flag(has("supports_tool_calling")),
        f"{prefix}_SUPPORTS_STRUCTURED_OUTPUT": flag(has("supports_structured_output")),
        f"{prefix}_SUPPORTS_VISION": flag(has("supports_vision")),
        f"{prefix}_SUPPORTS_EMBEDDINGS": flag(has("supports_embeddings")),
        f"{prefix}_SUPPORTS_RERANKING": flag(has("supports_reranking")),
        f"{prefix}_SUPPORTS_OCR": flag(has("supports_ocr")),
        f"{prefix}_SUPPORTS_TOKENIZATION": flag(spec.get("endpoint_type") != "in-process"),
        f"{prefix}_REASONING_FIELD": "auto" if has("supports_reasoning") else "none",
        f"{prefix}_CONTEXT_LENGTH": str(context),
        f"{prefix}_OUTPUT_LIMIT": str(output),
        f"{prefix}_CONCURRENCY": str(CAP_MAX_INFLIGHT),
        f"{prefix}_EXTRA_BODY_ALLOWED": (
            "chat_template_kwargs" if spec.get("backend") == "vllm-cuda" and has("supports_reasoning") else ""),
        f"{prefix}_REQUIRES_AUTHENTICATION": "false",
    }


def disabled(prefix):
    """The launcher's values for a role with no model, except CONCURRENCY:
    the orchestrator refuses 0 (model_capabilities: concurrency must be >= 1)."""
    values = {f"{prefix}_PROVIDER": "disabled", f"{prefix}_BACKEND": "disabled", f"{prefix}_ENABLED": "false"}
    for name in ("CHAT", "STREAMING", "REASONING", "TOOL_CALLING", "STRUCTURED_OUTPUT", "VISION",
                 "EMBEDDINGS", "RERANKING", "OCR", "TOKENIZATION"):
        values[f"{prefix}_SUPPORTS_{name}"] = "false"
    values.update({
        f"{prefix}_REASONING_FIELD": "none",
        f"{prefix}_CONTEXT_LENGTH": "0",
        f"{prefix}_OUTPUT_LIMIT": "0",
        f"{prefix}_CONCURRENCY": "1",
        f"{prefix}_EXTRA_BODY_ALLOWED": "",
        f"{prefix}_REQUIRES_AUTHENTICATION": "false",
    })
    return values


def atomic_write(path, text):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_secrets(path):
    """Create the secrets file once; afterwards only append a missing key.
    Values are generated here and never printed."""
    present = set()
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
        for line in existing.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                present.add(line.split("=", 1)[0].strip())
    missing = [key for key in SECRET_KEYS if key not in present]
    if not missing:
        os.chmod(path, 0o600)
        return "kept"
    header = "" if existing else (
        "# The dev stack's synthetic secrets: random per worktree, never production values.\n"
        "# Written by ops/dev/init-env.sh; never edit or print. Deleting this file orphans\n"
        "# the dev database (its password lives in the llmdev_pgdata volume).\n")
    body = existing if not existing or existing.endswith("\n") else existing + "\n"
    body = header + body + "".join(f"{key}={secrets.token_hex(32)}\n" for key in missing)
    atomic_write(path, body)
    return "created" if not existing else "extended"


def render(values, title):
    lines = [f"# {title}", f"# Written by ops/dev/init-env.sh at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}; "
             "re-run it instead of editing.", ""]
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            die(f"internal: {key} has a line break")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


paths = stack_paths()
bases = {role: engine_base(role, raw) for role, raw in given.items() if raw}
probed = {role: probe(role, base) for role, base in bases.items()}
main_id, main_window = probed["main"]
if main_window is None:
    die("--main: /v1/models did not report max_model_len; cannot size the context window")
known = manifest_models()

engines = {
    "CAP_UPSTREAMS": ",".join(f"{role}={base}" for role, base in bases.items()),
    "CAP_MAX_INFLIGHT": str(CAP_MAX_INFLIGHT),
}

main_url = f"{CAP}/main/v1"
context = main_window
gen = {
    "OPENAI_BASE_URL": main_url,
    "MAIN_MODEL": main_id,
    "VISION_BASE_URL": main_url,
    "VISION_MODEL": main_id,
}
gen.update(capability("MAIN", main_id, known.get(main_id), context))
gen.update(capability("VISION", main_id, known.get(main_id), context))

if "router" in probed:
    router_id, router_window = probed["router"]
    router_spec = known.get(router_id)
    router_limit = router_window or (router_spec or {}).get("context_limit") or context
    router_context = min(context, int(router_limit))
    router_url, router_mode = f"{CAP}/router/v1", "real"
else:
    router_id, router_spec, router_context = main_id, known.get(main_id), context
    router_url, router_mode = main_url, "shared"
gen.update({"ROUTER_BASE_URL": router_url, "ROUTER_MODEL": router_id,
            "AGENT_BASE_URL": router_url, "AGENT_MODEL": router_id})
gen.update(capability("ROUTER", router_id, router_spec, router_context))
gen.update(capability("AGENT", router_id, router_spec, router_context))

if "embed" in probed:
    embed_id, embed_window = probed["embed"]
    embed_spec = known.get(embed_id)
    embed_limit = embed_window or (embed_spec or {}).get("context_limit") or context
    embed_url = f"{CAP}/embed/v1"
    gen.update({"EMBED_BASE_URL": embed_url, "EMBED_VIA": embed_url, "EMBED_MODEL": embed_id})
    gen.update(capability("EMBED", embed_id, embed_spec, min(context, int(embed_limit))))
else:
    gen.update({"EMBED_BASE_URL": DISABLED_URL, "EMBED_VIA": DISABLED_URL, "EMBED_MODEL": "disabled"})
    gen.update(disabled("EMBED"))

if "rerank" in probed:
    rerank_id, rerank_window = probed["rerank"]
    rerank_spec = known.get(rerank_id)
    rerank_limit = rerank_window or (rerank_spec or {}).get("context_limit") or context
    gen.update({"RERANK_BACKEND": "remote", "RERANK_ENABLED": "true",
                "RERANK_BASE_URL": f"{CAP}/rerank", "RERANK_MODEL": rerank_id})
    gen.update(capability("RERANKER", rerank_id, rerank_spec, min(context, int(rerank_limit))))
else:
    gen.update({"RERANK_BACKEND": "disabled", "RERANK_ENABLED": "false",
                "RERANK_BASE_URL": "", "RERANK_MODEL": "disabled"})
    gen.update(disabled("RERANKER"))

gen.update({"OCR_ENABLED": "false", "OCR_BASE_URL": DISABLED_URL, "OCR_MODEL": "disabled"})
gen.update(disabled("OCR"))
gen.update({
    # The window, as the launcher writes it: all three equal the served one.
    "MODEL_MAX_CONTEXT": str(context),
    "DEFAULT_MAX_CONTEXT": str(context),
    "REPORT_MAX_CONTEXT": str(context),
    # Unknown from /v1/models (the launcher reads it from config.json).
    "MAIN_MODEL_NATIVE_CONTEXT": "",
    "MODEL_CONCURRENCY": str(CAP_MAX_INFLIGHT),
    # Off in dev: search and its background crawler, speech, video, the voice
    # archive, live Salesforce, the engine controller's poller.
    "SEARCH_ENABLED": "false",
    "SEARCH_PROVIDER": "searxng",
    "SEARXNG_URL": "",
    "WEB_KNOWLEDGE_WORKER_ENABLED": "false",
    "ASR_ENABLED": "false",
    "VIDEO_ANALYSIS_ENABLED": "false",
    "VOICE_ARCHIVE_ENABLED": "false",
    "SF_LIVE_ENABLED": "false",
    "ENGINE_CONTROLLER_URL": "",
})

for key, value in gen.items():
    if "://" in value and not (value.startswith(CAP + "/") or value == DISABLED_URL):
        die(f"internal: {key} would point outside the cap")

secrets_state = ensure_secrets(paths["secrets"])
atomic_write(paths["engines"], render(engines, "The dev inference cap's upstreams. Gitignored; never commit."))
atomic_write(paths["generated"], render(gen, "The dev orchestrator's generated settings. No secrets, no host addresses."))

print(f"init-env: secrets {secrets_state} ({paths['secrets']}, mode 0600, values not shown)")
print(f"init-env: wrote {paths['engines']} and {paths['generated']} (mode 0600)")
print(f"  main    real      {main_id}  window {context}  upstream {bases['main']}")
if router_mode == "real":
    print(f"  router  real      {router_id}  window {router_context}  upstream {bases['router']}")
else:
    print("  router  shared    the main model serves router and agent calls")
print("  vision  shared    the main model")
for role, label in (("embed", "embed "), ("rerank", "rerank")):
    if role in probed:
        print(f"  {label}  real      {probed[role][0]}  upstream {bases[role]}")
    else:
        print(f"  {label}  disabled  (no --{role})")
print("  ocr, asr, video, search, live Salesforce: disabled")
unknown = sorted({mid for mid in [main_id] + [p[0] for p in probed.values()] if mid not in known})
if unknown:
    print(f"  note: not in {MANIFEST}, capability flags left to the orchestrator's defaults: {', '.join(unknown)}")
print(f"  all engine traffic goes through inference-cap, at most {CAP_MAX_INFLIGHT} requests in flight")
PY
