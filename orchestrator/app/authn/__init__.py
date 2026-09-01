"""Enterprise identity: sessions, workspaces, RBAC, invitations, audit.

The package is deliberately split by concern — passwords, sessions, RBAC,
invitations, audit, throttling, the auth API and the admin API each live in
their own module — so no file grows into a monolith and each piece can be
tested alone.

`app/auth.py` (the module every existing route imports `require_user` from)
is the COMPATIBILITY SURFACE over this package: its functions now resolve the
session cookie instead of a hard-coded local account, and its signatures are
unchanged so history/uploads/memory routes did not have to be edited to become
enforced.
"""
