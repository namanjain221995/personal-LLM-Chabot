"""The scope vocabulary for the public developer API — closed, flat, and data.

CONTRACT-3 §7. FOUR scopes exist and no fifth can be spelled: an unknown
string is a parse ERROR, never a silently-dropped entry. That rule is the
whole point of this module. The alternative — tolerating an unrecognised
scope — means a typo in a console form (`responses.wrte`) produces a key that
looks configured and grants nothing, and a scope deleted in a later release
keeps living in `api_keys.scopes` as a value nothing checks.

WHY FOUR AND NOT SIX (2026-09-13, wave-1 review). This module shipped with
`webhooks.read` and `webhooks.manage` and a test named "the six the contract
names". The contract names four. CONTRACT-3 §7's endpoint table lists
`models.read`, `responses.read`, `responses.write` and `usage.read`, and the
paragraph directly under it puts webhooks among what `/v1` does **not**
expose, deliberately; the only webhook string anywhere in CONTRACT.md is
`api.webhooks.manage`, which §6 defines as a BROWSER capability on the
console, in the other vocabulary entirely. Shipping two scopes for endpoints
that do not exist would have meant a console offering a tick-box that grants
nothing, and — the part that matters — a vocabulary drifting away from the
document that is supposed to move first. If `/v1` ever grows webhook
endpoints, amend CONTRACT-3 §7 and add the scopes back here, in that order.

SCOPES ARE DATA, NOT ROLES. There is no implication table here, and adding one
would be a change of contract. `responses.write` does not grant
`responses.read`; `usage.read` does not grant `models.read`. A key that should
do both carries both. Roles (`authn/rbac.py`) answer "who is this person and
what may they administer"; scopes answer "what may this ONE machine
credential call", and the two vocabularies are deliberately disjoint —
CONTRACT-3 §1 keeps the browser and the API on different credentials, so a
capability must never be readable as a scope or the reverse. Webhook
MANAGEMENT is the clearest case: it is `Cap.API_WEBHOOKS_MANAGE` on the
console session, never a scope on a machine credential.

The wire form follows RFC 6749: space-delimited, case-sensitive tokens. The
storage form is a JSON array (`api_keys.scopes`, V34), sorted so two equal
scope sets compare equal as stored text and a diff in the console is
readable. Comma-delimited input is refused rather than guessed at — RFC 6749
does not permit it, and accepting both spellings means every consumer has to.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Iterable, List, Union


class Scope(str, Enum):
    """The closed vocabulary. Values are what V34 stores and what the wire
    carries; the member names are for call sites."""

    #: List the models this key may use, and read one of them (CONTRACT §7).
    MODELS_READ = "models.read"
    #: Read a response this key's project created. Deliberately NOT implied by
    #: MODELS_READ nor by RESPONSES_WRITE: a fire-and-forget integration that
    #: only posts work has no reason to be able to read back what other
    #: credentials in the same project generated.
    RESPONSES_READ = "responses.read"
    #: Create a response, and cancel one. Cancellation shares this scope
    #: because it is a write on the same object, and a caller able to start
    #: generation must be able to stop it — an integration that could only
    #: start work would burn the project's quota with no way to intervene.
    RESPONSES_WRITE = "responses.write"
    #: Read the project's own usage counters (CONTRACT §7 `/v1/usage`).
    USAGE_READ = "usage.read"


#: Every scope. Computed from the enum so a new member can never be forgotten
#: here, the same reason `ROLE_CAPS[SUPER_ADMIN]` is `frozenset(Cap)`.
ALL_SCOPES: FrozenSet[Scope] = frozenset(Scope)

#: One sentence per scope, for the console's key-creation form and for the
#: published OpenAPI document. Kept beside the vocabulary so a new scope
#: cannot ship undescribed — `test_every_scope_is_described` fails if it does.
SCOPE_DESCRIPTIONS: dict[Scope, str] = {
    Scope.MODELS_READ: "List the models this key may use.",
    Scope.RESPONSES_READ: "Read responses created by this project.",
    Scope.RESPONSES_WRITE: "Create and cancel responses.",
    Scope.USAGE_READ: "Read this project's usage counters.",
}

#: What the console offers when the person creating a key does not choose.
#:
#: The three scopes an integration that calls the API needs, and nothing else:
#: reading the project's usage counters is a separate job, usually for a
#: separate credential — a billing dashboard has no business being able to
#: spend the quota it is reading. STANDARDS.md (Stripe): ship the restricted
#: key as the default path and make the broad one the exception, so the blast
#: radius of a leak is small by default rather than by remembering to tick
#: boxes.
DEFAULT_SCOPES: FrozenSet[Scope] = frozenset(
    {Scope.MODELS_READ, Scope.RESPONSES_READ, Scope.RESPONSES_WRITE}
)

ScopeInput = Union[Scope, str]


class UnknownScopeError(ValueError):
    """A scope string outside the closed vocabulary.

    Carries the offending value so the console can say which entry was wrong,
    and the full vocabulary so the message is actionable without a second
    lookup. This is a 400 `invalid_request_error` on the management surface —
    never a 403, because the caller's credential is not what is at fault.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        known = ", ".join(sorted(s.value for s in ALL_SCOPES))
        super().__init__(f"unknown scope {value!r}; known scopes are: {known}")


