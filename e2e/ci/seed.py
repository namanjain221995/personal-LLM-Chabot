"""Seed ONE account into the throwaway CI stack. Runs inside the orchestrator
container, where `app` is importable and the database is already migrated.

    printf '%s\n' "$password" \
      | docker exec -i <container> python3 -c "$(cat e2e/ci/seed.py)" <username> <role>

THE PASSWORD ARRIVES ON STDIN, AND ONLY ON STDIN.

  * NOT in argv. `docker exec -i C python3 - name password` puts the password
    in the docker CLIENT's /proc/<pid>/cmdline, which is world-readable for
    the length of the exec, and in the shell history of whoever typed it.
  * NOT in the environment. `docker exec -e PASSWORD=... ` moves the same
    value from `ps` into `docker inspect`, which is no better: every process
    that can talk to the docker socket can read it back for as long as the
    exec exists.

The program text goes through `-c`, so the only thing in argv is this source,
the username and the role — none of which is a secret.

Exits 0 on success, 2 on a usage error, 1 when the seed did not happen.
"""

import sys


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: seed.py <username> <member|admin|super_admin>  (password on stdin)\n")
        return 2
    name, role = sys.argv[1], sys.argv[2]
    if role not in ("member", "admin", "super_admin"):
        sys.stderr.write(f"seed: {role!r} is not a role\n")
        return 2

    password = sys.stdin.readline().rstrip("\n")
    if len(password) < 12:
        # A blank line means the caller's plumbing broke. Refusing here beats
        # creating an account whose password is the empty string on a stack
        # that is about to have a public API key minted against it.
        sys.stderr.write("seed: no password on stdin (or shorter than 12 characters)\n")
        return 1

    from app import db
    from app.authn import passwords, store
    from app.config import settings

    row = db.get_user_by_username(name)
    if row is None:
        try:
            db.create_user(name, "!e2e-ci")
        except db.IntegrityError:
            pass
        row = db.get_user_by_username(name)
    if row is None:
        sys.stderr.write(f"seed: could not create or find {name}\n")
        return 1
    uid = int(row["id"])

    store.set_credentials(
        uid,
        password_hash=passwords.hash_password(password),
        email=f"{name}@test.local",
        display_name=name,
    )
    workspace = store.ensure_workspace(settings.workspace_name)
    ws = workspace["id"]  # a text id, not an integer
    store.upsert_membership(ws, uid, role)
    # The suite attaches documents on both accounts (uploads runs as the
    # member, the console's playground as the admin). Video stays OFF: the CI
    # stub engine does not pretend to analyse an MP4.
    store.set_member_feature_overrides(ws, uid, {"attachments": True, "video_analysis": False})
    # The password is never printed, not even truncated.
    sys.stdout.write(f"seeded user {uid} {name}@test.local as {role}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
