"""Who may invite an address that ALREADY has an account.

An invitation is the only account-creation path, and `store.accept_invitation`
does not always create an account: when the invited address already has a
users row it CLAIMS that row — new password, status back to 'active',
membership upserted — and `/auth/invitations/accept` then mints a session for
it. That is the intended way to re-onboard a returning colleague.

It was also an account takeover. The only guard on the issuing side fired for
an ACTIVE member, while `remove_member` deletes the membership AND disables the
account, so a removed or deactivated person satisfied neither arm: an admin
could invite that address, accept the one-time link themselves, and be signed
in AS that person — a deactivated SUPER ADMIN included, whose membership the
ON CONFLICT upsert then quietly rewrote to 'member'. The trail read as a
routine invite plus an acceptance (AUDIT.md F028, 2026-09-13).

THE RULE THIS MODULE HOLDS: an address that belongs to an existing account may
only be invited by someone who OUTRANKS that account — the same rank rule that
governs deactivating, removing, revoking and password-resetting a member.
Rank comes from the membership row. A removed account has no membership row
and its former rank is therefore unknowable, so it is treated as the highest
it could have been: only a SUPER_ADMIN may re-invite it.

It is a pure function of rows on purpose. The ISSUING path
(`admin_api.create_invitation`) applies it before an invitation exists, and the
ACCEPTING path (`authn/api.accept_invitation` → `store.accept_invitation`) must
apply the same rule to the invitations that were issued before this fix landed,
without the two definitions drifting apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .rbac import Role, outranks


@dataclass(frozen=True)
class Refusal:
    """Why an address may not be invited, in the shape the caller needs: the
    HTTP status to answer, the sentence the admin UI shows, a short reason for
    the audit event, and the target's role when it is known ("" when it is
    not). `audited` is False for the everyday "they are already here" mistake
    and True for a refusal that stopped someone reaching an account above
    their rank — that one is what the audit log exists to record."""

    status: int
    detail: str
    reason: str
    target_role: str
    audited: bool


def claim_refusal(
    actor_role: Role | str,
    *,
    target_user: Optional[Dict[str, Any]],
    target_membership: Optional[Dict[str, Any]],
) -> Optional[Refusal]:
    """None when `actor_role` may invite this address; a `Refusal` otherwise.

    `target_user` is the users row for the invited address (None when the
    address is new — the ordinary case, always allowed) and
    `target_membership` is `store.membership(user_id)` for it, which is None
    once the person has been removed from the workspace.
    """
    if target_user is None:
        return None

    if target_membership is not None:
        target_role = str(target_membership["role"])
        if str(target_user.get("status") or "") == "active":
            # Unchanged behaviour, and the sentence the admin UI already
            # shows: an active member does not need an invitation.
            return Refusal(
                status=409,
                detail="That person is already a member.",
                reason="already_a_member",
                target_role=target_role,
                audited=False,
            )
        if outranks(actor_role, target_role):
            # The legitimate case this whole path exists for: re-inviting a
            # deactivated person you already administer.
            return None
        return Refusal(
            status=403,
            detail="That address belongs to an account you cannot administer.",
            reason="outranked_account",
            target_role=target_role,
            audited=True,
        )

    # No membership row: the account was removed from the workspace (or never
    # joined it). Nothing records what rank it held — `remove_membership` is a
    # DELETE — so the safe reading is the highest it could have been. A super
    # admin re-inviting a departed colleague is still one call; everyone else
    # is refused, which is what closes F028 for a removed super admin.
    try:
        actor = Role(actor_role)
    except ValueError:  # an unknown role string: fail closed, never open
        actor = None
    if actor is Role.SUPER_ADMIN:
        return None
    return Refusal(
        status=403,
        detail="That address belongs to a former member; only a super admin can invite it again.",
        reason="former_member_unknown_rank",
        target_role="",
        audited=True,
    )