class InsufficientScopeError(Exception):
    """The key is valid, the tenant owns the object, the scope is missing.

    CONTRACT-3 §9 maps this to `403 insufficient_scope`. Naming the required
    scope is deliberate and is not a disclosure: per RFC 6750 it is the
    machine-actionable way for a client to learn which scope to ask for, and
    the caller already holds a credential for the project. What is NOT named
    is what the caller *does* hold — `granted` is carried for the server's own
    log line, and `challenge()` never renders it.
    """

    #: The error envelope fields (CONTRACT-3 §9), so the `/v1` exception
    #: handler can serialise this without a second mapping table. The values
    #: match `publicapi/errors.py::_CODES["insufficient_scope"]`, which is the
    #: authority for the envelope — two spellings of the same 403 would be a
    #: wire contract that depends on which layer raised it.
    code = "insufficient_scope"
    status_code = 403
    type = "permission_error"

    def __init__(
        self, required: Iterable[Scope], granted: Iterable[Scope] = ()
    ) -> None:
        self.required: FrozenSet[Scope] = frozenset(required)
        self.granted: FrozenSet[Scope] = frozenset(granted)
        missing = " ".join(sorted(s.value for s in self.required - self.granted))
        super().__init__(f"This API key lacks the required scope: {missing}.")

    def challenge(self) -> str:
        """The `WWW-Authenticate` value for the 403 (RFC 6750 §3)."""
        wanted = " ".join(sorted(s.value for s in self.required))
        return f'Bearer error="insufficient_scope", scope="{wanted}"'


def parse_scope(value: ScopeInput) -> Scope:
    """One scope, or `UnknownScopeError`. Case-sensitive, per RFC 6749."""
    if isinstance(value, Scope):
        return value
    if not isinstance(value, str):
        raise UnknownScopeError(repr(value))
    try:
        return Scope(value)
    except ValueError:
        raise UnknownScopeError(value) from None


def parse_scopes(values: Union[ScopeInput, Iterable[ScopeInput], None]) -> FrozenSet[Scope]:
    """A stored array or a wire string → the set, with every entry validated.

    Accepts the two shapes the platform actually has: the JSON array read back
    from `api_keys.scopes`, and the space-delimited string of RFC 6749.
    `None` and an empty input mean "no scopes" — a real state for a key that
    has been narrowed to nothing, which then fails every `requires()` check.

    A comma is refused rather than treated as a separator. RFC 6749's grammar
    has no comma in it, and a parser that accepts both spellings forces every
    downstream consumer (OpenAPI tooling, introspection clients, our own
    console) to accept both too.
    """
    if values is None:
        return frozenset()
    if isinstance(values, Scope):
        return frozenset({values})
    if isinstance(values, str):
        if "," in values:
            raise UnknownScopeError(values)
        values = values.split()
    return frozenset(parse_scope(v) for v in values)


def scope_names(scopes: Iterable[Scope]) -> List[str]:
    """The storage form: a sorted list of strings for the `scopes` jsonb column.

    Sorted AND deduplicated. Sorted so an unchanged scope set never shows up
    as a diff; deduplicated because this function is given whatever a console
    form posted, and a list is not a set (2026-09-13, wave-1 review):
    `scope_names(["models.read", "models.read"])` used to write
    `["models.read", "models.read"]` into `api_keys.scopes`. Harmless on read,
    since `parse_scopes` builds a frozenset — but the COLUMN is what the
    console renders and what the audit log quotes, and a duplicate there reads
    as a bug in the key rather than a bug in the writer.
    """
    return sorted({parse_scope(s).value for s in scopes})


def format_scopes(scopes: Iterable[Scope]) -> str:
    """The wire form: space-delimited, sorted (RFC 6749 §3.3)."""
    return " ".join(scope_names(scopes))


@dataclass(frozen=True)
class ScopeRequirement:
    """What one endpoint demands. Framework-free on purpose.

    The `/v1` routers are not built yet and this module must not import them
    (nor FastAPI, nor `db`): a requirement is therefore a value that can be
    declared next to a route, asserted in a test, rendered into the OpenAPI
    document, and checked by whatever dependency the router wave writes.

    `required` is a set because an endpoint may one day need two scopes at
    once; ALL of them must be held. There is no "any of" form, because a
    caller cannot tell from a 403 which of several alternatives to request.
    """

    required: FrozenSet[Scope]

    def missing(self, granted: Iterable[Scope]) -> FrozenSet[Scope]:
        return frozenset(self.required) - frozenset(granted)

    def satisfied_by(self, granted: Iterable[Scope]) -> bool:
        return not self.missing(granted)

    def check(self, granted: Iterable[Scope]) -> None:
        """Raise `InsufficientScopeError` unless every required scope is held.

        Deny by default (OWASP API5:2023): the failure is raised here, in the
        shared primitive, rather than left to each handler to remember — a
        route that forgets to call this has no scope declaration at all, which
        the router wave's startup check is what catches.
        """
        granted = frozenset(granted)
        if self.missing(granted):
            raise InsufficientScopeError(self.required, granted)

    def __str__(self) -> str:
        return format_scopes(self.required)


def requires(*scopes: ScopeInput) -> ScopeRequirement:
    """`requires(Scope.RESPONSES_WRITE)` — the declaration a route carries.

    At least one scope must be named. An empty requirement would read as
    "authenticated is enough", which for this platform is never true: every
    `/v1` route in CONTRACT-3 §7 except the public OpenAPI document names a
    scope, and that document is served without going through this at all.
    """
    if not scopes:
        raise ValueError("a route must require at least one scope")
    return ScopeRequirement(frozenset(parse_scope(s) for s in scopes))
