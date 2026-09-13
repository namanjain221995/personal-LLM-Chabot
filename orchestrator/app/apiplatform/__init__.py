"""The developer platform's server-side primitives (CONTRACT-3 §2).

This package is the half of the public API that is NOT HTTP: credentials,
tenancy, scopes, quotas, idempotency, usage recording and webhook delivery.
`app/publicapi/` is the `/v1` router that consumes it. The split is what keeps
the security decisions testable without a request: an API key either parses or
does not, a scope either covers an endpoint or does not, and neither question
needs FastAPI, a socket, or in the case of `keys`, a database.

Module ownership — one owner per file, so the pieces can be built side by side:

    keys.py      the key format, the offline checksum, the HMAC digest, the
                 pepper, and the rotate/revoke lifecycle          (wave 1)
    scopes.py    the closed scope vocabulary and `requires()`     (wave 1)
    console_api.py  the BROWSER-side console router mounted at
                 /admin/api/developers: projects, service accounts, keys,
                 models, usage, request logs, webhooks, limits and the
                 key-free playground. Session + capability, never a key
                 (CONTRACT-3 §6, §17)                             (wave 2)

Later waves add quotas, idempotency, usage recording and `webhooks/` beside
these; a new module can be added without touching one already shipped.

ONE IMPORT RULE CHANGED IN WAVE 2, and it is worth naming. `keys` and `scopes`
import neither `db` nor FastAPI, which is what lets a security decision be
tested without a request; `console_api` necessarily imports both, because it is
an HTTP surface over the V34 tables. Importing this package therefore now costs
FastAPI and `app.db` — both of which any process serving this application has
already loaded. It does NOT cost the engine stack: `app.llm` is imported inside
the one function that generates, so the console's project and key routes stay
importable without it.

`console_api.router` is mounted by `app/main.py` (a single-owner file in this
programme). Nothing here calls `include_router`.

Nothing in this package is importable from the chat application's request
path, and nothing in it reads a person's conversation: CONTRACT-3 §1 keeps
the browser identity (`ts_session`, `authn/`) and the machine identity (an
API key) apart, and this is the machine half.
"""
from __future__ import annotations

from . import console_api, keys, scopes
from .console_api import router as console_router
from .keys import (
    ENVIRONMENTS,
    KEY_PREFIX,
    KeyRotation,
    MintedKey,
    ParsedKey,
    PepperUnavailable,
    configure_pepper_store,
    key_digest,
    mint_key,
    plan_revocation,
    plan_rotation,
    redact,
    split_key,
    verify_secret,
)
from .scopes import (
    ALL_SCOPES,
    DEFAULT_SCOPES,
    InsufficientScopeError,
    Scope,
    ScopeRequirement,
    UnknownScopeError,
    format_scopes,
    parse_scopes,
    requires,
    scope_names,
)

__all__ = [
    "console_api",
    "console_router",
    "keys",
    "scopes",
    # keys
    "ENVIRONMENTS",
    "KEY_PREFIX",
    "KeyRotation",
    "MintedKey",
    "ParsedKey",
    "PepperUnavailable",
    "configure_pepper_store",
    "key_digest",
    "mint_key",
    "plan_revocation",
    "plan_rotation",
    "redact",
    "split_key",
    "verify_secret",
    # scopes
    "ALL_SCOPES",
    "DEFAULT_SCOPES",
    "InsufficientScopeError",
    "Scope",
    "ScopeRequirement",
    "UnknownScopeError",
    "format_scopes",
    "parse_scopes",
    "requires",
    "scope_names",
]
