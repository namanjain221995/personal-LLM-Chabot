"""Roles and capabilities — the ONE place authorization semantics live.

Routes never compare role strings. They ask for a capability
(`Depends(require_capability(Cap.MEMBERS_MANAGE))`) or ask the principal
(`principal.can(Cap.AUDIT_READ)`), and this table answers. Changing what an
admin may do is an edit HERE, not a hunt through the codebase for
`if role == "admin"`.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class Role(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    MEMBER = "member"


class Cap(str, Enum):
    """Workspace capabilities. Names mirror the admin surface they gate."""

    WORKSPACE_READ = "workspace.read"
    WORKSPACE_MANAGE = "workspace.manage"
    MEMBERS_READ = "members.read"
    MEMBERS_MANAGE = "members.manage"
    ROLES_MANAGE = "roles.manage"
    INVITES_MANAGE = "invites.manage"
    AUDIT_READ = "audit.read"
    #: Read another member's conversations/uploads/reports through the
    #: AUDITED admin viewer. Deliberately not implied by MEMBERS_READ —
    #: seeing the member list is not seeing their content.
    WORKSPACE_CONTENT_READ = "workspace_content.read"
    SESSIONS_MANAGE = "sessions.manage"
    SETTINGS_MANAGE = "settings.manage"
    #: The analytics console: platform-wide usage, per-person leaderboards and
    #: the inference/GPU telemetry behind them. SUPER_ADMIN only — an admin
    #: runs the workspace's people, not its infrastructure, and per-person
    #: consumption is closer to the audit log than to the member list. It is
    #: absent from _ADMIN_CAPS deliberately; do not add it there.
    ANALYTICS_READ = "analytics.read"


#: What each role can do. SUPER_ADMIN is computed as "everything" so a new
#: capability can never be forgotten from it.
_ADMIN_CAPS: FrozenSet[Cap] = frozenset(
    {
        Cap.WORKSPACE_READ,
        Cap.MEMBERS_READ,
        Cap.MEMBERS_MANAGE,
        Cap.INVITES_MANAGE,
        Cap.WORKSPACE_CONTENT_READ,
        Cap.SESSIONS_MANAGE,
    }
)

ROLE_CAPS: dict[Role, FrozenSet[Cap]] = {
    Role.SUPER_ADMIN: frozenset(Cap),
    # ADMIN runs the workspace day to day but cannot: change roles
    # (ROLES_MANAGE), read the audit log (AUDIT_READ), or change workspace
    # settings (WORKSPACE_MANAGE / SETTINGS_MANAGE). Content inspection IS
    # granted (WORKSPACE_CONTENT_READ) and every use of it is audited.
    Role.ADMIN: _ADMIN_CAPS,
    Role.MEMBER: frozenset(),
}


def capabilities(role: Role | str) -> FrozenSet[Cap]:
    try:
        return ROLE_CAPS[Role(role)]
    except ValueError:
        return frozenset()


def can(role: Role | str, cap: Cap) -> bool:
    return cap in capabilities(role)


#: Rank for "may actor administer target?" checks. An admin must never manage
#: an equal-or-higher role: not change their role, not deactivate them, not
#: revoke their sessions, not remove them. Super admins outrank everyone.
_RANK = {Role.MEMBER: 0, Role.ADMIN: 1, Role.SUPER_ADMIN: 2}


def outranks(actor: Role | str, target: Role | str) -> bool:
    """True when `actor` may perform member-management actions on `target`."""
    try:
        return _RANK[Role(actor)] > _RANK[Role(target)]
    except ValueError:
        return False


def assignable_roles(actor: Role | str) -> FrozenSet[Role]:
    """Roles the actor may hand out (inviting or changing roles).

    Only a SUPER_ADMIN can mint another SUPER_ADMIN or an ADMIN; an ADMIN can
    invite MEMBERs. Self-escalation is impossible by construction: you can
    never assign a role your own rank does not exceed — except that
    SUPER_ADMIN may assign SUPER_ADMIN, which is the one deliberate exception
    (someone has to be able to appoint a successor).
    """
    try:
        actor_role = Role(actor)
    except ValueError:
        return frozenset()
    if actor_role is Role.SUPER_ADMIN:
        return frozenset({Role.SUPER_ADMIN, Role.ADMIN, Role.MEMBER})
    if actor_role is Role.ADMIN:
        return frozenset({Role.MEMBER})
    return frozenset()
