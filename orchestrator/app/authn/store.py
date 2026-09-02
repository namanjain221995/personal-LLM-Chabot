"""SQL for the identity tables (V12). Synchronous, pooled, one concern.

Everything here is a plain function over `db.connection()` — async routes call
through `db.run_in_thread`, exactly like the rest of the app. Kept out of
db.py on purpose: that module owns the schema and the chat data; this one owns
identity. Row shapes are plain dicts (dict_row), same as everywhere else.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from .. import db
from .rbac import Role


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            "SELECT * FROM users WHERE lower(email) = lower(%s)", (email.strip(),)
        ).fetchone()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


def set_credentials(
    user_id: int,
    *,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    password_hash: Optional[str] = None,
) -> None:
    """Update identity fields; only the ones given. Password changes stamp
    password_changed_at — session revocation on change keys off it."""
    sets, args = [], []
    if email is not None:
        sets.append("email = %s")
        args.append(email.strip())
    if display_name is not None:
        sets.append("display_name = %s")
        args.append(display_name.strip())
    if password_hash is not None:
        sets.append("password_hash = %s")
        sets.append("password_changed_at = now()")
        args.append(password_hash)
    if not sets:
        return
    args.append(user_id)
    with db.connection() as con:
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", args)


def set_status(user_id: int, status: str) -> None:
    with db.connection() as con:
        con.execute("UPDATE users SET status = %s WHERE id = %s", (status, user_id))


def touch_last_active(user_id: int) -> None:
    """Coarse activity stamp for the Members page. Writes at most once a
    minute per user — this rides the request path and must stay cheap."""
    with db.connection() as con:
        con.execute(
            """UPDATE users SET last_active_at = now()
               WHERE id = %s
                 AND (last_active_at IS NULL OR last_active_at < now() - interval '60 seconds')""",
            (user_id,),
        )


# ---------------------------------------------------------------------------
# Workspace + memberships
# ---------------------------------------------------------------------------

def default_workspace() -> Optional[Dict[str, Any]]:
    """The workspace. The data model supports many; the deployment runs one —
    'oldest first' makes the choice deterministic if a second ever appears."""
    with db.connection() as con:
        return con.execute(
            "SELECT * FROM workspaces ORDER BY created_at, id LIMIT 1"
        ).fetchone()


def ensure_workspace(name: str) -> Dict[str, Any]:
    existing = default_workspace()
    if existing is not None:
        return existing
    row = {"id": _new_id(), "name": name}
    with db.connection() as con:
        con.execute(
            "INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (row["id"], name),
        )
    return default_workspace() or row


def membership(user_id: int) -> Optional[Dict[str, Any]]:
    """The user's membership row joined with its workspace (single-workspace
    deployment: first by workspace age)."""
    with db.connection() as con:
        return con.execute(
            """SELECT m.workspace_id, m.user_id, m.role, m.created_at AS member_since,
                      w.name AS workspace_name
               FROM workspace_memberships m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.user_id = %s
               ORDER BY w.created_at, w.id LIMIT 1""",
            (user_id,),
        ).fetchone()


def upsert_membership(workspace_id: str, user_id: int, role: str) -> None:
    Role(role)  # raises on garbage before it reaches SQL
    with db.connection() as con:
        con.execute(
            """INSERT INTO workspace_memberships (workspace_id, user_id, role)
               VALUES (%s, %s, %s)
               ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
            (workspace_id, user_id, role),
        )


def remove_membership(workspace_id: str, user_id: int) -> None:
    with db.connection() as con:
        con.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id = %s AND user_id = %s",
            (workspace_id, user_id),
        )


def count_active_super_admins(workspace_id: str, excluding: Optional[int] = None) -> int:
    """Active (not disabled) super admins — the number that must never hit 0."""
    with db.connection() as con:
        row = con.execute(
            """SELECT count(*) AS n
               FROM workspace_memberships m JOIN users u ON u.id = m.user_id
               WHERE m.workspace_id = %s AND m.role = 'super_admin'
                 AND u.status = 'active' AND (%s::integer IS NULL OR u.id <> %s)""",
            (workspace_id, excluding, excluding),
        ).fetchone()
    return int(row["n"]) if row else 0


