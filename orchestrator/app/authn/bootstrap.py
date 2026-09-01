"""First SUPER_ADMIN bootstrap + identity baseline.

Two jobs:

1. `ensure_identity_baseline()` — called from the FastAPI lifespan on every
   start. Creates the workspace if none exists and gives every user a
   membership (role: member). Idempotent, cheap, and it is what migrates a
   pre-auth install: the legacy local account becomes a member with an
   UNUSABLE password — present, data intact, unable to log in — until
   bootstrap grants credentials.

2. `python -m app.authn.bootstrap --email ... --name ...` — run inside the
   orchestrator container by `./techsara auth bootstrap`. Establishes (or
   updates) the first SUPER_ADMIN:
     - adopts the LEGACY LOCAL ACCOUNT when one exists (the oldest user)
       so every existing conversation/upload/memory row keeps its owner;
     - otherwise creates a fresh account;
     - claims any report files that predate ownership tracking;
     - never prints, logs, or stores a plaintext password anywhere but the
       interactive prompt's memory.

The password comes from the AUTH_BOOTSTRAP_PASSWORD environment variable or
an interactive no-echo prompt — never from argv (visible in `ps`/shell
history) and never hard-coded.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

from .. import db
from ..config import settings
from . import passwords, store
from .rbac import Role


def ensure_identity_baseline() -> None:
    """Workspace + memberships exist for every user. Safe to run always."""
    workspace = store.ensure_workspace(settings.workspace_name)
    with db.connection() as con:
        orphans = con.execute(
            """SELECT u.id FROM users u
               LEFT JOIN workspace_memberships m ON m.user_id = u.id
               WHERE m.user_id IS NULL"""
        ).fetchall()
    for row in orphans:
        store.upsert_membership(workspace["id"], int(row["id"]), Role.MEMBER.value)


def _legacy_local_user() -> Optional[dict]:
    """The account whose data a fresh bootstrap should adopt: the OLDEST user
    that has never been given an email (i.e. predates real login)."""
    with db.connection() as con:
        return con.execute(
            """SELECT * FROM users WHERE email IS NULL
               ORDER BY created_at, id LIMIT 1"""
        ).fetchone()


def bootstrap_super_admin(
    *, email: str, name: str, password: str, adopt_legacy: bool = True
) -> dict:
    """Create or promote the first SUPER_ADMIN. Returns a summary dict.

    Deliberately NOT limited to empty installs: re-running updates the same
    account's password (recovery path for a locked-out owner with shell
    access — equivalent power to the DB itself, so no privilege is gained).
    """
    problem = passwords.validate_new_password(password)
    if problem:
        raise SystemExit(f"password rejected: {problem}")
    email = email.strip()
    if "@" not in email:
        raise SystemExit("that does not look like an email address")

    db.init_schema()
    ensure_identity_baseline()
    workspace = store.ensure_workspace(settings.workspace_name)
    password_hash = passwords.hash_password(password)

    existing = store.get_user_by_email(email)
    adopted = False
    if existing is not None:
        user = existing
        store.set_credentials(
            int(user["id"]), display_name=name or None, password_hash=password_hash
        )
    else:
        legacy = _legacy_local_user() if adopt_legacy else None
        if legacy is not None:
            # Adopt: the pre-auth account's id keeps every FK it ever owned.
            store.set_credentials(
                int(legacy["id"]),
                email=email,
                display_name=name or legacy["username"],
                password_hash=password_hash,
            )
            user = store.get_user(int(legacy["id"]))
            adopted = True
        else:
            with db.connection() as con:
                user = con.execute(
                    """INSERT INTO users
                       (username, email, display_name, password_hash, status,
                        created_at, password_changed_at)
                       VALUES (%s, %s, %s, %s, 'active', now(), now())
                       RETURNING *""",
                    (email.lower(), email, name or email, password_hash),
                ).fetchone()

    store.set_status(int(user["id"]), "active")
    store.upsert_membership(workspace["id"], int(user["id"]), Role.SUPER_ADMIN.value)

    # Files generated before ownership tracking belong to the pre-auth
    # operator — the person running this command.
    from ..core.report_paths import list_reports

    try:
        on_disk = [r["filename"] for r in list_reports(settings.reports_dir)]
        claimed = store.claim_unbound_reports(on_disk, int(user["id"]))
    except Exception:
        claimed = 0

    store.record_audit(
        workspace_id=workspace["id"],
        actor_user_id=int(user["id"]),
        action="super_admin_bootstrapped",
        target_user_id=int(user["id"]),
        meta={"adopted_legacy_account": adopted, "reports_claimed": claimed},
    )
    return {
        "user_id": int(user["id"]),
        "email": email,
        "workspace": workspace["name"],
        "adopted_legacy_account": adopted,
        "reports_claimed": claimed,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.authn.bootstrap",
        description="Establish the first SUPER_ADMIN of the TechSara workspace.",
    )
    parser.add_argument("--email", required=True, help="the administrator's email")
    parser.add_argument("--name", default="", help="display name (optional)")
    parser.add_argument(
        "--no-adopt",
        action="store_true",
        help="do NOT adopt the legacy local account's data; create a new user",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("AUTH_BOOTSTRAP_PASSWORD", "")
    if not password:
        if not sys.stdin.isatty():
            print(
                "no TTY and AUTH_BOOTSTRAP_PASSWORD is not set — refusing to "
                "read a password from a pipe",
                file=sys.stderr,
            )
            return 2
        password = getpass.getpass("New SUPER_ADMIN password: ")
        confirm = getpass.getpass("Repeat it: ")
        if password != confirm:
            print("passwords did not match", file=sys.stderr)
            return 2

    result = bootstrap_super_admin(
        email=args.email,
        name=args.name,
        password=password,
        adopt_legacy=not args.no_adopt,
    )
    print(
        f"SUPER_ADMIN ready: {result['email']} in workspace "
        f"“{result['workspace']}”"
        + (" (adopted the existing local account's data)"
           if result["adopted_legacy_account"] else "")
        + (f"; claimed {result['reports_claimed']} existing report file(s)"
           if result["reports_claimed"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
