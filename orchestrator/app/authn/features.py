"""Feature access — WHICH TOOLS a person may use, as opposed to what they may
administer (that is `rbac.py`).

Two different questions, deliberately two different modules:

    rbac.Cap          may this account manage members / read the audit log?
    features.Feature  may this account run a web search / open Salesforce?

A capability is a property of the ROLE. A feature is a property of the
PERSON: an admin may want the whole workspace on the synced Salesforce copy
but only the two analysts allowed to query the live org, and that has
nothing to do with who can invite members.

RESOLUTION, in order, each layer overriding the one before:

    1. the built-in default in `FEATURES` (what a fresh workspace does)
    2. the workspace default an admin set     (workspaces.feature_defaults)
    3. the per-member override                (workspace_memberships.features)
    4. the invariants below                   (a dependency cannot dangle)
    5. super admins: everything on            (nobody can lock themselves out)

Only keys an admin actually touched are stored, at either layer, so a later
change to a built-in default reaches every account that never overrode it.

THE CLIENT IS NEVER THE GATE. The composer hides what a person may not use
(`/auth/me` carries the resolved map), but hiding is a courtesy: `/chat` and
the upload routes re-resolve from the database and refuse regardless of what
the client sent. See `enforce_chat_request`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence


class Feature(str, Enum):
    WEB_SEARCH = "web_search"
    DEEP_RESEARCH = "deep_research"
    SALESFORCE = "salesforce"
    SALESFORCE_LIVE = "salesforce_live"
    ATTACHMENTS = "attachments"
    VOICE_INPUT = "voice_input"


@dataclass(frozen=True)
class FeatureSpec:
    id: Feature
    label: str
    #: One line, shown under the label in the admin toggle list. Says what the
    #: person LOSES when it is off, because that is the decision being made.
    hint: str
    default: bool
    #: Turning this off turns those off too (and turning one of those on
    #: implies this). A live-Salesforce user without Salesforce mode has a
    #: toggle that cannot do anything.
    requires: Optional[Feature] = None


#: The registry. The admin UI renders this list verbatim, in this order, so a
#: new tool becomes manageable by appending one entry here.
FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        id=Feature.ATTACHMENTS,
        label="Photos, files and datasets",
        hint="Upload images, PDFs, documents and spreadsheets to ask about them.",
        default=True,
    ),
    FeatureSpec(
        id=Feature.VOICE_INPUT,
        label="Voice input",
        hint=(
            "Dictate into the message box. Audio is transcribed on this "
            "platform's own hardware and never stored."
        ),
        default=True,
    ),
    FeatureSpec(
        id=Feature.WEB_SEARCH,
        label="Web search",
        hint="Answer from the public web, and send search queries to the internet.",
        default=True,
    ),
    FeatureSpec(
        id=Feature.DEEP_RESEARCH,
        label="Deep research",
        hint="Run the multi-round research loop and write a cited report. Needs web search.",
        default=True,
        requires=Feature.WEB_SEARCH,
    ),
    FeatureSpec(
        id=Feature.SALESFORCE,
        label="Salesforce",
        hint="Answer from this workspace's synced Salesforce data.",
        default=True,
    ),
    FeatureSpec(
        id=Feature.SALESFORCE_LIVE,
        label="Live Salesforce",
        hint="Query the Salesforce org directly over the read-only API. Needs Salesforce.",
        default=True,
        requires=Feature.SALESFORCE,
    ),
)

BY_ID: Dict[str, FeatureSpec] = {spec.id.value: spec for spec in FEATURES}

#: Every feature id, in registry order — the shape of a resolved map.
IDS: tuple[str, ...] = tuple(spec.id.value for spec in FEATURES)


def defaults() -> Dict[str, bool]:
    """Layer 1: what a workspace does before anyone configures anything."""
    return {spec.id.value: spec.default for spec in FEATURES}


def clean(raw: Any) -> Dict[str, bool]:
    """A stored or submitted override, reduced to known keys and real bools.

    Unknown keys are dropped rather than rejected: a feature removed from the
    registry must not make every existing row unreadable, and a client that
    invents a key must not have it persisted.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, bool] = {}
    for key, value in raw.items():
        if key in BY_ID and isinstance(value, bool):
            out[key] = value
    return out