def list_members(
    workspace_id: str,
    *,
    query: str = "",
    role: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """(page, total) for the Members table. Filters compose; all optional."""
    where = ["m.workspace_id = %s"]
    args: List[Any] = [workspace_id]
    if query.strip():
        where.append(
            "(u.display_name ILIKE %s OR u.email ILIKE %s OR u.username ILIKE %s)"
        )
        needle = f"%{query.strip()}%"
        args += [needle, needle, needle]
    if role:
        where.append("m.role = %s")
        args.append(role)
    if status:
        where.append("u.status = %s")
        args.append(status)
    cond = " AND ".join(where)
    with db.connection() as con:
        total = con.execute(
            f"""SELECT count(*) AS n FROM workspace_memberships m
                JOIN users u ON u.id = m.user_id WHERE {cond}""",
            args,
        ).fetchone()
        rows = con.execute(
            f"""SELECT u.id, u.username, u.email, u.display_name, u.status,
                       u.created_at, u.last_active_at, m.role,
                       m.created_at AS member_since
                FROM workspace_memberships m JOIN users u ON u.id = m.user_id
                WHERE {cond}
                ORDER BY (m.role = 'super_admin') DESC, (m.role = 'admin') DESC,
                         lower(coalesce(u.display_name, u.username)), u.id
                LIMIT %s OFFSET %s""",
            args + [limit, offset],
        ).fetchall()
    return rows, int(total["n"]) if total else 0


def member_counts(workspace_id: str) -> Dict[str, int]:
    with db.connection() as con:
        members = con.execute(
            """SELECT count(*) AS n FROM workspace_memberships m
               JOIN users u ON u.id = m.user_id
               WHERE m.workspace_id = %s AND u.status = 'active'""",
            (workspace_id,),
        ).fetchone()
        pending = con.execute(
            """SELECT count(*) AS n FROM workspace_invitations
               WHERE workspace_id = %s AND accepted_at IS NULL
                 AND revoked_at IS NULL AND expires_at > now()""",
            (workspace_id,),
        ).fetchone()
    return {
        "members": int(members["n"]) if members else 0,
        "pending_invites": int(pending["n"]) if pending else 0,
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(
    *,
    session_id: str,
    token_hash: str,
    user_id: int,
    remember: bool,
    lifetime: timedelta,
    absolute_lifetime: timedelta,
    user_agent: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    now = _now()
    with db.connection() as con:
        return con.execute(
            """INSERT INTO auth_sessions
               (id, token_hash, user_id, remember, created_at, last_seen_at,
                expires_at, absolute_expires_at, user_agent, ip)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                session_id,
                token_hash,
                user_id,
                remember,
                now,
                now,
                now + lifetime,
                now + absolute_lifetime,
                user_agent[:400],
                ip[:100],
            ),
        ).fetchone()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            "SELECT * FROM auth_sessions WHERE id = %s", (session_id,)
        ).fetchone()


def roll_session(session_id: str, lifetime: timedelta) -> None:
    """Rolling renewal: push expires_at forward (never past the absolute
    ceiling), stamp last_seen. Called at most once per five minutes per
    session — the caller throttles, this just writes."""
    now = _now()
    with db.connection() as con:
        con.execute(
            """UPDATE auth_sessions
               SET last_seen_at = %s,
                   expires_at = LEAST(%s, absolute_expires_at)
               WHERE id = %s AND revoked_at IS NULL""",
            (now, now + lifetime, session_id),
        )


#: Why a session ended (auth_sessions.revoke_reason). Read back by /auth/me
#: for the browser that still holds the cookie, so the vocabulary is the
#: contract with the frontend's session-ended page.
REVOKE_LOGOUT = "logout"                      # the user signed out
REVOKE_USER_OTHERS = "user_revoked"           # "sign out other sessions"
REVOKE_PASSWORD_CHANGED = "password_changed"  # own password change
REVOKE_ADMIN = "admin_revoked"                # an admin signed the user out
REVOKE_PASSWORD_RESET = "password_reset"      # an admin reset the password
REVOKE_ACCOUNT_DISABLED = "account_disabled"  # an admin deactivated the account
REVOKE_ACCOUNT_REMOVED = "account_removed"    # an admin removed the member


def revoke_session(session_id: str, reason: str = REVOKE_LOGOUT) -> bool:
    with db.connection() as con:
        cur = con.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoke_reason = %s "
            "WHERE id = %s AND revoked_at IS NULL",
            (reason[:40], session_id),
        )
        return cur.rowcount > 0


def revoke_user_sessions(
    user_id: int, *, keep: Optional[str] = None, reason: str = REVOKE_ADMIN
) -> int:
    """Revoke every live session for a user, optionally sparing one (the one
    doing the revoking — 'log out other sessions'). `reason` is what the
    signed-out browser will be told."""
    with db.connection() as con:
        cur = con.execute(
            """UPDATE auth_sessions SET revoked_at = now(), revoke_reason = %s
               WHERE user_id = %s AND revoked_at IS NULL
                 AND (%s::text IS NULL OR id <> %s)""",
            (reason[:40], user_id, keep, keep),
        )
        return cur.rowcount


def workspace_admin_contacts(workspace_id: Optional[str], limit: int = 5) -> List[Dict[str, Any]]:
    """Active admins of a workspace — who a removed member can ask for
    access back. Super admins first. Emails only, no ids: this is shown to
    someone who is no longer a member."""
    with db.connection() as con:
        rows = con.execute(
            """SELECT u.email, u.display_name, m.role
                 FROM workspace_memberships m
                 JOIN users u ON u.id = m.user_id
                WHERE (%s::text IS NULL OR m.workspace_id = %s)
                  AND m.role IN ('super_admin', 'admin')
                  AND u.status = 'active'
                  AND coalesce(u.email, '') <> ''
                ORDER BY CASE m.role WHEN 'super_admin' THEN 0 ELSE 1 END, u.id
                LIMIT %s""",
            (workspace_id, workspace_id, int(limit)),
        ).fetchall()
    return [
        {"email": r["email"], "name": r.get("display_name") or "", "role": r["role"]}
        for r in rows
    ]


def list_sessions(user_id: int, *, live_only: bool = True) -> List[Dict[str, Any]]:
    cond = "AND revoked_at IS NULL AND expires_at > now()" if live_only else ""
    with db.connection() as con:
        return con.execute(
            f"""SELECT id, remember, created_at, last_seen_at, expires_at,
                       absolute_expires_at, revoked_at, user_agent, ip
                FROM auth_sessions WHERE user_id = %s {cond}
                ORDER BY last_seen_at DESC""",
            (user_id,),
        ).fetchall()


def prune_expired_sessions(older_than_days: int = 30) -> int:
    """Housekeeping: rows dead longer than the window carry no audit value the
    audit_events table does not already hold."""
    with db.connection() as con:
        cur = con.execute(
            """DELETE FROM auth_sessions
               WHERE (revoked_at IS NOT NULL OR expires_at < now())
                 AND GREATEST(coalesce(revoked_at, 'epoch'), expires_at)
                     < now() - make_interval(days => %s)""",
            (older_than_days,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

def create_invitation(
    *,
    workspace_id: str,
    email: str,
    name: str,
    role: str,
    token_hash: str,
    invited_by: int,
    ttl: timedelta,
) -> Dict[str, Any]:
    Role(role)
    with db.connection() as con:
        # One live invitation per address: re-inviting supersedes (revokes)
        # any earlier pending invite instead of leaving two valid tokens.
        con.execute(
            """UPDATE workspace_invitations SET revoked_at = now()
               WHERE workspace_id = %s AND lower(email) = lower(%s)
                 AND accepted_at IS NULL AND revoked_at IS NULL""",
            (workspace_id, email.strip()),
        )
        return con.execute(
            """INSERT INTO workspace_invitations
               (id, workspace_id, email, name, role, token_hash, invited_by, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                _new_id(),
                workspace_id,
                email.strip(),
                name.strip(),
                role,
                token_hash,
                invited_by,
                _now() + ttl,
            ),
        ).fetchone()


def get_invitation_by_token_hash(token_hash: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            "SELECT * FROM workspace_invitations WHERE token_hash = %s", (token_hash,)
        ).fetchone()


def get_invitation(invitation_id: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            "SELECT * FROM workspace_invitations WHERE id = %s", (invitation_id,)
        ).fetchone()


def list_invitations(workspace_id: str) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            """SELECT i.*, u.display_name AS invited_by_name
               FROM workspace_invitations i
               LEFT JOIN users u ON u.id = i.invited_by
               WHERE i.workspace_id = %s
               ORDER BY i.created_at DESC LIMIT 200""",
            (workspace_id,),
        ).fetchall()


def revoke_invitation(invitation_id: str) -> bool:
    with db.connection() as con:
        cur = con.execute(
            """UPDATE workspace_invitations SET revoked_at = now()
               WHERE id = %s AND accepted_at IS NULL AND revoked_at IS NULL""",
            (invitation_id,),
        )
        return cur.rowcount > 0


def accept_invitation(
    invitation_id: str,
    *,
    display_name: str,
    password_hash: str,
) -> Optional[Dict[str, Any]]:
    """Single-use, transactionally: burn the invite, create (or claim) the
    account, create the membership — all or nothing. Returns the user row, or
    None when the invitation was no longer acceptable (raced/expired/revoked).
    """
    with db.connection() as con:
        with con.transaction():
            inv = con.execute(
                """SELECT * FROM workspace_invitations
                   WHERE id = %s AND accepted_at IS NULL AND revoked_at IS NULL
                     AND expires_at > now()
                   FOR UPDATE""",
                (invitation_id,),
            ).fetchone()
            if inv is None:
                return None
            user = con.execute(
                "SELECT * FROM users WHERE lower(email) = lower(%s)", (inv["email"],)
            ).fetchone()
            if user is None:
                # Username is the email — the column is NOT NULL UNIQUE and
                # login is by email; a separate handle buys nothing here.
                user = con.execute(
                    """INSERT INTO users
                       (username, email, display_name, password_hash, status, created_at,
                        password_changed_at)
                       VALUES (%s, %s, %s, %s, 'active', now(), now()) RETURNING *""",
                    (
                        inv["email"].strip().lower(),
                        inv["email"].strip(),
                        display_name.strip() or inv["name"] or inv["email"],
                        password_hash,
                    ),
                ).fetchone()
            else:
                # Invited an address that already has an account (e.g. a
                # deactivated ex-member re-invited): claim it — set the new
                # credentials and reactivate.
                user = con.execute(
                    """UPDATE users SET display_name = %s, password_hash = %s,
                           status = 'active', password_changed_at = now()
                       WHERE id = %s RETURNING *""",
                    (
                        display_name.strip() or user["display_name"] or inv["name"],
                        password_hash,
                        user["id"],
                    ),
                ).fetchone()
            con.execute(
                """INSERT INTO workspace_memberships (workspace_id, user_id, role)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
                (inv["workspace_id"], user["id"], inv["role"]),
            )
            con.execute(
                """UPDATE workspace_invitations
                   SET accepted_at = now(), accepted_user_id = %s WHERE id = %s""",
                (user["id"], invitation_id),
            )
            return user


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def record_audit(
    *,
    workspace_id: Optional[str],
    actor_user_id: Optional[int],
    action: str,
    target_user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    ip: str = "",
    user_agent: str = "",
) -> None:
    with db.connection() as con:
        con.execute(
            """INSERT INTO audit_events
               (workspace_id, actor_user_id, action, target_user_id,
                resource_type, resource_id, meta, ip, user_agent)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                workspace_id,
                actor_user_id,
                action,
                target_user_id,
                resource_type,
                str(resource_id)[:300] if resource_id is not None else None,
                Jsonb(meta) if meta else None,
                ip[:100],
                user_agent[:400],
            ),
        )


def list_audit_events(
    workspace_id: str,
    *,
    action: str = "",
    actor: Optional[int] = None,
    target: Optional[int] = None,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Keyset-paginated (newest first) — the audit log grows without bound and
    OFFSET pagination degrades linearly."""
    where = ["e.workspace_id = %s"]
    args: List[Any] = [workspace_id]
    if action:
        where.append("e.action = %s")
        args.append(action)
    if actor is not None:
        where.append("e.actor_user_id = %s")
        args.append(actor)
    if target is not None:
        where.append("e.target_user_id = %s")
        args.append(target)
    if before_id is not None:
        where.append("e.id < %s")
        args.append(before_id)
    with db.connection() as con:
        return con.execute(
            f"""SELECT e.*, a.display_name AS actor_name, a.email AS actor_email,
                       t.display_name AS target_name, t.email AS target_email
                FROM audit_events e
                LEFT JOIN users a ON a.id = e.actor_user_id
                LEFT JOIN users t ON t.id = e.target_user_id
                WHERE {' AND '.join(where)}
                ORDER BY e.id DESC LIMIT %s""",
            args + [min(int(limit), 200)],
        ).fetchall()


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

def throttle_check(key: str) -> Optional[datetime]:
    """The lockout deadline when this key is currently locked, else None."""
    with db.connection() as con:
        row = con.execute(
            "SELECT locked_until FROM login_throttle WHERE key = %s", (key,)
        ).fetchone()
    until = row["locked_until"] if row else None
    return until if until is not None and until > _now() else None


def throttle_failure(
    key: str, *, window_seconds: int, max_fails: int, lock_seconds: int
) -> Optional[datetime]:
    """Register one failure; returns the lockout deadline if this crossed the
    line. Fixed window, atomic upsert — restarts do not forgive an attacker.
    Lockouts are short (minutes): brute force is throttled without handing an
    attacker a permanent denial-of-service button for any email they type."""
    with db.connection() as con:
        row = con.execute(
            """INSERT INTO login_throttle (key, fails, window_start)
               VALUES (%s, 1, now())
               ON CONFLICT (key) DO UPDATE SET
                 fails = CASE WHEN login_throttle.window_start
                                   < now() - make_interval(secs => %s)
                              THEN 1 ELSE login_throttle.fails + 1 END,
                 window_start = CASE WHEN login_throttle.window_start
                                          < now() - make_interval(secs => %s)
                                     THEN now() ELSE login_throttle.window_start END
               RETURNING fails""",
            (key, window_seconds, window_seconds),
        ).fetchone()
        if row is not None and int(row["fails"]) >= max_fails:
            locked = con.execute(
                """UPDATE login_throttle
                   SET locked_until = now() + make_interval(secs => %s), fails = 0,
                       window_start = now()
                   WHERE key = %s RETURNING locked_until""",
                (lock_seconds, key),
            ).fetchone()
            return locked["locked_until"] if locked else None
    return None


def throttle_clear(key: str) -> None:
    with db.connection() as con:
        con.execute("DELETE FROM login_throttle WHERE key = %s", (key,))


# ---------------------------------------------------------------------------
# Report files + preferences
# ---------------------------------------------------------------------------

def bind_report(filename: str, user_id: int, conversation_id: Optional[str]) -> None:
    with db.connection() as con:
        con.execute(
            """INSERT INTO report_files (filename, user_id, conversation_id)
               VALUES (%s, %s, %s)
               ON CONFLICT (filename) DO NOTHING""",
            (filename, user_id, conversation_id),
        )


def report_owner(filename: str) -> Optional[int]:
    with db.connection() as con:
        row = con.execute(
            "SELECT user_id FROM report_files WHERE filename = %s", (filename,)
        ).fetchone()
    return int(row["user_id"]) if row else None


def list_report_files(user_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            """SELECT filename, conversation_id, created_at FROM report_files
               WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user_id, limit),
        ).fetchall()


