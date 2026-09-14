#!/usr/bin/env python3
"""Mint the two test keys the conformance suite needs, through the console API.

    python tools/provision_key.py \
        --origin http://127.0.0.1:8081 \
        --email e2e-devadmin@example.test \
        --password-file /path/to/password-file \
        --out keys.json

WHY A SCRIPT (2026-09-13): a release manager should not hand-craft keys in a
browser before every run, and the suite needs TWO of them — a key with the
server's default scopes, and a key that holds only `models.read`, which is the
only way to prove the 403 `insufficient_scope` path without guessing at a
revoked or foreign key.

What it does, and nothing else:
  1. POST {origin}/auth/login with the console account;
  2. POST /admin/api/developers/projects      (environment "test");
  3. POST /admin/api/developers/projects/{id}/keys  with NO scopes field, so
     the server applies its own DEFAULT_SCOPES — the suite then tests what a
     developer who clicks "Create key" actually gets, on any server version;
  4. POST .../keys with scopes ["models.read"] (the limited key);
  5. with --lift-limits only: PUT .../limits with generous ceilings (below);
     a refusal there still writes the keys and exits 2;
  6. writes {"base_url", "project_id", "api_key", "limited_api_key",
     "scopes", "limited_scopes"} to --out with mode 0600. The suite reads
     "scopes": a built feature whose scope the key lacks is SKIP, not FAIL.

RUN IT AGAIN AFTER EVERY REBUILD THAT ADDS SCOPES. CONTRACT-3 §7: a stored key
keeps the scopes it was minted with, so a key made before 2026-09-13 holds no
embeddings.write, rerank.write or audio.write even on a server that now grants
them by default.

--lift-limits exists for a stack that still runs with
PUBLIC_API_ENFORCE_LIMITS=true (the owner's decision is limits OFF, CONTRACT-3
§12.1). On such a stack a conformance run spends the default 60 requests and
60,000 reserved output tokens a minute within seconds and every later test
reports a 429 instead of the behaviour it meant to check. It needs the
`api.limits.manage` capability and touches only the project this run created.
The limits_off feature test still reports whether the deployment sends
RateLimit headers at all, so lifting a project's ceilings hides nothing.

The secrets are written ONLY to --out. Nothing secret is printed: the terminal
sees the project id, the key prefixes and the scope lists.

`--origin` is the ORCHESTRATOR (or the web front door) — the console API is not
under /v1. The session cookie is `Secure`, and httpx will not replay a Secure
cookie over plain http://, so it is pinned as a header (the approach
scripts/devapi_smoke.py documents). No `Origin` header is sent: the CSRF layer
refuses a non-frontend Origin on a cookie request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


def _fail(message: str, response: httpx.Response | None = None) -> "NoReturn":  # type: ignore[name-defined]
    detail = ""
    if response is not None:
        detail = f" (HTTP {response.status_code}: {response.text[:300]})"
    raise SystemExit(f"provision_key: {message}{detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", required=True, help="orchestrator or front-door origin, e.g. http://127.0.0.1:8081")
    parser.add_argument("--email", required=True, help="a console account holding api.keys.create")
    parser.add_argument("--password-file", required=True, help="file whose first line is the password")
    parser.add_argument("--out", required=True, help="where to write the keys (mode 0600)")
    parser.add_argument("--project-name", default=None, help="default conformance-<epoch>")
    parser.add_argument(
        "--lift-limits",
        action="store_true",
        help="raise the new project's rate/token/concurrency ceilings (for stacks with PUBLIC_API_ENFORCE_LIMITS=true)",
    )
    args = parser.parse_args()

    origin = args.origin.rstrip("/")
    password = Path(args.password_file).read_text(encoding="utf-8").splitlines()[0]
    name = args.project_name or f"conformance-{int(time.time())}"

    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{origin}/auth/login", json={"email": args.email, "password": password})
        if r.status_code != 200:
            _fail("login failed", r)
        cookie = r.headers.get("set-cookie", "").split(";", 1)[0].strip()
        if "=" not in cookie:
            _fail("login answered 200 without a session cookie")
        client.headers["Cookie"] = cookie

        console = f"{origin}/admin/api/developers"
        r = client.post(f"{console}/projects", json={"name": name, "environment": "test"})
        if r.status_code not in (200, 201):
            _fail("project creation failed", r)
        project_id = r.json()["project"]["id"]

        # Keys first, limits last, and every failure from here on names the
        # project: the 2026-09-13 e2e run lifted limits before creating keys,
        # got a 404 and left a project with no keys and no id on screen.
        where = f"project {project_id} ({name}) was created; remove it from the console if unwanted"
        r = client.post(f"{console}/projects/{project_id}/keys", json={"name": "conformance (default scopes)"})
        if r.status_code not in (200, 201):
            _fail(f"default-scope key creation failed — {where}", r)
        full = r.json()

        r = client.post(
            f"{console}/projects/{project_id}/keys",
            json={"name": "conformance (models.read only)", "scopes": ["models.read"]},
        )
        if r.status_code not in (200, 201):
            _fail(f"limited key creation failed — {where}", r)
        limited = r.json()

        lift_error = None
        if args.lift_limits:
            r = client.put(
                f"{console}/projects/{project_id}/limits",
                json={
                    "rpm": 100_000,
                    "input_tpm": 100_000_000,
                    "output_tpm": 100_000_000,
                    "max_concurrency": 1_000,
                    "daily_token_quota": 10_000_000_000,
                },
            )
            if r.status_code == 200:
                print(f"limits    {r.json().get('limits')}")
            else:
                lift_error = f"lifting the project's limits failed (HTTP {r.status_code}: {r.text[:300]})"

    out = {
        "base_url": f"{origin}/v1",
        "project_id": project_id,
        "api_key": full["secret"],
        "limited_api_key": limited["secret"],
        "scopes": full["key"].get("scopes"),
        "limited_scopes": limited["key"].get("scopes"),
    }
    path = Path(args.out)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
    os.chmod(path, 0o600)

    print(f"project   {project_id} ({name})")
    print(f"key       {full['secret'][:26]}…  scopes={out['scopes']}")
    print(f"limited   {limited['secret'][:26]}…  scopes={out['limited_scopes']}")
    print(f"written   {path} (0600)")
    if lift_error:
        # The keys are usable; the run will wait out limit 429s instead.
        print(f"provision_key: {lift_error} — the keys above were written anyway", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
