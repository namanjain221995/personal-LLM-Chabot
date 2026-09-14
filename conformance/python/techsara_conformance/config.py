"""Where the suite points, read once per run.

Precedence, highest first: command-line option, environment variable, the
JSON written by tools/provision_key.py (`--keys-file` / TECHSARA_KEYS_FILE).
There is deliberately NO default base URL: a conformance run against the
wrong deployment is worse than no run (2026-09-13).

API keys are never accepted on the command line (shell history, `ps`), only
from the environment or the 0600 keys file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

#: The six public ids of CONTRACT-3 §15. Overridable per deployment because a
#: deployment may legitimately not configure one (§15 "declared when").
DEFAULT_MODELS = {
    "chat": "techsara-35b",
    "vision": "techsara-8b-vision",
    "ocr": "techsara-ocr",
    "embed": "techsara-embed",
    "rerank": "techsara-rerank",
    "whisper": "techsara-whisper",
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def normalise_base_url(value: str) -> str:
    """Accept `https://host`, `https://host/` or `https://host/v1` and return
    the `/v1` form the OpenAI client wants as `base_url`."""
    url = value.strip().rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


@dataclass(frozen=True)
class Target:
    base_url: str
    api_key: str
    limited_api_key: Optional[str]
    models: Dict[str, str]
    #: Output budget for every ordinary generation. Small on purpose: the
    #: suite proves shapes and semantics, not throughput, and may be pointed at
    #: an engine that real people are using.
    small_output_tokens: int
    long_output_tokens: int
    #: The ceiling CONTRACT-3 §8.3 promises for the chat model.
    expected_chat_max_output_tokens: int
    background_poll_timeout_s: float
    request_timeout_s: float
    capacity_probe: bool
    #: The scopes the MAIN key holds, when known: from the keys file that
    #: supplied the key, or TECHSARA_API_KEY_SCOPES beside TECHSARA_API_KEY.
    #: None = unknown, and then no test is skipped for a scope.
    #:
    #: WHY (2026-09-13, review finding): CONTRACT-3 §7 — "stored keys keep their
    #: stored scopes". A key minted before the rebuild that added
    #: embeddings.write / rerank.write / audio.write answers 403 on those routes,
    #: which would read as ~21 server defects on a correct deployment.
    scopes: Optional[FrozenSet[str]] = None
    scopes_source: str = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return self.base_url[: -len("/v1")]


def load(options: Any) -> Target:
    keys: Dict[str, Any] = {}
    keys_file = options.getoption("--keys-file") or os.environ.get("TECHSARA_KEYS_FILE")
    if keys_file:
        keys = json.loads(Path(keys_file).read_text(encoding="utf-8"))

    base = options.getoption("--base-url") or os.environ.get("TECHSARA_BASE_URL") or keys.get("base_url")
    if not base:
        raise ValueError(
            "no target: pass --base-url, set TECHSARA_BASE_URL, or point --keys-file at "
            "the JSON written by tools/provision_key.py"
        )
    env_key = os.environ.get("TECHSARA_API_KEY")
    api_key = env_key or keys.get("api_key")
    if not api_key:
        raise ValueError("no API key: set TECHSARA_API_KEY or use --keys-file")
    scopes: Optional[FrozenSet[str]] = None
    scopes_source = "unknown"
    if env_key:
        raw_scopes = os.environ.get("TECHSARA_API_KEY_SCOPES", "").strip()
        if raw_scopes:
            scopes = frozenset(s.strip() for s in raw_scopes.split(",") if s.strip())
            scopes_source = "TECHSARA_API_KEY_SCOPES"
    elif isinstance(keys.get("scopes"), list):
        scopes = frozenset(str(s) for s in keys["scopes"])
        scopes_source = f"keys file {keys_file}"
    limited = os.environ.get("TECHSARA_LIMITED_API_KEY") or keys.get("limited_api_key")

    models = dict(DEFAULT_MODELS)
    for role in models:
        override = os.environ.get(f"TECHSARA_{role.upper()}_MODEL")
        if override:
            models[role] = override

    return Target(
        base_url=normalise_base_url(base),
        api_key=api_key,
        limited_api_key=limited or None,
        models=models,
        small_output_tokens=_env_int("TECHSARA_SMALL_OUTPUT_TOKENS", 16),
        long_output_tokens=int(
            options.getoption("--long-output-tokens") or _env_int("TECHSARA_LONG_OUTPUT_TOKENS", 1024)
        ),
        expected_chat_max_output_tokens=_env_int("TECHSARA_EXPECT_CHAT_MAX_OUTPUT_TOKENS", 1_000_000),
        background_poll_timeout_s=_env_float("TECHSARA_BACKGROUND_POLL_TIMEOUT_S", 180.0),
        request_timeout_s=_env_float("TECHSARA_REQUEST_TIMEOUT_S", 180.0),
        capacity_probe=bool(options.getoption("--capacity-probe")),
        scopes=scopes,
        scopes_source=scopes_source,
    )