def claim_unbound_reports(filenames: List[str], user_id: int) -> int:
    """Bootstrap-time adoption: files that predate the ownership table belong
    to the pre-auth local operator, who is the person running bootstrap."""
    n = 0
    with db.connection() as con:
        for name in filenames:
            cur = con.execute(
                """INSERT INTO report_files (filename, user_id)
                   VALUES (%s, %s) ON CONFLICT (filename) DO NOTHING""",
                (name, user_id),
            )
            n += cur.rowcount
    return n


def get_preferences(user_id: int) -> Dict[str, Any]:
    with db.connection() as con:
        row = con.execute(
            "SELECT prefs FROM user_preferences WHERE user_id = %s", (user_id,)
        ).fetchone()
    return dict(row["prefs"]) if row and row["prefs"] else {}


def set_preferences(user_id: int, prefs: Dict[str, Any]) -> None:
    with db.connection() as con:
        con.execute(
            """INSERT INTO user_preferences (user_id, prefs, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (user_id) DO UPDATE
                 SET prefs = EXCLUDED.prefs, updated_at = now()""",
            (user_id, Jsonb(prefs)),
        )


# ---------------------------------------------------------------------------
# Admin content queries (the audited workspace-content viewer)
# ---------------------------------------------------------------------------

def admin_user_conversations(
    user_id: int, *, limit: int = 50, offset: int = 0
) -> Tuple[List[Dict[str, Any]], int]:
    """A member's conversations with message counts, for the admin viewer.
    Pagination is mandatory — never every conversation at once."""
    with db.connection() as con:
        total = con.execute(
            "SELECT count(*) AS n FROM conversations WHERE user_id = %s", (user_id,)
        ).fetchone()
        rows = con.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at, c.pinned, c.archived,
                      (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id)
                          AS message_count
               FROM conversations c
               WHERE c.user_id = %s
               ORDER BY c.updated_at DESC, c.seq DESC
               LIMIT %s OFFSET %s""",
            (user_id, min(int(limit), 100), offset),
        ).fetchall()
    return rows, int(total["n"]) if total else 0


def admin_conversation_messages(conversation_id: str) -> List[Dict[str, Any]]:
    """The full thread WITH timestamps, for the audited admin viewer.

    Not db.list_messages: that shape deliberately omits created_at (the
    user-facing thread keys on ids), while oversight needs to see WHEN each
    message was written.
    """
    with db.connection() as con:
        return con.execute(
            """SELECT id, role, content, meta, created_at FROM messages
               WHERE conversation_id = %s ORDER BY id""",
            (conversation_id,),
        ).fetchall()


def admin_user_uploads(
    user_id: int, *, limit: int = 50, offset: int = 0
) -> Tuple[List[Dict[str, Any]], int]:
    """Uploads across all of a member's conversations. Uploads carry no
    user_id column; ownership is the conversations join — the SAME join the
    normal-user path trusts, so the two can never disagree."""
    with db.connection() as con:
        total = con.execute(
            """SELECT count(*) AS n FROM uploads u
               JOIN conversations c ON c.id = u.conversation_id
               WHERE c.user_id = %s""",
            (user_id,),
        ).fetchone()
        rows = con.execute(
            """SELECT u.id, u.conversation_id, u.filename, u.bytes, u.status,
                      u.created_at, c.title AS conversation_title
               FROM uploads u JOIN conversations c ON c.id = u.conversation_id
               WHERE c.user_id = %s
               ORDER BY u.created_at DESC
               LIMIT %s OFFSET %s""",
            (user_id, min(int(limit), 100), offset),
        ).fetchall()
    return rows, int(total["n"]) if total else 0


def admin_user_overview(user_id: int) -> Dict[str, Any]:
    """Counters for the admin user-detail header."""
    with db.connection() as con:
        row = con.execute(
            """SELECT
                 (SELECT count(*) FROM conversations WHERE user_id = %(u)s) AS conversations,
                 (SELECT count(*) FROM messages m JOIN conversations c
                    ON c.id = m.conversation_id WHERE c.user_id = %(u)s) AS messages,
                 (SELECT count(*) FROM uploads up JOIN conversations c
                    ON c.id = up.conversation_id WHERE c.user_id = %(u)s) AS uploads,
                 (SELECT count(*) FROM report_files WHERE user_id = %(u)s) AS reports,
                 (SELECT count(*) FROM user_facts WHERE user_id = %(u)s) AS memory_facts,
                 (SELECT count(*) FROM research_runs WHERE user_id = %(u)s) AS research_runs
            """,
            {"u": user_id},
        ).fetchone()
    return dict(row) if row else {}


def workspace_overview(workspace_id: str) -> Dict[str, Any]:
    """Counters for the admin Overview page. One round trip."""
    with db.connection() as con:
        row = con.execute(
            """SELECT
                 (SELECT count(*) FROM workspace_memberships m JOIN users u
                    ON u.id = m.user_id
                    WHERE m.workspace_id = %(w)s AND u.status = 'active') AS active_members,
                 (SELECT count(*) FROM workspace_memberships m JOIN users u
                    ON u.id = m.user_id
                    WHERE m.workspace_id = %(w)s AND u.status = 'disabled') AS disabled_members,
                 (SELECT count(*) FROM workspace_invitations
                    WHERE workspace_id = %(w)s AND accepted_at IS NULL
                      AND revoked_at IS NULL AND expires_at > now()) AS pending_invites,
                 (SELECT count(*) FROM conversations) AS conversations,
                 (SELECT count(*) FROM messages) AS messages,
                 (SELECT count(*) FROM auth_sessions
                    WHERE revoked_at IS NULL AND expires_at > now()) AS live_sessions,
                 (SELECT count(*) FROM audit_events WHERE workspace_id = %(w)s) AS audit_events
            """,
            {"w": workspace_id},
        ).fetchone()
    return dict(row) if row else {}
