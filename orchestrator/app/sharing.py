"""Conversation sharing: the token, the policy, and the snapshot.

Three things live here because they are one decision made in three parts —
whether a conversation MAY be shared, what a shared copy of it CONTAINS, and
how the resulting link proves itself.

THE SNAPSHOT IS THE SECURITY MODEL. The public page renders a frozen,
sanitised copy taken at an instant the owner chose. It never joins to
`messages`, so there is no path from a public request to private data at all —
not a filtered path, not a careful path, none. What the owner says tomorrow
cannot appear on a link they shared today, because tomorrow's message is not
in the payload.

THE POLICY IS DETERMINISTIC. Whether a conversation may go public is decided
from PROVENANCE — which engine answered, what the message carries — not from
asking a model whether some text looks sensitive. A Salesforce answer is
blocked because it came from Salesforce, and that is knowable exactly. Secret
scanning runs as well, but as a second net under a floor that already holds:
it catches a key someone pasted into their own message, which provenance
cannot see.

WHAT IS DELIBERATELY NOT HERE. No attachment bytes, no signed storage URLs, no
system prompts, no tool payloads, no reasoning, no internal ids. The public
DTO is built by naming what goes IN, never by removing what must stay out — a
denylist is one new meta key away from leaking, and this file would not know.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db
from .config import settings

# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------

#: The addressable half: 16 random bytes, hex. Safe to store, safe to log.
_PUBLIC_ID_BYTES = 16
#: The bearer half: 32 random bytes (256 bits), urlsafe. NEVER stored.
_SECRET_BYTES = 32


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_token() -> Tuple[str, str, str]:
    """(public_id, token, secret_hash).

    The same shape as an auth session (authn/sessions.py) and for the same
    reason: the database holds sha256 of the secret, so a dump — or a backup,
    or this table on a screen — yields nothing that opens a link. Lookup is one
    primary-key hit on `public_id`, and the secret is then compared in
    constant time.
    """
    public_id = secrets.token_hex(_PUBLIC_ID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return public_id, f"{public_id}.{secret}", _hash(secret)


def split_token(token: str) -> Optional[Tuple[str, str]]:
    """(public_id, secret), or None for anything malformed.

    Malformed and wrong must be indistinguishable to the caller, so this
    returns None rather than raising anything a handler might report
    differently.
    """
    if not token or "." not in token:
        return None
    public_id, _, secret = token.partition(".")
    if not public_id or not secret or len(public_id) != _PUBLIC_ID_BYTES * 2:
        return None
    if not re.fullmatch(r"[0-9a-f]+", public_id):
        return None
    return public_id, secret


def secret_matches(secret: str, expected_hash: str) -> bool:
    """Constant time, because a timing signal on a bearer token is a way to
    guess it one byte at a time."""
    return hmac.compare_digest(_hash(secret), expected_hash)


def redact(token: str) -> str:
    """What a log line may contain: the addressable half and nothing else.

    A full share token in a log is a working link in a log.
    """
    parts = split_token(token)
    return f"{parts[0]}.<redacted>" if parts else "<malformed>"


# ---------------------------------------------------------------------------
# Policy — what may leave the workspace
# ---------------------------------------------------------------------------

#: Which engine answered, and whether its answer can go to the open internet.
#:
#: This is the load-bearing table. It is keyed on the ROUTE the engine stamped
#: on the message, which is a fact recorded at generation time by the code that
#: did the work — not an inference about the text afterwards.
PUBLIC_SAFE_ROUTES = frozenset(
    {
        "chat",        # ordinary conversation
        "search",      # public web, with public citations
        "url",         # a public page the user pasted
        "deep_research",  # public web research
        "clarify",     # a question back to the user
        "agent",       # planning over the above
        "vision",      # see below — blocked separately when an image is attached
        "",            # no route recorded: plain chat from before routing
    }
)

#: Routes whose answers ARE private data, whatever they happen to say.
PRIVATE_ROUTES: Dict[str, str] = {
    "sql": "Salesforce records",
    "dataset": "an uploaded dataset",
    "rag": "documents from this workspace",
}

#: Meta keys whose PRESENCE means the answer drew on something private, even
#: when the route looks innocuous — an agent turn that reached Salesforce is a
#: Salesforce turn.
PRIVATE_META_KEYS: Dict[str, str] = {
    "salesforce_sources": "Salesforce records",
    "salesforce_scope": "Salesforce records",
    "salesforce_error": "Salesforce records",
    # `citations` is NOT the web-source list (that is `sources`): it is
    # {record_id, object, url} — pointers to Salesforce records.
    "citations": "Salesforce records",
    "sql": "a database query against workspace data",
    "data": "a result set from workspace data",
    # Whatever a chart is drawn from, it was drawn from workspace rows.
    "chart_data": "a result set from workspace data",
    "chart": "a chart built from workspace data",
    "export_rows": "an exported result set from workspace data",
    "datasets": "an uploaded dataset",
    "document": "an uploaded document",
    "report_files": "generated files held in this workspace",
    "code_sources": "a private repository",
    "attachments": "uploaded files",
}

#: Secrets someone may have pasted into their own message. Provenance cannot
#: see these — the user typed them — so this is the second net.
#:
#: Patterns are deliberately narrow. A false positive blocks a share and the
#: owner cannot see why in detail (we never echo the match), so the cost of a
#: loose pattern is a confused person with no recourse.
_SECRET_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "a database connection string with a password",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s@/]+@"),
    ),
    (
        "an Authorization header",
        re.compile(r"\bAuthorization\s*[:=]\s*['\"]?(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    (
        "a password in a key/value pair",
        re.compile(
            r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*"
            r"['\"][^'\"\s]{8,}['\"]"
        ),
    ),
)

#: Hosts that are ours, not the internet's. A link to one of these on a public
#: page is an information leak and a dead link.
_INTERNAL_URL = re.compile(
    r"https?://(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|"
    r"10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"169\.254\.\d+\.\d+|"
    r"(?:orchestrator|postgres|searxng|prometheus|vllm[a-z-]*|grafana|pgadmin)"
    r"(?:[:/]|\b))",
    re.IGNORECASE,
)


@dataclass
class PolicyResult:
    """Whether this conversation may be shared, and why not."""

    public_allowed: bool
    workspace_allowed: bool
    #: Shown to the owner. Plain sentences, no internals, no matched secret.
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: How many completed messages a snapshot would contain.
    shareable_messages: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "public_allowed": self.public_allowed,
            "workspace_allowed": self.workspace_allowed,
            "blocking_reasons": self.blocking_reasons,
            "warnings": self.warnings,
            "shareable_messages": self.shareable_messages,
        }


def _scan_secrets(text: str) -> List[str]:
    """Names of the secret KINDS found. Never the values."""
    return [label for label, pattern in _SECRET_PATTERNS if pattern.search(text)]


def evaluate(
    messages: Sequence[Dict[str, Any]],
    *,
    policy: Optional[Dict[str, Any]] = None,
) -> PolicyResult:
    """Decide whether these messages may be shared, and how widely.

    Order matters: provenance first (cheap, exact, and the reason most
    conversations are blocked), then content scanning (which can only add
    blocks, never remove one).
    """
    policy = policy or {}
    reasons: List[str] = []
    warnings: List[str] = []

    completed = [m for m in messages if _is_shareable_message(m)]
    if not completed:
        return PolicyResult(
            public_allowed=False,
            workspace_allowed=False,
            blocking_reasons=["This conversation has nothing to share yet."],
            shareable_messages=0,
        )

    private_kinds: List[str] = []
    for m in completed:
        meta = m.get("meta") or {}
        route = str(meta.get("route") or "")
        if route in PRIVATE_ROUTES:
            private_kinds.append(PRIVATE_ROUTES[route])
        for key, what in PRIVATE_META_KEYS.items():
            if meta.get(key):
                private_kinds.append(what)
        # An image someone uploaded is private whatever the answer says.
        if m.get("has_attachment"):
            private_kinds.append("uploaded files")

    secret_kinds: List[str] = []
    internal_links = False
    for m in completed:
        body = str(m.get("content") or "")
        secret_kinds.extend(_scan_secrets(body))
        if _INTERNAL_URL.search(body):
            internal_links = True

    if private_kinds:
        unique = sorted(set(private_kinds))
        reasons.append(
            "This conversation draws on "
            + _join_english(unique)
            + ", which cannot be shared outside the workspace."
        )
    if secret_kinds:
        unique = sorted(set(secret_kinds))
        reasons.append(
            "A message appears to contain " + _join_english(unique) + "."
        )
    if internal_links:
        warnings.append(
            "Some links point to internal addresses and will not open for "
            "anyone outside this network."
        )

    public_enabled = bool(policy.get("public_enabled", settings.public_sharing_enabled))
    if not public_enabled:
        reasons.append("A workspace administrator has turned off public links.")

    # Secrets are the one thing too dangerous for a workspace link either: a
    # workspace share is still a copy that outlives the conversation.
    workspace_allowed = bool(policy.get("workspace_enabled", True)) and not secret_kinds
    if secret_kinds and policy.get("workspace_enabled", True):
        warnings.append("Remove the credential and try again to share internally.")

    return PolicyResult(
        public_allowed=not reasons,
        workspace_allowed=workspace_allowed,
        blocking_reasons=reasons,
        warnings=warnings,
        shareable_messages=len(completed),
    )


def _join_english(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _is_shareable_message(m: Dict[str, Any]) -> bool:
    """A completed, user-visible turn.

    Excludes: any role that is not user/assistant (system and tool turns are
    not the conversation, they are how it was produced), an assistant message
    still streaming, and an empty one.
    """
    if m.get("role") not in ("user", "assistant"):
        return False
    if m.get("status") == "streaming":
        return False
    return bool(str(m.get("content") or "").strip())


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

#: A public page must not become a denial-of-service vector against ourselves.
MAX_SNAPSHOT_MESSAGES = 400
MAX_SNAPSHOT_BYTES = 2_000_000


@dataclass
class Snapshot:
    payload: Dict[str, Any]
    content_hash: str
    last_message_id: Optional[int]
    message_count: int
    #: Set when the conversation was too large to include whole.
    truncated: bool = False


#: Citation fields that may be published: a title, a public URL, its domain.
#: Nothing that identifies our crawler, our cache, or a workspace source.
def _public_sources(meta: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for raw in meta.get("sources") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "")
        if not url.startswith(("http://", "https://")) or _INTERNAL_URL.search(url):
            continue
        out.append(
            {
                "n": int(raw.get("n") or len(out) + 1),
                "title": str(raw.get("title") or "")[:300],
                "url": url[:2000],
                "domain": str(raw.get("domain") or "")[:200],
            }
        )
    return out[:50]


def build(
    *,
    conversation_title: str,
    messages: Sequence[Dict[str, Any]],
    owner_name: Optional[str],
    created_at: datetime,
) -> Snapshot:
    """The public DTO.

    An ALLOWLIST, built by naming what goes in. Every field on a message and
    every key on its meta is dropped unless it appears below, so a meta key
    added next month is private by default and this file does not have to know
    it exists.
    """
    public_messages: List[Dict[str, Any]] = []
    last_id: Optional[int] = None
    truncated = False

    for m in messages:
        if not _is_shareable_message(m):
            continue
        if len(public_messages) >= MAX_SNAPSHOT_MESSAGES:
            truncated = True
            break
        meta = m.get("meta") or {}
        entry: Dict[str, Any] = {
            "role": m["role"],
            "content": str(m.get("content") or ""),
        }
        sources = _public_sources(meta)
        if sources:
            entry["sources"] = sources
        # The engine badge is the ONE piece of meta a reader benefits from,
        # and it is a single word from a closed set.
        route = str(meta.get("route") or "")
        if route in PUBLIC_SAFE_ROUTES and route not in ("", "chat"):
            entry["route"] = route
        public_messages.append(entry)
        if m.get("id") is not None:
            last_id = int(m["id"])

    payload: Dict[str, Any] = {
        "schema": 1,
        "title": (conversation_title or "Shared conversation")[:300],
        "messages": public_messages,
        "shared_at": created_at.isoformat(),
        "truncated": truncated,
    }
    if owner_name:
        payload["owner_name"] = owner_name[:120]

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        # Drop from the FRONT: the end of a conversation is the part a link is
        # usually shared for.
        while (
            payload["messages"]
            and len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            > MAX_SNAPSHOT_BYTES
        ):
            payload["messages"].pop(0)
            truncated = True
        payload["truncated"] = truncated
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return Snapshot(
        payload=payload,
        content_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        last_message_id=last_id,
        message_count=len(payload["messages"]),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

#: What the modal offers. `None` is "no expiry", allowed only when policy says.
EXPIRY_CHOICES: Dict[str, Optional[int]] = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "never": None,
}


def resolve_expiry(
    choice: str, *, policy: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[datetime], Optional[str]]:
    """(expires_at, error). Server-side, because an expiry the client picks is
    an expiry the client can decline to pick."""
    policy = policy or {}
    if choice not in EXPIRY_CHOICES:
        return None, "That expiry is not one of the options."
    days = EXPIRY_CHOICES[choice]
    if days is None:
        if not policy.get("allow_never", settings.public_share_allow_never):
            return None, "This workspace requires shared links to expire."
        return None, None
    max_days = int(policy.get("max_days", settings.public_share_max_days))
    if days > max_days:
        return None, f"This workspace caps shared links at {max_days} days."
    return datetime.now(timezone.utc) + timedelta(days=days), None