def _apply_invariants(resolved: Dict[str, bool]) -> Dict[str, bool]:
    """A dependency cannot dangle in either direction.

    Down: Salesforce off ⇒ Live Salesforce off, web search off ⇒ deep
    research off. Repeated until stable, so a chain (a → b → c) settles.
    """
    for _ in range(len(FEATURES)):
        changed = False
        for spec in FEATURES:
            if spec.requires is None:
                continue
            parent = spec.requires.value
            if resolved.get(spec.id.value) and not resolved.get(parent, False):
                resolved[spec.id.value] = False
                changed = True
        if not changed:
            break
    return resolved


def resolve(
    *,
    role: str = "member",
    workspace_defaults: Any = None,
    member_overrides: Any = None,
) -> Dict[str, bool]:
    """The effective map for one person. Always contains every feature id."""
    resolved = defaults()
    resolved.update(clean(workspace_defaults))
    resolved.update(clean(member_overrides))
    if str(role) == "super_admin":
        # The account that administers feature access must never be able to
        # take a tool away from itself — that is a support ticket nobody can
        # answer, because the admin UI itself would still be reachable but
        # the workspace's own owner could not test what they just changed.
        return {key: True for key in resolved}
    return _apply_invariants(resolved)


def allowed(features: Mapping[str, bool], feature: Feature) -> bool:
    """Read one flag from a resolved map, defaulting to the built-in."""
    value = features.get(feature.value)
    if isinstance(value, bool):
        return value
    return BY_ID[feature.value].default


@dataclass(frozen=True)
class ChatGate:
    """What one /chat request is allowed to do, after the tools it asked for
    are checked against the caller's access."""

    mode: str
    web_search: str
    deep_research: bool
    sf_live: bool
    #: Human-readable names of the tools that were taken away, for the one
    #: status line the user sees. Empty when nothing was blocked — the
    #: overwhelmingly common case, which must cost nothing.
    blocked: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.blocked)


def enforce_chat(
    resolved: Mapping[str, bool],
    *,
    mode: str,
    web_search: str,
    deep_research: bool,
    sf_live: bool,
) -> ChatGate:
    """The server-side gate for one chat turn.

    The client sends what its composer offered; this decides what actually
    runs. A blocked tool is DOWNGRADED, never an error: a member whose
    Salesforce access was removed mid-session should get an ordinary
    assistant answer with a line saying why, not a 403 in the middle of a
    conversation they were already having.
    """
    blocked: list[str] = []

    if mode != "assistant" and not allowed(resolved, Feature.SALESFORCE):
        blocked.append(BY_ID[Feature.SALESFORCE.value].label)
        mode = "assistant"
        sf_live = False
    elif sf_live and not allowed(resolved, Feature.SALESFORCE_LIVE):
        blocked.append(BY_ID[Feature.SALESFORCE_LIVE.value].label)
        sf_live = False

    if deep_research and not allowed(resolved, Feature.DEEP_RESEARCH):
        blocked.append(BY_ID[Feature.DEEP_RESEARCH.value].label)
        deep_research = False

    if not allowed(resolved, Feature.WEB_SEARCH) and web_search != "off":
        # "auto" counts: without this the classifier would still send the
        # question to a search provider for someone who may not use the web.
        blocked.append(BY_ID[Feature.WEB_SEARCH.value].label)
        web_search = "off"

    return ChatGate(
        mode=mode,
        web_search=web_search,
        deep_research=deep_research,
        sf_live=sf_live,
        blocked=tuple(blocked),
    )


def blocked_notice(blocked: Sequence[str]) -> str:
    """The one status line a downgraded turn shows."""
    if not blocked:
        return ""
    names = list(blocked)
    if len(names) == 1:
        subject = names[0]
    elif len(names) == 2:
        subject = f"{names[0]} and {names[1]}"
    else:
        subject = ", ".join(names[:-1]) + f" and {names[-1]}"
    verb = "is" if len(names) == 1 else "are"
    return f"{subject} {verb} turned off for your account — answering without it."


def catalog() -> list[dict]:
    """The registry as JSON for the admin UI."""
    return [
        {
            "id": spec.id.value,
            "label": spec.label,
            "hint": spec.hint,
            "default": spec.default,
            "requires": spec.requires.value if spec.requires else None,
        }
        for spec in FEATURES
    ]
